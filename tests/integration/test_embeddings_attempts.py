"""向量化的每次嘗試記帳、額度占用、鎖等待上限與 `--limit` 的重送計數，對真的 PostgreSQL
（Story 2.8 審查後的修正：Codex 高 1、中 2、3、5、7）。

1. 每次 HTTP 嘗試（含 adapter 內部的重試）各記一筆 `api_call`，日期是那次嘗試當下的 UTC 日期；
   每次嘗試前都檢查額度（真的 adapter 接 `httpx2.MockTransport`，假回應不是真的錄的，形狀照官方
   schema）。占用後、第一次送出前跨過 UTC 午夜時照新的一天重查（Codex 複查 C1）；第一次嘗試前
   總時限就到了不記請求與用量、放掉占用（C2）。
2. 送出前先占住估計額度：兩個 worker 同時接近上限時，只有放得下的那個送出；過期的占用轉成用量。
3. 取得執行資格與寫入時等論文那一列的鎖有上限（`lock_timeout_seconds`）。
5. `--limit` 不會被重送時才略過的篇數抵銷。
7. 轉成 halfvec 後全是 0 的向量當成回應不合格，不寫入；只讓那一篇失敗，其他篇照寫（Opus 確認低 3）。

假模型 `fake-embedding`（8 維）由超級使用者登記；標題、摘要、account id、token 都是測試自己編的。
"""

import json
import threading
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
import stamina
from pydantic import SecretStr

from radar.adapters.fake import FakeEmbeddingProvider
from radar.adapters.workers_ai import WorkersAiEmbeddingProvider
from radar.domain.embeddings import (
    EmbeddingConfig,
    EmbeddingRequestError,
    EmbedRunResult,
    EmbedStop,
    estimate_embedding_tokens,
)
from radar.domain.errors import ErrorCode, LockWaitTimeoutError
from radar.domain.policy import CloudSafeText, cloud_text
from radar.repo.db import EMBEDDING_QUOTA_LOCK_KEY, Database
from radar.services.embeddings import embed_papers
from tests.database import APP_ROLE, TestDatabase
from tests.integration.test_embeddings import (
    CONFIG,
    DIMENSIONS,
    MODEL,
    NOW,
    POLICY,
    SYSTEM,
    _calls,
    _config,
    _embeddings,
    _insert_usage,
    _paper,
    _runs,
    _sha,
    _tiny_quota,
    database,
    db,
)
from tests.llm_support import CODE_VERSION, execute

__all__ = ["database", "db"]  # pytest 的 fixture 從 test_embeddings 借來

FAKE_ACCOUNT = "0123456789abcdef0123456789abcdef"
FAKE_TOKEN = "fake-token-for-tests"
WAIT = 20.0
DAY_LIMIT = 930  # _tiny_quota()：每天 1 neuron、到 100% 才停 ≈ 930 token


class _Clock:
    """可以在測試中途撥動的時鐘（transport 裡撥，模擬跨過 UTC 午夜的重試）。"""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _run(
    database: Database,
    provider: Any,
    *,
    config: EmbeddingConfig = CONFIG,
    clock: Any = None,
    **kwargs: Any,
) -> EmbedRunResult:
    return embed_papers(
        SYSTEM,
        database,
        provider,
        config,
        POLICY,
        code_version=CODE_VERSION,
        clock=clock if clock is not None else (lambda: NOW),
        **kwargs,
    )


def _text(n: int) -> str:
    return f"Paper {n} on platform adoption\n\nAbstract {n}: teams adopt platforms."


def _tokens(*papers: int) -> int:
    return estimate_embedding_tokens([_text(n) for n in papers], CONFIG.token_estimate)


def _holds(db: TestDatabase) -> int:
    return int(execute(db, "SELECT count(*) FROM embedding_quota_hold")[0][0])


# --- 1. 每次嘗試記帳（真的 adapter 接 MockTransport） ---------------------------------------


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Iterator[None]:
    with stamina.set_testing(True, attempts=100, cap=True):
        yield


def _ok(request: httpx2.Request) -> httpx2.Response:
    """假回應（不是真的錄的，形狀照官方 schema）：每段一個 8 維的固定向量。"""
    count = len(json.loads(request.content)["documents"])
    data = [[(row + 1) / 10 + column / 100 for column in range(DIMENSIONS)] for row in range(count)]
    envelope = {
        "result": {"data": data, "shape": [count, DIMENSIONS]},
        "success": True,
        "errors": [],
        "messages": [],
    }
    return httpx2.Response(200, json=envelope)


