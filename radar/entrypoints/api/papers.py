"""api 的論文頁路由：一篇論文、顯示名稱、前端的設定值（Story 3.6、ADR-0048）。

- 三個端點都要登入（沒登入 401 `not_authenticated`），回應都帶 `Cache-Control: no-store`。
- `GET /api/v1/papers/{paper_id}`：看不到、不存在、路徑參數不是 UUID，一律丟同一個
  `ApiProblemError(not_found, headers=NO_STORE)`，狀態碼、本文、標頭都一樣（ADR-0002）。看不到與
  不存在的區分只在 services 的一句帶可見條件的查詢裡（這裡不判斷）；路徑參數宣告成字串、自己轉
  UUID，轉不了也走同一個 404（不讓框架回 422）。
- 這裡只做形狀轉換（`domain/contracts.py` 的模型），不判斷 AI 區塊的狀態、不回預計時間。
- 處理函式的名稱就是 operationId（`paper`、`display_names`、`client_config`，全站唯一）。
- 一定要在 `mount_web` 之前裝（`install_paper_routes` 會檢查）。
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict

from radar.domain.contracts import (
    ClassificationBlock,
    ClientConfig,
    DisplayNames,
    FullTextLinks,
    PaperAbstract,
    PaperBiblio,
    ResearchModelBlock,
    SnapshotRef,
    ZhSummaryBlock,
)
from radar.domain.errors import ErrorCode
from radar.domain.paper_page import PaperPage
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
from radar.services.paper_page import PaperPageSetup, get_paper_page


@dataclass(frozen=True)
class PaperRoutes:
    """論文頁路由要用的函式與資料（`build_paper_routes` 組；測試換成假的函式）。

    `load_paper`：成員看得到的那篇論文的資料，不存在或看不到回 None。
    """

    load_paper: Callable[[MemberViewer, uuid.UUID], PaperPage | None]
    display_names: DisplayNames
    client_config: ClientConfig


def build_paper_routes(
    database: Database,
    setup: PaperPageSetup,
    display_names: DisplayNames,
    client_config: ClientConfig,
    clock: Callable[[], datetime],
) -> PaperRoutes:
    """用資料庫、設定與時鐘（預算算台北月份用）組好論文頁路由。"""

    def load_paper(viewer: MemberViewer, paper_id: uuid.UUID) -> PaperPage | None:
        return get_paper_page(viewer, database, setup, paper_id, now=clock())

    return PaperRoutes(
        load_paper=load_paper, display_names=display_names, client_config=client_config
    )


class PaperPageResponse(BaseModel):
    """論文頁的資料：書目、目前的摘要、三個 AI 區塊（同一個信封）、全文連結、快照。

    沒有目前的摘要時 `abstract` 是 null；一個快照都還沒發佈時 `snapshot` 是 null。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    paper: PaperBiblio
    abstract: PaperAbstract | None
    zh_summary: ZhSummaryBlock
    classification: ClassificationBlock
    research_model: ResearchModelBlock
    full_text_links: FullTextLinks
    snapshot: SnapshotRef | None


def paper_page_response(page: PaperPage) -> PaperPageResponse:
    """services 的結果 → 回應的形狀（只轉換）。"""
    facts = page.facts
    return PaperPageResponse(
        paper=PaperBiblio.from_facts(facts, page.journal_key),
        abstract=None if facts.abstract is None else PaperAbstract.from_current(facts.abstract),
        zh_summary=ZhSummaryBlock.from_block(page.zh_summary),
        classification=ClassificationBlock.from_block(page.classification),
        research_model=ResearchModelBlock.from_block(page.research_model),
        full_text_links=FullTextLinks.from_facts(facts),
        snapshot=None if page.snapshot is None else SnapshotRef.from_snapshot(page.snapshot),
    )


def _not_found() -> ApiProblemError:
    # 看不到、不存在、壞 ID 都是這一個：狀態碼、本文、標頭相同。
    return ApiProblemError(ErrorCode.NOT_FOUND, headers=NO_STORE)


def install_paper_routes(app: FastAPI, routes: PaperRoutes, auth_routes: AuthRoutes) -> None:
    """把論文頁路由裝到 `app` 上（登入與 `Origin` 檢查用 `auth_routes`）。一定要在 `mount_web`
    之前呼叫。

    Raises:
        RuntimeError: 頁面殼已經掛上（之後才加的路由會被它蓋掉）。
    """
    if web_mounted(app):
        raise RuntimeError("頁面殼已經掛上：API 路由要在 mount_web 之前註冊")

    router = APIRouter(prefix=API_V1_PREFIX, dependencies=[Depends(origin_dependency(auth_routes))])

    @router.get("/papers/{paper_id}")
    def paper(paper_id: str, request: Request, response: Response) -> PaperPageResponse:
        """一篇論文的論文頁資料；看不到、不存在、ID 不是 UUID 都回同一個 404，沒登入 401。"""
        member = current_member(request, auth_routes)
        try:
            parsed = uuid.UUID(paper_id)
        except ValueError:
            raise _not_found() from None
        page = routes.load_paper(member.viewer, parsed)
        if page is None:
            raise _not_found()
        response.headers.update(NO_STORE)
        return paper_page_response(page)

    @router.get("/display-names")
    def display_names(request: Request, response: Response) -> DisplayNames:
        """期刊、主題、方法的顯示名稱（照設定檔順序，不含 `unclassified`）；沒登入 401。"""
        current_member(request, auth_routes)
        response.headers.update(NO_STORE)
        return routes.display_names

    @router.get("/client-config")
    def client_config(request: Request, response: Response) -> ClientConfig:
        """前端要用的設定值（時區）；沒登入 401。"""
        current_member(request, auth_routes)
        response.headers.update(NO_STORE)
        return routes.client_config

    app.include_router(router)
