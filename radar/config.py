"""唯一讀環境變數的地方（AD-18）。只有入口與 `bootstrap.py` 可以 import 這個模組。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, SecretStr, ValidationError

from radar.domain.jobs import scheduled_jobs
from radar.domain.settings import MigrationSettings, Settings, heartbeat_url_field

type ProcessKind = Literal["api", "worker", "cli"]

# 連線數預算（ADR-0003 決定第 4 點、AD-17）：以託管 PostgreSQL 可用的 35 條連線為準，
# api 8、worker 6、每個 CLI 行程 2，其餘留給備份與人工連線。
# 可用 RADAR_API_DATABASE_POOL_SIZE、RADAR_WORKER_DATABASE_POOL_SIZE、
# RADAR_CLI_DATABASE_POOL_SIZE 覆寫。
DATABASE_POOL_BUDGET: Mapping[ProcessKind, int] = {"api": 8, "worker": 6, "cli": 2}


class ConfigError(Exception):
    """設定缺漏或格式不對。訊息只列環境變數名稱與原因，不含任何設定值。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("設定錯誤：" + "；".join(problems))


def env_name(field: str) -> str:
    """設定欄位對應的環境變數名稱（`migration_database_url` → `RADAR_MIGRATION_DATABASE_URL`）。"""
    if field == "database_url":
        return "DATABASE_URL"
    return f"RADAR_{field.upper()}"


def _load[T: BaseModel](model: type[T], environ: Mapping[str, str] | None) -> T:
    """從環境變數組出設定物件；空字串當作沒設定。錯誤訊息不含任何設定值。"""
    env = os.environ if environ is None else environ
    values: dict[str, str] = {}
    for field in model.model_fields:
        raw = env.get(env_name(field), "")
        if raw != "":
            values[field] = raw
    try:
        return model.model_validate(values)
    except ValidationError as exc:
        problems: list[str] = []
        for error in exc.errors(include_input=False, include_url=False, include_context=False):
            field = str(error["loc"][0]) if error["loc"] else "?"
            if error["type"] == "missing":
                problems.append(f"缺少環境變數 {env_name(field)}")
            else:
                problems.append(f"環境變數 {env_name(field)} 格式不對（{error['msg']}）")
        raise ConfigError(problems) from None


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """api、worker 與一般 CLI 指令的設定。

    Raises:
        ConfigError: 缺少必填的值或格式不對；訊息不含任何設定值。
    """
    return _load(Settings, environ)


def load_migration_settings(environ: Mapping[str, str] | None = None) -> MigrationSettings:
    """`radar migrate` 與 alembic 指令的設定（擁有者角色的連線字串）。

    Raises:
        ConfigError: 缺少 `RADAR_MIGRATION_DATABASE_URL` 或格式不對；訊息不含任何設定值。
    """
    return _load(MigrationSettings, environ)


def database_pool_size(settings: Settings, process: ProcessKind) -> int:
    """這種行程的連線池大小：環境變數有覆寫就用覆寫值，否則用連線數預算。"""
    override = {
        "api": settings.api_database_pool_size,
        "worker": settings.worker_database_pool_size,
        "cli": settings.cli_database_pool_size,
    }[process]
    return DATABASE_POOL_BUDGET[process] if override is None else override


# worker 的連線怎麼分（ADR-0019）：佇列的連線池最多 2 條、佇列監聽新工作的 1 條，其餘給應用程式的
# 連線池（sweep 讀狀態表等）。預算 6 條時是 2＋1＋3。
WORKER_QUEUE_POOL_MAX = 2
WORKER_LISTEN_CONNECTIONS = 1
WORKER_MIN_CONNECTIONS = 3


@dataclass(frozen=True)
class WorkerConnections:
    """worker 的連線數：佇列的連線池、監聽、應用程式的連線池，加總等於 worker 的預算。"""

    queue_pool: int
    listen: int
    app_pool: int

    @property
    def total(self) -> int:
        return self.queue_pool + self.listen + self.app_pool


def worker_connection_split(settings: Settings) -> WorkerConnections:
    """把 worker 的連線數預算（預設 6，可用 `RADAR_WORKER_DATABASE_POOL_SIZE` 覆寫）拆成三份。

    監聽固定 1 條；佇列的連線池最多 2 條，應用程式的連線池至少 1 條、拿剩下的。

    Raises:
        ConfigError: 預算少於 3 條（三份各至少要 1 條）。
    """
    budget = database_pool_size(settings, "worker")
    if budget < WORKER_MIN_CONNECTIONS:
        raise ConfigError(
            [
                f"環境變數 {env_name('worker_database_pool_size')} 至少要"
                f" {WORKER_MIN_CONNECTIONS}（佇列連線池、監聽、應用程式連線池各 1 條）"
            ]
        )
    rest = budget - WORKER_LISTEN_CONNECTIONS
    queue_pool = min(WORKER_QUEUE_POOL_MAX, rest // 2)
    return WorkerConnections(
        queue_pool=queue_pool, listen=WORKER_LISTEN_CONNECTIONS, app_pool=rest - queue_pool
    )


def heartbeat_urls(settings: Settings) -> dict[str, str]:
    """每個排程的心跳網址（排程名稱 → 網址）；沒設網址的排程不在裡面。"""
    urls: dict[str, str] = {}
    for job in scheduled_jobs():
        value = getattr(settings, heartbeat_url_field(job.check))
        if isinstance(value, SecretStr):
            urls[job.check] = value.get_secret_value()
    return urls
