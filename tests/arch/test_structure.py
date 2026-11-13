"""目錄照架構文件的 Structural Seed（`web/` 留到 Epic 3）。"""

from pathlib import Path

import pytest

from radar.config import env_name
from radar.domain.settings import MigrationSettings, Settings
from tests.conftest import MARKED_DIRS

ROOT = Path(__file__).resolve().parents[2]

REQUIRED_DIRS = [
    "radar/domain",
    "radar/services",
    "radar/repo",
    "radar/adapters",
    "radar/entrypoints/api",
    "radar/entrypoints/cli",
    "radar/entrypoints/worker",
    "config",
    "prompts",
    "migrations",
    "deploy",
    "tests/unit",
    "tests/integration",
    "tests/arch",
    "tests/golden",
    "tests/fixtures",
    "tests/cassettes",
    "evals",
]

REQUIRED_FILES = [
    "radar/config.py",
    "radar/ports.py",
    "radar/domain/settings.py",
    "radar/entrypoints/bootstrap.py",
    "compose.yaml",
    "Dockerfile",
    ".dockerignore",
    ".env.example",
]


@pytest.mark.parametrize("relative", REQUIRED_DIRS)
def test_directory_exists(relative: str) -> None:
    assert (ROOT / relative).is_dir()


@pytest.mark.parametrize("relative", REQUIRED_FILES)
def test_file_exists(relative: str) -> None:
    assert (ROOT / relative).is_file()


def test_python_packages_have_init() -> None:
    for directory in (ROOT / "radar").rglob("*"):
        if directory.is_dir() and directory.name != "__pycache__":
            assert (directory / "__init__.py").is_file(), directory


def test_no_package_named_data() -> None:
    # .gitignore 忽略 data/ 與 **/data/，取這個名字的套件不會進 repo。
    assert not any(p.name == "data" for p in (ROOT / "radar").rglob("*") if p.is_dir())


def test_env_is_ignored_and_only_example_is_tracked() -> None:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in lines
    assert ".env.*" in lines
    assert "!.env.example" in lines


def test_env_example_lists_every_setting_without_values() -> None:
    entries = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )

    for field in Settings.model_fields:
        assert env_name(field) in entries
    assert all(value == "" for value in entries.values())


def test_every_test_file_sits_under_a_marked_dir() -> None:
    # CI 依標記分三次跑；不在這三個資料夾的測試沒有標記，會被默默略過（tests/conftest.py 也會擋）。
    tests_root = ROOT / "tests"
    misplaced = [
        str(path.relative_to(ROOT))
        for path in tests_root.rglob("test_*.py")
        if path.relative_to(tests_root).parts[0] not in MARKED_DIRS
    ]
    assert misplaced == []


def test_env_example_lists_migration_settings_without_values() -> None:
    entries = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )

    for field in MigrationSettings.model_fields:
        assert env_name(field) in entries
    assert entries["RADAR_MIGRATION_DATABASE_URL"] == ""
