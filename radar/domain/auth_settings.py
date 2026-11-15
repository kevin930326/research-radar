"""登入設定的型別與合法組合（ADR-0008、ADR-0046、AR-65 選項 A、Story 3.3）。

值只由 `radar/config.py` 的 `load_auth_settings` 從環境變數讀進來；組合不合法時 api 不啟動。
環境變數名稱是 `RADAR_` 加欄位名稱的大寫（`environment` → `RADAR_ENVIRONMENT`）。

三種環境與假登入：
- 開發（`development`）＋假登入：可以啟動，選一位允許名單上的成員登入。
- 試用（`trial`）＋假登入：一定要設定 Access 的 team 網域與 audience、管理者 email、站台來源
  （https），每次假登入都驗 `Cf-Access-Jwt-Assertion`，只登入管理者這一位。
- 正式（`production`，沒設時的預設）＋假登入：不啟動。
- 假登入與 Google 登入同時開：不啟動（Google 登入在 Epic 7；這裡先只有旗標）。
"""

import re
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from radar.domain.auth import InvalidEmailError, SessionPolicy, normalize_email


class Environment(StrEnum):
    """這個行程跑在哪一種環境。沒設定時是正式，假登入一律關著。"""

    DEVELOPMENT = "development"
    TRIAL = "trial"
    PRODUCTION = "production"


class FakeLoginMode(StrEnum):
    """假登入怎麼確認身分。"""

    PICK_MEMBER = "pick_member"  # 開發：選一位允許名單上的成員
    ACCESS_ADMIN = "access_admin"  # 試用：驗 Access 的 JWT，只登入管理者


# 開發環境沒設 `RADAR_SITE_ORIGIN` 時接受的來源（compose 的 api 綁在 127.0.0.1:8000）。
DEVELOPMENT_ORIGINS: Final = frozenset({"http://localhost:8000", "http://127.0.0.1:8000"})
_ORIGIN_SCHEMES: Final = frozenset({"http", "https"})
_HOST_LABEL: Final = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TEAM_DOMAIN: Final = re.compile(rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})+")
_AUDIENCE: Final = re.compile(r"[\x21-\x7e]{1,256}")


