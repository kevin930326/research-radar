"""api 的錯誤回應：RFC 9457（problem details），代碼在 `domain/errors.py`（Conventions「錯誤」）。

- 這個模組只做一件事：把代碼對應成 HTTP 狀態（`PROBLEM_STATUS`），並把各種錯誤組成同一種回應。
- 回應是 `application/problem+json`，只有四個欄位：`type`（固定 `about:blank`）、`title`（HTTP
  狀態的標準英文片語）、`status`、`code`。後端不回給人看的句子：不回 `detail`、例外訊息、驗證錯誤的
  輸入值；畫面上的字由前端 i18n 的 `errors.<code>` 提供（ADR-0047）。
- 路由要回錯誤時丟 `ApiProblemError(代碼)`；框架自己丟的 `HTTPException`（找不到路由、方法不對）與
  驗證錯誤由這裡的處理器換成同一種格式。未預期的例外由 `app.py` 的中介層回 `internal_error`。
- 例外：`/readyz` 的 503 是健康狀態（`{"status": "not_ready"}`），不是錯誤回應，不走這裡。
"""

from collections.abc import Callable, Mapping
from http import HTTPStatus
from types import MappingProxyType
from typing import Any, Final, Literal

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from radar.domain.errors import ErrorCode

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
# RFC 9457 §4.2.1：`about:blank` 表示「除了 HTTP 狀態本身，沒有更多語意」，`title` 用狀態的標準
# 片語；細分的原因放在擴充欄位 `code`。
PROBLEM_TYPE: Final = "about:blank"

# api 會回的代碼 → HTTP 狀態。只有這張表決定狀態；不在表裡的代碼不能當 api 的錯誤回應。
PROBLEM_STATUS: Final[Mapping[ErrorCode, HTTPStatus]] = MappingProxyType(
    {
        ErrorCode.REQUEST_INVALID: HTTPStatus.UNPROCESSABLE_CONTENT,
        ErrorCode.REQUEST_REJECTED: HTTPStatus.BAD_REQUEST,
        ErrorCode.NOT_AUTHENTICATED: HTTPStatus.UNAUTHORIZED,
        ErrorCode.ORIGIN_REJECTED: HTTPStatus.FORBIDDEN,
        ErrorCode.LOGIN_REJECTED: HTTPStatus.FORBIDDEN,
        ErrorCode.ADMIN_MEMBER_MISSING: HTTPStatus.FORBIDDEN,
        ErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
        ErrorCode.METHOD_NOT_ALLOWED: HTTPStatus.METHOD_NOT_ALLOWED,
        ErrorCode.INTERNAL_ERROR: HTTPStatus.INTERNAL_SERVER_ERROR,
    }
)

# 框架丟的 HTTPException：這幾個狀態有專屬代碼；其他 4xx 是 request_rejected、5xx 是
# internal_error。
_FRAMEWORK_CODES: Final[Mapping[int, ErrorCode]] = MappingProxyType(
    {
        HTTPStatus.UNAUTHORIZED: ErrorCode.NOT_AUTHENTICATED,
        HTTPStatus.NOT_FOUND: ErrorCode.NOT_FOUND,
        HTTPStatus.METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
        HTTPStatus.UNPROCESSABLE_CONTENT: ErrorCode.REQUEST_INVALID,
    }
)

# 錯誤回應的 `code` 只會是這幾個（= `PROBLEM_STATUS` 的鍵，單元測試逐一核對）。寫成固定的 Literal：
# OpenAPI 裡是一個列舉，前端型別由它產生；不用整個 ErrorCode，另一條線加的 LLM 代碼不會讓
# openapi.json 變動。
ProblemCode = Literal[
    "admin_member_missing",
    "internal_error",
    "login_rejected",
    "method_not_allowed",
    "not_authenticated",
    "not_found",
    "origin_rejected",
    "request_invalid",
    "request_rejected",
]

log = structlog.get_logger(__name__)


class ProblemDetails(BaseModel):
    """錯誤回應（RFC 9457）。`code` 只會是 api 會回的代碼（`PROBLEM_STATUS` 的鍵）。"""

    model_config = ConfigDict(extra="forbid")

    type: str
    title: str
    status: int
    code: ProblemCode


