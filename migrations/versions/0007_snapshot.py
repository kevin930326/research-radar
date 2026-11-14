"""快照表與論文的 `first_snapshot_id`（AD-19、ADR-0011「快照」、ADR-0026）。

- `snapshot`：每次每週更新套用時建一列；`number` 從 1 遞增（唯一、≥1），`weekly_run_id` 唯一
  （外鍵的索引由這個唯一約束提供）；狀態三種（CHECK）；已發佈的一定有 `as_of`。
  主鍵照慣例是 `uuidv7()`，不用 `number` 當主鍵。
- `paper.first_snapshot_id`：論文第一次被算進哪一個快照（外鍵＋索引 `ix_paper_first_snapshot_id`）。

和 `radar/repo/snapshots.py`、`radar/repo/papers.py` 的表定義一致（`alembic check` 比對）；
CHECK 約束 autogenerate 不比，這裡照抄同一份條件。新表由 0001 設的預設權限交給 `radar_app`。

版本：0007
上一版：0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(name: str) -> sa.Column[object]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "snapshot",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("weekly_run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_snapshot"),
        sa.ForeignKeyConstraint(
            ["weekly_run_id"], ["weekly_run.id"], name="fk_snapshot_weekly_run_id_weekly_run"
        ),
        sa.UniqueConstraint("number", name="uq_snapshot_number"),
        sa.UniqueConstraint("weekly_run_id", name="uq_snapshot_weekly_run_id"),
        sa.CheckConstraint("number >= 1", name="ck_snapshot_number_positive"),
        sa.CheckConstraint(
            "status IN ('building', 'published', 'failed')", name="ck_snapshot_status"
        ),
        sa.CheckConstraint(
            "status <> 'published' OR as_of IS NOT NULL", name="ck_snapshot_published_has_as_of"
        ),
    )

    op.add_column("paper", sa.Column("first_snapshot_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_paper_first_snapshot_id_snapshot", "paper", "snapshot", ["first_snapshot_id"], ["id"]
    )
    op.create_index("ix_paper_first_snapshot_id", "paper", ["first_snapshot_id"])


def downgrade() -> None:
    op.drop_index("ix_paper_first_snapshot_id", table_name="paper")
    op.drop_constraint("fk_paper_first_snapshot_id_snapshot", "paper", type_="foreignkey")
    op.drop_column("paper", "first_snapshot_id")
    op.drop_table("snapshot")
