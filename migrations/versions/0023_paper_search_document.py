"""全文檢索的資料 `paper_search_document`，以及 `api_call` 的結果代碼（Story 4.1、ADR-0005 的
「2026-11 實作更新」）。

- 新增 `paper_search_document`：一篇論文一列（`paper_id` UNIQUE、外鍵到 `paper`，論文刪掉時
  跟著刪），`document` 是 english 設定的 `tsvector`：標題（權重 A）＋目前的摘要
  （`paper_abstract.is_current`，權重 B；沒有目前的摘要就只有標題）。建 GIN 索引。
- 由觸發器維護（不由應用程式寫）：`paper` 新增或改了標題、`paper_abstract` 新增、修改、刪除之後，
  照那篇論文**現在**的標題與目前的摘要整列重算（`refresh_paper_search_document`）。摘要換成另一個來源
  （`is_current` 從一筆換到另一筆）時，兩筆的觸發器各算一次，最後一次看到的就是換好的狀態。
  重算先鎖論文那一列（`FOR NO KEY UPDATE`），鎖到之後才另起一句讀標題與摘要：同一篇同時改標題與
  摘要的兩個交易排隊，後到的看得到先到的改動，不會拿舊的內容蓋掉新的（審查 H5）。
  三個函式都是 SECURITY DEFINER（以遷移擁有者的權限寫），`search_path` 固定、表名寫全；應用程式
  角色對這張表只有 SELECT（收回 0001 預設給的 INSERT、UPDATE、DELETE），三個函式都收回 PUBLIC 的
  EXECUTE（審查 M1；觸發器觸發時不檢查 EXECUTE）。
- 被排除的非研究文章照樣有一列：檢索的查詢自己擋 `excluded_reason`（和清單、論文頁同一個條件），
  排除與取消排除都不必動這張表。
- 回填：遷移裡一句 `INSERT … SELECT` 把既有的論文全部算一次（一萬多篇約幾秒）。
- `api_call` 新增 `outcome`（結果代碼，可以是 NULL）：搜尋的查詢向量（`embed_query`）每次都記，
  `succeeded`、`unreachable`（沒有回應，或 408、429、5xx）、`rejected`（其他 4xx）、
  `quota_exhausted`（今天的額度放不下，沒有送出，估計 token 記 0）、`not_sent`（其他沒有送出的
  失敗，例如等額度鎖逾時，估計 token 記 0；審查 H2）。既有的列與背景向量化的列是 NULL（不回填：
  從狀態碼看不出「額度用完」）。
- `embedding_quota_hold` 新增 `operation`（`embed_documents` 或 `embed_query`；既有的列補
  `embed_documents`）：過期的占用轉成用量時照記（審查 H3）。

和 `radar/repo/retrieval.py`、`radar/repo/api_calls.py` 的表定義一致（`alembic check` 比對）；CHECK
約束 autogenerate 不比，這裡照抄同一份條件。觸發器與函式不在 `Base.metadata` 裡，由 integration
測試核對。可降版：降版會丟掉全文檢索的資料、`api_call.outcome` 與 `embedding_quota_hold.operation`。
重新升版會重算全文檢索的資料，但**`outcome` 的紀錄不會回來**（已經發生的搜尋的結果代碼，降版就
永久丟掉；占用的 `operation` 回來時全部是 `embed_documents`）。降版只在開發時用，正式不降版
（和其他加欄位的遷移一樣，例如 0015）。

版本：0023
上一版：0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

from radar.domain.database_requirements import APP_ROLE

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REFRESH = "refresh_paper_search_document"
_PAPER_FUNCTION = "paper_search_document_from_paper"
_ABSTRACT_FUNCTION = "paper_search_document_from_abstract"
_PAPER_TRIGGER = "tg_paper_search_document_from_paper"
_ABSTRACT_TRIGGER = "tg_paper_search_document_from_abstract"

# 論文的全文檢索資料：標題（A）＋目前的摘要（B）；後面接 WHERE 限定要算哪幾篇。
_DOCUMENT_SELECT = (
    "SELECT p.id,"
    " setweight(to_tsvector('pg_catalog.english'::regconfig, p.title), 'A')"
    " || setweight(to_tsvector('pg_catalog.english'::regconfig, COALESCE(a.content, '')), 'B')"
    " FROM public.paper AS p"
    " LEFT JOIN public.paper_abstract AS a ON a.paper_id = p.id AND a.is_current"
)

# 先鎖論文那一列、鎖到之後才另起一句讀標題與摘要（審查 H5）。同一篇的改標題（`UPDATE paper`
# 本身就鎖這一列）和改摘要（這裡鎖）因此排隊，順序固定是「論文 → 全文檢索資料」；後到的那一句
# 在 READ COMMITTED 下等到鎖之後拿新的快照，看得到先到的那一筆已經 commit 的改動。只用一句
# `INSERT … SELECT` 時，讀到的是等鎖之前的快照，後到的會拿舊標題（或舊摘要）蓋掉先到的結果。
# `FOR NO KEY UPDATE`：和 `UPDATE paper`（不改主鍵）互斥，但不擋其他表外鍵檢查的 KEY SHARE。
_CREATE_REFRESH = f"""
CREATE FUNCTION public.{_REFRESH}(target uuid) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
  PERFORM 1 FROM public.paper WHERE id = target FOR NO KEY UPDATE;
  INSERT INTO public.paper_search_document (paper_id, document)
  {_DOCUMENT_SELECT} WHERE p.id = target
  ON CONFLICT (paper_id) DO UPDATE
    SET document = EXCLUDED.document, updated_at = now();
