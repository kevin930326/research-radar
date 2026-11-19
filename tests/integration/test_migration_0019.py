"""遷移 0019（成員、允許名單、session）對真的 PostgreSQL：降版、升版、權限、CHECK。

- 降到 0018 後三張表都不在，再升回來都在（空的）。
- 應用程式角色不能刪 `member`（成員被其他紀錄引用）；`allowed_email`、`session` 可以刪。
- CHECK：顯示名稱非空白、最多 100 字元；`google_sub` 不能是空字串；email 小寫、像 email、最多
  254 字元；`token_sha256` 剛好 32 位元組；最後使用時間不早於建立時間。UNIQUE：email、
  `member_id`（一位成員一個 email）、`token_sha256`、`google_sub`。
資料全是測試自己編的（`*@example.com`、假名字）。
"""

import hashlib
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from tests.database import APP_ROLE, ROOT, TestDatabase, connect_admin, migrate

BIN = Path(sys.executable).parent
TABLES = ("member", "allowed_email", "session")
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


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


def _member(db: TestDatabase, **changes: object) -> uuid.UUID:
    values: dict[str, object] = {"display_name": "測試甲", "is_admin": False} | changes
    columns = ", ".join(values)
    placeholders = ", ".join("%s" for _ in values)
    (row,) = _app(
        db,
        f"INSERT INTO member ({columns}) VALUES ({placeholders}) RETURNING id",
        *values.values(),
    )
    return uuid.UUID(str(row[0]))


def _allowed(db: TestDatabase, member_id: uuid.UUID, email: str) -> None:
    _app(db, "INSERT INTO allowed_email (email, member_id) VALUES (%s, %s)", email, member_id)


def _session(db: TestDatabase, member_id: uuid.UUID, **changes: object) -> None:
    values: dict[str, object] = {
        "member_id": member_id,
        "token_sha256": hashlib.sha256(uuid.uuid4().bytes).digest(),
        "created_at": NOW,
        "last_seen_at": NOW,
    } | changes
    columns = ", ".join(values)
    placeholders = ", ".join("%s" for _ in values)
    _app(db, f"INSERT INTO session ({columns}) VALUES ({placeholders})", *values.values())


# --- 降版、升版 ---------------------------------------------------------------------------------


def test_downgrade_to_0018_drops_tables_and_upgrade_restores_them(db: TestDatabase) -> None:
    member_id = _member(db)
    _allowed(db, member_id, "member.one@example.com")
    _session(db, member_id)

    down = _alembic(db, "downgrade", "0018")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0018"}
    assert not set(TABLES) & _relations(db)
    assert db.has_table("judgment_event")

    migrate(db)

    assert db.revisions() == {"0021"}
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


def test_app_role_cannot_delete_members(db: TestDatabase) -> None:
    assert _granted(db, "member") == {"SELECT", "INSERT", "UPDATE"}
    assert _granted(db, "allowed_email") == set(TABLE_PRIVILEGES)
    assert _granted(db, "session") == set(TABLE_PRIVILEGES)
    member_id = _member(db)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(db, "DELETE FROM member WHERE id = %s", member_id)


# --- CHECK 與 UNIQUE ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"display_name": ""},
        {"display_name": "   "},
        {"display_name": "x" * 101},
        {"google_sub": ""},
    ],
)
def test_member_checks(db: TestDatabase, changes: dict[str, object]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _member(db, **changes)


def test_member_requires_is_admin(db: TestDatabase) -> None:
    with pytest.raises(psycopg.errors.NotNullViolation):
        _app(db, "INSERT INTO member (display_name) VALUES ('測試甲')")


@pytest.mark.parametrize(
    "email",
    [
        "Member.One@example.com",
        "member.one",
        "member one@example.com",
        "member.one@example",
        "@example.com",
        "a@" + "b" * 250 + ".com",
    ],
)
def test_allowed_email_checks(db: TestDatabase, email: str) -> None:
    member_id = _member(db)

    with pytest.raises(psycopg.errors.CheckViolation):
        _allowed(db, member_id, email)


def test_one_email_per_member_and_one_member_per_email(db: TestDatabase) -> None:
    first, second = _member(db), _member(db, display_name="測試乙")
    _allowed(db, first, "member.one@example.com")

    with pytest.raises(psycopg.errors.UniqueViolation):
        _allowed(db, second, "member.one@example.com")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _allowed(db, first, "member.two@example.com")
    _member(db, google_sub="sub-1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _member(db, google_sub="sub-1")


@pytest.mark.parametrize(
    "changes",
    [
        {"token_sha256": b"\x00" * 31},
        {"token_sha256": b"\x00" * 33},
        {"last_seen_at": NOW - timedelta(seconds=1)},
    ],
)
def test_session_checks(db: TestDatabase, changes: dict[str, object]) -> None:
    member_id = _member(db)

    with pytest.raises(psycopg.errors.CheckViolation):
        _session(db, member_id, **changes)


def test_session_token_hash_is_unique_and_member_must_exist(db: TestDatabase) -> None:
    member_id = _member(db)
    digest = hashlib.sha256(b"same").digest()
    _session(db, member_id, token_sha256=digest)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _session(db, member_id, token_sha256=digest)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _session(db, uuid.uuid4())
