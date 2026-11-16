"""登入、允許名單與 session 的形狀與規則（ADR-0008、ADR-0046、AD-23、Story 3.3）。

- session token：256 位元的隨機數（`secrets.token_urlsafe(32)`，43 個字元）；資料庫只存它的
  SHA-256（32 位元組），資料庫外洩也拿不到可以登入的值。
- email、顯示名稱、登入後的轉回路徑都是使用者輸入：這裡驗證，錯誤訊息不帶輸入的值（email 是個資，
  不進日誌與輸出）。
- 身分確認（開發的「選一位成員」、試用的 Access JWT）由注入的函式做，回 `ChosenMember` 或
  `VerifiedEmail`；被拒時丟 `LoginRejectedError`，原因是 `LoginRejection`。
"""

import re
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from radar.domain.fingerprints import session_token_sha256
from radar.domain.papers import check_storable_text
from radar.domain.viewer import MemberViewer

# --- session token ----------------------------------------------------------------------------

# 256 位元＝32 位元組；token_urlsafe 編成 43 個 base64url 字元（不含補位的 =）。
SESSION_TOKEN_BYTES: Final = 32
_TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{43}")
# 資料庫存的雜湊長度（`session.token_sha256` 的 CHECK 同一個數字）。
TOKEN_SHA256_BYTES: Final = 32


def new_session_token() -> str:
    """一個新的 session token（256 位元隨機數，base64url）。"""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def is_well_formed_token(value: object) -> bool:
    """`value` 是 `new_session_token` 產生的那種格式（43 個 base64url 字元）。"""
    return isinstance(value, str) and _TOKEN_PATTERN.fullmatch(value) is not None


def token_sha256(token: str) -> bytes:
    """token 的 SHA-256（32 位元組）：資料庫只存這個。

    Raises:
        ValueError: `token` 的格式不對（不先擋的話，任何字串都能拿來算雜湊查資料庫）。
    """
    if not is_well_formed_token(token):
        raise ValueError("session token 的格式不對")
    return session_token_sha256(token)


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """session 的期限（ADR-0008 第 2 項）：`idle` 沒使用就過期，從建立起最長 `max_age`。

    Raises:
        ValueError: 期限不是正的，或最長期限比閒置期限短。
    """

    idle: timedelta
    max_age: timedelta

    def __post_init__(self) -> None:
        if self.idle <= timedelta(0) or self.max_age <= timedelta(0):
            raise ValueError("session 的期限要是正的")
        if self.max_age < self.idle:
            raise ValueError("session 的最長期限不能比閒置期限短")


# 最後使用時間超過這麼久才更新（避免每個請求都寫一次資料庫；閒置期限以天計，差幾分鐘無妨）。
SESSION_ACTIVITY_WRITE_INTERVAL: Final = timedelta(minutes=5)


# --- email 與顯示名稱 -------------------------------------------------------------------------

EMAIL_MAX_LENGTH: Final = 254
_EMAIL_LOCAL_MAX_LENGTH: Final = 64
_EMAIL_LOCAL: Final = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*")
_EMAIL_LABEL: Final = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_EMAIL_DOMAIN: Final = re.compile(rf"{_EMAIL_LABEL}(?:\.{_EMAIL_LABEL})+")

DISPLAY_NAME_MAX_LENGTH: Final = 100
# 不准出現在顯示名稱的 Unicode 類別：控制字元（Cc）、格式字元（Cf，含雙向控制字元）、
# 行與段落分隔（Zl、Zp）、私用區（Co）、還沒分配（Cn）、單獨的代理字元（Cs）。
_DISPLAY_NAME_BANNED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cn", "Cs"})


class InvalidEmailError(ValueError):
    """email 的格式不對。訊息不帶輸入的值（email 是個資）。"""


class InvalidDisplayNameError(ValueError):
    """顯示名稱不合規定（空白、太長、含控制字元）。"""


