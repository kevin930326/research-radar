"""全文檢索的資料 `paper_search_document`，以及 `api_call` 的結果代碼（Story 4.1、ADR-0005 的
「2026-11 實作更新」）。

- 新增 `paper_search_document`：一篇論文一列（`paper_id` UNIQUE、外鍵到 `paper`，論文刪掉時
  跟著刪），`document` 是 english 設定的 `tsvector`：標題（權重 A）＋目前的摘要
  （`paper_abstract.is_current`，權重 B；沒有目前的摘要就只有標題）。建 GIN 索引。
- 由觸發器維護（不由應用程式寫）：`paper` 新增或改了標題、`paper_abstract` 新增、修改、刪除之後，
  照那篇論文**現在**的標題與目前的摘要整列重算（`refresh_paper_search_document`）。摘要換成另一個來源
  （`is_current` 從一筆換到另一筆）時，兩筆的觸發器各算一次，最後一次看到的就是換好的狀態。
  觸發器函式是 SECURITY DEFINER（以遷移擁有者的權限寫），`search_path` 固定、表名寫全；應用程式
  角色對這張表只有 SELECT（收回 0001 預設給的 INSERT、UPDATE、DELETE），也不能直接呼叫重算函式。
- 被排除的非研究文章照樣有一列：檢索的查詢自己擋 `excluded_reason`（和清單、論文頁同一個條件），
  排除與取消排除都不必動這張表。
- 回填：遷移裡一句 `INSERT … SELECT` 把既有的論文全部算一次（一萬多篇約幾秒）。
- `api_call` 新增 `outcome`（結果代碼，可以是 NULL）：搜尋的查詢向量（`embed_query`）每次都記，
  `succeeded`、`unreachable`（沒有回應，或 408、429、5xx）、`rejected`（其他 4xx）、
  `quota_exhausted`（今天的額度放不下，沒有送出，估計 token 記 0）。既有的列與背景向量化的列
  是 NULL（不回填：從狀態碼看不出「額度用完」）。

和 `radar/repo/retrieval.py`、`radar/repo/api_calls.py` 的表定義一致（`alembic check` 比對）；CHECK
約束 autogenerate 不比，這裡照抄同一份條件。觸發器與函式不在 `Base.metadata` 裡，由 integration
測試核對。可降版（降版會丟掉全文檢索的資料與 `outcome` 欄；重新升版會重算全文檢索的資料，
`outcome` 不回來）。

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

_CREATE_REFRESH = f"""
CREATE FUNCTION public.{_REFRESH}(target uuid) RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
  INSERT INTO public.paper_search_document (paper_id, document)
  {_DOCUMENT_SELECT} WHERE p.id = target
  ON CONFLICT (paper_id) DO UPDATE
    SET document = EXCLUDED.document, updated_at = now();
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
    op.execute(f"REVOKE EXECUTE ON FUNCTION public.{_REFRESH}(uuid) FROM PUBLIC")
    op.execute(_CREATE_PAPER_FUNCTION)
    op.execute(_CREATE_ABSTRACT_FUNCTION)
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
        "outcome IS NULL OR outcome IN ('succeeded', 'unreachable', 'rejected', 'quota_exhausted')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_api_call_outcome", "api_call", type_="check")
    op.drop_column("api_call", "outcome")
    op.execute(f"DROP TRIGGER {_ABSTRACT_TRIGGER} ON public.paper_abstract")
    op.execute(f"DROP TRIGGER {_PAPER_TRIGGER} ON public.paper")
    op.execute(f"DROP FUNCTION public.{_ABSTRACT_FUNCTION}()")
    op.execute(f"DROP FUNCTION public.{_PAPER_FUNCTION}()")
    op.execute(f"DROP FUNCTION public.{_REFRESH}(uuid)")
    op.drop_index("ix_paper_search_document_document", table_name="paper_search_document")
    op.drop_table("paper_search_document")
