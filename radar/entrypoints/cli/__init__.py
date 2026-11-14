"""`radar` 指令：專案唯一的 CLI 入口（NFR-11）。typer 只能出現在這個套件。"""

import uuid
from importlib.metadata import version as package_version
from typing import Annotated, NoReturn

import structlog
import typer

from radar.config import (
    ConfigError,
    database_pool_size,
    load_domain_config,
    load_import_baseline,
    load_migration_settings,
    load_settings,
)
from radar.domain.coverage import render_coverage_report
from radar.domain.cube import UnknownJournalError
from radar.domain.database_requirements import DatabaseRequirementsError
from radar.domain.import_report import render_import_report
from radar.domain.jobs import UnknownJobError, find_scheduled_job
from radar.domain.viewer import SystemViewer
from radar.entrypoints.bootstrap import (
    MigrationsNotFoundError,
    build_database,
    build_job_enqueuer,
    build_migration_runner,
    build_object_store,
)
from radar.entrypoints.logging_config import configure_logging
from radar.services.coverage import get_coverage
from radar.services.import_report import get_import_report
from radar.services.jobs import enqueue_scheduled_job
from radar.services.migrations import migrate_database
from radar.services.reparse import (
    AdvisoryLockLostError,
    ReparseBusyError,
    WeeklyUpdateInProgressError,
    reparse_papers,
)

