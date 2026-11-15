"""換人測試與金絲雀測試的框架（FR-7、AD-3、ADR-0002、ADR-0047）。

- **端點清單以 `openapi.json` 為準**（AD-3）：`endpoints_in_openapi` 讀出每個「方法＋路徑」。
- **每個端點要登記一次**（`EndpointRegistry.case` 裝飾器）：登記的函式可以先替成員 A 建好私人資料，
  再回傳**一份**請求（`EndpointRequest`：路徑參數、查詢、本文；方法與路徑樣板來自登記的端點）。
  框架拿這一份請求，成員 B 送一次、成員 A 送一次，**只換身分**。
- 框架核對**實際處理請求的路由**（Starlette 路由比對寫進 scope 的 `route`）就是登記的端點：
  路徑打錯、被別的路由接走、方法不對（只對上路徑），結果都不能當證據（`assert_reached`）。
- 換人測試（沒豁免的端點，`run_cross_member_case`）：B 要得到「看不到」（404 或空清單），A 自己要
  拿得到（對照）。沒有對照，路徑打錯、資料沒建成也會得到 404，換人測試會白白通過。
- 「看不到」的判定（ADR-0002：不回 403，不透露資料存在，`assert_hidden_from_other_member`）：
  - 404：本文要**剛好**是 RFC 9457 的四個欄位（ADR-0047：`type`、`title`、`status`、`code`），
    `application/problem+json`；多帶任何欄位都失敗。
  - 空清單：`items == []`，而且只能有回應模型宣告的清單欄位（Conventions「API」的 `items`、
    `next_cursor`、`total`、`snapshot`，`list_fields_of`）；`total` 沒有或是 0、`next_cursor`
    沒有值。
- 豁免換人測試時寫 `exempt="理由"` 與 `status=預期狀態碼`；豁免的端點照樣有呼叫函式，
  金絲雀測試一樣會打它，狀態碼不是預期的就失敗。
- 金絲雀（`run_canary_case`）：成員 A 的 email 是固定的金絲雀字串；B 打每個登記的端點，B 的回應
  （本文與標頭）與**這個請求期間**的日誌都不能出現它，也不能出現 A 的 session token。送出前先清空
  捕捉內容，並以這個請求的 request ID 確認日誌真的接到了。
- `coverage_problems` 比對 `openapi.json` 與登記表；`routes_missing_from_openapi` 找出躲在
  `openapi.json` 外面的路由（框架自己的頁面以「實際處理函式＋方法」辨認，不看路徑格式）。單元測試
  要求兩者都沒有問題，所以新增端點卻沒登記時 CI 失敗。
"""

import json
import re
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from types import CodeType
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts
from fastapi.testclient import TestClient
from httpx2 import Response
from starlette.types import Receive, Scope, Send

from radar.entrypoints.api.app import REQUEST_ID_HEADER
from radar.entrypoints.api.auth import SESSION_COOKIE
from radar.entrypoints.api.problems import PROBLEM_MEDIA_TYPE, PROBLEM_TYPE
from radar.entrypoints.api.web import is_web_shell

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPO_ROOT / "openapi.json"

# OpenAPI 的 path item 裡代表 HTTP 方法的鍵（其他鍵如 `parameters`、`summary` 不是端點）。
_OPENAPI_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

# 金絲雀字串：固定、全專案只有這裡會出現（成員 A 的私人資料裡放它）。
CANARY = "canary-7d3f9a2e-private"
CANARY_EMAIL = f"{CANARY}@example.com"

# 換人測試「看不到」的 404：剛好是這個本文（ADR-0047 的四個欄位），多一個欄位都不行。
NOT_FOUND_CODE = "not_found"
NOT_FOUND_PROBLEM: Mapping[str, object] = {
    "type": PROBLEM_TYPE,
    "title": HTTPStatus.NOT_FOUND.phrase,
    "status": HTTPStatus.NOT_FOUND.value,
    "code": NOT_FOUND_CODE,
}
# Conventions「API」的清單欄位：B 拿到空清單時，只能有回應模型宣告的這幾個欄位。
LIST_ENVELOPE = frozenset({"items", "next_cursor", "total", "snapshot"})

# 中介層記的「一個請求結束」那筆日誌（radar/entrypoints/api/app.py）。
REQUEST_FINISHED_EVENT = "請求完成"

