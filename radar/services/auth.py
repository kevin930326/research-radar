"""登入與 session（ADR-0008 第 2 項、ADR-0046、AD-23、Story 3.3）。

- 入口函式（`authenticate_session`、`log_out_session`、`log_in_with_fake_login`、
  `list_fake_login_members`）自己不寫資料：認出是哪位成員之後，交給以那位成員的 `MemberViewer`
  當 actor 的公開函式寫（`start_member_session`、`record_session_activity`、`end_session`）。
- 登出只看 token 雜湊，不要求 session 仍然有效：過期或成員已被移出名單的 session 也刪掉那一列
  （不然之後調長期限或把人加回名單，已經登出的 token 會重新有效）。
- 假登入先在交易外確認身分（試用環境要取 Access 的公鑰，不佔著交易等網路），再開交易找成員、
  建 session。身分確認是注入的函式（開發：`adapters/fake/login.py`；試用：Access 的 JWT），
  不建 Protocol（ADR-0001、ADR-0008 第 9 項）。
- 最後使用時間超過 `SESSION_ACTIVITY_WRITE_INTERVAL` 才更新：這是 session 自己的紀錄，不是業務
  資料，也不是 CSRF 能利用的寫入（只會延長受害者自己的 session）。
- 日誌只記成員 ID 與原因代碼，不記 email、token 或 JWT。
"""

import uuid
from collections.abc import Callable
from datetime import datetime

import structlog

from radar.domain.auth import (
    SESSION_ACTIVITY_WRITE_INTERVAL,
    AdminMemberMissingError,
    AllowedMember,
    AuthenticatedMember,
    ChosenMember,
    FakeLoginRequest,
    LoginIdentity,
    LoginRejectedError,
    LoginRejection,
    MemberSummary,
    NewSession,
    SessionPolicy,
    VerifiedEmail,
    is_well_formed_token,
    new_session_token,
    token_sha256,
)
from radar.domain.viewer import MemberViewer
from radar.repo.db import Connection, Database
from radar.repo.members import (
    find_allowed_member_by_email,
    find_allowed_member_by_id,
    list_allowed_members,
)
from radar.repo.sessions import (
    create_session,
    delete_session,
    find_active_session,
    find_session_owner,
    update_session_last_seen,
)

log = structlog.get_logger(__name__)

type ConfirmIdentity = Callable[[FakeLoginRequest], LoginIdentity]


def authenticate_session(
    database: Database, token: str | None, *, policy: SessionPolicy, now: datetime
) -> AuthenticatedMember | None:
    """cookie 裡的 token 對到的有效 session 與成員；沒有、格式不對、找不到、過期、被撤銷、成員已經
    不在允許名單都回 None。最後使用時間超過 5 分鐘才更新。"""
    if token is None or not is_well_formed_token(token):
        return None
    with database.transaction() as connection:
        member = find_active_session(connection, token_sha256(token), policy=policy, now=now)
        if member is None:
            return None
        if now - member.last_seen_at >= SESSION_ACTIVITY_WRITE_INTERVAL:
            record_session_activity(
                member.viewer, connection, session_id=member.session_id, now=now
            )
    return member


def record_session_activity(
    actor: MemberViewer, connection: Connection, *, session_id: uuid.UUID, now: datetime
) -> None:
    """把 `actor` 自己的這個 session 的最後使用時間改成 `now`。在呼叫端開好的交易裡做。"""
    update_session_last_seen(connection, session_id=session_id, member_id=actor.member_id, now=now)


def log_out_session(database: Database, token: str) -> None:
    """登出：cookie 裡的 token 對到的 session 不管有沒有過期、成員還在不在允許名單，都刪掉。

    格式不對或找不到就什麼都不做（登出一律成功）。
    """
    if not is_well_formed_token(token):
        return
    with database.transaction() as connection:
        owner = find_session_owner(connection, token_sha256(token))
        if owner is None:
            return
        ended = end_session(owner.viewer, connection, session_id=owner.session_id)
    # 交易 commit 成功才記：commit 失敗時那一列還在，不能記成已登出。
    if ended:
        log.info("已登出", member_id=str(owner.viewer.member_id))


def end_session(actor: MemberViewer, connection: Connection, *, session_id: uuid.UUID) -> bool:
    """刪掉 `actor` 自己的這個 session（之後同一個 token 立刻失效）；回有沒有刪到。

    在呼叫端開好的交易裡做、不記日誌：「已登出」由呼叫端在交易 commit 之後記。
    """
    return delete_session(connection, session_id=session_id, member_id=actor.member_id)


def list_fake_login_members(database: Database) -> list[MemberSummary]:
    """開發環境假登入可以選的成員（允許名單上的每一位，依顯示名稱排序；不含 email）。"""
    with database.transaction() as connection:
        members = list_allowed_members(connection)
    return [MemberSummary(member_id=m.member_id, display_name=m.display_name) for m in members]


def log_in_with_fake_login(
    database: Database,
    request: FakeLoginRequest,
    *,
    confirm: ConfirmIdentity,
    now: datetime,
) -> NewSession:
    """假登入：`confirm` 確認身分（交易外），再找允許名單上的成員、建 session。

    - 開發（`ChosenMember`）：選的人要在允許名單。
    - 試用（`VerifiedEmail`）：email 對到的成員要在允許名單而且是管理者；不然丟
      `AdminMemberMissingError`（呼叫端印出要先執行的兩個指令）。

    Raises:
        LoginRejectedError: 身分確認沒過，或選的人不在允許名單。
        AdminMemberMissingError: 試用環境的管理者成員不存在或不是管理者。
    """
    try:
        identity = confirm(request)
    except LoginRejectedError as exc:
        log.warning("假登入被拒", reason=exc.reason.value)
        raise
    with database.transaction() as connection:
        member = _find_member(connection, identity)
        session = start_member_session(member.viewer, connection, now=now)
    log.info("假登入成功", member_id=str(member.member_id))
    return session


def _find_member(connection: Connection, identity: LoginIdentity) -> AllowedMember:
    match identity:
        case ChosenMember(member_id=member_id):
            member = find_allowed_member_by_id(connection, member_id)
            if member is None:
                log.warning("假登入被拒", reason=LoginRejection.MEMBER_NOT_ALLOWED.value)
                raise LoginRejectedError(LoginRejection.MEMBER_NOT_ALLOWED)
            return member
        case VerifiedEmail(email=email):
            admin = find_allowed_member_by_email(connection, email)
            if admin is None or not admin.is_admin:
                raise AdminMemberMissingError
            return admin
        case _:
            raise TypeError(f"不認得的身分：{type(identity).__name__}")


def start_member_session(
    actor: MemberViewer, connection: Connection, *, now: datetime
) -> NewSession:
    """替 `actor` 建一個新的 session，回 cookie 要放的 token（資料庫只存它的 SHA-256）。

    在呼叫端開好的交易裡做。
    """
    token = new_session_token()
    session_id = create_session(
        connection, member_id=actor.member_id, token_sha256=token_sha256(token), now=now
    )
    return NewSession(session_id=session_id, member_id=actor.member_id, token=token)
