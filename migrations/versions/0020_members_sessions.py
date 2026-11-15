"""成員、允許名單與 session（Story 3.3、ADR-0008、ADR-0046、AD-23）。

- 新增 `member`：顯示名稱（非空白、最多 100 字元）、是否管理者、Google 的 `google_sub`（可空、
  UNIQUE，Epic 7 第一次 Google 登入時補上）、第一次登入時間、建立時間。應用程式角色收回 DELETE：
  成員被人工判斷、稽核等紀錄引用，移出名單是刪 `allowed_email`，不刪成員。
- 新增 `allowed_email`：小寫 email（UNIQUE、像 email 的 CHECK）、`member_id`（UNIQUE 外鍵：一位
  成員一個 email）。
- 新增 `session`：`member_id` 外鍵＋索引、`token_sha256`（bytea 32 位元組、UNIQUE；瀏覽器拿到的
  token 不進資料庫）、建立與最後使用時間（由應用程式的時鐘寫入）。
- 只建新表，不改任何既有的表（`judgment_event.member_id`、`audit_event.actor_member_id` 的外鍵
  不在這裡補）。

和 `radar/repo/members.py`、`radar/repo/sessions.py` 的表定義一致（`alembic check` 比對）；CHECK
約束 autogenerate 不比，這裡照抄同一份條件。可降版（降版會丟掉三張表與裡面的資料）。

版本號 0015～0019 保留給另一條線（Story 2.8～2.13），所以這一版接在 0014 後面；兩條線合併後會有
兩個頭，屆時由規劃端把 `down_revision` 改接到 0019，並刪掉 `tests/arch/test_migrations.py` 的
保留空號那一項。

版本：0020
上一版：0014（0015～0019 保留，合併後改接）
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import BYTEA

from radar.domain.database_requirements import APP_ROLE

revision: str = "0020"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "member",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("google_sub", sa.Text(), nullable=True),
        sa.Column("first_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_member"),
        sa.UniqueConstraint("google_sub", name="uq_member_google_sub"),
        sa.CheckConstraint(
            "display_name ~ '[^[:space:]]' AND char_length(display_name) <= 100",
            name="ck_member_display_name_format",
        ),
        sa.CheckConstraint(
            "google_sub IS NULL OR google_sub <> ''", name="ck_member_google_sub_not_empty"
        ),
    )
    op.create_table(
        "allowed_email",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_allowed_email"),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name="fk_allowed_email_member_id_member"
        ),
        sa.UniqueConstraint("email", name="uq_allowed_email_email"),
        sa.UniqueConstraint("member_id", name="uq_allowed_email_member_id"),
        sa.CheckConstraint(
            "email = lower(email) AND char_length(email) <= 254"
            " AND email ~ '^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$'",
            name="ck_allowed_email_email_format",
        ),
    )
    op.create_table(
        "session",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("token_sha256", BYTEA(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_session"),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"], name="fk_session_member_id_member"),
        sa.UniqueConstraint("token_sha256", name="uq_session_token_sha256"),
        sa.CheckConstraint(
            "octet_length(token_sha256) = 32", name="ck_session_token_sha256_length"
        ),
        sa.CheckConstraint("last_seen_at >= created_at", name="ck_session_last_seen_after_created"),
    )
    op.create_index("ix_session_member_id", "session", ["member_id"])

    # 成員不刪（被其他紀錄引用）；移出名單刪的是 allowed_email。
    op.execute(f"REVOKE DELETE ON member FROM {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("session")
    op.drop_table("allowed_email")
    op.drop_table("member")
