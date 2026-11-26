"""`radar summary-reports`：列出成員回報的中文重點錯誤、標「已確認無誤」（Story 3.12、FR-14 附則、
ADR-0015、ADR-0064）。

- `list`：以維運身分（`SystemViewer`）只讀。每筆還沒處理的回報印回報 ID、論文 ID 與標題、哪一欄、
  回報的成員姓名、時間（台北時間）、說明，以及被回報的那一筆中文重點是不是現在有效的（重新產生或
  摘要換了之後論文頁已經不顯示標記）。不印 email 與成員 ID。說明是成員寫的字：印之前照終端機的規則
  清掉控制字元（`domain/admin_runs.terminal_text`）。
- `confirm-ok`：以 `--admin` 這位管理者（允許名單上、`is_admin`）的名義寫一筆
  `summary_report_resolution`＝`confirmed_ok`＋稽核（同一個交易）。論文頁那一欄的標記要等這一欄
  最後一筆沒處理的回報結案時才不顯示（同一欄有兩位成員回報、只處理一筆，標記還在）。
- 「重新產生」還沒接上：閘門（AD-8）現在不會重跑已經有 `active` 的同一份輸入，
  要 Kevin 決定怎麼開這條
  路（ADR-0064）。

誰能跑：只限能登入主機、拿得到資料庫設定的維運者（同 `radar classification disagreements`）。
"""

import uuid
from typing import NoReturn

import structlog
import typer

from radar.config import ConfigError, database_pool_size, load_settings
from radar.domain.admin_runs import terminal_text
from radar.domain.snapshot import DATA_TIMEZONE
from radar.domain.summary_reports import OpenSummaryReport
from radar.domain.viewer import SystemViewer
from radar.entrypoints.bootstrap import Database, build_database
from radar.entrypoints.logging_config import configure_logging
from radar.services.judgments import (
    AdminRequiredError,
    SummaryReportNotOpenError,
    confirm_summary_report,
    list_summary_reports,
)

log = structlog.get_logger(__name__)

summary_reports_app = typer.Typer(
    help="成員回報的中文重點錯誤（summary_report）。", no_args_is_help=True
)

# 欄位的中文名稱（和論文頁一樣）。
_FIELD_NAMES = {
    "research_question": "研究問題",
    "method": "方法",
    "theory": "理論",
    "findings": "發現",
}


def format_report(item: OpenSummaryReport) -> list[str]:
    """一筆回報印成幾行：回報 ID、論文、欄位與回報的人、說明。"""
    report = item.report
    when = report.reported_at.astimezone(DATA_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    current = "" if item.is_current else "  （這一筆中文重點已經不是目前的，論文頁不顯示標記）"
    return [
        f"回報 {report.report_id}",
        f"  論文 {item.paper_id}  {terminal_text(item.title)}",
        f"  {_FIELD_NAMES[report.field.value]}  {terminal_text(report.member_name)}"
        f"（{when}）{current}",
        f"  說明 {terminal_text(report.note)}",
    ]


def _database() -> Database:
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("設定錯誤", problems=exc.problems)
        _fail("設定錯誤，沒有連資料庫：", exc.problems)
    return build_database(settings, pool_size=database_pool_size(settings, "cli"))


@summary_reports_app.command("list")
def list_reports() -> None:
    """列出還沒處理的回報（舊的在前）。沒有：印一行說明，退出碼 0。設定錯誤或資料庫錯誤：
    退出碼 1。"""
    configure_logging()
    database = _database()
    try:
        reports = list_summary_reports(SystemViewer(), database)
    except Exception as exc:
        # 例外訊息可能含主機或帳號，只記類別名稱。
        log.error("列出回報失敗", error_type=type(exc).__name__)
        _fail(f"列出回報失敗（{type(exc).__name__}）；細節見日誌")
    if not reports:
        typer.echo("沒有還沒處理的中文重點回報")
        return
    for index, item in enumerate(reports):
        if index > 0:
            typer.echo("")
        for line in format_report(item):
            typer.echo(line)
    typer.echo("")
    typer.echo(f"共 {len(reports)} 筆（時間是台北時間）")


@summary_reports_app.command("confirm-ok")
def confirm_ok(
    report_id: str = typer.Argument(..., help="回報 ID（`list` 印的）"),
    admin: str = typer.Option(..., "--admin", help="做這個處理的管理者的 email（允許名單上）"),
) -> None:
    """把一筆回報標成「已確認無誤」。論文頁那一欄的「有成員回報此欄可能有誤」，要等這一欄最後一筆
    沒處理的回報結案時才不顯示。

    回報 ID 不對、不存在或已經處理過：退出碼 1。`--admin` 不是管理者：退出碼 1。
    """
    configure_logging()
    try:
        parsed = uuid.UUID(report_id)
    except ValueError:
        _fail("回報 ID 的格式不對")
    database = _database()
    try:
        report = confirm_summary_report(SystemViewer(), database, parsed, admin_email=admin)
    except AdminRequiredError:
        _fail("--admin 要是允許名單上的管理者；什麼都沒寫")
    except SummaryReportNotOpenError:
        _fail("找不到這筆回報，或它已經處理過；什麼都沒寫")
    except Exception as exc:
        log.error("處理回報失敗", error_type=type(exc).__name__)
        _fail(f"處理回報失敗（{type(exc).__name__}）；細節見日誌")
    typer.echo(f"已確認無誤：回報 {report.report_id}（{_FIELD_NAMES[report.field.value]}）")


def _fail(headline: str, problems: list[str] | None = None) -> NoReturn:
    typer.echo(headline, err=True)
    for problem in problems or []:
        typer.echo(f"- {problem}", err=True)
    raise typer.Exit(1)
