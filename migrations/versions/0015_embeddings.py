"""向量：`embedding_model`、`embedding`，以及向量化的來歷與用量（Story 2.8、AD-12、AD-29、ADR-0005、
ADR-0040）。

- 新增 `embedding_model`（模型登錄：`model_id` 主鍵、維度），登記目前的模型
  `@cf/qwen/qwen3-embedding-0.6b`（1,024 維）。每個模型的 HNSW 部分索引要在遷移裡建，所以登錄也只由
  遷移寫：應用程式角色收回 INSERT、UPDATE、DELETE（0001 的預設權限給了四種）。
- 新增 `embedding`：主鍵（`item_type`, `item_id`, `model_id`），向量 `halfvec`（不帶維度）；
  （`model_id`, `dimensions`）外鍵指到 `embedding_model`、CHECK `vector_dims(vector) = dimensions`，
  維度不對寫不進去。`item_id` 不設外鍵（`item_type` 之後會有全文切塊等別種對象）。
- 目前模型的 HNSW 部分索引：`(vector::halfvec(1024)) halfvec_cosine_ops WHERE model_id = '…'`。
- `derivation_run`：`task` 多一個 `embed`；`prompt_version`、`prompt_sha256` 改成可以是 NULL，
  只有 embed 是 NULL（兩欄同時）；embed 另有（paper_id, model, input_sha256）只算 running 的部分
  唯一索引（上面那個索引含 prompt_version，NULL 擋不到）。
- `api_call`：`source` 多一個 `workers_ai`；新增 `estimated_input_tokens`（只有 workers_ai 有值：
  Workers AI 不回報用量，記自己估的 token）。

和 `radar/repo/embeddings.py`、`radar/repo/llm_ledger.py`、`radar/repo/api_calls.py` 的表定義一致
（`alembic check` 比對；HNSW 是運算式索引，`alembic check` 只比名稱，完整定義由 integration
測試核對）；
CHECK 約束 autogenerate 不比，這裡照抄同一份條件。可降版：降版會刪掉全部向量、向量化的
`derivation_run` 與 `workers_ai` 的 `api_call`，再把約束改回 0014 的樣子。

版本：0015
上一版：0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from radar.domain.database_requirements import APP_ROLE

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODEL_ID = "@cf/qwen/qwen3-embedding-0.6b"
DIMENSIONS = 1024
HNSW_INDEX = "ix_embedding_hnsw_qwen3_embedding_0_6b"
OLD_TASKS = "'zh_summary', 'classify', 'extract_model', 'merge_judge', 'qa_answer', 'eval_judge'"


class _HalfVec(sa.types.UserDefinedType[object]):
    """建表用的 `halfvec`（不帶維度）。和 `radar/repo/embeddings.py` 的同名型別一樣。"""

    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return "HALFVEC"


def upgrade() -> None:
    op.create_table(
        "embedding_model",
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("model_id", name="pk_embedding_model"),
        sa.UniqueConstraint(
            "model_id", "dimensions", name="uq_embedding_model_model_id_dimensions"
        ),
        sa.CheckConstraint("model_id <> ''", name="ck_embedding_model_model_id_not_empty"),
        sa.CheckConstraint(
            "provider ~ '^[a-z][a-z0-9_]*$'", name="ck_embedding_model_provider_format"
        ),
        sa.CheckConstraint(
            "dimensions BETWEEN 1 AND 4000", name="ck_embedding_model_dimensions_range"
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO embedding_model (model_id, provider, dimensions)"
            " VALUES (:model_id, 'workers_ai', :dimensions)"
        ).bindparams(model_id=MODEL_ID, dimensions=DIMENSIONS)
    )
    # 模型登錄只由遷移寫（每個模型要配一個 HNSW 索引）。
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON embedding_model FROM {APP_ROLE}")

    op.create_table(
        "embedding",
        sa.Column("item_type", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector", _HalfVec(), nullable=False),
        sa.Column("input_sha256", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
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
        sa.PrimaryKeyConstraint("item_type", "item_id", "model_id", name="pk_embedding"),
        sa.ForeignKeyConstraint(
            ["model_id", "dimensions"],
            ["embedding_model.model_id", "embedding_model.dimensions"],
            name="fk_embedding_model_id_embedding_model",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["derivation_run.id"], name="fk_embedding_run_id_derivation_run"
        ),
        sa.CheckConstraint("item_type IN ('paper')", name="ck_embedding_item_type"),
        sa.CheckConstraint(
            "input_sha256 ~ '^[0-9a-f]{64}$'", name="ck_embedding_input_sha256_format"
        ),
        sa.CheckConstraint("vector_dims(vector) = dimensions", name="ck_embedding_vector_dims"),
    )
    op.create_index("ix_embedding_run_id", "embedding", ["run_id"])
    op.create_index("ix_embedding_model_id_dimensions", "embedding", ["model_id", "dimensions"])
    # 每個模型一個 HNSW 部分索引（AD-12）。查詢要寫同一個運算式、帶 model_id 條件才用得到。
    op.execute(
        f"CREATE INDEX {HNSW_INDEX} ON embedding USING hnsw"
        f" ((vector::halfvec({DIMENSIONS})) halfvec_cosine_ops)"
        f" WHERE model_id = '{MODEL_ID}'"
    )

    # derivation_run：收 embed（沒有 prompt）。
    op.drop_constraint("ck_derivation_run_task", "derivation_run", type_="check")
    op.create_check_constraint(
        "ck_derivation_run_task", "derivation_run", f"task IN ({OLD_TASKS}, 'embed')"
    )
    op.alter_column("derivation_run", "prompt_version", nullable=True)
    op.alter_column("derivation_run", "prompt_sha256", nullable=True)
    op.create_check_constraint(
        "ck_derivation_run_prompt_version_iff_not_embed",
        "derivation_run",
        "(task = 'embed') = (prompt_version IS NULL)",
    )
    op.create_check_constraint(
        "ck_derivation_run_prompt_sha256_iff_prompt_version",
        "derivation_run",
        "(prompt_version IS NULL) = (prompt_sha256 IS NULL)",
    )
    op.create_index(
        "uq_derivation_run_embed_running",
        "derivation_run",
        ["paper_id", "model", "input_sha256"],
        unique=True,
        postgresql_where=sa.text("task = 'embed' AND status = 'running'"),
    )

    # api_call：收 workers_ai 與估計的 token。
    op.drop_constraint("ck_api_call_source", "api_call", type_="check")
    op.create_check_constraint(
        "ck_api_call_source", "api_call", "source IN ('openalex', 'workers_ai')"
    )
    op.add_column("api_call", sa.Column("estimated_input_tokens", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_api_call_estimated_input_tokens_not_negative",
        "api_call",
        "estimated_input_tokens IS NULL OR estimated_input_tokens >= 0",
    )
    op.create_check_constraint(
        "ck_api_call_estimated_input_tokens_iff_workers_ai",
        "api_call",
        "(source = 'workers_ai') = (estimated_input_tokens IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_api_call_estimated_input_tokens_iff_workers_ai", "api_call", type_="check"
    )
    op.drop_constraint("ck_api_call_estimated_input_tokens_not_negative", "api_call", type_="check")
    op.execute("DELETE FROM api_call WHERE source = 'workers_ai'")
    op.drop_column("api_call", "estimated_input_tokens")
    op.drop_constraint("ck_api_call_source", "api_call", type_="check")
    op.create_check_constraint("ck_api_call_source", "api_call", "source IN ('openalex')")

    op.drop_index(HNSW_INDEX, table_name="embedding")
    op.drop_index("ix_embedding_model_id_dimensions", table_name="embedding")
    op.drop_index("ix_embedding_run_id", table_name="embedding")
    op.drop_table("embedding")
    op.drop_table("embedding_model")

    op.drop_index("uq_derivation_run_embed_running", table_name="derivation_run")
    op.drop_constraint(
        "ck_derivation_run_prompt_sha256_iff_prompt_version", "derivation_run", type_="check"
    )
    op.drop_constraint(
        "ck_derivation_run_prompt_version_iff_not_embed", "derivation_run", type_="check"
    )
    op.execute("DELETE FROM derivation_run WHERE task = 'embed'")
    op.alter_column("derivation_run", "prompt_sha256", nullable=False)
    op.alter_column("derivation_run", "prompt_version", nullable=False)
    op.drop_constraint("ck_derivation_run_task", "derivation_run", type_="check")
    op.create_check_constraint("ck_derivation_run_task", "derivation_run", f"task IN ({OLD_TASKS})")
