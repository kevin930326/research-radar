"""稽核事件的形狀（AD-23、AD-26、NFR-16）：會改資料的請求與指令，在同一個交易寫一筆。

形狀在建構 `NewAuditEvent` 時就擋，資料庫的 CHECK 再擋一次（遷移 0014）：
- `actor_role` 是 `system` ⇔ `actor_member_id` 空的；
- `action`、`target_type` 是小寫識別字；
- `detail` 是物件（字串鍵），只放 ID 與代碼，不放 email 這類個資。
"""

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

_IDENTIFIER: Final = re.compile(r"[a-z][a-z0-9_]*")


class AuditActorRole(StrEnum):
    """做這件事的身分當時的角色（`audit_event.actor_role`）。"""

    MEMBER = "member"
    ADMIN = "admin"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class NewAuditEvent:
    """要新增的一筆 `audit_event`。

    Raises:
        ValueError: 角色與成員 ID 對不上、`action`／`target_type` 不是小寫識別字、`detail` 不是
            字串鍵的物件或轉不成 JSON。
    """

    actor_member_id: uuid.UUID | None
    actor_role: AuditActorRole
    action: str
    target_type: str
    target_id: uuid.UUID | None
    detail: Mapping[str, object]

    def __post_init__(self) -> None:
        role = AuditActorRole(self.actor_role)
        if self.actor_member_id is not None and not isinstance(self.actor_member_id, uuid.UUID):
            raise ValueError("actor_member_id 有值時要是 UUID")
        if (role is AuditActorRole.SYSTEM) != (self.actor_member_id is None):
            raise ValueError("只有 system 沒有 actor_member_id，其他角色一定要有")
        for name in ("action", "target_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"{name} 要是小寫識別字")
        if self.target_id is not None and not isinstance(self.target_id, uuid.UUID):
            raise ValueError("target_id 有值時要是 UUID")
        if not isinstance(self.detail, Mapping) or not all(
            isinstance(key, str) for key in self.detail
        ):
            raise ValueError("detail 要是字串鍵的物件")
        frozen = MappingProxyType(dict(self.detail))
        try:
            json.dumps(dict(frozen), ensure_ascii=False, allow_nan=False)
        except TypeError, ValueError:
            raise ValueError("detail 要轉得成 JSON") from None
        # 存回唯讀的複本，免得呼叫端留著的可變 dict 在驗證後被改掉。
        object.__setattr__(self, "detail", frozen)

    def detail_json(self) -> str:
        """`detail` 轉成 JSON 字串（寫進 jsonb）。"""
        return json.dumps(dict(self.detail), ensure_ascii=False, allow_nan=False)


def system_audit_event(
    *,
    action: str,
    target_type: str,
    target_id: uuid.UUID | None,
    detail: Mapping[str, object] | None = None,
) -> NewAuditEvent:
    """系統（沒有特定成員，例如 CLI 的維運指令）做的事。

    Raises:
        ValueError: 見 `NewAuditEvent`。
    """
    return NewAuditEvent(
        actor_member_id=None,
        actor_role=AuditActorRole.SYSTEM,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail={} if detail is None else detail,
    )
