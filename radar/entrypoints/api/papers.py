"""api 的論文頁路由：一篇論文、顯示名稱、前端的設定值（Story 3.6、ADR-0048）。

- 三個端點都要登入（沒登入 401 `not_authenticated`），回應都帶 `Cache-Control: no-store`。
- `GET /api/v1/papers/{paper_id}`：先認 session 再比對 `X-Radar-Expected-Member`（回應的
  `in_my_library` 是成員專屬的；`auth.member_as_expected`，不符 409、格式不對 400、沒帶照常，
  ADR-0055）。顯示名稱與設定值不是成員專屬的資料，不比對。
  看不到、不存在、路徑參數不是 UUID，一律丟同一個
  `ApiProblemError(not_found, headers=NO_STORE)`，狀態碼、本文、標頭都一樣（ADR-0002）。看不到與
  不存在的區分只在 services 的一句帶可見條件的查詢裡（這裡不判斷）；路徑參數宣告成字串、自己轉
  UUID，轉不了也走同一個 404（不讓框架回 422）。
- 這裡只做形狀轉換（`domain/contracts.py` 的模型），不判斷 AI 區塊的狀態、不回預計時間。
- 修正分類（Story 3.11）：`GET /api/v1/papers/{paper_id}/classification-fixes`（分類區塊與修正歷史，
  歷史只有姓名）、`POST` 同一個路徑（一次送主題與方法兩欄＋選填的理由，回修正後的分類區塊與歷史）。
  兩個都先認 session 再比對 `X-Radar-Expected-Member`（`member_as_expected`，ADR-0055）；POST 另外由
  router 的相依項檢查 `Origin`。看不到、不存在、ID 不是 UUID 都是同一個 404（修正看不到的論文
  也是）。
  送來的值不收（不是分類表的 key、沒選、和目前一樣、理由太長等，`domain/classification_fixes`）
  一律 422 `request_invalid`，本文不回顯送來的值；什麼都不寫。
  POST 錯誤的先後（ADR-0063）：`Origin` 403 → 沒登入 401 → 預期成員 409／400 → 論文看不到 404 →
  本文不對 422。看不到的論文在相依項裡查（`visible_paper`），相依項比本文的驗證先跑，所以壞本文
  不會蓋過 404。
- 處理函式的名稱就是 operationId（`paper`、`display_names`、`client_config`、
  `classification_fixes`、`fix_classification`，全站唯一）。
- 一定要在 `mount_web` 之前裝（`install_paper_routes` 會檢查）。
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from radar.domain.auth import AuthenticatedMember
from radar.domain.classification_fixes import ClassificationFixEntry, ClassificationFixInvalidError
from radar.domain.contracts import (
    ClassificationBlock,
    ClientConfig,
    DisplayNames,
    FullTextLinks,
    PaperAbstract,
    PaperBiblio,
    ResearchModelBlock,
    SnapshotRef,
    UtcDateTime,
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
    member_as_expected,
    origin_dependency,
)
from radar.entrypoints.api.problems import ApiProblemError
from radar.entrypoints.api.web import web_mounted
from radar.entrypoints.bootstrap import Database
from radar.services.judgments import (
    ClassificationFixes,
    fix_classification,
    get_classification_fixes,
    paper_is_visible,
)
from radar.services.paper_page import PaperPageSetup, get_paper_page

log = structlog.get_logger(__name__)


class ClassificationFixBody(BaseModel):
    """修正分類送來的內容：新的主題與方法（都是分類表的 `key`，各至少一個）、選填的理由（100 字
    以內）。

    這裡只擋形狀（字串的清單、字串或 null、多餘欄位）與過大的請求；分類表、至少一個、和目前不同、
    理由的字數與字元由 `domain/classification_fixes.check_classification_fix` 驗。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: list[StrictStr] = Field(max_length=64)
    method: list[StrictStr] = Field(max_length=64)
    reason: StrictStr | None = Field(default=None, max_length=1000)


