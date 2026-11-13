"""兩個資料庫角色（ADR-0003 決定第 3 點）：屬性，以及遷移後 radar_app 能做與不能做的事。"""

import psycopg
import pytest

from tests.database import APP_ROLE, OWNER_ROLE, TestDatabase, connect_admin, migrate


@pytest.mark.parametrize("role", [OWNER_ROLE, APP_ROLE])
def test_role_exists_and_is_not_privileged(role: str) -> None:
    with connect_admin() as admin:
        row = admin.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin"
            " FROM pg_roles WHERE rolname = %s",
            (role,),
        ).fetchone()

    assert row == (False, False, False, False, False, True)


def test_compose_database_is_owned_by_owner_role() -> None:
    with connect_admin() as admin:
        row = admin.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'radar'"
        ).fetchone()

    assert row == (OWNER_ROLE,)


def test_app_role_reads_but_cannot_change_alembic_version(
    migrated_database: TestDatabase,
) -> None:
    with migrated_database.connect(APP_ROLE) as app:
        assert app.execute("SELECT version_num FROM alembic_version").fetchall() == [("0001",)]
        for statement in (
            "UPDATE alembic_version SET version_num = 'x'",
            "INSERT INTO alembic_version VALUES ('x')",
            "DELETE FROM alembic_version",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.execute(statement)


def test_app_role_cannot_create_tables(migrated_database: TestDatabase) -> None:
    with migrated_database.connect(APP_ROLE) as app:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("CREATE TABLE app_made (id uuid PRIMARY KEY DEFAULT uuidv7())")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("CREATE SCHEMA app_schema")


def test_app_role_can_write_tables_owner_creates_later(fresh_database: TestDatabase) -> None:
    migrate(fresh_database)
    with fresh_database.connect(OWNER_ROLE) as owner:
        owner.execute("CREATE TABLE note (id uuid PRIMARY KEY DEFAULT uuidv7(), body text)")
        owner.execute("CREATE TABLE counter (id bigserial PRIMARY KEY, body text)")

    with fresh_database.connect(APP_ROLE) as app:
        for table in ("note", "counter"):
            app.execute(f"INSERT INTO {table} (body) VALUES ('a')")
            app.execute(f"UPDATE {table} SET body = 'b'")
            assert app.execute(f"SELECT body FROM {table}").fetchall() == [("b",)]
            app.execute(f"DELETE FROM {table}")
            assert app.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        # 預設權限不含改表結構。
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("DROP TABLE note")
