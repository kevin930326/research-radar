"""`radar classification disagreements`：列出成員意見不同的分類修正（Story 3.11、FR-16）。

以維運身分（`SystemViewer`）只讀，不改資料。每篇印論文 ID 與標題，下面每位成員一行：姓名、主題與
方法各自最新一次選的 `key` 與那一次的時間（台北時間）。不印 email、成員 ID 與理由。
「意見不同」只由有效值 view 判定（同一篇的同一欄，每位成員各取最新一次，兩人以上而且選的不一樣）；
這裡也按欄取最新，所以造成意見不同的那一欄一定印得出來（即使那位成員之後只改了另一欄）。

誰能跑：只限能登入主機、拿得到資料庫設定的維運者（ADR-0063）；指令本身不檢查應用程式的管理者
角色，連哪個資料庫由執行環境的設定決定。
"""

from datetime import datetime
from typing import NoReturn

import structlog
import typer

from radar.config import ConfigError, database_pool_size, load_settings
from radar.domain.classification_fixes import MemberPicks
from radar.domain.snapshot import DATA_TIMEZONE
from radar.domain.viewer import SystemViewer
from radar.entrypoints.bootstrap import build_database
from radar.entrypoints.logging_config import configure_logging
from radar.services.judgments import list_classification_disagreements

log = structlog.get_logger(__name__)

classification_app = typer.Typer(
    help="分類的人工修正（classification_fix）。", no_args_is_help=True
)

# 這位成員沒修正過這一欄（Story 2.7 之前或直接寫進去、只有一欄的修正）時印的字。
_NOT_SENT = "-"


def _field(keys: tuple[str, ...] | None, at: datetime | None) -> str:
    if keys is None or at is None:
        return _NOT_SENT
    when = at.astimezone(DATA_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    return f"{'、'.join(keys)}（{when}）"


def format_pick(pick: MemberPicks) -> str:
    """一位成員：姓名、主題、方法（各自最新一次的 key 與時間，台北），以兩個空白隔開。"""
    topic = _field(pick.topic, pick.topic_at)
    method = _field(pick.method, pick.method_at)
    return f"  {pick.member_name}  主題 {topic}  方法 {method}"


@classification_app.command("disagreements")
def disagreements() -> None:
    """列出成員意見不同的論文：每篇的論文 ID 與標題，以及每位成員主題、方法各自最新一次的選擇與
    時間。

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
        for pick in paper.picks:
            typer.echo(format_pick(pick))
    typer.echo("")
    typer.echo(f"共 {len(papers)} 篇（時間是台北時間）")


def _fail(headline: str, problems: list[str] | None = None) -> NoReturn:
    typer.echo(headline, err=True)
    for problem in problems or []:
        typer.echo(f"- {problem}", err=True)
    raise typer.Exit(1)
