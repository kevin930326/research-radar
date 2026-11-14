"""論文表 `paper` 與外部識別碼表 `external_id`（AD-4、AD-5、AD-6、ADR-0020）。

和 `radar/repo/papers.py` 的表定義一致（`alembic check` 比對）；CHECK 約束 autogenerate 不比，
這裡照抄同一份條件。新表由 0001 設的預設權限交給 `radar_app`。

版本：0003
上一版：0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("publication_year", sa.Integer(), nullable=True),
        sa.Column("journal_issn_l", sa.Text(), nullable=True),
        sa.Column("in_scope", sa.Boolean(), nullable=False),
        sa.Column("excluded_reason", sa.Text(), nullable=True),
        sa.Column("lab_visible", sa.Boolean(), nullable=False),
        sa.Column("openalex_fetched_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_paper"),
        sa.CheckConstraint(
            "origin IN ('tracked', 'expansion', 'import', 'manual')",
            name="ck_paper_origin",
        ),
        sa.CheckConstraint(
            "excluded_reason IS NULL OR excluded_reason <> ''",
            name="ck_paper_excluded_reason_not_empty",
        ),
        sa.CheckConstraint(
            "NOT in_scope OR lab_visible",
            name="ck_paper_in_scope_implies_lab_visible",
        ),
    )
    op.create_table(
        "external_id",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("scheme", sa.Text(), nullable=False),
        sa.Column("value_norm", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_external_id"),
        sa.ForeignKeyConstraint(["paper_id"], ["paper.id"], name="fk_external_id_paper_id_paper"),
        sa.UniqueConstraint("scheme", "value_norm", name="uq_external_id_scheme_value_norm"),
        sa.CheckConstraint("scheme IN ('doi', 'openalex')", name="ck_external_id_scheme"),
        sa.CheckConstraint("value_norm <> ''", name="ck_external_id_value_norm_not_empty"),
    )
    op.create_index("ix_external_id_paper_id", "external_id", ["paper_id"])


def downgrade() -> None:
    op.drop_index("ix_external_id_paper_id", table_name="external_id")
    op.drop_table("external_id")
    op.drop_table("paper")
