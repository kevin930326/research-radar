"""全站安全標頭與成員版前端（頁面殼）的送出方式（AD-21、AD-22、ADR-0029）。

- 安全標頭只有一份來源：同一個資料夾的 `security_headers.json`；`web/vite.config.ts` 的
  `vite preview` 也讀這個檔，兩邊不會走樣。
- 前端建置結果（`web/dist`）裡真的存在的檔案照送；`/api/` 以外找不到的 GET／HEAD 一律回
  `index.html`（頁面殼），由瀏覽器裡的路由決定顯示哪一頁，固定網址才打得開。
- `/api/` 底下找不到的路徑，不管什麼方法（含 TRACE、CONNECT 與自訂方法）都回 404 JSON。
"""

import json
from functools import cache
from importlib import resources
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

SECURITY_HEADERS_FILE = "security_headers.json"
INDEX_FILE = "index.html"
# `/api/` 底下找不到的路徑維持 FastAPI 預設的 404 JSON，不回頁面殼。
API_PREFIX = "api"
# 「其餘路徑」不設方法清單（做法見 mount_web）：
# /api/ 底下一律 404（不因方法變成 405）；其他路徑只有 GET／HEAD 回頁面殼，其餘方法 405。
SHELL_METHODS = frozenset({"GET", "HEAD"})
# 頁面殼每次都要回伺服器確認：換版後才拿得到新的資源檔名。
SHELL_HEADERS = {"Cache-Control": "no-cache"}

log = structlog.get_logger(__name__)


def _read_headers_file() -> str:
    return (
        resources.files("radar.entrypoints.api")
        .joinpath(SECURITY_HEADERS_FILE)
        .read_text(encoding="utf-8")
    )


@cache
def security_headers() -> dict[str, str]:
    """讀全站安全標頭；格式不對（不是「字串對字串」）就丟例外，api 不帶著壞掉的標頭啟動。"""
    loaded = json.loads(_read_headers_file())
    if not isinstance(loaded, dict) or not loaded:
        raise ValueError(f"{SECURITY_HEADERS_FILE} 必須是非空的物件")
    headers: dict[str, str] = {}
    for name, value in loaded.items():
        if not isinstance(name, str) or not isinstance(value, str) or not value:
            raise ValueError(f"{SECURITY_HEADERS_FILE} 的每一項都必須是非空字串：{name!r}")
        headers[name] = value
    return headers


def apply_security_headers(response: Response) -> None:
    """把整組安全標頭加到回應上（所有回應都加，含 404、500 與 /healthz）。

    AD-24 的例外掛點：之後「開啟自己的 PDF」（`GET /api/v1/pdf-files/<id>/content`）有自己
    固定的一組標頭（ADR-0013），寫在那個路由自己的一個地方；做那個 Story 時在這裡認得該路由、
    不覆寫它的標頭。現在還沒有那個路由，所以不先做。
    """
    for name, value in security_headers().items():
        response.headers[name] = value


def _is_api_path(path: str) -> bool:
    return path == API_PREFIX or path.startswith(f"{API_PREFIX}/")


def _file_inside(root: Path, relative: str) -> Path | None:
    """`relative` 指到 `root` 裡的實體檔案就回傳它；跳出資料夾（`..`、符號連結）或不存在回 None。"""
    try:
        candidate = (root / relative).resolve()
    except OSError, ValueError:
        # 例如路徑裡有 NUL 字元。
        return None
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def mount_web(app: FastAPI, web_dist: Path) -> None:
    """把前端建置結果掛到 `app` 上；必須在所有 API 路由（含 /healthz、/readyz）註冊之後呼叫。

    `web_dist` 不存在、或頁面殼不是建置資料夾裡的實體檔案（例如符號連結指到外面）時，只記一筆
    警告、照常啟動：/healthz、/readyz 不受影響，非 `/api/` 路徑回 404。
    """
    root = web_dist.resolve()
    index = _file_inside(root, INDEX_FILE)
    if index is None:
        log.warning(
            "找不到前端建置結果（或頁面殼不在建置資料夾裡），只提供 API", web_dist=str(web_dist)
        )
        return

    # 用 Starlette 的 Route（不是 FastAPI 的 api_route），端點又是類別：Starlette 對函式端點
    # 預設只收 GET，類別（ASGI 程式）端點不給 methods 才不受方法清單限制。
    app.router.routes.append(Route("/{path:path}", _WebShell(root, index), include_in_schema=False))


class _WebShell:
    """「其餘路徑」的 ASGI 端點：建置資料夾裡的檔案照送；其餘非 `/api/` 的路徑回頁面殼。"""

    def __init__(self, root: Path, index: Path) -> None:
        self._root = root
        self._index = index

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        # 查檔案會碰檔案系統，放到執行緒池，不卡住事件迴圈。
        response = await run_in_threadpool(self._respond, request)
        await response(scope, receive, send)

    def _respond(self, request: Request) -> Response:
        path: str = request.path_params["path"]
        if _is_api_path(path):
            raise HTTPException(status_code=404)
        if request.method not in SHELL_METHODS:
            raise HTTPException(status_code=405, headers={"Allow": "GET, HEAD"})
        found = _file_inside(self._root, path) if path else None
        if found is not None and found != self._index:
            return FileResponse(found)
        # 直接要 /index.html 也走這裡，同樣帶 no-cache。
        return FileResponse(self._index, headers=SHELL_HEADERS)
