"""遷移 0016（`batch_submission`、`batch_item`，Story 2.9、ADR-0041）對真的 PostgreSQL：降版、
升版、CHECK、唯一索引、外鍵與應用程式角色的權限。資料都是測試自己編的。"""

import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from radar.repo.db import Database
from tests.database import APP_ROLE, ROOT, TestDatabase, migrate
from tests.llm_support import create_paper, execute

BIN = Path(sys.executable).parent
BATCH_ID = "msgbatch_01FAKEbatchIdForTests"


def _alembic(database: TestDatabase, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BIN / "alembic"), *args],
        env={"PYTHONUNBUFFERED": "1", "RADAR_MIGRATION_DATABASE_URL": database.owner_url},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _app(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description is not None else []


def _submission(db: TestDatabase, **values: object) -> uuid.UUID:
    row: dict[str, object] = {"status": "queued", "item_count": 0, "estimated_max_cost_usd": 0}
    row.update(values)
    columns = ", ".join(row)
    placeholders = ", ".join("%s" for _ in row)
    [(submission_id,)] = _app(
        db,
        f"INSERT INTO batch_submission ({columns}) VALUES ({placeholders}) RETURNING id",
        *row.values(),
    )
    return uuid.UUID(str(submission_id))


def _run(db: TestDatabase) -> uuid.UUID:
    paper_id = create_paper(Database(db.app_url, connect_timeout=5, pool_size=1))
    [(run_id,)] = execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
        " input_sha256, input_kind, params, code_version, call_mode, status)"
        " VALUES (%s, 'zh_summary', 1, %s, 'claude-haiku-5-5', %s, 'abstract', '{}'::jsonb,"
        " 'test', 'batch', 'running') RETURNING id",
        paper_id,
        "a" * 64,
        uuid.uuid4().hex * 2,
    )
    return uuid.UUID(str(run_id))


def test_downgrade_to_0015_and_upgrade_again(db: TestDatabase) -> None:
    submission_id = _submission(db)
    _app(
        db,
        "INSERT INTO batch_item (batch_submission_id, run_id, estimated_max_cost_usd)"
        " VALUES (%s, %s, 0.001)",
        submission_id,
        _run(db),
    )

    down = _alembic(db, "downgrade", "0015")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0015"}
    assert not db.has_table("batch_submission")
    assert not db.has_table("batch_item")

    migrate(db)

    assert db.revisions() == {"0021"}
    assert db.has_table("batch_submission")
    assert db.has_table("batch_item")
    check = _alembic(db, "check")
    assert check.returncode == 0, check.stdout + check.stderr


def test_app_role_can_read_and_write_both_tables(db: TestDatabase) -> None:
    submission_id = _submission(db)
    item_id = _app(
        db,
        "INSERT INTO batch_item (batch_submission_id, run_id, estimated_max_cost_usd)"
        " VALUES (%s, %s, 0.001) RETURNING id",
        submission_id,
        _run(db),
    )[0][0]

    assert _app(db, "SELECT status, item_count FROM batch_submission") == [("queued", 0)]
    assert len(uuid.UUID(str(item_id)).hex) == 32
    _app(db, "UPDATE batch_submission SET item_count = 1 WHERE id = %s", submission_id)


@pytest.mark.parametrize(
    "values",
    [
        {"status": "unknown"},
        {"status": "queued", "provider_batch_id": BATCH_ID},
        {"status": "submitting", "provider_batch_id": BATCH_ID},
        {"status": "submitted"},
        {"status": "ended"},
        {"status": "submitted", "provider_batch_id": "has space", "submitted_at": "2026-11-15"},
        {"status": "submitted", "provider_batch_id": BATCH_ID},  # 沒有 submitted_at
        {"status": "queued", "submitted_at": "2026-11-15"},
        {"status": "failed"},  # 沒有 failure_reason
        {"status": "needs_attention"},
        {"status": "queued", "failure_reason": "batch_submit_uncertain"},
        {"status": "failed", "failure_reason": "Not A Code"},
        {"item_count": -1},
        {"estimated_max_cost_usd": -1},
        {"weekly_run_id": uuid.uuid4()},
    ],
)
def test_constraints_reject_inconsistent_rows(db: TestDatabase, values: dict[str, object]) -> None:
    with pytest.raises(psycopg.errors.IntegrityError):
        _submission(db, **values)


def test_valid_states_are_accepted(db: TestDatabase) -> None:
    _submission(db, status="submitting")
    _submission(
        db,
        status="submitted",
        provider_batch_id=BATCH_ID,
        submitted_at="2026-11-15T03:00:00Z",
    )
    _submission(db, status="failed", failure_reason="batch_input_changed")
    _submission(db, status="needs_attention", failure_reason="batch_submit_uncertain")
    _submission(
        db,
        status="needs_attention",
        failure_reason="batch_submit_uncertain",
        provider_batch_id="msgbatch_late",
    )
    [(weekly_run_id,)] = execute(
        db, "INSERT INTO weekly_run (status) VALUES ('published') RETURNING id"
    )
    _submission(db, weekly_run_id=weekly_run_id)


def test_provider_batch_id_is_unique_when_present(db: TestDatabase) -> None:
    values = {"status": "submitted", "provider_batch_id": BATCH_ID, "submitted_at": "2026-11-15"}
    _submission(db, **values)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _submission(db, **values)
    # 沒有 ID 的列可以很多筆。
    _submission(db)
    _submission(db)


def test_a_run_belongs_to_one_item_only(db: TestDatabase) -> None:
    submission_id = _submission(db)
    run_id = _run(db)
    insert = (
        "INSERT INTO batch_item (batch_submission_id, run_id, estimated_max_cost_usd)"
        " VALUES (%s, %s, 0.001)"
    )
    _app(db, insert, submission_id, run_id)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _app(db, insert, _submission(db), run_id)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _app(db, insert, submission_id, uuid.uuid4())
    with pytest.raises(psycopg.errors.CheckViolation):
        _app(
            db,
            "INSERT INTO batch_item (batch_submission_id, run_id, estimated_max_cost_usd)"
            " VALUES (%s, %s, -1)",
            submission_id,
            _run(db),
        )


def test_batch_item_has_no_status_column(db: TestDatabase) -> None:
    columns = {
        row[0]
        for row in execute(
            db,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'batch_item'",
        )
    }
    assert columns == {
        "id",
        "batch_submission_id",
        "run_id",
        "abstract_id",
        "estimated_max_cost_usd",
        "created_at",
    }
