"""遷移 0024（搜尋的評估題組 `eval_query`、`eval_query_relevant` 與 `eval_run`，Story 4.4）對真的
PostgreSQL：降版、升版、CHECK 約束、唯一約束、刪題目時正解跟著刪、應用程式角色的權限。

- 降到 0023：三張表都不見；快照還在。升回 0024：三張表回來、是空的。
- CHECK 擋下：不認得的題組、空的或太長的題目編號、空的查詢、空的備註、不認得的 `kind`、空的或
  不認得的 `query_sets`、空的或超出 0～1 的相似度下限、負的題數、結束早於開始。
- 同一組同一個編號只能一題；同一題同一篇正解只能一列；題目刪掉時正解跟著刪。
- 應用程式角色：題目兩張表可以新增、刪除；`eval_run` 只能新增與讀，不能改、不能刪。

資料全是測試自己編的。
"""

import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from tests.database import APP_ROLE, TestDatabase, connect_admin, migrate
from tests.integration.test_migration_0016 import _alembic
from tests.llm_support import execute

AS_OF = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
TABLES = {"eval_query", "eval_query_relevant", "eval_run"}
RUN_COLUMNS = (
    "kind, query_sets, min_similarities, embedding_model_id, query_count, results,"
    " started_at, finished_at"
)
GOOD_RUN = "'search', '{search}', '{0.2}', 'fake-embedding', 1, '{}', now(), now()"


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _tables(db: TestDatabase) -> set[str]:
    with connect_admin(db.name) as admin:
        rows = admin.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def _snapshot(db: TestDatabase) -> uuid.UUID:
    [(snapshot_id,)] = execute(
        db,
        "WITH r AS (INSERT INTO weekly_run (status) VALUES ('published') RETURNING id)"
        " INSERT INTO snapshot (number, weekly_run_id, status, as_of)"
        " SELECT 1, r.id, 'published', %s FROM r RETURNING id",
        AS_OF,
    )
    return uuid.UUID(str(snapshot_id))


def _question(db: TestDatabase, key: str = "q1") -> uuid.UUID:
    [(query_id,)] = execute(
        db,
        "INSERT INTO eval_query (query_set, query_key, query_text) VALUES ('search', %s, '平台')"
        " RETURNING id",
        key,
    )
    return uuid.UUID(str(query_id))


