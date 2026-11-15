"""api 的登入路由（Story 3.3、ADR-0008、ADR-0046）：用假的函式組 `AuthRoutes`，不連資料庫。

cookie 的屬性、Origin 的反例、401、假登入沒開時 404、掛上前端後仍回 JSON、頁面殼已掛時裝路由
丟錯、回應不帶 email。真資料庫的流程在 `tests/integration/test_sessions.py`。
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from radar.domain.auth import (
    AdminMemberMissingError,
    AuthenticatedMember,
    FakeLoginRequest,
    LoginRejectedError,
    LoginRejection,
    MemberSummary,
    NewSession,
)
from radar.domain.auth_settings import FakeLoginMode
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import (
    ACCESS_ASSERTION_HEADER,
    SESSION_COOKIE,
    AuthRoutes,
    FakeLoginRoutes,
    install_auth_routes,
)
from radar.entrypoints.api.web import mount_web, web_mounted
from radar.entrypoints.logging_config import configure_logging

ORIGIN = "https://testserver"
MEMBER_ID = uuid.UUID("00000000-0000-7000-8000-00000000000a")
SESSION_ID = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
GOOD_TOKEN = "t" * 43
NEW_TOKEN = "n" * 43
SHELL = "<!doctype html><title>shell</title>"
MAX_AGE = 30 * 24 * 3600
MEMBER = AuthenticatedMember(
    session_id=SESSION_ID,
    member_id=MEMBER_ID,
    display_name="測試甲",
    is_admin=False,
    last_seen_at=datetime(2026, 11, 15, tzinfo=UTC),
)


@dataclass
class FakeAuth:
    """記下每一次呼叫的假函式。"""

    login_error: Exception | None = None
    tokens_seen: list[str | None] = field(default_factory=list)
    logged_out: list[str] = field(default_factory=list)
    login_requests: list[FakeLoginRequest] = field(default_factory=list)

    def authenticate(self, token: str | None) -> AuthenticatedMember | None:
        self.tokens_seen.append(token)
        return MEMBER if token == GOOD_TOKEN else None

    def log_out(self, token: str) -> None:
        self.logged_out.append(token)

    def list_members(self) -> Sequence[MemberSummary]:
        return [MemberSummary(member_id=MEMBER_ID, display_name="測試甲")]

    def log_in(self, request: FakeLoginRequest) -> NewSession:
        self.login_requests.append(request)
        if self.login_error is not None:
            raise self.login_error
        return NewSession(session_id=SESSION_ID, member_id=MEMBER_ID, token=NEW_TOKEN)

    def routes(self, *, fake_login: bool = True) -> AuthRoutes:
        return AuthRoutes(
            authenticate=self.authenticate,
            log_out=self.log_out,
            allowed_origins=frozenset({ORIGIN}),
            cookie_max_age=MAX_AGE,
            fake_login=(
                FakeLoginRoutes(
                    mode=FakeLoginMode.PICK_MEMBER,
                    list_members=self.list_members,
                    log_in=self.log_in,
                )
                if fake_login
                else None
            ),
        )


@pytest.fixture
def web_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(SHELL, encoding="utf-8")
    return dist


def _client(auth: FakeAuth, *, fake_login: bool = True, web_dist: Path | None = None) -> TestClient:
    app = create_app(readiness_check=lambda: True)
    install_auth_routes(app, auth.routes(fake_login=fake_login))
    if web_dist is not None:
        mount_web(app, web_dist)
    return TestClient(app, base_url=ORIGIN)


def _cookie_header(value: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE}={value}"}


def _session_cookie(set_cookie: str) -> dict[str, str]:
    """`Set-Cookie` 裡的屬性（名稱小寫）；值為空字串的是旗標（HttpOnly、Secure）。"""
    parts = [part.strip() for part in set_cookie.split(";")]
    name, _, value = parts[0].partition("=")
    attributes = {"__name__": name, "__value__": value}
    for part in parts[1:]:
        key, _, attribute = part.partition("=")
        attributes[key.lower()] = attribute
    return attributes


# --- 假登入 -----------------------------------------------------------------------------------


def test_fake_login_sets_host_cookie_with_every_attribute() -> None:
    auth = FakeAuth()
    client = _client(auth)

    response = client.post(
        "/api/v1/auth/fake-login",
        json={"member_id": str(MEMBER_ID), "next": "/guide"},
        headers={"Origin": ORIGIN, ACCESS_ASSERTION_HEADER: "jwt-value"},
    )

    assert response.status_code == 200
    assert response.json() == {"next": "/guide"}
    assert response.headers["cache-control"] == "no-store"
    cookie = _session_cookie(response.headers["set-cookie"])
    assert cookie["__name__"] == "__Host-radar_session"
    assert cookie["__value__"] == NEW_TOKEN
    assert cookie["path"] == "/"
    assert cookie["samesite"].lower() == "lax"
    assert "secure" in cookie
    assert "httponly" in cookie
    assert cookie["max-age"] == str(MAX_AGE)
    assert "domain" not in cookie  # __Host- 前綴不能有 Domain
    assert auth.login_requests == [
        FakeLoginRequest(member_id=MEMBER_ID, access_assertion="jwt-value")
    ]


@pytest.mark.parametrize(
    "next_path",
    ["//evil.example.com", "https://evil.example.com", "/\\evil", "/a b", "", "/" + "a" * 3000],
)
def test_fake_login_sends_unsafe_next_back_to_root(next_path: str) -> None:
    client = _client(FakeAuth())

    response = client.post(
        "/api/v1/auth/fake-login",
        json={"member_id": str(MEMBER_ID), "next": next_path},
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 200
    assert response.json() == {"next": "/"}


def test_fake_login_without_next_goes_to_root() -> None:
    response = _client(FakeAuth()).post(
        "/api/v1/auth/fake-login", json={"member_id": str(MEMBER_ID)}, headers={"Origin": ORIGIN}
    )

    assert response.json() == {"next": "/"}


@pytest.mark.parametrize(
    "error",
    [
        LoginRejectedError(LoginRejection.NO_MEMBER_CHOSEN),
        LoginRejectedError(LoginRejection.MEMBER_NOT_ALLOWED),
        LoginRejectedError(LoginRejection.ASSERTION_INVALID),
    ],
)
def test_rejected_fake_login_is_403_without_cookie(error: Exception) -> None:
    client = _client(FakeAuth(login_error=error))

    response = client.post("/api/v1/auth/fake-login", json={}, headers={"Origin": ORIGIN})

    assert response.status_code == 403
    assert response.json() == {"detail": "login_rejected"}
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "assertions", [["valid-jwt", "other-jwt"], ["other-jwt", "valid-jwt"], ["same", "same"]]
)
def test_duplicate_access_assertion_headers_are_403_without_logging_in(
    assertions: list[str],
) -> None:
    auth = FakeAuth()
    headers = [("Origin", ORIGIN), *((ACCESS_ASSERTION_HEADER, value) for value in assertions)]

    response = _client(auth).post("/api/v1/auth/fake-login", json={}, headers=headers)

    assert response.status_code == 403
    assert response.json() == {"detail": "login_rejected"}
    assert auth.login_requests == []
    assert "set-cookie" not in response.headers


def test_missing_admin_member_is_403_and_logs_the_two_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    client = _client(FakeAuth(login_error=AdminMemberMissingError()))

    response = client.post("/api/v1/auth/fake-login", json={}, headers={"Origin": ORIGIN})

    assert response.status_code == 403
    assert response.json() == {"detail": "admin_member_missing"}
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    (record,) = [r for r in records if r.get("level") == "error"]
    assert record["commands"] == [
        "radar members allow <管理者 email> --name <名稱>",
        "radar members set-admin <管理者 email>",
    ]


def test_fake_login_options_list_members_without_email() -> None:
    response = _client(FakeAuth()).get("/api/v1/auth/fake-login")

    assert response.status_code == 200
    assert response.json() == {
        "mode": "pick_member",
        "members": [{"member_id": str(MEMBER_ID), "display_name": "測試甲"}],
    }


# --- Origin -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.example.com"},
        {"Origin": "null"},
        {"Origin": "https://testserver.evil.example.com"},
        {"Origin": "http://testserver"},
        {"Origin": ORIGIN + "/"},
    ],
)
def test_fake_login_with_wrong_or_missing_origin_is_403_and_creates_nothing(
    headers: dict[str, str],
) -> None:
    auth = FakeAuth()

    response = _client(auth).post(
        "/api/v1/auth/fake-login", json={"member_id": str(MEMBER_ID)}, headers=headers
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "origin_rejected"}
    assert auth.login_requests == []
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "origins",
    [
        [ORIGIN, "https://evil.example.com"],
        ["https://evil.example.com", ORIGIN],
        [ORIGIN, ORIGIN],
    ],
)
@pytest.mark.parametrize("path", ["/api/v1/auth/fake-login", "/api/v1/auth/logout"])
def test_duplicate_origin_headers_are_403(origins: list[str], path: str) -> None:
    auth = FakeAuth()
    headers = [("Origin", origin) for origin in origins]
    headers.append(("Cookie", f"{SESSION_COOKIE}={GOOD_TOKEN}"))

    response = _client(auth).post(path, json={"member_id": str(MEMBER_ID)}, headers=headers)

    assert response.status_code == 403
    assert response.json() == {"detail": "origin_rejected"}
    assert auth.login_requests == []
    assert auth.logged_out == []
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example.com"}])
def test_logout_with_wrong_or_missing_origin_keeps_the_session(headers: dict[str, str]) -> None:
    auth = FakeAuth()

    response = _client(auth).post(
        "/api/v1/auth/logout", headers={**headers, **_cookie_header(GOOD_TOKEN)}
    )

    assert response.status_code == 403
    assert auth.logged_out == []
    assert auth.tokens_seen == []
    assert "set-cookie" not in response.headers


def test_get_requests_do_not_need_origin() -> None:
    response = _client(FakeAuth()).get("/api/v1/me", headers=_cookie_header(GOOD_TOKEN))

    assert response.status_code == 200


# --- 我是誰 -----------------------------------------------------------------------------------


def test_me_returns_member_id_and_name_only() -> None:
    response = _client(FakeAuth()).get("/api/v1/me", headers=_cookie_header(GOOD_TOKEN))

    assert response.status_code == 200
    assert response.json() == {"member_id": str(MEMBER_ID), "display_name": "測試甲"}
    assert response.headers["cache-control"] == "no-store"
    assert "@" not in response.text


@pytest.mark.parametrize("cookie", [None, "", "wrong", "x" * 43])
def test_me_without_valid_session_is_401(cookie: str | None) -> None:
    auth = FakeAuth()
    headers = {} if cookie is None else _cookie_header(cookie)

    response = _client(auth).get("/api/v1/me", headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "not_authenticated"}
    assert response.headers["content-type"].startswith("application/json")


# --- 登出 -------------------------------------------------------------------------------------


def test_logout_ends_the_session_and_clears_the_cookie() -> None:
    auth = FakeAuth()

    response = _client(auth).post(
        "/api/v1/auth/logout", headers={"Origin": ORIGIN, **_cookie_header(GOOD_TOKEN)}
    )

    assert response.status_code == 204
    assert response.content == b""
    assert auth.logged_out == [GOOD_TOKEN]
    cookie = _session_cookie(response.headers["set-cookie"])
    assert cookie["__name__"] == SESSION_COOKIE
    assert cookie["max-age"] == "0"
    assert cookie["path"] == "/"
    assert "secure" in cookie
    assert "httponly" in cookie


def test_logout_does_not_require_a_valid_session() -> None:
    # 過期或被移出名單的 session：authenticate 認不出來，但登出仍要把 token 交給 log_out 刪掉。
    auth = FakeAuth()
    expired = "x" * 43

    response = _client(auth).post(
        "/api/v1/auth/logout", headers={"Origin": ORIGIN, **_cookie_header(expired)}
    )

    assert response.status_code == 204
    assert auth.logged_out == [expired]
    assert auth.tokens_seen == []
    assert _session_cookie(response.headers["set-cookie"])["max-age"] == "0"


def test_logout_without_session_is_still_204() -> None:
    auth = FakeAuth()

    response = _client(auth).post("/api/v1/auth/logout", headers={"Origin": ORIGIN})

    assert response.status_code == 204
    assert auth.logged_out == []
    parsed = SimpleCookie()
    parsed.load(response.headers["set-cookie"])
    assert parsed[SESSION_COOKIE]["max-age"] == "0"


# --- 假登入沒開 -------------------------------------------------------------------------------


@pytest.mark.parametrize("with_web", [False, True])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_fake_login_paths_are_404_json_when_disabled(
    web_dist: Path, with_web: bool, method: str
) -> None:
    client = _client(FakeAuth(), fake_login=False, web_dist=web_dist if with_web else None)

    response = client.request(
        method, "/api/v1/auth/fake-login", json={}, headers={"Origin": ORIGIN}
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert SHELL not in response.text


# --- 掛上前端之後 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "headers", "status"),
    [
        ("GET", "/api/v1/me", {}, 401),
        ("GET", "/api/v1/me", _cookie_header(GOOD_TOKEN), 200),
        ("GET", "/api/v1/auth/fake-login", {}, 200),
        ("POST", "/api/v1/auth/fake-login", {"Origin": ORIGIN}, 200),
        ("POST", "/api/v1/auth/fake-login", {}, 403),
        ("POST", "/api/v1/auth/logout", {}, 403),
    ],
)
def test_new_api_paths_still_return_json_after_web_is_mounted(
    web_dist: Path, method: str, path: str, headers: dict[str, str], status: int
) -> None:
    client = _client(FakeAuth(), web_dist=web_dist)

    response = client.request(method, path, json={"member_id": str(MEMBER_ID)}, headers=headers)

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    assert SHELL not in response.text
    # 對照：一般頁面仍是頁面殼。
    assert client.get("/guide").text == SHELL


def test_logout_after_web_is_mounted_is_not_the_shell(web_dist: Path) -> None:
    client = _client(FakeAuth(), web_dist=web_dist)

    response = client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})

    assert response.status_code == 204
    assert SHELL not in response.text


def test_installing_routes_after_web_is_mounted_raises(web_dist: Path) -> None:
    app = create_app(readiness_check=lambda: True, web_dist=web_dist)
    assert web_mounted(app)

    with pytest.raises(RuntimeError, match="mount_web 之前"):
        install_auth_routes(app, FakeAuth().routes())


def test_web_mounted_is_false_before_mount(web_dist: Path) -> None:
    app: FastAPI = create_app(readiness_check=lambda: True)

    assert not web_mounted(app)
    mount_web(app, web_dist)
    assert web_mounted(app)
