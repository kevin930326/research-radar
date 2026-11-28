"""api 的找論文清單路由（Story 3.13、AD-19、ADR-0002、ADR-0047）。

- `GET /api/v1/papers`：依期刊、年份、主題、方法、有沒有摘要、本週新增、範圍列出論文的一頁
  （`items`、`next_cursor`、`total`、`snapshot`）。要登入（沒登入 401 `not_authenticated`），回應帶
  `Cache-Control: no-store`。每一列的 `in_my_library` 與範圍「我的文獻庫」是成員專屬的：認完 session
  再比對 `X-Radar-Expected-Member`（`auth.member_as_expected`，不符 409、格式不對 400、沒帶照常，
  ADR-0055）。
- `topic_match`、`method_match`：主題、方法給多個值怎麼合（`any` 預設、`all`；Story 3.20）。和
  `scope` 一樣是單值參數：同一個參數給兩次時取最後一個（框架的行為），不算錯。
- 不認得的期刊、主題、方法代碼，年份超出範圍，任一個多值參數超過 50 個值，`cursor` 不是上一頁給的
  寫法 → 422 `request_invalid`（`ApiProblemError`，帶 `no-store`）；型別不對（`year=abc`、不認得的
  `scope`、`topic_match`／`method_match`）由框架擋下，一樣是 422 `request_invalid`。
- **搜尋與找相似論文**（Story 4.1 PR B、ADR-0050 的「2026-11 更新」）：`q`（一句話）或
  `similar_to`（論文 ID）有其中一個時改走 `services/retrieval.retrieve`，照相關度排；兩個都給 422
  `request_invalid`；都沒給時和上面完全一樣（同一段程式、同一句 SQL）。回應的形狀不變（`items`、
  `next_cursor`、`total`、`snapshot`）。篩選與範圍照常（`scope` 只有清單的三種，`demo` 不從這裡
  開放）。`next_cursor` 是融合後名次的位移（`domain/retrieval.RetrievalCursor`，寫明是搜尋還是找相似
  論文），和清單的 keyset 位置互相不收（422）。`q` 不收的字（空的、太長、控制字元等）422；起點論文
  不存在、看不到或被排除 404 `not_found`（同一個回應）；額度用完 503 `search_quota_exhausted`；
  Workers AI 連不上、逾時或 api 沒有 Cloudflare 金鑰 503 `search_embedding_unavailable`。這裡丟的
  錯誤都帶 `no-store`（框架擋下的型別錯誤是共用處理器回的，不帶，和清單一樣；ADR-0050 已知限制）；
  404、503 宣告在端點的 OpenAPI 回應上。查詢內容不寫進日誌（uvicorn 的 access log 關著、中介層只記
  路徑；`retrieve` 只記長度與有沒有中文）。
- 這裡只做形狀轉換（論文列用 `domain/contracts.PaperRow`），條件的驗證在
  `domain/paper_list.parse_paper_list_filters`，看不看得到在 repo 的查詢裡。
- `GET /api/v1/cube`：計數方塊（Story 3.15、ADR-0062）：`total` 與 `with_abstract` 的計數、
  `params`、`snapshot`，只有計數、不給比例（AD-19）。要登入（沒登入 401），帶 `no-store`；內容是
  實驗室看得到的算數論文的篇數，每位成員看到的一樣，所以不比對 `X-Radar-Expected-Member`。和論文
  清單裝在一起：計數方塊的每一格都要能用論文清單查出相同篇數（AD-19「點得回去」）。
- 處理函式的名稱就是 operationId（`list_papers`、`cube`，全站唯一）。一定要在 `mount_web` 之前裝。
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from radar.domain.contracts import CubeResponse, PaperRow, SnapshotRef, TrendParams
from radar.domain.domain_config import JournalsConfig
from radar.domain.embeddings import EmbeddingConfig
from radar.domain.errors import ErrorCode
from radar.domain.paper_list import (
    MatchMode,
    PaperListCursor,
    PaperListFilters,
    PaperListPage,
    PaperListQuery,
    PaperListScope,
    decode_paper_list_cursor,
    parse_paper_list_filters,
)
from radar.domain.paper_page import journal_key_for
from radar.domain.policy import CloudPolicy, CloudSafeText
from radar.domain.retrieval import (
    InvalidSearchQueryError,
    PaperSearchPage,
    RetrievalAnchorNotFoundError,
    RetrievalConfig,
    RetrievalCursor,
    RetrievalMode,
    RetrievalScope,
    SearchEmbeddingUnavailableError,
    SearchQuotaExhaustedError,
    clean_search_query,
    decode_retrieval_cursor,
    encode_retrieval_cursor,
)
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
from radar.entrypoints.api.problems import PROBLEM_MEDIA_TYPE, ApiProblemError, ProblemDetails
from radar.entrypoints.api.web import web_mounted
from radar.entrypoints.bootstrap import Database
from radar.ports import EmbeddingProvider
from radar.services import cube as cube_service
from radar.services import paper_list as paper_list_service
from radar.services import retrieval as retrieval_service
from radar.services.retrieval import MemberScope, RetrievalSetup

log = structlog.get_logger(__name__)

# 清單的範圍 → 同名的檢索範圍（清單沒有 `demo`，所以這個端點開放不了 `demo`）。
_RETRIEVAL_SCOPES: Final[Mapping[PaperListScope, MemberScope]] = {
    PaperListScope.PERSONAL: RetrievalScope.PERSONAL,
    PaperListScope.LAB: RetrievalScope.LAB,
    PaperListScope.TRACKED: RetrievalScope.TRACKED,
}

type SearchPage = Callable[
    [MemberViewer, PaperListFilters, str | None, uuid.UUID | None, int], PaperSearchPage
]


# 搜尋與找相似論文多出的兩種錯誤，在端點上宣告（PR B 審查 M9）：前端的型別與文件看得到；內容型別和
# 實際一樣是 `application/problem+json`（全站的 `default` 回應由 `problems.problem_openapi` 換）。
_PROBLEM_SCHEMA: Final = {"$ref": f"#/components/schemas/{ProblemDetails.__name__}"}
_SEARCH_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    404: {
        "description": "`similar_to` 的論文不存在、看不到或被排除（`not_found`）",
        "content": {PROBLEM_MEDIA_TYPE: {"schema": _PROBLEM_SCHEMA}},
    },
    503: {
        "description": (
            "搜尋的查詢沒能轉成向量：額度用完（`search_quota_exhausted`）或連不上"
            "（`search_embedding_unavailable`）"
        ),
        "content": {PROBLEM_MEDIA_TYPE: {"schema": _PROBLEM_SCHEMA}},
    },
}


def _cube_not_configured(_viewer: MemberViewer) -> CubeResponse:
    raise RuntimeError("計數方塊的讀取函式沒有組進路由")


def _search_not_configured(
    _viewer: MemberViewer,
    _filters: PaperListFilters,
    _query: str | None,
    _paper_id: uuid.UUID | None,
    _offset: int,
) -> PaperSearchPage:
    raise RuntimeError("搜尋與找相似論文的函式沒有組進路由")


@dataclass(frozen=True)
class PaperSearchSetup:
    """搜尋與找相似論文要的設定與依賴（api 啟動時組好）：查詢向量的模型、額度與時限
    （`embeddings.yaml`）、檢索的數字（`retrieval.yaml`）、送雲端的放行設定、時鐘，以及 Workers AI
    的 provider。api 行程沒有 Cloudflare 金鑰時 `provider` 是 None：搜尋回
    `search_embedding_unavailable`（不送、不記帳），找相似論文照常（不呼叫 provider）。"""

    embedding: EmbeddingConfig
    retrieval: RetrievalConfig
    cloud_policy: CloudPolicy
    provider: EmbeddingProvider | None
    clock: Callable[[], datetime]


class _ProviderNotConfigured:
    """api 沒有 Cloudflare 金鑰時放進 `RetrievalSetup` 的 provider：找相似論文用不到它；搜尋在
    呼叫 `retrieve` 之前就回 `search_embedding_unavailable`，走不到這裡（走到了就是程式錯，丟
    `RuntimeError`）。"""

    def __init__(self, model_id: str) -> None:
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        raise RuntimeError("api 沒有設定 Cloudflare 金鑰，不能向量化")

    def embed_query(self, text: CloudSafeText) -> list[float]:
        raise RuntimeError("api 沒有設定 Cloudflare 金鑰，不能向量化")


@dataclass(frozen=True)
class PaperListRoutes:
    """找論文清單路由要用的函式（`build_paper_list_routes` 組；測試換成假的函式）。

    `list_page`：成員在這些條件下、從這個位置之後的一頁。
    `parse_filters`：API 收到的條件 → 驗證過的條件；不合法丟 `InvalidPaperListQueryError`。
    `journal_key`：ISSN-L → 設定檔的期刊代號（不是追蹤的期刊回 None）。
    `load_cube`：成員看得到的計數方塊（Story 3.15）。沒給時呼叫會丟 `RuntimeError`（只測清單的
    測試不用組它；正式組裝一律由 `build_paper_list_routes` 給）。
    `search_page`：搜尋（`query`）或找相似論文（`paper_id`）從名次位移 `offset` 之後的一頁
    （Story 4.1 PR B）；丟的錯見 `install_paper_list_routes`。沒給時呼叫會丟 `RuntimeError`（同
    `load_cube`）。
    """

    list_page: Callable[[MemberViewer, PaperListFilters, PaperListCursor | None], PaperListPage]
    parse_filters: Callable[[PaperListQuery], PaperListFilters]
    journal_key: Callable[[str | None], str | None]
    load_cube: Callable[[MemberViewer], CubeResponse] = field(default=_cube_not_configured)
    search_page: SearchPage = field(default=_search_not_configured)


def build_paper_list_routes(
    database: Database,
    journals: JournalsConfig,
    taxonomy: Taxonomy,
    params: TrendParams,
    search: PaperSearchSetup,
) -> PaperListRoutes:
    """用資料庫、追蹤的期刊（代號 ↔ ISSN-L、追蹤起始年）、分類表（主題、方法代碼）、計數方塊的
    `params`（`contracts.build_trend_params` 從設定組好）與搜尋的設定組好路由。"""
    retrieval_setup = RetrievalSetup(
        embedding=search.embedding,
        retrieval=search.retrieval,
        cloud_policy=search.cloud_policy,
        provider=(
            search.provider
            if search.provider is not None
            else _ProviderNotConfigured(search.embedding.model_id)
        ),
        clock=search.clock,
    )

    def load_cube(viewer: MemberViewer) -> CubeResponse:
        return cube_service.get_cube(viewer, database, journals=journals, params=params)

    def list_page(
        viewer: MemberViewer, filters: PaperListFilters, after: PaperListCursor | None
    ) -> PaperListPage:
        return paper_list_service.list_papers(viewer, database, filters, after=after)

    def search_page(
        viewer: MemberViewer,
        filters: PaperListFilters,
        query: str | None,
        paper_id: uuid.UUID | None,
        offset: int,
    ) -> PaperSearchPage:
        if query is not None and search.provider is None:
            # 先照常驗查詢字串（不收的字一樣是 422），再回「連不上」：不送、不記帳。
            clean_search_query(query, max_length=search.retrieval.query_max_length)
            log.warning("api 沒有設定 Cloudflare 金鑰，搜尋回固定的錯誤")
            raise SearchEmbeddingUnavailableError()
        found = retrieval_service.retrieve(
            viewer,
            database,
            retrieval_setup,
            query=query,
            paper_id=paper_id,
            scope=_RETRIEVAL_SCOPES[filters.scope],
            filters=filters,
            k=search.retrieval.page_size,
            offset=offset,
        )
        rows = paper_list_service.list_ranked_papers(
            viewer, database, [hit.paper_id for hit in found.hits]
        )
        end = offset + len(found.hits)
        next_cursor = None
        if found.hits and end < found.total:
            mode = RetrievalMode.SEARCH if query is not None else RetrievalMode.SIMILAR
            next_cursor = encode_retrieval_cursor(RetrievalCursor(mode=mode, offset=end))
        # 快照用排名時讀到的那一個（`this_week` 的篩選與回應一致，審查 M3）。
        return PaperSearchPage(
            rows=rows, next_cursor=next_cursor, total=found.total, snapshot=found.snapshot
        )

    return PaperListRoutes(
        list_page=list_page,
        parse_filters=partial(parse_paper_list_filters, journals=journals, taxonomy=taxonomy),
        journal_key=partial(journal_key_for, journals),
        load_cube=load_cube,
        search_page=search_page,
    )


class PaperListResponse(BaseModel):
    """找論文清單的一頁（照出版年新到舊，沒有出版年的在最後；搜尋與找相似論文時照相關度）：這一頁的
    論文列、下一頁的位置（沒有下一頁是 null）、符合條件的總篇數（搜尋與找相似論文時是算得上結果的
    篇數）、目前已發佈的快照（一個都還沒發佈時是 null）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[PaperRow, ...]
    next_cursor: str | None
    total: int = Field(ge=0)
    snapshot: SnapshotRef | None


