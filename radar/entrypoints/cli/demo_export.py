"""`radar export-demo`（Story 3.7、ADR-0007、ADR-0061、AD-20）：把示範站要的資料匯出成靜態檔，只寫
`$RADAR_DEMO_EXPORT_DIR/preview/`。

輸出資料夾只看 `RADAR_DEMO_EXPORT_DIR`（和 worker 的匯出工作同一個設定）：這一次中斷、之後由 sweep
補排給 worker 時，寫到的是同一個地方（ADR-0061）。沒設時不連資料庫、退出碼 1。

為最新已發佈的快照新增一筆 `demo_export`（同一個快照已經有排隊中或執行中的就拒絕），接著在這個
行程裡直接做那一次匯出（不經佇列；接手、發佈鎖、補記成功和 worker 走同一套）。自我檢查沒過時不寫
任何檔、`manifest.json` 不換，印出每一條原因、退出碼 1。`RADAR_DEMO_PUBLISH` 開著時拒絕（正式發佈
還沒做）。

輸出與日誌只有匯出編號、篇數與原因，不印連線字串或資料內容；其他錯誤只記類別名稱（和 worker 一樣）。
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import structlog
import typer

from radar.config import (
    ConfigError,
    database_pool_size,
    load_context_values,
    load_domain_config,
    load_models_config,
    load_settings,
    load_taxonomy,
)
from radar.domain.demo_export import DemoExportBusyError
from radar.domain.viewer import SystemViewer
from radar.entrypoints.bootstrap import build_database
from radar.entrypoints.logging_config import configure_logging
from radar.services.demo_export import (
    DemoExportCheckError,
    DemoExportError,
    DemoExportSetup,
    DemoExportTakenOverError,
    NoPublishedSnapshotError,
    queue_demo_export,
    run_demo_export,
)

log = structlog.get_logger(__name__)


def _fail(headline: str, problems: list[str]) -> NoReturn:
    typer.echo(headline, err=True)
    for problem in problems:
        typer.echo(f"- {problem}", err=True)
    raise typer.Exit(1)


def export_demo() -> None:
    """把示範站的資料匯出成靜態檔，寫到 RADAR_DEMO_EXPORT_DIR/preview/（RADAR_DEMO_PUBLISH 預設關，
    這個 epic 不開）。

    退出碼：成功 0；還沒有已發佈的快照、已經有匯出在進行、自我檢查沒過、設定錯誤或其他失敗 1。
    """
    configure_logging()
    try:
        settings = load_settings()
        domain_config = load_domain_config()
        setup = DemoExportSetup(
            models=load_models_config(),
            taxonomy=load_taxonomy(),
            context_values=load_context_values(),
            journals=domain_config.journals,
            exclusions=domain_config.demo_publishers.exclusions(),
            publish=settings.demo_publish,
        )
    except ConfigError as exc:
        log.error("設定錯誤，無法匯出示範站", problems=exc.problems)
        _fail("設定錯誤，沒有匯出：", exc.problems)
    if setup.publish:
        _fail("RADAR_DEMO_PUBLISH 開著，但正式發佈還沒做；這個 epic 只寫 preview/，沒有匯出", [])
    if settings.demo_export_dir is None:
        _fail("沒有設定 RADAR_DEMO_EXPORT_DIR（匯出寫到哪個資料夾），沒有匯出", [])
    output_dir = Path(settings.demo_export_dir)
    demo_export_id: uuid.UUID | None = None
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        run = queue_demo_export(SystemViewer(), database)
        demo_export_id = run.id
        result = run_demo_export(
            SystemViewer(), database, setup, run.id, output_dir, clock=lambda: datetime.now(UTC)
        )
    except NoPublishedSnapshotError:
        _fail("還沒有已發佈的快照，沒有匯出", [])
    except DemoExportBusyError:
        _fail(
            "最新的快照已經有一筆排隊中或執行中的匯出，沒有再匯出一次"
            "（上一次中斷的話：排隊中 1 小時、執行中 6 小時後由 worker 接手做完）",
            [],
        )
    except DemoExportTakenOverError:
        _fail("這一次匯出被別的行程接手，這個指令沒有寫任何檔", [])
    except DemoExportCheckError as exc:
        _fail("自我檢查沒過，沒有寫任何檔、manifest.json 沒換：", exc.problems)
    except DemoExportError as exc:
        _fail(f"匯出失敗（{exc.failure.value}）：{exc}", [])
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error(
            "示範站匯出失敗",
            error_type=type(exc).__name__,
            demo_export_id=None if demo_export_id is None else str(demo_export_id),
        )
        _fail(f"匯出失敗（{type(exc).__name__}）；細節見日誌", [])
    if result is None:
        _fail("這一次匯出已經結束或正由別的行程在做，沒有再做", [])
    typer.echo(f"示範站匯出完成：{result.export_id}")
    typer.echo(f"論文：{result.paper_count} 篇，其中有完整內容 {result.full_content_count} 篇")
    typer.echo(f"manifest：{result.manifest_path}")
