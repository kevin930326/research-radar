"""背景工作的架構規則（AD-14、AD-15、ADR-0004、ADR-0019）：

- 凍結的 Procrastinate SQL 和安裝的套件一字不差，版本對得上。
- worker 登記的每個工作都在 `JOBS`、佇列一致、參數只有 `*_id: UUID`；壞簽名登記不進去。
- worker 沒有任何 Procrastinate 週期任務。
- `deploy/crontab` 只呼叫 `radar enqueue`，和 `JOBS` 的排程一一對應。
- 有 `status` 欄的表，不是在 sweep 的登記清單裡，就是在例外清單裡寫明理由。
"""

import ast
import importlib.metadata
import importlib.resources
import inspect
import re
import uuid
from pathlib import Path

import psycopg
import pytest

from radar.adapters.jobs import InvalidJobSignatureError, register_job
from radar.domain.jobs import JOBS, JobSpec, Queue, find_job, scheduled_jobs
from radar.domain.settings import Settings, heartbeat_url_field
from radar.domain.sweep import SWEEP_REGISTRY
from radar.entrypoints.bootstrap import build_worker
from radar.entrypoints.worker.tasks import register_tasks
from tests.database import OWNER_ROLE, ROOT, TestDatabase

PROCRASTINATE_VERSION = "3.11.0"
FROZEN_SQL = ROOT / "migrations" / "procrastinate" / f"schema-{PROCRASTINATE_VERSION}.sql"
FROZEN_MARKER = "-- ===== 以下是套件原檔，一字不改 =====\n"
CRONTAB = ROOT / "deploy" / "crontab"
# crontab 每一行的指令：在開著的 worker 容器裡執行 radar enqueue（不另開容器）。
ENQUEUE_COMMAND = re.compile(
    r"docker compose --project-directory /opt/research-radar exec -T worker"
    r" radar enqueue (?P<schedule>[a-z][a-z0-9-]*)"
)
CRON_FIELD = r"[0-9*/,-]+"
CRON_LINE = re.compile(rf"(?P<when>(?:{CRON_FIELD}\s+){{4}}{CRON_FIELD})\s+(?P<command>.+)")

# 有 status 欄、但不由 sweep 掃的表，要寫明理由。
STATUS_TABLE_EXCEPTIONS: dict[str, str] = {
    "procrastinate_jobs": (
        "Procrastinate 自己的佇列表；卡在執行中的工作由 sweep 的 stalled job 重試處理，"
        "不是登記清單的狀態表"
    ),
    "snapshot": (
        "在每週更新的套用交易裡建立並發佈（ADR-0026），交易外看不到建立中的快照，沒有會卡住的"
        "中間狀態；推進它的是已登記的 weekly_run（抓完待套用時仍是 fetching，由 sweep 補排）"
    ),
    "derivation_run": (
        "LLM 呼叫的來歷（Story 2.2、ADR-0030）：卡在 running 時不是重新排入工作，而是由 sweep 的"
        "另一步（expire_stale_runs）判定逾時改成 failed、留給下一次再跑；Batch 的跟著批次走"
    ),
    "zh_summary": "AI 表頭表（ADR-0015）：status 是結果的版本狀態，沒有會卡住的中間狀態",
    "classification": "AI 表頭表（ADR-0015）：status 是結果的版本狀態，沒有會卡住的中間狀態",
    "research_model": "AI 表頭表（ADR-0015）：status 是結果的版本狀態，沒有會卡住的中間狀態",
}

SETTINGS = Settings.model_validate({"database_url": "postgresql://radar_app@db:5432/radar"})


# --- 凍結的 SQL ---------------------------------------------------------------------------


def test_installed_procrastinate_matches_frozen_version() -> None:
    assert importlib.metadata.version("procrastinate") == PROCRASTINATE_VERSION
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"procrastinate=={PROCRASTINATE_VERSION}"' in pyproject


def test_frozen_sql_is_the_package_schema_verbatim() -> None:
    package_sql = (
        importlib.resources.files("procrastinate").joinpath("sql/schema.sql").read_text("utf-8")
    )
    frozen = FROZEN_SQL.read_text(encoding="utf-8")

    header, marker, body = frozen.partition(FROZEN_MARKER)
    assert marker, "凍結檔要有原檔開始的分隔線"
    assert body == package_sql
    assert "MIT" in header
    assert PROCRASTINATE_VERSION in header