class _Transport:
    """依序回覆（`Exception` 就丟出、`None` 就回 `_ok`）；回覆前可以跑 `on_request`。"""

    def __init__(
        self, *replies: httpx2.Response | Exception | None, on_request: Any = None
    ) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []
        self.on_request = on_request

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(len(self.requests))
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, Exception):
            raise reply
        return reply if reply is not None else _ok(request)


def _workers_ai(transport: _Transport, config: EmbeddingConfig = CONFIG) -> Any:
    return WorkersAiEmbeddingProvider(
        SecretStr(FAKE_ACCOUNT),
        SecretStr(FAKE_TOKEN),
        config.api,
        model_id=MODEL,
        dimensions=DIMENSIONS,
        transport=httpx2.MockTransport(transport),
    )


def test_timeout_then_success_records_both_attempts(db: TestDatabase, database: Database) -> None:
    # 第一次讀取逾時（Cloudflare 可能已經算了 neurons）、第二次成功：兩次都記估計 token。
    _paper(database, 1)
    transport = _Transport(httpx2.ReadTimeout("slow"))

    result = _run(database, _workers_ai(transport))

    assert len(transport.requests) == 2
    tokens = _tokens(1)
    assert [(row[1], row[3], row[4]) for row in _calls(db)] == [
        (None, tokens, NOW),
        (200, tokens, NOW),
    ]
    assert result.embedded == 1
    assert result.requests == 2
    assert result.estimated_tokens == 2 * tokens
    assert _holds(db) == 0


def test_retry_across_utc_midnight_is_recorded_and_checked_on_its_own_day(
    db: TestDatabase, database: Database
) -> None:
    # 11/15 的額度只剩剛好一次嘗試：第一次（23:59:55）逾時；重試時已經是 11/16 00:00:10，
    # 照 11/16 的額度檢查（放得下）、記在 11/16。照第一次的日期算就會被擋下，也會漏記新的一天。
    _paper(database, 1)
    tokens = _tokens(1)
    first = datetime(2026, 11, 15, 23, 59, 55, tzinfo=UTC)
    second = datetime(2026, 11, 16, 0, 0, 10, tzinfo=UTC)
    _insert_usage(db, DAY_LIMIT - tokens, datetime(2026, 11, 15, 1, 0, tzinfo=UTC))
    clock = _Clock(first)

    def cross_midnight(number: int) -> None:
        if number == 1:
            clock.now = second

    transport = _Transport(httpx2.ReadTimeout("slow"), on_request=cross_midnight)

    result = _run(database, _workers_ai(transport), config=_tiny_quota(), clock=clock)

    assert result.stop is EmbedStop.COMPLETED
    assert result.embedded == 1
    assert [(row[1], row[3], row[4]) for row in _calls(db)][1:] == [
        (None, tokens, first),
        (200, tokens, second),
    ]


def test_quota_is_checked_before_every_retry(db: TestDatabase, database: Database) -> None:
    # 今天只剩一次嘗試的額度：第一次 503，重試前占不到額度 → 不送第二次，停在 quota_reached
    # （不算失敗），這批的 run 改成 failed（embed_quota_reached），下一次續跑。
    _paper(database, 1)
    tokens = _tokens(1)
    _insert_usage(db, DAY_LIMIT - tokens, datetime(2026, 11, 15, 1, 0, tzinfo=UTC))
    transport = _Transport(httpx2.Response(503))

    result = _run(database, _workers_ai(transport), config=_tiny_quota())

    assert len(transport.requests) == 1
    assert result.stop is EmbedStop.QUOTA_REACHED
    assert not result.has_failures
    assert [(row[1], row[3]) for row in _calls(db)][1:] == [(503, tokens)]
    assert [(row[1], row[2]) for row in _runs(db)] == [("failed", "embed_quota_reached")]
    assert _embeddings(db) == {}
    assert _holds(db) == 0


def test_configuration_error_attempt_records_zero_tokens(
    db: TestDatabase, database: Database
) -> None:
    _paper(database, 1)
    transport = _Transport(httpx2.Response(403))

    with pytest.raises(Exception, match="embed_configuration"):
        _run(database, _workers_ai(transport))

    assert [(row[1], row[3]) for row in _calls(db)] == [(403, 0)]
    assert _holds(db) == 0