_PATH_PARAM = re.compile(r"\{([^{}]+)\}")


@dataclass(frozen=True, order=True)
class Endpoint:
    """一個端點：HTTP 方法（大寫）＋ `openapi.json` 裡的路徑樣板（`/api/v1/papers/{paper_id}`）。"""

    method: str
    path: str

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(frozen=True)
class Member:
    """測試裡的一位成員：ID、登入後的 session token、email。"""

    member_id: str
    token: str
    email: str


@dataclass(frozen=True)
class EndpointRequest:
    """一個用例唯一的一份請求：路徑參數、查詢、本文。

    方法與路徑樣板來自登記的端點，用例改不了；成員 A 與成員 B 送的就是這一份，只換身分。
    """

    path_params: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, str] = field(default_factory=dict)
    body: Any = None


@dataclass(frozen=True)
class ReachedRoute:
    """實際處理請求的那條路由：路徑樣板（含 router 的前綴）與它接受的方法。"""

    path: str | None
    methods: frozenset[str]

    def __str__(self) -> str:
        return f"{'/'.join(sorted(self.methods)) or '任何方法'} {self.path}"


@dataclass(frozen=True)
class SentRequest:
    """送出去的一個請求：回應，以及實際處理它的路由（沒有路由接手時是 None）。"""

    response: Response
    route: ReachedRoute | None


class _RouteRecorder:
    """包住 app 的 ASGI 程式：記下最後一個請求由哪一條路由處理。

    Starlette 的路由比對把選中的路由寫進 scope 的 `route`（同一個 dict 一路傳下去）。完全對上
    （`Match.FULL`）與只有路徑對上、方法不對（`PARTIAL`，回 405）都會寫，所以方法要另外核對。
    """

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.route: object | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        finally:
            if scope["type"] == "http":
                self.route = scope.get("route")


def _reached(app: FastAPI, route: object | None) -> ReachedRoute | None:
    """把 scope 裡的路由物件對回 app 的路由表（`include_router` 的前綴也算進路徑樣板）。"""
    if route is None:
        return None
    for context in iter_route_contexts(app.routes):
        if context.original_route is route or context.route is route:
            return ReachedRoute(context.path_format, frozenset(context.methods or ()))
    return ReachedRoute(
        getattr(route, "path_format", None), frozenset(getattr(route, "methods", None) or ())
    )


def endpoint_url(endpoint: Endpoint, path_params: Mapping[str, str]) -> str:
    """把路徑參數填進端點的路徑樣板（每個值都編碼，`/` 也是）。

    Raises:
        ValueError: 給的路徑參數和樣板裡的不一樣（少給或多給）。
    """
    names = _PATH_PARAM.findall(endpoint.path)
    if set(names) != set(path_params):
        raise ValueError(
            f"{endpoint} 的路徑參數要剛好是 {sorted(names)}，實際給了 {sorted(path_params)}"
        )
    return _PATH_PARAM.sub(
        lambda match: quote(str(path_params[match.group(1)]), safe=""), endpoint.path
    )


class PermissionContext:
    """登記的呼叫函式拿到的東西：app、資料的主人（成員 A）、呼叫的人（成員 B）。

    `extras` 給需要資料庫的用例放連線（例如整合測試放 `Database`），框架本身不用它。
    `client` 送出的每個請求都會記下實際處理它的路由（`send` 用它核對）。
    """

    def __init__(
        self,
        *,
        app: FastAPI,
        owner: Member,
        other: Member,
        origin: str,
        extras: Mapping[str, Any] | None = None,
        base_url: str = "https://testserver",
    ) -> None:
        self.app = app
        self.owner = owner
        self.other = other
        self.origin = origin
        self.extras: Mapping[str, Any] = dict(extras or {})
        self._recorder = _RouteRecorder(app)
        self.client = TestClient(self._recorder, base_url=base_url)

    def send(
        self,
        member: Member,
        endpoint: Endpoint,
        request: EndpointRequest,
        *,
        request_id: str | None = None,
    ) -> SentRequest:
        """以 `member` 的身分送登記的端點：方法與路徑樣板來自 `endpoint`，其餘來自 `request`。"""
        headers = {} if request_id is None else {REQUEST_ID_HEADER: request_id}
        self._recorder.route = None
        response = self._send(
            member,
            endpoint.method,
            endpoint_url(endpoint, request.path_params),
            params=dict(request.query),
            json=request.body,
            headers=headers,
        )
        return SentRequest(response, _reached(self.app, self._recorder.route))

    def as_other(self, method: str, path: str, **kwargs: Any) -> Response:
        """以成員 B 的身分送任意請求（帶 B 的 session cookie 與本站的 Origin）。

        換人測試的目標請求由框架送（`send`）；這個只給準備資料或檢查 session 用。
        """
        return self._send(self.other, method, path, **kwargs)

    def as_owner(self, method: str, path: str, **kwargs: Any) -> Response:
        """以成員 A（資料的主人）的身分送任意請求；例如呼叫函式經 API 替 A 建資料。"""
        return self._send(self.owner, method, path, **kwargs)

    def _send(self, member: Member, method: str, path: str, **kwargs: Any) -> Response:
        headers = {
            "Cookie": f"{SESSION_COOKIE}={member.token}",
            "Origin": self.origin,
            **kwargs.pop("headers", {}),
        }
        self.client.cookies.clear()
        return self.client.request(method, path, headers=headers, **kwargs)


