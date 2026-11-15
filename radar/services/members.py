"""允許名單與管理者（`radar members allow`／`set-admin`；ADR-0008 第 4 項、AD-23、Story 3.3）。

- actor 只能是 `SystemViewer`：第一位管理者還不存在時就要能跑（試用主機由 Kevin 經 SSH 直接執行，
  沒有 `--as`），所以是 ADR-0002 的「維運」身分。權限表的 `MANAGE_ALLOWLIST` 是 `ADMIN`，
  `SystemViewer` 過不了 `require`；表先不改（改表要改 PRD 字面與既有測試），這裡只收
  `SystemViewer`（型別＋執行時檢查），稽核記 `system`。要 Kevin 決定（spec 3.3 Design Notes）。
- 每次真的改到資料就在同一個交易寫一筆稽核；沒有改到（已在名單、本來就是管理者）不寫。
- 日誌與回傳值只有成員 ID，不帶 email。
"""

import structlog

from radar.domain.audit import system_audit_event
from radar.domain.auth import (
    MemberChange,
    MemberNotAllowedError,
    check_display_name,
    normalize_email,
)
from radar.domain.permissions import Action, PermissionDeniedError
from radar.domain.viewer import SystemViewer, Viewer
from radar.repo.audit import create_audit_event
from radar.repo.db import Database
from radar.repo.members import (
    create_allowed_member,
    find_allowed_member_by_email,
    update_member_admin,
)

log = structlog.get_logger(__name__)

# 稽核事件的動作與對象（小寫識別字，寫出去之後不改名）。
ALLOW_MEMBER_ACTION = "allow_member"
SET_MEMBER_ADMIN_ACTION = "set_member_admin"
MEMBER_TARGET = "member"


def _require_operator(actor: Viewer) -> None:
    """只有維運身分（`SystemViewer`）可以改允許名單。"""
    if not isinstance(actor, SystemViewer):
        raise PermissionDeniedError(actor, Action.MANAGE_ALLOWLIST)


def allow_member(
    actor: SystemViewer, database: Database, *, email: str, display_name: str
) -> MemberChange:
    """把 `email`（轉成小寫）加進允許名單，當下建立 `member`（`google_sub` 空的）並寫稽核。

    已經在名單裡時不變更、不寫稽核（`changed` 是 False，顯示名稱也不改）。

    Raises:
        PermissionDeniedError: `actor` 不是 `SystemViewer`。
        InvalidEmailError: email 的格式不對（還沒碰資料庫）。
        InvalidDisplayNameError: 名稱是空白、太長或含控制字元（還沒碰資料庫）。
    """
    _require_operator(actor)
    normalized = normalize_email(email)
    name = check_display_name(display_name)
    with database.transaction() as connection:
        existing = find_allowed_member_by_email(connection, normalized)
        if existing is not None:
            log.info("已經在允許名單，沒有變更", member_id=str(existing.member_id))
            return MemberChange(member_id=existing.member_id, changed=False)
        member_id = create_allowed_member(connection, email=normalized, display_name=name)
        create_audit_event(
            connection,
            system_audit_event(
                action=ALLOW_MEMBER_ACTION, target_type=MEMBER_TARGET, target_id=member_id
            ),
        )
    log.info("已加入允許名單", member_id=str(member_id))
    return MemberChange(member_id=member_id, changed=True)


def set_member_admin(actor: SystemViewer, database: Database, *, email: str) -> MemberChange:
    """把允許名單上 `email` 的成員設成管理者並寫稽核；本來就是管理者時不變更、不寫稽核。

    Raises:
        PermissionDeniedError: `actor` 不是 `SystemViewer`。
        InvalidEmailError: email 的格式不對（還沒碰資料庫）。
        MemberNotAllowedError: 這個 email 不在允許名單。
    """
    _require_operator(actor)
    normalized = normalize_email(email)
    with database.transaction() as connection:
        member = find_allowed_member_by_email(connection, normalized)
        if member is None:
            raise MemberNotAllowedError
        changed = update_member_admin(connection, member.member_id)
        if changed:
            create_audit_event(
                connection,
                system_audit_event(
                    action=SET_MEMBER_ADMIN_ACTION,
                    target_type=MEMBER_TARGET,
                    target_id=member.member_id,
                ),
            )
    if changed:
        log.info("已設成管理者", member_id=str(member.member_id))
    else:
        log.info("本來就是管理者，沒有變更", member_id=str(member.member_id))
    return MemberChange(member_id=member.member_id, changed=changed)
