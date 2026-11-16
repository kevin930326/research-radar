"""試用環境的假登入：驗 Cloudflare Access 加在請求上的 JWT（`Cf-Access-Jwt-Assertion`）。

AR-65 選項 A、ADR-0008（第 1.3 項的驗簽規則照搬）、ADR-0046。不讓函式庫替我們解讀型別：

- 標頭（header）自己解析：`alg` 一定要是字串 `RS256`、要有字串 `kid`、不收 `crit`。
- 公鑰從 Access 的金鑰清單（JWKS）只以 RSA 公鑰的型別載入（只取 `kty`、`n`、`e`，不收私鑰欄位），
  `use` 有寫就要是 `sig`、`alg` 有寫就要是 `RS256`，金鑰至少 2048 位元。
- 驗簽交給 joserfc（`jwt.decode` 只驗簽、不驗 claims），允許的演算法只有 `RS256`。
- claims 全部自己檢查型別與值：`iss` 是字串而且等於 `https://<team 網域>`；`aud` 是字串時要完全
  相等，是陣列時每一項都是字串而且含設定的 audience，其他型別一律拒絕；`exp` 一定要有、是整數
  （布林不算）而且還沒到；`nbf` 可以沒有這個欄位，有的話（含 `null`）一定要是整數而且已經到了
  （容許主機時鐘慢 `NOT_BEFORE_LEEWAY`，恰好 60 秒還收；`exp` 不容許）；`email` 是字串、轉成
  小寫後格式正確。
- 公鑰取得是注入的函式（`fetch_jwks`）：正式由 `fetch_access_jwks` 經 httpx2 取，測試傳假的函式，
  不連 Cloudflare（ADR-0008 第 9 項的做法，不另建 port）。

任何一關沒過都丟 `LoginRejectedError`（原因只進日誌，回應不說是哪一關）。日誌不記 JWT 與 email。
"""

import base64
import binascii
import hmac
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, Final, TypeGuard

import httpx2
import structlog
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey

from radar.domain.auth import (
    FakeLoginRequest,
    InvalidEmailError,
    LoginRejectedError,
    LoginRejection,
    VerifiedEmail,
    normalize_email,
)

log = structlog.get_logger(__name__)

ALGORITHM: Final = "RS256"
# Access 的公鑰清單在 team 網域底下的這個路徑。
JWKS_PATH: Final = "/cdn-cgi/access/certs"
# JWT 的長度上限（Access 的 token 約 1～2 KB；擋掉拿超大標頭耗資源的請求）。
ASSERTION_MAX_LENGTH: Final = 8192
MIN_RSA_KEY_BITS: Final = 2048
# 主機時鐘可能比 Cloudflare 慢一點：`nbf` 容許晚這麼久；`exp` 不放寬。
NOT_BEFORE_LEEWAY: Final = timedelta(seconds=60)
_SEGMENT: Final = re.compile(r"[A-Za-z0-9_-]+")
_KID_MAX_LENGTH: Final = 256

type FetchJwks = Callable[[], Mapping[str, Any]]


def _invalid(detail: str) -> LoginRejectedError:
    log.warning("Access 的 JWT 沒有通過驗證", check=detail)
    return LoginRejectedError(LoginRejection.ASSERTION_INVALID)


def _decode_segment(segment: str) -> object:
    """JWT 的一段（base64url、不補位）解成 JSON；解不開丟 ValueError。"""
    padded = segment + "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except binascii.Error, UnicodeDecodeError, ValueError, RecursionError:
        raise ValueError("JWT 的一段解不開") from None


def _header(token: str) -> tuple[str, str]:
    """檢查 JWT 的外形與標頭，回（演算法, kid）。"""
    if not isinstance(token, str) or not token or len(token) > ASSERTION_MAX_LENGTH:
        raise _invalid("length")
    parts = token.split(".")
    if len(parts) != 3 or not all(_SEGMENT.fullmatch(part) for part in parts):
        raise _invalid("shape")
    try:
        header = _decode_segment(parts[0])
    except ValueError:
        raise _invalid("header_json") from None
    if not isinstance(header, dict):
        raise _invalid("header_object")
    alg, kid = header.get("alg"), header.get("kid")
    if not isinstance(alg, str) or alg != ALGORITHM:
        raise _invalid("alg")
    if not isinstance(kid, str) or not kid or len(kid) > _KID_MAX_LENGTH:
        raise _invalid("kid")
    if "crit" in header:
        raise _invalid("crit")
    return alg, kid


def _public_key(jwks: object, kid: str) -> RSAKey:
    """金鑰清單裡 `kid` 對到的 RSA 公鑰（只取 `kty`、`n`、`e`）。"""
    keys = jwks.get("keys") if isinstance(jwks, Mapping) else None
    if not isinstance(keys, list):
        raise _invalid("jwks_shape")
    for entry in keys:
        if not isinstance(entry, Mapping) or entry.get("kid") != kid:
            continue
        if entry.get("kty") != "RSA":
            continue
        if entry.get("use", "sig") != "sig" or entry.get("alg", ALGORITHM) != ALGORITHM:
            continue
        n, e = entry.get("n"), entry.get("e")
        if not isinstance(n, str) or not isinstance(e, str):
            continue
        # 先看模數的位元數：太短的金鑰不匯入（joserfc 匯入時只發警告、照樣能用）。
        if _modulus_bits(n) < MIN_RSA_KEY_BITS:
            raise _invalid("jwk_size")
        try:
            key = RSAKey.import_key({"kty": "RSA", "n": n, "e": e})
        except JoseError, ValueError, TypeError:
            raise _invalid("jwk_import") from None
        return key
    raise _invalid("kid_not_found")