EndpointCall = Callable[[PermissionContext], EndpointRequest]


@dataclass(frozen=True)
class EndpointCase:
    """一個端點的登記：怎麼準備資料、送哪一份請求（`call`）。

    `exempt_reason` 有值就不跑換人測試（金絲雀照跑），這時 `expected_status` 是成員 B 打它的預期
    狀態碼；沒豁免時預期一律是「看不到」，不寫狀態碼。
    """

    endpoint: Endpoint
    call: EndpointCall
    exempt_reason: str | None = None
    expected_status: int | None = None


class EndpointRegistry:
    """端點的登記表。一個端點只能登記一次。"""

    def __init__(self) -> None:
        self._cases: dict[Endpoint, EndpointCase] = {}

    def case(
        self,
        method: str,
        path: str,
        *,
        exempt: str | None = None,
        status: int | None = None,
    ) -> Callable[[EndpointCall], EndpointCall]:
        """登記一個端點。被裝飾的函式回傳這個端點的一份請求（`EndpointRequest`）。

        `exempt` 是豁免換人測試的理由（不能空白），豁免時 `status` 寫成員 B 打它的預期狀態碼。

        Raises:
            ValueError: 同一個端點登記兩次。
        """
        endpoint = Endpoint(method.upper(), path)

        def register(call: EndpointCall) -> EndpointCall:
            if endpoint in self._cases:
                raise ValueError(f"{endpoint} 登記了兩次")
            self._cases[endpoint] = EndpointCase(endpoint, call, exempt, status)
            return call

        return register

    @property
    def cases(self) -> Mapping[Endpoint, EndpointCase]:
        return dict(self._cases)


def endpoints_in_openapi(document: Mapping[str, Any]) -> frozenset[Endpoint]:
    """`openapi.json` 列出的每個端點（方法＋路徑樣板）。"""
    return frozenset(
        Endpoint(method.upper(), path)
        for path, item in document.get("paths", {}).items()
        for method in item
        if method in _OPENAPI_METHODS
    )


def list_fields_of(document: Mapping[str, Any], endpoint: Endpoint) -> frozenset[str]:
    """端點 200 回應的模型宣告了哪些清單欄位（`LIST_ENVELOPE` 裡的）；沒有 `items` 表示不是清單。"""
    operation = document.get("paths", {}).get(endpoint.path, {}).get(endpoint.method.lower(), {})
    content = operation.get("responses", {}).get("200", {}).get("content", {})
    schema = content.get("application/json", {}).get("schema", {})
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        schema = document.get("components", {}).get("schemas", {}).get(ref.rsplit("/", 1)[1], {})
    return frozenset(schema.get("properties", {})) & LIST_ENVELOPE


