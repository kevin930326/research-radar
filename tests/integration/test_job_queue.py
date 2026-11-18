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
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import pytest
import structlog
from procrastinate import sql as procrastinate_sql
from procrastinate import utils as procrastinate_utils

from radar.adapters.fake import FakeHeartbeat
from radar.adapters.jobs import create_job_app, defer_job, register_job, retry_stalled_jobs
from radar.domain.jobs import PDF_LOCK, RUN_WEEKLY_UPDATE, JobSpec, Queue
from radar.domain.settings import Settings
from radar.domain.sweep import SweepEntry
from radar.entrypoints.worker.main import worker_options
from radar.repo.db import Database
from radar.services.sweep import SweepResult, sweep
from tests.database import APP_ROLE, ROOT, TestDatabase, migrate

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


# 每週更新發佈後排入的 AI 工作（Story 2.8 向量化、Story 2.9 Batch 規劃與送出）。
_AFTER_PUBLISH_JOBS = frozenset({"embed_papers", "plan_llm_batches", "submit_llm_batch"})


def _weekly_jobs(database: TestDatabase) -> list[dict[str, object]]:
    """每週更新的工作（不含發佈後排入的 AI 工作，Story 2.8、2.9）。"""
    return [job for job in _jobs(database) if job["task_name"] not in _AFTER_PUBLISH_JOBS]


def _embed_jobs(database: TestDatabase) -> list[dict[str, object]]:
    return [job for job in _jobs(database) if job["task_name"] == "embed_papers"]


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
    assert fresh_database.revisions() == {"0020"}


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
    assert "可用的工作：poll-batches、sweep" in result.stderr


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


def _retry_with_change_after_candidates(
    database: TestDatabase, monkeypatch: pytest.MonkeyPatch, job_id: int, change: str
) -> int:
    """查出候選之後、核對之前，用管理者連線對這筆工作的 worker 做 `change`（參數是工作 ID）。

    一次只改一個核對條件，確認每個條件各自擋得住（不靠 attempts 一起變）。
    """
    stalled_query = procrastinate_sql.queries["select_stalled_jobs_by_heartbeat"]
    job_app = create_job_app(database.app_url, max_connections=2, connect_timeout=5)
    with job_app.opened() as app:
        connector = app.connector.get_sync_connector()
        real_query_all = connector.execute_query_all

        def change_after_select(**kwargs: Any) -> list[dict[str, Any]]:
            rows = real_query_all(**kwargs)
            if kwargs["query"] == stalled_query:
                assert [row["id"] for row in rows] == [job_id]
                with psycopg.connect(database.admin_url, autocommit=True) as admin:
                    admin.execute(change, (job_id,))
            return rows

        monkeypatch.setattr(connector, "execute_query_all", change_after_select)
        return retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3)


def _job_worker(database: TestDatabase, job_id: int) -> object:
    with psycopg.connect(database.admin_url, autocommit=True) as admin:
        row = admin.execute(
            "SELECT worker_id FROM procrastinate_jobs WHERE id = %s", (job_id,)
        ).fetchone()
    assert row is not None
    return row[0]


