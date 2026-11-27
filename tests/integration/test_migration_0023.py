"""遷移 0023（全文檢索的資料 `paper_search_document`、`api_call.outcome`，Story 4.1、ADR-0005）
對真的 PostgreSQL：降版、升版、回填、觸發器、權限與 CHECK。

- 降到 0022：表、觸發器、函式、`outcome` 欄都不見；論文與摘要還在。
- 升回 0023：既有的論文全部回填（標題 A＋目前的摘要 B；沒有目前的摘要只有標題）。
- 觸發器：論文新增、改標題；摘要新增、改內容、換成另一個來源（`is_current` 從一筆換到另一筆）、刪除
  之後，那一篇的資料跟著重算；改標題以外的欄位不重算。
- 權限：應用程式角色只能讀這張表，不能直接寫、不能呼叫重算函式；經觸發器寫得進去。
- `api_call.outcome` 只收四個值或 NULL。

資料全是測試自己編的。
"""

import uuid
from typing import Any

import psycopg
import pytest

from radar.repo.db import Database
from tests.database import APP_ROLE, TestDatabase, connect_admin, migrate
from tests.integration.test_migration_0016 import _alembic
from tests.llm_support import create_paper, execute


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=1)


def _document(db: TestDatabase, paper_id: uuid.UUID) -> str | None:
    rows = execute(
        db, "SELECT document::text FROM paper_search_document WHERE paper_id = %s", paper_id
    )
    return None if not rows else str(rows[0][0])


def _app(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description is not None else []


def _objects(db: TestDatabase) -> tuple[set[str], set[str], set[str]]:
    with connect_admin(db.name) as admin:
        tables = admin.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
        triggers = admin.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal").fetchall()
        columns = admin.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'api_call'"
        ).fetchall()
    return (
        {str(row[0]) for row in tables},
        {str(row[0]) for row in triggers},
        {str(row[0]) for row in columns},
    )


def test_downgrade_drops_everything_and_upgrade_backfills_existing_papers(
    db: TestDatabase, database: Database
) -> None:
    with_abstract = create_paper(database, title="Trust in ledgers", abstract="Escrow contracts.")
    title_only = create_paper(
        database, doi="10.1/def", openalex_id="W2", title="Platform signals", abstract=None
    )

    down = _alembic(db, "downgrade", "0022")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0022"}
    tables, triggers, columns = _objects(db)
    assert "paper_search_document" not in tables
    assert not {name for name in triggers if "paper_search_document" in name}
    assert "outcome" not in columns
    assert execute(db, "SELECT count(*) FROM paper") == [(2,)]

    up = _alembic(db, "upgrade", "0023")

    assert up.returncode == 0, up.stdout + up.stderr
    tables, triggers, columns = _objects(db)
    assert "paper_search_document" in tables
    assert {
        "tg_paper_search_document_from_paper",
        "tg_paper_search_document_from_abstract",
    } <= triggers
    assert "outcome" in columns
    assert _document(db, with_abstract) == "'contract':5B 'escrow':4B 'ledger':3A 'trust':1A"
    assert _document(db, title_only) == "'platform':1A 'signal':2A"


def test_triggers_keep_the_document_in_step_with_title_and_current_abstract(
    db: TestDatabase, database: Database
) -> None:
    paper = create_paper(database, title="Trust in ledgers", abstract="Escrow contracts.")
    assert _document(db, paper) == "'contract':5B 'escrow':4B 'ledger':3A 'trust':1A"

    execute(db, "UPDATE paper SET title = 'Platform signals' WHERE id = %s", paper)
    assert _document(db, paper) == "'contract':4B 'escrow':3B 'platform':1A 'signal':2A"

    execute(db, "UPDATE paper_abstract SET content = 'Auction design.' WHERE paper_id = %s", paper)
    assert _document(db, paper) == "'auction':3B 'design':4B 'platform':1A 'signal':2A"

    # 換成另一個來源的摘要：舊的不是目前的、新的是目前的（兩句，各自觸發一次）。
    execute(
        db,
        "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
        " VALUES (%s, 'semantic_scholar', 'Matching markets.', false)",
        paper,
    )
    assert _document(db, paper) == "'auction':3B 'design':4B 'platform':1A 'signal':2A"
    execute(db, "UPDATE paper_abstract SET is_current = false WHERE paper_id = %s", paper)
    execute(
        db,
        "UPDATE paper_abstract SET is_current = true WHERE paper_id = %s"
        " AND source = 'semantic_scholar'",
        paper,
    )
    assert _document(db, paper) == "'market':4B 'match':3B 'platform':1A 'signal':2A"

    execute(db, "DELETE FROM paper_abstract WHERE paper_id = %s", paper)
    assert _document(db, paper) == "'platform':1A 'signal':2A"

    # 改標題以外的欄位不重算（`updated_at` 不變）。
    before = execute(db, "SELECT updated_at FROM paper_search_document WHERE paper_id = %s", paper)
    execute(db, "UPDATE paper SET excluded_reason = 'erratum' WHERE id = %s", paper)
    after = execute(db, "SELECT updated_at FROM paper_search_document WHERE paper_id = %s", paper)
    assert before == after


def test_app_role_only_reads_the_document_table(db: TestDatabase, database: Database) -> None:
    paper = create_paper(database)

    assert len(_app(db, "SELECT paper_id FROM paper_search_document")) == 1
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(db, "UPDATE paper_search_document SET document = ''::tsvector")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(db, "DELETE FROM paper_search_document")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(
            db,
            "INSERT INTO paper_search_document (paper_id, document) VALUES (%s, ''::tsvector)",
            uuid.uuid4(),
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(db, "SELECT refresh_paper_search_document(%s)", paper)
    # 應用程式自己的寫入路徑（改標題）經觸發器照樣寫得進去。
    _app(db, "UPDATE paper SET title = 'Platform signals' WHERE id = %s", paper)
    assert "'platform':1A" in str(_document(db, paper))


@pytest.mark.parametrize("outcome", ["succeeded", "unreachable", "rejected", "quota_exhausted"])
def test_outcome_accepts_the_four_codes(db: TestDatabase, outcome: str) -> None:
    execute(
        db,
        "INSERT INTO api_call (source, operation, cost_usd, estimated_input_tokens, called_at,"
        " outcome) VALUES ('workers_ai', 'embed_query', 0, 0, now(), %s)",
        outcome,
    )


def test_outcome_rejects_other_values(db: TestDatabase) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="outcome"):
        execute(
            db,
            "INSERT INTO api_call (source, operation, cost_usd, estimated_input_tokens, called_at,"
            " outcome) VALUES ('workers_ai', 'embed_query', 0, 0, now(), 'timeout')",
        )
