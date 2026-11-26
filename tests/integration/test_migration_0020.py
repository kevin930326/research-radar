"""遷移 0020（個人文獻庫、刪除前的封存）對真的 PostgreSQL：降版、升版、權限、CHECK、UNIQUE。

- 降到 0019 後兩張表都不在，再升回來都在（空的）。
- 應用程式角色不能改（UPDATE）兩張表，可以新增、讀、刪。
- `library_item`：`owner_id` NOT NULL；`added_via` 只收三種值；（成員, 論文）唯一；外鍵到成員與
  論文。
- `deleted_record`：`source_table` 是小寫識別字、`content` 是 JSON 物件；兩個成員欄位可空。
資料全是測試自己編的（`*@example.com`、假名字、假標題）。
"""

import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from radar.repo.db import Database
from tests.database import APP_ROLE, ROOT, TestDatabase, connect_admin, migrate
from tests.llm_support import create_paper

BIN = Path(sys.executable).parent
TABLES = ("library_item", "deleted_record")
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")


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
    """用應用程式角色執行一句 SQL，回所有列。"""
    with db.connect(APP_ROLE) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description is not None else []


def _relations(db: TestDatabase) -> set[str]:
    with connect_admin(db.name) as admin:
        rows = admin.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    return {row[0] for row in rows}


def _member(db: TestDatabase, name: str = "測試甲") -> uuid.UUID:
    ((member_id,),) = _app(
        db, "INSERT INTO member (display_name, is_admin) VALUES (%s, false) RETURNING id", name
    )
    return uuid.UUID(str(member_id))


def _paper(db: TestDatabase, n: int = 1) -> uuid.UUID:
    database = Database(db.app_url, connect_timeout=5, pool_size=1)
    return create_paper(database, doi=f"10.1/m{n}", openalex_id=f"W{n}")


def _item(db: TestDatabase, owner_id: object, paper_id: object, added_via: str = "manual") -> None:
    _app(
        db,
        "INSERT INTO library_item (owner_id, paper_id, added_via) VALUES (%s, %s, %s)",
        owner_id,
        paper_id,
        added_via,
    )


def _record(db: TestDatabase, **changes: object) -> None:
    values: dict[str, object] = {
        "source_table": "library_item",
        "source_id": uuid.uuid4(),
        "content": '{"id": "x"}',
    } | changes
    columns = ", ".join(values)
    placeholders = ", ".join("%s" for _ in values)
    _app(db, f"INSERT INTO deleted_record ({columns}) VALUES ({placeholders})", *values.values())


# --- 降版、升版 ---------------------------------------------------------------------------------


def test_downgrade_to_0019_drops_tables_and_upgrade_restores_them(db: TestDatabase) -> None:
    owner_id = _member(db)
    _item(db, owner_id, _paper(db))
    _record(db, owner_id=owner_id)

    down = _alembic(db, "downgrade", "0019")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0019"}
    assert not set(TABLES) & _relations(db)
    assert db.has_table("member")

    migrate(db)

    assert db.revisions() == {"0022"}
    assert _relations(db) >= set(TABLES)
    for table in TABLES:
        assert _app(db, f"SELECT count(*) FROM {table}") == [(0,)]


# --- 權限 ---------------------------------------------------------------------------------------


def _granted(db: TestDatabase, relation: str) -> set[str]:
    with connect_admin(db.name) as admin:
        return {
            privilege
            for privilege in TABLE_PRIVILEGES
            if admin.execute(
                "SELECT has_table_privilege(%s, %s, %s)", (APP_ROLE, relation, privilege)
            ).fetchone()
            == (True,)
        }


def test_app_role_cannot_update_either_table(db: TestDatabase) -> None:
    assert _granted(db, "library_item") == {"SELECT", "INSERT", "DELETE"}
    assert _granted(db, "deleted_record") == {"SELECT", "INSERT", "DELETE"}
    owner_id = _member(db)
    _item(db, owner_id, _paper(db))
    _record(db, owner_id=owner_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(db, "UPDATE library_item SET added_via = 'upload'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(db, "UPDATE deleted_record SET source_table = 'other'")
    _app(db, "DELETE FROM library_item")
    _app(db, "DELETE FROM deleted_record")


# --- library_item ------------------------------------------------------------------------------


def test_library_item_owner_is_required(db: TestDatabase) -> None:
    with pytest.raises(psycopg.errors.NotNullViolation):
        _item(db, None, _paper(db))


@pytest.mark.parametrize("added_via", ["upload", "shared_list", "manual"])
def test_library_item_accepts_three_ways(db: TestDatabase, added_via: str) -> None:
    _item(db, _member(db), _paper(db), added_via)

    ((stored, added_at_set),) = _app(db, "SELECT added_via, added_at IS NOT NULL FROM library_item")
    assert (stored, added_at_set) == (added_via, True)


@pytest.mark.parametrize("added_via", ["", "Manual", "bookmark"])
def test_library_item_rejects_other_ways(db: TestDatabase, added_via: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _item(db, _member(db), _paper(db), added_via)


def test_one_item_per_member_and_paper(db: TestDatabase) -> None:
    first, second = _member(db), _member(db, "測試乙")
    paper_id = _paper(db)
    _item(db, first, paper_id)
    _item(db, second, paper_id)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _item(db, first, paper_id, "upload")


def test_library_item_foreign_keys(db: TestDatabase) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _item(db, uuid.uuid4(), _paper(db))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _item(db, _member(db), uuid.uuid4())


# --- deleted_record ----------------------------------------------------------------------------


def test_deleted_record_members_may_be_empty(db: TestDatabase) -> None:
    _record(db)

    assert _app(db, "SELECT owner_id, deleted_by_member_id FROM deleted_record") == [(None, None)]


@pytest.mark.parametrize(
    "changes",
    [
        {"source_table": ""},
        {"source_table": "Library_item"},
        {"source_table": "library item"},
        {"content": "[1, 2]"},
        {"content": '"text"'},
    ],
)
def test_deleted_record_checks(db: TestDatabase, changes: dict[str, object]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _record(db, **changes)


def test_deleted_record_foreign_keys(db: TestDatabase) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _record(db, owner_id=uuid.uuid4())
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _record(db, deleted_by_member_id=uuid.uuid4())
