"""FastAPI 應用程式：健康檢查、request ID 與安全標頭中介層、成員版前端（頁面殼）。"""

import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from radar.entrypoints.api.web import apply_security_headers, mount_web, security_headers

REQUEST_ID_HEADER = "X-Request-ID"
# 外部帶進來的 request ID 當使用者輸入驗證：只收這個格式，其他一律換新的。
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")

log = structlog.get_logger(__name__)


def resolve_request_id(candidate: str | None) -> str:
    """合法就沿用，不合法（缺少、過長、含其他字元）就產生新的。"""
    if candidate is not None and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


def create_app(readiness_check: Callable[[], bool], *, web_dist: Path | None = None) -> FastAPI:
    """建立應用程式。`readiness_check` 回 True 表示可以接請求（由入口組裝好傳進來）。

    `web_dist` 有給時，在 API 路由之後掛上前端建置結果（`mount_web`）。
    """
    app = FastAPI(title="Research Radar")
    # 啟動時就讀一次：安全標頭檔壞掉時 api 不啟動，而不是等第一個請求才出錯。
    security_headers()

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        started = time.perf_counter()
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                response = await call_next(request)
            except Exception:
                log.exception(
                    "請求失敗",
                    method=request.method,
                    path=request.url.path,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
                # 自己回 500（內容和 Starlette 預設相同），回應才帶得到 request ID；
                # 例外已經連同 traceback 記進上面那筆日誌。
                response = PlainTextResponse("Internal Server Error", status_code=500)
                response.headers[REQUEST_ID_HEADER] = request_id
                apply_security_headers(response)
                return response
            response.headers[REQUEST_ID_HEADER] = request_id
            # 全站安全標頭（AD-22）：所有回應都帶，含 404 與上面自己組的 500。
            apply_security_headers(response)
            # 只記路徑，不記查詢字串（可能含使用者輸入）。
            log.info(
                "請求完成",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """行程活著就回 200；不碰資料庫。"""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """資料庫連得上、遷移版本不落後回 200，否則 503（AD-15）。

        同步函式，FastAPI 會放到執行緒池跑。
        """
        if readiness_check():
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not_ready"}, status_code=503)

    # FastAPI 的 GET 路由不會自動收 HEAD；另外登記 HEAD（同一個函式，狀態碼與標頭和 GET 一樣，
    # 伺服器不送 body），不放進 API 文件。
    app.add_api_route("/healthz", healthz, methods=["HEAD"], include_in_schema=False)
    app.add_api_route("/readyz", readyz, methods=["HEAD"], include_in_schema=False)

    # 前端的「其餘路徑回頁面殼」一定要排在所有 API 路由後面。
    if web_dist is not None:
        mount_web(app, web_dist)
    return app
