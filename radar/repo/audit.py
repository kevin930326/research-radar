"""`audit_event`（稽核事件）：表的定義（AD-26、AD-10、ADR-0035、Story 2.7）。

- 只新增：應用程式角色沒有 UPDATE、DELETE 權限（遷移 0014 收回）。
- `actor_member_id` 空的＝系統做的（`actor_role` 是 `system`），兩者由 CHECK 綁在一起；`action`
  是小寫識別字；`detail` 是 jsonb 物件。
- 寫入函式還沒有：留給第一個要寫稽核的 Story（AD-26 寫的 `record()` 不符合 repo 的前綴規則，名字
  到時候再定）。
"""

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Table, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB

from radar.repo.models import Base

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0014 建表） -----------------------------

audit_event_table = Table(
    "audit_event",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    # `member` 在 3.3，暫不設外鍵；空的＝系統。
    Column("actor_member_id", Uuid),
    Column("actor_role", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("target_type", Text, nullable=False),
    Column("target_id", Uuid),
    Column("detail", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Index("ix_audit_event_created_at", "created_at"),
    Index("ix_audit_event_target", "target_type", "target_id"),
    CheckConstraint("actor_role IN ('member', 'admin', 'system')", name="actor_role"),
    CheckConstraint(
        "(actor_role = 'system') = (actor_member_id IS NULL)", name="system_iff_no_member"
    ),
    CheckConstraint("action ~ '^[a-z][a-z0-9_]*$'", name="action_format"),
    CheckConstraint("target_type ~ '^[a-z][a-z0-9_]*$'", name="target_type_format"),
    CheckConstraint("jsonb_typeof(detail) = 'object'", name="detail_object"),
)