def test_migration_runs_the_frozen_file_without_importing_procrastinate() -> None:
    migration = (ROOT / "migrations" / "versions" / "0002_procrastinate_schema.py").read_text(
        encoding="utf-8"
    )

    assert f"schema-{PROCRASTINATE_VERSION}.sql" in migration
    assert not re.search(r"^\s*(import|from)\s+procrastinate", migration, re.MULTILINE)
    # 遷移自己不建擴充套件（凍結檔頭的 plpgsql 是 PostgreSQL 內建、已存在，只發 NOTICE）。
    tree = ast.parse(migration)
    docstring = ast.get_docstring(tree, clean=False)
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value != docstring
    ]
    assert not [text for text in literals if "EXTENSION" in text.upper()]


# --- worker 登記的工作 --------------------------------------------------------------------


def _registered_tasks() -> dict[str, object]:
    """正式的 worker 登記的工作（不連線）；不含 Procrastinate 內建的工作。"""
    parts = build_worker(SETTINGS, queue_pool_size=2, app_pool_size=3, heartbeat_urls={})
    register_tasks(parts.job_app.app, parts, SETTINGS)
    app = parts.job_app.app
    assert app.periodic_registry.periodic_tasks == {}
    return {name: task for name, task in app.tasks.items() if not task.name.startswith("builtin:")}


def test_worker_registers_exactly_the_jobs_list() -> None:
    assert set(_registered_tasks()) == {job.name for job in JOBS}


def test_every_registered_job_takes_only_uuid_ids_on_its_queue() -> None:
    for name, task in _registered_tasks().items():
        job = find_job(name)
        assert getattr(task, "queue") == job.queue.value, name  # noqa: B009
        func = inspect.unwrap(getattr(task, "func"))  # noqa: B009
        for parameter in inspect.signature(func, eval_str=True).parameters.values():
            assert parameter.name.endswith("_id"), (name, parameter.name)
            assert parameter.annotation is uuid.UUID, (name, parameter.name)


def test_register_job_rejects_bad_signature() -> None:
    parts = build_worker(SETTINGS, queue_pool_size=2, app_pool_size=3, heartbeat_urls={})

    def identify_upload(upload_item_id: str) -> None:
        pass

    with pytest.raises(InvalidJobSignatureError):
        register_job(
            parts.job_app.app,
            JobSpec("identify_upload", Queue.PDF),
            identify_upload,
            max_retries=0,
            retry_wait=1,
        )


def test_no_periodic_tasks_anywhere_in_radar() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "radar").rglob("*.py"))
        if ".periodic(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# --- crontab ------------------------------------------------------------------------------


def _crontab_schedules() -> list[str]:
    schedules: list[str] = []
    for line in CRONTAB.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = CRON_LINE.fullmatch(stripped)
        assert match, f"不是排程行：{line}"
        command = ENQUEUE_COMMAND.fullmatch(match["command"])
        assert command, f"crontab 只能呼叫 radar enqueue：{line}"
        schedules.append(command["schedule"])
    return schedules


def test_crontab_matches_scheduled_jobs_one_to_one() -> None:
    schedules = _crontab_schedules()

    assert len(schedules) == len(set(schedules))
    assert sorted(schedules) == sorted(job.check for job in scheduled_jobs())


def test_weekly_update_runs_once_a_week() -> None:
    # Story 1.11：分、時是固定的一個數字，日、月是 `*`，星期是一個值（0–7）→ 一週剛好一次。
    # 時間本身（UTC 週日 19:00＝台北週一 03:00）由 Story 3.19 確認主機時區時再看，這裡不寫死。
    lines = [
        line
        for line in CRONTAB.read_text(encoding="utf-8").splitlines()
        if line.endswith(" radar enqueue weekly-update")
    ]
    assert len(lines) == 1
    match = CRON_LINE.fullmatch(lines[0].strip())
    assert match
    minute, hour, day, month, weekday = match["when"].split()
    assert minute.isdigit()
    assert 0 <= int(minute) <= 59
    assert hour.isdigit()
    assert 0 <= int(hour) <= 23
    assert (day, month) == ("*", "*")
    assert weekday.isdigit()
    assert 0 <= int(weekday) <= 7


