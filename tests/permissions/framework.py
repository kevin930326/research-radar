"""換人測試與金絲雀測試的框架（FR-7、AD-3、ADR-0002、ADR-0047）。

- **端點清單以 `openapi.json` 為準**（AD-3）：`endpoints_in_openapi` 讀出每個「方法＋路徑」。
- **每個端點要登記一次**（`EndpointRegistry.case` 裝飾器）：登記的函式是「成員 B 怎麼呼叫這個
  端點」，可以先替成員 A 建好私人資料再呼叫。豁免換人測試時寫 `exempt="理由"`；豁免的端點照樣有
  呼叫函式，金絲雀測試一樣會打它。
- 沒有豁免的端點還要登記**對照**（`control=`）：成員 A 自己打同一筆資料要拿得到。沒有對照，
  路徑打錯、資料沒建成也會得到 404，換人測試會白白通過（`assert_visible_to_owner`）。
- `coverage_problems` 比對 `openapi.json` 與登記表：沒登記的端點、登記了但 `openapi.json` 沒有的
  端點、理由空白的豁免、沒有對照的換人測試都算問題。單元測試要求沒有問題，所以新增端點卻沒登記時
  CI 失敗。
- 換人測試的預期是「404 或空清單」（ADR-0002：不回 403，不透露資料存在）：
  `assert_hidden_from_other_member`。
- 金絲雀：成員 A 的 email 是固定的金絲雀字串；B 的回應（本文與標頭）與日誌都不能出現它，也不能出現
  A 的 session token（`secrets_found`）。
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from httpx2 import Response

from radar.entrypoints.api.auth import SESSION_COOKIE

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPO_ROOT / "openapi.json"

# OpenAPI 的 path item 裡代表 HTTP 方法的鍵（其他鍵如 `parameters`、`summary` 不是端點）。
_OPENAPI_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

# 金絲雀字串：固定、全專案只有這裡會出現（成員 A 的私人資料裡放它）。
CANARY = "canary-7d3f9a2e-private"
CANARY_EMAIL = f"{CANARY}@example.com"

# 換人測試「看不到」的兩種結果：404（和不存在同一個回應）或空清單（Conventions「API」的 `items`）。
NOT_FOUND_CODE = "not_found"


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
class PermissionContext:
    """登記的呼叫函式拿到的東西：client、資料的主人（成員 A）、呼叫的人（成員 B）。

    `extras` 給需要資料庫的用例放連線（例如整合測試放 `Database`），框架本身不用它。
    `scratch` 給同一個用例的呼叫函式與對照函式傳東西（例如呼叫函式建好的資料 ID），每個測試一份。
    """

    client: TestClient
    owner: Member
    other: Member
    origin: str
    extras: Mapping[str, Any] = field(default_factory=dict)
    scratch: dict[str, Any] = field(default_factory=dict)

    def as_other(self, method: str, path: str, **kwargs: Any) -> Response:
        """以成員 B 的身分送請求：帶 B 的 session cookie 與本站的 Origin（會改資料的請求要它）。"""
        return self._send(self.other, method, path, **kwargs)

    def as_owner(self, method: str, path: str, **kwargs: Any) -> Response:
        """以成員 A（資料的主人）的身分送請求：只給對照函式用。"""
        return self._send(self.owner, method, path, **kwargs)

    def _send(self, member: Member, method: str, path: str, **kwargs: Any) -> Response:
        headers = {
            "Cookie": f"{SESSION_COOKIE}={member.token}",
            "Origin": self.origin,
            **kwargs.pop("headers", {}),
        }
        self.client.cookies.clear()
        return self.client.request(method, path, headers=headers, **kwargs)


EndpointCall = Callable[[PermissionContext], Response]


@dataclass(frozen=True)
class EndpointCase:
    """一個端點的登記：成員 B 怎麼呼叫它；`exempt_reason` 有值就不跑換人測試（金絲雀照跑）。

    `control` 是對照：在 `call` 之後、同一個 context 上，成員 A 自己打同一筆資料（要拿得到）。
    """

    endpoint: Endpoint
    call: EndpointCall
    exempt_reason: str | None = None
    control: EndpointCall | None = None


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
        control: EndpointCall | None = None,
    ) -> Callable[[EndpointCall], EndpointCall]:
        """登記「成員 B 怎麼呼叫這個端點」。`exempt` 是豁免換人測試的理由（不能空白）；
        沒有豁免時要給 `control`（成員 A 自己打同一筆資料，用 `ctx.as_owner`）。

        Raises:
            ValueError: 同一個端點登記兩次。
        """
        endpoint = Endpoint(method.upper(), path)

        def register(call: EndpointCall) -> EndpointCall:
            if endpoint in self._cases:
                raise ValueError(f"{endpoint} 登記了兩次")
            self._cases[endpoint] = EndpointCase(endpoint, call, exempt, control)
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


def coverage_problems(
    document: Mapping[str, Any], cases: Mapping[Endpoint, EndpointCase]
) -> list[str]:
    """`openapi.json` 與登記表對不上的地方；空清單表示每個端點都有換人測試（含對照）或寫了
    理由的豁免。"""
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
        f"換人測試要有對照（成員 A 自己拿得到）：{endpoint}"
        for endpoint, case in sorted(cases.items())
        if case.exempt_reason is None and case.control is None
    ]
    return problems


# 不在 openapi.json 裡也可以的路由：FastAPI 自己的文件頁、成員版前端的頁面殼（`mount_web` 的
# `/{path:path}`，攤開後是 `/{path}`；只回靜態檔與 index.html），與健康檢查的 HEAD（和 GET 同一個
# 函式）。
FRAMEWORK_PATHS = frozenset(
    {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/{path}"}
)
UNDOCUMENTED_ALLOWED = frozenset({Endpoint("HEAD", "/healthz"), Endpoint("HEAD", "/readyz")})


def routes_missing_from_openapi(app: FastAPI) -> list[str]:
    """app 上有、`openapi.json` 卻沒列的路由（例如 `include_in_schema=False`）：會躲過換人測試。"""
    documented = endpoints_in_openapi(app.openapi())
    missing: list[str] = []
    # iter_route_contexts 把 include_router 裝進來的路由攤開（app.routes 只看得到 router 物件）。
    for context in iter_route_contexts(app.routes):
        path = context.path_format
        if not isinstance(context.original_route, APIRoute):
            if path not in FRAMEWORK_PATHS:
                missing.append(f"不是一般 API 路由、也不在 openapi.json：{path}")
            continue
        for method in sorted(context.methods or ()):
            endpoint = Endpoint(method, str(path))
            if endpoint not in documented and endpoint not in UNDOCUMENTED_ALLOWED:
                missing.append(f"沒有列進 openapi.json：{endpoint}")
    return missing


def assert_hidden_from_other_member(response: Response) -> None:
    """換人測試的預期：404（RFC 9457、代碼 not_found）或空清單（`items == []`）。

    Raises:
        AssertionError: 其他結果（含 403：回 403 等於告訴對方資料存在，ADR-0002）。
    """
    if response.status_code == 404:
        body = response.json()
        assert body.get("code") == NOT_FOUND_CODE, f"404 但代碼不是 not_found：{body}"
        return
    assert response.status_code == 200, (
        f"成員 B 看別人的資料要得到 404 或空清單，實際是 {response.status_code}：{response.text}"
    )
    body = response.json()
    assert isinstance(body, dict), f"200 但不是清單的回應：{response.text}"
    assert body.get("items") == [], f"成員 B 拿到了成員 A 的資料：{response.text}"
    # 清單是空的、總數卻不是 0：等於透露了 A 的資料筆數。
    assert body.get("total") in (None, 0), f"成員 B 看得到成員 A 的資料筆數：{response.text}"


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


def response_texts(response: Response) -> list[str]:
    """回應裡要檢查的文字：本文與每個標頭的名稱和值。"""
    return [response.text, *(f"{name}: {value}" for name, value in response.headers.items())]


def secrets_found(texts: Iterable[str], secrets: Iterable[str]) -> list[str]:
    """`texts` 裡出現了哪些秘密字串（金絲雀、別人的 token）；空清單表示都沒出現。"""
    joined = "\n".join(texts)
    return [secret for secret in secrets if secret in joined]
