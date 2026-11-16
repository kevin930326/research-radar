"""Batch 送出：`batch_submission`、`batch_item`（Story 2.9、AD-14、ADR-0004 決定第 2、4 點、
ADR-0041）。

- 新增 `batch_submission`（Batch 的狀態表）：狀態七種（`queued`、`submitting`、`submitted`、
  `ended`、`imported`、`failed`、`needs_attention`）；`weekly_run_id` 可以是 NULL（手動補跑）；
  供應商 batch ID 格式 `^[A-Za-z0-9_-]{1,128}$`、非空時唯一；`queued`／`submitting` 一定沒有、
  `submitted`／`ended`／`imported` 一定有；`failure_reason` 只在 `failed`／`needs_attention` 有。
- 新增 `batch_item`（批次裡的一筆，`custom_id` = `id.hex`）：`run_id` 唯一指到 `derivation_run`；
  沒有 `status`（進度跟著 run）；`abstract_id` 不設外鍵（和表頭一樣）。

和 `radar/repo/llm_batches.py` 的表定義一致（`alembic check` 比對；CHECK 約束 autogenerate 不比，
這裡照抄同一份條件）。可降版：降版刪掉兩張表（連同裡面的資料）。

版本：0016
上一版：0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'queued', 'submitting', 'submitted', 'ended', 'imported', 'failed', 'needs_attention'"


def upgrade() -> None:
    op.create_table(
        "batch_submission",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("weekly_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provider_batch_id", sa.Text(), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("estimated_max_cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_batch_submission"),
        sa.ForeignKeyConstraint(
            ["weekly_run_id"],
            ["weekly_run.id"],
            name="fk_batch_submission_weekly_run_id_weekly_run",
        ),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_batch_submission_status"),
        sa.CheckConstraint(
            "provider_batch_id IS NULL OR provider_batch_id ~ '^[A-Za-z0-9_-]{1,128}$'",
            name="ck_batch_submission_provider_batch_id_format",
        ),
        sa.CheckConstraint(
            "status NOT IN ('queued', 'submitting') OR provider_batch_id IS NULL",
            name="ck_batch_submission_no_provider_batch_id_before_submitted",
        ),
        sa.CheckConstraint(
            "status NOT IN ('submitted', 'ended', 'imported') OR provider_batch_id IS NOT NULL",
            name="ck_batch_submission_provider_batch_id_once_submitted",
        ),
        sa.CheckConstraint(
            "status NOT IN ('submitted', 'ended', 'imported') OR submitted_at IS NOT NULL",
            name="ck_batch_submission_submitted_at_once_submitted",
        ),
        sa.CheckConstraint(
            "submitted_at IS NULL OR provider_batch_id IS NOT NULL",
            name="ck_batch_submission_submitted_at_needs_provider_batch_id",
        ),
        sa.CheckConstraint(
            "(status IN ('failed', 'needs_attention')) = (failure_reason IS NOT NULL)",
            name="ck_batch_submission_failure_reason_iff_failed_or_needs_attention",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR failure_reason ~ '^[a-z][a-z0-9_]*$'",
            name="ck_batch_submission_failure_reason_format",
        ),
        sa.CheckConstraint("item_count >= 0", name="ck_batch_submission_item_count_not_negative"),
        sa.CheckConstraint(
            "estimated_max_cost_usd >= 0",
            name="ck_batch_submission_estimated_max_cost_usd_not_negative",
        ),
    )
    op.create_index("ix_batch_submission_weekly_run_id", "batch_submission", ["weekly_run_id"])
    op.create_index(
        "uq_batch_submission_provider_batch_id",
        "batch_submission",
        ["provider_batch_id"],
        unique=True,
        postgresql_where=sa.text("provider_batch_id IS NOT NULL"),
    )

    op.create_table(
        "batch_item",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("batch_submission_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("abstract_id", sa.Uuid(), nullable=True),
        sa.Column("estimated_max_cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_batch_item"),
        sa.ForeignKeyConstraint(
            ["batch_submission_id"],
            ["batch_submission.id"],
            name="fk_batch_item_batch_submission_id_batch_submission",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["derivation_run.id"], name="fk_batch_item_run_id_derivation_run"
        ),
        sa.UniqueConstraint("run_id", name="uq_batch_item_run_id"),
        sa.CheckConstraint(
            "estimated_max_cost_usd >= 0",
            name="ck_batch_item_estimated_max_cost_usd_not_negative",
        ),
    )
    op.create_index("ix_batch_item_batch_submission_id", "batch_item", ["batch_submission_id"])


def downgrade() -> None:
    op.drop_index("ix_batch_item_batch_submission_id", table_name="batch_item")
    op.drop_table("batch_item")
    op.drop_index("uq_batch_submission_provider_batch_id", table_name="batch_submission")
    op.drop_index("ix_batch_submission_weekly_run_id", table_name="batch_submission")
    op.drop_table("batch_submission")
