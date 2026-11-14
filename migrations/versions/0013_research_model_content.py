"""研究模型的內容（Story 2.6、ADR-0012、ADR-0015、ADR-0034）。

- `research_model` 加論文層級的內容欄位（全部可為 NULL）：`is_review_or_meta_analysis`、四個情境
  欄位（情境值清單的 `key`）、`sample_size`、`limitations`、`future_research`，與三個計數
  （`dropped_context_value_count`、`unsupported_context_value_count`、`corrected_support_count`）。
  CHECK：`key` 寫法、樣本數 ≥ 1、文字不是空白、計數不是負數、`rejected` 的內容與計數全是 NULL。
- 新增 `construct`：以 `normalized_name` 為唯一鍵，`canonical_id` 不可為空、指向自己這張表（新構面
  指向自己，由 `repo/constructs.py` 寫入）。
- 新增 `construct_mention`（一篇提到的一個構面）與 `hypothesis`（一條關係），掛在研究模型表頭底下、
  不帶自己的狀態，指向原本的構面。CHECK 照 `radar/repo/ai_outputs.py`。

和 `radar/repo/ai_outputs.py`、`radar/repo/constructs.py` 的表定義一致（`alembic check` 比對）；
CHECK 約束 autogenerate 不比，這裡照抄同一份條件。0012 已經有的研究模型表頭（2.2 只有表頭）內容與
計數是 NULL，CHECK 都成立。權限由 0001 的預設權限給（應用程式角色可以新增、讀取）。可降版（降版
會丟掉研究模型的內容、構面、構面提及與假設）。

版本：0013
上一版：0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONTEXT_COLUMNS = ("country", "industry", "research_design", "unit_of_analysis")
_TEXT_COLUMNS = ("limitations", "future_research")
_COUNT_COLUMNS = (
    "dropped_context_value_count",
    "unsupported_context_value_count",
    "corrected_support_count",
)
_CONTENT_COLUMNS = (
    "is_review_or_meta_analysis",
    *_CONTEXT_COLUMNS,
    "sample_size",
    *_TEXT_COLUMNS,
    *_COUNT_COLUMNS,
)
_RESEARCH_MODEL_CHECKS = (
    (
        "ck_research_model_context_key_format",
        " AND ".join(
            f"({column} IS NULL OR {column} ~ '^[a-z][a-z0-9_]*$')" for column in _CONTEXT_COLUMNS
        ),
    ),
    ("ck_research_model_sample_size_positive", "sample_size IS NULL OR sample_size >= 1"),
    (
        "ck_research_model_content_not_blank",
        "(limitations IS NULL OR limitations ~ '[^[:space:]]')"
        " AND (future_research IS NULL OR future_research ~ '[^[:space:]]')",
    ),
    (
        "ck_research_model_counts_not_negative",
        " AND ".join(f"({column} IS NULL OR {column} >= 0)" for column in _COUNT_COLUMNS),
    ),
    (
        "ck_research_model_rejected_has_no_content",
        "status <> 'rejected' OR ("
        + " AND ".join(f"{column} IS NULL" for column in _CONTENT_COLUMNS)
        + ")",
    ),
)


def upgrade() -> None:
    op.add_column(
        "research_model", sa.Column("is_review_or_meta_analysis", sa.Boolean(), nullable=True)
    )
    for column in _CONTEXT_COLUMNS:
        op.add_column("research_model", sa.Column(column, sa.Text(), nullable=True))
    op.add_column("research_model", sa.Column("sample_size", sa.Integer(), nullable=True))
    for column in _TEXT_COLUMNS:
        op.add_column("research_model", sa.Column(column, sa.Text(), nullable=True))
    for column in _COUNT_COLUMNS:
        op.add_column("research_model", sa.Column(column, sa.Integer(), nullable=True))
    for name, condition in _RESEARCH_MODEL_CHECKS:
        op.create_check_constraint(name, "research_model", condition)

    op.create_table(
        "construct",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("canonical_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_construct"),
        sa.ForeignKeyConstraint(
            ["canonical_id"], ["construct.id"], name="fk_construct_canonical_id_construct"
        ),
        sa.UniqueConstraint("normalized_name", name="uq_construct_normalized_name"),
        sa.CheckConstraint(
            "normalized_name ~ '[^[:space:]]'", name="ck_construct_normalized_name_not_blank"
        ),
    )
    op.create_index("ix_construct_canonical_id", "construct", ["canonical_id"])

    op.create_table(
        "construct_mention",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("research_model_id", sa.Uuid(), nullable=False),
        sa.Column("construct_id", sa.Uuid(), nullable=False),
        sa.Column("name_en", sa.Text(), nullable=False),
        sa.Column("label_zh", sa.Text(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=True),
        sa.Column("scale_source", sa.Text(), nullable=True),
        sa.Column("item_location", sa.Text(), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=True),
        sa.Column("cronbach_alpha", sa.Numeric(), nullable=True),
        sa.Column("composite_reliability", sa.Numeric(), nullable=True),
        sa.Column("ave", sa.Numeric(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_construct_mention"),
        sa.ForeignKeyConstraint(
            ["research_model_id"],
            ["research_model.id"],
            name="fk_construct_mention_research_model_id_research_model",
        ),
        sa.ForeignKeyConstraint(
            ["construct_id"], ["construct.id"], name="fk_construct_mention_construct_id_construct"
        ),
        sa.UniqueConstraint(
            "research_model_id",
            "construct_id",
            name="uq_construct_mention_research_model_id_construct_id",
        ),
        sa.CheckConstraint(
            "name_en ~ '[^[:space:]]' AND label_zh ~ '[^[:space:]]'"
            " AND (definition IS NULL OR definition ~ '[^[:space:]]')"
            " AND (scale_source IS NULL OR scale_source ~ '[^[:space:]]')"
            " AND (item_location IS NULL OR item_location ~ '[^[:space:]]')",
            name="ck_construct_mention_text_not_blank",
        ),
        sa.CheckConstraint(
            "item_count IS NULL OR item_count >= 1", name="ck_construct_mention_item_count_positive"
        ),
        sa.CheckConstraint(
            "(cronbach_alpha IS NULL OR cronbach_alpha BETWEEN 0 AND 1)"
            " AND (composite_reliability IS NULL OR composite_reliability BETWEEN 0 AND 1)"
            " AND (ave IS NULL OR ave BETWEEN 0 AND 1)",
            name="ck_construct_mention_reliability_range",
        ),
    )
    op.create_index("ix_construct_mention_construct_id", "construct_mention", ["construct_id"])

    op.create_table(
        "hypothesis",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("research_model_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("sentence", sa.Text(), nullable=False),
        sa.Column("sentence_sha256", sa.Text(), nullable=False),
        sa.Column("cause_construct_id", sa.Uuid(), nullable=False),
        sa.Column("effect_construct_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_type", sa.Text(), nullable=False),
        sa.Column("support", sa.Text(), nullable=False),
        sa.Column("beta", sa.Numeric(), nullable=True),
        sa.Column("p_value", sa.Numeric(), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=True),
        sa.Column("effect_type", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_hypothesis"),
        sa.ForeignKeyConstraint(
            ["research_model_id"],
            ["research_model.id"],
            name="fk_hypothesis_research_model_id_research_model",
        ),
        sa.ForeignKeyConstraint(
            ["cause_construct_id"],
            ["construct.id"],
            name="fk_hypothesis_cause_construct_id_construct",
        ),
        sa.ForeignKeyConstraint(
            ["effect_construct_id"],
            ["construct.id"],
            name="fk_hypothesis_effect_construct_id_construct",
        ),
        sa.UniqueConstraint(
            "research_model_id", "ordinal", name="uq_hypothesis_research_model_id_ordinal"
        ),
        sa.UniqueConstraint(
            "research_model_id",
            "sentence_sha256",
            "cause_construct_id",
            "effect_construct_id",
            name="uq_hypothesis_sentence_relation",
        ),
        sa.CheckConstraint("ordinal >= 1", name="ck_hypothesis_ordinal_positive"),
        sa.CheckConstraint(
            "sentence ~ '[^[:space:]]' AND (effect_type IS NULL OR effect_type ~ '[^[:space:]]')",
            name="ck_hypothesis_text_not_blank",
        ),
        sa.CheckConstraint(
            "sentence_sha256 ~ '^[0-9a-f]{64}$'", name="ck_hypothesis_sentence_sha256_format"
        ),
        sa.CheckConstraint(
            "evidence_type IN ('empirical_test', 'analytical', 'simulation', 'qualitative',"
            " 'conceptual')",
            name="ck_hypothesis_evidence_type",
        ),
        sa.CheckConstraint(
            "support IN ('supported', 'not_supported', 'partially_supported', 'not_stated',"
            " 'not_determined')",
            name="ck_hypothesis_support",
        ),
        sa.CheckConstraint(
            "(evidence_type = 'empirical_test') = (support <> 'not_determined')",
            name="ck_hypothesis_support_matches_evidence_type",
        ),
        sa.CheckConstraint(
            "cause_construct_id <> effect_construct_id",
            name="ck_hypothesis_cause_differs_from_effect",
        ),
        sa.CheckConstraint(
            "beta IS NULL OR (beta > '-Infinity'::numeric AND beta < 'Infinity'::numeric)",
            name="ck_hypothesis_beta_finite",
        ),
        sa.CheckConstraint(
            "p_value IS NULL OR p_value BETWEEN 0 AND 1", name="ck_hypothesis_p_value_range"
        ),
        sa.CheckConstraint(
            "sample_size IS NULL OR sample_size >= 1", name="ck_hypothesis_sample_size_positive"
        ),
    )
    op.create_index("ix_hypothesis_cause_construct_id", "hypothesis", ["cause_construct_id"])
    op.create_index("ix_hypothesis_effect_construct_id", "hypothesis", ["effect_construct_id"])


def downgrade() -> None:
    op.drop_table("hypothesis")
    op.drop_table("construct_mention")
    op.drop_table("construct")
    for name, _ in reversed(_RESEARCH_MODEL_CHECKS):
        op.drop_constraint(name, "research_model", type_="check")
    for column in reversed(_CONTENT_COLUMNS):
        op.drop_column("research_model", column)