def _modulus_bits(n: str) -> int:
    """JWK 的 `n`（base64url 的大端整數）有幾個位元；解不開回 0。"""
    if _SEGMENT.fullmatch(n) is None:
        return 0
    try:
        raw = base64.urlsafe_b64decode((n + "=" * (-len(n) % 4)).encode("ascii"))
    except binascii.Error, ValueError:
        return 0
    return int.from_bytes(raw, "big").bit_length()


def _is_int(value: object) -> TypeGuard[int]:
    """JSON 的整數：`int` 而且不是布林（bool 是 int 的子類別，不算數字）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _check_audience(value: object, audience: str) -> bool:
    # 公開的值，不必比較固定時間；hmac.compare_digest 遇到非 ASCII 字串會丟 TypeError。
    if isinstance(value, str):
        return value == audience
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return audience in value
    return False


def verify_access_jwt(
    token: str,
    *,
    team_domain: str,
    audience: str,
    fetch_jwks: FetchJwks,
    now: datetime,
) -> str:
    """驗 Access 的 JWT，回裡面的 email（小寫）。

    Raises:
        LoginRejectedError: 外形、標頭、金鑰、簽章、`iss`、`aud`、`exp`、`nbf`、`email` 任一不對
            （`ASSERTION_INVALID`），或取不到公鑰（`ACCESS_KEYS_UNAVAILABLE`）。
    """
    _alg, kid = _header(token)
    try:
        jwks = fetch_jwks()
    except Exception as exc:
        # 例外訊息可能帶網址或回應內容，只記類別名稱。
        log.warning("取不到 Access 的公鑰", error_type=type(exc).__name__)
        raise LoginRejectedError(LoginRejection.ACCESS_KEYS_UNAVAILABLE) from None
    key = _public_key(jwks, kid)
    try:
        decoded = jwt.decode(token, key, algorithms=[ALGORITHM])
    except JoseError, ValueError, TypeError:
        raise _invalid("signature") from None
    claims: Mapping[str, object] = decoded.claims
    if decoded.header.get("alg") != ALGORITHM:
        raise _invalid("alg")

    issuer = claims.get("iss")
    if not isinstance(issuer, str) or issuer != f"https://{team_domain}":
        raise _invalid("iss")
    if not _check_audience(claims.get("aud"), audience):
        raise _invalid("aud")
    timestamp = now.timestamp()
    # `exp` 一定要有；缺欄位與 null 都不是整數，一樣拒絕。
    expires = claims.get("exp")
    if not _is_int(expires) or expires <= timestamp:
        raise _invalid("exp")
    # `nbf` 可以沒有這個欄位；有這個欄位（含 null）就一定要是整數。
    if "nbf" in claims:
        not_before = claims["nbf"]
        if not _is_int(not_before) or not_before > timestamp + NOT_BEFORE_LEEWAY.total_seconds():
            raise _invalid("nbf")
    email = claims.get("email")
    try:
        return normalize_email(email)
    except InvalidEmailError:
        raise _invalid("email") from None


def confirm_access_identity(
    request: FakeLoginRequest,
    *,
    team_domain: str,
    audience: str,
    admin_email: str,
    fetch_jwks: FetchJwks,
    clock: Callable[[], datetime],
) -> VerifiedEmail:
    """試用環境的假登入：不能選人；請求上的 Access JWT 要有效，email 要等於管理者 email。

    Raises:
        LoginRejectedError: 帶了 `member_id`、沒有 JWT、JWT 沒過、email 不是管理者。
    """
    if request.member_id is not None:
        raise LoginRejectedError(LoginRejection.MEMBER_CHOICE_NOT_ALLOWED)
    if request.access_assertion is None:
        raise LoginRejectedError(LoginRejection.ASSERTION_MISSING)
    email = verify_access_jwt(
        request.access_assertion,
        team_domain=team_domain,
        audience=audience,
        fetch_jwks=fetch_jwks,
        now=clock(),
    )
    if not hmac.compare_digest(email, admin_email):
        raise LoginRejectedError(LoginRejection.EMAIL_NOT_ADMIN)
    return VerifiedEmail(email=email)


def fetch_access_jwks(
    team_domain: str, *, timeout: float = 5.0, transport: httpx2.BaseTransport | None = None
) -> dict[str, Any]:
    """向 `https://<team 網域>/cdn-cgi/access/certs` 取 Access 的公鑰清單（JWKS）。

    不重試、不跟轉址。

    Raises:
        AccessKeysError: 連不上、逾時、狀態碼不是 200、回應不是 JSON 物件。
    """
    url = f"https://{team_domain}{JWKS_PATH}"
    try:
        with httpx2.Client(timeout=timeout, transport=transport) as client:
            response = client.get(url)
    except httpx2.HTTPError as exc:
        raise AccessKeysError(type(exc).__name__) from None
    if response.status_code != 200:
        raise AccessKeysError(f"HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise AccessKeysError("回應不是 JSON") from None
    if not isinstance(body, dict):
        raise AccessKeysError("回應不是 JSON 物件")
    return body


class AccessKeysError(Exception):
    """取不到 Access 的公鑰清單。訊息只有錯誤類別或狀態碼。"""
