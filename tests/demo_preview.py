"""重新產生給前端的範例資料 `web/e2e/fixtures/demo-preview/`（Story 3.7、ADR-0061）。

用法（在 repo 根目錄，測試用資料庫 radar-ci 要先起來，**一定要先設好變數**）::

    export RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres
    uv run --no-sync python -m tests.demo_preview web/e2e/fixtures/demo-preview

做的事：在測試用的 PostgreSQL 建一個全新的資料庫、遷移、寫入 `demo_export_support.preview_specs()`
的 60 篇假論文（標題 `Fake Paper …`），用正式的 `run_demo_export` 匯出，把 `preview/` 底下的內容
整個換掉目標資料夾，最後刪掉那個資料庫。論文 ID 與時間每次不同（UUIDv7），所以重新產生後的檔案
會和原本的不一樣；結構與涵蓋的情況相同。

沒設 `RADAR_TEST_ADMIN_DATABASE_URL`、或它指到 5432（本機的真實資料）時拒絕執行。
"""

import os
import shutil
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from radar.domain.demo_export import PREVIEW_DIR
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.services.demo_export import queue_demo_export, run_demo_export
from tests.database import ADMIN_URL_ENV, create_database, drop_database, migrate
from tests.demo_export_support import (
    EXPORTED_AT,
    demo_setup,
    preview_specs,
    publish_snapshots,
    seed_papers,
)

# 本機的真實資料在這個埠（compose 專案 `radar`）：不准連。
REAL_DATA_PORT = 5432

SYSTEM = SystemViewer()


def _check_admin_url() -> None:
    raw = os.environ.get(ADMIN_URL_ENV, "")
    if not raw:
        raise SystemExit(f"先設好 {ADMIN_URL_ENV}（測試用資料庫，例如 55432）再執行")
    if make_url(raw).port in (None, REAL_DATA_PORT):
        raise SystemExit(f"{ADMIN_URL_ENV} 要明確指到測試用資料庫的埠，不能是 {REAL_DATA_PORT}")


def generate(target: Path) -> None:
    """產生範例資料，整個換掉 `target`（見模組說明）。"""
    _check_admin_url()
    test_db = create_database(with_extensions=True)
    try:
        migrate(test_db)
        database = Database(test_db.app_url, connect_timeout=5, pool_size=2)
        snapshots = publish_snapshots(test_db)
        seed_papers(test_db, database, preview_specs(), snapshots=snapshots)
        work = target.parent / f".{target.name}.work"
        shutil.rmtree(work, ignore_errors=True)
        run = queue_demo_export(SYSTEM, database)
        run_demo_export(SYSTEM, database, demo_setup(), run.id, work, clock=lambda: EXPORTED_AT)
        shutil.rmtree(target, ignore_errors=True)
        (work / PREVIEW_DIR).rename(target)
        shutil.rmtree(work, ignore_errors=True)
    finally:
        drop_database(test_db)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("用法：python -m tests.demo_preview <目標資料夾>")
    generate(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
