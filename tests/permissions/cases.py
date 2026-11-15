"""每個 API 端點的登記：送哪一份請求，以及（如果豁免換人測試）為什麼（Story 3.4、ADR-0047）。

**新增端點的 Story 要在這裡加一筆**，否則 `tests/unit/test_permission_coverage.py` 失敗：

- 會讀或改「某位成員的資料」的端點（例如 `GET /api/v1/library-items`、帶出私人欄位的
  `GET /api/v1/papers/{paper_id}`）：不豁免。登記的函式先替成員 A 建好資料（整合測試的
  `ctx.extras["database"]` 是 `Database`），再回傳**一份**請求（`EndpointRequest`：路徑參數、查詢、
  本文）。框架用這一份請求，先以成員 B 的身分送（要得到 404 或空清單），再以成員 A 的身分送（對照，
  要拿得到），兩次都要打到登記的這個端點。能放文字的私人資料，請把 `CANARY` 放進去，金絲雀測試才
  查得到。
- 不碰任何成員資料、或只回呼叫者自己的資料：寫 `exempt="理由"` 與 `status=成員 B 打它的預期狀態碼`；
  登記的函式照樣要寫（金絲雀測試用，狀態碼不是預期的就失敗）。
"""

from tests.permissions.framework import EndpointRegistry, EndpointRequest, PermissionContext

REGISTRY = EndpointRegistry()


@REGISTRY.case("GET", "/healthz", exempt="不需要登入，只回行程活著；不讀任何成員資料", status=200)
def _healthz(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


@REGISTRY.case(
    "GET",
    "/readyz",
    exempt="不需要登入，只回資料庫連得上、遷移不落後；不讀成員資料",
    status=200,
)
def _readyz(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


@REGISTRY.case(
    "GET",
    "/api/v1/me",
    exempt="只回呼叫者自己的成員 ID 與名稱，不收別人的 ID，沒有別人的資料可拿",
    status=200,
)
def _me(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


@REGISTRY.case(
    "POST",
    "/api/v1/auth/logout",
    exempt="只刪呼叫者自己 cookie 對到的 session，不收成員 ID 或 session ID",
    status=204,
)
def _logout(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


@REGISTRY.case(
    "GET",
    "/api/v1/auth/fake-login",
    exempt=(
        "假登入的選人清單：開發環境依設計列出全部測試成員的名稱（不含 email），試用環境是空清單，"
        "正式環境沒有這個端點（ADR-0046）"
    ),
    status=200,
)
def _fake_login_options(_ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest()


@REGISTRY.case(
    "POST",
    "/api/v1/auth/fake-login",
    exempt=(
        "登入本身：開發環境選人登入就是它的用途，試用環境只讓管理者登入，正式環境沒有這個端點"
        "（ADR-0046）；回應只有轉回路徑"
    ),
    status=200,
)
def _fake_login(ctx: PermissionContext) -> EndpointRequest:
    return EndpointRequest(body={"member_id": ctx.other.member_id})
