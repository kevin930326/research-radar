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

- **同一句查詢、不同的設定重跑（評估用，Story 4.4）**：`embed_retrieval_query` 先把查詢驗過、
  轉成向量（記帳與額度和搜尋同一條路），回 `SearchQueryVector`；之後把它當 `query_vector` 和同一
  句 `query` 一起交給 `retrieve`，就不再呼叫 provider（例如 `radar eval search` 用多個相似度下限
  重跑同一題時，查詢向量只算一次）。`query_vector` 要和這次的 `query`（驗過的字）與
  `setup.embedding.model_id` 對得上，對不上就丟錯；沒給時行為和原本一樣。
  - **只收這個模組算出來的向量**（PR C 審查 H1）：`embed_retrieval_query` 產生物件時用行程內的
    亂數（`_SEAL_SALT`，只在這個模組）對「查詢字、模型、向量」算一個 HMAC 封條（`seal`）；`retrieve`
    重算比對，對不上（呼叫端自己建的、`dataclasses.replace` 換過向量的）一律丟錯。所以重用的向量
    一定是這個行程裡、經過驗字、外送檢查與記帳算出來的。API 不傳這個參數（架構測試
    `tests/arch/test_query_vector_reuse.py`：只有 `services/evaluation.py` 傳）。
- **呼叫端開好的唯讀交易（評估用，PR C 審查 H2）**：`connection` 給了就在那個交易裡讀（呼叫端要用
  `Database.read_only_snapshot_transaction()` 開），不另開交易；同一題的多個相似度下限因此讀同一份
  資料。給 `connection` 的搜尋一定要同時給 `query_vector`（交易裡不呼叫 provider）。

`setup` 由行程啟動時讀好的設定組成（services 不讀設定檔）。
"""

import hmac
import secrets
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, overload

import structlog

from radar.domain.embeddings import EmbeddingConfig
from radar.domain.fingerprints import query_vector_seal
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
from radar.repo.db import Database, DbConnection
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


# 查詢向量封條的亂數（每個行程各自產生、不存不印；見模組說明）。
_SEAL_SALT: Final = secrets.token_bytes(32)


def _seal(text: str, model_id: str, vector: Sequence[float]) -> bytes:
    return query_vector_seal(_SEAL_SALT, text, model_id, vector)


@dataclass(frozen=True, slots=True)
class SearchQueryVector:
    """驗過的查詢字串（去掉頭尾空白）與它的查詢向量（`model_id` 這個模型的）。只由
    `embed_retrieval_query` 產生（帶封條 `seal`）；交給 `retrieve(query_vector=…)` 重用，不再呼叫
    provider。自己建的物件封條對不上，`retrieve` 不收。"""

    text: str
    model_id: str
    vector: tuple[float, ...]
    seal: bytes = field(repr=False)


def embed_retrieval_query(
    viewer: Viewer, database: Database, setup: RetrievalSetup, query: object
) -> SearchQueryVector:
    """把搜尋的查詢驗過、轉成向量（和 `retrieve` 的搜尋同一條路：驗字 → 日誌只記長度與有沒有
    中文 → `clear_for_cloud` → `services/embeddings.embed_search_query` 記帳、占額度）。

    Raises:
        InvalidSearchQueryError: 查詢字串不收（不呼叫 provider）。
        SearchQuotaExhaustedError: Workers AI 今天的額度用完了。
        SearchEmbeddingUnavailableError: 查詢沒能轉成向量（連不上等）。
        EmbeddingModelMismatchError: provider 的模型和設定不同。
    """
    text = clean_search_query(query, max_length=setup.retrieval.query_max_length)
    log.info("檢索：查詢向量", **query_log_facts(text))
    values = tuple(float(v) for v in _embed_cleaned_query(viewer, database, setup, text))
    model_id = setup.embedding.model_id
    return SearchQueryVector(
        text=text, model_id=model_id, vector=values, seal=_seal(text, model_id, values)
    )


def _embed_cleaned_query(
    viewer: Viewer, database: Database, setup: RetrievalSetup, text: str
) -> list[float]:
    # 驗過的查詢字串照「使用者打的字」放行（空字串在呼叫前已經擋下）。
    safe = clear_for_cloud(text, ContentSource.USER_QUERY, None, setup.cloud_policy)
    return embed_search_query(
        viewer, database, setup.provider, setup.embedding, safe, clock=setup.clock
    )


@overload
def retrieve(
    viewer: PublicViewer,
    database: Database,
    setup: RetrievalSetup,
    *,
    query: str | None = None,
    query_vector: SearchQueryVector | None = None,
    paper_id: uuid.UUID | None = None,
    scope: Literal[RetrievalScope.DEMO],
    filters: PaperListFilters,
    k: int,
    offset: int = 0,
    connection: DbConnection | None = None,
) -> RetrievalPage: ...


@overload
def retrieve(
    viewer: Viewer,
    database: Database,
    setup: RetrievalSetup,
    *,
    query: str | None = None,
    query_vector: SearchQueryVector | None = None,
    paper_id: uuid.UUID | None = None,
    scope: MemberScope,
    filters: PaperListFilters,
    k: int,
    offset: int = 0,
    connection: DbConnection | None = None,
) -> RetrievalPage: ...


def retrieve(
    viewer: Viewer,
    database: Database,
    setup: RetrievalSetup,
    *,
    query: str | None = None,
    query_vector: SearchQueryVector | None = None,
    paper_id: uuid.UUID | None = None,
    scope: RetrievalScope,
    filters: PaperListFilters,
    k: int,
    offset: int = 0,
    connection: DbConnection | None = None,
) -> RetrievalPage:
    """依 `query`（搜尋）或 `paper_id`（找相似論文）檢索，回融合後名次 `offset + 1` 起最多 `k` 篇的
    論文 ID 與名次，以及 `total`（見模組說明）。

    Raises:
        ValueError: `query` 與 `paper_id` 不是剛好給一個、給了 `query_vector` 卻沒有 `query`
            （或它和 `query`、設定的模型對不上，或不是 `embed_retrieval_query` 算出來的）、給了
            `connection` 的搜尋沒有 `query_vector`、`k` 不是正整數或超過
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
    if query_vector is not None and query is None:
        raise ValueError("query_vector 只能和 query 一起給")
    if connection is not None and query is not None and query_vector is None:
        raise ValueError("給了 connection 的搜尋要同時給 query_vector（交易裡不呼叫 provider）")
    if type(k) is not int or not 1 <= k <= config.max_results:
        raise ValueError(f"k 要是 1 到 {config.max_results} 的整數")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset 要是非負整數")
    expected = PaperListScope.TRACKED if scope is RetrievalScope.DEMO else PaperListScope(scope)
    if filters.scope is not expected:
        raise ValueError("filters.scope 和 scope 對不上")

    if paper_id is not None:
        matches, snapshot = _similar(viewer, database, setup, paper_id, scope, filters, connection)
    else:
        matches, snapshot = _search(
            viewer, database, setup, query, query_vector, scope, filters, connection
        )
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


