"""api 的個人文獻庫路由：列出、加入、移出、復原（Story 3.16、ADR-0002、ADR-0047、ADR-0048）。

- 四個端點都要登入（沒登入 401 `not_authenticated`），回應都帶 `Cache-Control: no-store`；會改資料的
  三個（PUT、DELETE、POST）由 router 的相依項檢查 `Origin`（`check_origin`）。
- 會改資料的三個共用一個相依項（`acting_member`）認 session 的成員，並比對請求標頭
  `X-Radar-Expected-Member`（這個分頁以為的成員，ADR-0055）：有帶、和 session 的成員不同 → 409
  `member_changed`；值不是成員 ID（或重複出現）→ 400 `expected_member_invalid`；兩種都在呼叫
  services 之前擋下，什麼都不寫。沒帶標頭照常（權限只看 session；這個標頭防的是自己人的舊分頁，
  不是惡意請求）。
- `GET /api/v1/library`：自己的文獻庫的一頁（`items`、`next_cursor`、`total`）；別人讀到的是自己的
  （沒有就是空清單）。`cursor` 不是上一頁給的寫法 → 422 `request_invalid`。
- `PUT /api/v1/library/{paper_id}`：加入（`added_via = manual`）；已經在就回原本那一筆。
- `DELETE /api/v1/library/{paper_id}`：移出，回移出前的那一筆，但 `paper.in_my_library` 是假
  （ADR-0053 更新 2026-11-21；200，不是 204：換人測試的對照要有本文）。
- `POST /api/v1/library/{paper_id}/restore`：復原最近一次移出的那一筆；已經在文獻庫就回現在那一筆、
  不動封存。
- 看不到、不存在、被排除、別人的、路徑參數不是 UUID：一律丟同一個
  `ApiProblemError(not_found, headers=NO_STORE)`（ADR-0002，和論文頁同一個寫法）；路徑參數宣告成
  字串、自己轉 UUID。
- 這裡只做形狀轉換（論文列用 `domain/contracts.PaperRow`），不判斷看不看得到。
- 處理函式的名稱就是 operationId（`list_library`、`add_library_item`、`remove_library_item`、
  `restore_library_item`，全站唯一）。一定要在 `mount_web` 之前裝。
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from radar.domain.auth import AuthenticatedMember
from radar.domain.contracts import PaperRow, UtcDateTime
from radar.domain.domain_config import JournalsConfig
from radar.domain.errors import ErrorCode
from radar.domain.library import (
    AddedVia,
    LibraryCursor,
    LibraryEntry,
    LibraryPage,
    decode_library_cursor,
)
from radar.domain.paper_page import journal_key_for
from radar.domain.viewer import MemberViewer
from radar.entrypoints.api.auth import (
    API_V1_PREFIX,
    NO_STORE,
    AuthRoutes,
    current_member,
    origin_dependency,
)
from radar.entrypoints.api.problems import ApiProblemError
from radar.entrypoints.api.web import web_mounted
from radar.entrypoints.bootstrap import Database
from radar.services import library as library_service

type LibraryChange = Callable[[MemberViewer, uuid.UUID], LibraryEntry | None]

# 前端送加入、移出、復原時帶的「這個分頁以為的成員」（這一頁載入時 `/me` 回的 member_id；
# ADR-0055）。
EXPECTED_MEMBER_HEADER: Final = "X-Radar-Expected-Member"

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class LibraryRoutes:
    """文獻庫路由要用的函式（`build_library_routes` 組；測試換成假的函式）。

    `add`／`remove`／`restore`：成員對 `paper_id` 做這件事，回那一筆；看不到、不在、沒有封存回
    None。
    `journal_key`：ISSN-L → 設定檔的期刊代號（不是追蹤的期刊回 None）。
    """

    list_page: Callable[[MemberViewer, LibraryCursor | None], LibraryPage]
    add: LibraryChange
    remove: LibraryChange
    restore: LibraryChange
    journal_key: Callable[[str | None], str | None]


def build_library_routes(database: Database, journals: JournalsConfig) -> LibraryRoutes:
    """用資料庫與追蹤的期刊（ISSN-L → 期刊代號）組好文獻庫路由。"""

    def list_page(viewer: MemberViewer, after: LibraryCursor | None) -> LibraryPage:
        return library_service.list_library(viewer, database, after=after)

    def add(actor: MemberViewer, paper_id: uuid.UUID) -> LibraryEntry | None:
        return library_service.add_library_item(actor, database, paper_id)

    def remove(actor: MemberViewer, paper_id: uuid.UUID) -> LibraryEntry | None:
        return library_service.remove_library_item(actor, database, paper_id)

    def restore(actor: MemberViewer, paper_id: uuid.UUID) -> LibraryEntry | None:
        return library_service.restore_library_item(actor, database, paper_id)

    return LibraryRoutes(
        list_page=list_page,
        add=add,
        remove=remove,
        restore=restore,
        journal_key=partial(journal_key_for, journals),
    )


class LibraryItemResponse(BaseModel):
    """文獻庫的一筆：論文列、怎麼加入的、加入的時間（UTC）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    paper: PaperRow
    added_via: AddedVia
    added_at: UtcDateTime


