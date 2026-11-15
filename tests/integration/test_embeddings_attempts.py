"""向量化的每次嘗試記帳、額度占用、鎖等待上限與 `--limit` 的重送計數，對真的 PostgreSQL
（Story 2.8 審查後的修正：Codex 高 1、中 2、3、5、7）。

1. 每次 HTTP 嘗試（含 adapter 內部的重試）各記一筆 `api_call`，日期是那次嘗試當下的 UTC 日期；
   每次嘗試前都檢查額度（真的 adapter 接 `httpx2.MockTransport`，假回應不是真的錄的，形狀照官方
   schema）。
2. 送出前先占住估計額度：兩個 worker 同時接近上限時，只有放得下的那個送出；過期的占用轉成用量。
3. 取得執行資格與寫入時等論文那一列的鎖有上限（`lock_timeout_seconds`）。
5. `--limit` 不會被重送時才略過的篇數抵銷。
7. 轉成 halfvec 後全是 0 的向量當成回應不合格，不寫入。

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
from radar.domain.errors import ErrorCode
from radar.domain.policy import CloudSafeText
from radar.repo.db import Database
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

    assert result.stop is EmbedStop.FAILED
    assert result.failure_reason is ErrorCode.EMBED_RESPONSE_INVALID
    assert _embeddings(db) == {}
    assert [(row[1], row[2]) for row in _runs(db)] == [("failed", "embed_response_invalid")]
    # 沒有寫入，下一次（回應正常時）照樣會再做這一篇，不會被永久略過。
    assert _run(database, FakeEmbeddingProvider(DIMENSIONS, model_id=MODEL)).embedded == 1
