"""佇列 adapter：排入的選項、工作的簽名檢查與參數轉換（用 Procrastinate 的 InMemoryConnector）。"""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import procrastinate
import pytest
from procrastinate import exceptions as procrastinate_exceptions
from procrastinate.jobs import Job
from procrastinate.testing import InMemoryConnector

from radar.adapters.jobs import (
    InvalidJobSignatureError,
    check_job_signature,
    create_job_app,
    defer_job,
    dumps_job_args,
    libpq_conninfo,
    register_job,
    retry_stalled_jobs,
)
from radar.domain.jobs import PDF_LOCK, SWEEP, JobSpec, Queue

ITEM = uuid.UUID("018f0000-0000-7000-8000-0000000000aa")
IDENTIFY = JobSpec("identify_upload", Queue.PDF)
EMBED = JobSpec("embed_paper", Queue.AI)


@pytest.fixture
def app() -> procrastinate.App:
    return procrastinate.App(connector=InMemoryConnector())


def _jobs(app: procrastinate.App) -> list[dict[str, Any]]:
    connector = app.connector
    assert isinstance(connector, InMemoryConnector)
    return list(connector.jobs.values())


def test_defer_sets_queue_queueing_lock_and_string_ids(app: procrastinate.App) -> None:
    assert defer_job(app, EMBED, {"paper_id": ITEM}) is True

    [job] = _jobs(app)
    assert job["task_name"] == "embed_paper"
    assert job["queue_name"] == "ai"
    assert job["lock"] is None
    assert job["queueing_lock"] == f"embed_paper:{ITEM}"
    assert job["args"] == {"paper_id": str(ITEM)}


def test_pdf_jobs_share_one_lock(app: procrastinate.App) -> None:
    other = uuid.UUID("018f0000-0000-7000-8000-0000000000bb")

    defer_job(app, IDENTIFY, {"upload_item_id": ITEM})
    defer_job(app, IDENTIFY, {"upload_item_id": other})

    assert [job["lock"] for job in _jobs(app)] == [PDF_LOCK, PDF_LOCK]
    assert [job["queue_name"] for job in _jobs(app)] == ["pdf", "pdf"]


def test_same_work_queued_twice_is_reported_not_raised(app: procrastinate.App) -> None:
    assert defer_job(app, SWEEP, {}) is True
    assert defer_job(app, SWEEP, {}) is False

    assert len(_jobs(app)) == 1
    assert _jobs(app)[0]["queueing_lock"] == "sweep"


def test_defer_rejects_non_id_arguments(app: procrastinate.App) -> None:
    with pytest.raises(ValueError, match="工作參數"):
        defer_job(app, EMBED, {"paper": ITEM})
    assert _jobs(app) == []


def test_json_turns_uuid_into_string_and_rejects_other_objects() -> None:
    assert json.loads(dumps_job_args({"paper_id": ITEM})) == {"paper_id": str(ITEM)}
    with pytest.raises(TypeError):
        dumps_job_args({"paper": object()})


# --- 簽名檢查 ------------------------------------------------------------------------


def good(upload_item_id: uuid.UUID, *, paper_id: uuid.UUID) -> None:
    pass


def no_arguments() -> None:
    pass


def bad_name(upload: uuid.UUID) -> None:
    pass


def bad_type(upload_item_id: str) -> None:
    pass


def bad_default(upload_item_id: uuid.UUID = ITEM) -> None:
    pass


def bad_varargs(*upload_item_id: uuid.UUID) -> None:
    pass


def bad_kwargs(**upload_item_id: uuid.UUID) -> None:
    pass


def test_good_signatures() -> None:
    assert check_job_signature(good) == ("upload_item_id", "paper_id")
    assert check_job_signature(no_arguments) == ()


@pytest.mark.parametrize("func", [bad_name, bad_type, bad_default, bad_varargs, bad_kwargs])
def test_bad_signatures_are_rejected(func: Callable[..., None]) -> None:
    with pytest.raises(InvalidJobSignatureError):
        check_job_signature(func)


def test_register_rejects_bad_signature(app: procrastinate.App) -> None:
    with pytest.raises(InvalidJobSignatureError):
        register_job(app, IDENTIFY, bad_type, max_retries=0, retry_wait=1)
    assert "identify_upload" not in app.tasks


def test_registered_job_gets_uuid_back_and_queue_and_retry(app: procrastinate.App) -> None:
    received: list[object] = []

    def record(upload_item_id: uuid.UUID) -> None:
        received.append(upload_item_id)

    register_job(app, IDENTIFY, record, max_retries=2, retry_wait=5)
    task = app.tasks["identify_upload"]

    task(upload_item_id=str(ITEM))

    assert received == [ITEM]
    assert task.queue == "pdf"
    assert isinstance(task.retry_strategy, procrastinate.RetryStrategy)
    assert task.retry_strategy.max_attempts == 2


def identify_upload(upload_item_id: uuid.UUID) -> None:
    pass


