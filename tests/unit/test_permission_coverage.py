"""換人測試與金絲雀測試的框架（Story 3.4、FR-7、AD-3、ADR-0047），不連資料庫。

- 根目錄的 `openapi.json` 和程式產生的一樣（CI 另外有一步重新輸出後 `git diff`）。
- `openapi.json` 的每個端點都在登記表（`tests/permissions/cases.py`）：有換人測試或寫了理由的豁免。
- app 上沒有躲在 `openapi.json` 外面的 API 路由；真正的 `main()` 組出來的 app 也一樣。
- **示範**：加一個只在這裡存在的端點，框架真的報「這個端點沒有換人測試」；登記之後就不報。
- 換人測試的判定（404 或空清單）與金絲雀檢查，對故意寫錯（會洩漏）的端點真的會失敗；對照（A 自己
  拿得到）擋住「路徑打錯也是 404」的假通過。
- 框架送的是同一份請求、只換身分，並核對實際處理請求的路由：只有 B 被送到別的路由也會失敗。
- 框架自己的頁面（頁面殼、文件頁、健康檢查的 HEAD）以實際處理函式辨認：換了處理函式就報錯。
- 「看不到」的 404 與空清單多帶一個欄位就失敗；金絲雀走真的日誌設定，寫進日誌就失敗。
真的資料庫、真的兩位成員的版本在 `tests/integration/test_permissions.py`。
"""

