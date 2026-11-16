"""`session`（登入狀態）：表的定義與唯一的寫入者（ADR-0008 第 2 項、AD-23、Story 3.3）。

- 瀏覽器拿到的 token 不進資料庫，只存它的 SHA-256（`token_sha256`，32 位元組，UNIQUE）。
- 時間（`created_at`、`last_seen_at`）由呼叫端的時鐘傳入，查有效 session 時也用同一個時鐘：
  閒置超過期限、從建立起超過最長期限，就當作不存在。過期的列由之後的 sweep 清（這裡不刪）。
- 成員從允許名單移除（`allowed_email` 沒有他的列）後，他的 session 立刻失效：查找時一起比對
  允許名單。撤銷（`radar members revoke`，Story 7.2）在同一個交易刪掉他的 session。
- 查找沒有 viewer（ADR-0002 的列名例外）：要先查 session 才知道是誰；列在
  `tests/arch/test_repo_rules.py` 的 `INFRASTRUCTURE_EXCEPTIONS`。登出用的
  `find_session_owner` 不看期限與允許名單：已經過期的 session 登出時也要刪掉那一列。
- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。
"""

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    Table,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import BYTEA

from radar.domain.auth import (
    TOKEN_SHA256_BYTES,
    AuthenticatedMember,
    SessionOwner,
    SessionPolicy,
)
from radar.repo.models import Base

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0019 建表） -----------------------------

session_table = Table(
    "session",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("member_id", Uuid, ForeignKey("member.id"), nullable=False),
    Column("token_sha256", BYTEA, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("token_sha256"),
    Index("ix_session_member_id", "member_id"),
    CheckConstraint(
        f"octet_length(token_sha256) = {TOKEN_SHA256_BYTES}", name="token_sha256_length"
    ),
    CheckConstraint("last_seen_at >= created_at", name="last_seen_after_created"),
)

# --- SQL ----------------------------------------------------------------------------------

_INSERT: Final = text(
    "INSERT INTO session (member_id, token_sha256, created_at, last_seen_at)"
    " VALUES (:member_id, :token_sha256, :now, :now)"
    " RETURNING id"
)
# 只往後更新：時鐘回撥或兩個請求同時更新時不會把時間改舊。
_TOUCH: Final = text(
    "UPDATE session SET last_seen_at = :now"
    " WHERE id = :session_id AND member_id = :member_id AND last_seen_at < :now"
)
_DELETE: Final = text(
    "DELETE FROM session WHERE id = :session_id AND member_id = :member_id RETURNING id"
)
_FIND_ACTIVE: Final = text(
    "SELECT s.id, s.member_id, m.display_name, m.is_admin, s.last_seen_at"
    " FROM session AS s"
    " JOIN member AS m ON m.id = s.member_id"
    " JOIN allowed_email AS a ON a.member_id = s.member_id"
    " WHERE s.token_sha256 = :token_sha256"
    " AND s.last_seen_at > :idle_cutoff AND s.created_at > :max_cutoff"
)

# 登出用：只看 token 雜湊，不管期限與允許名單（過期或被移出名單的 session 也要刪得掉）。
_FIND_OWNER: Final = text(
    "SELECT s.id, s.member_id, m.is_admin"
    " FROM session AS s"
    " JOIN member AS m ON m.id = s.member_id"
    " WHERE s.token_sha256 = :token_sha256"
)


# --- 寫入 ---------------------------------------------------------------------------------


def create_session(
    connection: Connection, *, member_id: uuid.UUID, token_sha256: bytes, now: datetime
) -> uuid.UUID:
    """新增一個 session（建立與最後使用時間都是 `now`），回它的 id。不 commit。

    Raises:
        ValueError: `token_sha256` 不是 32 位元組，或 `now` 沒有時區。
    """
    _check_digest(token_sha256)
    _check_aware(now)
    value = connection.execute(
        _INSERT, {"member_id": member_id, "token_sha256": token_sha256, "now": now}
    ).scalar_one()
    return uuid.UUID(str(value))


def update_session_last_seen(
    connection: Connection, *, session_id: uuid.UUID, member_id: uuid.UUID, now: datetime
) -> None:
    """把這位成員的這個 session 的最後使用時間改成 `now`（只往後改）。不 commit。"""
    _check_aware(now)
    connection.execute(_TOUCH, {"session_id": session_id, "member_id": member_id, "now": now})


def delete_session(connection: Connection, *, session_id: uuid.UUID, member_id: uuid.UUID) -> bool:
    """刪掉這位成員的這個 session；有刪到回 True。不 commit。"""
    return (
        connection.execute(
            _DELETE, {"session_id": session_id, "member_id": member_id}
        ).scalar_one_or_none()
        is not None
    )


# --- 查找（登入流程用，沒有 viewer） ----------------------------------------------------------


def find_active_session(
    connection: Connection, token_sha256: bytes, *, policy: SessionPolicy, now: datetime
) -> AuthenticatedMember | None:
    """雜湊是 `token_sha256` 的有效 session 與它的成員；找不到、閒置超過期限、超過最長期限、
    成員已經不在允許名單時都回 None。

    Raises:
        ValueError: `token_sha256` 不是 32 位元組，或 `now` 沒有時區。
    """
    _check_digest(token_sha256)
    _check_aware(now)
    row = connection.execute(
        _FIND_ACTIVE,
        {
            "token_sha256": token_sha256,
            "idle_cutoff": now - policy.idle,
            "max_cutoff": now - policy.max_age,
        },
    ).one_or_none()
    if row is None:
        return None
    session_id, member_id, display_name, is_admin, last_seen_at = row.tuple()
    return AuthenticatedMember(
        session_id=uuid.UUID(str(session_id)),
        member_id=uuid.UUID(str(member_id)),
        display_name=str(display_name),
        is_admin=bool(is_admin),
        last_seen_at=last_seen_at,
    )


def find_session_owner(connection: Connection, token_sha256: bytes) -> SessionOwner | None:
    """雜湊是 `token_sha256` 的 session 與它的成員，不管有沒有過期、成員還在不在允許名單；
    找不到回 None。登出用（之後交給 `delete_session` 刪）。

    Raises:
        ValueError: `token_sha256` 不是 32 位元組。
    """
    _check_digest(token_sha256)
    row = connection.execute(_FIND_OWNER, {"token_sha256": token_sha256}).one_or_none()
    if row is None:
        return None
    session_id, member_id, is_admin = row.tuple()
    return SessionOwner(
        session_id=uuid.UUID(str(session_id)),
        member_id=uuid.UUID(str(member_id)),
        is_admin=bool(is_admin),
    )


def _check_digest(value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) != TOKEN_SHA256_BYTES:
        raise ValueError(f"token_sha256 要是 {TOKEN_SHA256_BYTES} 位元組")


def _check_aware(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("時間要帶時區")
