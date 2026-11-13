"""組裝：唯一建立 adapter 與資料庫連線的地方（AD-1）。"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from radar.domain.schema import SchemaState
from radar.domain.settings import MigrationSettings, Settings
from radar.repo.db import Database
from radar.repo.migrations import MigrationScripts, read_schema_state, upgrade_to_head

# 入口不能直接 import radar.repo，這個例外從這裡轉出去給入口攔。
from radar.repo.migrations import MigrationsNotFoundError as MigrationsNotFoundError

# 遷移檔的位置：工作目錄下的 migrations/（映像的 WORKDIR 是 /app，開發時是 repo 根目錄）。
DEFAULT_MIGRATIONS_DIR = Path("migrations")


@dataclass(frozen=True)
class ReadinessProbes:
    """就緒檢查用的兩個探測函式；共用同一個連線池。"""

    ping: Callable[[], bool]
    schema_state: Callable[[], SchemaState]


def build_readiness_probes(
    settings: Settings, *, pool_size: int, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
) -> ReadinessProbes:
    """建立資料庫連線池，回傳 ping 與遷移狀態兩個探測函式。

    Raises:
        MigrationsNotFoundError: 找不到遷移檔（比對版本要用）。
    """
    scripts = MigrationScripts.load(migrations_dir)
    database = Database(
        settings.database_url.get_secret_value(),
        connect_timeout=settings.database_connect_timeout,
        pool_size=pool_size,
    )
    return ReadinessProbes(
        ping=database.ping, schema_state=partial(read_schema_state, database, scripts)
    )


def build_migration_runner(
    settings: MigrationSettings, *, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
) -> Callable[[], frozenset[str]]:
    """回傳「用擁有者角色遷移到最新版本」的函式。

    Raises:
        MigrationsNotFoundError: 找不到遷移檔。
    """
    scripts = MigrationScripts.load(migrations_dir)
    return partial(
        upgrade_to_head,
        settings.migration_database_url.get_secret_value(),
        scripts,
        connect_timeout=settings.database_connect_timeout,
    )