import json
import signal
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
import structlog
import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import BaseModel
from starlette.requests import Request as StarletteRequest
from starlette.routing import Route

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
from radar.entrypoints.api.problems import PROBLEM_MEDIA_TYPE, ApiProblemError
from radar.entrypoints.api.web import mount_web
from radar.entrypoints.bootstrap import Database
from radar.entrypoints.logging_config import configure_logging
from tests.permissions.cases import REGISTRY
from tests.permissions.framework import (
    CANARY,
    NOT_FOUND_PROBLEM,
    OPENAPI_PATH,
    Endpoint,
    EndpointCase,
    EndpointRegistry,
    EndpointRequest,
    Member,
    PermissionContext,
    assert_hidden_from_other_member,
    assert_visible_to_owner,
    coverage_problems,
    endpoint_url,
    endpoints_in_openapi,
    list_fields_of,
    response_texts,
    routes_missing_from_openapi,
    run_canary_case,
    run_cross_member_case,
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


def _note_request(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest(path_params={"note_id": "1"})


def test_new_endpoint_passes_once_it_is_registered() -> None:
    document = _schema_app_with_test_only_endpoint().openapi()
    extra = EndpointRegistry()
    extra.case("GET", TEST_ONLY_PATH)(_note_request)

    assert coverage_problems(document, {**REGISTRY.cases, **extra.cases}) == []


def test_exempt_case_without_expected_status_is_reported() -> None:
    document = _schema_app_with_test_only_endpoint().openapi()
    extra = EndpointRegistry()
    extra.case("GET", TEST_ONLY_PATH, exempt="測試")(_note_request)

    assert coverage_problems(document, {**REGISTRY.cases, **extra.cases}) == [
        f"豁免的端點要寫預期狀態碼：GET {TEST_ONLY_PATH}"
    ]


def test_cross_member_case_with_expected_status_is_reported() -> None:
    # 沒豁免的端點預期一律是「看不到」；寫了狀態碼代表登記的人以為它會回別的東西。
    document = _schema_app_with_test_only_endpoint().openapi()
    extra = EndpointRegistry()
    extra.case("GET", TEST_ONLY_PATH, status=200)(_note_request)

    assert coverage_problems(document, {**REGISTRY.cases, **extra.cases}) == [
        f"沒豁免的端點不寫預期狀態碼（預期一律是 404 或空清單）：GET {TEST_ONLY_PATH}"
    ]


def test_registered_endpoint_missing_from_openapi_is_reported() -> None:
    extra = EndpointRegistry()

    @extra.case("DELETE", "/api/v1/gone", exempt="測試", status=204)
    def _gone(_ctx: PermissionContext) -> EndpointRequest:
        return EndpointRequest()

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
            status=case.expected_status,
        )(case.call)

    assert coverage_problems(_committed_openapi(), registry.cases) == [
        "豁免換人測試要寫理由：GET /healthz"
    ]


def test_registering_the_same_endpoint_twice_fails() -> None:
    registry = EndpointRegistry()
    registry.case("get", "/x", exempt="測試", status=200)(lambda _ctx: EndpointRequest())

    with pytest.raises(ValueError, match="登記了兩次"):
        registry.case("GET", "/x", exempt="測試", status=200)(lambda _ctx: EndpointRequest())


def test_route_hidden_from_openapi_is_reported() -> None:
    app = build_schema_app()

    @app.get("/api/v1/secret-notes", include_in_schema=False)
    def secret_notes() -> dict[str, str]:
        return {}

    assert routes_missing_from_openapi(app) == ["沒有列進 openapi.json：GET /api/v1/secret-notes"]


def _web_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>shell</title>", encoding="utf-8")
    return dist


def _private_catch_all(_request: StarletteRequest) -> JSONResponse:
    return JSONResponse({"private_text": "成員 A 的資料"})


def test_web_shell_is_allowed(tmp_path: Path) -> None:
    app = build_schema_app()
    mount_web(app, _web_dist(tmp_path))

    assert routes_missing_from_openapi(app) == []


def test_catch_all_route_before_the_web_shell_is_reported(tmp_path: Path) -> None:
    # 路徑格式和頁面殼一樣（/{path:path}），處理函式卻不是頁面殼：不能因為長得像就放行。
    app = build_schema_app()
    app.router.routes.append(Route("/{path:path}", _private_catch_all, include_in_schema=False))
    mount_web(app, _web_dist(tmp_path))

    assert routes_missing_from_openapi(app) == [
        "不是一般 API 路由、也不是框架自己的頁面（處理函式或方法對不上）：/{path}"
    ]


def test_route_on_a_docs_path_with_another_handler_is_reported() -> None:
    app = build_schema_app()
    app.router.routes.append(Route("/redoc", _private_catch_all, include_in_schema=False))

    assert routes_missing_from_openapi(app) == [
        "不是一般 API 路由、也不是框架自己的頁面（處理函式或方法對不上）：/redoc"
    ]


def test_docs_route_with_other_methods_is_reported() -> None:
    # 處理函式是 FastAPI 的文件頁，方法卻多了 POST：方法也要對得上。
    app = build_schema_app()
    (docs,) = [route for route in app.router.routes if getattr(route, "path", None) == "/docs"]
    assert isinstance(docs, Route)
    app.router.routes.append(
        Route("/docs", docs.endpoint, methods=["GET", "POST"], include_in_schema=False)
    )

    assert routes_missing_from_openapi(app) == [
        "不是一般 API 路由、也不是框架自己的頁面（處理函式或方法對不上）：/docs"
    ]


def test_web_shell_mounted_twice_is_reported(tmp_path: Path) -> None:
    app = build_schema_app()
    dist = _web_dist(tmp_path)
    mount_web(app, dist)
    mount_web(app, dist)

    assert routes_missing_from_openapi(app) == ["框架自己的頁面裝了不只一次：前端的頁面殼"]


def test_health_head_with_another_handler_is_reported() -> None:
    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    def private_head() -> dict[str, str]:
        return {"private_text": "成員 A 的資料"}

    app.add_api_route("/healthz", private_head, methods=["HEAD"], include_in_schema=False)

    assert routes_missing_from_openapi(app) == [
        "健康檢查的 HEAD 要和 GET 用同一個處理函式：HEAD /healthz"
    ]


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


class _NoteList(BaseModel):
    """清單的回應模型（Conventions「API」：`items`＋`total`）。"""

    items: list[str]
    total: int


def _notes_app(*, leaky: bool = False, misroute_other: bool = False) -> FastAPI:
    """一個只在這裡存在的私人筆記端點。

    - `leaky`：故意忘了檢查擁有者。
    - `misroute_other`：成員 B 的請求在路由之前被改到一個打錯的路徑（`/test-only-note/`，
      少一個 s），成員 A 的照常；模擬「A 打對、只有 B 打錯」。
    """
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
    if misroute_other:

        @app.middleware("http")
        async def misroute(request: Request, call_next: Any) -> Any:
            if OTHER.token in request.headers.get("cookie", ""):
                request.scope["path"] = request.scope["path"].replace("-notes/", "-note/")
            return await call_next(request)

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
    def notes(request: Request) -> _NoteList:
        caller = _authenticate(request.cookies.get(SESSION_COOKIE))
        assert caller is not None
        items = [text for owner, text in NOTES.values() if leaky or owner == str(caller.member_id)]
        return _NoteList(items=items, total=len(items))

    app.include_router(router)
    return app


def _notes_client(*, leaky: bool) -> TestClient:
    return TestClient(_notes_app(leaky=leaky), base_url=ORIGIN)


def _context(client: TestClient) -> PermissionContext:
    """原本的檢查函式用：只拿 client 的 app（請求由 as_other／as_owner 送）。"""
    return _app_context(cast(FastAPI, client.app))


def _app_context(app: FastAPI) -> PermissionContext:
    return PermissionContext(app=app, owner=OWNER, other=OTHER, origin=ORIGIN, base_url=ORIGIN)


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


# --- 框架送同一份請求、只換身分，並核對實際處理請求的路由 -------------------------------------


def _note_case(note_id: str = NOTE_ID) -> EndpointCase:
    registry = EndpointRegistry()

    @registry.case("GET", TEST_ONLY_PATH)
    def _note(_ctx: PermissionContext) -> EndpointRequest:
        return EndpointRequest(path_params={"note_id": note_id})

    return registry.cases[Endpoint("GET", TEST_ONLY_PATH)]


def _notes_list_case() -> EndpointCase:
    registry = EndpointRegistry()

    @registry.case("GET", "/api/v1/test-only-notes")
    def _notes(_ctx: PermissionContext) -> EndpointRequest:
        return EndpointRequest()

    return registry.cases[Endpoint("GET", "/api/v1/test-only-notes")]


def test_cross_member_case_passes_when_the_endpoint_checks_the_owner() -> None:
    ctx = _app_context(_notes_app())

    run_cross_member_case(_note_case(), ctx)
    run_cross_member_case(_notes_list_case(), ctx)


@pytest.mark.parametrize("case", [_note_case(), _notes_list_case()], ids=["detail", "list"])
def test_cross_member_case_fails_for_a_leaky_endpoint(case: EndpointCase) -> None:
    with pytest.raises(AssertionError, match="成員 B"):
        run_cross_member_case(case, _app_context(_notes_app(leaky=True)))


def test_cross_member_case_fails_when_only_the_other_member_misses_the_endpoint() -> None:
    # Codex 的反例：B 打錯路徑得到 404（本文還是正確的 not_found），A 打對得到 200。只看兩個回應，
    # 判定與對照都會通過；框架核對實際處理 B 請求的路由，所以失敗。
    app = _notes_app(misroute_other=True)
    ctx = _app_context(app)
    misrouted = ctx.as_other("GET", f"/api/v1/test-only-notes/{NOTE_ID}")
    assert misrouted.status_code == 404
    assert_hidden_from_other_member(misrouted)
    assert_visible_to_owner(ctx.as_owner("GET", f"/api/v1/test-only-notes/{NOTE_ID}"))

    with pytest.raises(AssertionError, match=r"成員 B 的請求沒有打到登記的端點"):
        run_cross_member_case(_note_case(), ctx)


def test_cross_member_case_fails_when_both_members_use_a_missing_record() -> None:
    # 兩人送的是同一份請求、都打到登記的端點，但資料不存在：B 的 404 不能當證據，對照會失敗。
    with pytest.raises(AssertionError, match="對照失敗"):
        run_cross_member_case(_note_case("no-such-note"), _app_context(_notes_app()))


def test_cross_member_case_fails_for_a_registered_path_that_no_route_serves() -> None:
    # 登記的路徑樣板打錯（少一個 s）：兩人都打不到任何路由。
    registry = EndpointRegistry()

    @registry.case("GET", "/api/v1/test-only-note/{note_id}")
    def _note(_ctx: PermissionContext) -> EndpointRequest:
        return EndpointRequest(path_params={"note_id": NOTE_ID})

    (case,) = registry.cases.values()
    with pytest.raises(AssertionError, match="沒有打到登記的端點"):
        run_cross_member_case(case, _app_context(_notes_app()))


def test_send_reports_the_route_that_really_handled_the_request() -> None:
    ctx = _app_context(_notes_app())
    note = Endpoint("GET", TEST_ONLY_PATH)

    sent = ctx.send(ctx.owner, note, EndpointRequest(path_params={"note_id": NOTE_ID}))
    wrong_method = ctx.send(
        ctx.owner, Endpoint("POST", TEST_ONLY_PATH), EndpointRequest(path_params={"note_id": "x"})
    )

    assert sent.route is not None
    assert (sent.route.path, sent.route.methods) == (TEST_ONLY_PATH, frozenset({"GET"}))
    # 只對上路徑（405）時，路由是 GET 的那一條：方法對不上，不算打到 POST。
    assert wrong_method.response.status_code == 405
    assert wrong_method.route is not None
    assert "POST" not in wrong_method.route.methods


def test_endpoint_url_fills_and_encodes_path_params() -> None:
    endpoint = Endpoint("GET", TEST_ONLY_PATH)

    assert endpoint_url(endpoint, {"note_id": "a/b c"}) == "/api/v1/test-only-notes/a%2Fb%20c"
    with pytest.raises(ValueError, match="路徑參數"):
        endpoint_url(endpoint, {})
    with pytest.raises(ValueError, match="路徑參數"):
        endpoint_url(endpoint, {"note_id": "1", "extra": "2"})


# --- 「看不到」的 404 與空清單不能多帶欄位 ---------------------------------------------------


def _problem_404(body: dict[str, object], media_type: str = PROBLEM_MEDIA_TYPE) -> Response:
    return Response(
        404, content=json.dumps(body).encode("utf-8"), headers={"content-type": media_type}
    )


def test_exact_not_found_problem_passes() -> None:
    assert_hidden_from_other_member(_problem_404(dict(NOT_FOUND_PROBLEM)))


def test_not_found_with_an_extra_private_field_fails() -> None:
    body = {**NOT_FOUND_PROBLEM, "private_text": "成員 A 的資料"}

    with pytest.raises(AssertionError, match="四個欄位"):
        assert_hidden_from_other_member(_problem_404(body))


def test_not_found_with_only_the_code_fails() -> None:
    with pytest.raises(AssertionError, match="四個欄位"):
        assert_hidden_from_other_member(_problem_404({"code": "not_found"}))


def test_not_found_that_is_not_problem_json_fails() -> None:
    with pytest.raises(AssertionError, match="problem"):
        assert_hidden_from_other_member(_problem_404(dict(NOT_FOUND_PROBLEM), "application/json"))


def test_empty_list_with_an_extra_private_field_fails() -> None:
    response = Response(200, json={"items": [], "total": 0, "private_text": "成員 A 的資料"})

    with pytest.raises(AssertionError, match="回應模型以外的欄位"):
        assert_hidden_from_other_member(response)


def test_empty_list_fields_come_from_the_response_model() -> None:
    # 回應模型只宣告 items 與 total：next_cursor 就算是 null 也不能出現；不是清單的端點只能 404。
    document = _notes_app().openapi()
    list_fields = list_fields_of(document, Endpoint("GET", "/api/v1/test-only-notes"))
    detail_fields = list_fields_of(document, Endpoint("GET", TEST_ONLY_PATH))

    assert list_fields == {"items", "total"}
    assert detail_fields == frozenset()
    assert_hidden_from_other_member(
        Response(200, json={"items": [], "total": 0}), list_fields=list_fields
    )
    with pytest.raises(AssertionError, match="回應模型以外的欄位"):
        assert_hidden_from_other_member(
            Response(200, json={"items": [], "next_cursor": None}), list_fields=list_fields
        )
    with pytest.raises(AssertionError, match="不是清單"):
        assert_hidden_from_other_member(
            Response(200, json={"items": []}), list_fields=detail_fields
        )


def test_empty_list_with_a_next_cursor_fails() -> None:
    with pytest.raises(AssertionError, match="下一頁"):
        assert_hidden_from_other_member(Response(200, json={"items": [], "next_cursor": "abc"}))


# --- 金絲雀：預期狀態碼、只看目標請求的日誌、走真的日誌設定 -----------------------------------

DIARY_PATH = "/api/v1/test-only-diary"
diary_log = structlog.get_logger("tests.test_only_diary")


def _diary_app(*, log_canary: bool) -> FastAPI:
    """一個只在這裡存在、豁免換人測試的端點；`log_canary` 時故意把成員 A 的金絲雀寫進日誌。"""
    app = _notes_app()

    @app.get(DIARY_PATH)
    def diary() -> dict[str, str]:
        if log_canary:
            diary_log.info("測試用：不小心把別人的資料寫進日誌", text=f"成員 A 的日記 {CANARY}")
        return {"status": "ok"}

    return app


def _diary_case(status: int = 200) -> EndpointCase:
    registry = EndpointRegistry()

    @registry.case("GET", DIARY_PATH, exempt="測試用", status=status)
    def _diary(_ctx: PermissionContext) -> EndpointRequest:
        return EndpointRequest()

    return registry.cases[Endpoint("GET", DIARY_PATH)]


def _log_reader(capsys: pytest.CaptureFixture[str]) -> Callable[[], str]:
    def read_logs() -> str:
        captured = capsys.readouterr()
        return captured.out + captured.err

    return read_logs


def test_canary_case_passes_for_a_clean_endpoint(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()

    run_canary_case(_diary_case(), _app_context(_diary_app(log_canary=False)), _log_reader(capsys))


def test_canary_case_fails_when_the_endpoint_logs_the_canary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 真的日誌設定（configure_logging 的 JSON 輸出到 stdout），不是替身。
    configure_logging()

    with pytest.raises(AssertionError, match="日誌出現了：金絲雀"):
        run_canary_case(
            _diary_case(), _app_context(_diary_app(log_canary=True)), _log_reader(capsys)
        )


def test_canary_case_ignores_logs_from_before_the_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 準備資料時留下的日誌（含金絲雀、含別的請求的「請求完成」）在送出前就清掉，不算這個請求的。
    configure_logging()
    registry = EndpointRegistry()

    @registry.case("GET", DIARY_PATH, exempt="測試用", status=200)
    def _diary(ctx: PermissionContext) -> EndpointRequest:
        ctx.as_owner("GET", "/healthz")
        diary_log.info("測試用：準備資料", text=CANARY)
        return EndpointRequest()

    (case,) = registry.cases.values()
    run_canary_case(case, _app_context(_diary_app(log_canary=False)), _log_reader(capsys))


def test_canary_case_fails_without_the_target_request_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 日誌沒接到（這裡故意讀不到任何東西）：「日誌裡沒有金絲雀」不能當證據。
    configure_logging()

    with pytest.raises(AssertionError, match="沒有接到這個請求"):
        run_canary_case(_diary_case(), _app_context(_diary_app(log_canary=False)), lambda: "")


def test_canary_case_fails_for_an_unexpected_status(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()

    with pytest.raises(AssertionError, match="預期 204，實際是 200"):
        run_canary_case(
            _diary_case(status=204),
            _app_context(_diary_app(log_canary=False)),
            _log_reader(capsys),
        )


def test_canary_case_fails_when_the_request_misses_the_endpoint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 豁免的端點打錯路徑得到 404：不能因為「沒帶出金絲雀」就通過。
    configure_logging()
    registry = EndpointRegistry()
    registry.case("GET", "/api/v1/test-only-diaries", exempt="測試用", status=404)(
        lambda _ctx: EndpointRequest()
    )
    (case,) = registry.cases.values()

    with pytest.raises(AssertionError, match="沒有打到登記的端點"):
        run_canary_case(case, _app_context(_diary_app(log_canary=False)), _log_reader(capsys))


def test_canary_case_checks_non_exempt_endpoints_are_hidden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()

    run_canary_case(_note_case(), _app_context(_notes_app()), _log_reader(capsys))
    with pytest.raises(AssertionError, match="成員 B"):
        run_canary_case(_note_case(), _app_context(_notes_app(leaky=True)), _log_reader(capsys))


@pytest.fixture
def diary_context(capsys: pytest.CaptureFixture[str]) -> PermissionContext:
    # 和整合測試一樣：context 在 fixture（setup 階段）建好，日誌也在這裡先設一次。
    configure_logging()
    return _app_context(_diary_app(log_canary=False))


def test_canary_case_with_a_context_built_in_a_fixture(
    diary_context: PermissionContext, capsys: pytest.CaptureFixture[str]
) -> None:
    # 整合測試的寫法：測試本體裡再設一次日誌，capsys 才讀得到目標請求的日誌（只在 fixture 設定會
    # 報「沒有接到這個請求」，CI 上 c73d72e 就是這樣失敗的）。
    configure_logging()

    run_canary_case(_diary_case(), diary_context, _log_reader(capsys))