class _ClockJumpsBeforeSending(WorkersAiEmbeddingProvider):
    """取得資格之後、adapter 第一次送出之前把時鐘撥到 `jump_to`（模擬這段期間跨過 UTC 午夜）。"""

    def __init__(
        self,
        transport: _Transport,
        clock: _Clock,
        jump_to: datetime,
        on_jump: Any = None,
    ) -> None:
        super().__init__(
            SecretStr(FAKE_ACCOUNT),
            SecretStr(FAKE_TOKEN),
            CONFIG.api,
            model_id=MODEL,
            dimensions=DIMENSIONS,
            transport=httpx2.MockTransport(transport),
        )
        self.clock = clock
        self.jump_to = jump_to
        self.on_jump = on_jump

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        self.clock.now = self.jump_to
        if self.on_jump is not None:
            self.on_jump()
        return super().embed_documents(texts)


BEFORE_MIDNIGHT = datetime(2026, 11, 15, 23, 59, 58, tzinfo=UTC)
AFTER_MIDNIGHT = datetime(2026, 11, 16, 0, 0, 5, tzinfo=UTC)


def test_crossing_utc_midnight_before_the_first_send_rechecks_the_full_new_day(
    db: TestDatabase, database: Database
) -> None:
    # 11/15 23:59:58 占用成功（還有一次的空間）；取得資格後跨過午夜，11/16 的額度已經被別的工作
    # 用滿：第一次送出前照 11/16 重查，放不下就不送（沿用 11/15 的占用會送出、記在 11/16、突破當日
    # 門檻）。
    _paper(database, 1)
    tokens = _tokens(1)
    _insert_usage(db, DAY_LIMIT - tokens, datetime(2026, 11, 15, 1, 0, tzinfo=UTC))
    clock = _Clock(BEFORE_MIDNIGHT)
    transport = _Transport()

    def other_job_fills_the_new_day() -> None:
        _insert_usage(db, DAY_LIMIT, datetime(2026, 11, 16, 0, 0, 1, tzinfo=UTC))

    result = _run(
        database,
        _ClockJumpsBeforeSending(transport, clock, AFTER_MIDNIGHT, other_job_fills_the_new_day),
        config=_tiny_quota(),
        clock=clock,
    )

    assert transport.requests == []
    assert result.stop is EmbedStop.QUOTA_REACHED
    assert not result.has_failures
    assert result.requests == 0
    assert len(_calls(db)) == 2  # 只有事先寫的兩天用量，沒有新的請求
    assert [(row[1], row[2]) for row in _runs(db)] == [("failed", "embed_quota_reached")]
    assert _embeddings(db) == {}
    assert _holds(db) == 0  # 11/15 的占用放掉了，不記用量


def test_crossing_utc_midnight_before_the_first_send_holds_and_records_on_the_new_day(
    db: TestDatabase, database: Database
) -> None:
    # 對照：11/16 還有空間：換成 11/16 的占用、送出，記在 11/16；11/15 不多記任何用量。
    _paper(database, 1)
    tokens = _tokens(1)
    _insert_usage(db, DAY_LIMIT - tokens, datetime(2026, 11, 15, 1, 0, tzinfo=UTC))
    clock = _Clock(BEFORE_MIDNIGHT)
    transport = _Transport()

    result = _run(
        database,
        _ClockJumpsBeforeSending(transport, clock, AFTER_MIDNIGHT),
        config=_tiny_quota(),
        clock=clock,
    )

    assert len(transport.requests) == 1
    assert result.embedded == 1
    assert [(row[1], row[3], row[4]) for row in _calls(db)][1:] == [(200, tokens, AFTER_MIDNIGHT)]
    assert _holds(db) == 0