# 例外時不印區域變數：裡面可能有連線字串。
app = typer.Typer(
    help="Research Radar 的指令列工具。",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
log = structlog.get_logger(__name__)

# 資源指令：`radar weekly-run <動詞>`（Conventions「CLI」：`radar <名詞> <動詞>`）。
weekly_run_app = typer.Typer(help="每週更新（weekly_run）。", no_args_is_help=True)
app.add_typer(weekly_run_app, name="weekly-run")


@app.callback()
def _root() -> None:
    """Research Radar 的指令列工具。"""
    # 每個指令執行前先驗證領域設定（config/*.yaml，AD-18）；錯了就不執行、退出碼 1。
    try:
        load_domain_config()
    except ConfigError as exc:
        _fail("領域設定錯誤，沒有執行：", exc.problems)


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


@app.command()
def enqueue(
    job: str = typer.Argument(help="排程名稱，例如 sweep"),
) -> None:
    """把一個排程工作排進它的佇列（cron 呼叫；排程表在 deploy/crontab）。

    已經在排隊時不會多排一筆，退出碼一樣是 0。不認得的工作退出碼 2；
    設定錯誤或連不上資料庫退出碼 1，只印錯誤類別，不印連線字串。
    """
    configure_logging()
    try:
        find_scheduled_job(job)
    except UnknownJobError as exc:
        typer.echo(f"不認得的工作：{job[:64]!r}", err=True)
        typer.echo(f"可用的工作：{'、'.join(exc.available)}", err=True)
        raise typer.Exit(2) from None
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("設定錯誤，無法排入工作", problems=exc.problems)
        _fail("設定錯誤，沒有排入：", exc.problems)
    try:
        with build_job_enqueuer(
            settings, max_connections=database_pool_size(settings, "cli")
        ) as enqueue_job:
            queued = enqueue_scheduled_job(job, enqueue_job)
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("排入工作失敗", schedule=job, error_type=type(exc).__name__)
        _fail(f"排入失敗（{type(exc).__name__}）；細節見日誌", [])
    typer.echo(f"已排入 {job}" if queued else f"{job} 已經在排隊，沒有再排一筆")


@app.command()
def reparse() -> None:
    """只用已存的原始資料重新解析已經匯入的論文（不連 OpenAlex 或任何外部來源）。

    排除規則改了、或解析多了欄位時跑這個。解析結果先放暫存表、最後一個交易套用；
    不新增論文、不建立快照。每週更新正在抓取、或另一個 reparse 正在跑時拒絕執行、不寫任何東西，
    退出碼 1。
    持有寫入鎖的連線在兩個交易之間斷了（例如資料庫重啟）時中止、退出碼 1，印出原因與「直接重跑即可」
    （已經 commit 的不受影響）；斷在交易進行中時印一般的失敗訊息，同樣不會 commit。
    設定錯誤或執行失敗退出碼 1，只印錯誤類別，不印連線字串。
    """
    configure_logging()
    try:
        settings = load_settings()
        domain_config = load_domain_config()
    except ConfigError as exc:
        log.error("設定錯誤，無法重新解析", problems=exc.problems)
        _fail("設定錯誤，沒有重新解析：", exc.problems)
    try:
        # 建立物件儲存的 client 也放在這裡：botocore 初始化失敗的訊息會帶整個端點網址。
        store = build_object_store(settings)
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        result = reparse_papers(
            SystemViewer(),
            database,
            store,
            scope=domain_config.journals.scope_settings(),
            exclusion_rules=domain_config.exclusion_rules,
        )
    except WeeklyUpdateInProgressError as exc:
        log.error("每週更新正在進行，沒有重新解析", weekly_run_id=str(exc.weekly_run_id))
        _fail(str(exc), [])
    except ReparseBusyError as exc:
        log.error("另一個 reparse 或每週更新正在寫入，沒有重新解析")
        _fail(str(exc), [])
    except AdvisoryLockLostError as exc:
        # 訊息與原因只帶錯誤類別，不帶連線字串（radar/repo/db.py）。
        log.error("寫入鎖不見了，重新解析中止", reason=exc.reason)
        _fail(f"{exc}；直接重跑 radar reparse 即可", [])
    except ConfigError as exc:
        # build_object_store 缺設定時丟的；訊息只列環境變數名稱。
        log.error("設定錯誤，無法重新解析", problems=exc.problems)
        _fail("設定錯誤，沒有重新解析：", exc.problems)
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("重新解析失敗", error_type=type(exc).__name__)
        _fail(f"重新解析失敗（{type(exc).__name__}）；細節見日誌", [])
    typer.echo("重新解析完成")
    typer.echo(f"讀到的論文（外部鍵）：{result.raw_keys}")
    typer.echo(f"不在資料庫、略過：{result.not_in_database}")
    typer.echo(f"物件不見：{result.missing_object}")
    typer.echo(f"內容指紋對不上：{result.hash_mismatch}")
    typer.echo(f"解析失敗：{result.parse_error}")
    typer.echo(f"ID 對不上：{result.id_mismatch}")
    typer.echo(f"放進暫存表：{result.staged}")
    typer.echo(f"更新：{result.updated}")
    typer.echo(f"資料較舊、沒寫：{result.stale}")
    typer.echo(f"識別碼衝突：{result.conflicts}")


@weekly_run_app.command("report")
def weekly_run_report(
    weekly_run_id: Annotated[
        uuid.UUID | None, typer.Option("--id", help="每週更新的 id；不給就是最新的一筆")
    ] = None,
) -> None:
    """印出一次每週更新的匯入報表（只讀，不改任何資料）。

    每本期刊每年的過濾前篇數、依期刊的排除篇數、和 OQ-02 基準（config/import_baseline.yaml）
    的差異。沒有任何每週更新或找不到這個 id：退出碼 1；--id 不是 UUID：退出碼 2；
    設定錯誤或讀不到資料庫：退出碼 1，只印錯誤類別，不印連線字串。
    """
    configure_logging()
    try:
        settings = load_settings()
        domain_config = load_domain_config()
        baseline = load_import_baseline()
    except ConfigError as exc:
        log.error("設定錯誤，無法產生匯入報表", problems=exc.problems)
        _fail("設定錯誤，沒有產生報表：", exc.problems)
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        report = get_import_report(
            SystemViewer(),
            database,
            weekly_run_id,
            journals=domain_config.journals,
            baseline=baseline,
        )
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("讀取匯入報表失敗", error_type=type(exc).__name__)
        _fail(f"讀取匯入報表失敗（{type(exc).__name__}）；細節見日誌", [])
    if report is None:
        if weekly_run_id is None:
            _fail("還沒有任何每週更新", [])
        _fail(f"找不到這次每週更新：{weekly_run_id}", [])
    for line in render_import_report(report):
        typer.echo(line)


@app.command("coverage")
def coverage() -> None:
    """印出最新已發佈快照的覆蓋率（只讀，不改任何資料）。

    快照編號與截至日期（台北時間）、每本期刊每年的收錄篇數（total）與有摘要的篇數
    （with_abstract）、依目前的摘要來源分的篇數。只算 counted_paper 裡的論文（追蹤範圍內、
    沒有被排除、已算進已發佈的快照）。還沒有已發佈的快照、計數裡有設定沒有的期刊、設定錯誤或
    讀不到資料庫：退出碼 1；資料庫的錯誤只印類別，不印連線字串。
    """
    configure_logging()
    try:
        settings = load_settings()
        domain_config = load_domain_config()
    except ConfigError as exc:
        log.error("設定錯誤，無法讀取覆蓋率", problems=exc.problems)
        _fail("設定錯誤，沒有讀取覆蓋率：", exc.problems)
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        report = get_coverage(SystemViewer(), database, journals=domain_config.journals)
    except UnknownJournalError as exc:
        log.error("計數裡有設定沒有的期刊", issn_ls=list(exc.issn_ls))
        _fail(
            f"計數裡有設定沒有的期刊（ISSN-L：{'、'.join(exc.issn_ls)}）；"
            "config/journals.yaml 改過但論文的追蹤範圍還沒重算",
            ["重算追蹤範圍的 radar rescope 還沒做；在那之前先把那本期刊加回設定"],
        )
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("讀取覆蓋率失敗", error_type=type(exc).__name__)
        _fail(f"讀取覆蓋率失敗（{type(exc).__name__}）；細節見日誌", [])
    if report is None:
        _fail("還沒有已發佈的快照", [])
    for line in render_coverage_report(report):
        typer.echo(line)


def _fail(headline: str, problems: list[str]) -> NoReturn:
    typer.echo(headline, err=True)
    for problem in problems:
        typer.echo(f"- {problem}", err=True)
    raise typer.Exit(1)


def main() -> None:
    app()
