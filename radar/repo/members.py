"""`member`（成員）與 `allowed_email`（允許名單）：表的定義與唯一的寫入者（ADR-0008 第 4 項、
AD-23、Story 3.3）。

- `radar members allow` 當下就建 `member`（`google_sub` 空的，Epic 7 第一次 Google 登入時補上），
  同一句 SQL 寫 `allowed_email`（小寫 email）。一位成員一個 email，一個 email 一位成員（UNIQUE）。
- email 只用來查找，查找結果不帶出 email（`AllowedMember` 沒有這個欄位）。
- 查找沒有 viewer（ADR-0002 的列名例外）：登入流程要先查名單才知道是誰；列在
  `tests/arch/test_repo_rules.py` 的 `INFRASTRUCTURE_EXCEPTIONS`。
- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。稽核在 `services/members.py`
  同一個交易寫。
"""

import uuid
from typing import Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from radar.domain.auth import AllowedMember
from radar.repo.models import Base

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0020 建表） -----------------------------

member_table = Table(
    "member",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("display_name", Text, nullable=False),
    Column("is_admin", Boolean, nullable=False),
    # Google 的使用者識別碼（Epic 7 第一次登入時補上）；之後以它認人。
    Column("google_sub", Text),
    Column("first_login_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("google_sub"),
    CheckConstraint(
        "display_name ~ '[^[:space:]]' AND char_length(display_name) <= 100",
        name="display_name_format",
    ),
    CheckConstraint("google_sub IS NULL OR google_sub <> ''", name="google_sub_not_empty"),
)

allowed_email_table = Table(
    "allowed_email",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("email", Text, nullable=False),
    Column("member_id", Uuid, ForeignKey("member.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("email"),
    # 一位成員一個 email；也當外鍵的索引。
    UniqueConstraint("member_id"),
    CheckConstraint(
        "email = lower(email) AND char_length(email) <= 254"
        " AND email ~ '^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$'",
        name="email_format",
    ),
)

# --- SQL ----------------------------------------------------------------------------------

# 成員與允許名單同一句寫入：email 已經在名單裡時 UNIQUE 擋下、整個交易退回，不留沒有 email 的成員。
_INSERT_MEMBER: Final = text(
    "WITH new_member AS ("
    " INSERT INTO member (display_name, is_admin) VALUES (:display_name, false) RETURNING id)"
    " INSERT INTO allowed_email (email, member_id) SELECT :email, id FROM new_member"
    " RETURNING member_id"
)
_SET_ADMIN: Final = text(
    "UPDATE member SET is_admin = true WHERE id = :member_id AND NOT is_admin RETURNING id"
)
_SELECT_MEMBER: Final = (
    "SELECT m.id, m.display_name, m.is_admin"
    " FROM member AS m JOIN allowed_email AS a ON a.member_id = m.id"
)
_FIND_BY_EMAIL: Final = text(f"{_SELECT_MEMBER} WHERE a.email = :email")
_FIND_BY_ID: Final = text(f"{_SELECT_MEMBER} WHERE m.id = :member_id")
_LIST: Final = text(f"{_SELECT_MEMBER} ORDER BY m.id")


def _member(row: tuple[object, object, object]) -> AllowedMember:
    member_id, display_name, is_admin = row
    return AllowedMember(
        member_id=uuid.UUID(str(member_id)), display_name=str(display_name), is_admin=bool(is_admin)
    )


# --- 寫入 ---------------------------------------------------------------------------------


def create_allowed_member(connection: Connection, *, email: str, display_name: str) -> uuid.UUID:
    """新增一位成員（不是管理者、沒有 `google_sub`）並把 `email` 加進允許名單，回成員 ID。

    `email`、`display_name` 要先經 `domain/auth` 的 `normalize_email`、`check_display_name`。
    在呼叫端開好的交易裡做，不 commit。

    Raises:
        sqlalchemy.exc.IntegrityError: email 已經在名單裡（呼叫端先查過；兩個指令同時跑
            時才會發生）。
    """
    value = connection.execute(
        _INSERT_MEMBER, {"email": email, "display_name": display_name}
    ).scalar_one()
    return uuid.UUID(str(value))


def update_member_admin(connection: Connection, member_id: uuid.UUID) -> bool:
    """把這位成員設成管理者；本來就是（或沒有這位成員）回 False。不 commit。"""
    return connection.execute(_SET_ADMIN, {"member_id": member_id}).scalar_one_or_none() is not None


# --- 查找（登入流程用，沒有 viewer） ----------------------------------------------------------


def find_allowed_member_by_email(connection: Connection, email: str) -> AllowedMember | None:
    """允許名單上 email 是 `email`（小寫）的成員；不在名單回 None。"""
    row = connection.execute(_FIND_BY_EMAIL, {"email": email}).one_or_none()
    return None if row is None else _member(row.tuple())


def find_allowed_member_by_id(connection: Connection, member_id: uuid.UUID) -> AllowedMember | None:
    """允許名單上 ID 是 `member_id` 的成員；不存在或已經不在名單回 None。"""
    row = connection.execute(_FIND_BY_ID, {"member_id": member_id}).one_or_none()
    return None if row is None else _member(row.tuple())


def list_allowed_members(connection: Connection) -> list[AllowedMember]:
    """允許名單上的每一位成員，依加入的先後排序（開發環境的假登入選人用）。

    成員 ID 是 UUIDv7，照 ID 排就是加入的先後；不照名稱排，免得順序跟著資料庫的定序變。
    """
    return [_member(row.tuple()) for row in connection.execute(_LIST)]