def test_total_timeout_before_the_first_attempt_records_nothing_and_releases_the_hold(
    db: TestDatabase, database: Database
) -> None:
    # 建立總時限之後執行緒停住，回來時總時限已到：adapter 在任何回呼之前停下、一個請求都沒送。
    # 不能把第 5 步的占用當成「已送出、可能計費」結清：不記 api_call、不算請求與用量、占用放掉。
    _paper(database, 1)
    readings = iter([0.0])

    def monotonic() -> float:
        return next(readings, 1_000_000.0)

    transport = _Transport()
    provider = WorkersAiEmbeddingProvider(
        SecretStr(FAKE_ACCOUNT),
        SecretStr(FAKE_TOKEN),
        CONFIG.api,
        model_id=MODEL,
        dimensions=DIMENSIONS,
        transport=httpx2.MockTransport(transport),
        monotonic=monotonic,
    )

    result = _run(database, provider)

    assert transport.requests == []
    assert result.stop is EmbedStop.FAILED
    assert result.failure_reason is ErrorCode.EMBED_TRANSIENT
    assert result.requests == 0
    assert result.estimated_tokens == 0
    assert _calls(db) == []
    assert _holds(db) == 0
    assert [(row[1], row[2]) for row in _runs(db)] == [("failed", "embed_transient")]


# --- 2. 額度占用 -----------------------------------------------------------------------------


class _Gated(FakeEmbeddingProvider):
    """停在「等回應」，直到 `release`。"""

    def __init__(self) -> None:
        super().__init__(DIMENSIONS, model_id=MODEL)
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        vectors = super().embed_documents(texts)
        self.entered.set()
        if not self.release.wait(WAIT):
            raise AssertionError("等不到 release")
        return vectors


class _Thread:
    def __init__(self, target: Any) -> None:
        self.result: EmbedRunResult | None = None
        self.error: BaseException | None = None

        def run() -> None:
            try:
                self.result = target()
            except BaseException as exc:
                self.error = exc

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self) -> EmbedRunResult:
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "執行緒沒有在時限內結束"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _near_the_limit(db: TestDatabase, database: Database) -> int:
    """4 篇論文（一批 2 篇）；今天的用量只剩一批的空間。回一批的估計 token。"""
    for n in (1, 2, 3, 4):
        _paper(database, n)
    batch = _tokens(1, 2)
    assert _tokens(3, 4) == batch
    _insert_usage(db, DAY_LIMIT - batch - 1, datetime(2026, 11, 15, 1, 0, tzinfo=UTC))
    return batch


def test_a_worker_waiting_for_its_response_holds_its_quota(
    db: TestDatabase, database: Database
) -> None:
    # A 已經送出、還在等回應（帳上還沒有 api_call）：B 照「已用＋A 的占用」判斷，放不下就不送。
    _near_the_limit(db, database)
    a_provider = _Gated()
    a = _Thread(lambda: _run(database, a_provider, config=_tiny_quota()))
    assert a_provider.entered.wait(WAIT)
    assert _holds(db) == 1

    b_provider = FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL)
    b_result = _run(database, b_provider, config=_tiny_quota())

    assert b_result.stop is EmbedStop.QUOTA_REACHED
    assert b_provider.document_calls == []
    a_provider.release.set()
    a_result = a.join()
    assert a_result.embedded == 2
    assert a_result.stop is EmbedStop.QUOTA_REACHED  # 第二批也放不下
    used = sum(row[3] for row in _calls(db))
    assert used <= DAY_LIMIT
    assert _holds(db) == 0


def test_two_workers_starting_together_near_the_limit_send_only_one_batch(
    db: TestDatabase, database: Database
) -> None:
    _near_the_limit(db, database)
    gate = threading.Barrier(2)
    providers = [FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL) for _ in range(2)]

    def start(provider: FakeEmbeddingProvider) -> Any:
        def target() -> EmbedRunResult:
            gate.wait(WAIT)
            return _run(database, provider, config=_tiny_quota())

        return target

    workers = [_Thread(start(provider)) for provider in providers]
    results = [worker.join() for worker in workers]

    assert sorted(len(provider.document_calls) for provider in providers) == [0, 1]
    assert {result.stop for result in results} == {EmbedStop.QUOTA_REACHED}
    assert sum(result.embedded for result in results) == 2
    assert sum(row[3] for row in _calls(db)) <= DAY_LIMIT
    assert _holds(db) == 0


