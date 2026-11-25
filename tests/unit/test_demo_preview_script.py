"""範例產生器 `tests/demo_preview.py` 的防呆（Story 3.7 第 2 輪審查 G6、第 3 輪 H5）：目標的
每一層上層都不准是符號連結、搬移相對於目錄 fd、替換失敗時舊範例留著、連舊的都搬不回去時
留著備份、整段拿 `web/e2e/fixtures/` 的檔案鎖。全部在 pytest 的暫存資料夾裡做，不連資料庫、
不動 repo 裡的範例。"""

import fcntl
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import tests.demo_preview as demo_preview


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


@pytest.fixture
def fixtures_fd(tmp_path: Path) -> Iterator[int]:
    _layout(tmp_path)
    descriptor = demo_preview.open_fixtures(tmp_path, tmp_path / "web" / "e2e" / "fixtures")
    yield descriptor
    os.close(descriptor)


def _work(tmp_path: Path, *, fresh: bool) -> Path:
    work = tmp_path / "web" / "e2e" / "fixtures" / ".work"
    (work / "out").mkdir(parents=True)
    if fresh:
        (work / "out" / "preview").mkdir()
        (work / "out" / "preview" / "manifest.json").write_text("new", encoding="utf-8")
    return work


@pytest.mark.parametrize("link", ["web", "web/e2e", "web/e2e/fixtures"])
def test_opening_the_fixtures_refuses_any_symlinked_level(tmp_path: Path, link: str) -> None:
    root = tmp_path / "repo"
    _layout(root)
    elsewhere = tmp_path / "elsewhere"
    (root / link).rename(elsewhere)
    (root / link).symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(SystemExit, match="符號連結"):
        demo_preview.open_fixtures(root, root / "web" / "e2e" / "fixtures")


def test_failed_replacement_puts_the_old_example_back(tmp_path: Path, fixtures_fd: int) -> None:
    _work(tmp_path, fresh=False)  # 沒有 out/preview：搬不進去

    with pytest.raises(FileNotFoundError):
        demo_preview.replace_target(fixtures_fd, ".work", "demo-preview")

    target = tmp_path / "web" / "e2e" / "fixtures" / "demo-preview"
    assert (target / "manifest.json").read_text(encoding="utf-8") == "old"


def test_replacement_swaps_in_the_new_example(tmp_path: Path, fixtures_fd: int) -> None:
    work = _work(tmp_path, fresh=True)

    demo_preview.replace_target(fixtures_fd, ".work", "demo-preview")

    target = tmp_path / "web" / "e2e" / "fixtures" / "demo-preview"
    assert (target / "manifest.json").read_text(encoding="utf-8") == "new"
    assert (work / "old" / "manifest.json").read_text(encoding="utf-8") == "old"


def test_replacement_moves_relative_to_the_opened_fixtures(
    tmp_path: Path, fixtures_fd: int
) -> None:
    # H5：打開之後 fixtures 被搬走、原位換成指到別處的符號連結：搬移仍在原本的資料夾裡做。
    work = _work(tmp_path, fresh=True)
    fixtures = tmp_path / "web" / "e2e" / "fixtures"
    moved = tmp_path / "web" / "e2e" / "moved"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fixtures.rename(moved)
    fixtures.symlink_to(elsewhere, target_is_directory=True)

    demo_preview.replace_target(fixtures_fd, work.name, "demo-preview")

    assert list(elsewhere.iterdir()) == []
    assert (moved / "demo-preview" / "manifest.json").read_text(encoding="utf-8") == "new"


