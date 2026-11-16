"""稽核事件的形狀（AD-23、AD-26、NFR-16、ADR-0035、Story 2.12、Story 3.3）。表在遷移 0014
（`repo/audit.py`）：會改資料的請求與指令，在同一個交易寫一筆。

- 只新增、不改不刪（應用程式角色沒有 UPDATE、DELETE）。
- 系統做的（`actor_role` 是 `system`）沒有成員 id，其他角色一定有（資料庫的 CHECK 也綁著）。
- `action`、`target_type` 是小寫識別字；`detail` 是 JSON 物件，只放 id、計數、代碼這類資料，不放
  email 這類個資、文件內容、模型輸出或金鑰（寫的人負責）。
"""

import json
import math
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


def _check_json(value: object) -> None:
    """`detail` 裡的值要是 JSON 寫得出來、jsonb 存得進去的（有限的數字、沒有 NUL 的字串）。"""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("detail 不能有 NaN 或無限大")
    if isinstance(value, str) and "\x00" in value:
        raise ValueError("detail 的字串不能有 NUL 字元")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("detail 的鍵要是字串")
            _check_json(key)
            _check_json(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_json(item)


@dataclass(frozen=True, slots=True)
class NewAuditEvent:
    """要新增的一筆 `audit_event`。

    Raises:
        ValueError: 角色不認得、成員 id 或 `target_id` 有值但不是 UUID、系統身分帶了成員 id
            （或其他角色沒帶）、`action`／`target_type` 不是小寫識別字、`detail` 不是 JSON 物件
            或寫不進 jsonb。
    """

    actor_role: AuditActorRole
    actor_member_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: uuid.UUID | None
    detail: Mapping[str, object]

    def __post_init__(self) -> None:
        role = AuditActorRole(self.actor_role)
        if self.actor_member_id is not None and not isinstance(self.actor_member_id, uuid.UUID):
            raise ValueError("actor_member_id 有值時要是 UUID")
        if (role is AuditActorRole.SYSTEM) != (self.actor_member_id is None):
            raise ValueError("系統身分沒有成員 id，其他角色一定要有")
        for name in ("action", "target_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} 要是小寫識別字")
        if self.target_id is not None and not isinstance(self.target_id, uuid.UUID):
            raise ValueError("target_id 有值時要是 UUID")
        if not isinstance(self.detail, Mapping):
            raise ValueError("detail 要是 JSON 物件")
        _check_json(self.detail)
        frozen = MappingProxyType(dict(self.detail))
        try:
            json.dumps(dict(frozen), allow_nan=False)
        except TypeError, ValueError:
            raise ValueError("detail 要寫得成 JSON") from None
        # 存回唯讀的複本，免得呼叫端留著的可變 dict 在驗證後被改掉。
        object.__setattr__(self, "detail", frozen)

    def detail_json(self) -> str:
        """`detail` 的 JSON 文字（寫進 jsonb 用）。"""
        return json.dumps(dict(self.detail), allow_nan=False, ensure_ascii=False, sort_keys=True)


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
        actor_role=AuditActorRole.SYSTEM,
        actor_member_id=None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail={} if detail is None else detail,
    )