def test_expired_hold_is_counted_as_used_on_its_day(db: TestDatabase, database: Database) -> None:
    # 行程中斷留下的占用：過了有效期限，下一次占用照估計轉成那天的用量（當作已計費）。
    _paper(database, 1)
    reserved = NOW - timedelta(hours=1)
    execute(
        db,
        "INSERT INTO embedding_quota_hold (estimated_input_tokens, reserved_at, expires_at)"
        " VALUES (%s, %s, %s)",
        DAY_LIMIT,
        reserved,
        NOW - timedelta(minutes=1),
    )
    provider = FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL)

    result = _run(database, provider, config=_tiny_quota())

    assert result.stop is EmbedStop.QUOTA_REACHED
    assert provider.document_calls == []
    assert [(row[1], row[3], row[4]) for row in _calls(db)] == [(None, DAY_LIMIT, reserved)]
    assert _holds(db) == 0


def test_live_hold_of_another_worker_is_counted(db: TestDatabase, database: Database) -> None:
    _paper(database, 1)
    execute(
        db,
        "INSERT INTO embedding_quota_hold (estimated_input_tokens, reserved_at, expires_at)"
        " VALUES (%s, %s, %s)",
        DAY_LIMIT,
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=10),
    )
    provider = FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL)

    result = _run(database, provider, config=_tiny_quota())

    assert result.stop is EmbedStop.QUOTA_REACHED
    assert provider.document_calls == []
    assert _calls(db) == []
    assert _holds(db) == 1


def test_hold_is_released_when_nothing_is_claimed(db: TestDatabase, database: Database) -> None:
    # 挑中的論文在取得資格時都被別的工作拿走：什麼都沒送，占用放掉、不記用量。
    busy = _paper(database, 1)
    execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, model, input_sha256, input_kind, params,"
        " code_version, status, call_mode) VALUES (%s, 'embed', %s, %s, 'abstract', '{}',"
        " 'test', 'running', 'interactive')",
        busy,
        MODEL,
        _sha("Paper 1 on platform adoption", "Abstract 1: teams adopt platforms."),
    )

    result = _run(database, FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL))

    assert result.busy == 1
    assert result.requests == 0
    assert _calls(db) == []
    assert _holds(db) == 0


class _HoldExpiresWhileCalling(FakeEmbeddingProvider):
    """等回應時，自己的占用過期、被另一次執行的占用轉成用量（模擬回應慢到超過占用期限）。"""

    def __init__(self, db: TestDatabase, database: Database) -> None:
        super().__init__(DIMENSIONS, model_id=MODEL)
        self.db = db
        self.database = database
        self.other: EmbedRunResult | None = None

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        vectors = super().embed_documents(texts)
        execute(
            self.db,
            "UPDATE embedding_quota_hold SET reserved_at = reserved_at - interval '20 minutes',"
            " expires_at = reserved_at - interval '10 minutes'",
        )
        # 另一次執行：這篇正在跑（busy、不送），但它的占用會先把過期的占用轉成用量。
        self.other = _run(self.database, FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL))
        return vectors


def test_settling_a_hold_already_counted_as_used_records_nothing_more(
    db: TestDatabase, database: Database
) -> None:
    # ADR-0040 決定 11：占用被轉成用量之後，原本的 worker 才回來結清，只刪不記（不重複算），這次的
    # 報表也不計入這個請求（報表和帳對得上）。
    _paper(database, 1)
    provider = _HoldExpiresWhileCalling(db, database)

    result = _run(database, provider)

    assert provider.other is not None
    assert provider.other.busy == 1
    assert [(row[1], row[3]) for row in _calls(db)] == [(None, _tokens(1))]
    assert result.embedded == 1
    assert result.requests == 0
    assert result.estimated_tokens == 0
    assert _holds(db) == 0


# --- 3. 等鎖的上限 ---------------------------------------------------------------------------

SHORT_LOCK = _config(lock_timeout_seconds=0.2)


def test_claim_does_not_wait_forever_for_a_locked_paper(
    db: TestDatabase, database: Database
) -> None:
    locked = _paper(database, 1)
    other = _paper(database, 2)
    with db.connect(APP_ROLE, autocommit=False) as holder:
        holder.execute("SELECT id FROM paper WHERE id = %s FOR UPDATE", (locked,))
        provider = FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL)

        result = _run(database, provider, config=SHORT_LOCK)

        holder.rollback()

    assert result.busy == 1
    assert result.embedded == 1
    assert not result.has_failures
    assert set(_embeddings(db)) == {other}
    assert [row[0] for row in _runs(db)] == [other]
    # 下一次補上。
    assert _run(database, FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL)).embedded == 1