class ApiProblemError(Exception):
    """路由要回錯誤時丟這個；狀態由 `PROBLEM_STATUS` 決定，`headers` 照樣帶上。

    Raises:
        ValueError: 代碼不在 `PROBLEM_STATUS` 裡。
    """

    def __init__(self, code: ErrorCode, *, headers: Mapping[str, str] | None = None) -> None:
        if code not in PROBLEM_STATUS:
            raise ValueError(f"代碼 {code.value} 沒有對應的 HTTP 狀態（PROBLEM_STATUS）")
        super().__init__(code.value)
        self.code = code
        self.headers = dict(headers or {})


def problem_response(
    code: ErrorCode,
    *,
    status: int | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """組一個 RFC 9457 回應。狀態照 `PROBLEM_STATUS`；只有框架丟的 HTTPException 會傳 `status`
    保留原本的狀態碼（例如 413 還是 413，代碼是 request_rejected）。

    Raises:
        KeyError: 代碼不在 `PROBLEM_STATUS` 裡。
    """
    resolved = HTTPStatus(PROBLEM_STATUS[code]) if status is None else status
    # 用 model_validate：代碼不在 ProblemCode 裡時這裡就失敗，不會送出文件以外的代碼。
    body = ProblemDetails.model_validate(
        {"type": PROBLEM_TYPE, "title": _phrase(resolved), "status": int(resolved), "code": code}
    )
    return JSONResponse(
        body.model_dump(),
        status_code=int(resolved),
        headers=dict(headers or {}),
        media_type=PROBLEM_MEDIA_TYPE,
    )


def _phrase(status: int) -> str:
    """HTTP 狀態的標準英文片語；不是標準狀態碼時用 `Error`。"""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def code_for_status(status: int) -> ErrorCode:
    """框架丟的 HTTPException 用哪個代碼。"""
    if status in _FRAMEWORK_CODES:
        return _FRAMEWORK_CODES[status]
    if 400 <= status < 500:
        return ErrorCode.REQUEST_REJECTED
    return ErrorCode.INTERNAL_ERROR


async def _api_problem_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiProblemError)
    return problem_response(exc.code, headers=exc.headers)


async def _http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    # detail 不回給使用者（框架預設是英文片語）；只留標頭（例如 405 的 Allow）。
    return problem_response(
        code_for_status(exc.status_code), status=exc.status_code, headers=exc.headers
    )


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # 只記錯誤的種類：輸入值、pydantic 的訊息、欄位路徑都可能帶著使用者輸入，不進日誌、不回應。
    kinds = sorted({str(error.get("type", "")) for error in exc.errors()})
    log.warning("請求的格式不對", method=request.method, path=request.url.path, kinds=kinds)
    return problem_response(ErrorCode.REQUEST_INVALID)


def install_problem_handlers(app: FastAPI) -> None:
    """把錯誤處理器裝到 app 上：`ApiProblemError`、框架的 HTTPException、驗證錯誤。"""
    app.add_exception_handler(ApiProblemError, _api_problem_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)


# 每個端點在 OpenAPI 裡都有這個 `default` 回應（FastAPI(responses=...)）；有了它，FastAPI 就不再自動
# 加上它自己格式的 422（HTTPValidationError）。
PROBLEM_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    "default": {"model": ProblemDetails, "description": "錯誤（RFC 9457）"}
}


def problem_openapi(app: FastAPI) -> Callable[[], dict[str, Any]]:
    """包住 `app.openapi`：`default` 回應的內容型別改成 `application/problem+json`。

    FastAPI 會把 `responses` 的模型掛在路由自己的回應型別（`application/json`）底下；錯誤回應實際上
    是 `application/problem+json`，文件要和實際一致。
    """
    original = app.openapi

    def openapi() -> dict[str, Any]:
        # FastAPI 自己會快取、路由變了才重產；這裡每次都套一次（已經換過的不會再變，可以重複套）。
        schema = original()
        for path_item in schema.get("paths", {}).values():
            for operation in path_item.values():
                default = operation.get("responses", {}).get("default")
                if default is None:
                    continue
                content = default.get("content", {})
                media = content.pop("application/json", None)
                if media is not None:
                    content[PROBLEM_MEDIA_TYPE] = media
        return schema

    return openapi