class AuthSettings(BaseModel):
    """api 的登入設定（只有 api 讀）。管理者 email 是個資，用 SecretStr：印出設定時只有星號。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: Environment = Environment.PRODUCTION
    # 假登入（AD-2 的登入測試替身）；Google 登入（Epic 7）。兩個不能同時開。
    fake_login: bool = False
    google_login: bool = False
    # 管理者 email（小寫）：只從環境變數讀，不進 repo 的任何檔案、不進日誌。
    admin_email: SecretStr | None = None
    # Cloudflare Access 的 team 網域（例如 `myteam.cloudflareaccess.com`，不含 https://）與
    # 應用程式的 audience（AUD tag）。
    access_team_domain: str | None = None
    access_audience: str | None = None
    # 本站的來源（`https://radar.example.com`，沒有路徑與結尾斜線）：會改資料的請求，`Origin`
    # 一定要等於它。
    site_origin: str | None = None
    # session 的期限（天，ADR-0008 第 2 項）。
    session_idle_days: int = Field(default=14, ge=1, le=365)
    session_max_days: int = Field(default=30, ge=1, le=365)

    @field_validator("admin_email")
    @classmethod
    def _check_admin_email(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            return SecretStr(normalize_email(value.get_secret_value()))
        except InvalidEmailError:
            # 訊息不帶值（email 是個資）。
            raise ValueError("email 的格式不對（要像 name@example.com）") from None

    @field_validator("access_team_domain")
    @classmethod
    def _check_team_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _TEAM_DOMAIN.fullmatch(value) is None or len(value) > 253:
            raise ValueError(
                "要是小寫的主機名稱（例如 myteam.cloudflareaccess.com），不含 https://"
            )
        return value

    @field_validator("access_audience")
    @classmethod
    def _check_audience(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _AUDIENCE.fullmatch(value) is None:
            raise ValueError("要是 1～256 個可印出的 ASCII 字元，不含空白")
        return value

    @field_validator("site_origin")
    @classmethod
    def _check_site_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parts = urlsplit(value)
            hostname, port = parts.hostname, parts.port
        except ValueError:
            raise ValueError("來源無法解析") from None
        if (
            parts.scheme not in _ORIGIN_SCHEMES
            or not hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
            or "?" in value
            or "#" in value
        ):
            raise ValueError("要是 https://主機[:連接埠]（沒有路徑、結尾斜線、查詢字串或帳密）")
        origin = f"{parts.scheme}://{hostname}" + ("" if port is None else f":{port}")
        if origin != value:
            raise ValueError("要全部小寫，而且和瀏覽器送的 Origin 寫法相同")
        return value


def auth_settings_problems(settings: AuthSettings, env_name: Callable[[str], str]) -> list[str]:
    """設定組合的問題（空清單＝可以啟動）。每條寫明環境變數名稱與原因，不含任何設定值。

    `env_name` 把欄位名稱換成環境變數名稱（`radar/config.py` 的 `env_name`；domain 不能
    import config）。
    """
    problems: list[str] = []
    fake = env_name("fake_login")
    if settings.fake_login and settings.google_login:
        problems.append(
            f"{fake} 與 {env_name('google_login')} 不能同時開（假登入與 Google 登入不能並存）"
        )
    if settings.fake_login and settings.environment is Environment.PRODUCTION:
        problems.append(
            f"正式環境（{env_name('environment')} 是 production 或沒設定）不能開假登入（{fake}）"
        )
    if settings.fake_login and settings.environment is Environment.TRIAL:
        required = {
            "access_team_domain": settings.access_team_domain,
            "access_audience": settings.access_audience,
            "admin_email": settings.admin_email,
            "site_origin": settings.site_origin,
        }
        problems.extend(
            f"缺少環境變數 {env_name(field)}（試用環境開假登入時一定要設定）"
            for field, value in required.items()
            if value is None
        )
        if settings.site_origin is not None and not settings.site_origin.startswith("https://"):
            problems.append(f"環境變數 {env_name('site_origin')} 在試用環境要是 https://")
    if settings.session_max_days < settings.session_idle_days:
        problems.append(
            f"環境變數 {env_name('session_max_days')} 不能小於 {env_name('session_idle_days')}"
        )
    return problems


def allowed_origins(settings: AuthSettings) -> frozenset[str]:
    """會改資料的請求可以帶的 `Origin`：有設 `site_origin` 就只有它；開發環境沒設時是本機的
    `localhost:8000`、`127.0.0.1:8000`；其他情況是空集合（拒絕所有會改資料的請求）。"""
    if settings.site_origin is not None:
        return frozenset({settings.site_origin})
    if settings.environment is Environment.DEVELOPMENT:
        return DEVELOPMENT_ORIGINS
    return frozenset()


def session_policy(settings: AuthSettings) -> SessionPolicy:
    """設定裡的 session 期限。

    Raises:
        ValueError: 最長期限比閒置期限短（`load_auth_settings` 已經先擋）。
    """
    return SessionPolicy(
        idle=timedelta(days=settings.session_idle_days),
        max_age=timedelta(days=settings.session_max_days),
    )


def fake_login_mode(settings: AuthSettings) -> FakeLoginMode | None:
    """假登入的模式；沒開假登入時是 None。

    Raises:
        ValueError: 正式環境開了假登入（`load_auth_settings` 已經先擋，這裡再擋一次）。
    """
    if not settings.fake_login:
        return None
    match settings.environment:
        case Environment.DEVELOPMENT:
            return FakeLoginMode.PICK_MEMBER
        case Environment.TRIAL:
            return FakeLoginMode.ACCESS_ADMIN
        case _:
            raise ValueError("正式環境不能開假登入")
