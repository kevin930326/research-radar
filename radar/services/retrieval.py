"""檢索的唯一入口（Story 4.1、FR-26、FR-27、AD-11、ADR-0005）：搜尋、找相似論文、問答、展示問答都
呼叫 `retrieve`，其他地方不自己寫向量或全文檢索的 SQL（SQL 只在 `repo/retrieval.py`）。

`retrieve(viewer, database, setup, *, query 或 paper_id, scope, filters, k, offset)`：

- **搜尋（`query`）**：查詢字串當使用者輸入驗（`domain/retrieval.clean_search_query`）→ 日誌只記
  長度與有沒有中文 → `clear_for_cloud`（來源 `user_query`）→
  `services/embeddings.embed_search_query`
  （`embed_query`、記帳、額度可以用到 100%；額度用完或連不上丟固定的錯，不降級成只用關鍵字）→
  一個唯讀、REPEATABLE READ 的交易裡讀目前已發佈的快照（`this_week` 用）並跑兩條腿＋RRF
  （`repo/retrieval.search_papers`）。
- **找相似論文（`paper_id`）**：不呼叫 provider、不占額度。同一個交易裡讀起點
  （`repo/embeddings.find_visible_paper_embedding`：看不到、不存在、被排除都丟
  `RetrievalAnchorNotFoundError`）→ 起點沒有這個模型的向量回空結果（不是錯誤）→ 只用向量腿、拿存好的
  向量查、不含起點自己。
- 向量一律只比 `setup.embedding.model_id`（設定指定的那一個模型）。
- 範圍：先套 viewer 的可見條件、再套 `scope`；`demo` 只給 `PublicViewer`（overload 讓型別檢查擋；
  執行時 `retrieve` 一進來、送查詢向量或讀起點之前就擋，`repo/visibility.visible_demo_papers`
  組條件時再擋一次）。`filters` 的期刊、年份、主題、方法、
  `has_abstract`、`this_week` 和找論文清單同一套；`filters.scope` 要和 `scope` 同名（`demo` 時要是
  預設的 `tracked`），對不上就丟錯（不默默以其中一個為準）。
- 結果：融合後全部的結果（最多 2 × `leg_limit` 篇）在 Python 切出 `offset` 之後的 `k` 篇；`total` 是
  融合後的篇數。兩者出自同一句查詢，所以 `total` 等於翻得到的篇數。結果帶著排名那個交易讀到的
  已發佈快照（`RetrievalPage.snapshot`；找相似論文的起點沒有向量時也照樣讀），呼叫端的回應用它，
  不另外重讀（PR B 審查 M3）。

`setup` 由行程啟動時讀好的設定組成（services 不讀設定檔）。
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, overload

import structlog

from radar.domain.embeddings import EmbeddingConfig
from radar.domain.paper_list import PaperListFilters, PaperListScope
from radar.domain.policy import CloudPolicy, ContentSource, clear_for_cloud
from radar.domain.retrieval import (
    FusedMatch,
    RetrievalAnchorNotFoundError,
    RetrievalConfig,
    RetrievalHit,
    RetrievalPage,
    RetrievalScope,
    clean_search_query,
    query_log_facts,
)
from radar.domain.snapshot import Snapshot
from radar.domain.viewer import PublicViewer, SystemViewer, Viewer
from radar.ports import EmbeddingProvider
from radar.repo.db import Database
from radar.repo.embeddings import find_visible_paper_embedding
from radar.repo.retrieval import search_papers
from radar.repo.snapshots import find_latest_published_snapshot
from radar.services.embeddings import embed_search_query

log = structlog.get_logger(__name__)

type MemberScope = Literal[RetrievalScope.PERSONAL, RetrievalScope.LAB, RetrievalScope.TRACKED]


@dataclass(frozen=True)
class RetrievalSetup:
    """檢索要的設定與依賴（行程啟動時組好）：查詢向量的模型與額度（`embeddings.yaml`）、檢索的數字
    （`retrieval.yaml`）、送雲端的放行設定、provider、時鐘（額度的 UTC 日期與記帳時間）。"""

    embedding: EmbeddingConfig
    retrieval: RetrievalConfig
    cloud_policy: CloudPolicy
    provider: EmbeddingProvider
    clock: Callable[[], datetime]


@overload
def retrieve(
    viewer: PublicViewer,
    database: Database,
    setup: RetrievalSetup,
    *,
    query: str | None = None,
    paper_id: uuid.UUID | None = None,
    scope: Literal[RetrievalScope.DEMO],
    filters: PaperListFilters,
    k: int,
    offset: int = 0,
) -> RetrievalPage: ...


@overload
def retrieve(
    viewer: Viewer,
    database: Database,
    setup: RetrievalSetup,
    *,
    query: str | None = None,
    paper_id: uuid.UUID | None = None,
    scope: MemberScope,
    filters: PaperListFilters,
    k: int,
    offset: int = 0,
) -> RetrievalPage: ...


def retrieve(
    viewer: Viewer,
    database: Database,
    setup: RetrievalSetup,
    *,
    query: str | None = None,
    paper_id: uuid.UUID | None = None,
    scope: RetrievalScope,
    filters: PaperListFilters,
    k: int,
    offset: int = 0,
) -> RetrievalPage:
    """依 `query`（搜尋）或 `paper_id`（找相似論文）檢索，回融合後名次 `offset + 1` 起最多 `k` 篇的
    論文 ID 與名次，以及 `total`（見模組說明）。

    Raises:
        ValueError: `query` 與 `paper_id` 不是剛好給一個、`k` 不是正整數或超過
            `RetrievalConfig.max_results`、`offset` 不是非負整數，或 `filters.scope` 和 `scope`
            對不上。
        TypeError: `scope` 是 `demo` 但 `viewer` 不是 `PublicViewer`。
        InvalidSearchQueryError: 查詢字串不收（不呼叫 provider）。
        RetrievalAnchorNotFoundError: 找相似論文的起點不存在、看不到或被排除。
        SearchQuotaExhaustedError: Workers AI 今天的額度用完了。
        SearchEmbeddingUnavailableError: 查詢沒能轉成向量（連不上等）。
        EmbeddingModelMismatchError: provider 的模型和設定不同。
    """
    config = setup.retrieval
    scope = RetrievalScope(scope)
    if scope is RetrievalScope.DEMO and not isinstance(viewer, PublicViewer):
        # 分流之前就擋（審查 H1）：不送查詢向量、不占額度、不讀起點。repo 組條件時會再擋一次。
        raise TypeError(f"demo 範圍只給 PublicViewer（現在是 {type(viewer).__name__}）")
    if (query is None) == (paper_id is None):
        raise ValueError("query 與 paper_id 要剛好給一個")
    if type(k) is not int or not 1 <= k <= config.max_results:
        raise ValueError(f"k 要是 1 到 {config.max_results} 的整數")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset 要是非負整數")
    expected = PaperListScope.TRACKED if scope is RetrievalScope.DEMO else PaperListScope(scope)
    if filters.scope is not expected:
        raise ValueError("filters.scope 和 scope 對不上")

    if paper_id is not None:
        matches, snapshot = _similar(viewer, database, setup, paper_id, scope, filters)
    else:
        matches, snapshot = _search(viewer, database, setup, query, scope, filters)
    page = matches[offset : offset + k]
    return RetrievalPage(
        hits=tuple(
            RetrievalHit(
                paper_id=match.paper_id,
                rank=offset + index + 1,
                vector_rank=match.vector_rank,
                text_rank=match.text_rank,
            )
            for index, match in enumerate(page)
        ),
        total=len(matches),
        offset=offset,
        k=k,
        snapshot=snapshot,
    )


def _search(
    viewer: Viewer,
    database: Database,
    setup: RetrievalSetup,
    query: object,
    scope: RetrievalScope,
    filters: PaperListFilters,
) -> tuple[list[FusedMatch], Snapshot | None]:
    config = setup.retrieval
    text = clean_search_query(query, max_length=config.query_max_length)
    log.info("檢索：搜尋", scope=scope.value, **query_log_facts(text))
    # 驗過的查詢字串照「使用者打的字」放行（空字串在上面已經擋下）。
    safe = clear_for_cloud(text, ContentSource.USER_QUERY, None, setup.cloud_policy)
    vector = embed_search_query(
        viewer, database, setup.provider, setup.embedding, safe, clock=setup.clock
    )
    with database.read_only_snapshot_transaction() as connection:
        snapshot = find_latest_published_snapshot(SystemViewer(), connection)
        matches = search_papers(
            viewer,
            connection,
            model_id=setup.embedding.model_id,
            vector=vector,
            text_query=text,
            scope=scope,
            filters=filters,
            current_snapshot_id=None if snapshot is None else snapshot.id,
            leg_limit=config.leg_limit,
            rrf_k=config.rrf_k,
            max_distance=config.vector_max_distance,
        )
    return matches, snapshot


def _similar(
    viewer: Viewer,
    database: Database,
    setup: RetrievalSetup,
    paper_id: uuid.UUID,
    scope: RetrievalScope,
    filters: PaperListFilters,
) -> tuple[list[FusedMatch], Snapshot | None]:
    config = setup.retrieval
    model_id = setup.embedding.model_id
    with database.read_only_snapshot_transaction() as connection:
        anchor = find_visible_paper_embedding(viewer, connection, paper_id, model_id=model_id)
        if anchor is None:
            raise RetrievalAnchorNotFoundError()
        log.info("檢索：找相似論文", scope=scope.value, has_vector=anchor.embedding is not None)
        snapshot = find_latest_published_snapshot(SystemViewer(), connection)
        if anchor.embedding is None:
            return [], snapshot
        matches = search_papers(
            viewer,
            connection,
            model_id=model_id,
            vector=anchor.embedding.vector,
            text_query=None,
            scope=scope,
            filters=filters,
            current_snapshot_id=None if snapshot is None else snapshot.id,
            leg_limit=config.leg_limit,
            rrf_k=config.rrf_k,
            max_distance=config.vector_max_distance,
            exclude_paper_id=paper_id,
        )
    return matches, snapshot