def test_downgrade_to_0023_drops_the_tables_and_upgrade_restores_them(db: TestDatabase) -> None:
    snapshot_id = _snapshot(db)
    query_id = _question(db)
    execute(
        db,
        "INSERT INTO eval_query_relevant (eval_query_id, relevant_doi) VALUES (%s, '10.9999/f.1')",
        query_id,
    )
    execute(
        db,
        f"INSERT INTO eval_run (snapshot_id, {RUN_COLUMNS}) VALUES (%s, {GOOD_RUN})",
        snapshot_id,
    )

    down = _alembic(db, "downgrade", "0023")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0023"}
    assert not TABLES & _tables(db)
    assert execute(db, "SELECT id FROM snapshot") == [(snapshot_id,)]

    up = _alembic(db, "upgrade", "0024")

    assert up.returncode == 0, up.stdout + up.stderr
    assert db.revisions() == {"0024"}
    assert _tables(db) >= TABLES
    for table in sorted(TABLES):
        assert execute(db, f"SELECT count(*) FROM {table}") == [(0,)]


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        (
            "INSERT INTO eval_query (query_set, query_key, query_text) VALUES ('other', 'q', 'x')",
            "ck_eval_query_query_set",
        ),
        (
            "INSERT INTO eval_query (query_set, query_key, query_text) VALUES ('search', '', 'x')",
            "ck_eval_query_query_key_length",
        ),
        (
            "INSERT INTO eval_query (query_set, query_key, query_text)"
            " VALUES ('search', repeat('q', 65), 'x')",
            "ck_eval_query_query_key_length",
        ),
        (
            "INSERT INTO eval_query (query_set, query_key, query_text) VALUES ('search', 'q', '')",
            "ck_eval_query_query_text_length",
        ),
        (
            f"INSERT INTO eval_run ({RUN_COLUMNS})"
            " VALUES ('classify', '{search}', '{0.2}', 'm', 1, '{}', now(), now())",
            "ck_eval_run_kind",
        ),
        (
            f"INSERT INTO eval_run ({RUN_COLUMNS})"
            " VALUES ('search', '{}', '{0.2}', 'm', 1, '{}', now(), now())",
            "ck_eval_run_query_sets",
        ),
        (
            f"INSERT INTO eval_run ({RUN_COLUMNS})"
            " VALUES ('search', '{search,other}', '{0.2}', 'm', 1, '{}', now(), now())",
            "ck_eval_run_query_sets",
        ),
        (
            f"INSERT INTO eval_run ({RUN_COLUMNS})"
            " VALUES ('search', '{search}', '{}', 'm', 1, '{}', now(), now())",
            "ck_eval_run_min_similarities",
        ),
        (
            f"INSERT INTO eval_run ({RUN_COLUMNS})"
            " VALUES ('search', '{search}', '{0.2,1}', 'm', 1, '{}', now(), now())",
            "ck_eval_run_min_similarities",
        ),
        (
            f"INSERT INTO eval_run ({RUN_COLUMNS})"
            " VALUES ('search', '{search}', '{-0.1}', 'm', 1, '{}', now(), now())",
            "ck_eval_run_min_similarities",
        ),
        (
            # 有合法值也有 NULL 元素（`ALL` 是未知，只靠範圍條件會放行；審查 M7）。
            f"INSERT INTO eval_run ({RUN_COLUMNS})"
            " VALUES ('search', '{search}', '{0.2,NULL}', 'm', 1, '{}', now(), now())",
            "ck_eval_run_min_similarities",
        ),
        (
            f"INSERT INTO eval_run ({RUN_COLUMNS})"
            " VALUES ('search', '{search}', '{NULL}', 'm', 1, '{}', now(), now())",
            "ck_eval_run_min_similarities",
        ),
        (
            f"INSERT INTO eval_run ({RUN_COLUMNS})"
            " VALUES ('search', '{search}', '{0.2}', 'm', -1, '{}', now(), now())",
            "ck_eval_run_query_count_not_negative",
        ),
        (
            f"INSERT INTO eval_run ({RUN_COLUMNS})"
            " VALUES ('search', '{search}', '{0.2}', 'm', 1, '{}', now(), now() - interval '1 s')",
            "ck_eval_run_finished_after_started",
        ),
    ],
    ids=[
        "unknown-set",
        "empty-key",
        "long-key",
        "empty-query",
        "unknown-kind",
        "no-sets",
        "unknown-set-in-run",
        "no-floors",
        "floor-one",
        "floor-negative",
        "floor-null-with-valid",
        "floor-only-null",
        "negative-count",
        "finished-before-started",
    ],
)
def test_checks_reject_bad_rows(db: TestDatabase, sql: str, constraint: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        execute(db, sql)

    assert caught.value.diag.constraint_name == constraint


def test_empty_note_is_rejected_and_missing_note_is_fine(db: TestDatabase) -> None:
    query_id = _question(db)
    execute(
        db,
        "INSERT INTO eval_query_relevant (eval_query_id, relevant_doi) VALUES (%s, '10.9999/a')",
        query_id,
    )

    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        execute(
            db,
            "INSERT INTO eval_query_relevant (eval_query_id, relevant_doi, note)"
            " VALUES (%s, '10.9999/b', '')",
            query_id,
        )

    assert caught.value.diag.constraint_name == "ck_eval_query_relevant_note_length"


def test_unique_keys_and_cascade(db: TestDatabase) -> None:
    query_id = _question(db)
    execute(
        db,
        "INSERT INTO eval_query_relevant (eval_query_id, relevant_doi) VALUES (%s, '10.9999/a')",
        query_id,
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        _question(db)
    with pytest.raises(psycopg.errors.UniqueViolation):
        execute(
            db,
            "INSERT INTO eval_query_relevant (eval_query_id, relevant_doi)"
            " VALUES (%s, '10.9999/a')",
            query_id,
        )
    # 另一組可以用同一個編號。
    execute(
        db,
        "INSERT INTO eval_query (query_set, query_key, query_text)"
        " VALUES ('similar', 'q1', '10.9999/z')",
    )

    execute(db, "DELETE FROM eval_query WHERE id = %s", query_id)

    assert execute(db, "SELECT count(*) FROM eval_query_relevant") == [(0,)]


def test_app_role_writes_questions_but_only_appends_runs(db: TestDatabase) -> None:
    with db.connect(APP_ROLE) as connection:
        connection.execute(
            "INSERT INTO eval_query (query_set, query_key, query_text) VALUES ('search', 'q', 'x')"
        )
        connection.execute("DELETE FROM eval_query")
        connection.execute(f"INSERT INTO eval_run ({RUN_COLUMNS}) VALUES ({GOOD_RUN})")
        assert connection.execute("SELECT count(*) FROM eval_run").fetchall() == [(1,)]
        connection.commit()

    for sql in ("UPDATE eval_run SET query_count = 2", "DELETE FROM eval_run"):
        with (
            db.connect(APP_ROLE) as connection,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            connection.execute(sql)
