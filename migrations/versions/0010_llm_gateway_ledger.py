"""LLM 閘門的來歷、帳本與三張 AI 表頭表（AD-7、AD-8、ADR-0009、ADR-0015、ADR-0030）。

- `derivation_run`：一次模型呼叫一筆來歷；（對象, 工作, prompt 版本, 模型, input_sha256）有只算
  `running` 而且不是評估的部分唯一索引（同一份輸入同時只有一個在跑）。`eval_run_id` 暫不設外鍵
  （Epic 8 的 `eval_run` 還不存在）。
- `llm_call`：一次呼叫的帳（`cost_usd numeric(12,6)`），`run_id` 唯一；`radar_app` 收回 UPDATE、
  DELETE（0001 的預設權限會給四種，帳只能新增）。
- `zh_summary`、`classification`、`research_model`：只有表頭欄位（內容欄位由 2.4～2.6 加）；
  `(paper_id, lang) WHERE status = 'active'` 部分唯一索引；`abstract_id` 不設外鍵（ADR-0030）。

和 `radar/repo/llm_ledger.py`、`radar/repo/ai_outputs.py` 的表定義一致（`alembic check` 比對）；
CHECK 約束 autogenerate 不比，這裡照抄同一份條件。

版本：0010
上一版：0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from radar.domain.database_requirements import APP_ROLE

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HEADER_TABLES = ("zh_summary", "classification", "research_model")
KEY_COLUMNS = ["paper_id", "task", "prompt_version", "model", "input_sha256"]
SHA256 = "'^[0-9a-f]{64}$'"


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "derivation_run",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("prompt_sha256", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_sha256", sa.Text(), nullable=False),
        sa.Column("input_kind", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=True),
        sa.Column("context_values_version", sa.Text(), nullable=True),
        sa.Column("code_version", sa.Text(), nullable=False),
        sa.Column("call_mode", sa.Text(), nullable=False),
        sa.Column("eval_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_derivation_run"),
        sa.ForeignKeyConstraint(
            ["paper_id"], ["paper.id"], name="fk_derivation_run_paper_id_paper"
        ),
        sa.CheckConstraint(
            "task IN ('zh_summary', 'classify', 'extract_model', 'merge_judge', 'qa_answer',"
            " 'eval_judge')",
            name="ck_derivation_run_task",
        ),
        sa.CheckConstraint("prompt_version >= 1", name="ck_derivation_run_prompt_version_positive"),
        sa.CheckConstraint(
            f"prompt_sha256 ~ {SHA256}", name="ck_derivation_run_prompt_sha256_format"
        ),
        sa.CheckConstraint(
            f"input_sha256 ~ {SHA256}", name="ck_derivation_run_input_sha256_format"
        ),
        sa.CheckConstraint("model <> ''", name="ck_derivation_run_model_not_empty"),
        sa.CheckConstraint(
            "input_kind IN ('title', 'abstract', 'fulltext')", name="ck_derivation_run_input_kind"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(params) = 'object'", name="ck_derivation_run_params_object"
        ),
        sa.CheckConstraint(
            "taxonomy_version IS NULL OR taxonomy_version <> ''",
            name="ck_derivation_run_taxonomy_version_not_empty",
        ),
        sa.CheckConstraint(
            "context_values_version IS NULL OR context_values_version <> ''",
            name="ck_derivation_run_context_values_version_not_empty",
        ),
        sa.CheckConstraint("code_version <> ''", name="ck_derivation_run_code_version_not_empty"),
        sa.CheckConstraint(
            "call_mode IN ('interactive', 'batch')", name="ck_derivation_run_call_mode"
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_derivation_run_status"
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)",
            name="ck_derivation_run_failure_reason_iff_failed",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR failure_reason ~ '^[a-z][a-z0-9_]*$'",
            name="ck_derivation_run_failure_reason_format",
        ),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)",
            name="ck_derivation_run_finished_at_iff_not_running",
        ),
    )
    op.create_index("ix_derivation_run_key", "derivation_run", KEY_COLUMNS)
    op.create_index(
        "uq_derivation_run_running",
        "derivation_run",
        KEY_COLUMNS,
        unique=True,
        postgresql_where=sa.text("status = 'running' AND eval_run_id IS NULL"),
    )
    op.create_index(
        "ix_derivation_run_running_created_at",
        "derivation_run",
        ["created_at"],
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "llm_call",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("price_tier", sa.Text(), nullable=False),
        sa.Column("is_batch", sa.Boolean(), nullable=False),
        sa.Column("response_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_llm_call"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["derivation_run.id"], name="fk_llm_call_run_id_derivation_run"
        ),
        sa.UniqueConstraint("run_id", name="uq_llm_call_run_id"),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_creation_input_tokens >= 0"
            " AND cache_read_input_tokens >= 0",
            name="ck_llm_call_tokens_not_negative",
        ),
        sa.CheckConstraint("cost_usd >= 0", name="ck_llm_call_cost_usd_not_negative"),
        sa.CheckConstraint(
            "price_tier IN ('standard', 'long_context')", name="ck_llm_call_price_tier"
        ),
    )
    # 帳只能新增（0001 的預設權限給了四種）。
    op.execute(f"REVOKE UPDATE, DELETE ON llm_call FROM {APP_ROLE}")

    for table in HEADER_TABLES:
        op.create_table(
            table,
            sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
            sa.Column("paper_id", sa.Uuid(), nullable=False),
            sa.Column("run_id", sa.Uuid(), nullable=False),
            sa.Column("input_kind", sa.Text(), nullable=False),
            sa.Column("input_sha256", sa.Text(), nullable=False),
            sa.Column("abstract_id", sa.Uuid(), nullable=True),
            sa.Column("lang", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("reject_reason", sa.Text(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False
            ),
            sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
            sa.ForeignKeyConstraint(["paper_id"], ["paper.id"], name=f"fk_{table}_paper_id_paper"),
            sa.ForeignKeyConstraint(
                ["run_id"], ["derivation_run.id"], name=f"fk_{table}_run_id_derivation_run"
            ),
            sa.UniqueConstraint("run_id", name=f"uq_{table}_run_id"),
            sa.CheckConstraint(
                "input_kind IN ('title', 'abstract', 'fulltext')", name=f"ck_{table}_input_kind"
            ),
            sa.CheckConstraint(f"input_sha256 ~ {SHA256}", name=f"ck_{table}_input_sha256_format"),
            sa.CheckConstraint("lang IN ('zh-Hant', 'en', 'zxx')", name=f"ck_{table}_lang"),
            sa.CheckConstraint(
                "status IN ('candidate', 'active', 'superseded', 'rejected')",
                name=f"ck_{table}_status",
            ),
            sa.CheckConstraint(
                "(status = 'rejected') = (reject_reason IS NOT NULL)",
                name=f"ck_{table}_reject_reason_iff_rejected",
            ),
            sa.CheckConstraint(
                "reject_reason IS NULL OR reject_reason ~ '^[a-z][a-z0-9_]*$'",
                name=f"ck_{table}_reject_reason_format",
            ),
        )
        op.create_index(f"ix_{table}_paper_id", table, ["paper_id"])
        op.create_index(f"ix_{table}_abstract_id", table, ["abstract_id"])
        op.create_index(
            f"uq_{table}_active_lang",
            table,
            ["paper_id", "lang"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        )


def downgrade() -> None:
    for table in reversed(HEADER_TABLES):
        op.drop_index(f"uq_{table}_active_lang", table_name=table)
        op.drop_index(f"ix_{table}_abstract_id", table_name=table)
        op.drop_index(f"ix_{table}_paper_id", table_name=table)
        op.drop_table(table)
    op.drop_table("llm_call")
    op.drop_index("ix_derivation_run_running_created_at", table_name="derivation_run")
    op.drop_index("uq_derivation_run_running", table_name="derivation_run")
    op.drop_index("ix_derivation_run_key", table_name="derivation_run")
    op.drop_table("derivation_run")
