"""`radar members allow`／`set-admin`：允許名單與管理者（ADR-0008 第 4 項、FR-36、Story 3.3）。

以維運身分（`SystemViewer`）執行，每次真的改到資料就寫一筆稽核（AD-23）。輸出與日誌只有成員 ID，
不印 email（email 是個資）。`radar members revoke` 在 Story 7.2。
"""

from typing import Annotated, NoReturn

import structlog
import typer

from radar.config import ConfigError, database_pool_size, load_settings
from radar.domain.auth import InvalidDisplayNameError, InvalidEmailError, MemberNotAllowedError
from radar.domain.settings import Settings
from radar.domain.viewer import SystemViewer
from radar.entrypoints.bootstrap import build_database
from radar.entrypoints.logging_config import configure_logging
from radar.services.members import allow_member, set_member_admin

log = structlog.get_logger(__name__)

members_app = typer.Typer(help="允許名單與成員（member）。", no_args_is_help=True)

_EmailArgument = Annotated[
    str, typer.Argument(help="成員的 email（會轉成小寫）", show_default=False)
]
_NameOption = Annotated[
    str, typer.Option("--name", help="畫面上顯示的名稱（必填）", show_default=False)
]


@members_app.command("allow")
def allow(
    email: _EmailArgument,
    name: _NameOption,
) -> None:
    """把 email 加進允許名單，當下建立成員（還沒有 Google 的 sub）並寫稽核事件。

    已經在名單裡：不變更、不寫稽核，退出碼 0。email 格式不對、名稱空白或太長：退出碼 1，不寫任何
    東西。設定錯誤或資料庫錯誤：退出碼 1，只印錯誤類別。輸出只有成員 ID，不印 email。
    """
    configure_logging()
    settings = _settings("沒有加入允許名單")
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        change = allow_member(SystemViewer(), database, email=email, display_name=name)
    except InvalidEmailError as exc:
        log.error("email 格式不對，沒有加入允許名單")
        _fail(f"沒有加入允許名單：{exc}")
    except InvalidDisplayNameError as exc:
        log.error("名稱不合規定，沒有加入允許名單")
        _fail(f"沒有加入允許名單：{exc}")
    except Exception as exc:
        # 例外訊息可能含主機、帳號或 email，只記類別名稱。
        log.error("加入允許名單失敗", error_type=type(exc).__name__)
        _fail(f"加入允許名單失敗（{type(exc).__name__}）；細節見日誌")
    if change.changed:
        typer.echo(f"已加入允許名單：成員 ID {change.member_id}")
    else:
        typer.echo(f"已經在允許名單裡，沒有變更：成員 ID {change.member_id}")


@members_app.command("set-admin")
def set_admin(email: _EmailArgument) -> None:
    """把允許名單上這個 email 的成員設成管理者並寫稽核事件。

    本來就是管理者：不變更、不寫稽核，退出碼 0。不在允許名單或 email 格式不對：退出碼 1。
    設定錯誤或資料庫錯誤：退出碼 1，只印錯誤類別。輸出只有成員 ID，不印 email。
    """
    configure_logging()
    settings = _settings("沒有設定管理者")
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        change = set_member_admin(SystemViewer(), database, email=email)
    except InvalidEmailError as exc:
        log.error("email 格式不對，沒有設定管理者")
        _fail(f"沒有設定管理者：{exc}")
    except MemberNotAllowedError as exc:
        log.error("email 不在允許名單，沒有設定管理者")
        _fail(f"沒有設定管理者：{exc}")
    except Exception as exc:
        # 例外訊息可能含主機、帳號或 email，只記類別名稱。
        log.error("設定管理者失敗", error_type=type(exc).__name__)
        _fail(f"設定管理者失敗（{type(exc).__name__}）；細節見日誌")
    if change.changed:
        typer.echo(f"已設成管理者：成員 ID {change.member_id}")
    else:
        typer.echo(f"本來就是管理者，沒有變更：成員 ID {change.member_id}")


def _settings(what: str) -> Settings:
    """一般指令的設定；錯了就印原因、退出碼 1。"""
    try:
        return load_settings()
    except ConfigError as exc:
        log.error("設定錯誤", problems=exc.problems)
        _fail(f"設定錯誤，{what}：", exc.problems)


def _fail(headline: str, problems: list[str] | None = None) -> NoReturn:
    typer.echo(headline, err=True)
    for problem in problems or []:
        typer.echo(f"- {problem}", err=True)
    raise typer.Exit(1)
