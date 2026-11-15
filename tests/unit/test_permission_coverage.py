"""換人測試與金絲雀測試的框架（Story 3.4、FR-7、AD-3、ADR-0047），不連資料庫。

- 根目錄的 `openapi.json` 和程式產生的一樣（CI 另外有一步重新輸出後 `git diff`）。
- `openapi.json` 的每個端點都在登記表（`tests/permissions/cases.py`）：有換人測試或寫了理由的豁免。
- app 上沒有躲在 `openapi.json` 外面的 API 路由；真正的 `main()` 組出來的 app 也一樣。
- **示範**：加一個只在這裡存在的端點，框架真的報「這個端點沒有換人測試」；登記之後就不報。
- 換人測試的判定（404 或空清單）與金絲雀檢查，對故意寫錯（會洩漏）的端點真的會失敗；對照（A 自己
  拿得到）擋住「路徑打錯也是 404」的假通過。
真的資料庫、真的兩位成員的版本在 `tests/integration/test_permissions.py`。
"""

import json
import signal
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from httpx2 import Response

import radar.entrypoints.api.main as api_main
from radar.adapters.fake.login import pick_member_identity
from radar.config import env_name
from radar.domain.auth import AuthenticatedMember
from radar.domain.auth_settings import AuthSettings, Environment
from radar.domain.errors import ErrorCode
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import (
    SESSION_COOKIE,
    AuthRoutes,
    build_auth_routes,
    install_auth_routes,
)
from radar.entrypoints.api.main import install_api_routes
from radar.entrypoints.api.openapi import build_schema_app, render_openapi
from radar.entrypoints.api.problems import ApiProblemError
from radar.entrypoints.bootstrap import Database
from tests.permissions.cases import REGISTRY
from tests.permissions.framework import (
    CANARY,
    OPENAPI_PATH,
    Endpoint,
    EndpointRegistry,
    Member,
    PermissionContext,
    assert_hidden_from_other_member,
    assert_visible_to_owner,
    coverage_problems,
    endpoints_in_openapi,
    response_texts,
    routes_missing_from_openapi,
    secrets_found,
)

ORIGIN = "https://testserver"
TEST_ONLY_PATH = "/api/v1/test-only-notes/{note_id}"
OWNER = Member(member_id=str(uuid.UUID(int=0xA)), token="a" * 43, email="owner@example.com")
OTHER = Member(member_id=str(uuid.UUID(int=0xB)), token="b" * 43, email="other@example.com")


def _committed_openapi() -> dict[str, object]:
    loaded: dict[str, object] = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    return loaded


# --- 真正的端點清單 ---------------------------------------------------------------------------


def test_committed_openapi_matches_the_app() -> None:
    # 和 CI 的「重新輸出後 git diff」同一件事；本機先擋，不必等 CI。
    assert OPENAPI_PATH.read_text(encoding="utf-8") == render_openapi()


def test_every_endpoint_in_openapi_has_a_cross_member_case_or_exemption() -> None:
    assert coverage_problems(_committed_openapi(), REGISTRY.cases) == []


def test_openapi_lists_the_current_endpoints() -> None:
    # 端點清單確實讀得出來（不是空的、也沒有把 parameters 之類的鍵當成方法）。
    assert endpoints_in_openapi(_committed_openapi()) == {
        Endpoint("GET", "/healthz"),
        Endpoint("GET", "/readyz"),
        Endpoint("GET", "/api/v1/me"),
        Endpoint("POST", "/api/v1/auth/logout"),
        Endpoint("GET", "/api/v1/auth/fake-login"),
        Endpoint("POST", "/api/v1/auth/fake-login"),
    }


