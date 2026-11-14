"""全站安全標頭與成員版前端（頁面殼）的送出方式（AD-21、AD-22、ADR-0029）。

- 安全標頭只有一份來源：同一個資料夾的 `security_headers.json`；`web/vite.config.ts` 的
  `vite preview` 也讀這個檔，兩邊不會走樣。
- 前端建置結果（`web/dist`）裡真的存在的檔案照送；`/api/` 以外找不到的 GET／HEAD 一律回
  `index.html`（頁面殼），由瀏覽器裡的路由決定顯示哪一頁，固定網址才打得開。
"""

import json
from functools import cache
from importlib import resources
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

SECURITY_HEADERS_FILE = "security_headers.json"
INDEX_FILE = "index.html"
# `/api/` 底下找不到的路徑維持 FastAPI 預設的 404 JSON，不回頁面殼。
API_PREFIX = "api"
# 「其餘路徑」接所有方法：/api/ 底下一律 404（不因方法變成 405）；
# 其他路徑只有 GET／HEAD 回頁面殼，其餘方法 405。
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
SHELL_METHODS = frozenset({"GET", "HEAD"})

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

    `web_dist` 不存在時只記一筆警告、照常啟動：/healthz、/readyz 不受影響，非 `/api/` 路徑回 404。
    """
    root = web_dist.resolve()
    index = root / INDEX_FILE
    if not index.is_file():
        log.warning("找不到前端建置結果，只提供 API", web_dist=str(web_dist))
        return

    @app.api_route("/{path:path}", methods=ALL_METHODS, include_in_schema=False)
    def web_shell(path: str, request: Request) -> Response:
        """建置資料夾裡的檔案照送；其餘非 `/api/` 的路徑回頁面殼。"""
        if _is_api_path(path):
            raise HTTPException(status_code=404)
        if request.method not in SHELL_METHODS:
            raise HTTPException(status_code=405, headers={"Allow": "GET, HEAD"})
        found = _file_inside(root, path) if path else None
        if found is not None:
            return FileResponse(found)
        # 頁面殼每次都要回伺服器確認：換版後才拿得到新的資源檔名。
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
