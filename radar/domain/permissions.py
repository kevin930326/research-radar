"""寫入權限表（PRD FR-7 的權限表、AD-27）：全專案只有這一份。

每個會改資料的 services 函式，動資料之前先呼叫 `require(actor, 動作, 資源擁有者)`。
讀取規則不放這裡：誰看得到什麼只寫在 `radar/repo/visibility.py`（AD-4）。
"""

import uuid
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from radar.domain.viewer import MemberViewer, SystemViewer, Viewer


class Rule(StrEnum):
    """誰可以做。"""

    ANY_MEMBER = "any_member"  # 任何成員（含管理者）
    OWNER = "owner"  # 只有資源的擁有者；管理者不因為是管理者就通過
    ADMIN = "admin"  # 只有管理者
    SYSTEM = "system"  # 只有 `SystemViewer`（沒有特定使用者的工作）


class Action(StrEnum):
    """會改資料的動作。名稱是「動詞_名詞」。"""

    CORRECT_CLASSIFICATION = "correct_classification"
    CONFIRM_CONSTRUCT_MERGE = "confirm_construct_merge"
    MERGE_CONSTRUCTS = "merge_constructs"
    ADD_LIBRARY_ITEM = "add_library_item"
    REMOVE_LIBRARY_ITEM = "remove_library_item"
    UPLOAD_PDF = "upload_pdf"
    DELETE_PDF = "delete_pdf"
    MANAGE_ALLOWLIST = "manage_allowlist"
    MANAGE_SETTINGS = "manage_settings"
    INGEST_PAPERS = "ingest_papers"


# FR-7 權限表的寫入列。`PublicViewer`（訪客）一律唯讀，所以不在任何規則裡。
PERMISSIONS: Mapping[Action, Rule] = MappingProxyType(
    {
        # 修正分類：任何成員（修正事件只新增、不刪除）。
        Action.CORRECT_CLASSIFICATION: Rule.ANY_MEMBER,
        # 構面合併：成員可以在證據簡報流程中確認（待管理者複核，FR-21）；正式合併與推翻只有管理者。
        Action.CONFIRM_CONSTRUCT_MERGE: Rule.ANY_MEMBER,
        Action.MERGE_CONSTRUCTS: Rule.ADMIN,
        # 加入／移出個人文獻庫：只有本人。
        Action.ADD_LIBRARY_ITEM: Rule.OWNER,
        Action.REMOVE_LIBRARY_ITEM: Rule.OWNER,
        # 上傳 PDF：任何成員（上傳的人就是擁有者）；刪除 PDF：只有本人。
        Action.UPLOAD_PDF: Rule.ANY_MEMBER,
        Action.DELETE_PDF: Rule.OWNER,
        # 允許名單、主題、期刊等設定：只有管理者（CLI）。
        Action.MANAGE_ALLOWLIST: Rule.ADMIN,
        Action.MANAGE_SETTINGS: Rule.ADMIN,
        # 匯入與每週更新寫論文：只有系統工作。
        Action.INGEST_PAPERS: Rule.SYSTEM,
    }
)


class PermissionDeniedError(Exception):
    """這個身分不能做這個動作。訊息只帶動作與身分的種類，不帶成員 ID。"""

    def __init__(self, actor: Viewer, action: Action) -> None:
        self.action = action
        super().__init__(f"{type(actor).__name__} 不能做 {action}")


def is_allowed(actor: Viewer, action: Action, owner_id: uuid.UUID | None) -> bool:
    """`actor` 能不能對擁有者是 `owner_id` 的資源做 `action`（沒有擁有者的資源傳 None）。"""
    match PERMISSIONS[action], actor:
        case Rule.SYSTEM, SystemViewer():
            return True
        case Rule.ANY_MEMBER, MemberViewer():
            return True
        case Rule.ADMIN, MemberViewer(is_admin=True):
            return True
        case Rule.OWNER, MemberViewer(member_id=member_id):
            return owner_id is not None and member_id == owner_id
        case _:
            return False


def require(actor: Viewer, action: Action, owner_id: uuid.UUID | None) -> None:
    """不符合權限表就丟 `PermissionDeniedError`。

    Raises:
        PermissionDeniedError: `actor` 不能做 `action`。
    """
    if not is_allowed(actor, action, owner_id):
        raise PermissionDeniedError(actor, action)