def _fail_after_the_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一次搬（舊的搬到備份）照做，之後每一次搬都失敗。"""
    real = os.rename
    calls: list[int] = []

    def rename(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) > 1:
            raise OSError(5, "disk")
        real(*args, **kwargs)

    monkeypatch.setattr(os, "rename", rename)


def test_backup_is_kept_when_the_old_example_cannot_be_put_back(
    tmp_path: Path, fixtures_fd: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H5：新的搬不進去、舊的也搬不回去：丟 BackupKeptError，訊息帶備份的位置。
    work = _work(tmp_path, fresh=True)
    monkeypatch.setattr(demo_preview, "FIXTURES", tmp_path / "web" / "e2e" / "fixtures")
    _fail_after_the_backup(monkeypatch)

    with pytest.raises(demo_preview.BackupKeptError) as caught:
        demo_preview.replace_target(fixtures_fd, ".work", "demo-preview")

    monkeypatch.undo()
    assert caught.value.backup == work / "old"
    assert str(work / "old") in str(caught.value)
    assert (work / "old" / "manifest.json").read_text(encoding="utf-8") == "old"


def _patch_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    fixtures = tmp_path / "web" / "e2e" / "fixtures"
    monkeypatch.setattr(demo_preview, "ROOT", tmp_path)
    monkeypatch.setattr(demo_preview, "FIXTURES", fixtures)
    monkeypatch.setattr(demo_preview, "TARGET", fixtures / "demo-preview")

    def export(output: Path) -> str:
        (output / "preview").mkdir(parents=True)
        (output / "preview" / "manifest.json").write_text("new", encoding="utf-8")
        return "2-1"

    monkeypatch.setattr(demo_preview, "_export", export)
    monkeypatch.setattr(demo_preview, "check_complete", lambda preview: None)
    return fixtures


def test_generation_keeps_the_work_directory_when_the_backup_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H5：連舊的都搬不回去時，不在 finally 刪掉暫存資料夾（裡面有舊範例的備份）。
    _layout(tmp_path)
    fixtures = _patch_paths(monkeypatch, tmp_path)
    _fail_after_the_backup(monkeypatch)

    with pytest.raises(demo_preview.BackupKeptError) as caught:
        demo_preview._generate_locked()

    monkeypatch.undo()
    assert (caught.value.backup / "manifest.json").read_text(encoding="utf-8") == "old"
    assert caught.value.backup.parent.parent == fixtures


def _interrupt_after_the_backup(monkeypatch: pytest.MonkeyPatch, *, restore: bool) -> None:
    """第一次搬（舊的搬到備份）照做；`restore` 時第二次（新的搬進去）丟 OSError、第三次（舊的
    搬回來）丟 KeyboardInterrupt，不然第二次就丟 KeyboardInterrupt。"""
    real = os.rename
    calls: list[int] = []

    def rename(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            real(*args, **kwargs)
            return
        if restore and len(calls) == 2:
            raise OSError(5, "disk")
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "rename", rename)


@pytest.mark.parametrize("restore", [True, False], ids=["while-restoring", "while-replacing"])
def test_generation_keeps_the_backup_when_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restore: bool,
) -> None:
    # 第 4 輪（Codex 第 3 次複查高 #8 剩下的）：還原舊範例途中（或搬新的進去時）按 Ctrl-C，
    # KeyboardInterrupt 不是 OSError；備份還在、範例位置是空的，就不在 finally 刪掉暫存資料夾。
    _layout(tmp_path)
    fixtures = _patch_paths(monkeypatch, tmp_path)
    _interrupt_after_the_backup(monkeypatch, restore=restore)

    with pytest.raises(KeyboardInterrupt):
        demo_preview._generate_locked()

    monkeypatch.undo()
    (work,) = [path for path in fixtures.iterdir() if path.name != "demo-preview"]
    assert not (fixtures / "demo-preview").exists()
    assert (work / "old" / "manifest.json").read_text(encoding="utf-8") == "old"
    assert str(work / "old") in capsys.readouterr().err


def test_generation_interrupted_after_the_old_example_is_back_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 範例位置有東西（舊的已經搬回來）時，中斷照樣清掉暫存資料夾。
    _layout(tmp_path)
    fixtures = _patch_paths(monkeypatch, tmp_path)
    real = os.rename
    calls: list[int] = []

    def rename(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise OSError(5, "disk")
        real(*args, **kwargs)
        if len(calls) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "rename", rename)

    with pytest.raises(KeyboardInterrupt):
        demo_preview._generate_locked()

    monkeypatch.undo()
    assert sorted(path.name for path in fixtures.iterdir()) == ["demo-preview"]
    assert (fixtures / "demo-preview" / "manifest.json").read_text(encoding="utf-8") == "old"


def test_generation_cleans_up_the_work_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _layout(tmp_path)
    fixtures = _patch_paths(monkeypatch, tmp_path)

    assert demo_preview._generate_locked() == "2-1"

    assert sorted(path.name for path in fixtures.iterdir()) == ["demo-preview"]
    assert (fixtures / "demo-preview" / "manifest.json").read_text(encoding="utf-8") == "new"


def test_generation_holds_the_output_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _layout(tmp_path)
    monkeypatch.setattr(demo_preview, "ROOT", tmp_path)
    monkeypatch.setattr(demo_preview, "FIXTURES", target.parent)
    monkeypatch.setattr(demo_preview, "TARGET", target)
    held: list[bool] = []

    def probe() -> str:
        descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
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
