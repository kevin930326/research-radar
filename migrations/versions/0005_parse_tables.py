"""解析後的論文欄位、摘要、關鍵字與暫存表（AD-5、AD-7、AD-25、AD-31、ADR-0014、ADR-0016、ADR-0022）。

- `paper` 加作者、卷期頁碼、類型、撤稿標記、引用清單、被引用次數、指紋 `source_hash`。
- `paper_abstract`：一篇可以有多個來源的摘要，最多一筆 `is_current`（部分唯一索引保證），
  `uploaded_pdf` 永遠不是目前的。
- `paper_keyword`：（論文, 來源, key）唯一；來源現在只有 `openalex`。
- `ingest_staging`：解析結果先放這裡，最後一個交易再套用；`kind` 現在只有 `reparse`。

和 `radar/repo/papers.py`、`radar/repo/ingest_staging.py` 的表定義一致（`alembic check` 比對）；
CHECK 約束 autogenerate 不比，這裡照抄同一份條件。新表由 0001 設的預設權限交給 `radar_app`。
外鍵的索引由唯一約束（開頭是 `paper_id`）提供。

版本：0005
上一版：0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHA256 = "'^[0-9a-f]{64}$'"
_KEYWORD_KEY = "'^[a-z0-9]+(?:-[a-z0-9]+)*$'"


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.add_column("paper", sa.Column("work_type", sa.Text(), nullable=True))
    op.add_column(
        "paper",
        sa.Column("is_retracted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("paper", sa.Column("volume", sa.Text(), nullable=True))
    op.add_column("paper", sa.Column("issue", sa.Text(), nullable=True))
    op.add_column("paper", sa.Column("first_page", sa.Text(), nullable=True))
    op.add_column("paper", sa.Column("last_page", sa.Text(), nullable=True))
    op.add_column(
        "paper",
        sa.Column(
            "authors",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.add_column(
        "paper",
        sa.Column(
            "referenced_works",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.add_column("paper", sa.Column("cited_by_count", sa.Integer(), nullable=True))
    op.add_column("paper", sa.Column("source_hash", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_paper_cited_by_count_not_negative",
        "paper",
        "cited_by_count IS NULL OR cited_by_count >= 0",
    )
    op.create_check_constraint(
        "ck_paper_source_hash_format",
        "paper",
        f"source_hash IS NULL OR source_hash ~ {_SHA256}",
    )

    op.create_table(
        "paper_abstract",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paper_abstract"),
        sa.ForeignKeyConstraint(
            ["paper_id"], ["paper.id"], name="fk_paper_abstract_paper_id_paper"
        ),
        sa.UniqueConstraint("paper_id", "source", name="uq_paper_abstract_paper_id_source"),
        sa.CheckConstraint(
            "source IN ('openalex', 'semantic_scholar', 'uploaded_pdf')",
            name="ck_paper_abstract_source",
        ),
        sa.CheckConstraint("content <> ''", name="ck_paper_abstract_content_not_empty"),
        sa.CheckConstraint(
            "NOT (is_current AND source = 'uploaded_pdf')",
            name="ck_paper_abstract_uploaded_pdf_not_current",
        ),
    )
    op.create_index(
        "uq_paper_abstract_current",
        "paper_abstract",
        ["paper_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "paper_keyword",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_paper_keyword"),
        sa.ForeignKeyConstraint(["paper_id"], ["paper.id"], name="fk_paper_keyword_paper_id_paper"),
        sa.UniqueConstraint(
            "paper_id", "source", "key", name="uq_paper_keyword_paper_id_source_key"
        ),
        sa.CheckConstraint("source IN ('openalex')", name="ck_paper_keyword_source"),
        sa.CheckConstraint(
            f"key ~ {_KEYWORD_KEY} AND char_length(key) <= 200",
            name="ck_paper_keyword_key_format",
        ),
        sa.CheckConstraint("display_name <> ''", name="ck_paper_keyword_display_name_not_empty"),
    )

    op.create_table(
        "ingest_staging",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_key", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parsed", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("excluded_reason", sa.Text(), nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_ingest_staging"),
        sa.UniqueConstraint(
            "batch_id",
            "source",
            "external_key",
            name="uq_ingest_staging_batch_id_source_external_key",
        ),
        sa.CheckConstraint("kind IN ('reparse')", name="ck_ingest_staging_kind"),
        sa.CheckConstraint("source IN ('openalex')", name="ck_ingest_staging_source"),
        sa.CheckConstraint("external_key <> ''", name="ck_ingest_staging_external_key_not_empty"),
        sa.CheckConstraint(f"source_hash ~ {_SHA256}", name="ck_ingest_staging_source_hash_format"),
        sa.CheckConstraint(
            "excluded_reason IS NULL OR excluded_reason <> ''",
            name="ck_ingest_staging_excluded_reason_not_empty",
        ),
    )
    op.create_index("ix_ingest_staging_kind", "ingest_staging", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_ingest_staging_kind", table_name="ingest_staging")
    op.drop_table("ingest_staging")
    op.drop_table("paper_keyword")
    op.drop_index("uq_paper_abstract_current", table_name="paper_abstract")
    op.drop_table("paper_abstract")
    op.drop_constraint("ck_paper_source_hash_format", "paper", type_="check")
    op.drop_constraint("ck_paper_cited_by_count_not_negative", "paper", type_="check")
    for column in (
        "source_hash",
        "cited_by_count",
        "referenced_works",
        "authors",
        "last_page",
        "issue",
        "first_page",
        "volume",
        "is_retracted",
        "work_type",
    ):
        op.drop_column("paper", column)