def coverage_problems(
    document: Mapping[str, Any], cases: Mapping[Endpoint, EndpointCase]
) -> list[str]:
    """`openapi.json` 與登記表對不上的地方；空清單表示每個端點都有換人測試或寫了理由與預期狀態碼的
    豁免。"""
    documented = endpoints_in_openapi(document)
    problems = [
        f"這個端點沒有換人測試：{endpoint}" for endpoint in sorted(documented - cases.keys())
    ]
    problems += [
        f"登記了 openapi.json 沒有的端點：{endpoint}"
        for endpoint in sorted(cases.keys() - documented)
    ]
    problems += [
        f"豁免換人測試要寫理由：{endpoint}"
        for endpoint, case in sorted(cases.items())
        if case.exempt_reason is not None and not case.exempt_reason.strip()
    ]
    problems += [
        f"豁免的端點要寫預期狀態碼：{endpoint}"
        for endpoint, case in sorted(cases.items())
        if case.exempt_reason is not None and case.expected_status is None
    ]
    problems += [
        f"沒豁免的端點不寫預期狀態碼（預期一律是 404 或空清單）：{endpoint}"
        for endpoint, case in sorted(cases.items())
        if case.exempt_reason is None and case.expected_status is not None
    ]
    return problems


# 不在 openapi.json 裡也可以的路由，以「實際處理函式＋方法」辨認，不看路徑格式：
# - FastAPI 自己的文件頁（/openapi.json、/docs、/docs/oauth2-redirect、/redoc）：處理函式要是
#   FastAPI 的那幾個（每個 app 各有一份閉包，比對函式的程式碼物件），方法要一樣。
# - 成員版前端的頁面殼（`mount_web` 的 `/{path:path}`，攤開後是 `/{path}`）：處理程式要是頁面殼
#   本身、不設方法清單（它自己只對 GET／HEAD 回頁面殼）。
# - 健康檢查的 HEAD：要和同一個路徑的 GET 用同一個處理函式。
# 每一種都只能出現一次。
WEB_SHELL_PATH = "/{path}"
UNDOCUMENTED_HEAD_PATHS = frozenset({"/healthz", "/readyz"})

_DocRoutes = Mapping[str, tuple[frozenset[str], CodeType | None]]


def _fastapi_doc_routes() -> _DocRoutes:
    """一個全新的 FastAPI 自己帶的路由：路徑 → （方法, 處理函式的程式碼物件）。"""
    return {
        str(context.path_format): (
            frozenset(context.methods or ()),
            getattr(context.endpoint, "__code__", None),
        )
        for context in iter_route_contexts(FastAPI().routes)
        if not isinstance(context.original_route, APIRoute)
    }


def _framework_route_kind(context: RouteContext, doc_routes: _DocRoutes) -> str | None:
    """這條非 API 路由是哪一種框架頁面；處理函式或方法對不上就回 None。"""
    path = str(context.path_format)
    route = context.original_route
    endpoint = getattr(route, "endpoint", None)
    methods = getattr(route, "methods", None)
    if path == WEB_SHELL_PATH and is_web_shell(endpoint) and methods is None:
        return "前端的頁面殼"
    expected = doc_routes.get(path)
    if expected is None:
        return None
    expected_methods, expected_code = expected
    code = getattr(endpoint, "__code__", None)
    if frozenset(methods or ()) == expected_methods and code is not None and code is expected_code:
        return f"FastAPI 的文件頁 {path}"
    return None


def routes_missing_from_openapi(app: FastAPI) -> list[str]:
    """app 上有、`openapi.json` 卻沒列的路由（例如 `include_in_schema=False`）：會躲過換人測試。"""
    documented = endpoints_in_openapi(app.openapi())
    doc_routes = _fastapi_doc_routes()
    # iter_route_contexts 把 include_router 裝進來的路由攤開（app.routes 只看得到 router 物件）。
    contexts = list(iter_route_contexts(app.routes))
    get_handlers = {
        str(context.path_format): context.original_route.endpoint
        for context in contexts
        if isinstance(context.original_route, APIRoute) and "GET" in (context.methods or ())
    }
    missing: list[str] = []
    framework_seen: Counter[str] = Counter()
    endpoints_seen: Counter[Endpoint] = Counter()
    for context in contexts:
        path = str(context.path_format)
        route = context.original_route
        if not isinstance(route, APIRoute):
            kind = _framework_route_kind(context, doc_routes)
            if kind is None:
                missing.append(
                    f"不是一般 API 路由、也不是框架自己的頁面（處理函式或方法對不上）：{path}"
                )
            else:
                framework_seen[kind] += 1
            continue
        for method in sorted(context.methods or ()):
            endpoint = Endpoint(method, path)
            endpoints_seen[endpoint] += 1
            if endpoint in documented:
                continue
            if method == "HEAD" and path in UNDOCUMENTED_HEAD_PATHS:
                if context.methods != {"HEAD"} or get_handlers.get(path) is not route.endpoint:
                    missing.append(f"健康檢查的 HEAD 要和 GET 用同一個處理函式：{endpoint}")
                continue
            missing.append(f"沒有列進 openapi.json：{endpoint}")
    missing += [
        f"框架自己的頁面裝了不只一次：{kind}"
        for kind, count in sorted(framework_seen.items())
        if count > 1
    ]
    missing += [
        f"同一個端點裝了不只一次：{endpoint}"
        for endpoint, count in sorted(endpoints_seen.items())
        if count > 1
    ]
    return missing


