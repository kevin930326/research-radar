"""`radar export-schema`：輸出成員版 API 的 `openapi.json`（AD-21、NFR-12、ADR-0047）。

文件由 api 的程式產生（`radar.entrypoints.api.openapi`）。ADR-0001 規定 api、cli、worker 三個入口
互相不 import，所以這裡用子行程執行它、取標準輸出寫檔，不 import api（CLI 也不必載入 FastAPI）。
`demo-openapi.json` 在 Story 3.7 加。
"""

import subprocess
import sys
from pathlib import Path
from typing import Annotated

import structlog
import typer

from radar.entrypoints.logging_config import configure_logging

# 產生文件的模組（在 api 套件裡）。
OPENAPI_MODULE = "radar.entrypoints.api.openapi"
# 架構文件的專案結構：`openapi.json` 放 repo 根目錄。
DEFAULT_OUTPUT = Path("openapi.json")
# 正常幾秒內完成；卡住時不要一直等。
TIMEOUT_SECONDS = 120

log = structlog.get_logger(__name__)


def export_schema(
    output: Annotated[
        Path, typer.Option("--output", help="寫到哪個檔（預設是工作目錄的 openapi.json）")
    ] = DEFAULT_OUTPUT,
) -> None:
    """輸出成員版 API 的 openapi.json（前端型別由它產生；CI 檢查它和程式一致）。"""
    configure_logging()
    try:
        result = subprocess.run(
            [sys.executable, "-m", OPENAPI_MODULE],
            capture_output=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.error("產生 openapi.json 逾時，沒有寫檔", timeout_seconds=TIMEOUT_SECONDS)
        raise typer.Exit(1) from None
    if result.returncode != 0 or not result.stdout:
        # 子行程的錯誤輸出直接轉出去（是程式自己的 traceback，給開發者看）。
        sys.stderr.write(result.stderr.decode("utf-8", errors="replace"))
        log.error("產生 openapi.json 失敗，沒有寫檔", returncode=result.returncode)
        raise typer.Exit(1)
    output.write_bytes(result.stdout)
    typer.echo(f"已寫入 {output}")
