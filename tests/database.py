"""測試用的資料庫：每次建一個全新的 `radar_test_<亂數>`，用完 `DROP ... WITH (FORCE)`。

連線位置來自 `RADAR_TEST_ADMIN_DATABASE_URL`（要用超級使用者），預設是本機／CI 的 compose `db`。
角色 `radar_owner`、`radar_app` 是整個 PostgreSQL 共用的，由 `deploy/db-init/10-roles.sql` 建一次；
每個測試資料庫的擁有者是 `radar_owner`，擴充套件用同一份 `deploy/db-init/20-extensions.sql` 建。
連不上時測試失敗並說明要先起 `db`，不跳過。
"""

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import make_url

from radar.repo.migrations import MigrationScripts, upgrade_to_head

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL_ENV = "RADAR_TEST_ADMIN_DATABASE_URL"
DEFAULT_ADMIN_URL = "postgresql://postgres@127.0.0.1:5432/postgres"
EXTENSIONS_SQL = ROOT / "deploy" / "db-init" / "20-extensions.sql"
MIGRATIONS_DIR = ROOT / "migrations"
OWNER_ROLE = "radar_owner"
APP_ROLE = "radar_app"
START_DB_HINT = (
    "連不上測試用的 PostgreSQL（{env}）。請先起 compose 的 db："
    "docker compose --env-file /dev/null -p radar-1-3 -f compose.yaml up -d --wait db"
)


def admin_url() -> str:
    return os.environ.get(ADMIN_URL_ENV) or DEFAULT_ADMIN_URL


def _url_for(database: str, role: str | None = None) -> str:
    url = make_url(admin_url()).set(database=database)
    if role is not None:
        # 本機與 CI 用 trust 驗證，角色沒有密碼。
        url = url.set(username=role, password=None)
    return url.render_as_string(hide_password=False)


def connect_admin(database: str = "postgres") -> psycopg.Connection:
    """以超級使用者連線（autocommit）。連不上就讓測試失敗並提示先起 db。"""
    try:
        return psycopg.connect(_url_for(database), autocommit=True, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.fail(START_DB_HINT.format(env=ADMIN_URL_ENV), pytrace=False)


@dataclass(frozen=True)
class TestDatabase:
    """一個測試資料庫的名稱與三種身分的連線字串。"""

    __test__ = False  # 不是測試類別

    name: str

    @property
    def owner_url(self) -> str:
        return _url_for(self.name, OWNER_ROLE)

    @property
    def app_url(self) -> str:
        return _url_for(self.name, APP_ROLE)

    @property
    def admin_url(self) -> str:
        return _url_for(self.name)

    def connect(self, role: str, *, autocommit: bool = True) -> psycopg.Connection:
        url = {OWNER_ROLE: self.owner_url, APP_ROLE: self.app_url}[role]
        return psycopg.connect(url, autocommit=autocommit, connect_timeout=5)

    def has_table(self, table: str) -> bool:
        with psycopg.connect(self.admin_url, autocommit=True, connect_timeout=5) as connection:
            row = connection.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()
        assert row is not None
        return bool(row[0])

    def revisions(self) -> set[str]:
        with psycopg.connect(self.admin_url, autocommit=True, connect_timeout=5) as connection:
            rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        return {str(row[0]) for row in rows}


def create_database(*, with_extensions: bool) -> TestDatabase:
    """建一個擁有者是 `radar_owner` 的全新資料庫。

    `with_extensions=False` 時從 `template0` 建、沒有任何擴充套件（模擬管理者還沒建好的資料庫）。
    """
    database = TestDatabase(f"radar_test_{secrets.token_hex(6)}")
    with connect_admin() as admin:
        statement = sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(database.name), sql.Identifier(OWNER_ROLE)
        )
        if not with_extensions:
            statement = sql.SQL("{} TEMPLATE template0").format(statement)
        admin.execute(statement)
    if with_extensions:
        with connect_admin(database.name) as admin:
            admin.execute(EXTENSIONS_SQL.read_bytes())
    return database


def drop_database(database: TestDatabase) -> None:
    with connect_admin() as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database.name))
        )


def migrate(database: TestDatabase) -> frozenset[str]:
    """用擁有者角色遷移到最新版本（和 `radar migrate` 走同一個函式）。"""
    return upgrade_to_head(
        database.owner_url, MigrationScripts.load(MIGRATIONS_DIR), connect_timeout=5
    )
