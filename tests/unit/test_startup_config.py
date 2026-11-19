"""領域設定錯了就不啟動（AD-18）：api、worker 的 `main()` 回 1，CLI 每個指令退出碼 1。

`RADAR_CONFIG_DIR` 指到改壞的設定；其餘環境變數是可以啟動的測試值，證明擋下的是領域設定。
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import radar.entrypoints.api.main as api_main
import radar.entrypoints.cli as cli
import radar.entrypoints.worker.main as worker_main

ROOT = Path(__file__).resolve().parents[2]
APP_URL = "postgresql://radar_app@db:5432/radar"
BAD_FIELD = "journals.yaml：journals.2.issn_l："

runner = CliRunner()


class _ShouldNotBuildError(Exception):
    """設定錯了卻走到組裝這一步。"""


@pytest.fixture
def bad_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    journals = target / "journals.yaml"
    text = journals.read_text(encoding="utf-8")
    journals.write_text(text.replace("issn_l: 0742-1222", "issn_l: abc"), encoding="utf-8")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(target))
    monkeypatch.setenv("DATABASE_URL", APP_URL)
    return target


def _fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
    raise _ShouldNotBuildError


def _problems(out: str) -> list[str]:
    for line in out.splitlines():
        if line.startswith("{"):
            record = json.loads(line)
            if record.get("event") == "設定錯誤，無法啟動":
                return [str(problem) for problem in record["problems"]]
    return []


def test_api_does_not_start(
    bad_config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(api_main, "build_readiness_probes", _fail_if_called)

    assert api_main.main() == 1

    problems = _problems(capsys.readouterr().out)
    assert any(problem.startswith(BAD_FIELD) for problem in problems), problems


def test_worker_does_not_start(
    bad_config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(worker_main, "build_worker", _fail_if_called)

    assert worker_main.main() == 1

    problems = _problems(capsys.readouterr().out)
    assert any(problem.startswith(BAD_FIELD) for problem in problems), problems


@pytest.mark.parametrize("args", [["version"], ["enqueue", "sweep"], ["migrate"], ["reparse"]])
def test_cli_command_does_not_run(
    bad_config: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setattr(cli, "build_job_enqueuer", _fail_if_called)
    monkeypatch.setattr(cli, "build_migration_runner", _fail_if_called)
    monkeypatch.setattr(cli, "build_database", _fail_if_called)
    monkeypatch.setattr(cli, "build_object_store", _fail_if_called)
    monkeypatch.setattr(cli, "reparse_papers", _fail_if_called)
    monkeypatch.setenv("RADAR_MIGRATION_DATABASE_URL", "postgresql://radar_owner@db:5432/radar")

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 1
    assert "領域設定錯誤" in result.stderr
    assert f"- {BAD_FIELD}" in result.stderr
    assert result.stdout == ""


def test_cli_missing_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path / "nowhere"))

    result = runner.invoke(cli.app, ["version"])

    assert result.exit_code == 1
    assert "journals.yaml：找不到檔案" in result.stderr
    assert "openalex.yaml：找不到檔案" in result.stderr
    assert "exclusion_rules.yaml：找不到檔案" in result.stderr
    assert "keyword_sources.yaml：找不到檔案" in result.stderr


def test_cli_good_config_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(ROOT / "config"))

    result = runner.invoke(cli.app, ["version"])

    assert result.exit_code == 0


# --- Story 1.7 的兩個新設定檔改壞時也不啟動 --------------------------------------------------

NEW_FILE_BREAKAGES = {
    "exclusion_rules": (
        "exclusion_rules.yaml",
        "reason: editors_comments",
        "reason: Bad-Code",
        "exclusion_rules.yaml：rules.0.reason：",
    ),
    "keyword_sources": (
        "keyword_sources.yaml",
        "active: openalex",
        "active: crossref",
        "keyword_sources.yaml：active：",
    ),
    "demo_publishers": (
        "demo_publishers.yaml",
        "name: IEEE\n",
        "name: '   '\n",
        "demo_publishers.yaml：excluded.1.name：",
    ),
}


@pytest.fixture(params=list(NEW_FILE_BREAKAGES))
def broken_new_file(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    filename, old, new, expected = NEW_FILE_BREAKAGES[request.param]
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    path = target / filename
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(target))
    monkeypatch.setenv("DATABASE_URL", APP_URL)
    return expected


def test_api_does_not_start_with_bad_new_file(
    broken_new_file: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(api_main, "build_readiness_probes", _fail_if_called)

    assert api_main.main() == 1

    problems = _problems(capsys.readouterr().out)
    assert any(problem.startswith(broken_new_file) for problem in problems), problems


def test_worker_does_not_start_with_bad_new_file(
    broken_new_file: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(worker_main, "build_worker", _fail_if_called)

    assert worker_main.main() == 1

    problems = _problems(capsys.readouterr().out)
    assert any(problem.startswith(broken_new_file) for problem in problems), problems


@pytest.mark.parametrize("args", [["version"], ["reparse"]])
def test_cli_does_not_run_with_bad_new_file(
    broken_new_file: str, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setattr(cli, "build_database", _fail_if_called)
    monkeypatch.setattr(cli, "build_object_store", _fail_if_called)
    monkeypatch.setattr(cli, "reparse_papers", _fail_if_called)

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 1
    assert f"- {broken_new_file}" in result.stderr
    assert result.stdout == ""