@dataclass(frozen=True)
class PaperRoutes:
    """論文頁路由要用的函式與資料（`build_paper_routes` 組；測試換成假的函式）。

    `load_paper`：成員看得到的那篇論文的資料，不存在或看不到回 None。
    `paper_visible`：成員看不看得到那篇論文（修正分類在驗本文之前先查）。
    `load_classification_fixes`：那篇論文的分類區塊與修正歷史；看不到回 None。
    `fix_classification`：成員修正那篇論文的分類；看不到回 None，送來的值不收丟
    `ClassificationFixInvalidError`。
    """

    load_paper: Callable[[MemberViewer, uuid.UUID], PaperPage | None]
    display_names: DisplayNames
    client_config: ClientConfig
    paper_visible: Callable[[MemberViewer, uuid.UUID], bool]
    load_classification_fixes: Callable[[MemberViewer, uuid.UUID], ClassificationFixes | None]
    fix_classification: Callable[
        [MemberViewer, uuid.UUID, ClassificationFixBody], ClassificationFixes | None
    ]


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

    def paper_visible(viewer: MemberViewer, paper_id: uuid.UUID) -> bool:
        return paper_is_visible(viewer, database, paper_id)

    def load_classification_fixes(
        viewer: MemberViewer, paper_id: uuid.UUID
    ) -> ClassificationFixes | None:
        return get_classification_fixes(viewer, database, setup, paper_id, now=clock())

    def fix(
        actor: MemberViewer, paper_id: uuid.UUID, body: ClassificationFixBody
    ) -> ClassificationFixes | None:
        return fix_classification(
            actor,
            database,
            setup,
            paper_id,
            topic=body.topic,
            method=body.method,
            reason=body.reason,
            now=clock(),
        )

    return PaperRoutes(
        load_paper=load_paper,
        display_names=display_names,
        client_config=client_config,
        paper_visible=paper_visible,
        load_classification_fixes=load_classification_fixes,
        fix_classification=fix,
    )


class PaperPageResponse(BaseModel):
    """論文頁的資料：書目、目前的摘要、三個 AI 區塊（同一個信封）、全文連結、快照。

    沒有目前的摘要時 `abstract` 是 null；一個快照都還沒發佈時 `snapshot` 是 null。
    `in_my_library`：這篇在不在**目前登入的成員自己**的文獻庫（Story 3.17、ADR-0053）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    paper: PaperBiblio
    abstract: PaperAbstract | None
    zh_summary: ZhSummaryBlock
    classification: ClassificationBlock
    research_model: ResearchModelBlock
    full_text_links: FullTextLinks
    snapshot: SnapshotRef | None
    in_my_library: StrictBool


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
        in_my_library=facts.in_my_library,
    )


class ClassificationFixEntryResponse(BaseModel):
    """修正歷史的一筆：時間（UTC）、成員姓名（只有姓名，不帶 email 或成員 ID）、選的主題與方法
    （分類表的 `key`；那一次沒送的欄位是 null）、理由（沒寫是 null）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fixed_at: UtcDateTime
    member_name: str
    topic: tuple[str, ...] | None
    method: tuple[str, ...] | None
    reason: str | None

    @classmethod
    def from_entry(cls, entry: ClassificationFixEntry) -> ClassificationFixEntryResponse:
        return cls(
            fixed_at=entry.fixed_at,
            member_name=entry.member_name,
            topic=entry.topic,
            method=entry.method,
            reason=entry.reason,
        )


