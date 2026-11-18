"""個人文獻庫與刪除前的封存（Story 3.16、AD-4、AD-26）。

- 新增 `library_item`：成員（`owner_id`，NOT NULL 外鍵到 `member`）的文獻庫裡的一篇論文
  （`paper_id`，外鍵到 `paper`、有索引）；`added_via` 三種值（`upload`、`shared_list`、`manual`）；
  `added_at` 預設 `now()`。（`owner_id`, `paper_id`）UNIQUE：同一位成員同一篇只有一筆。清單照
  （`owner_id`, `added_at`, `id`）的索引分頁（也當 `owner_id` 外鍵的索引）。
- 新增 `deleted_record`：刪除前把來源列整列複製成 jsonb（`content`，CHECK 是物件），記來源表
  （`source_table`，小寫識別字）、來源列的 id、擁有者（可空）、刪的人（可空＝系統）、刪的時間。
  不設外鍵到來源表（來源列已經刪掉）；兩個成員欄位外鍵到 `member`（成員不刪）。索引：
  （`owner_id`, `source_table`, `deleted_at`）找自己最新的紀錄；`deleted_by_member_id` 給外鍵。
- 兩張表都收回應用程式角色的 UPDATE：文獻庫的一筆只新增、刪除（移出）、復原（用原本的值新增），
  封存只新增、刪除（復原時消耗）。
- 只建新表，不改任何既有的表。

和 `radar/repo/library.py`、`radar/repo/deleted_records.py` 的表定義一致（`alembic check` 比對）；
CHECK 約束 autogenerate 不比，這裡照抄同一份條件。可降版（降版會丟掉兩張表與裡面的資料）。

版本：0020
上一版：0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from radar.domain.database_requirements import APP_ROLE

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "library_item",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("added_via", sa.Text(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_library_item"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["member.id"], name="fk_library_item_owner_id_member"
        ),
        sa.ForeignKeyConstraint(["paper_id"], ["paper.id"], name="fk_library_item_paper_id_paper"),
        sa.UniqueConstraint("owner_id", "paper_id", name="uq_library_item_owner_id_paper_id"),
        sa.CheckConstraint(
            "added_via IN ('upload', 'shared_list', 'manual')",
            name="ck_library_item_added_via_values",
        ),
    )
    op.create_index("ix_library_item_paper_id", "library_item", ["paper_id"])
    op.create_index(
        "ix_library_item_owner_id_added_at_id", "library_item", ["owner_id", "added_at", "id"]
    )

    op.create_table(
        "deleted_record",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("source_table", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("deleted_by_member_id", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_deleted_record"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["member.id"], name="fk_deleted_record_owner_id_member"
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by_member_id"],
            ["member.id"],
            name="fk_deleted_record_deleted_by_member_id_member",
        ),
        sa.CheckConstraint(
            "source_table ~ '^[a-z][a-z0-9_]*$'", name="ck_deleted_record_source_table_format"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'", name="ck_deleted_record_content_object"
        ),
    )
    op.create_index(
        "ix_deleted_record_owner_id_source_table_deleted_at",
        "deleted_record",
        ["owner_id", "source_table", "deleted_at"],
    )
    op.create_index(
        "ix_deleted_record_deleted_by_member_id", "deleted_record", ["deleted_by_member_id"]
    )

    # 文獻庫的一筆與封存都不改：只新增、刪除。
    op.execute(f"REVOKE UPDATE ON library_item FROM {APP_ROLE}")
    op.execute(f"REVOKE UPDATE ON deleted_record FROM {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("deleted_record")
    op.drop_table("library_item")
