"""遷移 0022（示範站匯出的狀態表 `demo_export`，Story 3.7、ADR-0061）對真的 PostgreSQL：降版、升版、
CHECK 約束與部分唯一索引。

- 降到 0021：`demo_export` 表與它的部分唯一索引都不見；快照還在。
- 升回 0022：表回來（降版會丟掉裡面的紀錄，匯出的檔案不受影響）。
- CHECK 擋下：序號 0、`failed` 沒有原因、有原因卻不是 `failed`、`succeeded` 沒有結束時間、`running`
  沒有 `run_token`、不認得的狀態。部分唯一索引：同一個快照只能有一筆 `queued` 或 `running`。
資料全是測試自己編的。
"""

import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from tests.database import TestDatabase, connect_admin, migrate
from tests.integration.test_migration_0016 import _alembic
from tests.llm_support import execute

AS_OF = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
IN_PROGRESS_INDEX = "uq_demo_export_snapshot_id_in_progress"


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _snapshot(db: TestDatabase) -> uuid.UUID:
    [(snapshot_id,)] = execute(
        db,
        "WITH r AS (INSERT INTO weekly_run (status) VALUES ('published') RETURNING id)"
        " INSERT INTO snapshot (number, weekly_run_id, status, as_of)"
        " SELECT 1, r.id, 'published', %s FROM r RETURNING id",
        AS_OF,
    )
    return uuid.UUID(str(snapshot_id))


def _tables_and_indexes(db: TestDatabase) -> tuple[set[str], set[str]]:
    with connect_admin(db.name) as admin:
        tables = admin.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
        indexes = admin.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        ).fetchall()
    return {str(row[0]) for row in tables}, {str(row[0]) for row in indexes}


def test_downgrade_to_0021_drops_the_table_and_upgrade_restores_it(db: TestDatabase) -> None:
    snapshot_id = _snapshot(db)
    execute(
        db,
        "INSERT INTO demo_export (snapshot_id, sequence, status) VALUES (%s, 1, 'queued')",
        snapshot_id,
    )

    down = _alembic(db, "downgrade", "0021")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0021"}
    tables, indexes = _tables_and_indexes(db)
    assert "demo_export" not in tables
    assert IN_PROGRESS_INDEX not in indexes
    assert execute(db, "SELECT id FROM snapshot") == [(snapshot_id,)]

    migrate(db)

    assert db.revisions() == {"0023"}
    tables, indexes = _tables_and_indexes(db)
    assert "demo_export" in tables
    assert IN_PROGRESS_INDEX in indexes
    assert execute(db, "SELECT count(*) FROM demo_export") == [(0,)]


@pytest.mark.parametrize(
    ("columns", "values", "constraint"),
    [
        ("sequence, status", "0, 'queued'", "ck_demo_export_ck_demo_export_sequence_positive"),
        ("sequence, status", "1, 'paused'", "ck_demo_export_ck_demo_export_status"),
        (
            "sequence, status, finished_at",
            "1, 'failed', now()",
            "ck_demo_export_ck_demo_export_failure_reason_iff_failed",
        ),
        (
            "sequence, status, failure_reason",
            "1, 'queued', 'error'",
            "ck_demo_export_ck_demo_export_failure_reason_iff_failed",
        ),
        (
            "sequence, status",
            "1, 'succeeded'",
            "ck_demo_export_ck_demo_export_finished_at_once_finished",
        ),
        (
            "sequence, status",
            "1, 'running'",
            "ck_demo_export_ck_demo_export_run_token_when_running",
        ),
    ],
    ids=[
        "sequence-zero",
        "unknown-status",
        "failed-without-reason",
        "reason-without-failed",
        "succeeded-without-finished-at",
        "running-without-token",
    ],
)
def test_checks_reject_bad_rows(
    db: TestDatabase, columns: str, values: str, constraint: str
) -> None:
    snapshot_id = _snapshot(db)

    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        execute(
            db,
            f"INSERT INTO demo_export (snapshot_id, {columns}) VALUES (%s, {values})",
            snapshot_id,
        )

    # 專案的命名規則會在遷移寫的名稱前再加一次 `ck_demo_export_`（其他 CHECK 也是這樣）。
    assert caught.value.diag.constraint_name == constraint


def test_one_queued_or_running_per_snapshot(db: TestDatabase) -> None:
    snapshot_id = _snapshot(db)
    execute(
        db,
        "INSERT INTO demo_export (snapshot_id, sequence, status, run_token)"
        " VALUES (%s, 1, 'running', gen_random_uuid())",
        snapshot_id,
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        execute(
            db,
            "INSERT INTO demo_export (snapshot_id, sequence, status) VALUES (%s, 2, 'queued')",
            snapshot_id,
        )
    # 結束的不算：同一個快照可以有很多筆結束的，再加一筆排隊中的。
    execute(
        db,
        "UPDATE demo_export SET status = 'succeeded', finished_at = now() WHERE snapshot_id = %s",
        snapshot_id,
    )
    execute(
        db,
        "INSERT INTO demo_export (snapshot_id, sequence, status) VALUES (%s, 2, 'queued')",
        snapshot_id,
    )
    assert execute(db, "SELECT count(*) FROM demo_export") == [(2,)]
