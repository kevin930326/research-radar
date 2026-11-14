"""中文重點的四欄內容（Story 2.4、ADR-0015、ADR-0032）。

- `zh_summary` 加 `research_question`、`method`、`theory`、`findings` 四個可為 NULL 的 text 欄位
  （摘要沒提到的欄位是 NULL，「摘要未提及」由畫面的 i18n 出）。
- CHECK：`rejected` 沒有內容（四欄全是 NULL）；有值時不是空白。

和 `radar/repo/ai_outputs.py` 的表定義一致（`alembic check` 比對）；CHECK 約束 autogenerate 不比，
這裡照抄同一份條件。0010 已經有的列（2.2、2.3 只有表頭）四欄都是 NULL，兩個 CHECK 都成立。

版本：0011
上一版：0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONTENT_COLUMNS = ("research_question", "method", "theory", "findings")


def upgrade() -> None:
    for column in CONTENT_COLUMNS:
        op.add_column("zh_summary", sa.Column(column, sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_zh_summary_rejected_has_no_content",
        "zh_summary",
        "status <> 'rejected' OR (research_question IS NULL AND method IS NULL"
        " AND theory IS NULL AND findings IS NULL)",
    )
    op.create_check_constraint(
        "ck_zh_summary_content_not_blank",
        "zh_summary",
        "(research_question IS NULL OR research_question ~ '[^[:space:]]')"
        " AND (method IS NULL OR method ~ '[^[:space:]]')"
        " AND (theory IS NULL OR theory ~ '[^[:space:]]')"
        " AND (findings IS NULL OR findings ~ '[^[:space:]]')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_zh_summary_content_not_blank", "zh_summary", type_="check")
    op.drop_constraint("ck_zh_summary_rejected_has_no_content", "zh_summary", type_="check")
    for column in reversed(CONTENT_COLUMNS):
        op.drop_column("zh_summary", column)
