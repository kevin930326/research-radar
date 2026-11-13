"""唯一讀環境變數的地方（AD-18）。只有入口與 `bootstrap.py` 可以 import 這個模組。"""

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ValidationError

from radar.domain.settings import MigrationSettings, Settings

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
