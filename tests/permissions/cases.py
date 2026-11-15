"""每個 API 端點的登記：成員 B 怎麼呼叫它，以及（如果豁免換人測試）為什麼（Story 3.4、ADR-0047）。

**新增端點的 Story 要在這裡加一筆**，否則 `tests/unit/test_permission_coverage.py` 失敗：

- 會讀或改「某位成員的資料」的端點（例如 `GET /api/v1/library-items`、帶出私人欄位的
  `GET /api/v1/papers/{paper_id}`）：不豁免。呼叫函式先替成員 A 建好資料（整合測試的
  `ctx.extras["database"]` 是 `Database`），再用 `ctx.as_other(...)` 以成員 B 的身分打 A 的資料；
  整合測試會檢查結果是 404 或空清單。能放文字的私人資料，請把 `CANARY` 放進去，金絲雀測試才查得到。
- 不碰任何成員資料、或只回呼叫者自己的資料：寫 `exempt="理由"`；呼叫函式照樣要寫（金絲雀測試用）。
"""

from httpx2 import Response

from tests.permissions.framework import EndpointRegistry, PermissionContext

REGISTRY = EndpointRegistry()


@REGISTRY.case("GET", "/healthz", exempt="不需要登入，只回行程活著；不讀任何成員資料")
def _healthz(ctx: PermissionContext) -> Response:
    return ctx.as_other("GET", "/healthz")


@REGISTRY.case("GET", "/readyz", exempt="不需要登入，只回資料庫連得上、遷移不落後；不讀成員資料")
def _readyz(ctx: PermissionContext) -> Response:
    return ctx.as_other("GET", "/readyz")


@REGISTRY.case(
    "GET", "/api/v1/me", exempt="只回呼叫者自己的成員 ID 與名稱，不收別人的 ID，沒有別人的資料可拿"
)
def _me(ctx: PermissionContext) -> Response:
    return ctx.as_other("GET", "/api/v1/me")


@REGISTRY.case(
    "POST",
    "/api/v1/auth/logout",
    exempt="只刪呼叫者自己 cookie 對到的 session，不收成員 ID 或 session ID",
)
def _logout(ctx: PermissionContext) -> Response:
    return ctx.as_other("POST", "/api/v1/auth/logout")


@REGISTRY.case(
    "GET",
    "/api/v1/auth/fake-login",
    exempt=(
        "假登入的選人清單：開發環境依設計列出全部測試成員的名稱（不含 email），試用環境是空清單，"
        "正式環境沒有這個端點（ADR-0046）"
    ),
)
def _fake_login_options(ctx: PermissionContext) -> Response:
    return ctx.as_other("GET", "/api/v1/auth/fake-login")


@REGISTRY.case(
    "POST",
    "/api/v1/auth/fake-login",
    exempt=(
        "登入本身：開發環境選人登入就是它的用途，試用環境只讓管理者登入，正式環境沒有這個端點"
        "（ADR-0046）；回應只有轉回路徑"
    ),
)
def _fake_login(ctx: PermissionContext) -> Response:
    return ctx.as_other("POST", "/api/v1/auth/fake-login", json={"member_id": ctx.other.member_id})