class _LocksPaperWhileCalling(FakeEmbeddingProvider):
    """送出後、寫入前，另一條連線鎖住某一篇論文（測完才放）。"""

    def __init__(self, db: TestDatabase, paper_id: uuid.UUID) -> None:
        super().__init__(DIMENSIONS, model_id=MODEL)
        self.holder = db.connect(APP_ROLE, autocommit=False)
        self.paper_id = paper_id

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        vectors = super().embed_documents(texts)
        self.holder.execute("SELECT id FROM paper WHERE id = %s FOR UPDATE", (self.paper_id,))
        return vectors


def test_write_that_cannot_get_the_paper_lock_fails_only_that_paper(
    db: TestDatabase, database: Database
) -> None:
    locked = _paper(database, 1)
    other = _paper(database, 2)
    provider = _LocksPaperWhileCalling(db, locked)
    try:
        result = _run(database, provider, config=SHORT_LOCK)
    finally:
        provider.holder.rollback()
        provider.holder.close()

    assert result.embedded == 1
    assert result.failed == 1
    assert result.failure_reason is ErrorCode.EMBED_LOCK_TIMEOUT
    assert result.has_failures
    assert set(_embeddings(db)) == {other}
    statuses = {row[0]: (row[1], row[2]) for row in _runs(db)}
    assert statuses == {locked: ("failed", "embed_lock_timeout"), other: ("succeeded", None)}


def _hold_quota_lock(db: TestDatabase) -> Any:
    """另一條連線拿著額度鎖不放（交易級；呼叫端 rollback 才放）。"""
    holder = db.connect(APP_ROLE, autocommit=False)
    holder.execute("SELECT pg_advisory_xact_lock(%s)", (EMBEDDING_QUOTA_LOCK_KEY,))
    return holder


def test_quota_lock_timeout_before_a_batch_fails_it_and_goes_on_to_the_next(
    db: TestDatabase, database: Database
) -> None:
    # 一批一篇：兩批都等不到額度鎖 → 各自算失敗（還沒有 run）、不送，不會中止整次執行。
    for n in (1, 2):
        _paper(database, n)
    provider = FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL)
    holder = _hold_quota_lock(db)
    try:
        result = _run(database, provider, config=_config(lock_timeout_seconds=0.2, batch_size=1))
    finally:
        holder.rollback()
        holder.close()

    assert provider.document_calls == []
    assert result.stop is EmbedStop.COMPLETED
    assert result.failed == 2
    assert result.failure_reason is ErrorCode.EMBED_QUOTA_LOCK_TIMEOUT
    assert result.has_failures
    assert _runs(db) == []
    assert _calls(db) == []
    assert _holds(db) == 0
    # 鎖放開後下一次補上。
    assert _run(database, FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL)).embedded == 2


class _TakesQuotaLockAfterFirstRequest(WorkersAiEmbeddingProvider):
    """第一個請求（回 503）送出後，另一條連線拿走額度鎖；重試前等不到鎖時放開它（讓後面的論文
    照做）。"""

    def __init__(self, db: TestDatabase, config: EmbeddingConfig) -> None:
        self.db = db
        self.holder: Any = None
        self.lock_timeouts = 0
        self.transport = _Transport(httpx2.Response(503), on_request=self._take)
        super().__init__(
            SecretStr(FAKE_ACCOUNT),
            SecretStr(FAKE_TOKEN),
            config.api,
            model_id=MODEL,
            dimensions=DIMENSIONS,
            transport=httpx2.MockTransport(self.transport),
        )

    def _take(self, number: int) -> None:
        if number == 1:
            self.holder = _hold_quota_lock(self.db)

    def release(self) -> None:
        if self.holder is not None:
            self.holder.rollback()
            self.holder.close()
            self.holder = None

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        try:
            return super().embed_documents(texts)
        except LockWaitTimeoutError:
            self.lock_timeouts += 1
            self.release()
            raise


