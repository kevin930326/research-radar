"""api 的登入路由：我是誰、登出、假登入（ADR-0008、ADR-0046、AD-23、Story 3.3）。

- 路由都在同一個 `/api/v1` router；router 的相依項檢查 `Origin`：GET／HEAD／OPTIONS 以外的方法，
  `Origin` 缺少或不在允許清單一律 403（ADR-0008 第 3 項）。不做成全站中介層：`/api/` 底下沒註冊的
  路徑不管什麼方法都要回 404（`web.py`）。GET 不改業務資料（session 的最後使用時間除外）。
  `check_origin`（相依項 `origin_dependency`）與 `current_member` 是模組函式，`/api/v1` 底下其他
  router（論文頁，`papers.py`）也用同一份（Story 3.6）。成員專屬的讀取與文獻庫的寫入改用
  `member_as_expected`：認完 session 再比對 `X-Radar-Expected-Member`（ADR-0055）。
- 一定要在 `mount_web` 之前裝：頁面殼的「其餘路徑」會蓋掉之後才加的路由，所以頁面殼已經掛上時
  `install_auth_routes` 直接丟錯。
- session 的 cookie：`__Host-radar_session`、HttpOnly、Secure、SameSite=Lax、Path=/、沒有 Domain；
  值是 256 位元的隨機 token（資料庫只存它的 SHA-256）。
- 假登入的兩條路由只在開了假登入時註冊；沒開時打這些路徑是 404 JSON（掛了前端也一樣）。
- `Origin` 與 `Cf-Access-Jwt-Assertion` 重複出現一律拒絕（不同層可能各取一個，就算其中一個是對的
  也不收）。
- 回應不帶 email；被拒的原因只進日誌，回應只有代碼（`domain/errors.py`），格式是 RFC 9457
  （`problems.py`，Story 3.4）。成功的回應都有 Pydantic 模型，前端型別由 `openapi.json` 產生。
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict

from radar.domain.auth import (
    AdminMemberMissingError,
    AuthenticatedMember,
    FakeLoginRequest,
    LoginIdentity,
    LoginRejectedError,
    LoginRejection,
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
from radar.entrypoints.api.problems import ApiProblemError
from radar.entrypoints.api.web import web_mounted
from radar.entrypoints.bootstrap import Database
from radar.services.auth import (
    authenticate_session,
    list_fake_login_members,
    log_in_with_fake_login,
    log_out_session,
)

SESSION_COOKIE: Final = "__Host-radar_session"
ACCESS_ASSERTION_HEADER: Final = "Cf-Access-Jwt-Assertion"
API_V1_PREFIX: Final = "/api/v1"
# 不改資料的方法：不檢查 Origin（瀏覽器的同源 GET 也常常不帶 Origin）。
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
# 登入狀態的回應不給任何快取存。
NO_STORE: Final = {"Cache-Control": "no-store"}
# 前端在成員專屬的讀取與文獻庫的寫入帶的「這個分頁以為的成員」（畫面上那份資料是用誰的身分讀的；
# ADR-0055）。
EXPECTED_MEMBER_HEADER: Final = "X-Radar-Expected-Member"

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
    # 收 cookie 裡的 token：不管 session 有沒有過期都刪（不先走 authenticate）。
    log_out: Callable[[str], None]
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

    def log_out(token: str) -> None:
        log_out_session(database, token)

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


class MemberResponse(BaseModel):
    """一位成員：ID 與顯示名稱（不含 email）。"""

    model_config = ConfigDict(extra="forbid")

    member_id: uuid.UUID
    display_name: str


class FakeLoginOptionsResponse(BaseModel):
    """假登入的模式與可以選的成員（試用環境不能選人，清單是空的）。"""

    model_config = ConfigDict(extra="forbid")

    mode: FakeLoginMode
    members: list[MemberResponse]


class FakeLoginResultResponse(BaseModel):
    """假登入成功：登入後要轉回的站內路徑。"""

    model_config = ConfigDict(extra="forbid")

    next: str


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


def check_origin(request: Request, routes: AuthRoutes) -> None:
    """會改資料的請求：`Origin` 要在允許清單（ADR-0008 第 3 項）。GET／HEAD／OPTIONS 不檢查。

    `/api/v1` 底下每個 router 都把它掛成相依項（`origin_dependency`）。

    Raises:
        ApiProblemError: `origin_rejected`（`Origin` 缺少、重複或不在允許清單）。
    """
    if request.method in SAFE_METHODS:
        return
    # 要恰好一個：重複的標頭（即使其中一個是對的）也拒絕，不讓不同層各取一個。
    origins = request.headers.getlist("origin")
    if len(origins) != 1 or origins[0] not in routes.allowed_origins:
        log.warning(
            "會改資料的請求來源不是本站，拒絕",
            method=request.method,
            path=request.url.path,
            origin_count=len(origins),
        )
        raise ApiProblemError(ErrorCode.ORIGIN_REJECTED)


def origin_dependency(routes: AuthRoutes) -> Callable[[Request], Awaitable[None]]:
    """給 `APIRouter(dependencies=[Depends(...)])` 用的 `check_origin`。"""

    async def origin_check(request: Request) -> None:
        check_origin(request, routes)

    return origin_check


def current_member(request: Request, routes: AuthRoutes) -> AuthenticatedMember:
    """cookie 的 session 認出的成員。

    Raises:
        ApiProblemError: `not_authenticated`（沒有 cookie、session 無效或過期），帶 `no-store`。
    """
    member = routes.authenticate(request.cookies.get(SESSION_COOKIE))
    if member is None:
        raise ApiProblemError(ErrorCode.NOT_AUTHENTICATED, headers=NO_STORE)
    return member


def _expected_member(value: str) -> uuid.UUID | None:
    """標頭的值 → 成員 ID；只收標準寫法（36 字、有連字號，大小寫不拘），其他回 None。"""
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value.lower() else None


def check_expected_member(request: Request, member: AuthenticatedMember) -> None:
    """請求帶了 `X-Radar-Expected-Member` 時，要和這次 session 的成員是同一位（ADR-0055）。

    沒帶就不檢查（照常）。比對的是 session 認出的成員，不看請求裡別的欄位。文獻庫的三個寫入與成員
    專屬的讀取（論文頁、找論文清單、文獻庫清單）都經 `member_as_expected` 用這一個函式。

    Raises:
        ApiProblemError: `expected_member_invalid`（值不是成員 ID，或標頭出現不只一次）、
            `member_changed`（和 session 的成員不同）；都帶 `no-store`。
    """
    values = request.headers.getlist(EXPECTED_MEMBER_HEADER)
    if not values:
        return
    expected = _expected_member(values[0]) if len(values) == 1 else None
    if expected is None:
        log.warning(
            "預期成員標頭格式不對，拒絕",
            method=request.method,
            path=request.url.path,
            header_count=len(values),
        )
        raise ApiProblemError(ErrorCode.EXPECTED_MEMBER_INVALID, headers=NO_STORE)
    if expected != member.member_id:
        # 不記成員 ID：兩位成員的 ID 一起進日誌沒有必要（只要知道擋過）。
        log.info(
            "預期成員和 session 不同（換了人），不讀不寫",
            method=request.method,
            path=request.url.path,
        )
        raise ApiProblemError(ErrorCode.MEMBER_CHANGED, headers=NO_STORE)


def member_as_expected(request: Request, routes: AuthRoutes) -> AuthenticatedMember:
    """先認 session（`current_member`，沒登入 401），再比對預期成員（`check_expected_member`，
    409／400）；回 session 的成員。都在呼叫 services 之前。"""
    member = current_member(request, routes)
    check_expected_member(request, member)
    return member


def install_auth_routes(app: FastAPI, routes: AuthRoutes) -> None:
    """把登入路由裝到 `app` 上。一定要在 `mount_web` 之前呼叫。

    Raises:
        RuntimeError: 頁面殼已經掛上（之後才加的路由會被它蓋掉）。
    """
    if web_mounted(app):
        raise RuntimeError("頁面殼已經掛上：API 路由要在 mount_web 之前註冊")

    router = APIRouter(prefix=API_V1_PREFIX, dependencies=[Depends(origin_dependency(routes))])

    @router.get("/me")
    def me(request: Request, response: Response) -> MemberResponse:
        """我是誰：成員 ID 與顯示名稱（不含 email）；沒登入 401。"""
        member = current_member(request, routes)
        response.headers.update(NO_STORE)
        return MemberResponse(member_id=member.member_id, display_name=member.display_name)

    @router.post("/auth/logout", status_code=204)
    def logout(request: Request) -> Response:
        """登出：刪掉 cookie 對到的 session（過期的也刪）、清掉 cookie；沒登入也回 204。"""
        token = request.cookies.get(SESSION_COOKIE)
        if token is not None:
            routes.log_out(token)
        response = Response(status_code=204, headers=NO_STORE)
        _clear_session_cookie(response)
        return response

    fake = routes.fake_login
    if fake is not None:

        @router.get("/auth/fake-login")
        def fake_login_options(response: Response) -> FakeLoginOptionsResponse:
            """假登入的模式與可以選的成員（試用環境不能選人，清單是空的）。"""
            members = [
                MemberResponse(member_id=m.member_id, display_name=m.display_name)
                for m in fake.list_members()
            ]
            response.headers.update(NO_STORE)
            return FakeLoginOptionsResponse(mode=fake.mode, members=members)

        @router.post("/auth/fake-login")
        def fake_login(
            body: FakeLoginBody, request: Request, response: Response
        ) -> FakeLoginResultResponse:
            """假登入：確認身分、建 session、設 cookie，回站內的轉回路徑。"""
            # Access 的 JWT 最多一個：重複就拒絕（不管哪一個有效、排在前面還是後面）。
            assertions = request.headers.getlist(ACCESS_ASSERTION_HEADER)
            if len(assertions) > 1:
                log.warning("假登入被拒", reason=LoginRejection.ASSERTION_DUPLICATED.value)
                raise ApiProblemError(ErrorCode.LOGIN_REJECTED, headers=NO_STORE)
            login_request = FakeLoginRequest(
                member_id=body.member_id,
                access_assertion=assertions[0] if assertions else None,
            )
            try:
                session = fake.log_in(login_request)
            except LoginRejectedError:
                raise ApiProblemError(ErrorCode.LOGIN_REJECTED, headers=NO_STORE) from None
            except AdminMemberMissingError as exc:
                log.error(
                    "試用環境的管理者成員還沒建立，假登入被拒；先執行下面兩個指令",
                    commands=list(exc.commands),
                )
                raise ApiProblemError(ErrorCode.ADMIN_MEMBER_MISSING, headers=NO_STORE) from None
            response.headers.update(NO_STORE)
            _set_session_cookie(response, session.token, routes.cookie_max_age)
            return FakeLoginResultResponse(next=safe_next_path(body.next))

    app.include_router(router)