def _media_type(response: Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def assert_reached(sent: SentRequest, endpoint: Endpoint, who: str) -> None:
    """實際處理這個請求的路由，就是登記的端點（路徑樣板相同、接受這個方法）。

    Raises:
        AssertionError: 沒有路由接手（找不到路徑）、被別的路由接走、只對上路徑（方法不對）。
    """
    route = sent.route
    reached = route is not None and route.path == endpoint.path and endpoint.method in route.methods
    assert reached, (
        f"{who}的請求沒有打到登記的端點 {endpoint}：實際處理的路由是 {route or '沒有'}"
        f"（{sent.response.status_code}），這個結果不能當證據"
    )


def assert_hidden_from_other_member(
    response: Response, *, list_fields: frozenset[str] = LIST_ENVELOPE
) -> None:
    """換人測試的預期：404（剛好是 RFC 9457 的四個欄位、代碼 not_found）或空清單。

    `list_fields` 是空清單可以有的欄位（框架從端點的回應模型推出，`list_fields_of`）；沒有 `items`
    表示這個端點不是清單，只接受 404。

    Raises:
        AssertionError: 其他結果（含 403：回 403 等於告訴對方資料存在，ADR-0002），或 404／空清單
            多帶了欄位。
    """
    if response.status_code == 404:
        assert _media_type(response) == PROBLEM_MEDIA_TYPE, (
            f"404 但不是 {PROBLEM_MEDIA_TYPE}：{response.headers.get('content-type')}"
        )
        body = response.json()
        assert body == NOT_FOUND_PROBLEM, (
            f"404 的本文要剛好是 RFC 9457 的四個欄位（ADR-0047），不能多帶或少帶：{body}"
        )
        return
    assert response.status_code == 200, (
        f"成員 B 看別人的資料要得到 404 或空清單，實際是 {response.status_code}：{response.text}"
    )
    assert _media_type(response) == "application/json", (
        f"200 但不是 application/json：{response.headers.get('content-type')}"
    )
    body = response.json()
    assert isinstance(body, dict), f"200 但不是清單的回應：{response.text}"
    assert "items" in list_fields, (
        f"這個端點的回應模型不是清單（沒有 items），成員 B 只能拿到 404：{response.text}"
    )
    extra = sorted(body.keys() - list_fields)
    assert not extra, f"成員 B 的空清單多了回應模型以外的欄位 {extra}：{response.text}"
    assert body.get("items") == [], f"成員 B 拿到了成員 A 的資料：{response.text}"
    # 清單是空的、總數卻不是 0：等於透露了 A 的資料筆數。
    assert body.get("total") in (None, 0), f"成員 B 看得到成員 A 的資料筆數：{response.text}"
    assert body.get("next_cursor") is None, f"成員 B 的空清單卻有下一頁：{response.text}"


def assert_visible_to_owner(response: Response) -> None:
    """對照的預期：成員 A 自己打同一筆資料，200 且有內容（清單的話 `items` 不是空的）。

    Raises:
        AssertionError: 拿不到（路徑打錯、資料沒建成、權限寫錯把主人也擋掉）。
    """
    assert response.status_code == 200, (
        f"對照失敗：成員 A 自己也拿不到（{response.status_code}），換人測試的 404 不能當證據："
        f"{response.text}"
    )
    body = response.json()
    if isinstance(body, dict) and "items" in body:
        assert body["items"], f"對照失敗：成員 A 自己的清單是空的：{response.text}"
    else:
        assert body, f"對照失敗：回應是空的：{response.text}"


def run_cross_member_case(case: EndpointCase, ctx: PermissionContext) -> None:
    """換人測試：同一份請求，成員 B 送要「看不到」，接著成員 A 送要拿得到；兩次都要打到登記的端點。

    Raises:
        AssertionError: 任何一項不符合。
        ValueError: 這個端點是豁免的。
    """
    if case.exempt_reason is not None:
        raise ValueError(f"{case.endpoint} 豁免換人測試，不能跑")
    request = case.call(ctx)
    other = ctx.send(ctx.other, case.endpoint, request)
    assert_reached(other, case.endpoint, "成員 B ")
    assert_hidden_from_other_member(
        other.response, list_fields=list_fields_of(ctx.app.openapi(), case.endpoint)
    )
    owner = ctx.send(ctx.owner, case.endpoint, request)
    assert_reached(owner, case.endpoint, "對照（成員 A）")
    assert_visible_to_owner(owner.response)


def request_log_records(logs: str, request_id: str) -> list[dict[str, Any]]:
    """日誌（每行一筆 JSON）裡屬於這個 request ID 的紀錄。"""
    records: list[dict[str, Any]] = []
    for line in logs.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("request_id") == request_id:
            records.append(record)
    return records


def run_canary_case(
    case: EndpointCase, ctx: PermissionContext, read_logs: Callable[[], str]
) -> None:
    """金絲雀測試：成員 B 送這個端點的請求，回應與這段期間的日誌都不能有 A 的金絲雀或 token。

    `read_logs` 每次呼叫回傳上次之後的日誌並清空（例如 pytest 的 `capsys.readouterr()`）。送出前先
    清空一次（準備資料、兩位成員登入留下的不算），送出時帶一個新的 request ID，日誌裡要有這個 ID 的
    「請求完成」，才算真的接到了這個請求的日誌。日誌設定（`configure_logging()`）要在測試本體裡呼叫：
    在 fixture（setup 階段）設定的話，handler 綁到 setup 階段的輸出，`capsys` 讀不到這個請求的日誌，
    這裡會報錯。

    Raises:
        AssertionError: 沒打到登記的端點、狀態不是預期的（豁免）或不是「看不到」（沒豁免）、沒接到
            這個請求的日誌、出現了金絲雀或 A 的 token。
    """
    request = case.call(ctx)
    read_logs()
    request_id = uuid.uuid4().hex
    sent = ctx.send(ctx.other, case.endpoint, request, request_id=request_id)
    logs = read_logs()

    assert_reached(sent, case.endpoint, "成員 B ")
    if case.exempt_reason is None:
        assert_hidden_from_other_member(
            sent.response, list_fields=list_fields_of(ctx.app.openapi(), case.endpoint)
        )
    else:
        assert sent.response.status_code == case.expected_status, (
            f"成員 B 打 {case.endpoint} 預期 {case.expected_status}，實際是 "
            f"{sent.response.status_code}：{sent.response.text}"
        )
    assert sent.response.headers.get(REQUEST_ID_HEADER) == request_id, (
        f"回應的 {REQUEST_ID_HEADER} 不是送出去的那一個"
    )
    finished = [
        record
        for record in request_log_records(logs, request_id)
        if record.get("event") == REQUEST_FINISHED_EVENT
    ]
    assert finished, (
        f"沒有接到這個請求（{request_id}）的「{REQUEST_FINISHED_EVENT}」日誌，"
        "「日誌裡沒有金絲雀」不能當證據"
    )
    labels = {CANARY: "金絲雀", ctx.owner.token: "成員 A 的 session token"}
    leaked = secrets_found([*response_texts(sent.response), logs], labels)
    assert leaked == [], (
        f"成員 B 打 {case.endpoint} 的回應或日誌出現了：{'、'.join(labels[s] for s in leaked)}"
    )


def response_texts(response: Response) -> list[str]:
    """回應裡要檢查的文字：本文與每個標頭的名稱和值。"""
    return [response.text, *(f"{name}: {value}" for name, value in response.headers.items())]


def secrets_found(texts: Iterable[str], secrets: Iterable[str]) -> list[str]:
    """`texts` 裡出現了哪些秘密字串（金絲雀、別人的 token）；空清單表示都沒出現。"""
    joined = "\n".join(texts)
    return [secret for secret in secrets if secret in joined]
