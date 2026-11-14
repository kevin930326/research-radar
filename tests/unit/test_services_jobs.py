"""排程工作、`radar enqueue` 的用例與 sweep，全部用替身（不連資料庫）。"""

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest

import radar.services.sweep as sweep_module
from radar.adapters.fake import FakeHeartbeat, FakeJobQueue
from radar.domain.jobs import SWEEP, JobSpec, Queue, UnknownJobError
from radar.domain.sweep import SWEEP_BATCH_LIMIT, SweepEntry
from radar.services.jobs import enqueue_scheduled_job
from radar.services.scheduled import run_scheduled_job
from radar.services.sweep import SweepResult, sweep

ROW_A = uuid.UUID("018f0000-0000-7000-8000-00000000000a")
ROW_B = uuid.UUID("018f0000-0000-7000-8000-00000000000b")
IDENTIFY = JobSpec("identify_upload", Queue.PDF)
ENTRY = SweepEntry(
    table="upload_item",
    status_column="status",
    start_status="received",
    time_column="updated_at",
    job=IDENTIFY,
    stuck_after=timedelta(minutes=30),
)


# --- run_scheduled_job ---------------------------------------------------------------


def test_scheduled_job_success_sends_its_check_once() -> None:
    heartbeat = FakeHeartbeat()

    assert run_scheduled_job("sweep", lambda: 7, heartbeat) == 7
    assert heartbeat.sent == ["sweep"]


def test_scheduled_job_failure_sends_nothing_and_reraises() -> None:
    heartbeat = FakeHeartbeat()

    def fail() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_scheduled_job("sweep", fail, heartbeat)
    assert heartbeat.sent == []


# --- enqueue_scheduled_job -----------------------------------------------------------


def test_enqueue_scheduled_job_puts_job_on_its_queue() -> None:
    queue = FakeJobQueue()

    assert enqueue_scheduled_job("sweep", queue.enqueue) is True
    assert [(item.job, dict(item.ids)) for item in queue.queued] == [(SWEEP, {})]


def test_enqueue_twice_reports_already_queued() -> None:
    queue = FakeJobQueue()

    assert enqueue_scheduled_job("sweep", queue.enqueue) is True
    assert enqueue_scheduled_job("sweep", queue.enqueue) is False
    assert len(queue.queued) == 1


def test_unknown_schedule_does_not_enqueue() -> None:
    queue = FakeJobQueue()

    with pytest.raises(UnknownJobError):
        enqueue_scheduled_job("nope", queue.enqueue)
    assert queue.queued == []


# --- sweep ---------------------------------------------------------------------------


class FakeDatabase:
    """只記交易開了幾次、目前是不是在交易裡。"""

    def __init__(self) -> None:
        self.transactions = 0
        self.in_transaction = False

    @contextmanager
    def transaction(self) -> Iterator[str]:
        self.transactions += 1
        self.in_transaction = True
        try:
            yield "connection"
        finally:
            self.in_transaction = False


@pytest.fixture
def stuck_rows(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[uuid.UUID]]:
    """替換 repo 的查詢：每張表回傳這裡設定的卡住的列。"""
    rows: dict[str, list[uuid.UUID]] = {}

    def find_stuck_ids(connection: Any, entry: SweepEntry, limit: int) -> list[uuid.UUID]:
        assert connection == "connection"
        assert limit == SWEEP_BATCH_LIMIT
        return rows.get(entry.table, [])

    monkeypatch.setattr(sweep_module, "find_stuck_ids", find_stuck_ids)
    return rows


def _sweep(
    database: FakeDatabase,
    queue: FakeJobQueue,
    heartbeat: FakeHeartbeat,
    registry: tuple[SweepEntry, ...] = (ENTRY,),
    enqueue: Any = None,
) -> SweepResult:
    return sweep(
        database,  # type: ignore[arg-type]
        registry,
        enqueue or queue.enqueue,
        queue.retry_stalled,
        heartbeat,
    )


def test_sweep_requeues_stuck_rows_with_table_id_parameter(
    stuck_rows: dict[str, list[uuid.UUID]],
) -> None:
    stuck_rows["upload_item"] = [ROW_A, ROW_B]
    database, queue, heartbeat = FakeDatabase(), FakeJobQueue(stalled=2), FakeHeartbeat()

    result = _sweep(database, queue, heartbeat)

    assert result == SweepResult(stuck_rows=2, requeued=2, retried_jobs=2)
    assert [(item.job, dict(item.ids)) for item in queue.queued] == [
        (IDENTIFY, {"upload_item_id": ROW_A}),
        (IDENTIFY, {"upload_item_id": ROW_B}),
    ]
    assert queue.retry_calls == 1
    assert heartbeat.sent == ["sweep"]


def test_sweep_twice_does_not_add_more(stuck_rows: dict[str, list[uuid.UUID]]) -> None:
    stuck_rows["upload_item"] = [ROW_A]
    database, queue, heartbeat = FakeDatabase(), FakeJobQueue(), FakeHeartbeat()

    _sweep(database, queue, heartbeat)
    again = _sweep(database, queue, heartbeat)

    assert again == SweepResult(stuck_rows=1, requeued=0, retried_jobs=0)
    assert len(queue.queued) == 1
    assert heartbeat.sent == ["sweep", "sweep"]


def test_sweep_enqueues_only_after_transaction_ends(
    stuck_rows: dict[str, list[uuid.UUID]],
) -> None:
    stuck_rows["upload_item"] = [ROW_A]
    database, queue, heartbeat = FakeDatabase(), FakeJobQueue(), FakeHeartbeat()
    seen: list[bool] = []

    def enqueue(job: JobSpec, ids: Mapping[str, uuid.UUID]) -> bool:
        seen.append(database.in_transaction)
        return queue.enqueue(job, ids)

    _sweep(database, queue, heartbeat, enqueue=enqueue)

    assert database.transactions == 1
    assert seen == [False]


def test_empty_registry_skips_database_but_retries_stalled_jobs() -> None:
    database, queue, heartbeat = FakeDatabase(), FakeJobQueue(stalled=1), FakeHeartbeat()

    result = _sweep(database, queue, heartbeat, registry=())

    assert result == SweepResult(stuck_rows=0, requeued=0, retried_jobs=1)
    assert database.transactions == 0
    assert heartbeat.sent == ["sweep"]


def test_enqueue_failure_sends_no_heartbeat(stuck_rows: dict[str, list[uuid.UUID]]) -> None:
    stuck_rows["upload_item"] = [ROW_A]
    database, queue, heartbeat = FakeDatabase(), FakeJobQueue(), FakeHeartbeat()

    def broken(_job: JobSpec, _ids: Mapping[str, uuid.UUID]) -> bool:
        raise ConnectionError("queue down")

    with pytest.raises(ConnectionError):
        _sweep(database, queue, heartbeat, enqueue=broken)
    assert heartbeat.sent == []


def test_retry_stalled_failure_sends_no_heartbeat() -> None:
    heartbeat = FakeHeartbeat()

    def broken() -> int:
        raise ConnectionError("queue down")

    with pytest.raises(ConnectionError):
        sweep(FakeDatabase(), (), FakeJobQueue().enqueue, broken, heartbeat)  # type: ignore[arg-type]
    assert heartbeat.sent == []
