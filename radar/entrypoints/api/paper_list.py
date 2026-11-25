"""api 的找論文清單路由（Story 3.13、AD-19、ADR-0002、ADR-0047）。

- `GET /api/v1/papers`：依期刊、年份、主題、方法、有沒有摘要、本週新增、範圍列出論文的一頁
  （`items`、`next_cursor`、`total`、`snapshot`）。要登入（沒登入 401 `not_authenticated`），回應帶
  `Cache-Control: no-store`。每一列的 `in_my_library` 與範圍「我的文獻庫」是成員專屬的：認完 session
  再比對 `X-Radar-Expected-Member`（`auth.member_as_expected`，不符 409、格式不對 400、沒帶照常，
  ADR-0055）。
- 不認得的期刊、主題、方法代碼，年份超出範圍，任一個多值參數超過 50 個值，`cursor` 不是上一頁給的
  寫法 → 422 `request_invalid`（`ApiProblemError`，帶 `no-store`）；型別不對（`year=abc`、不認得的
  `scope`）由框架擋下，一樣是 422 `request_invalid`。
- 這裡只做形狀轉換（論文列用 `domain/contracts.PaperRow`），條件的驗證在
  `domain/paper_list.parse_paper_list_filters`，看不看得到在 repo 的查詢裡。
- `GET /api/v1/cube`：計數方塊（Story 3.15、ADR-0062）：`total` 與 `with_abstract` 的計數、
  `params`、`snapshot`，只有計數、不給比例（AD-19）。要登入（沒登入 401），帶 `no-store`；內容是
  實驗室看得到的算數論文的篇數，每位成員看到的一樣，所以不比對 `X-Radar-Expected-Member`。和論文
  清單裝在一起：計數方塊的每一格都要能用論文清單查出相同篇數（AD-19「點得回去」）。
- 處理函式的名稱就是 operationId（`list_papers`、`cube`，全站唯一）。一定要在 `mount_web` 之前裝。
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from radar.domain.contracts import CubeResponse, PaperRow, SnapshotRef, TrendParams
from radar.domain.domain_config import JournalsConfig
from radar.domain.errors import ErrorCode
from radar.domain.paper_list import (
    PaperListCursor,
    PaperListFilters,
    PaperListPage,
    PaperListQuery,
    PaperListScope,
    decode_paper_list_cursor,
    parse_paper_list_filters,
)
from radar.domain.paper_page import journal_key_for
from radar.domain.taxonomy import Taxonomy
from radar.domain.viewer import MemberViewer
from radar.entrypoints.api.auth import (
    API_V1_PREFIX,
    NO_STORE,
    AuthRoutes,
    current_member,
    member_as_expected,
    origin_dependency,
)
from radar.entrypoints.api.problems import ApiProblemError
from radar.entrypoints.api.web import web_mounted
from radar.entrypoints.bootstrap import Database
from radar.services import cube as cube_service
from radar.services import paper_list as paper_list_service


def _cube_not_configured(_viewer: MemberViewer) -> CubeResponse:
    raise RuntimeError("計數方塊的讀取函式沒有組進路由")


@dataclass(frozen=True)
class PaperListRoutes:
    """找論文清單路由要用的函式（`build_paper_list_routes` 組；測試換成假的函式）。

    `list_page`：成員在這些條件下、從這個位置之後的一頁。
    `parse_filters`：API 收到的條件 → 驗證過的條件；不合法丟 `InvalidPaperListQueryError`。
    `journal_key`：ISSN-L → 設定檔的期刊代號（不是追蹤的期刊回 None）。
    `load_cube`：成員看得到的計數方塊（Story 3.15）。沒給時呼叫會丟 `RuntimeError`（只測清單的
    測試不用組它；正式組裝一律由 `build_paper_list_routes` 給）。
    """

    list_page: Callable[[MemberViewer, PaperListFilters, PaperListCursor | None], PaperListPage]
    parse_filters: Callable[[PaperListQuery], PaperListFilters]
    journal_key: Callable[[str | None], str | None]
    load_cube: Callable[[MemberViewer], CubeResponse] = field(default=_cube_not_configured)


def build_paper_list_routes(
    database: Database, journals: JournalsConfig, taxonomy: Taxonomy, params: TrendParams
) -> PaperListRoutes:
    """用資料庫、追蹤的期刊（代號 ↔ ISSN-L、追蹤起始年）、分類表（主題、方法代碼）與計數方塊的
    `params`（`contracts.build_trend_params` 從設定組好）組好路由。"""

    def load_cube(viewer: MemberViewer) -> CubeResponse:
        return cube_service.get_cube(viewer, database, journals=journals, params=params)

    def list_page(
        viewer: MemberViewer, filters: PaperListFilters, after: PaperListCursor | None
    ) -> PaperListPage:
        return paper_list_service.list_papers(viewer, database, filters, after=after)

    return PaperListRoutes(
        list_page=list_page,
        parse_filters=partial(parse_paper_list_filters, journals=journals, taxonomy=taxonomy),
        journal_key=partial(journal_key_for, journals),
        load_cube=load_cube,
    )


class PaperListResponse(BaseModel):
    """找論文清單的一頁（照出版年新到舊，沒有出版年的在最後）：這一頁的論文列、下一頁的位置（沒有
    下一頁是 null）、符合條件的總篇數、目前已發佈的快照（一個都還沒發佈時是 null）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[PaperRow, ...]
    next_cursor: str | None
    total: int = Field(ge=0)
    snapshot: SnapshotRef | None


