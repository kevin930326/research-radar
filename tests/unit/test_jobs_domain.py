"""背景工作清單與 sweep 登記清單的規則（radar/domain/jobs.py、radar/domain/sweep.py）。"""

import uuid
from datetime import timedelta

import pytest

from radar.domain.jobs import (
    JOBS,
    PDF_LOCK,
    POLL_BATCHES,
    RUN_WEEKLY_UPDATE,
    SWEEP,
    WEEKLY_UPDATE,
    JobSpec,
    Queue,
    UnknownJobError,
    check_job_ids,
    find_job,
    find_scheduled_job,
    is_id_parameter,
    queueing_lock,
    scheduled_jobs,
)
from radar.domain.sweep import SWEEP_REGISTRY, SweepEntry

A = uuid.UUID("018f0000-0000-7000-8000-000000000001")
B = uuid.UUID("018f0000-0000-7000-8000-000000000002")


def test_four_queues() -> None:
    assert [queue.value for queue in Queue] == ["ingest", "ai", "pdf", "maintenance"]


def test_jobs_have_unique_names_and_schedules() -> None:
    names = [job.name for job in JOBS]
    schedules = [job.schedule for job in JOBS if job.schedule is not None]
    assert len(names) == len(set(names))
    assert len(schedules) == len(set(schedules))


def test_sweep_is_scheduled_on_maintenance() -> None:
    assert SWEEP in JOBS
    assert SWEEP.queue is Queue.MAINTENANCE
    assert find_scheduled_job("sweep") is SWEEP
    assert find_job("sweep") is SWEEP
    assert SWEEP.check == "sweep"
    assert SWEEP.lock is None
    assert scheduled_jobs() == [POLL_BATCHES, SWEEP, WEEKLY_UPDATE]


def test_weekly_update_jobs() -> None:
    # 排程名稱用連字號（crontab、心跳、radar enqueue），工作名稱用底線。
    assert WEEKLY_UPDATE in JOBS
    assert RUN_WEEKLY_UPDATE in JOBS
    assert (WEEKLY_UPDATE.name, WEEKLY_UPDATE.queue, WEEKLY_UPDATE.check) == (
        "weekly_update",
        Queue.INGEST,
        "weekly-update",
    )
    assert WEEKLY_UPDATE.lock is None
    assert find_scheduled_job("weekly-update") is WEEKLY_UPDATE
    # 推進一筆 weekly_run 的工作沒有排程，帶 weekly_update 這把鎖（同時只跑一個）。
    assert (RUN_WEEKLY_UPDATE.name, RUN_WEEKLY_UPDATE.queue) == ("run_weekly_update", Queue.INGEST)
    assert RUN_WEEKLY_UPDATE.schedule is None
    assert RUN_WEEKLY_UPDATE.lock == "weekly_update"
    assert find_job("run_weekly_update") is RUN_WEEKLY_UPDATE


@pytest.mark.parametrize("lock", ["", 1])
def test_job_spec_lock_must_be_non_empty_string(lock: object) -> None:
    with pytest.raises(ValueError, match="鎖"):
        JobSpec("probe", Queue.INGEST, lock=lock)  # type: ignore[arg-type]


@pytest.mark.parametrize("lock", ["other", PDF_LOCK])
def test_pdf_jobs_cannot_set_their_own_lock(lock: str) -> None:
    with pytest.raises(ValueError, match="pdf"):
        JobSpec("identify_upload", Queue.PDF, lock=lock)


def test_unknown_schedule_lists_available() -> None:
    with pytest.raises(UnknownJobError) as caught:
        find_scheduled_job("nope")

    assert caught.value.name == "nope"
    assert caught.value.available == ["poll-batches", "sweep", "weekly-update"]


def test_unknown_job_name() -> None:
    with pytest.raises(UnknownJobError):
        find_job("nope")


@pytest.mark.parametrize(
    ("name", "schedule"),
    [("Sweep", None), ("sweep-it", None), ("", None), ("sweep", "Sweep"), ("sweep", "a b")],
)
def test_job_spec_rejects_bad_names(name: str, schedule: str | None) -> None:
    with pytest.raises(ValueError, match="名稱"):
        JobSpec(name, Queue.AI, schedule=schedule)


def test_job_without_schedule_has_no_check() -> None:
    with pytest.raises(ValueError, match="沒有排程"):
        _ = JobSpec("identify_upload", Queue.PDF).check


def test_queueing_lock_without_ids_is_job_name() -> None:
    assert queueing_lock(SWEEP, {}) == "sweep"


def test_queueing_lock_with_ids_is_sorted_by_parameter_name() -> None:
    job = JobSpec("merge_pair", Queue.AI)

    first = queueing_lock(job, {"b_id": B, "a_id": A})
    second = queueing_lock(job, {"a_id": A, "b_id": B})

    assert first == second == f"merge_pair:{A}:{B}"


@pytest.mark.parametrize(
    "ids",
    [{"upload": A}, {"Upload_id": A}, {"upload_id": str(A)}, {"upload_id": 1}],
)
def test_job_ids_must_be_uuid_named_id(ids: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="工作參數"):
        check_job_ids(ids)


def _entry(**changes: object) -> SweepEntry:
    values: dict[str, object] = {
        "table": "upload_item",
        "status_column": "status",
        "start_status": "received",
        "time_column": "updated_at",
        "job": JobSpec("identify_upload", Queue.PDF),
        "stuck_after": timedelta(minutes=30),
    }
    values.update(changes)
    return SweepEntry(**values)  # type: ignore[arg-type]


def test_sweep_entry_parameter_is_table_id() -> None:
    assert _entry().id_parameter == "upload_item_id"


@pytest.mark.parametrize(
    "changes",
    [
        {"table": "Upload_item"},
        {"table": "upload item"},
        {"table": "upload_item; DROP TABLE x"},
        {"table": "a" * 64},
        {"status_column": 'status"'},
        {"time_column": "updated-at"},
        {"start_status": ""},
        {"stuck_after": timedelta(0)},
        {"stuck_after": timedelta(seconds=-1)},
    ],
)
def test_sweep_entry_rejects_bad_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"識別字|起始狀態|卡住"):
        _entry(**changes)


def test_sweep_entry_table_must_start_with_letter() -> None:
    # 開頭是底線的表名，`<表名>_id` 不是合法的工作參數名稱，sweep 排入時才會出錯；登記時就要擋。
    with pytest.raises(ValueError, match="識別字"):
        _entry(table="_upload_item")
    with pytest.raises(ValueError, match="識別字"):
        _entry(time_column="_updated_at")
    assert is_id_parameter(_entry(table="upload_item2").id_parameter)


def test_registry_has_weekly_run() -> None:
    # Story 2.9 起登記清單不只一列（batch_submission 也登記了），找 weekly_run 那一列。
    [entry] = [entry for entry in SWEEP_REGISTRY if entry.table == "weekly_run"]
    assert (entry.table, entry.status_column, entry.start_status, entry.time_column) == (
        "weekly_run",
        "status",
        "fetching",
        "updated_at",
    )
    assert entry.job is RUN_WEEKLY_UPDATE
    assert entry.stuck_after == timedelta(hours=2)
    assert entry.id_parameter == "weekly_run_id"
