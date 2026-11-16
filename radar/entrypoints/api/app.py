"""FastAPI 應用程式：健康檢查、request ID 與安全標頭中介層、錯誤格式、成員版前端（頁面殼）。"""

import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from radar.domain.errors import ErrorCode
from radar.entrypoints.api.problems import (
    PROBLEM_RESPONSES,
    install_problem_handlers,
    problem_openapi,
    problem_response,
)
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


def _operation_id(route: APIRoute) -> str:
    return route.name


def create_app(readiness_check: Callable[[], bool], *, web_dist: Path | None = None) -> FastAPI:
    """建立應用程式。`readiness_check` 回 True 表示可以接請求（由入口組裝好傳進來）。

    `web_dist` 有給時，在 API 路由之後掛上前端建置結果（`mount_web`）。
    """
    # 每個端點在 OpenAPI 裡都有 RFC 9457 的錯誤回應；錯誤一律經 problems.py 組成同一種格式
    # （ADR-0047）。
    # operationId 用路由函式的名稱（me、logout…）：orval 產生的前端函式名稱就是它，比預設的
    # 「名稱＋路徑＋方法」好讀；名稱要全站唯一（測試檢查）。
    app = FastAPI(
        title="Research Radar",
        responses=PROBLEM_RESPONSES,
        generate_unique_id_function=_operation_id,
    )
    install_problem_handlers(app)
    app.openapi = problem_openapi(app)  # type: ignore[method-assign]
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
                # 自己回 500（RFC 9457，只有代碼、不帶例外訊息），回應才帶得到 request ID；
                # 例外已經連同 traceback 記進上面那筆日誌。
                response = problem_response(ErrorCode.INTERNAL_ERROR)
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

    # 503 是健康狀態（compose 與冒煙測試讀它），不是 RFC 9457 的錯誤回應（ADR-0047）。
    @app.get(
        "/readyz",
        responses={
            503: {
                "description": "還沒準備好",
                "content": {"application/json": {"example": {"status": "not_ready"}}},
            }
        },
    )
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
