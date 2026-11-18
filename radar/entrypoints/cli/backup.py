"""`radar backup`／`radar restore`（Story 3.18、FR-48、AD-28、ADR-0051）。

- `radar backup`：應用程式角色（`DATABASE_URL`）讀資料庫，`pg_dump` → age（只用公鑰
  `RADAR_BACKUP_AGE_RECIPIENT`）→ 物件儲存的 `backup/`。這個 Story 不送心跳、不設排程（7.5）。
- `radar restore`：擁有者角色（`RADAR_MIGRATION_DATABASE_URL`），讀**解密後**的備份檔或標準輸入
  （主機上沒有私鑰：在 Mac 上 `age -d` 解密、經 SSH 串流過來），只還原到空的資料庫。

輸出與日誌只有筆數、版本與物件的鍵，不印連線字串、email 或資料內容。
"""

import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Annotated, NoReturn

import structlog
import typer

from radar.config import ConfigError, database_pool_size, load_migration_settings, load_settings
from radar.domain.backup import (
    COUNT_LABELS,
    BackupError,
    BackupManifest,
    BackupMode,
    RestoreCountMismatchError,
    RestoreRefusedError,
)
from radar.entrypoints.bootstrap import (
    build_backup_tools,
    build_database,
    build_object_store,
    build_restore_database,
    build_restore_tools,
)
from radar.entrypoints.logging_config import configure_logging
from radar.services.backup import run_backup, run_restore

log = structlog.get_logger(__name__)

_SharedOnlyOption = Annotated[
    bool,
    typer.Option(
        "--shared-only",
        help="成員資料表只帶結構、不帶任何一列（例如 Mac 搬到試用主機）",
    ),
]
_SourceArgument = Annotated[
    str,
    typer.Argument(
        help="解密後的備份檔；- 代表標準輸入（經 SSH 串流時用）",
        metavar="備份檔或-",
        show_default=False,
    ),
]


def _utc_now() -> datetime:
    """備份時間（測試換掉它來固定時間）。"""
    return datetime.now(UTC)


def backup(shared_only: _SharedOnlyOption = False) -> None:
    """把整個資料庫加密備份到物件儲存的 backup/（--shared-only：成員資料表只有結構）。

    先檢查 pg_dump 的大版本不低於資料庫；沒有設定 age 公鑰（RADAR_BACKUP_AGE_RECIPIENT）就不備份，
    不會改成不加密。失敗時 backup/ 不留半個檔，退出碼 1，只印錯誤類別與原因，不印連線字串。
    """
    configure_logging()
    mode = BackupMode.SHARED_ONLY if shared_only else BackupMode.FULL
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("設定錯誤，沒有備份", problems=exc.problems)
        _fail("設定錯誤，沒有備份：", exc.problems)
    recipient = settings.backup_age_recipient
    if recipient is None:
        log.error("沒有設定 age 公鑰，沒有備份")
        _fail(
            "沒有設定 age 公鑰（環境變數 RADAR_BACKUP_AGE_RECIPIENT），沒有備份；"
            "不會改成不加密的備份"
        )
    try:
        store = build_object_store(settings)
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        pg_dump_version, start_dump = build_backup_tools(settings)
        result = run_backup(
            database,
            store,
            mode=mode,
            recipient=recipient,
            pg_dump_version=pg_dump_version,
            start_dump=start_dump,
            now=_utc_now,
        )
    except ConfigError as exc:
        log.error("設定錯誤，沒有備份", problems=exc.problems)
        _fail("設定錯誤，沒有備份：", exc.problems)
    except BackupError as exc:
        log.error("備份失敗", error_type=type(exc).__name__)
        _fail(f"備份失敗：{exc}")
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("備份失敗", error_type=type(exc).__name__)
        _fail(f"備份失敗（{type(exc).__name__}）；backup/ 沒有留下這次的檔案，細節見日誌")
    typer.echo(f"已備份：{result.object_key}")
    for line in _manifest_lines(result.manifest):
        typer.echo(line)


def restore(source: _SourceArgument) -> None:
    """把解密後的備份還原到空的資料庫（擁有者角色 RADAR_MIGRATION_DATABASE_URL）。

    目標不是空的、版本比備份時舊：不執行，印出原因。整個還原在一個交易裡，失敗就整個退回。
    還原後重新數筆數和清單比對，不一致就失敗、印出哪一項差多少；還原的資料留著，不自動刪除。
    """
    configure_logging()
    try:
        settings = load_migration_settings()
    except ConfigError as exc:
        log.error("設定錯誤，沒有還原", problems=exc.problems)
        _fail("設定錯誤，沒有還原：", exc.problems)
    with ExitStack() as stack:
        stream = _open_source(source, stack)
        try:
            database = build_restore_database(settings)
            pg_restore_version, restore_archive = build_restore_tools(settings)
            result = run_restore(
                database,
                stream,
                pg_restore_version=pg_restore_version,
                restore_archive=restore_archive,
            )
        except ConfigError as exc:
            log.error("設定錯誤，沒有還原", problems=exc.problems)
            _fail("設定錯誤，沒有還原：", exc.problems)
        except RestoreRefusedError as exc:
            log.error("還原前的檢查沒過，沒有還原", problems=len(exc.problems))
            _fail("沒有還原：", exc.problems)
        except RestoreCountMismatchError as exc:
            log.error("還原後的筆數和清單不一致", differences=len(exc.differences))
            _fail(
                "還原後的筆數和清單不一致（還原的資料留著，沒有刪除；"
                "要不要刪掉重來由 Kevin 決定）：",
                exc.differences,
            )
        except BackupError as exc:
            log.error("還原失敗", error_type=type(exc).__name__)
            _fail(f"還原失敗：{exc}")
        except Exception as exc:
            # 例外訊息可能含主機與帳號，只記類別名稱。
            log.error("還原失敗", error_type=type(exc).__name__)
            _fail(f"還原失敗（{type(exc).__name__}）；細節見日誌")
    typer.echo("已還原，筆數和清單一致")
    for line in _manifest_lines(result.manifest):
        typer.echo(line)


def _open_source(source: str, stack: ExitStack) -> IO[bytes]:
    if source == "-":
        if sys.stdin.isatty():
            _fail(
                "標準輸入是終端機：要用管線把解密後的備份傳進來（例如 age -d … | radar restore -）"
            )
        return sys.stdin.buffer
    path = Path(source)
    if not path.is_file():
        _fail("找不到備份檔（或它不是一般檔案）")
    try:
        return stack.enter_context(path.open("rb"))
    except OSError as exc:
        _fail(f"讀不了備份檔（{type(exc).__name__}）")


def _manifest_lines(manifest: BackupManifest) -> list[str]:
    counts = manifest.counts.model_dump()
    alembic = "、".join(manifest.alembic_revisions) or "—"
    lines = [
        f"模式：{manifest.mode.label}",
        f"備份時間（UTC）：{manifest.created_at.isoformat()}",
        f"PostgreSQL {manifest.postgres_version}、pgvector {manifest.pgvector_version}、"
        f"pg_dump {manifest.pg_dump_version}、Alembic {alembic}",
    ]
    lines += [f"{COUNT_LABELS[name]}：{value}" for name, value in counts.items()]
    lines += [
        f"成員資料表 {table}：{rows} 列"
        for table, rows in sorted(manifest.member_table_rows.items())
    ]
    return lines


def _fail(headline: str, problems: list[str] | None = None) -> NoReturn:
    typer.echo(headline, err=True)
    for problem in problems or []:
        typer.echo(f"- {problem}", err=True)
    raise typer.Exit(1)
