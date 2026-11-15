"""`audit_event`（稽核事件）：表的定義（AD-26、AD-10、ADR-0035、Story 2.7）。

- 只新增：應用程式角色沒有 UPDATE、DELETE 權限（遷移 0014 收回）。
- `actor_member_id` 空的＝系統做的（`actor_role` 是 `system`），兩者由 CHECK 綁在一起；`action`
  是小寫識別字；`detail` 是 jsonb 物件。
- 唯一的寫入者是這裡的 `create_audit_event`（Story 3.3 起；AD-26 寫的 `record()` 不符合 repo 的
  前綴規則，所以叫這個名字）。形狀由 `domain/audit.NewAuditEvent` 在建構時擋，CHECK 再擋一次。
  函式收呼叫端開好的交易連線（和被稽核的改動同一個交易），不 commit。
"""

import uuid
from typing import Final

from sqlalchemy import CheckConstraint, Column, Connection, DateTime, Index, Table, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB

from radar.domain.audit import AuditActorRole, NewAuditEvent
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

# --- 寫入 ---------------------------------------------------------------------------------

_INSERT: Final = text(
    "INSERT INTO audit_event (actor_member_id, actor_role, action, target_type, target_id, detail)"
    " VALUES (:actor_member_id, :actor_role, :action, :target_type, :target_id,"
    " CAST(:detail AS jsonb))"
    " RETURNING id"
)


def create_audit_event(connection: Connection, event: NewAuditEvent) -> uuid.UUID:
    """新增一筆稽核事件，回它的 id。在呼叫端開好的交易裡做（和被稽核的改動同一個交易），不 commit。

    Raises:
        TypeError: `event` 不是 `NewAuditEvent`（形狀在它建構時已經驗過）。
    """
    if not isinstance(event, NewAuditEvent):
        raise TypeError("event 要是 NewAuditEvent")
    value = connection.execute(
        _INSERT,
        {
            "actor_member_id": event.actor_member_id,
            "actor_role": AuditActorRole(event.actor_role).value,
            "action": event.action,
            "target_type": event.target_type,
            "target_id": event.target_id,
            "detail": event.detail_json(),
        },
    ).scalar_one()
    return uuid.UUID(str(value))