def test_development_assembly_has_the_same_endpoints_as_openapi() -> None:
    # 正式組裝（開發環境＋假登入，端點最多的組合）和 openapi.json 的端點一樣：openapi.json 不是另外
    # 手寫的清單。組裝時不碰資料庫，資料庫物件只是佔位。
    app = create_app(readiness_check=lambda: True)
    settings = AuthSettings(environment=Environment.DEVELOPMENT, fake_login=True)
    install_api_routes(
        app,
        auth_routes=build_auth_routes(
            cast(Database, object()), settings, pick_member_identity, lambda: datetime.now(UTC)
        ),
    )

    assert endpoints_in_openapi(app.openapi()) == endpoints_in_openapi(_committed_openapi())


def test_no_api_route_is_hidden_from_openapi() -> None:
    assert routes_missing_from_openapi(build_schema_app()) == []


class _CapturedServer:
    """頂替 uvicorn.Server：記下 main() 組好的 app，不開連接埠。"""

    apps: ClassVar[list[Any]] = []

    def __init__(self, config: Any) -> None:
        self.should_exit = False
        _CapturedServer.apps.append(config.app)

    def run(self) -> None:
        return None


class _Probes:
    ping = staticmethod(lambda: True)
    schema_state = staticmethod(lambda: None)
    database = object()  # 只組裝、不送請求，不會碰到資料庫


def test_real_main_assembly_matches_openapi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 走真正的 main()（開發環境＋假登入，端點最多的組合；前端也掛上），只換掉資料庫與伺服器啟動。
    # main() 若另外裝了沒經 install_api_routes 的路由，這裡的端點清單就會和 openapi.json 不同。
    for name in AuthSettings.model_fields:
        monkeypatch.delenv(env_name(name), raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://radar_app@db:5432/radar")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(OPENAPI_PATH.parent / "config"))
    monkeypatch.setenv("RADAR_ENVIRONMENT", "development")
    monkeypatch.setenv("RADAR_FAKE_LOGIN", "true")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>shell</title>", encoding="utf-8")
    monkeypatch.setattr(api_main, "build_readiness_probes", lambda *_a, **_k: _Probes())
    monkeypatch.setattr(api_main, "WEB_DIST", dist)
    monkeypatch.setattr(uvicorn, "Server", _CapturedServer)
    _CapturedServer.apps.clear()
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        assert api_main.main() == 0
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)

    (app,) = _CapturedServer.apps
    assert endpoints_in_openapi(app.openapi()) == endpoints_in_openapi(_committed_openapi())
    assert routes_missing_from_openapi(app) == []


# --- 示範：新增端點沒有換人測試時，框架真的會報錯 ---------------------------------------------


def _schema_app_with_test_only_endpoint() -> FastAPI:
    app = build_schema_app()

    @app.get(TEST_ONLY_PATH)
    def test_only_note(note_id: str) -> dict[str, str]:
        return {"note_id": note_id}

    return app


def test_new_endpoint_without_a_case_is_reported() -> None:
    document = _schema_app_with_test_only_endpoint().openapi()

    assert coverage_problems(document, REGISTRY.cases) == [
        f"這個端點沒有換人測試：GET {TEST_ONLY_PATH}"
    ]


def test_new_endpoint_passes_once_it_is_registered() -> None:
    document = _schema_app_with_test_only_endpoint().openapi()
    extra = EndpointRegistry()

    def _control(ctx: PermissionContext) -> Response:
        return ctx.as_owner("GET", "/api/v1/test-only-notes/1")

    @extra.case("GET", TEST_ONLY_PATH, control=_control)
    def _note(ctx: PermissionContext) -> Response:
        return ctx.as_other("GET", "/api/v1/test-only-notes/1")

    assert coverage_problems(document, {**REGISTRY.cases, **extra.cases}) == []


def test_cross_member_case_without_control_is_reported() -> None:
    document = _schema_app_with_test_only_endpoint().openapi()
    extra = EndpointRegistry()

    @extra.case("GET", TEST_ONLY_PATH)
    def _note(ctx: PermissionContext) -> Response:
        return ctx.as_other("GET", "/api/v1/test-only-notes/1")

    assert coverage_problems(document, {**REGISTRY.cases, **extra.cases}) == [
        f"換人測試要有對照（成員 A 自己拿得到）：GET {TEST_ONLY_PATH}"
    ]


