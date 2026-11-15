"""分類的標籤（Story 2.5、ADR-0015、ADR-0033）。

- `classification` 加 `dropped_label_count`（可為 NULL：丟掉的清單外標籤數）。CHECK：不是負數；
  `rejected` 沒有計數。
- 新增 `classification_label`：一列一個標籤（`field` 是 `topic` 或 `method`，`key` 是分類表的
  代號），掛在分類表頭底下、不帶自己的狀態。CHECK：`key` 的寫法；`unclassified` 只能是主題。
  UNIQUE（`classification_id`, `field`, `key`）同時當外鍵的索引。

和 `radar/repo/ai_outputs.py` 的表定義一致（`alembic check` 比對）；CHECK 約束 autogenerate 不比，
這裡照抄同一份條件。0011 已經有的分類表頭（2.2 只有表頭）計數是 NULL，兩個 CHECK 都成立。
權限由 0001 的預設權限給（應用程式角色可以新增、讀取）。

版本：0012
上一版：0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("classification", sa.Column("dropped_label_count", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_classification_dropped_label_count_not_negative",
        "classification",
        "dropped_label_count IS NULL OR dropped_label_count >= 0",
    )
    op.create_check_constraint(
        "ck_classification_rejected_has_no_dropped_count",
        "classification",
        "status <> 'rejected' OR dropped_label_count IS NULL",
    )
    op.create_table(
        "classification_label",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("classification_id", sa.Uuid(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_classification_label"),
        sa.ForeignKeyConstraint(
            ["classification_id"],
            ["classification.id"],
            name="fk_classification_label_classification_id_classification",
        ),
        sa.UniqueConstraint(
            "classification_id",
            "field",
            "key",
            name="uq_classification_label_classification_id_field_key",
        ),
        sa.CheckConstraint("field IN ('topic', 'method')", name="ck_classification_label_field"),
        sa.CheckConstraint("key ~ '^[a-z][a-z0-9_]*$'", name="ck_classification_label_key_format"),
        sa.CheckConstraint(
            "field = 'topic' OR key <> 'unclassified'",
            name="ck_classification_label_unclassified_only_topic",
        ),
    )


def downgrade() -> None:
    op.drop_table("classification_label")
    op.drop_constraint(
        "ck_classification_rejected_has_no_dropped_count", "classification", type_="check"
    )
    op.drop_constraint(
        "ck_classification_dropped_label_count_not_negative", "classification", type_="check"
    )
    op.drop_column("classification", "dropped_label_count")
