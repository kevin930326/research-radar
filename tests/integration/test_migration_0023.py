"""遷移 0023（全文檢索的資料 `paper_search_document`、`api_call.outcome`，Story 4.1、ADR-0005）
對真的 PostgreSQL：降版、升版、回填、觸發器、權限與 CHECK。

- 降到 0022：表、觸發器、函式、`outcome` 欄都不見；論文與摘要還在。
- 升回 0023：既有的論文全部回填（標題 A＋目前的摘要 B；沒有目前的摘要只有標題）。
- 觸發器：論文新增、改標題；摘要新增、改內容、換成另一個來源（`is_current` 從一筆換到另一筆）、刪除
  之後，那一篇的資料跟著重算；改標題以外的欄位不重算。
- 同一篇同時改標題與摘要（兩條連線、兩個交易）：後到的等先到的 commit，最後的資料兩邊的改動都有
  （審查 H5；等待用 `pg_stat_activity` 確認、輪詢有上限次數，不靠固定 sleep）。
- 權限：應用程式角色只能讀這張表，不能直接寫；三個函式應用程式角色都沒有 EXECUTE（審查 M1）；
  經觸發器寫得進去。
- `api_call.outcome` 只收五個值或 NULL；`embedding_quota_hold.operation` 既有的列補
  `embed_documents`。

資料全是測試自己編的。
"""

import threading
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


FUNCTIONS = (
    "refresh_paper_search_document",
    "paper_search_document_from_paper",
    "paper_search_document_from_abstract",
)


def _objects(db: TestDatabase) -> tuple[set[str], set[str], set[str]]:
    with connect_admin(db.name) as admin:
        tables = admin.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
        triggers = admin.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal").fetchall()
        columns = admin.execute(
            "SELECT table_name || '.' || column_name FROM information_schema.columns"
            " WHERE table_name IN ('api_call', 'embedding_quota_hold')"
        ).fetchall()
    return (
        {str(row[0]) for row in tables},
        {str(row[0]) for row in triggers},
        {str(row[0]) for row in columns},
    )


def _functions(db: TestDatabase) -> set[str]:
    rows = execute(
        db,
        "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace"
        " AND proname = ANY(%s)",
        list(FUNCTIONS),
    )
    return {str(row[0]) for row in rows}


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
    assert "api_call.outcome" not in columns
    assert "embedding_quota_hold.operation" not in columns
    assert _functions(db) == set()
    assert execute(db, "SELECT count(*) FROM paper") == [(2,)]
    # 0022 時留下的占用（背景向量化的）：升版後補成 embed_documents。
    execute(
        db,
        "INSERT INTO embedding_quota_hold (estimated_input_tokens, reserved_at, expires_at)"
        " VALUES (5, now(), now() + interval '1 minute')",
    )

    up = _alembic(db, "upgrade", "0023")

    assert up.returncode == 0, up.stdout + up.stderr
    tables, triggers, columns = _objects(db)
    assert "paper_search_document" in tables
    assert {
        "tg_paper_search_document_from_paper",
        "tg_paper_search_document_from_abstract",
    } <= triggers
    assert {"api_call.outcome", "embedding_quota_hold.operation"} <= columns
    assert _functions(db) == set(FUNCTIONS)
    assert execute(db, "SELECT operation FROM embedding_quota_hold") == [("embed_documents",)]
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
    # 三個 SECURITY DEFINER 函式都沒有開放給應用程式角色（審查 M1）。
    privileges = _app(
        db,
        "SELECT proname, has_function_privilege(current_user, oid, 'EXECUTE') FROM pg_proc"
        " WHERE pronamespace = 'public'::regnamespace AND proname = ANY(%s) ORDER BY proname",
        list(FUNCTIONS),
    )
    assert privileges == [(name, False) for name in sorted(FUNCTIONS)]
    # 應用程式自己的寫入路徑（改標題）經觸發器照樣寫得進去。
    _app(db, "UPDATE paper SET title = 'Platform signals' WHERE id = %s", paper)
    assert "'platform':1A" in str(_document(db, paper))


@pytest.mark.parametrize(
    "outcome", ["succeeded", "unreachable", "rejected", "quota_exhausted", "not_sent"]
)
def test_outcome_accepts_the_five_codes(db: TestDatabase, outcome: str) -> None:
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


# --- 同一篇同時改標題與摘要（審查 H5） ---------------------------------------------------------

WAIT_POLLS = 5_000  # 輪詢「後到的交易在等鎖」的上限次數（每次一句查詢，不 sleep）


def _wait_until_waiting_on_a_lock(db: TestDatabase, pid: int) -> None:
    with connect_admin(db.name) as admin:
        for _ in range(WAIT_POLLS):
            row = admin.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
            ).fetchone()
            if row is not None and row[0] == "Lock":
                return
    raise AssertionError("後到的交易沒有在等鎖")


TITLE = "UPDATE paper SET title = 'Platform signals' WHERE id = %s"
ABSTRACT = "UPDATE paper_abstract SET content = 'Auction design.' WHERE paper_id = %s"


@pytest.mark.parametrize(
    ("first", "second"),
    [(TITLE, ABSTRACT), (ABSTRACT, TITLE)],
    ids=["title-first", "abstract-first"],
)
def test_concurrent_title_and_abstract_updates_both_reach_the_document(
    db: TestDatabase, database: Database, first: str, second: str
) -> None:
    paper = create_paper(database, title="Trust in ledgers", abstract="Escrow contracts.")
    early = db.connect(APP_ROLE, autocommit=False)
    late = db.connect(APP_ROLE, autocommit=False)
    errors: list[BaseException] = []
    try:
        early.execute(first, (paper,))  # 先到的：改了、還沒 commit
        row = late.execute("SELECT pg_backend_pid()").fetchone()
        assert row is not None
        late_pid = int(row[0])

        def run_late() -> None:
            try:
                late.execute(second, (paper,))
                late.commit()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run_late)
        worker.start()
        _wait_until_waiting_on_a_lock(db, late_pid)
        early.commit()
        worker.join(20)
        assert not worker.is_alive()
    finally:
        early.close()
        late.close()

    assert errors == []
    assert _document(db, paper) == "'auction':3B 'design':4B 'platform':1A 'signal':2A"