def normalize_email(value: object) -> str:
    """驗證並轉成小寫的 email（允許名單與比對都用小寫，ADR-0008 第 1.4 項）。

    只收 ASCII：`本地部分@網域`，網域至少兩段、每段是英數與連字號；不去掉前後空白（含空白就拒絕）。

    Raises:
        InvalidEmailError: 不是字串、太長、格式不對；訊息不帶輸入的值。
    """
    if not isinstance(value, str):
        raise InvalidEmailError("email 要是字串")
    if not value or len(value) > EMAIL_MAX_LENGTH or not value.isascii():
        raise InvalidEmailError(f"email 要是 1～{EMAIL_MAX_LENGTH} 個 ASCII 字元")
    lowered = value.lower()
    local, separator, domain = lowered.rpartition("@")
    if (
        not separator
        or not local
        or len(local) > _EMAIL_LOCAL_MAX_LENGTH
        or _EMAIL_LOCAL.fullmatch(local) is None
        or _EMAIL_DOMAIN.fullmatch(domain) is None
    ):
        raise InvalidEmailError("email 的格式不對")
    return lowered


def check_display_name(value: object) -> str:
    """驗證成員的顯示名稱，回去掉前後空白的值（ADR-0008 第 6 項：由管理者用 CLI 填，必填）。

    Raises:
        InvalidDisplayNameError: 不是字串、只有空白、超過 100 個字元、含控制字元或格式字元。
    """
    if not isinstance(value, str):
        raise InvalidDisplayNameError("名稱要是字串")
    name = value.strip()
    if not name:
        raise InvalidDisplayNameError("名稱不能是空白")
    if len(name) > DISPLAY_NAME_MAX_LENGTH:
        raise InvalidDisplayNameError(f"名稱最多 {DISPLAY_NAME_MAX_LENGTH} 個字元")
    if any(unicodedata.category(char) in _DISPLAY_NAME_BANNED_CATEGORIES for char in name):
        raise InvalidDisplayNameError("名稱不能含控制字元、格式字元或分行")
    try:
        check_storable_text(name, "名稱")
    except ValueError as exc:
        raise InvalidDisplayNameError(str(exc)) from None
    return name


# --- 登入後的轉回路徑 -------------------------------------------------------------------------

DEFAULT_NEXT_PATH: Final = "/"
NEXT_PATH_MAX_LENGTH: Final = 2048


def safe_next_path(value: object) -> str:
    """登入後要轉回的路徑：只收站內路徑，其他一律改回 `/`（ADR-0008 第 1.6 項）。

    站內路徑：`/` 開頭、不是 `//`、不含反斜線、只有可印出的 ASCII（沒有空白與控制字元）、
    不超過 2048 個字元、解析後沒有協定與主機。
    """
    if not isinstance(value, str) or not value or len(value) > NEXT_PATH_MAX_LENGTH:
        return DEFAULT_NEXT_PATH
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return DEFAULT_NEXT_PATH
    if any(not ("\x21" <= char <= "\x7e") for char in value):
        return DEFAULT_NEXT_PATH
    try:
        parts = urlsplit(value)
    except ValueError:
        return DEFAULT_NEXT_PATH
    if parts.scheme or parts.netloc:
        return DEFAULT_NEXT_PATH
    return value


# --- 身分確認 ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FakeLoginRequest:
    """一次假登入請求帶來的東西：選的成員（開發）與 Access 的 JWT（試用，標頭的原值）。

    兩者都是不可信的輸入，由身分確認函式檢查。
    """

    member_id: uuid.UUID | None
    access_assertion: str | None


@dataclass(frozen=True, slots=True)
class ChosenMember:
    """開發環境：選了這位成員（還沒確認他在名單裡；登入時查）。"""

    member_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class VerifiedEmail:
    """試用環境：Access 的 JWT 驗過了，這是裡面的 email（小寫、等於管理者 email）。"""

    email: str


type LoginIdentity = ChosenMember | VerifiedEmail


