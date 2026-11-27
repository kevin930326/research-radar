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
  第二個請求在第一個還沒 commit 占用時就來搶，也要排隊等額度鎖（審查 M5）。
- 背景向量化的 80% 把搜尋的用量算進去；搜尋到 100% 前照常（審查 M6）。
- 沒有送出的失敗（等額度鎖逾時、adapter 送出前就停下）也記一列 `not_sent`（審查 H2）。
- 占用記下是 `embed_query`：搜尋中斷留下的占用過期後照 `embed_query` 轉成用量（審查 H3）。真的
  adapter 送出後被中斷（`BaseException`）時照「已送出」記一列，不會被當成沒送出（H3 的核對）。

provider 是測試自己寫的替身（不連網路）；向量是測試自己編的數字。
"""

import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import httpx2
import pytest
import stamina
from pydantic import SecretStr
from structlog.testing import capture_logs

from radar.adapters.fake import FakeEmbeddingProvider
from radar.adapters.workers_ai import WorkersAiEmbeddingProvider
from radar.domain.embeddings import (
    EMBED_SEPARATOR,
    EmbeddingConfig,
    EmbeddingConfigurationError,
    EmbeddingTransientError,
    EmbedStop,
    estimate_embedding_tokens,
    quota_allows,
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
from radar.repo.db import EMBEDDING_QUOTA_LOCK_KEY, Database
from radar.services.embeddings import embed_papers
from radar.services.retrieval import RetrievalSetup, retrieve
from tests.database import APP_ROLE, TestDatabase, migrate
from tests.integration.test_embeddings import DIMENSIONS, MODEL, NOW, _config
from tests.integration.test_retrieval import RETRIEVAL, QueryVectors, near, put_vector
from tests.llm_support import CODE_VERSION, create_paper, execute

WAIT = 20.0
WAIT_POLLS = 5_000  # 輪詢「在等額度鎖」的上限次數（每次一句查詢，不 sleep）
VIEWER = MemberViewer(member_id=uuid.UUID("018f0000-0000-7000-8000-0000000000c1"), is_admin=False)
# 每日免費額度 1 neuron（約 930 個估計 token），背景向量化停在 80%。
SMALL_QUOTA = {
    "daily_free_neurons": 1,
    "neurons_per_million_input_tokens": 1075,
    "stop_at_ratio": "0.8",
    "verified_on": "2026-11-15",
    "source_url": "https://developers.cloudflare.com/workers-ai/platform/pricing/",
}
SMALL = _config(quota=SMALL_QUOTA)
FAST_LOCK = _config(quota=SMALL_QUOTA, lock_timeout_seconds=0.2)
FAKE_ACCOUNT = "0123456789abcdef0123456789abcdef"
FAKE_TOKEN = "fake-token-for-tests"
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


# --- 第二個請求在第一個 commit 占用之前就來搶（審查 M5） ------------------------------------------


class _ClockThatStopsInsideTheQuotaLock:
    """第一次被讀（`_QueryLedger` 拿到額度鎖之後才讀時鐘）時停住，等測試放行。"""

    def __init__(self) -> None:
        self.inside = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        if self.calls == 1:
            self.inside.set()
            if not self.release.wait(WAIT):
                raise AssertionError("等不到放行")
        return NOW


def _waiting_for_the_quota_lock(db: TestDatabase) -> bool:
    unsigned = EMBEDDING_QUOTA_LOCK_KEY & 0xFFFF_FFFF_FFFF_FFFF
    ((waiting,),) = execute(
        db,
        "SELECT count(*) FROM pg_locks"
        " WHERE locktype = 'advisory' AND NOT granted AND objsubid = 1"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        " AND classid = %s::bigint::oid AND objid = %s::bigint::oid",
        unsigned >> 32,
        unsigned & 0xFFFF_FFFF,
    )
    return bool(waiting)


def _poll(condition: Callable[[], bool], what: str) -> None:
    for _ in range(WAIT_POLLS):
        if condition():
            return
    raise AssertionError(what)


def test_second_search_waits_for_the_quota_lock_before_the_first_commits_its_hold(
    db: TestDatabase, database: Database
) -> None:
    # 第一個搜尋拿到額度鎖、還沒寫占用就停住；第二個搜尋這時來搶，要卡在額度鎖上（看 pg_locks）。
    # 放行後第一個占到、送出；第二個看到占用而放不下。拿掉額度鎖時第二個不會等，兩個都送出。
    paper = create_paper(database, title="Trust in ledgers")
    put_vector(db, paper, near(0))
    used(db, 922)  # 922＋6 放得下；再加 6 就超過約 930.23
    clock = _ClockThatStopsInsideTheQuotaLock()
    first_provider, second_provider = QueryVectors(), QueryVectors()
    first_setup = RetrievalSetup(
        embedding=SMALL,
        retrieval=RETRIEVAL,
        cloud_policy=CloudPolicy(),
        provider=first_provider,
        clock=clock,
    )

    first, first_result = _in_thread(lambda: search(database, first_setup))
    assert clock.inside.wait(WAIT)
    second, second_result = _in_thread(lambda: search(database, setup_with(second_provider)))
    try:
        _poll(lambda: _waiting_for_the_quota_lock(db), "第二個搜尋沒有在等額度鎖")
        assert second_result == []
    finally:
        clock.release.set()
        first.join(WAIT)
        second.join(WAIT)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(first_result) == 1
    assert not isinstance(first_result[0], BaseException)
    assert len(second_result) == 1
    assert isinstance(second_result[0], SearchQuotaExhaustedError)
    assert len(first_provider.queries) == 1
    assert second_provider.queries == []
    assert sorted(query_calls(db), key=repr) == sorted(
        [("quota_exhausted", None, 0), ("succeeded", 200, QUERY_TOKENS)], key=repr
    )


# --- 背景的 80% 把搜尋的用量算進去（審查 M6） -----------------------------------------------------


def test_search_usage_counts_toward_the_background_stop_and_search_goes_on_to_100_percent(
    db: TestDatabase, database: Database
) -> None:
    title = "A study of information systems"
    abstract = "We study how teams adopt information systems."
    create_paper(database, title=title, abstract=abstract)
    paper_tokens = estimate_embedding_tokens(
        [title + EMBED_SEPARATOR + abstract], SMALL.token_estimate
    )
    # 只算背景的用量時放得下這一篇；加上一次搜尋（6 個）就超過 80%。
    background_used = 742 - paper_tokens
    assert quota_allows(background_used, paper_tokens, SMALL.quota)
    assert not quota_allows(background_used + QUERY_TOKENS, paper_tokens, SMALL.quota)
    used(db, background_used)
    provider = FakeEmbeddingProvider(dimensions=8, model_id=MODEL)

    search(database, setup_with(QueryVectors()))
    background = embed_papers(
        SystemViewer(),
        database,
        provider,
        SMALL,
        CloudPolicy(),
        code_version=CODE_VERSION,
        clock=lambda: NOW,
    )
    search(database, setup_with(QueryVectors()))  # 超過 80%、還沒到 100%：搜尋照常

    assert background.stop is EmbedStop.QUOTA_REACHED
    assert provider.document_calls == []
    assert query_calls(db) == [("succeeded", 200, QUERY_TOKENS)] * 2


# --- 沒有送出的失敗也記一列（審查 H2） ------------------------------------------------------


def test_quota_lock_timeout_before_the_hold_records_not_sent(
    db: TestDatabase, database: Database
) -> None:
    holder = db.connect(APP_ROLE, autocommit=False)
    holder.execute("SELECT pg_advisory_xact_lock(%s)", (EMBEDDING_QUOTA_LOCK_KEY,))
    provider = QueryVectors()
    try:
        with capture_logs() as logs, pytest.raises(SearchEmbeddingUnavailableError):
            search(database, setup_with(provider, FAST_LOCK))
    finally:
        holder.rollback()
        holder.close()

    assert provider.queries == []
    assert query_calls(db) == [("not_sent", None, 0)]
    assert execute(db, "SELECT count(*) FROM embedding_quota_hold") == [(0,)]
    assert QUERY_TEXT not in repr(logs)


@pytest.fixture
def _no_retry_wait() -> Iterator[None]:
    with stamina.set_testing(True, attempts=100, cap=True):
        yield


class _Transport:
    def __init__(self, reply: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.reply = reply
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.reply(request)


def _workers_ai(transport: _Transport, monotonic: Callable[[], float] | None = None) -> Any:
    extra: dict[str, Any] = {} if monotonic is None else {"monotonic": monotonic}
    return WorkersAiEmbeddingProvider(
        SecretStr(FAKE_ACCOUNT),
        SecretStr(FAKE_TOKEN),
        SMALL.api,
        model_id=MODEL,
        dimensions=DIMENSIONS,
        transport=httpx2.MockTransport(transport),
        **extra,
    )


def _never(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError("不該送出")


@pytest.mark.usefixtures("_no_retry_wait")
def test_adapter_stopping_before_the_first_send_records_not_sent(
    db: TestDatabase, database: Database
) -> None:
    # 建立總時限之後時間就到了：adapter 在任何嘗試之前停下（after(sent=False)），一個請求都沒送。
    readings = iter([0.0])
    transport = _Transport(_never)
    provider = _workers_ai(transport, lambda: next(readings, 1_000_000.0))

    with pytest.raises(SearchEmbeddingUnavailableError):
        search(database, setup_with(provider))

    assert transport.requests == []
    assert query_calls(db) == [("not_sent", None, 0)]
    assert execute(db, "SELECT count(*) FROM embedding_quota_hold") == [(0,)]


# --- 占用的操作種類與中斷（審查 H3） -------------------------------------------------------------


def test_search_hold_is_marked_embed_query_while_waiting_for_the_response(
    db: TestDatabase, database: Database
) -> None:
    provider = Blocking()
    thread, result = _in_thread(lambda: search(database, setup_with(provider)))
    try:
        assert provider.entered.wait(WAIT)
        assert execute(
            db, "SELECT operation, estimated_input_tokens FROM embedding_quota_hold"
        ) == [("embed_query", QUERY_TOKENS)]
    finally:
        provider.release.set()
        thread.join(WAIT)
    assert not thread.is_alive()
    assert not isinstance(result[0], BaseException)


def test_expired_search_hold_is_counted_as_embed_query(
    db: TestDatabase, database: Database
) -> None:
    # 搜尋的行程送出後被砍、沒回來結清：占用過期後，下一次占用把它照 embed_query 轉成用量。
    reserved = NOW - timedelta(hours=1)
    execute(
        db,
        "INSERT INTO embedding_quota_hold (estimated_input_tokens, operation, reserved_at,"
        " expires_at) VALUES (%s, 'embed_query', %s, %s)",
        QUERY_TOKENS,
        reserved,
        reserved + timedelta(minutes=12),
    )

    search(database, setup_with(QueryVectors()))

    rows = execute(
        db,
        "SELECT operation, outcome, http_status, estimated_input_tokens, called_at FROM api_call"
        " ORDER BY called_at",
    )
    assert rows == [
        ("embed_query", None, None, QUERY_TOKENS, reserved),
        ("embed_query", "succeeded", 200, QUERY_TOKENS, NOW),
    ]
    assert execute(db, "SELECT count(*) FROM embedding_quota_hold") == [(0,)]


class _Killed(BaseException):
    """模擬請求已經送出、還沒回應時行程被中斷（不是 Exception，不會被當成一般錯誤接住）。"""


def _killed_after_sending(request: httpx2.Request) -> httpx2.Response:
    raise _Killed()


@pytest.mark.usefixtures("_no_retry_wait")
def test_interrupt_after_sending_is_recorded_as_sent_once(
    db: TestDatabase, database: Database
) -> None:
    # H3 的核對：真的 adapter 的送出包在 try/finally，finally 一定呼叫 after(sent=True)，所以送出後
    # 中斷照「可能已計費」記一列（不會當成沒送出而只放掉占用），也不會再記第二列。
    transport = _Transport(_killed_after_sending)

    with pytest.raises(_Killed):
        search(database, setup_with(_workers_ai(transport)))

    assert len(transport.requests) == 1
    assert query_calls(db) == [("unreachable", None, QUERY_TOKENS)]
    assert execute(db, "SELECT count(*) FROM embedding_quota_hold") == [(0,)]