def test_registered_job_rejects_bad_arguments(app: procrastinate.App) -> None:
    register_job(app, IDENTIFY, identify_upload, max_retries=0, retry_wait=1)
    task = app.tasks["identify_upload"]

    with pytest.raises(ValueError, match="badly formed"):
        task(upload_item_id="not-a-uuid")
    with pytest.raises(TypeError):
        task(upload_item_id=str(ITEM), extra_id=str(ITEM))
    assert task.retry_strategy is None


def test_job_app_has_no_periodic_tasks_and_hides_url() -> None:
    job_app = create_job_app(
        "postgresql://radar_app:s3cret@db:5432/radar", max_connections=2, connect_timeout=3
    )

    assert job_app.app.periodic_registry.periodic_tasks == {}
    assert "s3cret" not in repr(job_app)


def test_retry_waits_grow_linearly_then_stop(app: procrastinate.App) -> None:
    register_job(app, IDENTIFY, identify_upload, max_retries=2, retry_wait=5)
    strategy = app.tasks["identify_upload"].retry_strategy
    assert isinstance(strategy, procrastinate.RetryStrategy)

    def wait_seconds(attempts: int) -> float | None:
        before = datetime.now(UTC)
        job = cast(Job, SimpleNamespace(attempts=attempts))
        decision = strategy.get_retry_decision(exception=RuntimeError("x"), job=job)
        if decision is None:
            return None
        assert decision.retry_at is not None
        return round((decision.retry_at - before).total_seconds())

    # 第 n 次重試前等 n × retry_wait 秒；用完 max_retries 次就不再試。
    assert [wait_seconds(attempts) for attempts in (0, 1, 2)] == [5, 10, None]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+psycopg://radar_app@db:5432/radar",
            "postgresql://radar_app@db:5432/radar",
        ),
        ("postgresql://radar_app@db:5432/radar", "postgresql://radar_app@db:5432/radar"),
    ],
)
def test_pool_conninfo_uses_libpq_scheme(url: str, expected: str) -> None:
    # 設定允許 SQLAlchemy 寫法；交給 psycopg_pool 前要換成 libpq 認得的開頭。
    assert libpq_conninfo(url) == expected
    job_app = create_job_app(url, max_connections=2, connect_timeout=3)
    assert job_app._pool_args()["conninfo"] == expected


# --- 重試卡住的工作（假的 connector 與 job_manager，不連資料庫）------------------------------


class _PlpgsqlRaiseError(Exception):
    sqlstate = "P0001"


class _ConnectionLostError(Exception):
    sqlstate = "08006"


def _connector_error(cause: Exception) -> procrastinate_exceptions.ConnectorException:
    try:
        raise procrastinate_exceptions.ConnectorException from cause
    except procrastinate_exceptions.ConnectorException as error:
        return error


class _FakeConnector:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.finished: list[object] = []

    def get_sync_connector(self) -> _FakeConnector:
        return self

    def execute_query_all(self, **_kwargs: object) -> list[dict[str, object]]:
        return self.rows

    def execute_query(self, **kwargs: object) -> None:
        self.finished.append(kwargs["job_id"])


class _FakeManager:
    def __init__(self, errors: dict[int, Exception]) -> None:
        self.errors = errors
        self.retried: list[int] = []

    def retry_job_by_id(self, *, job_id: int, retry_at: object) -> None:
        if job_id in self.errors:
            raise self.errors[job_id]
        self.retried.append(job_id)


def _stalled_app(
    rows: list[dict[str, object]], errors: dict[int, Exception]
) -> tuple[Any, _FakeConnector, _FakeManager]:
    connector, manager = _FakeConnector(rows), _FakeManager(errors)
    return SimpleNamespace(connector=connector, job_manager=manager), connector, manager


def _row(job_id: int, attempts: int = 0) -> dict[str, object]:
    return {"id": job_id, "task_name": "sweep", "attempts": attempts}


def test_stalled_jobs_out_of_retries_are_failed_not_retried() -> None:
    app, connector, manager = _stalled_app([_row(1, attempts=2), _row(2, attempts=1)], {})

    assert retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=2) == 1
    assert connector.finished == [1]
    assert manager.retried == [2]


def test_stalled_job_whose_state_changed_is_skipped() -> None:
    errors: dict[int, Exception] = {1: _connector_error(_PlpgsqlRaiseError())}
    app, connector, manager = _stalled_app([_row(1), _row(2)], errors)

    assert retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3) == 1
    assert manager.retried == [2]
    assert connector.finished == []


def test_other_database_errors_still_propagate() -> None:
    # 連線斷了不能當成「這筆跳過」：sweep 要失敗、不送心跳，才看得到資料庫出事。
    errors: dict[int, Exception] = {1: _connector_error(_ConnectionLostError())}
    app, _connector, manager = _stalled_app([_row(1), _row(2)], errors)

    with pytest.raises(procrastinate_exceptions.ConnectorException):
        retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3)
    assert manager.retried == []
