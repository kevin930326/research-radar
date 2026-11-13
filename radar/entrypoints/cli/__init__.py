"""`radar` 指令：專案唯一的 CLI 入口（NFR-11）。typer 只能出現在這個套件。"""

from importlib.metadata import version as package_version
from typing import NoReturn

import structlog
import typer

from radar.config import ConfigError, load_migration_settings
from radar.domain.database_requirements import DatabaseRequirementsError
from radar.entrypoints.bootstrap import MigrationsNotFoundError, build_migration_runner
from radar.entrypoints.logging_config import configure_logging
from radar.services.migrations import migrate_database

# 例外時不印區域變數：裡面可能有連線字串。
app = typer.Typer(
    help="Research Radar 的指令列工具。",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
log = structlog.get_logger(__name__)


@app.callback()
def _root() -> None:
    """Research Radar 的指令列工具。"""


@app.command()
def version() -> None:
    """印出 radar 的版本。"""
    typer.echo(package_version("radar"))


@app.command()
def migrate() -> None:
    """用擁有者角色（RADAR_MIGRATION_DATABASE_URL）把資料庫遷移到最新版本。

    失敗時退出碼 1，原因印在 stderr；連線字串與密碼不會出現在輸出裡。
    """
    configure_logging()
    try:
        settings = load_migration_settings()
    except ConfigError as exc:
        log.error("設定錯誤，無法遷移", problems=exc.problems)
        _fail("設定錯誤，沒有遷移：", exc.problems)
    try:
        migrate_database(build_migration_runner(settings))
    except DatabaseRequirementsError as exc:
        log.error("資料庫不符合需求，遷移已整個退回", problems=exc.problems)
        _fail("資料庫不符合需求，遷移已整個退回：", exc.problems)
    except MigrationsNotFoundError:
        log.error("找不到遷移檔（工作目錄下的 migrations/）")
        _fail("找不到遷移檔：請在有 migrations/ 的目錄執行", [])
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("遷移失敗，已整個退回", error_type=type(exc).__name__)
        _fail(f"遷移失敗，已整個退回（{type(exc).__name__}）；細節見日誌", [])


def _fail(headline: str, problems: list[str]) -> NoReturn:
    typer.echo(headline, err=True)
    for problem in problems:
        typer.echo(f"- {problem}", err=True)
    raise typer.Exit(1)


def main() -> None:
    app()