def test_quota_lock_timeout_before_a_retry_fails_only_that_batch(
    db: TestDatabase, database: Database
) -> None:
    # 一批一篇：第 1 篇第一次 503，送出後另一條連線拿走額度鎖；重試前等不到 → 這批的 run 改成
    # failed（embed_quota_lock_timeout），不再送；第一次嘗試照記。第 2 篇在鎖放開後照做。
    for n in (1, 2):
        _paper(database, n)
    config = _config(lock_timeout_seconds=0.2, batch_size=1)
    provider = _TakesQuotaLockAfterFirstRequest(db, config)
    try:
        result = _run(database, provider, config=config)
    finally:
        provider.release()

    assert provider.lock_timeouts == 1
    assert len(provider.transport.requests) == 2  # 第 1 篇的第一次、第 2 篇
    assert result.stop is EmbedStop.COMPLETED
    assert result.embedded == 1
    assert result.failed == 1
    assert result.failure_reason is ErrorCode.EMBED_QUOTA_LOCK_TIMEOUT
    assert result.has_failures
    assert [(row[1], row[3]) for row in _calls(db)] == [(503, _tokens(1)), (200, _tokens(2))]
    assert sorted((row[1], row[2] or "") for row in _runs(db)) == [
        ("failed", "embed_quota_lock_timeout"),
        ("succeeded", ""),
    ]
    assert _holds(db) == 0


def _wait_for_quota_lock_with_statement_timeout(database: Database) -> None:
    with database.transaction_with_lock_timeout(5_000) as connection:
        connection.exec_driver_sql("SET LOCAL statement_timeout = '100ms'")
        Database.transaction_lock(connection, EMBEDDING_QUOTA_LOCK_KEY)


def _divide_by_zero(database: Database) -> None:
    with database.transaction_with_lock_timeout(5_000) as connection:
        connection.exec_driver_sql("SELECT 1 / 0")


def test_statement_timeout_while_waiting_for_a_lock_counts_as_lock_timeout(
    db: TestDatabase, database: Database
) -> None:
    # 57014（statement_timeout 比 lock_timeout 先到、等鎖的那一句被取消）也算等鎖逾時；
    # 其他錯誤原樣往上丟。
    holder = _hold_quota_lock(db)
    try:
        with pytest.raises(LockWaitTimeoutError):
            _wait_for_quota_lock_with_statement_timeout(database)
    finally:
        holder.rollback()
        holder.close()

    with pytest.raises(Exception, match="division by zero") as caught:
        _divide_by_zero(database)
    assert not isinstance(caught.value, LockWaitTimeoutError)


# --- 5. `--limit` 與重送 ---------------------------------------------------------------------


class _RejectsFirstBatchWhileAnotherWorkerFinishesIt(FakeEmbeddingProvider):
    """第一批回請求不合法（400）；同時「另一個 worker」把這兩篇做好（直接寫成功的 run 與向量）。"""

    def __init__(self, db: TestDatabase, papers: dict[uuid.UUID, str]) -> None:
        super().__init__(DIMENSIONS, model_id=MODEL)
        self.db = db
        self.papers = papers

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        vectors = super().embed_documents(texts)
        if len(self.document_calls) > 1:
            return vectors
        vector = "[" + ",".join(["0.5"] * DIMENSIONS) + "]"
        for paper_id, sha in self.papers.items():
            [(run_id,)] = execute(
                self.db,
                "INSERT INTO derivation_run (paper_id, task, model, input_sha256, input_kind,"
                " params, code_version, status, call_mode, finished_at) VALUES (%s, 'embed', %s,"
                " %s, 'abstract', '{}', 'other-worker', 'succeeded', 'interactive', now())"
                " RETURNING id",
                paper_id,
                MODEL,
                sha,
            )
            execute(
                self.db,
                "INSERT INTO embedding (item_type, item_id, model_id, dimensions, vector,"
                " input_sha256, run_id) VALUES ('paper', %s, %s, %s, %s::halfvec, %s, %s)",
                paper_id,
                MODEL,
                DIMENSIONS,
                vector,
                sha,
                run_id,
            )
        raise EmbeddingRequestError("HTTP 400", http_status=400)


def test_limit_is_not_given_back_by_papers_skipped_when_resending(
    db: TestDatabase, database: Database
) -> None:
    # --limit 2：送出的兩篇被整批拒收，另一個 worker 隨後做好這兩篇；一篇一篇重送時才發現「沒變」，
    # 這兩篇已經送過一次，仍算這次送的篇數：不會因此再送第 3、4 篇。
    ids = [_paper(database, n) for n in (1, 2, 3, 4)]
    shas = {
        ids[n - 1]: _sha(f"Paper {n} on platform adoption", f"Abstract {n}: teams adopt platforms.")
        for n in (1, 2)
    }
    provider = _RejectsFirstBatchWhileAnotherWorkerFinishesIt(db, shas)

    result = _run(database, provider, limit=2)

    assert [len(call) for call in provider.document_calls] == [2]
    assert result.stop is EmbedStop.LIMIT_REACHED
    assert result.unchanged == 2
    assert set(_embeddings(db)) == set(shas)
    assert ids[2] not in _embeddings(db)
    assert ids[3] not in _embeddings(db)