class LoginRejection(StrEnum):
    """假登入被拒的原因。只記進日誌，回應一律是同一個代碼（不告訴對方哪一關沒過）。"""

    NO_MEMBER_CHOSEN = "no_member_chosen"  # 開發：沒有選人
    MEMBER_NOT_ALLOWED = "member_not_allowed"  # 開發：選的人不在允許名單
    MEMBER_CHOICE_NOT_ALLOWED = "member_choice_not_allowed"  # 試用：帶了 member_id（不能選人）
    ASSERTION_MISSING = "assertion_missing"  # 試用：沒有 Cf-Access-Jwt-Assertion
    ASSERTION_DUPLICATED = "assertion_duplicated"  # Cf-Access-Jwt-Assertion 出現不只一次
    ASSERTION_INVALID = "assertion_invalid"  # 試用：格式、簽章、iss、aud、期限、演算法任一不對
    ACCESS_KEYS_UNAVAILABLE = "access_keys_unavailable"  # 試用：取不到 Access 的公鑰
    EMAIL_NOT_ADMIN = "email_not_admin"  # 試用：JWT 的 email 不是管理者 email


class LoginRejectedError(Exception):
    """假登入被拒。訊息只有原因代碼，不帶 email、成員 ID 或 JWT 的內容。"""

    def __init__(self, reason: LoginRejection) -> None:
        self.reason = reason
        super().__init__(f"假登入被拒：{reason.value}")


# 管理者成員還沒建好時要先執行的兩個指令；email 用佔位字（不印真的管理者 email）。
ADMIN_SETUP_COMMANDS: Final = (
    "radar members allow <管理者 email> --name <名稱>",
    "radar members set-admin <管理者 email>",
)


class AdminMemberMissingError(Exception):
    """試用環境：管理者 email 對應的成員不在允許名單，或還不是管理者。"""

    def __init__(self) -> None:
        self.commands = ADMIN_SETUP_COMMANDS
        super().__init__(
            "管理者成員還沒建立：先執行 "
            + "，再執行 ".join(f"`{command}`" for command in ADMIN_SETUP_COMMANDS)
        )


# --- 成員與 session ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AllowedMember:
    """允許名單上的一位成員（不含 email：只有查找用到 email，查完不帶出去）。"""

    member_id: uuid.UUID
    display_name: str
    is_admin: bool

    @property
    def viewer(self) -> MemberViewer:
        return MemberViewer(member_id=self.member_id, is_admin=self.is_admin)


@dataclass(frozen=True, slots=True)
class MemberSummary:
    """畫面上要用的成員資料：ID 與顯示名稱（不含 email）。"""

    member_id: uuid.UUID
    display_name: str


@dataclass(frozen=True, slots=True)
class MemberChange:
    """`radar members allow`／`set-admin` 的結果：這位成員的 ID、這次有沒有改到資料。"""

    member_id: uuid.UUID
    changed: bool


class MemberNotAllowedError(Exception):
    """這個 email 不在允許名單。訊息不帶 email。"""

    def __init__(self) -> None:
        super().__init__("這個 email 不在允許名單；先執行 radar members allow")


@dataclass(frozen=True, slots=True)
class AuthenticatedMember:
    """有效 session 認出的成員（`last_seen_at` 用來決定要不要更新最後使用時間）。"""

    session_id: uuid.UUID
    member_id: uuid.UUID
    display_name: str
    is_admin: bool
    last_seen_at: datetime

    @property
    def viewer(self) -> MemberViewer:
        return MemberViewer(member_id=self.member_id, is_admin=self.is_admin)


@dataclass(frozen=True, slots=True)
class SessionOwner:
    """某個 session 是哪位成員的（登出用：不管 session 有沒有過期、成員還在不在名單）。"""

    session_id: uuid.UUID
    member_id: uuid.UUID
    is_admin: bool

    @property
    def viewer(self) -> MemberViewer:
        return MemberViewer(member_id=self.member_id, is_admin=self.is_admin)


@dataclass(frozen=True, slots=True)
class NewSession:
    """剛建立的 session：`token` 是要放進 cookie 的原值（資料庫只有它的雜湊），只在這裡出現一次。"""

    session_id: uuid.UUID
    member_id: uuid.UUID
    token: str

    def __repr__(self) -> str:
        # token 是登入憑證：印出物件（例如日誌、例外）時不帶它。
        return f"NewSession(session_id={self.session_id}, member_id={self.member_id})"
