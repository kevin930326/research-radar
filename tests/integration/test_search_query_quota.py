"""搜尋的查詢向量：用量、額度與失敗（Story 4.1、AD-12、AD-25、ADR-0005、ADR-0040）對真的
PostgreSQL。

- 每次都寫一筆 `api_call`（`workers_ai`、operation `embed_query`、結果代碼 `outcome`），和背景向量化
  同一本帳。
- 搜尋可以用到每日免費額度的 100%：背景向量化停在 `stop_at_ratio`（這裡 0.8）時，搜尋照常。
- 額度用完：不送、不降級成只用關鍵字，丟 `SearchQuotaExhaustedError`（`search_quota_exhausted`），
  記一列 `quota_exhausted`（估計 token 0、沒有狀態碼）、寫一筆警告日誌。
- 連不上（重試用完）：丟 `SearchEmbeddingUnavailableError`（`search_embedding_unavailable`），記一列
  `unreachable`；設定錯（401）記 `rejected`、估計 token 0。查 `api_call` 就分得出兩種情況。
- 回應的向量當作使用者輸入驗：維度不對、全 0 都丟 `SearchEmbeddingUnavailableError`。
- 兩個請求同時搶最後的額度：額度鎖讓它們排隊，先占到的送出、另一個看到占用而放不下，只有一個送出。

provider 是測試自己寫的替身（不連網路）；向量是測試自己編的數字。
"""

import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from structlog.testing import capture_logs

from radar.adapters.fake import FakeEmbeddingProvider
from radar.domain.embeddings import (
    EmbeddingConfig,
    EmbeddingConfigurationError,
    EmbeddingTransientError,
    EmbedStop,
)
from radar.domain.errors import ErrorCode
from radar.domain.paper_list import PaperListFilters
from radar.domain.policy import CloudPolicy, CloudSafeText
from radar.domain.retrieval import (
    RetrievalScope,
    SearchEmbeddingUnavailableError,
    SearchQuotaExhaustedError,
)
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.services.embeddings import embed_papers
from radar.services.retrieval import RetrievalSetup, retrieve
from tests.database import TestDatabase, migrate
from tests.integration.test_embeddings import MODEL, NOW, _config
from tests.integration.test_retrieval import RETRIEVAL, QueryVectors, near, put_vector
from tests.llm_support import CODE_VERSION, create_paper, execute

WAIT = 20.0
VIEWER = MemberViewer(member_id=uuid.UUID("018f0000-0000-7000-8000-0000000000c1"), is_admin=False)
# 每日免費額度 1 neuron（約 930 個估計 token），背景向量化停在 80%。
SMALL = _config(
    quota={
        "daily_free_neurons": 1,
        "neurons_per_million_input_tokens": 1075,
        "stop_at_ratio": "0.8",
        "verified_on": "2026-11-15",
        "source_url": "https://developers.cloudflare.com/workers-ai/platform/pricing/",
    }
)
QUERY_TEXT = "trust"  # ceil(5 ÷ 3)＋4＝6 個估計 token
QUERY_TOKENS = 6


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    execute(
        fresh_database,
        "INSERT INTO embedding_model (model_id, provider, dimensions) VALUES (%s, 'fake', 8)",
        MODEL,
    )
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=4)


def setup_with(provider: Any, config: EmbeddingConfig = SMALL) -> RetrievalSetup:
    return RetrievalSetup(
        embedding=config,
        retrieval=RETRIEVAL,
        cloud_policy=CloudPolicy(),
        provider=provider,
        clock=lambda: NOW,
    )


def search(database: Database, setup: RetrievalSetup, query: str = QUERY_TEXT) -> Any:
    return retrieve(
        VIEWER,
        database,
        setup,
        query=query,
        scope=RetrievalScope.TRACKED,
        filters=PaperListFilters(),
        k=20,
    )


def used(db: TestDatabase, tokens: int) -> None:
    """今天（UTC）已經用掉 `tokens` 個估計 token（背景向量化記的一列）。"""
    execute(
        db,
        "INSERT INTO api_call (source, operation, http_status, cost_usd, estimated_input_tokens,"
        " called_at) VALUES ('workers_ai', 'embed_documents', 200, 0, %s, %s)",
        tokens,
        NOW,
    )


def query_calls(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db,
        "SELECT outcome, http_status, estimated_input_tokens FROM api_call"
        " WHERE operation = 'embed_query' ORDER BY created_at, id",
    )


class Failing:
    """embed_query 丟 `error` 的替身。"""

    model_id = MODEL

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def embed_documents(self, texts: Any) -> list[list[float]]:
        raise AssertionError("不該呼叫")

    def embed_query(self, text: CloudSafeText) -> list[float]:
        self.calls += 1
        raise self.error


class Returning:
    """embed_query 回固定（可能不合格）向量的替身。"""

    model_id = MODEL

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector

    def embed_documents(self, texts: Any) -> list[list[float]]:
        raise AssertionError("不該呼叫")

    def embed_query(self, text: CloudSafeText) -> list[float]:
        return self.vector


