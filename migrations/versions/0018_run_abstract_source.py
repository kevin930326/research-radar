"""`derivation_run` 記下送出時用的摘要來源（Story 2.12、FR-11、AD-26、ADR-0044）。

- 新增 `abstract_source text NULL`：這次呼叫送出的摘要是哪個來源（`openalex`／`semantic_scholar`
  ／`uploaded_pdf`）；只看標題的 run 是 NULL。`radar runs remove-by-source` 照它找出要刪的表頭：
  `paper_abstract` 在來源拿掉摘要時整列刪掉（表頭的 `abstract_id` 就接不回來源了），run 自己記著
  才查得到。
- CHECK：值只能是那三個來源；向量化（`task = 'embed'`）先不記（ADR-0044）。
- 回填：表頭（三張）與 `batch_item` 記的 `abstract_id` 還接得到 `paper_abstract` 的 run，補上那一筆
  摘要的來源；接不到的（摘要列已刪）留 NULL，`remove-by-source` 印成「來源不明」。只補還是 NULL
  的列，做兩次結果一樣。

和 `radar/repo/llm_ledger.py` 的表定義一致（`alembic check` 比對；CHECK 約束 autogenerate 不比，
這裡照抄同一份條件）。可降版：降版刪掉這一欄（連同記下的來源）。

版本：0018
上一版：0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

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


def downgrade() -> None:
    op.drop_constraint(
        "ck_derivation_run_abstract_source_not_embed", "derivation_run", type_="check"
    )
    op.drop_constraint("ck_derivation_run_abstract_source", "derivation_run", type_="check")
    op.drop_column("derivation_run", "abstract_source")
