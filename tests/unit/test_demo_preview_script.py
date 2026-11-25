"""範例產生器 `tests/demo_preview.py` 的防呆（Story 3.7 第 2 輪審查 G6）：目標的每一層上層都不准是
符號連結、替換失敗時舊範例留著、整段拿發佈檔案鎖。全部在 pytest 的暫存資料夾裡做，不連資料庫、
不動 repo 裡的範例。"""

import fcntl
import os
from pathlib import Path

import pytest

import tests.demo_preview as demo_preview
from radar.services.demo_export import DEMO_OUTPUT_LOCK_FILE


def _layout(root: Path) -> Path:
    fixtures = root / "web" / "e2e" / "fixtures"
    fixtures.mkdir(parents=True)
    target = fixtures / "demo-preview"
    target.mkdir()
    (target / "manifest.json").write_text("old", encoding="utf-8")
    return target


def test_target_inside_the_repo_is_accepted(tmp_path: Path) -> None:
    target = _layout(tmp_path)

    demo_preview.check_target(tmp_path, target)


@pytest.mark.parametrize(
    "link", ["web", "web/e2e", "web/e2e/fixtures", "web/e2e/fixtures/demo-preview"]
)
def test_any_symlinked_level_is_refused(tmp_path: Path, link: str) -> None:
    root = tmp_path / "repo"
    real = _layout(root)
    elsewhere = tmp_path / "elsewhere"
    (root / link).rename(elsewhere)
    (root / link).symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(SystemExit, match="符號連結"):
        demo_preview.check_target(root, real)


def test_failed_replacement_puts_the_old_example_back(tmp_path: Path) -> None:
    target = _layout(tmp_path)
    missing = tmp_path / "work" / "fresh"  # 不存在：搬不進去
    (tmp_path / "work").mkdir()

    with pytest.raises(FileNotFoundError):
        demo_preview.replace_target(missing, tmp_path / "work" / "old", target)

    assert (target / "manifest.json").read_text(encoding="utf-8") == "old"


def test_replacement_swaps_in_the_new_example(tmp_path: Path) -> None:
    target = _layout(tmp_path)
    fresh = tmp_path / "work" / "fresh"
    fresh.mkdir(parents=True)
    (fresh / "manifest.json").write_text("new", encoding="utf-8")

    demo_preview.replace_target(fresh, tmp_path / "work" / "old", target)

    assert (target / "manifest.json").read_text(encoding="utf-8") == "new"
    assert (tmp_path / "work" / "old" / "manifest.json").read_text(encoding="utf-8") == "old"


def test_generation_holds_the_output_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _layout(tmp_path)
    monkeypatch.setattr(demo_preview, "ROOT", tmp_path)
    monkeypatch.setattr(demo_preview, "FIXTURES", target.parent)
    monkeypatch.setattr(demo_preview, "TARGET", target)
    held: list[bool] = []

    def probe() -> str:
        descriptor = os.open(target.parent / DEMO_OUTPUT_LOCK_FILE, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(True)
        finally:
            os.close(descriptor)
        return "2-1"

    monkeypatch.setattr(demo_preview, "_generate_locked", probe)

    demo_preview.generate("postgresql://postgres@127.0.0.1:55432/postgres")

    assert held == [True]
