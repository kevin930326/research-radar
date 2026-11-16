"""`derivation_run` 記下送出時用的摘要來源（Story 2.12、FR-11、AD-26、ADR-0044）。

- 新增 `abstract_source text NULL`：這次呼叫送出的摘要是哪個來源（`openalex`／`semantic_scholar`
  ／`uploaded_pdf`）；只看標題的 run 是 NULL。`radar runs remove-by-source` 照它找出要刪的表頭：
  `paper_abstract` 在來源拿掉摘要時整列刪掉（表頭的 `abstract_id` 就接不回來源了），run 自己記著
  才查得到。
- CHECK：值只能是那三個來源；向量化（`task = 'embed'`）先不記（ADR-0044）。
- 回填：表頭（三張）與 `batch_item` 記的 `abstract_id` 還接得到 `paper_abstract` 的 run，補上那一筆
  摘要的來源；接不到的（摘要列已刪）留 NULL，`remove-by-source` 印成「來源不明」。只補還是 NULL
  的列，做兩次結果一樣。
- 回填在途的 run（審查後第 5 點）：還沒有表頭、也沒有 `batch_item` 的 `running`／
  `llm_result_pending`（看摘要、不是向量化），用論文現在的標題與目前摘要、run 記的設定版本重算
  輸入指紋（`domain/fingerprints.input_sha256`，指紋算進每段的來源），和 run 的一樣就補上目前摘要
  的來源；不一樣（輸入已經變了，這些 run 也不會再被接續）留 NULL。
- `embedding.abstract_source text NULL`（審查後第 2 點）：向量化當時用的摘要來源（只看標題的是
  NULL），`remove-by-source` 照它刪向量。CHECK 同上三個值。**既有的向量一律不回填、留 NULL**
  （審查後第三輪）：現在的目前摘要、時間戳記、輸入指紋都只能說明現在的狀態，證明不了向量化當時
  讀的是哪個來源，所以不猜。NULL 的看摘要向量由 `remove-by-source` 保守處理：移除任何一個來源時
  連同它們一起刪掉，下一次 `embed_papers`（每週更新後排入，或 `radar embeddings backfill`）照
  現在的輸入重算並記下來源；只看標題的不動。遷移之後新寫的向量一律記來源。

和 `radar/repo/llm_ledger.py`、`radar/repo/embeddings.py` 的表定義一致（`alembic check` 比對；
CHECK 約束 autogenerate 不比，這裡照抄同一份條件）。可降版：降版刪掉這兩欄（連同記下的來源）。

版本：0018
上一版：0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from radar.domain.ai_outputs import (
    CurrentAbstract,
    InputKind,
    LlmTask,
    NoLlmInputError,
    PaperLlmSource,
    build_llm_input,
)
from radar.domain.fingerprints import input_sha256
from radar.domain.papers import AbstractSource

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 回填：表頭與 batch_item 記的 abstract_id → 那一筆摘要的來源。只補 NULL 的列（重跑結果一樣）。
BACKFILL = (
    "UPDATE derivation_run AS r SET abstract_source = a.source"
    " FROM ("
    " SELECT run_id, abstract_id FROM zh_summary WHERE abstract_id IS NOT NULL"
    " UNION ALL SELECT run_id, abstract_id FROM classification WHERE abstract_id IS NOT NULL"
    " UNION ALL SELECT run_id, abstract_id FROM research_model WHERE abstract_id IS NOT NULL"
    " UNION ALL SELECT run_id, abstract_id FROM batch_item WHERE abstract_id IS NOT NULL"
    ") AS used JOIN paper_abstract AS a ON a.id = used.abstract_id"
    " WHERE r.id = used.run_id AND r.abstract_source IS NULL AND r.task <> 'embed'"
    " AND r.input_kind = 'abstract'"
)


# 在途、還沒有表頭也沒有 batch_item、看摘要的 run，連同論文現在的標題與目前摘要。
IN_FLIGHT = (
    "SELECT r.id, r.paper_id, r.task, r.input_sha256, r.taxonomy_version,"
    " r.context_values_version, p.title, a.id AS abstract_id, a.source, a.content"
    " FROM derivation_run AS r JOIN paper AS p ON p.id = r.paper_id"
    " JOIN paper_abstract AS a ON a.paper_id = r.paper_id AND a.is_current"
    " WHERE r.abstract_source IS NULL AND r.task <> 'embed' AND r.input_kind = 'abstract'"
    " AND (r.status = 'running'"
    " OR (r.status = 'failed' AND r.failure_reason = 'llm_result_pending'))"
    " AND NOT EXISTS (SELECT 1 FROM zh_summary AS h WHERE h.run_id = r.id)"
    " AND NOT EXISTS (SELECT 1 FROM classification AS h WHERE h.run_id = r.id)"
    " AND NOT EXISTS (SELECT 1 FROM research_model AS h WHERE h.run_id = r.id)"
    " AND NOT EXISTS (SELECT 1 FROM batch_item AS b WHERE b.run_id = r.id)"
)


def backfill_in_flight(connection: sa.Connection) -> int:
    """在途的 run 用輸入指紋辨識摘要來源（見模組說明），回補了幾筆。只補還是 NULL 的列。"""
    filled = 0
    for row in connection.execute(sa.text(IN_FLIGHT)).mappings().all():
        source = AbstractSource(row["source"])
        paper = PaperLlmSource(
            paper_id=row["paper_id"],
            title=row["title"],
            abstract=CurrentAbstract(id=row["abstract_id"], source=source, content=row["content"]),
        )
        try:
            current = build_llm_input(paper, InputKind.ABSTRACT)
            fingerprint = input_sha256(
                current.blocks,
                LlmTask(row["task"]),
                taxonomy_version=row["taxonomy_version"],
                context_values_version=row["context_values_version"],
            )
        except NoLlmInputError, ValueError:
            continue
        if fingerprint != row["input_sha256"]:
            continue
        filled += connection.execute(
            sa.text(
                "UPDATE derivation_run SET abstract_source = :source"
                " WHERE id = :id AND abstract_source IS NULL"
            ),
            {"id": row["id"], "source": source.value},
        ).rowcount
    return filled


def upgrade() -> None:
    op.add_column("derivation_run", sa.Column("abstract_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_derivation_run_abstract_source",
        "derivation_run",
        "abstract_source IS NULL"
        " OR abstract_source IN ('openalex', 'semantic_scholar', 'uploaded_pdf')",
    )
    op.create_check_constraint(
        "ck_derivation_run_abstract_source_not_embed",
        "derivation_run",
        "abstract_source IS NULL OR task <> 'embed'",
    )
    op.execute(BACKFILL)
    backfill_in_flight(op.get_bind())
    op.add_column("embedding", sa.Column("abstract_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_embedding_abstract_source",
        "embedding",
        "abstract_source IS NULL"
        " OR abstract_source IN ('openalex', 'semantic_scholar', 'uploaded_pdf')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_embedding_abstract_source", "embedding", type_="check")
    op.drop_column("embedding", "abstract_source")
    op.drop_constraint(
        "ck_derivation_run_abstract_source_not_embed", "derivation_run", type_="check"
    )
    op.drop_constraint("ck_derivation_run_abstract_source", "derivation_run", type_="check")
    op.drop_column("derivation_run", "abstract_source")
