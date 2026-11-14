"""Alembic 的執行環境：`radar migrate` 與 alembic 指令共用（AD-18）。

- `radar migrate`：`radar.repo.migrations.upgrade_to_head` 把開好交易的連線放在
  `config.attributes["connection"]`，這裡直接用它；交易由它負責提交或退回。
- alembic 指令（例如 CI 的 `alembic check`）：從 `radar.config` 讀 `RADAR_MIGRATION_DATABASE_URL`
  （擁有者角色）自己連線；不另外讀環境變數。
- 不支援離線模式（`--sql`）：第一個遷移要連到資料庫檢查功能。
"""

from alembic import context
from sqlalchemy import Connection

# 把各表的定義登記到 Base.metadata（alembic check 比對用）。
import radar.repo.api_calls  # api_call
import radar.repo.ingest_staging  # ingest_staging
import radar.repo.papers  # paper、external_id、paper_abstract、paper_keyword
import radar.repo.raw_fetches  # noqa: F401  # raw_fetch
from radar.repo.migrations import include_name
from radar.repo.models import Base

config = context.config
target_metadata = Base.metadata


def run_with_connection(connection: Connection) -> None:
    """全部遷移在同一個交易裡：任何一步失敗就整個退回，連 alembic_version 都不留。

    `compare_server_default=True`：`alembic check` 也比對欄位預設值（Alembic 預設不比）。
    `include_name`：佇列（Procrastinate）的表不比對，它們不在模型裡（ADR-0019）。
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_from_alembic_command() -> None:
    from radar.config import ConfigError, load_migration_settings
    from radar.entrypoints.logging_config import configure_logging
    from radar.repo.migrations import create_migration_engine

    configure_logging()
    try:
        settings = load_migration_settings()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    engine = create_migration_engine(
        settings.migration_database_url.get_secret_value(),
        connect_timeout=settings.database_connect_timeout,
    )
    try:
        with engine.connect() as connection:
            run_with_connection(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise SystemExit("不支援離線模式（--sql）：遷移要連到資料庫執行")

shared_connection = config.attributes.get("connection")
if shared_connection is None:
    run_from_alembic_command()
else:
    run_with_connection(shared_connection)