def test_registered_endpoint_missing_from_openapi_is_reported() -> None:
    extra = EndpointRegistry()

    @extra.case("DELETE", "/api/v1/gone", exempt="測試")
    def _gone(ctx: PermissionContext) -> Response:
        return ctx.as_other("DELETE", "/api/v1/gone")

    assert coverage_problems(_committed_openapi(), {**REGISTRY.cases, **extra.cases}) == [
        "登記了 openapi.json 沒有的端點：DELETE /api/v1/gone"
    ]


@pytest.mark.parametrize("reason", ["", "   "])
def test_blank_exemption_reason_is_reported(reason: str) -> None:
    registry = EndpointRegistry()
    for endpoint, case in REGISTRY.cases.items():
        registry.case(
            endpoint.method,
            endpoint.path,
            exempt=reason if endpoint.path == "/healthz" else case.exempt_reason,
        )(case.call)

    assert coverage_problems(_committed_openapi(), registry.cases) == [
        "豁免換人測試要寫理由：GET /healthz"
    ]


def test_registering_the_same_endpoint_twice_fails() -> None:
    registry = EndpointRegistry()
    registry.case("get", "/x", exempt="測試")(lambda ctx: ctx.as_other("GET", "/x"))

    with pytest.raises(ValueError, match="登記了兩次"):
        registry.case("GET", "/x", exempt="測試")(lambda ctx: ctx.as_other("GET", "/x"))


def test_route_hidden_from_openapi_is_reported() -> None:
    app = build_schema_app()

    @app.get("/api/v1/secret-notes", include_in_schema=False)
    def secret_notes() -> dict[str, str]:
        return {}

    assert routes_missing_from_openapi(app) == ["沒有列進 openapi.json：GET /api/v1/secret-notes"]


# --- 換人測試的判定與金絲雀檢查，對會洩漏的端點真的會失敗 -------------------------------------

NOTE_ID = "note-1"
NOTES = {NOTE_ID: (OWNER.member_id, f"成員 A 的私人筆記 {CANARY}")}


def _member(member: Member) -> AuthenticatedMember:
    return AuthenticatedMember(
        session_id=uuid.UUID(int=1),
        member_id=uuid.UUID(member.member_id),
        display_name="測試",
        is_admin=False,
        last_seen_at=datetime(2026, 11, 15, tzinfo=UTC),
    )


def _authenticate(token: str | None) -> AuthenticatedMember | None:
    return {OWNER.token: _member(OWNER), OTHER.token: _member(OTHER)}.get(token or "")


def _notes_client(*, leaky: bool) -> TestClient:
    """一個只在這裡存在的私人筆記端點；`leaky` 時故意忘了檢查擁有者。"""
    app = create_app(readiness_check=lambda: True)
    install_auth_routes(
        app,
        AuthRoutes(
            authenticate=_authenticate,
            log_out=lambda _token: None,
            allowed_origins=frozenset({ORIGIN}),
            cookie_max_age=60,
        ),
    )
    router = APIRouter()

    @router.get("/api/v1/test-only-notes/{note_id}")
    def note(note_id: str, request: Request) -> dict[str, str]:
        caller = _authenticate(request.cookies.get(SESSION_COOKIE))
        if caller is None:
            raise ApiProblemError(ErrorCode.NOT_AUTHENTICATED)
        found = NOTES.get(note_id)
        if found is None or (not leaky and found[0] != str(caller.member_id)):
            raise ApiProblemError(ErrorCode.NOT_FOUND)
        return {"note_id": note_id, "text": found[1]}

    @router.get("/api/v1/test-only-notes")
    def notes(request: Request) -> dict[str, list[str]]:
        caller = _authenticate(request.cookies.get(SESSION_COOKIE))
        assert caller is not None
        return {
            "items": [
                text for owner, text in NOTES.values() if leaky or owner == str(caller.member_id)
            ]
        }

    app.include_router(router)
    return TestClient(app, base_url=ORIGIN)