END;
$$
"""

_CREATE_PAPER_FUNCTION = f"""
CREATE FUNCTION public.{_PAPER_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
  PERFORM public.{_REFRESH}(NEW.id);
  RETURN NULL;
END;
$$
"""

_CREATE_ABSTRACT_FUNCTION = f"""
CREATE FUNCTION public.{_ABSTRACT_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    PERFORM public.{_REFRESH}(NEW.paper_id);
  ELSIF TG_OP = 'DELETE' THEN
    PERFORM public.{_REFRESH}(OLD.paper_id);
  ELSE
    PERFORM public.{_REFRESH}(OLD.paper_id);
    IF NEW.paper_id IS DISTINCT FROM OLD.paper_id THEN
      PERFORM public.{_REFRESH}(NEW.paper_id);
    END IF;
  END IF;
  RETURN NULL;
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "paper_search_document",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("document", TSVECTOR(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paper_search_document"),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.id"],
            name="fk_paper_search_document_paper_id_paper",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("paper_id", name="uq_paper_search_document_paper_id"),
    )
    op.create_index(
        "ix_paper_search_document_document",
        "paper_search_document",
        ["document"],
        postgresql_using="gin",
    )
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON paper_search_document FROM {APP_ROLE}")
    op.execute(_CREATE_REFRESH)
    op.execute(_CREATE_PAPER_FUNCTION)
    op.execute(_CREATE_ABSTRACT_FUNCTION)
    # 三個 SECURITY DEFINER 函式都不開放給一般角色（審查 M1）。觸發器觸發時不檢查 EXECUTE 權限
    # （只在 CREATE TRIGGER 時檢查建立的人，也就是遷移擁有者），所以寫入路徑照常重算。
    for signature in (f"{_REFRESH}(uuid)", f"{_PAPER_FUNCTION}()", f"{_ABSTRACT_FUNCTION}()"):
        op.execute(f"REVOKE EXECUTE ON FUNCTION public.{signature} FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER {_PAPER_TRIGGER} AFTER INSERT OR UPDATE OF title ON public.paper"
        f" FOR EACH ROW EXECUTE FUNCTION public.{_PAPER_FUNCTION}()"
    )
    op.execute(
        f"CREATE TRIGGER {_ABSTRACT_TRIGGER} AFTER INSERT OR UPDATE OR DELETE"
        f" ON public.paper_abstract FOR EACH ROW EXECUTE FUNCTION public.{_ABSTRACT_FUNCTION}()"
    )
    # 回填既有的論文（觸發器只管之後的改動）。
    op.execute(f"INSERT INTO public.paper_search_document (paper_id, document) {_DOCUMENT_SELECT}")

    op.add_column("api_call", sa.Column("outcome", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_api_call_outcome",
        "api_call",
        "outcome IS NULL OR outcome IN"
        " ('succeeded', 'unreachable', 'rejected', 'quota_exhausted', 'not_sent')",
    )

    # 占用記下是哪一種請求（審查 H3）：搜尋中斷留下的占用過期後照 `embed_query` 記。既有的占用
    # 都是背景向量化的（0023 之前搜尋還不記帳），用預設值補成 `embed_documents`。不加 CHECK：
    # 0015 的遷移測試逐字核對這張表的 CHECK 只有兩條（既有測試不改）；值由寫入函式
    # `repo/api_calls.create_embedding_quota_hold` 擋，轉成 `api_call` 時還有它的
    # `operation_format`。
    op.add_column(
        "embedding_quota_hold",
        sa.Column(
            "operation",
            sa.Text(),
            server_default=sa.text("'embed_documents'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("embedding_quota_hold", "operation")
    op.drop_constraint("ck_api_call_outcome", "api_call", type_="check")
    op.drop_column("api_call", "outcome")
    op.execute(f"DROP TRIGGER {_ABSTRACT_TRIGGER} ON public.paper_abstract")
    op.execute(f"DROP TRIGGER {_PAPER_TRIGGER} ON public.paper")
    op.execute(f"DROP FUNCTION public.{_ABSTRACT_FUNCTION}()")
    op.execute(f"DROP FUNCTION public.{_PAPER_FUNCTION}()")
    op.execute(f"DROP FUNCTION public.{_REFRESH}(uuid)")
    op.drop_index("ix_paper_search_document_document", table_name="paper_search_document")
    op.drop_table("paper_search_document")
