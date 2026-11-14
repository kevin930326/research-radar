"""`radar reparse` 的輸出與退出碼（替身取代 `reparse_papers` 與 bootstrap，不連資料庫）。"""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import radar.entrypoints.cli as cli
from radar.adapters.fake import FakeObjectStore
from radar.domain.domain_config import ExclusionRulesConfig
from radar.domain.scope import ScopeSettings
from radar.domain.viewer import SystemViewer
from radar.services.reparse import ReparseResult

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[2]
SECRET_URL = "postgresql://radar_app:s3cret-APP@db.example:5432/radar"
OBJECT_STORE_ENV = {
    "RADAR_OBJECT_STORE_ENDPOINT_URL": "http://127.0.0.1:9000",
    "RADAR_OBJECT_STORE_REGION": "us-east-1",
    "RADAR_OBJECT_STORE_BUCKET": "radar",
    "RADAR_OBJECT_STORE_ACCESS_KEY_ID": "test-access",
    "RADAR_OBJECT_STORE_SECRET_ACCESS_KEY": "test-secret-KEY",
}
RESULT = ReparseResult(
    raw_keys=11,
    not_in_database=2,
    missing_object=1,
    hash_mismatch=3,
    parse_error=4,
    id_mismatch=5,
    staged=6,
    updated=7,
    stale=8,
    conflicts=9,
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)
    monkeypatch.delenv("RADAR_CLI_DATABASE_POOL_SIZE", raising=False)
    for name, value in OBJECT_STORE_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    recorded: dict[str, Any] = {}
    store = FakeObjectStore()
    database = object()

    def build_database(_settings: object, **kwargs: Any) -> object:
        recorded["build_database"] = kwargs
        return database

    def build_object_store(_settings: object) -> FakeObjectStore:
        return store

    def reparse_papers(actor: object, db: object, st: object, **kwargs: Any) -> ReparseResult:
        recorded["reparse"] = {"actor": actor, "database": db, "store": st, **kwargs}
        return RESULT

    monkeypatch.setattr(cli, "build_database", build_database)
    monkeypatch.setattr(cli, "build_object_store", build_object_store)
    monkeypatch.setattr(cli, "reparse_papers", reparse_papers)
    recorded["database"] = database
    recorded["store"] = store
    return recorded


def test_help_lists_reparse() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "reparse" in result.output


@pytest.mark.usefixtures("env")
def test_success_prints_every_number(calls: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["reparse"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "重新解析完成"
    assert [line.rsplit("：", 1)[1] for line in lines[1:]] == [
        "11",
        "2",
        "1",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
    ]
    assert calls["build_database"] == {"pool_size": 2}
    reparse = calls["reparse"]
    assert isinstance(reparse["actor"], SystemViewer)
    assert reparse["database"] is calls["database"]
    assert reparse["store"] is calls["store"]
    assert isinstance(reparse["scope"], ScopeSettings)
    assert "1471-7727" in reparse["scope"].journals
    assert isinstance(reparse["exclusion_rules"], ExclusionRulesConfig)
    assert "parse" not in reparse
    assert "source" not in reparse


@pytest.mark.usefixtures("calls")
def test_missing_database_url_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    result = runner.invoke(cli.app, ["reparse"])

    assert result.exit_code == 1
    assert "DATABASE_URL" in result.stderr
    assert "重新解析完成" not in result.stdout


@pytest.mark.usefixtures("env")
def test_missing_object_store_settings_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RADAR_OBJECT_STORE_BUCKET")
    monkeypatch.setattr(cli, "reparse_papers", _explode)

    result = runner.invoke(cli.app, ["reparse"])

    assert result.exit_code == 1
    assert "RADAR_OBJECT_STORE_BUCKET" in result.stderr
    assert "test-secret-KEY" not in result.output


def _explode(*_args: Any, **_kwargs: Any) -> ReparseResult:
    raise RuntimeError(f"connection to {SECRET_URL} failed")


@pytest.mark.usefixtures("env", "calls")
def test_failure_prints_only_error_class(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "reparse_papers", _explode)

    result = runner.invoke(cli.app, ["reparse"])

    assert result.exit_code == 1
    assert "RuntimeError" in result.stderr
    assert "s3cret-APP" not in result.output
    assert "db.example" not in result.output
    assert "s3cret-APP" not in capsys.readouterr().out
