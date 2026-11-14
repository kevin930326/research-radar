"""背景工作佇列對真的 PostgreSQL：遷移與降版、`radar enqueue`＋worker 子行程、pdf 的鎖、
sweep 補排（含經 CLI 與正式 worker）、被 SIGKILL 的 worker 留下的工作、舊候選清單的競爭、
重試撞到排隊鎖、worker 比遷移先起來（Story 1.4、ADR-0019）。
"""

import asyncio
import contextlib
import json
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
import structlog
from procrastinate import sql as procrastinate_sql
from procrastinate import utils as procrastinate_utils

from radar.adapters.fake import FakeHeartbeat
from radar.adapters.jobs import create_job_app, defer_job, register_job, retry_stalled_jobs
from radar.domain.jobs import PDF_LOCK, JobSpec, Queue
from radar.domain.settings import Settings
from radar.domain.sweep import SweepEntry
from radar.entrypoints.worker.main import worker_options
from radar.repo.db import Database
from radar.services.sweep import SweepResult, sweep
from tests.database import ROOT, TestDatabase, migrate

BIN = Path(sys.executable).parent
FROZEN_SQL = ROOT / "migrations" / "procrastinate" / "schema-3.11.0.sql"
QUEUE_TABLES = {
    "procrastinate_jobs",
    "procrastinate_workers",
    "procrastinate_events",
    "procrastinate_periodic_defers",
}
QUEUE_TYPES = {
    "procrastinate_job_status",
    "procrastinate_job_event_type",
    "procrastinate_job_to_defer_v1",
}
WAIT_SECONDS = 30.0


# --- 共用的小工具 --------------------------------------------------------------------------


def _admin_rows(database: TestDatabase, query: str) -> list[tuple[object, ...]]:
    with psycopg.connect(database.admin_url, autocommit=True) as connection:
        return connection.execute(query).fetchall()


def _queue_objects(database: TestDatabase) -> dict[str, set[str]]:
    """public 裡名稱以 procrastinate_ 開頭的表、函式、型別（不含陣列與表的列型別）。"""
    tables = _admin_rows(
        database,
        "SELECT relname FROM pg_class WHERE relkind = 'r' AND relname LIKE 'procrastinate%'",
    )
    routines = _admin_rows(
        database,
        "SELECT p.oid::regprocedure::text FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'public' AND p.proname LIKE 'procrastinate%'",
    )
    types = _admin_rows(
        database,
        "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
        " WHERE n.nspname = 'public' AND t.typname LIKE '%procrastinate%'",
    )
    return {
        "tables": {str(row[0]) for row in tables},
        "routines": {str(row[0]) for row in routines},
        "types": {str(row[0]) for row in types},
    }


def _jobs(database: TestDatabase) -> list[dict[str, object]]:
    with psycopg.connect(database.admin_url, autocommit=True) as connection:
        cursor = connection.execute(
            "SELECT id, queue_name, task_name, status::text, attempts, lock, queueing_lock, args"
            " FROM procrastinate_jobs ORDER BY id"
        )
        names = [column.name for column in cursor.description or []]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _wait_until(condition: Callable[[], bool], what: str, timeout: float = WAIT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"等不到：{what}")
        time.sleep(0.2)


