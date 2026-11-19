"""輸出成員版 API 的 `openapi.json`（AD-21、NFR-12、ADR-0047）。

`python -m radar.entrypoints.api.openapi` 把文件印到標準輸出；`radar export-schema` 用子行程執行它
再寫檔（ADR-0001：api、cli、worker 三個入口互相不 import）。

- 路由用和正式組裝同一個 `install_api_routes` 裝，所以端點清單和正式的 api 一樣。
- 只在開了假登入時才有的路由也列出來（這裡把假登入打開）：`openapi.json` 是全部端點的清單，
  換人測試的登記表拿它比對，不能因為環境設定漏掉端點。
- 不連資料庫、不讀設定：登入、論文頁與文獻庫路由的函式換成不會被呼叫的替身（只用來產生文件）；
  顯示名稱是空的（回應的形狀和內容無關）。
- 輸出固定：縮排 2、中文不跳脫、結尾換行；同一份程式每次輸出一樣，CI 才能用 `git diff` 比對。
"""

import json
import sys
import uuid
from collections.abc import Sequence
from typing import NoReturn

from fastapi import FastAPI

from radar.domain.auth import AuthenticatedMember, FakeLoginRequest, MemberSummary, NewSession
from radar.domain.auth_settings import FakeLoginMode
from radar.domain.contracts import DisplayNames, build_client_config
from radar.domain.library import LibraryCursor, LibraryEntry, LibraryPage
from radar.domain.paper_page import PaperPage
from radar.domain.viewer import MemberViewer
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import AuthRoutes, FakeLoginRoutes
from radar.entrypoints.api.library import LibraryRoutes
from radar.entrypoints.api.main import install_api_routes
from radar.entrypoints.api.papers import PaperRoutes


def _not_for_requests(*_args: object) -> NoReturn:
    raise RuntimeError("這個 app 只用來產生 openapi.json，不接請求")


def _authenticate(_token: str | None) -> AuthenticatedMember | None:
    _not_for_requests()


def _log_out(_token: str) -> None:
    _not_for_requests()


def _list_members() -> Sequence[MemberSummary]:
    _not_for_requests()


def _log_in(_request: FakeLoginRequest) -> NewSession:
    _not_for_requests()


def _not_ready() -> bool:
    _not_for_requests()


def _load_paper(_viewer: MemberViewer, _paper_id: uuid.UUID) -> PaperPage | None:
    _not_for_requests()


def _list_library(_viewer: MemberViewer, _after: LibraryCursor | None) -> LibraryPage:
    _not_for_requests()


def _change_library(_actor: MemberViewer, _paper_id: uuid.UUID) -> LibraryEntry | None:
    _not_for_requests()


def _journal_key(_issn_l: str | None) -> str | None:
    _not_for_requests()


def build_schema_app() -> FastAPI:
    """組一個只用來產生文件的 app：路由和正式的一樣，而且假登入的路由也在。"""
    app = create_app(readiness_check=_not_ready)
    install_api_routes(
        app,
        auth_routes=AuthRoutes(
            authenticate=_authenticate,
            log_out=_log_out,
            allowed_origins=frozenset(),
            cookie_max_age=0,
            fake_login=FakeLoginRoutes(
                mode=FakeLoginMode.PICK_MEMBER, list_members=_list_members, log_in=_log_in
            ),
        ),
        paper_routes=PaperRoutes(
            load_paper=_load_paper,
            display_names=DisplayNames(journals=(), topics=(), methods=()),
            client_config=build_client_config(),
        ),
        library_routes=LibraryRoutes(
            list_page=_list_library,
            add=_change_library,
            remove=_change_library,
            restore=_change_library,
            journal_key=_journal_key,
        ),
    )
    return app


def render_openapi() -> str:
    """`openapi.json` 的內容。"""
    document = build_schema_app().openapi()
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    # 直接寫 UTF-8 位元組：不受執行環境的語系設定影響。
    sys.stdout.buffer.write(render_openapi().encode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