class ClassificationFixesResponse(BaseModel):
    """一篇論文的分類區塊（照目前的有效值，和論文頁的 `classification` 同一個形狀）與修正歷史（新的
    在前）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classification: ClassificationBlock
    history: tuple[ClassificationFixEntryResponse, ...]


def classification_fixes_response(fixes: ClassificationFixes) -> ClassificationFixesResponse:
    """services 的結果 → 回應的形狀（只轉換）。"""
    return ClassificationFixesResponse(
        classification=ClassificationBlock.from_block(fixes.classification),
        history=tuple(ClassificationFixEntryResponse.from_entry(entry) for entry in fixes.history),
    )


def _not_found() -> ApiProblemError:
    # 看不到、不存在、壞 ID 都是這一個：狀態碼、本文、標頭相同。
    return ApiProblemError(ErrorCode.NOT_FOUND, headers=NO_STORE)


def _parse_paper_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise _not_found() from None


def install_paper_routes(app: FastAPI, routes: PaperRoutes, auth_routes: AuthRoutes) -> None:
    """把論文頁路由裝到 `app` 上（登入與 `Origin` 檢查用 `auth_routes`）。一定要在 `mount_web`
    之前呼叫。

    Raises:
        RuntimeError: 頁面殼已經掛上（之後才加的路由會被它蓋掉）。
    """
    if web_mounted(app):
        raise RuntimeError("頁面殼已經掛上：API 路由要在 mount_web 之前註冊")

    router = APIRouter(prefix=API_V1_PREFIX, dependencies=[Depends(origin_dependency(auth_routes))])

    def visible_paper(paper_id: str, request: Request) -> tuple[AuthenticatedMember, uuid.UUID]:
        # 修正分類的寫入：先認 session（沒登入 401）、比對預期成員（409／400），再解析 ID、查這位
        # 成員看不看得到（看不到、不存在、壞 ID 都 404）。相依項在本文驗證之前跑，所以沒登入或
        # 看不到的請求不會先得到 422（ADR-0063 錯誤的先後）。
        member = member_as_expected(request, auth_routes)
        parsed = _parse_paper_id(paper_id)
        if not routes.paper_visible(member.viewer, parsed):
            raise _not_found()
        return member, parsed

    @router.get("/papers/{paper_id}")
    def paper(paper_id: str, request: Request, response: Response) -> PaperPageResponse:
        """一篇論文的論文頁資料；看不到、不存在、ID 不是 UUID 都回同一個 404，沒登入 401；
        `X-Radar-Expected-Member` 和登入的成員不同 409 `member_changed`、格式不對 400
        `expected_member_invalid`（回應含 `in_my_library`，ADR-0055）。"""
        member = member_as_expected(request, auth_routes)
        parsed = _parse_paper_id(paper_id)
        page = routes.load_paper(member.viewer, parsed)
        if page is None:
            raise _not_found()
        response.headers.update(NO_STORE)
        return paper_page_response(page)

    @router.get("/papers/{paper_id}/classification-fixes")
    def classification_fixes(
        paper_id: str, request: Request, response: Response
    ) -> ClassificationFixesResponse:
        """一篇論文的分類區塊與修正歷史（新的在前；只有姓名）；看不到、不存在、ID 不是 UUID 都回
        同一個 404，沒登入 401；`X-Radar-Expected-Member` 和登入的成員不同 409 `member_changed`、
        格式不對 400 `expected_member_invalid`。"""
        member = member_as_expected(request, auth_routes)
        fixes = routes.load_classification_fixes(member.viewer, _parse_paper_id(paper_id))
        if fixes is None:
            raise _not_found()
        response.headers.update(NO_STORE)
        return classification_fixes_response(fixes)

    @router.post("/papers/{paper_id}/classification-fixes")
    def fix_classification(
        target: Annotated[tuple[AuthenticatedMember, uuid.UUID], Depends(visible_paper)],
        body: ClassificationFixBody,
        response: Response,
    ) -> ClassificationFixesResponse:
        """修正一篇看得到的論文的分類：主題與方法一次送（各至少一個、都是分類表的 key、兩欄不能都和
        目前一樣）、理由選填（100 字以內），立即生效、不能撤回；回修正後的分類區塊與歷史。看不到、
        不存在、ID 不是 UUID 都回同一個 404；送來的值不收 422 `request_invalid`（什麼都不寫）；
        沒登入 401；`Origin` 缺少或不是本站 403 `origin_rejected`；`X-Radar-Expected-Member` 和
        登入的成員不同 409 `member_changed`、格式不對 400 `expected_member_invalid`。看不到的論文
        帶壞本文也是 404（先查論文、再驗本文）。"""
        member, parsed = target
        try:
            fixes = routes.fix_classification(member.viewer, parsed, body)
        except ClassificationFixInvalidError as exc:
            # 只記原因代號與欄位名稱，不記送來的值。
            log.info("修正分類不收", problem=exc.problem.value, field=exc.field)
            raise ApiProblemError(ErrorCode.REQUEST_INVALID, headers=NO_STORE) from None
        if fixes is None:
            raise _not_found()
        response.headers.update(NO_STORE)
        return classification_fixes_response(fixes)

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
