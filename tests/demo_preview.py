"""重新產生給前端的範例資料 `web/e2e/fixtures/demo-preview/`（Story 3.7、ADR-0061）。

用法（在 repo 根目錄，測試用資料庫 radar-ci 要先起來，**一定要先設好變數**）::

    export RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres
    uv run --no-sync python -m tests.demo_preview

做的事：在測試用的 PostgreSQL 建一個全新的資料庫、遷移、寫入 `demo_export_support.preview_specs()`
的 60 篇假論文（標題 `Fake Paper …`），用正式的 `run_demo_export` 匯出到一個這次專用的暫存資料夾
（和範例放在同一層，`tempfile.mkdtemp` 建，名稱每次不同）。確認匯出成功、manifest 列的每個檔都在
而且雜湊對得上之後，才把範例資料夾換掉；最後刪掉那個資料庫與暫存資料夾。論文 ID 與時間每次不同
（UUIDv7），所以重新產生後的檔案會和原本的不一樣；結構與涵蓋的情況相同。

防呆：
- 只會換掉 `web/e2e/fixtures/demo-preview/`（固定，不收命令列參數）。從 repo 根目錄往下每一層都不准
  是符號連結，上層解析後一定要等於 repo 裡的 `web/e2e/fixtures/`（不會把搬移、刪除導到 repo 外）。
- 整段拿 `web/e2e/fixtures/` 底下的發佈檔案鎖（和匯出寫 `preview/` 時同一把，
  `services.demo_export.demo_output_lock`）：同時跑兩次時一次一個，不會互相搬走、刪掉對方的。
- 換的時候先把舊範例搬到暫存資料夾，新的搬不進去就把舊的搬回來（不會兩邊都沒有）。
- `RADAR_TEST_ADMIN_DATABASE_URL` 一定要是 `127.0.0.1:55432`（測試用的資料庫）；沒設、或指到別的
  主機或埠（例如 5432 的真實資料）時拒絕執行。
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

from radar.domain.demo_export import MANIFEST_FILE, PREVIEW_DIR, DemoManifest
from radar.domain.fingerprints import demo_file_sha256
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.services.demo_export import demo_output_lock, queue_demo_export, run_demo_export
from tests.database import ADMIN_URL_ENV, create_database, drop_database, migrate
from tests.demo_export_support import (
    EXPORTED_AT,
    demo_setup,
    preview_specs,
    publish_snapshots,
    seed_papers,
)

ROOT = Path(__file__).resolve().parents[1]
# 唯一會被換掉的資料夾。
FIXTURES = ROOT / "web" / "e2e" / "fixtures"
TARGET = FIXTURES / "demo-preview"
# 測試用的資料庫（compose 專案 radar-ci）；本機的真實資料在 5432，不准連。
TEST_DATABASE_HOST = "127.0.0.1"
TEST_DATABASE_PORT = 55432

SYSTEM = SystemViewer()


def check_admin_url(raw: str) -> None:
    """`raw` 一定要指到 `127.0.0.1:55432`。

    Raises:
        SystemExit: 沒設、或主機或埠不對。
    """
    if not raw:
        raise SystemExit(f"先設好 {ADMIN_URL_ENV}（測試用資料庫 127.0.0.1:55432）再執行")
    url = make_url(raw)
    if (url.host, url.port) != (TEST_DATABASE_HOST, TEST_DATABASE_PORT):
        raise SystemExit(
            f"{ADMIN_URL_ENV} 要指到測試用資料庫 {TEST_DATABASE_HOST}:{TEST_DATABASE_PORT}"
        )


def check_complete(preview: Path) -> None:
    """`preview` 是一份完整的匯出：manifest 通過模型，列的每個檔都在、雜湊與大小對得上，而且最上層
    只有 manifest 與那一次的匯出目錄。

    Raises:
        SystemExit: 不完整。
    """
    manifest = DemoManifest.model_validate_json((preview / MANIFEST_FILE).read_bytes())
    for entry in manifest.files:
        content = (preview / entry.path).read_bytes()
        if len(content) != entry.size or demo_file_sha256(content) != entry.sha256:
            raise SystemExit(f"匯出的 {entry.path} 和 manifest 對不上，沒有換掉範例")
    names = sorted(path.name for path in preview.iterdir())
    if names != sorted([MANIFEST_FILE, manifest.export_id]):
        raise SystemExit("匯出的資料夾多了別的東西，沒有換掉範例")


def check_target(root: Path, target: Path) -> None:
    """`target` 從 `root` 往下每一層都不是符號連結，上層解析後就是 `root/web/e2e/fixtures`。

    Raises:
        SystemExit: 有一層是符號連結，或位置不對。
    """
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise SystemExit(f"{current.relative_to(root)} 是符號連結，沒有換掉範例")
    expected = root.resolve(strict=True) / "web" / "e2e" / "fixtures"
    if target.parent.resolve(strict=True) != expected:
        raise SystemExit("範例資料夾的位置不對，沒有換掉範例")


def replace_target(fresh: Path, old: Path, target: Path) -> None:
    """把 `fresh` 換成 `target`：舊的先搬到 `old`，新的搬不進去就把舊的搬回來。"""
    had_old = target.exists()
    if had_old:
        target.rename(old)
    try:
        fresh.rename(target)
    except BaseException:
        if had_old:
            old.rename(target)
        raise


def generate(admin_url: str) -> None:
    """產生範例資料，確認完整後才換掉 `TARGET`（見模組說明）。"""
    check_admin_url(admin_url)
    check_target(ROOT, TARGET)
    with demo_output_lock(FIXTURES):
        result = _generate_locked()
    print(json.dumps({"replaced": str(TARGET.relative_to(ROOT)), "export_id": result}))


def _generate_locked() -> str:
    check_target(ROOT, TARGET)
    work = Path(tempfile.mkdtemp(prefix=f".{TARGET.name}.work-", dir=FIXTURES))
    try:
        test_db = create_database(with_extensions=True)
        try:
            migrate(test_db)
            database = Database(test_db.app_url, connect_timeout=5, pool_size=2)
            snapshots = publish_snapshots(test_db)
            seed_papers(test_db, database, preview_specs(), snapshots=snapshots)
            run = queue_demo_export(SYSTEM, database)
            result = run_demo_export(
                SYSTEM, database, demo_setup(), run.id, work / "out", clock=lambda: EXPORTED_AT
            )
            if result is None:
                raise SystemExit("匯出沒有做，沒有換掉範例")
        finally:
            drop_database(test_db)
        fresh = work / "out" / PREVIEW_DIR
        check_complete(fresh)
        check_target(ROOT, TARGET)
        replace_target(fresh, work / "old", TARGET)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result.export_id


def main() -> None:
    if len(sys.argv) != 1:
        raise SystemExit("用法：python -m tests.demo_preview（不收參數，只換掉範例資料夾）")
    generate(os.environ.get(ADMIN_URL_ENV, ""))


if __name__ == "__main__":
    main()