def _radar(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # 刻意不繼承目前的環境變數，只給這次需要的。
    return subprocess.run(
        [str(BIN / "radar"), *args],
        env={"PYTHONUNBUFFERED": "1", **env},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _spawn(argv: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        env={"PYTHONUNBUFFERED": "1", **env},
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop(process: subprocess.Popen[str]) -> tuple[int, list[dict[str, object]], str]:
    process.send_signal(signal.SIGTERM)
    out, err = process.communicate(timeout=60)
    records = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return process.returncode, records, err


@pytest.fixture
def migrated_fresh(fresh_database: TestDatabase) -> TestDatabase:
    """全新、已遷移、這個測試自己用的資料庫（可以寫）。"""
    migrate(fresh_database)
    return fresh_database


# --- 遷移 --------------------------------------------------------------------------------


def test_migration_creates_queue_objects_without_extensions(fresh_database: TestDatabase) -> None:
    extensions_before = _admin_rows(fresh_database, "SELECT extname FROM pg_extension ORDER BY 1")

    migrate(fresh_database)

    objects = _queue_objects(fresh_database)
    assert objects["tables"] == QUEUE_TABLES
    assert objects["types"] >= QUEUE_TYPES
    frozen = FROZEN_SQL.read_text(encoding="utf-8")
    assert len(objects["routines"]) == frozen.count("CREATE FUNCTION")
    # 遷移不建擴充套件：檔頭的 CREATE EXTENSION IF NOT EXISTS plpgsql 只發 NOTICE。
    assert _admin_rows(fresh_database, "SELECT extname FROM pg_extension ORDER BY 1") == (
        extensions_before
    )
    assert fresh_database.revisions() == {"0002"}


def test_downgrade_to_0001_removes_every_queue_object(fresh_database: TestDatabase) -> None:
    env = {"RADAR_MIGRATION_DATABASE_URL": fresh_database.owner_url}
    migrate(fresh_database)

    down = subprocess.run(
        [str(BIN / "alembic"), "downgrade", "0001"],
        env={"PYTHONUNBUFFERED": "1", **env},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert down.returncode == 0, down.stdout + down.stderr
    assert fresh_database.revisions() == {"0001"}
    assert _queue_objects(fresh_database) == {"tables": set(), "routines": set(), "types": set()}

    migrate(fresh_database)

    assert _queue_objects(fresh_database)["tables"] == QUEUE_TABLES


def test_alembic_check_ignores_queue_tables(migrated_fresh: TestDatabase) -> None:
    result = subprocess.run(
        [str(BIN / "alembic"), "check"],
        env={"PYTHONUNBUFFERED": "1", "RADAR_MIGRATION_DATABASE_URL": migrated_fresh.owner_url},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No new upgrade operations detected" in result.stdout
    assert "procrastinate" not in result.stdout + result.stderr


# --- radar enqueue ＋ worker 子行程 -------------------------------------------------------


def test_enqueue_sweep_runs_on_worker(migrated_fresh: TestDatabase) -> None:
    env = {"DATABASE_URL": migrated_fresh.app_url}
    worker = _spawn([sys.executable, "-m", "radar.entrypoints.worker"], env)
    try:
        result = _radar("enqueue", "sweep", env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "已排入 sweep" in result.stdout

        _wait_until(
            lambda: [job["status"] for job in _jobs(migrated_fresh)] == ["succeeded"],
            "sweep 執行成功",
        )
    finally:
        code, records, err = _stop(worker)

    [job] = _jobs(migrated_fresh)
    assert (job["queue_name"], job["task_name"], job["queueing_lock"]) == (
        "maintenance",
        "sweep",
        "sweep",
    )
    assert code == 0, err
    assert err == ""
    assert records[0]["event"] == "worker 啟動"
    # 工作執行中的日誌帶 job_id。
    done = [r for r in records if r["event"] == "sweep 完成"]
    assert [r["job_id"] for r in done] == [job["id"]]
    assert any(r["event"] == "正常關機" and r["signal"] == "SIGTERM" for r in records)
    assert "radar_app@" not in json.dumps(records, ensure_ascii=False)


def test_enqueue_twice_keeps_one_todo(migrated_fresh: TestDatabase) -> None:
    env = {"DATABASE_URL": migrated_fresh.app_url}

    first = _radar("enqueue", "sweep", env=env)
    second = _radar("enqueue", "sweep", env=env)

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert "已經在排隊" in second.stdout
    assert [(job["status"], job["task_name"]) for job in _jobs(migrated_fresh)] == [
        ("todo", "sweep")
    ]


def test_enqueue_unreachable_database_prints_only_error_type() -> None:
    started = time.monotonic()
    result = _radar(
        "enqueue",
        "sweep",
        env={"DATABASE_URL": "postgresql://radar_app:s3cret-PW@127.0.0.1:1/radar"},
    )

    assert result.returncode == 1
    assert "PoolTimeout" in result.stderr
    output = result.stdout + result.stderr
    assert "s3cret-PW" not in output
    assert "127.0.0.1:1" not in output
    assert "port 1" not in output
    # 連線逾時（預設 3 秒）內就失敗，不是 Procrastinate 預設的 30 秒。
    assert time.monotonic() - started < 20


def test_unknown_job_exits_2_without_database() -> None:
    result = _radar("enqueue", "nope", env={"DATABASE_URL": "postgresql://radar_app@127.0.0.1:1/x"})

    assert result.returncode == 2
    assert "可用的工作：sweep" in result.stderr


# --- pdf 的鎖 -----------------------------------------------------------------------------

PDF_PROBE = JobSpec("probe_pdf", Queue.PDF)
MAINTENANCE_PROBE = JobSpec("probe_maintenance", Queue.MAINTENANCE)


def test_pdf_jobs_never_overlap_while_maintenance_jobs_do(migrated_fresh: TestDatabase) -> None:
    job_app = create_job_app(migrated_fresh.app_url, max_connections=2, connect_timeout=5)
    runs: dict[str, list[tuple[float, float]]] = {"pdf": [], "maintenance": []}

    def probe(queue: str) -> Callable[[uuid.UUID], None]:
        def run(probe_id: uuid.UUID) -> None:
            started = time.monotonic()
            time.sleep(1.0)
            runs[queue].append((started, time.monotonic()))

        return run

    register_job(job_app.app, PDF_PROBE, probe("pdf"), max_retries=0, retry_wait=1)
    register_job(job_app.app, MAINTENANCE_PROBE, probe("maintenance"), max_retries=0, retry_wait=1)
    with job_app.opened() as app:
        # 對照組排前面：兩個 maintenance 會被同時拿走；兩個 pdf 帶同一把鎖，一次只能拿一個。
        for job in (MAINTENANCE_PROBE, MAINTENANCE_PROBE, PDF_PROBE, PDF_PROBE):
            assert defer_job(app, job, {"probe_id": uuid.uuid4()}) is True

    settings = Settings.model_validate(
        {"database_url": migrated_fresh.app_url, "worker_concurrency": 2}
    )

    async def run_worker_until_done() -> None:
        async with job_app.opened_async() as app:
            worker = asyncio.create_task(app.run_worker_async(**worker_options(settings)))
            deadline = time.monotonic() + WAIT_SECONDS
            while sum(len(intervals) for intervals in runs.values()) < 4:
                assert time.monotonic() < deadline, runs
                assert not worker.done(), worker
                await asyncio.sleep(0.1)
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    asyncio.run(run_worker_until_done())

    def overlap(intervals: list[tuple[float, float]]) -> bool:
        (start_a, end_a), (start_b, end_b) = sorted(intervals)
        return start_b < end_a and start_a < end_b

    assert not overlap(runs["pdf"]), runs
    assert overlap(runs["maintenance"]), runs
    jobs = _jobs(migrated_fresh)
    assert [job["lock"] for job in jobs if job["queue_name"] == "pdf"] == [PDF_LOCK, PDF_LOCK]
    assert {job["status"] for job in jobs} == {"succeeded"}


# --- sweep --------------------------------------------------------------------------------

PROBE_TABLE_SQL = (
    "CREATE TABLE probe_item ("
    " id uuid PRIMARY KEY DEFAULT uuidv7(),"
    " status text NOT NULL,"
    " updated_at timestamptz NOT NULL)"
)
IDENTIFY_PROBE = JobSpec("identify_probe", Queue.PDF)
PROBE_ENTRY = SweepEntry(
    table="probe_item",
    status_column="status",
    start_status="received",
    time_column="updated_at",
    job=IDENTIFY_PROBE,
    stuck_after=timedelta(hours=1),
)


def test_sweep_requeues_only_stuck_rows_once(migrated_fresh: TestDatabase) -> None:
    with migrated_fresh.connect("radar_owner") as owner:
        owner.execute(PROBE_TABLE_SQL)
        stuck = owner.execute(
            "INSERT INTO probe_item (status, updated_at)"
            " VALUES ('received', now() - interval '2 hours') RETURNING id"
        ).fetchone()
        owner.execute(
            "INSERT INTO probe_item (status, updated_at) VALUES"
            " ('received', now() - interval '10 minutes'),"
            " ('matched', now() - interval '2 hours')"
        )
    assert stuck is not None
    database = Database(migrated_fresh.app_url, connect_timeout=5, pool_size=1)
    job_app = create_job_app(migrated_fresh.app_url, max_connections=2, connect_timeout=5)
    heartbeat = FakeHeartbeat()

    with job_app.opened() as app:

        def run() -> SweepResult:
            return sweep(
                database,
                (PROBE_ENTRY,),
                lambda job, ids: defer_job(app, job, ids),
                lambda: retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3),
                heartbeat,
            )

        first = run()
        second = run()

    assert first == SweepResult(stuck_rows=1, requeued=1, retried_jobs=0)
    assert second == SweepResult(stuck_rows=1, requeued=0, retried_jobs=0)
    [job] = _jobs(migrated_fresh)
    assert job["task_name"] == "identify_probe"
    assert job["queue_name"] == "pdf"
    assert job["args"] == {"probe_item_id": str(stuck[0])}
    assert job["queueing_lock"] == f"identify_probe:{stuck[0]}"
    assert heartbeat.sent == ["sweep", "sweep"]


# 正式的 worker（main()），sweep 的登記清單換成測試狀態表 probe_item，並登記它要排入的工作
# （執行時把參數寫進 marker）；sweep 本身照正式的 register_tasks 登記與執行。
PROBE_REGISTRY_WORKER_SCRIPT = """
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import radar.entrypoints.worker.main as worker_main
import radar.entrypoints.worker.tasks as worker_tasks
from radar.adapters.jobs import register_job
from radar.domain.jobs import JobSpec, Queue
from radar.domain.sweep import SweepEntry

marker = Path(sys.argv[1])
identify_probe = JobSpec("identify_probe", Queue.PDF)
worker_tasks.SWEEP_REGISTRY = (
    SweepEntry(
        table="probe_item",
        status_column="status",
        start_status="received",
        time_column="updated_at",
        job=identify_probe,
        stuck_after=timedelta(hours=1),
    ),
)
real_register_tasks = worker_main.register_tasks


def register_with_probe_job(app, parts, settings):
    real_register_tasks(app, parts, settings)

    def run_identify_probe(probe_item_id: uuid.UUID) -> None:
        marker.write_text(str(probe_item_id))

    register_job(app, identify_probe, run_identify_probe, max_retries=0, retry_wait=1)


worker_main.register_tasks = register_with_probe_job
raise SystemExit(worker_main.main())
"""


def test_cli_enqueued_sweep_on_worker_requeues_registered_stuck_row(
    migrated_fresh: TestDatabase, tmp_path: Path
) -> None:
    with migrated_fresh.connect("radar_owner") as owner:
        owner.execute(PROBE_TABLE_SQL)
        stuck = owner.execute(
            "INSERT INTO probe_item (status, updated_at)"
            " VALUES ('received', now() - interval '2 hours') RETURNING id"
        ).fetchone()
        owner.execute(
            "INSERT INTO probe_item (status, updated_at) VALUES"
            " ('received', now() - interval '10 minutes'),"
            " ('matched', now() - interval '2 hours')"
        )
    assert stuck is not None
    script = tmp_path / "probe_registry_worker.py"
    script.write_text(PROBE_REGISTRY_WORKER_SCRIPT, encoding="utf-8")
    marker = tmp_path / "identify-probe-ran"
    env = {"DATABASE_URL": migrated_fresh.app_url}
    worker = _spawn([sys.executable, "-I", str(script), str(marker)], env)
    try:
        result = _radar("enqueue", "sweep", env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "已排入 sweep" in result.stdout

        _wait_until(
            lambda: (
                [(job["task_name"], job["status"]) for job in _jobs(migrated_fresh)]
                == [("sweep", "succeeded"), ("identify_probe", "succeeded")]
            ),
            "sweep 補排的工作也執行成功",
        )
    finally:
        code, records, err = _stop(worker)

    assert code == 0, err
    sweep_job, requeued = _jobs(migrated_fresh)
    assert (sweep_job["queue_name"], sweep_job["args"]) == ("maintenance", {})
    assert requeued["task_name"] == "identify_probe"
    assert requeued["queue_name"] == "pdf"
    assert requeued["lock"] == PDF_LOCK
    assert requeued["args"] == {"probe_item_id": str(stuck[0])}
    assert requeued["queueing_lock"] == f"identify_probe:{stuck[0]}"
    assert marker.read_text() == str(stuck[0])
    [done] = [r for r in records if r["event"] == "sweep 完成"]
    assert (done["stuck_rows"], done["requeued"], done["retried_jobs"]) == (1, 1, 0)
    assert done["job_id"] == sweep_job["id"]


# 正式的 worker（main()）再多登記一個很慢的測試工作；其餘照正式程式跑。
SLOW_WORKER_SCRIPT = """
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import radar.entrypoints.worker.main as worker_main
from radar.adapters.jobs import register_job
from radar.domain.jobs import JobSpec, Queue

marker = Path(sys.argv[1])
# 慢工作睡幾秒（預設 120：等著被 SIGKILL）。
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0
real_register_tasks = worker_main.register_tasks


def register_with_slow_job(app, parts, settings):
    real_register_tasks(app, parts, settings)

    def slow_probe(probe_id: uuid.UUID) -> None:
        marker.write_text(str(probe_id))
        time.sleep(seconds)

    register_job(app, JobSpec("slow_probe", Queue.PDF), slow_probe, max_retries=0, retry_wait=1)


worker_main.register_tasks = register_with_slow_job
raise SystemExit(worker_main.main())
"""


def test_job_of_killed_worker_is_retried_by_sweep(
    migrated_fresh: TestDatabase, tmp_path: Path
) -> None:
    script = tmp_path / "slow_worker.py"
    script.write_text(SLOW_WORKER_SCRIPT, encoding="utf-8")
    marker = tmp_path / "slow-started"
    worker = _spawn(
        [sys.executable, "-I", str(script), str(marker)],
        {"DATABASE_URL": migrated_fresh.app_url},
    )
    job_app = create_job_app(migrated_fresh.app_url, max_connections=2, connect_timeout=5)
    try:
        with job_app.opened() as app:
            defer_job(app, JobSpec("slow_probe", Queue.PDF), {"probe_id": uuid.uuid4()})
        _wait_until(marker.exists, "慢工作開始執行")
    finally:
        worker.kill()  # SIGKILL：worker 沒機會收尾，工作停在 doing
        worker.communicate(timeout=30)

    [job] = _jobs(migrated_fresh)
    assert (job["status"], job["attempts"]) == ("doing", 0)
    # worker 的心跳過期（等同超過 worker_stalled_seconds 沒報到）。
    with psycopg.connect(migrated_fresh.admin_url, autocommit=True) as admin:
        admin.execute("UPDATE procrastinate_workers SET last_heartbeat = now() - interval '1 hour'")

    heartbeat = FakeHeartbeat()
    database = Database(migrated_fresh.app_url, connect_timeout=5, pool_size=1)
    with job_app.opened() as app:
        result = sweep(
            database,
            (),
            lambda job, ids: defer_job(app, job, ids),
            lambda: retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3),
            heartbeat,
        )

    assert result.retried_jobs == 1
    [job] = _jobs(migrated_fresh)
    assert (job["status"], job["attempts"], job["lock"]) == ("todo", 1, PDF_LOCK)
    assert heartbeat.sent == ["sweep"]


def test_sigterm_waits_for_running_job_then_exits_zero(
    migrated_fresh: TestDatabase, tmp_path: Path
) -> None:
    script = tmp_path / "slow_worker.py"
    script.write_text(SLOW_WORKER_SCRIPT, encoding="utf-8")
    marker = tmp_path / "slow-started"
    worker = _spawn(
        [sys.executable, "-I", str(script), str(marker), "2"],
        {"DATABASE_URL": migrated_fresh.app_url},
    )
    job_app = create_job_app(migrated_fresh.app_url, max_connections=2, connect_timeout=5)
    try:
        with job_app.opened() as app:
            defer_job(app, JobSpec("slow_probe", Queue.PDF), {"probe_id": uuid.uuid4()})
        _wait_until(marker.exists, "慢工作開始執行")
        [job] = _jobs(migrated_fresh)
        assert job["status"] == "doing"
    finally:
        # 工作做到一半送 SIGTERM：要等它做完（約 2 秒）才正常關機。
        code, records, err = _stop(worker)

    assert code == 0, err
    assert any(record["event"] == "正常關機" for record in records)
    [job] = _jobs(migrated_fresh)
    assert job["status"] == "succeeded"


# --- 卡住的工作的特殊情況 ------------------------------------------------------------------


def _insert_job(
    database: TestDatabase,
    *,
    status: str,
    queueing_lock: str | None = "sweep",
    attempts: int = 0,
    stalled_worker: bool = True,
) -> int:
    """直接寫一筆 sweep 工作；`stalled_worker` 時掛在一個一小時沒報到的 worker 底下。"""
    with psycopg.connect(database.admin_url, autocommit=True) as admin:
        worker_id = None
        if stalled_worker:
            worker_row = admin.execute(
                "INSERT INTO procrastinate_workers (last_heartbeat)"
                " VALUES (now() - interval '1 hour') RETURNING id"
            ).fetchone()
            assert worker_row is not None
            worker_id = worker_row[0]
        row = admin.execute(
            "INSERT INTO procrastinate_jobs"
            " (queue_name, task_name, status, attempts, queueing_lock, worker_id)"
            " VALUES ('maintenance', 'sweep', %s::procrastinate_job_status, %s, %s, %s)"
            " RETURNING id",
            (status, attempts, queueing_lock, worker_id),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _statuses(database: TestDatabase) -> dict[object, tuple[object, object]]:
    return {job["id"]: (job["status"], job["attempts"]) for job in _jobs(database)}


def test_stalled_job_with_same_lock_already_queued_is_failed(migrated_fresh: TestDatabase) -> None:
    stalled = _insert_job(migrated_fresh, status="doing")
    queued = _insert_job(migrated_fresh, status="todo", stalled_worker=False)
    job_app = create_job_app(migrated_fresh.app_url, max_connections=2, connect_timeout=5)

    with job_app.opened() as app:
        retried = retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3)

    assert retried == 0
    # 從「執行中」結束時 Procrastinate 會把 attempts 加一（這次執行也算一次）。
    assert _statuses(migrated_fresh) == {stalled: ("failed", 1), queued: ("todo", 0)}


def test_stalled_job_out_of_retries_is_failed(migrated_fresh: TestDatabase) -> None:
    used_up = _insert_job(migrated_fresh, status="doing", attempts=3, queueing_lock=None)
    job_app = create_job_app(migrated_fresh.app_url, max_connections=2, connect_timeout=5)

    with job_app.opened() as app:
        retried = retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3)

    assert retried == 0
    assert _statuses(migrated_fresh) == {used_up: ("failed", 4)}


def test_stalled_job_finished_meanwhile_is_skipped(
    migrated_fresh: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished = _insert_job(migrated_fresh, status="doing", queueing_lock=None)
    other = _insert_job(migrated_fresh, status="doing", queueing_lock=None)
    job_app = create_job_app(migrated_fresh.app_url, max_connections=2, connect_timeout=5)

    with job_app.opened() as app:
        connector = app.connector.get_sync_connector()
        real_select = connector.execute_query_all

        def select_then_finish(**kwargs: Any) -> list[dict[str, Any]]:
            # 查出卡住的工作之後、重試之前，原本的 worker 晚一步把第一筆做完了。
            rows = real_select(**kwargs)
            with psycopg.connect(migrated_fresh.admin_url, autocommit=True) as admin:
                admin.execute(
                    "UPDATE procrastinate_jobs SET status = 'succeeded' WHERE id = %s",
                    (finished,),
                )
            return rows

        monkeypatch.setattr(connector, "execute_query_all", select_then_finish)
        retried = retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3)

    assert retried == 1
    assert _statuses(migrated_fresh) == {finished: ("succeeded", 0), other: ("todo", 1)}


def test_second_sweep_with_stale_candidates_leaves_job_taken_by_healthy_worker(
    migrated_fresh: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 重現兩輪 sweep 拿到同一份（舊的）候選清單：第一輪重排、健康的 worker 接手之後，
    # 第二輪不可以再把它改回排隊（否則重複執行、突破重試上限、pdf 的鎖被提早放掉）。
    stalled = _insert_job(migrated_fresh, status="doing", queueing_lock=None)
    stalled_query = procrastinate_sql.queries["select_stalled_jobs_by_heartbeat"]
    job_app = create_job_app(migrated_fresh.app_url, max_connections=2, connect_timeout=5)

    with job_app.opened() as app:
        connector = app.connector.get_sync_connector()
        real_query_all = connector.execute_query_all
        candidates: list[dict[str, Any]] = []

        def same_candidates_every_time(**kwargs: Any) -> list[dict[str, Any]]:
            if kwargs["query"] != stalled_query:
                return real_query_all(**kwargs)
            if not candidates:
                candidates.extend(real_query_all(**kwargs))
            return [dict(row) for row in candidates]

        monkeypatch.setattr(connector, "execute_query_all", same_candidates_every_time)
        first = retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3)

        # 健康的 worker 照 Procrastinate 自己的流程登記、取走這個工作。
        with psycopg.connect(migrated_fresh.admin_url, autocommit=True) as admin:
            registered = admin.execute("SELECT * FROM procrastinate_register_worker_v1()")
            healthy = registered.fetchone()
            assert healthy is not None
            fetched = admin.execute(
                "SELECT id FROM procrastinate_fetch_job_v2(ARRAY['maintenance'], %s)",
                (healthy[0],),
            ).fetchone()

        second = retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3)

    assert [row["id"] for row in candidates] == [stalled]
    assert fetched == (stalled,)
    assert (first, second) == (1, 0)
    [job] = _jobs(migrated_fresh)
    assert (job["status"], job["attempts"]) == ("doing", 1)
    with psycopg.connect(migrated_fresh.admin_url, autocommit=True) as admin:
        owner = admin.execute(
            "SELECT worker_id FROM procrastinate_jobs WHERE id = %s", (stalled,)
        ).fetchone()
    assert owner == (healthy[0],)


def test_stale_candidate_out_of_retries_is_not_failed_once_taken_by_healthy_worker(
    migrated_fresh: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 「用完次數就標成失敗」那條路也要先核對：候選清單裡的 attempts 是舊的。
    stalled = _insert_job(migrated_fresh, status="doing", queueing_lock=None, attempts=1)
    stalled_query = procrastinate_sql.queries["select_stalled_jobs_by_heartbeat"]
    job_app = create_job_app(migrated_fresh.app_url, max_connections=2, connect_timeout=5)

    with job_app.opened() as app:
        connector = app.connector.get_sync_connector()
        real_query_all = connector.execute_query_all
        candidates: list[dict[str, Any]] = []

        def same_candidates_every_time(**kwargs: Any) -> list[dict[str, Any]]:
            if kwargs["query"] != stalled_query:
                return real_query_all(**kwargs)
            if not candidates:
                candidates.extend(real_query_all(**kwargs))
            return [dict(row) for row in candidates]

        monkeypatch.setattr(connector, "execute_query_all", same_candidates_every_time)
        # 上限 2：第一輪 attempts 1 → 重排成 2。
        first = retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=2)
        with psycopg.connect(migrated_fresh.admin_url, autocommit=True) as admin:
            healthy = admin.execute("SELECT * FROM procrastinate_register_worker_v1()").fetchone()
            assert healthy is not None
            admin.execute(
                "SELECT id FROM procrastinate_fetch_job_v2(ARRAY['maintenance'], %s)",
                (healthy[0],),
            )
        # 第二輪：上限降到 1，舊清單的 attempts（1）已到上限，但工作已經在健康的 worker 手上。
        second = retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=1)

    assert (first, second) == (1, 0)
    assert _statuses(migrated_fresh) == {stalled: ("doing", 2)}


def test_guarded_retry_works_through_the_workers_async_connector(
    migrated_fresh: TestDatabase,
) -> None:
    # worker 裡的 sweep 是同步工作、在執行緒跑，連線池是非同步的那個（和 CLI 不同的路）。
    retried = _insert_job(migrated_fresh, status="doing", queueing_lock=None)
    used_up = _insert_job(migrated_fresh, status="doing", queueing_lock=None, attempts=3)
    job_app = create_job_app(migrated_fresh.app_url, max_connections=2, connect_timeout=5)

    async def retry_like_the_worker() -> int:
        async with job_app.opened_async() as app:
            return await procrastinate_utils.sync_to_async(
                retry_stalled_jobs, app, seconds_since_heartbeat=60, max_retries=3
            )

    assert asyncio.run(retry_like_the_worker()) == 1
    assert _statuses(migrated_fresh) == {retried: ("todo", 1), used_up: ("failed", 4)}


FLAKY = JobSpec("flaky_probe", Queue.MAINTENANCE)


def test_failed_job_whose_retry_hits_queueing_lock_ends_as_failed(
    migrated_fresh: TestDatabase,
) -> None:
    # 並行數 1：A 執行中又排入同一件事 B（同一個排隊鎖），A 失敗要重試時撞鎖。
    # A 要確實結束（failed），不能卡在 doing；B 照常執行。
    job_app = create_job_app(migrated_fresh.app_url, max_connections=2, connect_timeout=5)
    probe_id = uuid.uuid4()
    runs: list[str] = []

    def flaky(probe_id: uuid.UUID) -> None:
        runs.append("run")
        if len(runs) == 1:
            other = create_job_app(migrated_fresh.app_url, max_connections=1, connect_timeout=5)
            with other.opened() as other_app:
                assert defer_job(other_app, FLAKY, {"probe_id": probe_id}) is True
            raise RuntimeError("第一次執行失敗")

    register_job(job_app.app, FLAKY, flaky, max_retries=3, retry_wait=1)
    with job_app.opened() as app:
        assert defer_job(app, FLAKY, {"probe_id": probe_id}) is True

    settings = Settings.model_validate(
        {"database_url": migrated_fresh.app_url, "worker_concurrency": 1}
    )

    def settled() -> bool:
        return [job["status"] for job in _jobs(migrated_fresh)] == ["failed", "succeeded"]

    async def run_worker_until_settled() -> None:
        async with job_app.opened_async() as app:
            worker = asyncio.create_task(app.run_worker_async(**worker_options(settings)))
            deadline = time.monotonic() + WAIT_SECONDS
            while not settled():
                assert time.monotonic() < deadline, _jobs(migrated_fresh)
                assert not worker.done(), worker
                await asyncio.sleep(0.2)
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    with structlog.testing.capture_logs() as logs:
        asyncio.run(run_worker_until_settled())

    first, second = _jobs(migrated_fresh)
    # 從「執行中」結束時 Procrastinate 會把 attempts 加一（這次執行也算一次）。
    assert (first["status"], first["attempts"], first["queueing_lock"]) == (
        "failed",
        1,
        f"flaky_probe:{probe_id}",
    )
    # B 只執行一次就成功（成功結束時 attempts 一樣加一）。
    assert (second["status"], second["attempts"]) == ("succeeded", 1)
    assert runs == ["run", "run"]
    warnings = [
        log for log in logs if log["event"].startswith("工作失敗要重試，但同一件事已經在排隊")
    ]
    assert [(log["failed_job_id"], log["log_level"]) for log in warnings] == [
        (first["id"], "warning")
    ]


# --- worker 比遷移先起來 ------------------------------------------------------------------


def test_worker_waits_for_migration_then_takes_jobs(fresh_database: TestDatabase) -> None:
    env = {"DATABASE_URL": fresh_database.app_url, "RADAR_WORKER_RESTART_INTERVAL": "1"}
    worker = _spawn([sys.executable, "-m", "radar.entrypoints.worker"], env)
    try:
        time.sleep(3)
        # worker 不遷移、不建任何表。
        assert not fresh_database.has_table("alembic_version")
        assert not fresh_database.has_table("procrastinate_jobs")

        migrated = _radar("migrate", env={"RADAR_MIGRATION_DATABASE_URL": fresh_database.owner_url})
        assert migrated.returncode == 0, migrated.stdout + migrated.stderr
        queued = _radar("enqueue", "sweep", env=env)
        assert queued.returncode == 0, queued.stdout + queued.stderr

        _wait_until(
            lambda: [job["status"] for job in _jobs(fresh_database)] == ["succeeded"],
            "遷移後 worker 自動開始接工作",
        )
    finally:
        code, records, err = _stop(worker)

    assert code == 0, err
    assert err == ""
    warnings = [r for r in records if r["level"] == "warning" and "稍後重開" in str(r["event"])]
    assert warnings
    assert all("radar_app@" not in json.dumps(r, ensure_ascii=False) for r in records)
