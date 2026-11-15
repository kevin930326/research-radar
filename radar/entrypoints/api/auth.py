"""api 的登入路由：我是誰、登出、假登入（ADR-0008、ADR-0046、AD-23、Story 3.3）。

- 路由都在同一個 `/api/v1` router；router 的相依項檢查 `Origin`：GET／HEAD／OPTIONS 以外的方法，
  `Origin` 缺少或不在允許清單一律 403（ADR-0008 第 3 項）。不做成全站中介層：`/api/` 底下沒註冊的
  路徑不管什麼方法都要回 404（`web.py`）。GET 不改業務資料（session 的最後使用時間除外）。
- 一定要在 `mount_web` 之前裝：頁面殼的「其餘路徑」會蓋掉之後才加的路由，所以頁面殼已經掛上時
  `install_auth_routes` 直接丟錯。
- session 的 cookie：`__Host-radar_session`、HttpOnly、Secure、SameSite=Lax、Path=/、沒有 Domain；
  值是 256 位元的隨機 token（資料庫只存它的 SHA-256）。
- 假登入的兩條路由只在開了假登入時註冊；沒開時打這些路徑是 404 JSON（掛了前端也一樣）。
- 回應不帶 email；被拒的原因只進日誌，回應只有代碼（`domain/errors.py`）。錯誤格式在 3.4 換成
  RFC 9457，這裡先用 FastAPI 預設的 `{"detail": <代碼>}`。
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from radar.domain.auth import (
    AdminMemberMissingError,
    AuthenticatedMember,
    FakeLoginRequest,
    LoginIdentity,
    LoginRejectedError,
    MemberSummary,
    NewSession,
    safe_next_path,
)
from radar.domain.auth_settings import (
    AuthSettings,
    FakeLoginMode,
    allowed_origins,
    fake_login_mode,
    session_policy,
)
from radar.domain.errors import ErrorCode
from radar.entrypoints.api.web import web_mounted
from radar.entrypoints.bootstrap import Database
from radar.services.auth import (
    authenticate_session,
    end_session,
    list_fake_login_members,
    log_in_with_fake_login,
)

SESSION_COOKIE: Final = "__Host-radar_session"
ACCESS_ASSERTION_HEADER: Final = "Cf-Access-Jwt-Assertion"
API_V1_PREFIX: Final = "/api/v1"
# 不改資料的方法：不檢查 Origin（瀏覽器的同源 GET 也常常不帶 Origin）。
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
# 登入狀態的回應不給任何快取存。
NO_STORE: Final = {"Cache-Control": "no-store"}

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class FakeLoginRoutes:
    """假登入要用的函式（只在開了假登入時有）。"""

    mode: FakeLoginMode
    list_members: Callable[[], Sequence[MemberSummary]]
    log_in: Callable[[FakeLoginRequest], NewSession]


@dataclass(frozen=True)
class AuthRoutes:
    """登入路由要用的一組函式與設定（`build_auth_routes` 組；測試換成假的函式）。"""

    authenticate: Callable[[str | None], AuthenticatedMember | None]
    log_out: Callable[[AuthenticatedMember], None]
    allowed_origins: frozenset[str]
    cookie_max_age: int
    fake_login: FakeLoginRoutes | None = None


def build_auth_routes(
    database: Database,
    auth_settings: AuthSettings,
    confirm: Callable[[FakeLoginRequest], LoginIdentity] | None,
    clock: Callable[[], datetime],
) -> AuthRoutes:
    """用資料庫、設定、身分確認函式（`bootstrap.build_fake_login_confirm`）與時鐘組好登入路由。

    Raises:
        ValueError: 開了假登入卻沒有身分確認函式。
    """
    policy = session_policy(auth_settings)

    def authenticate(token: str | None) -> AuthenticatedMember | None:
        return authenticate_session(database, token, policy=policy, now=clock())

    def log_out(member: AuthenticatedMember) -> None:
        end_session(member.viewer, database, session_id=member.session_id)

    mode = fake_login_mode(auth_settings)
    fake: FakeLoginRoutes | None = None
    if mode is not None:
        if confirm is None:
            raise ValueError("開了假登入卻沒有身分確認函式")
        verify = confirm

        def list_members() -> Sequence[MemberSummary]:
            # 試用環境不能選人：不列出任何成員。
            if mode is FakeLoginMode.PICK_MEMBER:
                return list_fake_login_members(database)
            return ()

        def log_in(request: FakeLoginRequest) -> NewSession:
            return log_in_with_fake_login(database, request, confirm=verify, now=clock())

        fake = FakeLoginRoutes(mode=mode, list_members=list_members, log_in=log_in)
    return AuthRoutes(
        authenticate=authenticate,
        log_out=log_out,
        allowed_origins=allowed_origins(auth_settings),
        cookie_max_age=int(policy.max_age.total_seconds()),
        fake_login=fake,
    )


class FakeLoginBody(BaseModel):
    """假登入的請求內容：選的成員（只有開發環境用得到）、登入後要轉回的路徑。"""

    model_config = ConfigDict(extra="forbid")

    member_id: uuid.UUID | None = None
    next: str | None = None


def _set_session_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def _member_json(member_id: uuid.UUID, display_name: str) -> dict[str, str]:
    return {"member_id": str(member_id), "display_name": display_name}


def install_auth_routes(app: FastAPI, routes: AuthRoutes) -> None:
    """把登入路由裝到 `app` 上。一定要在 `mount_web` 之前呼叫。

    Raises:
        RuntimeError: 頁面殼已經掛上（之後才加的路由會被它蓋掉）。
    """
    if web_mounted(app):
        raise RuntimeError("頁面殼已經掛上：API 路由要在 mount_web 之前註冊")

    async def check_origin(request: Request) -> None:
        """會改資料的請求：`Origin` 要在允許清單（ADR-0008 第 3 項）。"""
        if request.method in SAFE_METHODS:
            return
        origin = request.headers.get("origin")
        if origin is None or origin not in routes.allowed_origins:
            log.warning(
                "會改資料的請求來源不是本站，拒絕",
                method=request.method,
                path=request.url.path,
                has_origin=origin is not None,
            )
            raise HTTPException(status_code=403, detail=ErrorCode.ORIGIN_REJECTED.value)

    router = APIRouter(prefix=API_V1_PREFIX, dependencies=[Depends(check_origin)])

    def current_member(request: Request) -> AuthenticatedMember:
        member = routes.authenticate(request.cookies.get(SESSION_COOKIE))
        if member is None:
            raise HTTPException(
                status_code=401, detail=ErrorCode.NOT_AUTHENTICATED.value, headers=NO_STORE
            )
        return member

    @router.get("/me")
    def me(request: Request) -> JSONResponse:
        """我是誰：成員 ID 與顯示名稱（不含 email）；沒登入 401。"""
        member = current_member(request)
        return JSONResponse(_member_json(member.member_id, member.display_name), headers=NO_STORE)

    @router.post("/auth/logout", status_code=204)
    def logout(request: Request) -> Response:
        """登出：刪掉這個 session、清掉 cookie；沒登入也回 204。"""
        member = routes.authenticate(request.cookies.get(SESSION_COOKIE))
        if member is not None:
            routes.log_out(member)
        response = Response(status_code=204, headers=NO_STORE)
        _clear_session_cookie(response)
        return response

    fake = routes.fake_login
    if fake is not None:

        @router.get("/auth/fake-login")
        def fake_login_options() -> JSONResponse:
            """假登入的模式與可以選的成員（試用環境不能選人，清單是空的）。"""
            members = [_member_json(m.member_id, m.display_name) for m in fake.list_members()]
            return JSONResponse({"mode": fake.mode.value, "members": members}, headers=NO_STORE)

        @router.post("/auth/fake-login")
        def fake_login(body: FakeLoginBody, request: Request) -> JSONResponse:
            """假登入：確認身分、建 session、設 cookie，回站內的轉回路徑。"""
            login_request = FakeLoginRequest(
                member_id=body.member_id,
                access_assertion=request.headers.get(ACCESS_ASSERTION_HEADER),
            )
            try:
                session = fake.log_in(login_request)
            except LoginRejectedError:
                raise HTTPException(
                    status_code=403, detail=ErrorCode.LOGIN_REJECTED.value, headers=NO_STORE
                ) from None
            except AdminMemberMissingError as exc:
                log.error(
                    "試用環境的管理者成員還沒建立，假登入被拒；先執行下面兩個指令",
                    commands=list(exc.commands),
                )
                raise HTTPException(
                    status_code=403, detail=ErrorCode.ADMIN_MEMBER_MISSING.value, headers=NO_STORE
                ) from None
            response = JSONResponse({"next": safe_next_path(body.next)}, headers=NO_STORE)
            _set_session_cookie(response, session.token, routes.cookie_max_age)
            return response

    app.include_router(router)