@contextmanager
def _reading(database: Database, connection: DbConnection | None) -> Iterator[DbConnection]:
    """呼叫端給了唯讀交易的連線就用它（不另開、不結束），沒給就自己開一個唯讀、REPEATABLE READ
    的交易。"""
    if connection is not None:
        yield connection
        return
    with database.read_only_snapshot_transaction() as opened:
        yield opened


def _search(
    viewer: Viewer,
    database: Database,
    setup: RetrievalSetup,
    query: object,
    query_vector: SearchQueryVector | None,
    scope: RetrievalScope,
    filters: PaperListFilters,
    connection: DbConnection | None,
) -> tuple[list[FusedMatch], Snapshot | None]:
    config = setup.retrieval
    text = clean_search_query(query, max_length=config.query_max_length)
    log.info("檢索：搜尋", scope=scope.value, **query_log_facts(text))
    vector: Sequence[float]
    if query_vector is None:
        vector = _embed_cleaned_query(viewer, database, setup, text)
    else:
        # 重用先算好的查詢向量（不呼叫 provider、不記帳）：要是這個模組算的（封條對得上）、
        # 同一句查詢、同一個模型的。
        if not isinstance(query_vector, SearchQueryVector) or not hmac.compare_digest(
            query_vector.seal,
            _seal(query_vector.text, query_vector.model_id, query_vector.vector),
        ):
            raise ValueError("query_vector 不是 embed_retrieval_query 算出來的")
        if query_vector.text != text or query_vector.model_id != setup.embedding.model_id:
            raise ValueError("query_vector 和這次的 query 或設定的模型對不上")
        vector = query_vector.vector
    with _reading(database, connection) as reading:
        snapshot = find_latest_published_snapshot(SystemViewer(), reading)
        matches = search_papers(
            viewer,
            reading,
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
    connection: DbConnection | None,
) -> tuple[list[FusedMatch], Snapshot | None]:
    config = setup.retrieval
    model_id = setup.embedding.model_id
    with _reading(database, connection) as reading:
        anchor = find_visible_paper_embedding(viewer, reading, paper_id, model_id=model_id)
        if anchor is None:
            raise RetrievalAnchorNotFoundError()
        log.info("檢索：找相似論文", scope=scope.value, has_vector=anchor.embedding is not None)
        snapshot = find_latest_published_snapshot(SystemViewer(), reading)
        if anchor.embedding is None:
            return [], snapshot
        matches = search_papers(
            viewer,
            reading,
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
