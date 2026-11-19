"""示範站可用：論文的授權、出版社與 `demo_eligible`，以及 view `demo_full_paper`（Story 3.7a、
ADR-0007 第 5 點、ADR-0052）。

- `paper` 加四欄：
  - `primary_license text`：出版社自己那一版（OpenAlex `primary_location.license`）的授權原字串；
    NULL＝沒有。CHECK 不是空字串（`ck_paper_primary_license_not_empty`）。
  - `publisher_ids text[] NOT NULL DEFAULT '{}'`、`publisher_names text[] NOT NULL DEFAULT '{}'`：
    那一版的出版社 ID 與名稱（含 lineage）。兩個都是空的＝沒有出版社欄位。
  - `demo_eligible boolean NOT NULL DEFAULT false`：由 `domain/policy.is_demo_eligible` 算好存下。
    既有的列都是 false：還沒用 `radar reparse` 補資料之前，一篇都不算可用。
- view `demo_full_paper`：示範站有完整內容的論文，全系統只有這一個定義。條件：在 `counted_paper`
  裡（計入統計）、`demo_eligible`、有目前用的摘要（`paper_abstract.is_current`），而且那筆摘要的
  來源允許公開。**「摘要來源允許公開」只在這裡定義**：只有 `openalex`（Semantic Scholar 還沒有
  書面授權；`uploaded_pdf` 永遠不是目前的摘要）。
  - 建在 `counted_paper` 上：「計入統計」只有那一個定義。之後重建 `counted_paper` 要先刪掉這個
    view，重建後再建回來。
  - 欄位明列（不用 `*`），撤掉應用程式角色的 INSERT、UPDATE、DELETE（同 0008 的理由）。
- 不加索引：`demo_eligible` 只在這個 view 與之後的匯出用，論文約一萬篇，掃全表就夠快。

和 `radar/repo/papers.py` 的表定義一致（`alembic check` 比對）；CHECK 約束 autogenerate 不比，
這裡照抄同一份條件。可降版（降版會丟掉 view 與四欄的資料，重跑 reparse 補得回來）。

版本：0021
上一版：0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

from radar.domain.database_requirements import APP_ROLE

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_TEXT_ARRAY = sa.text("'{}'::text[]")


def upgrade() -> None:
    op.add_column("paper", sa.Column("primary_license", sa.Text(), nullable=True))
    op.add_column(
        "paper",
        sa.Column(
            "publisher_ids", ARRAY(sa.Text()), server_default=_EMPTY_TEXT_ARRAY, nullable=False
        ),
    )
    op.add_column(
        "paper",
        sa.Column(
            "publisher_names", ARRAY(sa.Text()), server_default=_EMPTY_TEXT_ARRAY, nullable=False
        ),
    )
    op.add_column(
        "paper",
        sa.Column("demo_eligible", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_check_constraint(
        "ck_paper_primary_license_not_empty",
        "paper",
        "primary_license IS NULL OR primary_license <> ''",
    )
    op.execute(
        "CREATE VIEW demo_full_paper AS"
        " SELECT c.id, c.journal_issn_l, c.publication_year, c.first_snapshot_id"
        " FROM counted_paper AS c"
        " JOIN paper AS p ON p.id = c.id"
        " JOIN paper_abstract AS a ON a.paper_id = c.id AND a.is_current"
        " WHERE p.demo_eligible AND a.source IN ('openalex')"
    )
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON demo_full_paper FROM {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP VIEW demo_full_paper")
    op.drop_constraint("ck_paper_primary_license_not_empty", "paper", type_="check")
    op.drop_column("paper", "demo_eligible")
    op.drop_column("paper", "publisher_names")
    op.drop_column("paper", "publisher_ids")
    op.drop_column("paper", "primary_license")
