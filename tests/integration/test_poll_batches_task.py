"""worker 的 `poll_batches` 工作（Story 2.11、ADR-0043）對真的 PostgreSQL：沒有批次時不建
provider、健康才送 `poll-batches` 心跳（有錯誤或有 `needs_attention` 不送）、匯入完關掉 provider、
工作層不重試。

不跑 Procrastinate worker；provider 是假的（狀態與結果行不是真的錄的，形狀照官方文件）；環境變數只用
測試值。
"""

from collections.abc import Iterator
from types import TracebackType
from typing import Self

import pytest

import radar.entrypoints.worker.tasks as worker_tasks
from radar.adapters.fake import FakeHeartbeat, FakeLlmProvider
from radar.adapters.fake.jobs import FakeJobQueue
from radar.domain.jobs import POLL_BATCHES
from radar.domain.llm import LlmBatchResult, LlmBatchStatus
from radar.repo.db import Database
from tests.batch_import_support import batch_items, provider_with, submitted_batch, succeeded
from tests.batch_support import BATCH_ID, batch_setup
from tests.database import TestDatabase, migrate
from tests.integration.test_llm_batch_tasks import _parts, _task
from tests.llm_support import execute


class _ClosingFake(FakeLlmProvider):
    """可以 `with` 的假 provider；記下有沒有關，關了以後不能再用。"""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def retrieve_batch(self, provider_batch_id: str) -> LlmBatchStatus:
        assert not self.closed
        return super().retrieve_batch(provider_batch_id)

    def batch_results(self, provider_batch_id: str) -> Iterator[LlmBatchResult]:
        assert not self.closed
        return super().batch_results(provider_batch_id)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _heartbeats(parts: object) -> list[str]:
    beat = parts.send_heartbeat  # type: ignore[attr-defined]
    assert isinstance(beat, FakeHeartbeat)
    return beat.sent


def test_poll_task_without_batches_needs_no_key_and_sends_heartbeat(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[object] = []
    monkeypatch.setattr(worker_tasks, "build_llm_provider", lambda *a: built.append(a))
    parts, _ = _parts(db, monkeypatch, FakeJobQueue())

    _task(parts, POLL_BATCHES.name)()

    assert built == []
    assert _heartbeats(parts) == [POLL_BATCHES.check]


def test_poll_task_imports_and_closes_the_provider(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(db.app_url, connect_timeout=5, pool_size=2)
    submission_id = submitted_batch(database, batch_setup())
    provider = _ClosingFake()
    provider_with(
        [succeeded(item) for item in batch_items(database, submission_id)], provider=provider
    )
    monkeypatch.setattr(worker_tasks, "build_llm_provider", lambda settings: provider)
    parts, _ = _parts(db, monkeypatch, FakeJobQueue())

    _task(parts, POLL_BATCHES.name)()

    assert provider.closed
    assert execute(db, "SELECT status FROM batch_submission") == [("imported",)]
    assert _heartbeats(parts) == [POLL_BATCHES.check]


def test_poll_task_sends_no_heartbeat_while_a_batch_needs_attention(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(db.app_url, connect_timeout=5, pool_size=2)
    submitted_batch(database, batch_setup())
    provider = _ClosingFake()  # 沒準備狀態：查詢回 404 → needs_attention
    monkeypatch.setattr(worker_tasks, "build_llm_provider", lambda settings: provider)
    parts, _ = _parts(db, monkeypatch, FakeJobQueue())

    _task(parts, POLL_BATCHES.name)()

    assert execute(db, "SELECT status, provider_batch_id FROM batch_submission") == [
        ("needs_attention", BATCH_ID)
    ]
    assert _heartbeats(parts) == []
    # 下一次 cron：沒有 submitted／ended 要查，但 needs_attention 還在，照樣不送。
    _task(parts, POLL_BATCHES.name)()
    assert _heartbeats(parts) == []


def test_poll_is_scheduled_on_the_ai_queue_and_never_retried(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts, _ = _parts(db, monkeypatch, FakeJobQueue(), job_max_retries=3)
    task = parts.job_app.app.tasks[POLL_BATCHES.name]

    assert task.retry_strategy is None
    assert task.queue == "ai"
    assert POLL_BATCHES.schedule == "poll-batches"
    assert POLL_BATCHES.lock == "poll_batches"