def _context(client: TestClient) -> PermissionContext:
    return PermissionContext(client=client, owner=OWNER, other=OTHER, origin=ORIGIN)


def test_cross_member_check_passes_for_404_and_empty_list() -> None:
    ctx = _context(_notes_client(leaky=False))

    assert_hidden_from_other_member(ctx.as_other("GET", f"/api/v1/test-only-notes/{NOTE_ID}"))
    assert_hidden_from_other_member(ctx.as_other("GET", "/api/v1/test-only-notes"))


@pytest.mark.parametrize("path", [f"/api/v1/test-only-notes/{NOTE_ID}", "/api/v1/test-only-notes"])
def test_cross_member_check_fails_for_a_leaky_endpoint(path: str) -> None:
    ctx = _context(_notes_client(leaky=True))

    with pytest.raises(AssertionError, match="成員 B"):
        assert_hidden_from_other_member(ctx.as_other("GET", path))


def test_owner_control_passes_for_the_owner() -> None:
    ctx = _context(_notes_client(leaky=False))

    assert_visible_to_owner(ctx.as_owner("GET", f"/api/v1/test-only-notes/{NOTE_ID}"))
    assert_visible_to_owner(ctx.as_owner("GET", "/api/v1/test-only-notes"))


def test_owner_control_catches_a_wrong_path() -> None:
    # 路徑打錯：成員 B 拿到 404，換人測試會通過；對照讓 A 也打同一個錯路徑，就會失敗。
    ctx = _context(_notes_client(leaky=False))
    wrong = f"/api/v1/test-only-note/{NOTE_ID}"

    assert_hidden_from_other_member(ctx.as_other("GET", wrong))
    with pytest.raises(AssertionError, match="對照失敗"):
        assert_visible_to_owner(ctx.as_owner("GET", wrong))


def test_owner_control_catches_an_empty_owner_list() -> None:
    with pytest.raises(AssertionError, match="對照失敗"):
        assert_visible_to_owner(Response(200, json={"items": []}))


def test_cross_member_check_rejects_a_leaked_total() -> None:
    with pytest.raises(AssertionError, match="筆數"):
        assert_hidden_from_other_member(Response(200, json={"items": [], "total": 1}))
    assert_hidden_from_other_member(Response(200, json={"items": [], "total": 0}))


def test_cross_member_check_rejects_403_and_401() -> None:
    client = _notes_client(leaky=False)
    forbidden = client.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example"})
    unauthenticated = client.get(f"/api/v1/test-only-notes/{NOTE_ID}")

    assert (forbidden.status_code, unauthenticated.status_code) == (403, 401)
    for response in (forbidden, unauthenticated):
        with pytest.raises(AssertionError):
            assert_hidden_from_other_member(response)


def test_canary_check_finds_the_canary_in_a_leaky_response() -> None:
    leaky = _context(_notes_client(leaky=True)).as_other(
        "GET", f"/api/v1/test-only-notes/{NOTE_ID}"
    )
    safe = _context(_notes_client(leaky=False)).as_other(
        "GET", f"/api/v1/test-only-notes/{NOTE_ID}"
    )

    assert secrets_found(response_texts(leaky), [CANARY, OWNER.token]) == [CANARY]
    assert secrets_found(response_texts(safe), [CANARY, OWNER.token]) == []


def test_canary_check_looks_at_headers_too() -> None:
    response = _context(_notes_client(leaky=False)).as_other("GET", "/healthz")
    response.headers["X-Debug"] = OWNER.token

    assert secrets_found(response_texts(response), [CANARY, OWNER.token]) == [OWNER.token]