def test_papers_skipped_at_the_first_claim_still_do_not_count_toward_the_limit(
    db: TestDatabase, database: Database
) -> None:
    # 對照：第一次取得資格時就略過的（正在跑）照舊不算進 limit。
    busy = _paper(database, 1)
    for n in (2, 3, 4):
        _paper(database, n)
    execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, model, input_sha256, input_kind, params,"
        " code_version, status, call_mode) VALUES (%s, 'embed', %s, %s, 'abstract', '{}',"
        " 'test', 'running', 'interactive')",
        busy,
        MODEL,
        _sha("Paper 1 on platform adoption", "Abstract 1: teams adopt platforms."),
    )

    result = _run(database, FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL), limit=2)

    assert result.stop is EmbedStop.LIMIT_REACHED
    assert result.busy == 1
    assert result.embedded == 2


# --- 7. 轉成 halfvec 後全是 0 ----------------------------------------------------------------


class _Vectors(FakeEmbeddingProvider):
    def __init__(self, value: float) -> None:
        super().__init__(DIMENSIONS, model_id=MODEL)
        self.value = value

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        super().embed_documents(texts)
        return [[self.value] * DIMENSIONS for _ in texts]


@pytest.mark.parametrize("value", [0.0, 1e-9])
def test_vectors_that_are_zero_at_halfvec_precision_are_not_written(
    db: TestDatabase, database: Database, value: float
) -> None:
    _paper(database, 1)

    result = _run(database, _Vectors(value))

    # 低 3 之後只讓全 0 的那一篇失敗、不停下（原本整批失敗、停在 failed）。
    assert result.stop is EmbedStop.COMPLETED
    assert result.has_failures
    assert result.failed == 1
    assert result.failure_reason is ErrorCode.EMBED_RESPONSE_INVALID
    assert _embeddings(db) == {}
    assert [(row[1], row[2]) for row in _runs(db)] == [("failed", "embed_response_invalid")]
    # 沒有寫入，下一次（回應正常時）照樣會再做這一篇，不會被永久略過。
    assert _run(database, FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL)).embedded == 1


class _ZeroFor(FakeEmbeddingProvider):
    """指定的那篇（照文字開頭）每次都回全 0 的向量，其他篇正常。"""

    def __init__(self, prefix: str) -> None:
        super().__init__(DIMENSIONS, model_id=MODEL)
        self.prefix = prefix

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        vectors = super().embed_documents(texts)
        return [
            [0.0] * DIMENSIONS if cloud_text(text).startswith(self.prefix) else vector
            for text, vector in zip(texts, vectors, strict=True)
        ]


def test_a_paper_that_always_gets_a_zero_vector_fails_alone_and_does_not_block_the_rest(
    db: TestDatabase, database: Database
) -> None:
    # 第 1 篇每次都拿到全 0：只有它失敗，同一批的第 2 篇與後面的第 3 篇照寫（原本整批失敗、
    # 下一次照 id 順序又挑到同一批，回填永遠卡在這裡）。
    ids = [_paper(database, n) for n in (1, 2, 3)]

    first = _run(database, _ZeroFor("Paper 1 "))

    assert first.stop is EmbedStop.COMPLETED
    assert first.embedded == 2
    assert first.failed == 1
    assert first.failure_reason is ErrorCode.EMBED_RESPONSE_INVALID
    assert first.has_failures
    assert set(_embeddings(db)) == {ids[1], ids[2]}
    statuses = {row[0]: (row[1], row[2]) for row in _runs(db)}
    assert statuses[ids[0]] == ("failed", "embed_response_invalid")

    # 下一次：第 1 篇還是全 0、還是失敗，其他篇沒變（不送），不會卡住。
    second = _run(database, _ZeroFor("Paper 1 "))
    assert second.embedded == 0
    assert second.failed == 1
    assert second.unchanged == 2
