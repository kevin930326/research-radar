"""每週更新的狀態表與每本期刊的進度（AD-14、ADR-0004、ADR-0014、ADR-0025）。

- `weekly_run`：一次每週更新一列；狀態五種（CHECK），同時只能有一筆 `fetching`（部分唯一索引
  `uq_weekly_run_fetching`）。`updated_at` 每抓完一頁、額度停下時更新，是 sweep 的時間欄。
- `weekly_run_journal`：每本期刊抓到哪一頁（`next_cursor`）、抓完的時間與過濾前後的篇數；
  （`weekly_run_id`, `journal_key`）唯一，外鍵的索引由這個唯一約束提供。沒有 `status` 欄。
- `ingest_staging.kind` 多一種 `weekly_update`（約束同名，先刪再建）。

和 `radar/repo/weekly_runs.py`、`radar/repo/ingest_staging.py` 的表定義一致
（`alembic check` 比對）；CHECK 約束 autogenerate 不比，這裡照抄同一份條件。
新表由 0001 設的預設權限交給 `radar_app`。

版本：0006
上一版：0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COUNTS = ("pages", "fetched", "staged", "excluded", "out_of_scope", "unparseable")


def _timestamp(name: str) -> sa.Column[object]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "weekly_run",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("fetch_finished_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_weekly_run"),
        sa.CheckConstraint(
            "status IN ('fetching', 'published', 'ai_done', 'exported', 'failed')",
            name="ck_weekly_run_status",
        ),
    )
    op.create_index(
        "uq_weekly_run_fetching",
        "weekly_run",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'fetching'"),
    )

    op.create_table(
        "weekly_run_journal",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("weekly_run_id", sa.Uuid(), nullable=False),
        sa.Column("journal_key", sa.Text(), nullable=False),
        sa.Column("next_cursor", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *(
            sa.Column(name, sa.Integer(), server_default=sa.text("0"), nullable=False)
            for name in _COUNTS
        ),
        sa.Column(
            "fetched_by_year",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_weekly_run_journal"),
        sa.ForeignKeyConstraint(
            ["weekly_run_id"],
            ["weekly_run.id"],
            name="fk_weekly_run_journal_weekly_run_id_weekly_run",
        ),
        sa.UniqueConstraint(
            "weekly_run_id",
            "journal_key",
            name="uq_weekly_run_journal_weekly_run_id_journal_key",
        ),
        sa.CheckConstraint(
            "journal_key ~ '^[a-z][a-z0-9_]*$' AND char_length(journal_key) <= 64",
            name="ck_weekly_run_journal_journal_key_format",
        ),
        sa.CheckConstraint(
            "next_cursor IS NULL OR next_cursor <> ''",
            name="ck_weekly_run_journal_next_cursor_not_empty",
        ),
        sa.CheckConstraint(
            " AND ".join(f"{name} >= 0" for name in _COUNTS),
            name="ck_weekly_run_journal_counts_not_negative",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(fetched_by_year) = 'object'",
            name="ck_weekly_run_journal_fetched_by_year_object",
        ),
    )

    op.drop_constraint("ck_ingest_staging_kind", "ingest_staging", type_="check")
    op.create_check_constraint(
        "ck_ingest_staging_kind", "ingest_staging", "kind IN ('reparse', 'weekly_update')"
    )


def downgrade() -> None:
    # 每週更新的暫存列是還沒套用的中間結果，和兩張表一起拿掉，才放得回只有 reparse 的約束。
    op.execute("DELETE FROM ingest_staging WHERE kind = 'weekly_update'")
    op.drop_constraint("ck_ingest_staging_kind", "ingest_staging", type_="check")
    op.create_check_constraint("ck_ingest_staging_kind", "ingest_staging", "kind IN ('reparse')")
    op.drop_table("weekly_run_journal")
    op.drop_index("uq_weekly_run_fetching", table_name="weekly_run")
    op.drop_table("weekly_run")
