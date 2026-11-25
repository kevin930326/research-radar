"""示範站匯出的狀態表 `demo_export`（Story 3.7、AD-14、AD-20、AR-17、ADR-0061）。

- 一列＝一次匯出：`snapshot_id`（外鍵到 `snapshot`）、`sequence`（這個快照的第幾次，匯出編號是
  `<快照編號>-<sequence>`，CHECK ≥ 1）、`status`（`queued`、`running`、`succeeded`、`failed`）、
  `failure_reason`（只有 `failed` 有，是原因代碼）、`created_at`、`updated_at`（NOT NULL，sweep 用
  它判斷卡住）、`started_at`、`finished_at`（結束的一定有）。
- （`snapshot_id`, `sequence`）UNIQUE（也當外鍵的索引）；部分唯一索引
  `uq_demo_export_snapshot_id_in_progress`：同一個快照只能有一筆 `queued` 或 `running`。
- 只建新表，不改任何既有的表。

和 `radar/repo/demo_exports.py` 的表定義一致（`alembic check` 比對）；CHECK 約束 autogenerate
不比，這裡照抄同一份條件。可降版（降版會丟掉這張表與裡面的紀錄；匯出的檔案不受影響）。

版本：0022
上一版：0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "demo_export",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_demo_export"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["snapshot.id"], name="fk_demo_export_snapshot_id_snapshot"
        ),
        sa.UniqueConstraint("snapshot_id", "sequence", name="uq_demo_export_snapshot_id_sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_demo_export_sequence_positive"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_demo_export_status"
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)",
            name="ck_demo_export_failure_reason_iff_failed",
        ),
        sa.CheckConstraint(
            "status NOT IN ('succeeded', 'failed') OR finished_at IS NOT NULL",
            name="ck_demo_export_finished_at_once_finished",
        ),
    )
    op.create_index(
        "uq_demo_export_snapshot_id_in_progress",
        "demo_export",
        ["snapshot_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_table("demo_export")
