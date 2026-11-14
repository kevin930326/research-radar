"""原始資料的抓取紀錄 `raw_fetch` 與 API 呼叫紀錄 `api_call`（AD-25、AD-29、ADR-0014、ADR-0021）。

和 `radar/repo/raw_fetches.py`、`radar/repo/api_calls.py` 的表定義一致（`alembic check` 比對）；
CHECK 約束 autogenerate 不比，這裡照抄同一份條件。新表由 0001 設的預設權限交給 `radar_app`。

版本：0004
上一版：0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_fetch",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_key", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_fetch"),
        sa.UniqueConstraint(
            "source",
            "external_key",
            "fetched_at",
            name="uq_raw_fetch_source_external_key_fetched_at",
        ),
        sa.CheckConstraint("source IN ('openalex')", name="ck_raw_fetch_source"),
        sa.CheckConstraint("external_key <> ''", name="ck_raw_fetch_external_key_not_empty"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_raw_fetch_sha256_format"),
    )
    op.create_index("ix_raw_fetch_source_sha256", "raw_fetch", ["source", "sha256"])

    op.create_table(
        "api_call",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("remaining_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("limit_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_call"),
        sa.CheckConstraint("source IN ('openalex')", name="ck_api_call_source"),
        sa.CheckConstraint("operation ~ '^[a-z][a-z0-9_]*$'", name="ck_api_call_operation_format"),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_api_call_http_status_range",
        ),
        sa.CheckConstraint("cost_usd >= 0", name="ck_api_call_cost_usd_not_negative"),
    )
    op.create_index("ix_api_call_source_called_at", "api_call", ["source", "called_at"])


def downgrade() -> None:
    op.drop_index("ix_api_call_source_called_at", table_name="api_call")
    op.drop_table("api_call")
    op.drop_index("ix_raw_fetch_source_sha256", table_name="raw_fetch")
    op.drop_table("raw_fetch")
