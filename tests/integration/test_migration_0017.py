"""遷移 0017（Batch 匯入：`llm_call` 的部分唯一索引、`batch_submission` 的 `ended_at`／
`imported_at`，Story 2.11、ADR-0043）對真的 PostgreSQL：降版、升版、CHECK、唯一索引。資料都是測試
自己編的。"""

import uuid
from typing import Any

import psycopg
import pytest

from tests.database import APP_ROLE, TestDatabase, migrate
from tests.integration.test_migration_0016 import BATCH_ID, _alembic, _run
from tests.llm_support import execute


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _app(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description is not None else []


def _submission(db: TestDatabase, status: str, **values: object) -> uuid.UUID:
    row: dict[str, object] = {
        "status": status,
        "item_count": 0,
        "estimated_max_cost_usd": 0,
        "provider_batch_id": f"msgbatch_{uuid.uuid4().hex}",
        "submitted_at": "2026-11-15T03:00:00Z",
    }
    row.update(values)
    columns = ", ".join(row)
    placeholders = ", ".join("%s" for _ in row)
    [(submission_id,)] = _app(
        db,
        f"INSERT INTO batch_submission ({columns}) VALUES ({placeholders}) RETURNING id",
        *row.values(),
    )
    return uuid.UUID(str(submission_id))


def _columns(db: TestDatabase) -> set[str]:
    return {
        str(row[0])
        for row in execute(
            db,
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'batch_submission'",
        )
    }


def _index(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db, "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_llm_call_batch_run_id'"
    )


def _ledger(db: TestDatabase, run_id: uuid.UUID, *, is_batch: bool) -> None:
    _app(
        db,
        "INSERT INTO llm_call (run_id, input_tokens, output_tokens, cache_creation_input_tokens,"
        " cache_read_input_tokens, cost_usd, price_tier, is_batch, output_text)"
        " VALUES (%s, 1, 1, 0, 0, 0.000001, 'standard', %s, '{}')",
        run_id,
        is_batch,
    )


def test_downgrade_to_0016_and_upgrade_again(db: TestDatabase) -> None:
    _submission(db, "submitted")

    down = _alembic(db, "downgrade", "0016")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0016"}
    assert {"ended_at", "imported_at"}.isdisjoint(_columns(db))
    assert _index(db) == []

    migrate(db)

    assert db.revisions() == {"0018"}
    assert {"ended_at", "imported_at"} <= _columns(db)
    [(definition,)] = _index(db)
    assert "UNIQUE" in definition
    assert "WHERE is_batch" in definition
    check = _alembic(db, "check")
    assert check.returncode == 0, check.stdout + check.stderr


@pytest.mark.parametrize(
    "values",
    [
        {"status": "ended"},  # 沒有 ended_at
        {"status": "imported", "ended_at": "2026-11-15T04:00:00Z"},  # 沒有 imported_at
        {"status": "imported", "imported_at": "2026-11-15T05:00:00Z"},  # 沒有 ended_at
    ],
)
def test_timestamps_are_required_once_ended_or_imported(
    db: TestDatabase, values: dict[str, object]
) -> None:
    status = str(values.pop("status"))
    with pytest.raises(psycopg.errors.CheckViolation):
        _submission(db, status, **values)


def test_valid_ended_and_imported_rows_are_accepted(db: TestDatabase) -> None:
    ended = _submission(db, "ended", ended_at="2026-11-15T04:00:00Z")
    _submission(db, "imported", ended_at="2026-11-15T04:00:00Z", imported_at="2026-11-15T05:00:00Z")
    _submission(db, "submitted", provider_batch_id=BATCH_ID)

    _app(
        db,
        "UPDATE batch_submission SET status = 'imported', imported_at = now() WHERE id = %s",
        ended,
    )


def test_a_batch_run_is_charged_at_most_once(db: TestDatabase) -> None:
    run_id = _run(db)
    _ledger(db, run_id, is_batch=True)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _ledger(db, run_id, is_batch=True)
    # 匯入用的衝突目標：同一筆 Batch run 的第二筆帳什麼都不做。
    inserted = _app(
        db,
        "INSERT INTO llm_call (run_id, input_tokens, output_tokens, cache_creation_input_tokens,"
        " cache_read_input_tokens, cost_usd, price_tier, is_batch, output_text)"
        " VALUES (%s, 1, 1, 0, 0, 0.000001, 'standard', true, '{}')"
        " ON CONFLICT (run_id) WHERE is_batch DO NOTHING RETURNING id",
        run_id,
    )
    assert inserted == []