def test_each_search_writes_one_embed_query_api_call(db: TestDatabase, database: Database) -> None:
    search(database, setup_with(QueryVectors()))

    assert query_calls(db) == [("succeeded", 200, QUERY_TOKENS)]
    assert execute(db, "SELECT source, cost_usd FROM api_call") == [("workers_ai", 0)]


def test_search_may_use_the_share_background_embedding_leaves_for_it(
    db: TestDatabase, database: Database
) -> None:
    # 約 85% 已經用掉：背景向量化（停在 80%）不送，搜尋照常。
    used(db, 790)
    create_paper(database)
    provider = FakeEmbeddingProvider(dimensions=8, model_id=MODEL)

    background = embed_papers(
        SystemViewer(),
        database,
        provider,
        SMALL,
        CloudPolicy(),
        code_version=CODE_VERSION,
        clock=lambda: NOW,
    )
    search(database, setup_with(QueryVectors()))

    assert background.stop is EmbedStop.QUOTA_REACHED
    assert provider.document_calls == []
    assert query_calls(db) == [("succeeded", 200, QUERY_TOKENS)]


def test_quota_exhausted_does_not_send_or_fall_back_and_is_recorded(
    db: TestDatabase, database: Database
) -> None:
    paper = create_paper(database, title="Trust in ledgers")
    put_vector(db, paper, near(0))
    used(db, 928)  # 928＋6 超過約 930.23
    provider = QueryVectors()

    with capture_logs() as logs, pytest.raises(SearchQuotaExhaustedError) as caught:
        search(database, setup_with(provider))

    assert caught.value.code is ErrorCode.SEARCH_QUOTA_EXHAUSTED
    assert str(caught.value) == "search_quota_exhausted"
    assert provider.queries == []
    assert query_calls(db) == [("quota_exhausted", None, 0)]
    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert [entry["event"] for entry in warnings] == [
        "Workers AI 今天的額度用完了，搜尋回固定的錯誤（不降級成只用關鍵字）"
    ]
    assert QUERY_TEXT not in repr(logs)
    assert execute(db, "SELECT count(*) FROM embedding_quota_hold") == [(0,)]


def test_unreachable_workers_ai_is_a_different_code_and_outcome(
    db: TestDatabase, database: Database
) -> None:
    paper = create_paper(database, title="Trust in ledgers")
    put_vector(db, paper, near(0))
    provider = Failing(EmbeddingTransientError("連線錯誤"))

    with pytest.raises(SearchEmbeddingUnavailableError) as caught:
        search(database, setup_with(provider))

    assert caught.value.code is ErrorCode.SEARCH_EMBEDDING_UNAVAILABLE
    assert provider.calls == 1
    assert query_calls(db) == [("unreachable", None, QUERY_TOKENS)]
    assert execute(db, "SELECT count(*) FROM embedding_quota_hold") == [(0,)]


def test_configuration_error_is_rejected_and_not_billed(
    db: TestDatabase, database: Database
) -> None:
    provider = Failing(EmbeddingConfigurationError("401", http_status=401))

    with pytest.raises(SearchEmbeddingUnavailableError):
        search(database, setup_with(provider))

    assert query_calls(db) == [("rejected", 401, 0)]


@pytest.mark.parametrize(
    "vector", [[0.1] * 7, [0.0] * 8, [float("nan")] + [0.1] * 7], ids=["dims", "zero", "nan"]
)
def test_bad_query_vectors_are_rejected_like_user_input(
    db: TestDatabase, database: Database, vector: list[float]
) -> None:
    with pytest.raises(SearchEmbeddingUnavailableError):
        search(database, setup_with(Returning(vector)))

    # 回了 200：照記這一次請求的用量。
    assert query_calls(db) == [("succeeded", 200, QUERY_TOKENS)]


class Blocking(QueryVectors):
    """第一個呼叫停在 `embed_query` 裡，等測試放行（占用已經在帳上）。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed_query(self, text: CloudSafeText) -> list[float]:
        self.entered.set()
        if not self.release.wait(WAIT):
            raise AssertionError("等不到放行")
        return super().embed_query(text)


def _in_thread(target: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    outcome: list[Any] = []

    def run() -> None:
        try:
            outcome.append(target())
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def test_two_searches_racing_for_the_last_quota_send_only_one(
    db: TestDatabase, database: Database
) -> None:
    paper = create_paper(database, title="Trust in ledgers")
    put_vector(db, paper, near(0))
    used(db, 922)  # 922＋6＝928 放得下；再加 6 就超過約 930.23
    first = Blocking()
    second = QueryVectors()

    thread, first_result = _in_thread(lambda: search(database, setup_with(first)))
    assert first.entered.wait(WAIT)  # 第一個已經占好額度、停在等回應
    with pytest.raises(SearchQuotaExhaustedError):
        search(database, setup_with(second))
    first.release.set()
    thread.join(WAIT)

    assert not thread.is_alive()
    assert len(first_result) == 1
    assert not isinstance(first_result[0], BaseException)
    assert second.queries == []
    assert sorted(query_calls(db), key=repr) == sorted(
        [("quota_exhausted", None, 0), ("succeeded", 200, QUERY_TOKENS)], key=repr
    )
    assert execute(db, "SELECT count(*) FROM embedding_quota_hold") == [(0,)]
