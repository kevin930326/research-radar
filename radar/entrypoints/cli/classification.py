"""`radar classification disagreements`：列出成員意見不同的分類修正（Story 3.11、FR-16）。

以維運身分（`SystemViewer`）只讀，不改資料。每篇印論文 ID 與標題，下面每位成員一行：最新一次送出的
時間（台北時間）、姓名、選的主題與方法（分類表的 `key`）。不印 email、成員 ID 與理由。
「意見不同」只由有效值 view 判定（同一篇的同一欄，每位成員各取最新一次，兩人以上而且選的不一樣）。
"""

from typing import NoReturn

import structlog
import typer

from radar.config import ConfigError, database_pool_size, load_settings
from radar.domain.classification_fixes import ClassificationFixEntry
from radar.domain.snapshot import DATA_TIMEZONE
from radar.domain.viewer import SystemViewer
from radar.entrypoints.bootstrap import build_database
from radar.entrypoints.logging_config import configure_logging
from radar.services.judgments import list_classification_disagreements

log = structlog.get_logger(__name__)

classification_app = typer.Typer(
    help="分類的人工修正（classification_fix）。", no_args_is_help=True
)

# 沒有送這一欄（Story 2.7 之前或直接寫進去、只有一欄的修正）時印的字。
_NOT_SENT = "-"


def _keys(keys: tuple[str, ...] | None) -> str:
    return _NOT_SENT if keys is None else "、".join(keys)


def format_pick(entry: ClassificationFixEntry) -> str:
    """一位成員最新一次的選擇：時間（台北）、姓名、主題、方法，以兩個空白隔開。"""
    when = entry.fixed_at.astimezone(DATA_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    return f"  {when}  {entry.member_name}  主題 {_keys(entry.topic)}  方法 {_keys(entry.method)}"


@classification_app.command("disagreements")
def disagreements() -> None:
    """列出成員意見不同的論文：每篇的論文 ID 與標題，以及每位成員最新一次選的主題、方法與時間。

    沒有意見不同的論文：印一行說明，退出碼 0。設定錯誤或資料庫錯誤：退出碼 1，只印錯誤類別。
    """
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("設定錯誤", problems=exc.problems)
        _fail("設定錯誤，沒有列出意見不同的修正：", exc.problems)
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        papers = list_classification_disagreements(SystemViewer(), database)
    except Exception as exc:
        # 例外訊息可能含主機或帳號，只記類別名稱。
        log.error("列出意見不同的修正失敗", error_type=type(exc).__name__)
        _fail(f"列出意見不同的修正失敗（{type(exc).__name__}）；細節見日誌")
    if not papers:
        typer.echo("沒有成員意見不同的分類修正")
        return
    for index, paper in enumerate(papers):
        if index > 0:
            typer.echo("")
        typer.echo(f"論文 {paper.paper_id}  {paper.title}")
        for entry in paper.picks:
            typer.echo(format_pick(entry))
    typer.echo("")
    typer.echo(f"共 {len(papers)} 篇（時間是台北時間）")


def _fail(headline: str, problems: list[str] | None = None) -> NoReturn:
    typer.echo(headline, err=True)
    for problem in problems or []:
        typer.echo(f"- {problem}", err=True)
    raise typer.Exit(1)
