"""搜尋的評估題組 `eval_query`、`eval_query_relevant`，與最小版本的 `eval_run`（Story 4.4、FR-26、
FR-27、FR-41、Conventions「評估」）。

- `eval_query`：一題。`query_set`（CHECK `search`／`similar`）、`query_key`（Kevin 題目檔的
  `query_id`，1～64 字）、`query_text`（驗過的查詢字串或正規化後的起點 DOI，1～2000 字）；
  （`query_set`, `query_key`）UNIQUE。
- `eval_query_relevant`：一題的一篇正解，`eval_query_id` 外鍵（題目刪掉時跟著刪）、`relevant_doi`
  （正規化後的 DOI）、`note`（選填，1～500 字）；（`eval_query_id`, `relevant_doi`）UNIQUE（也當外鍵
  的索引）。
- `eval_run`：一次評估。`kind`（CHECK 現在只有 `search`；Epic 8 加 AI 產出的評估時放寬）、
  `query_sets`（`text[]`，至少一個、只能是 `search`／`similar`）、`min_similarities`（`numeric[]`，
  至少一個、不可有 NULL 元素（`ALL` 遇到 NULL 是未知、CHECK 會放行，所以另外用
  `array_position` 擋）、每個在 0（含）到 1（不含））、`embedding_model_id`、`snapshot_id`（外鍵到
  `snapshot`，可以是 NULL，有索引）、`query_count`（≥ 0）、`results`（jsonb）、`started_at`、
  `finished_at`（不早於開始）。應用程式角色收回 UPDATE、DELETE（紀錄只新增）。
- `derivation_run.eval_run_id`（0010）**不在這裡補外鍵**：Story 4.4 先不動，列給 Kevin 決定。
- 只建新表，不改任何既有的表。

和 `radar/repo/evaluation.py` 的表定義一致（`alembic check` 比對）；CHECK 約束 autogenerate 不比，
這裡照抄同一份條件。可降版（降版會丟掉三張表與裡面的題目、評估紀錄；題目檔在 Kevin 的 Mac 上，重新
匯入即可恢復題目，但**評估紀錄（`eval_run`）丟了就回不來**：當時的模型、快照與每題的名次無法從
CSV 重建。降版和其他遷移一樣只在開發時用；真的要降版前先用 `radar backup` 留一份）。

版本：0024
上一版：0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from radar.domain.database_requirements import APP_ROLE

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "eval_query",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("query_set", sa.Text(), nullable=False),
        sa.Column("query_key", sa.Text(), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_query"),
        sa.UniqueConstraint("query_set", "query_key", name="uq_eval_query_query_set_query_key"),
        sa.CheckConstraint("query_set IN ('search', 'similar')", name="query_set"),
        sa.CheckConstraint("char_length(query_key) BETWEEN 1 AND 64", name="query_key_length"),
        sa.CheckConstraint("char_length(query_text) BETWEEN 1 AND 2000", name="query_text_length"),
    )
    op.create_table(
        "eval_query_relevant",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("eval_query_id", sa.Uuid(), nullable=False),
        sa.Column("relevant_doi", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_query_relevant"),
        sa.ForeignKeyConstraint(
            ["eval_query_id"],
            ["eval_query.id"],
            name="fk_eval_query_relevant_eval_query_id_eval_query",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "eval_query_id",
            "relevant_doi",
            name="uq_eval_query_relevant_eval_query_id_relevant_doi",
        ),
        sa.CheckConstraint(
            "char_length(relevant_doi) BETWEEN 1 AND 2000", name="relevant_doi_length"
        ),
        sa.CheckConstraint(
            "note IS NULL OR char_length(note) BETWEEN 1 AND 500", name="note_length"
        ),
    )
    op.create_table(
        "eval_run",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("query_sets", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("min_similarities", postgresql.ARRAY(sa.Numeric()), nullable=False),
        sa.Column("embedding_model_id", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("query_count", sa.Integer(), nullable=False),
        sa.Column("results", postgresql.JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_run"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["snapshot.id"], name="fk_eval_run_snapshot_id_snapshot"
        ),
        sa.CheckConstraint("kind IN ('search')", name="kind"),
        sa.CheckConstraint(
            "cardinality(query_sets) >= 1 AND query_sets <@ ARRAY['search', 'similar']::text[]",
            name="query_sets",
        ),
        sa.CheckConstraint(
            "cardinality(min_similarities) >= 1"
            " AND array_position(min_similarities, NULL) IS NULL"
            " AND 0 <= ALL (min_similarities) AND 1 > ALL (min_similarities)",
            name="min_similarities",
        ),
        sa.CheckConstraint("query_count >= 0", name="query_count_not_negative"),
        sa.CheckConstraint("finished_at >= started_at", name="finished_after_started"),
    )
    op.create_index("ix_eval_run_snapshot_id", "eval_run", ["snapshot_id"])
    # 評估紀錄只新增（0001 的預設權限給了四種）。
    op.execute(f"REVOKE UPDATE, DELETE ON eval_run FROM {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("eval_run")
    op.drop_table("eval_query_relevant")
    op.drop_table("eval_query")