def paper_list_response(
    page: PaperListPage, journal_key: Callable[[str | None], str | None]
) -> PaperListResponse:
    """services 的一頁 → 回應的形狀（只轉換）。"""
    return PaperListResponse(
        items=tuple(PaperRow.from_facts(row, journal_key(row.journal_issn_l)) for row in page.rows),
        next_cursor=page.next_cursor,
        total=page.total,
        snapshot=None if page.snapshot is None else SnapshotRef.from_snapshot(page.snapshot),
    )


def _invalid() -> ApiProblemError:
    return ApiProblemError(ErrorCode.REQUEST_INVALID, headers=NO_STORE)


def install_paper_list_routes(
    app: FastAPI, routes: PaperListRoutes, auth_routes: AuthRoutes
) -> None:
    """把找論文清單路由裝到 `app` 上（登入與 `Origin` 檢查用 `auth_routes`）。一定要在 `mount_web`
    之前呼叫。

    Raises:
        RuntimeError: 頁面殼已經掛上（之後才加的路由會被它蓋掉）。
    """
    if web_mounted(app):
        raise RuntimeError("頁面殼已經掛上：API 路由要在 mount_web 之前註冊")

    router = APIRouter(prefix=API_V1_PREFIX, dependencies=[Depends(origin_dependency(auth_routes))])

    @router.get("/papers")
    def list_papers(
        request: Request,
        response: Response,
        journal: Annotated[list[str], Query(default_factory=list)],
        year: Annotated[list[int], Query(default_factory=list)],
        topic: Annotated[list[str], Query(default_factory=list)],
        method: Annotated[list[str], Query(default_factory=list)],
        has_abstract: bool | None = None,
        this_week: bool = False,
        scope: PaperListScope = PaperListScope.TRACKED,
        cursor: str | None = None,
    ) -> PaperListResponse:
        """依條件列出論文的一頁（一頁的篇數見 `client-config`）。期刊、年份給多個值是「或」，主題、
        方法給多個值是「且」，不同欄位之間是「且」；`cursor` 是上一頁的 `next_cursor`。條件不合法
        422，沒登入 401；`X-Radar-Expected-Member` 和登入的成員不同 409 `member_changed`、格式
        不對 400 `expected_member_invalid`（每一列含 `in_my_library`，ADR-0055）。"""
        member = member_as_expected(request, auth_routes)
        query = PaperListQuery(
            journals=tuple(journal),
            years=tuple(year),
            topics=tuple(topic),
            methods=tuple(method),
            has_abstract=has_abstract,
            this_week=this_week,
            scope=scope,
        )
        try:
            filters = routes.parse_filters(query)
            after = None if cursor is None else decode_paper_list_cursor(cursor)
        except ValueError:  # 含 InvalidPaperListQueryError（條件）與壞的 cursor
            raise _invalid() from None
        page = routes.list_page(member.viewer, filters, after)
        response.headers.update(NO_STORE)
        return paper_list_response(page, routes.journal_key)

    @router.get("/cube")
    def cube(request: Request, response: Response) -> CubeResponse:
        """計數方塊：每本追蹤期刊每一年算數的論文篇數（`total`）與其中有摘要的篇數
        （`with_abstract`），加上規則用到的設定值（`params`）與目前已發佈的快照（還沒有是 null）。
        只有計數，比例由前端算。沒登入 401。"""
        member = current_member(request, auth_routes)
        found = routes.load_cube(member.viewer)
        response.headers.update(NO_STORE)
        return found

    app.include_router(router)
