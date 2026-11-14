"""佇列 adapter：排入的選項、工作的簽名檢查與參數轉換（用 Procrastinate 的 InMemoryConnector）。"""

import asyncio
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import procrastinate
import pytest
from procrastinate import exceptions as procrastinate_exceptions
from procrastinate import sql as procrastinate_sql
from procrastinate.jobs import Job
from procrastinate.testing import InMemoryConnector

from radar.adapters.jobs import (
    FAIL_IF_STILL_STALLED,
    RETRY_IF_STILL_STALLED,
    InvalidJobSignatureError,
    RadarJobManager,
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
    """查卡住的工作回 `rows`；「核對後重試／標成失敗」依 `errors`、`gone` 丟錯或回空的。"""

    def __init__(
        self, rows: list[dict[str, object]], errors: dict[int, Exception], gone: set[int]
    ) -> None:
        self.rows = rows
        self.errors = errors
        self.gone = gone
        self.retried: list[int] = []
        self.finished: list[object] = []

    def get_sync_connector(self) -> _FakeConnector:
        return self

    def execute_query_all(self, *, query: str, **kwargs: Any) -> list[dict[str, object]]:
        if query == procrastinate_sql.queries["select_stalled_jobs_by_heartbeat"]:
            return self.rows
        job_id = kwargs["job_id"]
        if job_id in self.gone:
            return []  # 鎖住重新核對時已經不是卡住的狀態
        if query == RETRY_IF_STILL_STALLED:
            if job_id in self.errors:
                raise self.errors[job_id]
            self.retried.append(job_id)
        else:
            assert query == FAIL_IF_STILL_STALLED
            self.finished.append(job_id)
        return [{"id": job_id}]


def _stalled_app(
    rows: list[dict[str, object]], errors: dict[int, Exception], gone: frozenset[int] = frozenset()
) -> tuple[Any, _FakeConnector]:
    connector = _FakeConnector(rows, errors, set(gone))
    return SimpleNamespace(connector=connector), connector


def _row(job_id: int, attempts: int = 0) -> dict[str, object]:
    return {"id": job_id, "task_name": "sweep", "attempts": attempts, "worker_id": 7}


def test_stalled_jobs_out_of_retries_are_failed_not_retried() -> None:
    app, connector = _stalled_app([_row(1, attempts=2), _row(2, attempts=1)], {})

    assert retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=2) == 1
    assert connector.finished == [1]
    assert connector.retried == [2]


def test_stalled_job_whose_state_changed_is_skipped() -> None:
    errors: dict[int, Exception] = {1: _connector_error(_PlpgsqlRaiseError())}
    app, connector = _stalled_app([_row(1), _row(2)], errors)

    assert retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3) == 1
    assert connector.retried == [2]
    assert connector.finished == []


def test_other_database_errors_still_propagate() -> None:
    # 連線斷了不能當成「這筆跳過」：sweep 要失敗、不送心跳，才看得到資料庫出事。
    errors: dict[int, Exception] = {1: _connector_error(_ConnectionLostError())}
    app, connector = _stalled_app([_row(1), _row(2)], errors)

    with pytest.raises(procrastinate_exceptions.ConnectorException):
        retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3)
    assert connector.retried == []


def test_candidate_no_longer_stalled_is_neither_retried_nor_counted() -> None:
    # 候選清單是舊的：第 1 筆鎖住重新核對時已經被健康的 worker 接手（核對不通過、回空的）。
    app, connector = _stalled_app([_row(1), _row(2, attempts=5)], {}, gone=frozenset({1, 2}))

    assert retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3) == 0
    assert connector.retried == []
    assert connector.finished == []


def test_guard_passes_the_candidates_worker_attempts_and_heartbeat_window() -> None:
    seen: list[dict[str, Any]] = []
    app, connector = _stalled_app([_row(1, attempts=2)], {})
    real = connector.execute_query_all

    def record(*, query: str, **kwargs: Any) -> list[dict[str, object]]:
        if query == RETRY_IF_STILL_STALLED:
            seen.append(kwargs)
        return real(query=query, **kwargs)

    connector.execute_query_all = record  # type: ignore[method-assign]
    retry_stalled_jobs(app, seconds_since_heartbeat=90, max_retries=3)

    assert seen == [{"job_id": 1, "attempts": 2, "worker_id": 7, "seconds_since_heartbeat": 90}]


def test_guarded_queries_lock_the_job_row_and_use_the_frozen_functions() -> None:
    # 核對與改動在同一個敘述裡，先鎖住工作列；呼叫的 Procrastinate 函式要和套件自己用的一致
    # （升版時這裡會失敗，提醒回來核對）。
    for query in (RETRY_IF_STILL_STALLED, FAIL_IF_STILL_STALLED):
        assert "FOR UPDATE OF job" in query
        assert "job.status = 'doing'" in query
        assert "job.attempts = %(attempts)s" in query
    assert "procrastinate_retry_job_v2(" in procrastinate_sql.queries["retry_job"]
    assert "procrastinate_retry_job_v2(" in RETRY_IF_STILL_STALLED
    assert "procrastinate_finish_job_v1(" in procrastinate_sql.queries["finish_job"]
    assert "procrastinate_finish_job_v1(" in FAIL_IF_STILL_STALLED


# --- 工作失敗要重試、卻撞到排隊鎖 -----------------------------------------------------------


class _QueueingLockConnector(InMemoryConnector):
    """重試時丟 `UniqueViolation`（`queueing_lock` 有值＝撞到排隊鎖）。"""

    def __init__(self, queueing_lock: str | None) -> None:
        super().__init__()
        self.queueing_lock = queueing_lock
        self.finished: list[tuple[object, object]] = []

    async def execute_query_async(self, query: str, **arguments: Any) -> None:
        if query == procrastinate_sql.queries["retry_job"]:
            raise procrastinate_exceptions.UniqueViolation(
                constraint_name="some_constraint", queueing_lock=self.queueing_lock
            )
        if query == procrastinate_sql.queries["finish_job"]:
            self.finished.append((arguments["job_id"], arguments["status"]))
            return
        raise AssertionError(query)


def _failed_job() -> Job:
    return Job(id=42, queue="maintenance", task_name="sweep", lock=None, queueing_lock="sweep")


def test_retry_hitting_queueing_lock_marks_job_failed() -> None:
    connector = _QueueingLockConnector(queueing_lock="sweep")
    manager = RadarJobManager(connector=connector)

    asyncio.run(manager.retry_job(job=_failed_job(), retry_at=datetime.now(UTC)))

    assert connector.finished == [(42, "failed")]


def test_retry_other_unique_violation_still_raises() -> None:
    connector = _QueueingLockConnector(queueing_lock=None)
    manager = RadarJobManager(connector=connector)

    with pytest.raises(procrastinate_exceptions.UniqueViolation):
        asyncio.run(manager.retry_job(job=_failed_job(), retry_at=datetime.now(UTC)))
    assert connector.finished == []


def test_job_app_uses_radar_job_manager() -> None:
    job_app = create_job_app("postgresql://u@h/db", max_connections=2, connect_timeout=3)

    assert isinstance(job_app.app.job_manager, RadarJobManager)
    assert job_app.app.job_manager.connector is job_app.app.connector