class LibraryPageResponse(BaseModel):
    """文獻庫的一頁（照加入時間新到舊）：這一頁的筆、下一頁的位置（沒有下一頁是 null）、總篇數。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[LibraryItemResponse, ...]
    next_cursor: str | None
    total: int = Field(ge=0)


def library_item_response(
    entry: LibraryEntry, journal_key: Callable[[str | None], str | None]
) -> LibraryItemResponse:
    """services 的一筆 → 回應的形狀（只轉換）。"""
    return LibraryItemResponse(
        paper=PaperRow.from_facts(entry.paper, journal_key(entry.paper.journal_issn_l)),
        added_via=entry.item.added_via,
        added_at=entry.item.added_at,
    )


def _not_found() -> ApiProblemError:
    # 看不到、不存在、不在、壞 ID 都是這一個：狀態碼、本文、標頭相同。
    return ApiProblemError(ErrorCode.NOT_FOUND, headers=NO_STORE)


def _paper_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise _not_found() from None


def _expected_member(value: str) -> uuid.UUID | None:
    """標頭的值 → 成員 ID；只收標準寫法（36 字、有連字號，大小寫不拘），其他回 None。"""
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value.lower() else None


def check_expected_member(request: Request, member: AuthenticatedMember) -> None:
    """請求帶了 `X-Radar-Expected-Member` 時，要和這次 session 的成員是同一位（ADR-0055）。

    沒帶就不檢查（照常）。比對的是 session 認出的成員，不看請求裡別的欄位。

    Raises:
        ApiProblemError: `expected_member_invalid`（值不是成員 ID，或標頭出現不只一次）、
            `member_changed`（和 session 的成員不同）；都帶 `no-store`。
    """
    values = request.headers.getlist(EXPECTED_MEMBER_HEADER)
    if not values:
        return
    expected = _expected_member(values[0]) if len(values) == 1 else None
    if expected is None:
        log.warning(
            "文獻庫寫入的預期成員標頭格式不對，拒絕",
            method=request.method,
            path=request.url.path,
            header_count=len(values),
        )
        raise ApiProblemError(ErrorCode.EXPECTED_MEMBER_INVALID, headers=NO_STORE)
    if expected != member.member_id:
        # 不記成員 ID：兩位成員的 ID 一起進日誌沒有必要（只要知道擋過）。
        log.info("文獻庫寫入的預期成員和 session 不同（換了人），不寫", method=request.method)
        raise ApiProblemError(ErrorCode.MEMBER_CHANGED, headers=NO_STORE)


def install_library_routes(app: FastAPI, routes: LibraryRoutes, auth_routes: AuthRoutes) -> None:
    """把文獻庫路由裝到 `app` 上（登入與 `Origin` 檢查用 `auth_routes`）。一定要在 `mount_web`
    之前呼叫。

    Raises:
        RuntimeError: 頁面殼已經掛上（之後才加的路由會被它蓋掉）。
    """
    if web_mounted(app):
        raise RuntimeError("頁面殼已經掛上：API 路由要在 mount_web 之前註冊")

    router = APIRouter(prefix=API_V1_PREFIX, dependencies=[Depends(origin_dependency(auth_routes))])

    def acting_member(request: Request) -> AuthenticatedMember:
        # 三個寫入共用：先認 session（沒登入 401），再比對預期成員（409／400）；都在呼叫 services
        # 之前。
        member = current_member(request, auth_routes)
        check_expected_member(request, member)
        return member

    def change(
        member: AuthenticatedMember, response: Response, paper_id: str, run: LibraryChange
    ) -> LibraryItemResponse:
        entry = run(member.viewer, _paper_id(paper_id))
        if entry is None:
            raise _not_found()
        response.headers.update(NO_STORE)
        return library_item_response(entry, routes.journal_key)

    @router.get("/library")
    def list_library(
        request: Request, response: Response, cursor: str | None = None
    ) -> LibraryPageResponse:
        """自己的文獻庫的一頁（一頁的篇數見 `client-config`）；`cursor` 是上一頁的 `next_cursor`，
        格式不對 422，沒登入 401。"""
        member = current_member(request, auth_routes)
        after = None
        if cursor is not None:
            try:
                after = decode_library_cursor(cursor)
            except ValueError:
                raise ApiProblemError(ErrorCode.REQUEST_INVALID, headers=NO_STORE) from None
        page = routes.list_page(member.viewer, after)
        response.headers.update(NO_STORE)
        return LibraryPageResponse(
            items=tuple(library_item_response(entry, routes.journal_key) for entry in page.entries),
            next_cursor=page.next_cursor,
            total=page.total,
        )

    @router.put("/library/{paper_id}")
    def add_library_item(
        paper_id: str,
        member: Annotated[AuthenticatedMember, Depends(acting_member)],
        response: Response,
    ) -> LibraryItemResponse:
        """把一篇看得到的論文加進自己的文獻庫；已經在就回原本那一筆。看不到、不存在、ID 不是 UUID
        都回同一個 404，沒登入 401；`Origin` 缺少或不是本站 403 `origin_rejected`；
        `X-Radar-Expected-Member` 和登入的成員不同 409 `member_changed`、格式不對 400
        `expected_member_invalid`。"""
        return change(member, response, paper_id, routes.add)

    @router.delete("/library/{paper_id}")
    def remove_library_item(
        paper_id: str,
        member: Annotated[AuthenticatedMember, Depends(acting_member)],
        response: Response,
    ) -> LibraryItemResponse:
        """把一篇移出自己的文獻庫，回移出前的那一筆（`paper.in_my_library` 是假）；不在自己的文獻庫
        回 404，沒登入 401；`Origin` 缺少或不是本站 403 `origin_rejected`；
        `X-Radar-Expected-Member` 和登入的成員不同 409 `member_changed`、格式不對 400
        `expected_member_invalid`。"""
        return change(member, response, paper_id, routes.remove)

    @router.post("/library/{paper_id}/restore")
    def restore_library_item(
        paper_id: str,
        member: Annotated[AuthenticatedMember, Depends(acting_member)],
        response: Response,
    ) -> LibraryItemResponse:
        """復原最近一次移出的那一篇（原本的加入方式與時間）；已經在文獻庫就回現在那一筆，不動任何
        封存。自己沒有移出過回 404，沒登入 401；`Origin` 缺少或不是本站 403 `origin_rejected`；
        `X-Radar-Expected-Member` 和登入的成員不同 409 `member_changed`、格式不對 400
        `expected_member_invalid`。"""
        return change(member, response, paper_id, routes.restore)

    app.include_router(router)