def paper_list_response(
    page: PaperListPage | PaperSearchPage, journal_key: Callable[[str | None], str | None]
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


def _problem(code: ErrorCode) -> ApiProblemError:
    return ApiProblemError(code, headers=NO_STORE)


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

    @router.get("/papers", responses=_SEARCH_RESPONSES)
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
        topic_match: MatchMode = MatchMode.ANY,
        method_match: MatchMode = MatchMode.ANY,
        cursor: str | None = None,
        q: str | None = None,
        similar_to: uuid.UUID | None = None,
    ) -> PaperListResponse:
        """依條件列出論文的一頁（一頁的篇數見 `client-config`）。期刊、年份給多個值是「或」；主題、
        方法給多個值預設是「或」，`topic_match`／`method_match` 是 `all` 時是「且」；不同欄位之間是
        「且」；`cursor` 是上一頁的 `next_cursor`。條件不合法
        422，沒登入 401；`X-Radar-Expected-Member` 和登入的成員不同 409 `member_changed`、格式
        不對 400 `expected_member_invalid`（每一列含 `in_my_library`，ADR-0055）。

        `q`（一句話搜尋）或 `similar_to`（找和這篇相似的論文，不含它自己）有其中一個時照相關度排，
        篩選與範圍照常；兩個都給 422。`q` 不收的字 422；`similar_to` 的論文不存在或看不到 404；
        Workers AI 今天的額度用完 503 `search_quota_exhausted`、連不上 503
        `search_embedding_unavailable`（都不降級成只用關鍵字）。"""
        member = member_as_expected(request, auth_routes)
        if q is not None and similar_to is not None:
            raise _invalid()
        query = PaperListQuery(
            journals=tuple(journal),
            years=tuple(year),
            topics=tuple(topic),
            methods=tuple(method),
            has_abstract=has_abstract,
            this_week=this_week,
            scope=scope,
            topic_match=topic_match,
            method_match=method_match,
        )
        if q is not None or similar_to is not None:
            found = _search(member.viewer, query, cursor, q, similar_to)
            response.headers.update(NO_STORE)
            return paper_list_response(found, routes.journal_key)
        try:
            filters = routes.parse_filters(query)
            after = None if cursor is None else decode_paper_list_cursor(cursor)
        except ValueError:  # 含 InvalidPaperListQueryError（條件）與壞的 cursor
            raise _invalid() from None
        page = routes.list_page(member.viewer, filters, after)
        response.headers.update(NO_STORE)
        return paper_list_response(page, routes.journal_key)

    def _search(
        viewer: MemberViewer,
        query: PaperListQuery,
        cursor: str | None,
        q: str | None,
        similar_to: uuid.UUID | None,
    ) -> PaperSearchPage:
        """搜尋或找相似論文的一頁；錯誤換成 api 的錯誤回應（見 `list_papers` 的說明）。"""
        mode = RetrievalMode.SEARCH if q is not None else RetrievalMode.SIMILAR
        try:
            filters = routes.parse_filters(query)
            # 清單的 keyset 位置、另一種檢索的位置都不收。
            offset = 0 if cursor is None else decode_retrieval_cursor(cursor, mode=mode).offset
        except ValueError:  # 含 InvalidPaperListQueryError（條件）與壞的 cursor
            raise _invalid() from None
        try:
            return routes.search_page(viewer, filters, q, similar_to, offset)
        except InvalidSearchQueryError:
            raise _invalid() from None
        except RetrievalAnchorNotFoundError:
            # 不存在、看不到、被排除：同一個 404（ADR-0002，不透露論文存在）。
            raise _problem(ErrorCode.NOT_FOUND) from None
        except SearchQuotaExhaustedError:
            raise _problem(ErrorCode.SEARCH_QUOTA_EXHAUSTED) from None
        except SearchEmbeddingUnavailableError:
            raise _problem(ErrorCode.SEARCH_EMBEDDING_UNAVAILABLE) from None

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
