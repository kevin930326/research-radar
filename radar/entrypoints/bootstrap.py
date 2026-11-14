"""組裝：唯一建立 adapter 與資料庫連線的地方（AD-1）。"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from radar.adapters.heartbeat import HttpHeartbeat
from radar.adapters.jobs import JobApp, create_job_app, defer_job, retry_stalled_jobs
from radar.domain.jobs import Enqueue
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


@contextmanager
def build_job_enqueuer(settings: Settings, *, max_connections: int) -> Iterator[Enqueue]:
    """開佇列的連線池（最多 `max_connections` 條），交出排入工作的函式；離開區塊時關掉。

    Raises:
        Exception: `connect_timeout` 秒內連不上（psycopg_pool 的 PoolTimeout）。
    """
    job_app = create_job_app(
        settings.database_url.get_secret_value(),
        max_connections=max_connections,
        connect_timeout=settings.database_connect_timeout,
    )
    with job_app.opened() as app:
        yield partial(defer_job, app)


@dataclass(frozen=True)
class WorkerParts:
    """worker 需要的東西：佇列 App、應用程式的資料庫、排入工作、重試卡住的工作、送心跳。

    `enqueue` 與 `retry_stalled` 用的是佇列 App 目前的連線（worker 開著連線池時才能用）。
    """

    job_app: JobApp
    database: Database
    enqueue: Enqueue
    retry_stalled: Callable[[], int]
    send_heartbeat: Callable[[str], None]


def build_worker(
    settings: Settings,
    *,
    queue_pool_size: int,
    app_pool_size: int,
    heartbeat_urls: Mapping[str, str],
) -> WorkerParts:
    """組 worker 的零件，不連線（連線池由 worker 的監督迴圈開與關）。

    連線數：佇列的連線池 `queue_pool_size`＋佇列監聽 1 條＋應用程式的連線池 `app_pool_size`。
    """
    url = settings.database_url.get_secret_value()
    job_app = create_job_app(
        url, max_connections=queue_pool_size, connect_timeout=settings.database_connect_timeout
    )
    return WorkerParts(
        job_app=job_app,
        database=Database(
            url, connect_timeout=settings.database_connect_timeout, pool_size=app_pool_size
        ),
        enqueue=partial(defer_job, job_app.app),
        retry_stalled=partial(
            retry_stalled_jobs,
            job_app.app,
            seconds_since_heartbeat=settings.worker_stalled_seconds,
            max_retries=settings.job_max_retries,
        ),
        send_heartbeat=HttpHeartbeat(
            heartbeat_urls,
            timeout=settings.heartbeat_timeout,
            attempts=settings.heartbeat_attempts,
            wait_initial=settings.heartbeat_retry_wait_initial,
            wait_max=settings.heartbeat_retry_wait_max,
            retry_after_max=settings.heartbeat_retry_after_max,
        ),
    )
