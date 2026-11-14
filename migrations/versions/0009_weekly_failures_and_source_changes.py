"""每週更新的失敗次數與論文的變動時間（Story 1.11、ADR-0028）。

- `weekly_run.failure_count`：從建立起累計失敗幾次（NOT NULL、預設 0、CHECK ≥0）；到設定的上限
  （`RADAR_WEEKLY_UPDATE_MAX_FAILURES`）就把那一筆改成 `failed`。
- `paper.source_changed_at`：寫入時 `source_hash` 變了的最後時間；NULL＝寫入以來指紋沒變過。

和 `radar/repo/weekly_runs.py`、`radar/repo/papers.py` 的表定義一致（`alembic check` 比對）；
CHECK 約束 autogenerate 不比，這裡照抄同一份條件。view `counted_paper` 沒有用到這兩欄，不必重建。

版本：0009
上一版：0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "weekly_run",
        sa.Column("failure_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        "ck_weekly_run_failure_count_not_negative", "weekly_run", "failure_count >= 0"
    )
    op.add_column(
        "paper", sa.Column("source_changed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("paper", "source_changed_at")
    op.drop_constraint("ck_weekly_run_failure_count_not_negative", "weekly_run", type_="check")
    op.drop_column("weekly_run", "failure_count")
