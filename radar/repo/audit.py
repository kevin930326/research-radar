"""`audit_event`（稽核事件）：表的定義與唯一的寫入者（AD-26、AD-10、ADR-0035、Story 2.7、2.12）。

- 只新增：應用程式角色沒有 UPDATE、DELETE 權限（遷移 0014 收回）。
- `actor_member_id` 空的＝系統做的（`actor_role` 是 `system`），兩者由 CHECK 綁在一起；`action`
  是小寫識別字；`detail` 是 jsonb 物件。
- 寫入是 `create_audit_event`（AD-26 寫的 `record()` 不符合 repo 的前綴規則，Story 2.12 定成這個
  名字；Story 3.3 的成員指令也用它）；形狀在 `domain/audit.NewAuditEvent` 建構時就驗過。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    Index,
    Table,
    Text,
    Uuid,
    text,
)
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


def create_audit_event(
    connection: Connection, event: NewAuditEvent, *, created_at: datetime | None = None
) -> uuid.UUID:
    """新增一筆稽核事件，回它的 id。在呼叫端開好的交易裡做，不 commit（和它記錄的那件事同一個
    交易：那件事退回，稽核也不留）。`created_at` 沒給是資料庫的 `now()`（交易開始的時間）；修正
    分類給它和兩筆人工判斷同一個時間。

    Raises:
        TypeError: `event` 不是 `NewAuditEvent`（形狀在它建構時已經驗過）。
        ValueError: `created_at` 沒有時區。
    """
    if not isinstance(event, NewAuditEvent):
        raise TypeError("event 要是 NewAuditEvent")
    if created_at is not None and (created_at.tzinfo is None or created_at.utcoffset() is None):
        raise ValueError("created_at 要是帶時區的時間")
    value = connection.execute(
        text(
            "INSERT INTO audit_event"
            " (actor_member_id, actor_role, action, target_type, target_id, detail, created_at)"
            " VALUES (:actor_member_id, :actor_role, :action, :target_type, :target_id,"
            " CAST(:detail AS jsonb), COALESCE(CAST(:created_at AS timestamptz), now()))"
            " RETURNING id"
        ),
        {
            "actor_member_id": event.actor_member_id,
            "actor_role": AuditActorRole(event.actor_role).value,
            "action": event.action,
            "target_type": event.target_type,
            "target_id": event.target_id,
            "detail": event.detail_json(),
            "created_at": created_at,
        },
    ).scalar_one()
    return uuid.UUID(str(value))