def test_candidate_whose_worker_reported_in_again_is_skipped(
    migrated_fresh: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 只動心跳：worker 在查出候選之後又報到了（attempts、worker_id 都沒變）→ 不可以重排。
    job = _insert_job(migrated_fresh, status="doing", queueing_lock=None)
    worker = _job_worker(migrated_fresh, job)

    retried = _retry_with_change_after_candidates(
        migrated_fresh,
        monkeypatch,
        job,
        "UPDATE procrastinate_workers SET last_heartbeat = now()"
        " WHERE id = (SELECT worker_id FROM procrastinate_jobs WHERE id = %s)",
    )

    assert retried == 0
    assert _statuses(migrated_fresh) == {job: ("doing", 0)}
    assert _job_worker(migrated_fresh, job) == worker


def test_candidate_whose_worker_row_was_deleted_is_still_retried(
    migrated_fresh: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 失聯的 worker 列在查出候選之後被清掉（例如新的 worker 啟動時清理）：worker_id 變 NULL，
    # 工作沒有人在做，照常重排。
    job = _insert_job(migrated_fresh, status="doing", queueing_lock=None)

    retried = _retry_with_change_after_candidates(
        migrated_fresh,
        monkeypatch,
        job,
        "DELETE FROM procrastinate_workers"
        " WHERE id = (SELECT worker_id FROM procrastinate_jobs WHERE id = %s)",
    )

    assert retried == 1
    assert _statuses(migrated_fresh) == {job: ("todo", 1)}
    assert _job_worker(migrated_fresh, job) is None


def test_candidate_now_held_by_another_worker_is_skipped(
    migrated_fresh: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 只動 worker_id：工作在查出候選之後換到另一個 worker 名下（attempts 沒變）→ 不是候選上那一筆
    # 的狀態了，不動它。另一個 worker 也是失聯的，所以只有 worker_id 這個條件擋得住。
    job = _insert_job(migrated_fresh, status="doing", queueing_lock=None)

    retried = _retry_with_change_after_candidates(
        migrated_fresh,
        monkeypatch,
        job,
        "WITH other AS ("
        " INSERT INTO procrastinate_workers (last_heartbeat)"
        " VALUES (now() - interval '1 hour') RETURNING id)"
        " UPDATE procrastinate_jobs SET worker_id = (SELECT id FROM other) WHERE id = %s",
    )

    assert retried == 0
    assert _statuses(migrated_fresh) == {job: ("doing", 0)}


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


# 鎖等待測試的期限：連線、每個查詢、等鎖都有上限，資料庫沒回應時也不會讓測試卡住。
# statement_timeout／lock_timeout 是資料庫那端的期限，資料庫整個沒回應時管不到客戶端；所以主執行緒
# 碰資料庫的每一步都交給 daemon 執行緒跑、主執行緒最多等 `_STEP_SECONDS`（見 `_within`）。
_LIMIT_MS = int(WAIT_SECONDS * 1000)
_STEP_SECONDS = 15.0
_SESSION_LIMITS = f"-c statement_timeout={_LIMIT_MS} -c lock_timeout={_LIMIT_MS}"
_POLL_LIMITS = "-c statement_timeout=5000 -c lock_timeout=5000"


def _bounded_connect(url: str, *, autocommit: bool, options: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=autocommit, connect_timeout=5, options=options)


def _with_session_limits(url: str) -> str:
    """連線字串加上 `options`：佇列連線池開出來的每條連線也有查詢與等鎖的期限。"""
    return f"{url}{'&' if '?' in url else '?'}options={quote(_SESSION_LIMITS)}"


def _terminate_backends(database: TestDatabase, role: str) -> None:
    """清理用：結束這個測試資料庫裡 `role` 的所有連線（盡力而為，失敗或逾時都不蓋掉原本的錯誤）。"""

    def terminate() -> None:
        with _bounded_connect(database.admin_url, autocommit=True, options=_POLL_LIMITS) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = current_database() AND usename = %s",
                (role,),
            )

    with contextlib.suppress(psycopg.Error, TimeoutError):
        _within(_STEP_SECONDS, terminate, "清理連線")


class _DaemonCall[T]:
    """在 daemon 執行緒跑 `func`。它卡住時，測試和 pytest 都不必等它結束（不 join 到底）。"""

    def __init__(self, func: Callable[[], T], name: str) -> None:
        self._func = func
        self._name = name
        self._done = False
        self._value: T | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)

    def _run(self) -> None:
        try:
            self._value = self._func()
            self._done = True
        except BaseException as exc:  # 交回主執行緒再丟
            self._error = exc

    def start(self) -> None:
        self._thread.start()

    def running(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout)

    def result(self, timeout: float) -> T:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError(f"{self._name} 超過 {timeout} 秒還沒結束")
        if self._error is not None:
            raise self._error
        assert self._done
        return self._value  # type: ignore[return-value]  # _done 為真時就是 func 的回傳值


def _within[T](seconds: float, func: Callable[[], T], what: str) -> T:
    """在 daemon 執行緒跑 `func`，主執行緒最多等 `seconds` 秒；資料庫沒回應就丟 TimeoutError。"""
    call = _DaemonCall(func, name=what)
    call.start()
    return call.result(timeout=seconds)


def test_sweep_waiting_on_row_lock_rechecks_after_the_other_transaction_commits(
    migrated_fresh: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 上面兩個測試是「第一輪做完、才跑第二輪」，沒有真的撞在一起。這裡用兩條連線製造列鎖等待：
    # 連線 A 鎖住工作列、在交易裡把它重排並交給健康的 worker（還沒提交）；連線 B 的 sweep 從
    # 已提交的資料查到它是候選，卡在 FOR UPDATE 等 A。A 提交後，B 必須用最新的那一版重新核對
    # （attempts 已經變了），回傳 0、不動它。
    # 期限：每條連線 5 秒內要連上；A、B 的每個查詢與等鎖最多 WAIT_SECONDS 秒（A 另外設交易閒置
    # 上限，測試行程掛了伺服器也會放掉鎖）；B 在 daemon 執行緒跑，逾時就結束它的連線、不等它。
    # 主執行緒碰資料庫的每一步（含 watcher、清理、A 的查詢）都用 `_within` 限時，資料庫整個沒回應
    # 時測試也會在有限時間內失敗結束。
    stalled = _within(
        _STEP_SECONDS,
        lambda: _insert_job(migrated_fresh, status="doing", queueing_lock=None),
        "建立測試工作",
    )

    def register_healthy_worker() -> tuple[Any, ...] | None:
        with _bounded_connect(
            migrated_fresh.admin_url, autocommit=True, options=_SESSION_LIMITS
        ) as admin:
            return admin.execute("SELECT * FROM procrastinate_register_worker_v1()").fetchone()

    healthy = _within(_STEP_SECONDS, register_healthy_worker, "註冊健康的 worker")
    assert healthy is not None
    stalled_query = procrastinate_sql.queries["select_stalled_jobs_by_heartbeat"]
    job_app = create_job_app(
        _with_session_limits(migrated_fresh.app_url), max_connections=2, connect_timeout=5
    )
    candidates: list[object] = []

    def sweep_on_connection_b() -> int:
        with job_app.opened() as app:
            connector = app.connector.get_sync_connector()
            real_query_all = connector.execute_query_all

            def record_candidates(**kwargs: Any) -> list[dict[str, Any]]:
                rows = real_query_all(**kwargs)
                if kwargs["query"] == stalled_query:
                    candidates.extend(row["id"] for row in rows)
                return rows

            monkeypatch.setattr(connector, "execute_query_all", record_candidates)
            return retry_stalled_jobs(app, seconds_since_heartbeat=60, max_retries=3)

    connection_a = _within(
        _STEP_SECONDS,
        lambda: _bounded_connect(
            migrated_fresh.admin_url,
            autocommit=False,
            options=f"{_SESSION_LIMITS} -c idle_in_transaction_session_timeout={2 * _LIMIT_MS}",
        ),
        "連線 A 連上資料庫",
    )
    sweep_b = _DaemonCall(sweep_on_connection_b, name="連線 B 的 sweep")
    # A 的某一步逾時後，做那一步的執行緒可能還在用 connection_a；記下來，清理時就不跨執行緒去關它。
    a_stuck = False

    def on_a[T](seconds: float, func: Callable[[], T], what: str) -> T:
        nonlocal a_stuck
        try:
            return _within(seconds, func, what)
        except TimeoutError:
            a_stuck = True
            raise

    try:

        def lock_and_hand_over_on_a() -> int:
            connection_a.execute(
                "SELECT id FROM procrastinate_jobs WHERE id = %s FOR UPDATE", (stalled,)
            )
            connection_a.execute(
                "SELECT procrastinate_retry_job_v2(%s, now(), NULL, NULL, NULL)", (stalled,)
            )
            taken = connection_a.execute(
                "SELECT id FROM procrastinate_fetch_job_v2(ARRAY['maintenance'], %s)",
                (healthy[0],),
            ).fetchone()
            assert taken == (stalled,)
            return connection_a.info.backend_pid

        a_pid = on_a(WAIT_SECONDS + 5, lock_and_hand_over_on_a, "連線 A 鎖住並交出工作")

        sweep_b.start()

        def count_waiting_on_a() -> int:
            with _bounded_connect(
                migrated_fresh.admin_url, autocommit=True, options=_POLL_LIMITS
            ) as watcher:
                row = watcher.execute(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE %s = ANY(pg_blocking_pids(pid)) AND wait_event_type = 'Lock'",
                    (a_pid,),
                ).fetchone()
            return 0 if row is None else row[0]

        def b_is_waiting_on_a() -> bool:
            if not sweep_b.running():
                return True  # B 沒有等鎖就結束了：跳出等待，由下面的斷言報錯。
            return _within(_STEP_SECONDS, count_waiting_on_a, "查 B 是否在等 A 的鎖") > 0

        _wait_until(b_is_waiting_on_a, "連線 B 的 sweep 卡在等 A 的列鎖")
        assert sweep_b.running(), "B 沒有等鎖就結束了，沒有製造出交錯"
        assert candidates == [stalled]

        on_a(WAIT_SECONDS + 5, connection_a.commit, "連線 A 提交")
        retried = sweep_b.result(timeout=WAIT_SECONDS)
    finally:
        # 放掉 A 的鎖：A 每一步都準時結束時才關連線（關連線也限時，卡住就不管它）。A 有一步逾時，
        # 原執行緒可能還拿著這條連線，另開執行緒去關會和它搶，所以不碰它；鎖改由資料庫端放掉
        # （A 的查詢期限、交易閒置期限，以及 fixture 拆除時的 DROP DATABASE … WITH (FORCE)）。
        # B 還沒結束就結束它的連線，最多再等 5 秒，之後不管它（daemon 執行緒不會擋住 pytest 結束）。
        if not a_stuck:
            with contextlib.suppress(TimeoutError):
                _within(5, connection_a.close, "關閉連線 A")
        sweep_b.join(timeout=5)
        if sweep_b.running():
            _terminate_backends(migrated_fresh, APP_ROLE)
            sweep_b.join(timeout=5)

    assert retried == 0
    assert _within(_STEP_SECONDS, lambda: _statuses(migrated_fresh), "讀工作狀態") == {
        stalled: ("doing", 1)
    }
    assert (
        _within(_STEP_SECONDS, lambda: _job_worker(migrated_fresh, stalled), "讀工作的 worker")
        == healthy[0]
    )


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


# --- 每週更新：radar enqueue weekly-update ＋ 正式的 worker（Story 1.8） ------------------------

# 正式的 worker（main()），只把 bootstrap 組的 OpenAlex 與物件儲存換成替身（不連網路）；
# 兩個工作照正式的 register_tasks 登記與執行，期刊清單用 repo 的 config/journals.yaml。
WEEKLY_UPDATE_WORKER_SCRIPT = """
import contextlib

import radar.entrypoints.worker.main as worker_main
import radar.entrypoints.worker.tasks as worker_tasks
from radar.adapters.fake import FakeObjectStore, FakePaperSource


def fake_paper_source(settings, openalex_config):
    # 正式的 adapter 用 with 開關；替身包一層 nullcontext。每本期刊回一頁空結果。
    return contextlib.nullcontext(FakePaperSource())


worker_tasks.build_paper_source = fake_paper_source
worker_tasks.build_object_store = lambda settings: FakeObjectStore()
raise SystemExit(worker_main.main())
"""


def test_enqueue_weekly_update_runs_both_jobs_on_worker(
    migrated_fresh: TestDatabase, tmp_path: Path
) -> None:
    script = tmp_path / "weekly_update_worker.py"
    script.write_text(WEEKLY_UPDATE_WORKER_SCRIPT, encoding="utf-8")
    env = {"DATABASE_URL": migrated_fresh.app_url}
    worker = _spawn([sys.executable, "-I", str(script)], env)
    try:
        result = _radar("enqueue", "weekly-update", env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "已排入 weekly-update" in result.stdout

        _wait_until(
            lambda: (
                [(job["task_name"], job["status"]) for job in _weekly_jobs(migrated_fresh)]
                == [("weekly_update", "succeeded"), ("run_weekly_update", "succeeded")]
            ),
            "weekly_update 排入的 run_weekly_update 也執行成功",
        )
    finally:
        code, records, err = _stop(worker)

    assert code == 0, err
    # Story 2.8：發佈快照後排入一筆 embed_papers（沒有 Cloudflare 設定，在 worker 裡會失敗，
    # 不影響每週更新）；上面只看每週更新的工作。
    assert [job["queue_name"] for job in _embed_jobs(migrated_fresh)] == ["ai"]
    scheduled, run = _weekly_jobs(migrated_fresh)
    assert (scheduled["queue_name"], scheduled["lock"], scheduled["args"]) == ("ingest", None, {})
    assert scheduled["queueing_lock"] == "weekly_update"
    [(weekly_run_id, status, finished_at)] = _admin_rows(
        migrated_fresh, "SELECT id, status, fetch_finished_at FROM weekly_run"
    )
    # 抓完後接著套用（Story 1.9）：狀態從 fetching 變 published，快照 1 發佈。
    assert status == "published"
    assert _admin_rows(migrated_fresh, "SELECT number, weekly_run_id, status FROM snapshot") == [
        (1, weekly_run_id, "published")
    ]
    assert run["queue_name"] == "ingest"
    assert run["lock"] == "weekly_update"
    assert run["args"] == {"weekly_run_id": str(weekly_run_id)}
    assert run["queueing_lock"] == f"run_weekly_update:{weekly_run_id}"
    # 替身每本期刊回一頁空結果：14 本各送一次請求、各記一頁進度，全部抓完。
    assert finished_at is not None
    journals = _admin_rows(
        migrated_fresh, "SELECT journal_key, pages, fetched, finished_at FROM weekly_run_journal"
    )
    assert len(journals) == 14
    assert all(
        pages == 1 and fetched == 0 and done is not None for _, pages, fetched, done in journals
    )
    assert _admin_rows(migrated_fresh, "SELECT count(*) FROM api_call") == [(14,)]
    assert _admin_rows(migrated_fresh, "SELECT count(*) FROM paper") == [(0,)]
    started = [r for r in records if r["event"] == "每週更新已排入抓取"]
    assert [r["job_id"] for r in started] == [scheduled["id"]]
    assert [r["job_id"] for r in records if r["event"] == "每週更新抓取完成"] == [run["id"]]


# 正式的 worker（main()），OpenAlex 換成每頁停一下的替身（每本期刊一頁空結果）、物件儲存換成替身；
# run_weekly_update 照正式的 register_tasks 登記與執行（對抗式審查第 5 條）。
SLOW_WEEKLY_WORKER_SCRIPT = """
import contextlib
import sys
import time

import radar.entrypoints.worker.main as worker_main
import radar.entrypoints.worker.tasks as worker_tasks
from radar.adapters.fake import FakeObjectStore, FakePaperSource

seconds_per_page = float(sys.argv[1])


class SlowPaperSource(FakePaperSource):
    def fetch_journal_page(self, query, cursor):
        time.sleep(seconds_per_page)
        return super().fetch_journal_page(query, cursor)


def slow_paper_source(settings, openalex_config):
    return contextlib.nullcontext(SlowPaperSource())


worker_tasks.build_paper_source = slow_paper_source
worker_tasks.build_object_store = lambda settings: FakeObjectStore()
raise SystemExit(worker_main.main())
"""


def _job_events(database: TestDatabase) -> dict[tuple[object, str], datetime]:
    """每個工作開始與成功的時間（佇列自己記的事件）。"""
    rows = _admin_rows(
        database,
        "SELECT job_id, type::text, at FROM procrastinate_events"
        " WHERE type IN ('started', 'succeeded') ORDER BY id",
    )
    events: dict[tuple[object, str], datetime] = {}
    for job_id, kind, at in rows:
        assert isinstance(at, datetime)
        events[(job_id, str(kind))] = at
    return events


def test_weekly_update_runs_never_overlap_with_two_worker_slots(
    migrated_fresh: TestDatabase, tmp_path: Path
) -> None:
    # worker 同時可以跑 2 個工作；第一筆 run_weekly_update 執行中再排第二筆（同一把佇列鎖）：
    # 第二筆要等第一筆做完才開始，兩筆的執行時段不重疊。
    with migrated_fresh.connect(APP_ROLE) as connection:
        row = connection.execute(
            "INSERT INTO weekly_run (status) VALUES ('fetching') RETURNING id"
        ).fetchone()
    assert row is not None
    weekly_run_id = row[0]
    script = tmp_path / "slow_weekly_worker.py"
    script.write_text(SLOW_WEEKLY_WORKER_SCRIPT, encoding="utf-8")
    env = {"DATABASE_URL": migrated_fresh.app_url, "RADAR_WORKER_CONCURRENCY": "2"}
    # 14 本期刊各一頁、每頁停 0.3 秒：第一筆約 4 秒。
    worker = _spawn([sys.executable, "-I", str(script), "0.3"], env)
    job_app = create_job_app(migrated_fresh.app_url, max_connections=2, connect_timeout=5)
    try:
        with job_app.opened() as app:
            assert defer_job(app, RUN_WEEKLY_UPDATE, {"weekly_run_id": weekly_run_id}) is True
            _wait_until(
                lambda: [job["status"] for job in _weekly_jobs(migrated_fresh)] == ["doing"],
                "第一筆開始執行",
            )
            # 排隊鎖只擋排隊中的，第一筆執行中時第二筆排得進去（像 sweep 補排或手動再排）。
            assert defer_job(app, RUN_WEEKLY_UPDATE, {"weekly_run_id": weekly_run_id}) is True
            # worker 還有一個空位，第二筆仍在排隊：擋住它的是佇列鎖。
            time.sleep(1.0)
            assert [job["status"] for job in _weekly_jobs(migrated_fresh)] == ["doing", "todo"]
        _wait_until(
            lambda: [job["status"] for job in _weekly_jobs(migrated_fresh)] == ["succeeded"] * 2,
            "兩筆都執行成功",
        )
    finally:
        code, records, err = _stop(worker)

    assert code == 0, err
    # Story 2.8：發佈快照後排入一筆 embed_papers（沒有 Cloudflare 設定，在 worker 裡會失敗，
    # 不影響每週更新）；上面只看每週更新的工作。
    assert [job["queue_name"] for job in _embed_jobs(migrated_fresh)] == ["ai"]
    first, second = _weekly_jobs(migrated_fresh)
    assert first["lock"] == second["lock"] == "weekly_update"
    events = _job_events(migrated_fresh)
    first_started = events[(first["id"], "started")]
    first_done = events[(first["id"], "succeeded")]
    second_started = events[(second["id"], "started")]
    second_done = events[(second["id"], "succeeded")]
    assert first_started < first_done <= second_started < second_done, events
    # 第一筆抓完 14 本並套用、發佈；第二筆看到的是已發佈，略過（Story 1.9）。
    assert [r["job_id"] for r in records if r["event"] == "每週更新抓取完成"] == [first["id"]]
    assert [r["job_id"] for r in records if r["event"] == "每週更新已套用並發佈快照"] == [
        first["id"]
    ]
    assert [r["job_id"] for r in records if r["event"] == "每週更新不是抓取中，略過"] == [
        second["id"]
    ]
    assert _admin_rows(migrated_fresh, "SELECT count(*) FROM api_call") == [(14,)]


# --- 每週更新端到端：抓取中斷、重試續跑、套用、快照 1、匯入報表（Story 1.9） ----------------

# 正式的 worker（main()），OpenAlex 換成回 I&O 三頁（錄好的回應，CC0）、第 2 頁第一次失敗的替身，
# 物件儲存換成替身；替身放在模組層級，工作重試時沿用同一個（失敗只發生一次）。
RESUMING_WEEKLY_WORKER_SCRIPT = """
import contextlib
import sys
from datetime import UTC, datetime

sys.path.insert(0, sys.argv[1])  # 讀錄好的回應（tests/openalex_cassette.py）

import radar.entrypoints.worker.main as worker_main
import radar.entrypoints.worker.tasks as worker_tasks
from radar.adapters.fake import FakeObjectStore, FakePaperSource
from radar.domain.raw_records import RawRecord, RawSource
from tests.openalex_cassette import ISSN_L, cassette_pages, work_bytes

FETCHED_AT = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
pages = [
    [
        RawRecord(RawSource.OPENALEX, work["id"].rsplit("/", 1)[-1], work_bytes(work), FETCHED_AT)
        for work in page
    ]
    for page in cassette_pages()
]
source = FakePaperSource({ISSN_L: pages})
source.fail_once(ISSN_L, 2)
store = FakeObjectStore()

worker_tasks.build_paper_source = lambda settings, openalex_config: contextlib.nullcontext(source)
worker_tasks.build_object_store = lambda settings: store
raise SystemExit(worker_main.main())
"""


def test_weekly_update_resumes_after_retry_applies_and_reports(
    migrated_fresh: TestDatabase, tmp_path: Path
) -> None:
    script = tmp_path / "resuming_weekly_worker.py"
    script.write_text(RESUMING_WEEKLY_WORKER_SCRIPT, encoding="utf-8")
    env = {"DATABASE_URL": migrated_fresh.app_url, "RADAR_JOB_RETRY_WAIT": "1"}
    worker = _spawn([sys.executable, "-I", str(script), str(ROOT)], env)
    try:
        result = _radar("enqueue", "weekly-update", env=env)
        assert result.returncode == 0, result.stdout + result.stderr

        _wait_until(
            lambda: (
                [(job["task_name"], job["status"]) for job in _weekly_jobs(migrated_fresh)]
                == [("weekly_update", "succeeded"), ("run_weekly_update", "succeeded")]
            ),
            "run_weekly_update 重試後續跑、套用成功",
            timeout=60,
        )
    finally:
        code, records, err = _stop(worker)

    assert code == 0, err
    # Story 2.8：發佈快照後排入一筆 embed_papers（沒有 Cloudflare 設定，在 worker 裡會失敗，
    # 不影響每週更新）；上面只看每週更新的工作。
    assert [job["queue_name"] for job in _embed_jobs(migrated_fresh)] == ["ai"]
    _scheduled, run = _weekly_jobs(migrated_fresh)
    # 第 1 次在 I&O 第 2 頁失敗，第 2 次從那一頁續跑、抓完並套用。
    assert run["attempts"] == 2
    [(weekly_run_id, status)] = _admin_rows(migrated_fresh, "SELECT id, status FROM weekly_run")
    assert status == "published"
    assert _admin_rows(migrated_fresh, "SELECT number, weekly_run_id, status FROM snapshot") == [
        (1, weekly_run_id, "published")
    ]
    assert _admin_rows(migrated_fresh, "SELECT count(*) FROM paper") == [(200,)]
    assert _admin_rows(migrated_fresh, "SELECT count(*) FROM ingest_staging") == [(0,)]
    assert [r["event"] for r in records if r.get("journal") == "io" and r["level"] == "error"] == [
        "每週更新抓取失敗，進度已保存"
    ]
    # 心跳在套用之後送（沒設網址：記一筆警告、不送），只有一次。
    heartbeats = [r for r in records if r["event"] == "這個排程沒有設定心跳網址，不送"]
    assert [(r["check"], r["job_id"]) for r in heartbeats] == [("weekly-update", run["id"])]

    report = _radar("weekly-run", "report", env={"DATABASE_URL": migrated_fresh.app_url})

    assert report.returncode == 0, report.stdout + report.stderr
    lines = report.stdout.splitlines()
    assert lines[0] == f"每週更新 {weekly_run_id}"
    assert lines[1] == "狀態：已發佈"
    assert any(line.startswith("快照 1：截至 ") for line in lines)
    io_rows = [line for line in lines if line.split()[:1] == ["io"]]
    # 每年的過濾前篇數、排除篇數、和基準比對各一列；I&O 的基準是 180。
    assert len(io_rows) == 3
    assert io_rows[0].split()[-1] == "248"
    assert io_rows[2].split()[1:4] == ["225", "180", "+25.0%"]
