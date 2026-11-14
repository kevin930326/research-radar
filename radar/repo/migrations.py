"""Alembic 遷移：找遷移檔、讀遷移狀態、讀資料庫功能、遷移到最新版本（AD-15、AD-18）。

`migrations/env.py` 與 `radar migrate` 共用：`radar migrate` 把開好交易的連線放進
`config.attributes["connection"]` 交給 env.py；alembic 指令則由 env.py 自己連線。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Self

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.pool import NullPool

from radar.domain.database_requirements import APP_ROLE, DatabaseFeatures
from radar.domain.schema import SchemaState, classify_schema
from radar.repo.db import Database

# 背景工作佇列（Procrastinate）自己的表都以這個前綴開頭。它們由遷移 0002 執行凍結的 SQL 建立，
# 不在 `Base.metadata` 裡，`alembic check` 不比對（AD-18、ADR-0019）。
QUEUE_TABLE_PREFIX = "procrastinate_"


def include_name(name: str | None, type_: str, _parent_names: object) -> bool:
    """`alembic check`（autogenerate）要不要比對這個物件：佇列的表不比對，其他都比對。

    表被排除時，它的欄位、索引、約束也跟著不比對。
    """
    return not (type_ == "table" and name is not None and name.startswith(QUEUE_TABLE_PREFIX))


class MigrationsNotFoundError(Exception):
    """找不到遷移檔的資料夾（裡面要有 `env.py`）。"""


@dataclass(frozen=True)
class MigrationScripts:
    """程式帶的遷移檔：最新版本（heads）與認得的全部版本。"""

    directory: Path
    heads: frozenset[str]
    known: frozenset[str]

    @classmethod
    def load(cls, directory: Path) -> Self:
        """讀 `directory` 裡的遷移檔。

        Raises:
            MigrationsNotFoundError: `directory/env.py` 不存在。
        """
        directory = directory.resolve()
        if not (directory / "env.py").is_file():
            raise MigrationsNotFoundError(f"找不到遷移檔：{directory} 裡沒有 env.py")
        script = ScriptDirectory.from_config(_alembic_config(directory))
        return cls(
            directory=directory,
            heads=frozenset(script.get_heads()),
            known=frozenset(revision.revision for revision in script.walk_revisions()),
        )

    def alembic_config(self) -> Config:
        return _alembic_config(self.directory)


def _alembic_config(directory: Path) -> Config:
    """不讀 alembic.ini（映像裡沒有）；只指定遷移檔的位置。"""
    config = Config()
    config.set_main_option("script_location", str(directory))
    return config


def create_migration_engine(url: str, *, connect_timeout: int) -> Engine:
    """遷移用的 Engine：不留連線（NullPool），只設連線逾時。

    不設 statement_timeout：之後的遷移（例如建索引）可能要跑比較久。
    """
    return create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": connect_timeout})


def read_schema_state(database: Database, scripts: MigrationScripts) -> SchemaState:
    """資料庫的遷移版本和程式比起來的狀態。只讀，不建 `alembic_version`。"""
    return classify_schema(database.schema_revisions(), heads=scripts.heads, known=scripts.known)


def read_database_features(connection: Connection) -> DatabaseFeatures:
    """讀第一個遷移要檢查的功能（`uuidv7()`、pgvector 版本、`pg_trgm`、應用程式角色、
    執行遷移的是不是資料庫擁有者）。
    """
    row = (
        connection.execute(
            text(
                "SELECT"
                " to_regprocedure('pg_catalog.uuidv7()') IS NOT NULL AS has_uuidv7,"
                " (SELECT extversion FROM pg_extension WHERE extname = 'vector')"
                " AS pgvector_version,"
                " EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') AS has_pg_trgm,"
                " EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :app_role) AS has_app_role,"
                " EXISTS (SELECT 1 FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba"
                " WHERE d.datname = current_database() AND r.rolname = current_user"
                " AND NOT r.rolsuper) AS runs_as_database_owner"
            ),
            {"app_role": APP_ROLE},
        )
        .mappings()
        .one()
    )
    return DatabaseFeatures(
        has_uuidv7=bool(row["has_uuidv7"]),
        pgvector_version=row["pgvector_version"],
        has_pg_trgm=bool(row["has_pg_trgm"]),
        has_app_role=bool(row["has_app_role"]),
        runs_as_database_owner=bool(row["runs_as_database_owner"]),
    )


def upgrade_to_head(url: str, scripts: MigrationScripts, *, connect_timeout: int) -> frozenset[str]:
    """在一個交易裡遷移到最新版本，回傳遷移後的版本。

    任何一步失敗（包含第一個遷移的功能檢查）整個交易退回，連 `alembic_version` 都不留。

    Raises:
        DatabaseRequirementsError: 資料庫缺少需要的功能。
        SQLAlchemyError: 連不上或執行失敗。
    """
    engine = create_migration_engine(url, connect_timeout=connect_timeout)
    try:
        with engine.begin() as connection:
            config = scripts.alembic_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            return frozenset(MigrationContext.configure(connection).get_current_heads())
    finally:
        engine.dispose()
