"""view `counted_paper`：哪些論文算數，全系統只有這一個定義（AD-5、ADR-0011「哪些論文算數」、
ADR-0027）。

- 條件：`in_scope` 為真、`excluded_reason` 是空的、`first_snapshot_id` 指向已發佈的快照。
- 欄位明列（不用 `p.*`）：`paper` 之後加欄位不會默默跑進 view，也讓統計查詢只拿得到計數要的欄位。
- 0001 設的預設權限會給 `radar_app` 四種權限，這裡撤掉 INSERT、UPDATE、DELETE，只留 SELECT。
  （view 有 JOIN，PostgreSQL 本來就不讓它自動更新；撤權限是為了之後有人加 INSTEAD OF 觸發器
  或改寫 view 時仍然擋得住。）

view 不在 `Base.metadata` 裡，`alembic check` 不比對它（autogenerate 只看表）。
之後的遷移要對 view 用到的欄位（`paper` 的 `id`、`journal_issn_l`、`publication_year`、
`first_snapshot_id`、`lab_visible`、`in_scope`、`excluded_reason`，`snapshot.status`）改型別或刪除時，
PostgreSQL 會擋下，要在同一個遷移裡先 `DROP VIEW`、改完再重建。

版本：0008
上一版：0007
"""

from collections.abc import Sequence

from alembic import op

from radar.domain.database_requirements import APP_ROLE

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE VIEW counted_paper AS"
        " SELECT p.id, p.journal_issn_l, p.publication_year, p.first_snapshot_id, p.lab_visible"
        " FROM paper AS p"
        " JOIN snapshot AS s ON s.id = p.first_snapshot_id"
        " WHERE p.in_scope AND p.excluded_reason IS NULL AND s.status = 'published'"
    )
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON counted_paper FROM {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP VIEW counted_paper")