def test_sweep_runs_every_15_minutes() -> None:
    lines = [
        line for line in CRONTAB.read_text(encoding="utf-8").splitlines() if line.endswith(" sweep")
    ]
    assert len(lines) == 1
    assert lines[0].startswith("*/15 * * * * ")


def test_every_schedule_has_its_own_heartbeat_setting() -> None:
    fields = {heartbeat_url_field(job.check) for job in scheduled_jobs()}
    assert fields <= set(Settings.model_fields)
    declared = {name for name in Settings.model_fields if name.startswith("heartbeat_url_")}
    assert declared == fields


# --- sweep 的登記清單 ---------------------------------------------------------------------


def test_registry_jobs_are_in_jobs_list() -> None:
    for entry in SWEEP_REGISTRY:
        assert entry.job in JOBS, entry


def test_registry_jobs_take_exactly_the_row_id_parameter() -> None:
    # sweep 重新排入時只帶 `<表名>_id`；登記的工作參數不一樣，每次補排都會 TypeError。
    tasks = _registered_tasks()
    for entry in SWEEP_REGISTRY:
        task = tasks[entry.job.name]
        func = inspect.unwrap(getattr(task, "func"))  # noqa: B009
        parameters = tuple(inspect.signature(func, eval_str=True).parameters)
        assert parameters == (entry.id_parameter,), entry


def _status_tables(connection: psycopg.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT c.relname FROM pg_attribute a"
        " JOIN pg_class c ON c.oid = a.attrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
        " AND a.attname = 'status' AND a.attnum > 0 AND NOT a.attisdropped"
    ).fetchall()
    return {str(row[0]) for row in rows}


def status_table_problems(
    connection: psycopg.Connection, registered: set[str], exceptions: dict[str, str]
) -> list[str]:
    """回傳每一條違規；沒有違規時是空清單。"""
    tables = _status_tables(connection)
    problems = [
        f"{table}：有 status 欄，但不在 sweep 的登記清單，也不在例外清單"
        for table in sorted(tables - registered - set(exceptions))
    ]
    problems += [
        f"{table}：在例外清單裡，但資料庫沒有這張有 status 欄的表"
        for table in sorted(set(exceptions) - tables)
    ]
    problems += [f"{table}：例外沒有寫理由" for table, why in exceptions.items() if not why.strip()]
    return problems


def test_every_status_table_is_swept_or_excused(migrated_database: TestDatabase) -> None:
    registered = {entry.table for entry in SWEEP_REGISTRY}
    with migrated_database.connect(OWNER_ROLE) as connection:
        assert status_table_problems(connection, registered, STATUS_TABLE_EXCEPTIONS) == []


def test_status_table_check_catches_unregistered_table(migrated_database: TestDatabase) -> None:
    # 證明守門真的擋得住：在交易裡建一張有 status 欄的表，檢查完整個退回。
    # 已經登記的真的狀態表（例如 weekly_run）照樣算登記過，只有新建的這張沒有。
    already = {entry.table for entry in SWEEP_REGISTRY}
    with migrated_database.connect(OWNER_ROLE, autocommit=False) as connection:
        try:
            connection.execute(
                "CREATE TABLE weekly_probe (id uuid PRIMARY KEY DEFAULT uuidv7(), status text)"
            )
            problems = status_table_problems(connection, already, STATUS_TABLE_EXCEPTIONS)
            registered = status_table_problems(
                connection, {"weekly_probe", *already}, STATUS_TABLE_EXCEPTIONS
            )
        finally:
            connection.rollback()

    assert problems == ["weekly_probe：有 status 欄，但不在 sweep 的登記清單，也不在例外清單"]
    assert registered == []


def test_crontab_file_is_the_only_schedule_file() -> None:
    deploy = ROOT / "deploy"
    names = {path.name for path in deploy.iterdir() if path.is_file()}
    assert "crontab" in names
    assert not (deploy / ".gitkeep").exists()
    assert not list(Path(ROOT / "radar").rglob("crontab*"))
