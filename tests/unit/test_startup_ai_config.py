"""AI 設定錯了就不啟動（Story 2.9）：`check_ai_config` 一次列出全部問題；worker 的 `main()` 回 1、
CLI 每個指令退出碼 1。

`RADAR_CONFIG_DIR` 指到改壞的副本（repo 的 config/ 複製一份再改）；prompt 資料夾用 repo 的或
tmp 的空資料夾。不讀秘密、不連線。
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import radar.entrypoints.cli as cli
import radar.entrypoints.worker.main as worker_main
from radar.config import ConfigError
from radar.entrypoints.startup import check_ai_config

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "prompts"
APP_URL = "postgresql://radar_app@db:5432/radar"

runner = CliRunner()


class _ShouldNotBuildError(Exception):
    """設定錯了卻走到組裝這一步。"""


def _fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
    raise _ShouldNotBuildError


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(target))
    monkeypatch.setenv("DATABASE_URL", APP_URL)
    return target


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, (path.name, old)
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_repo_config_passes(config: Path) -> None:
    check_ai_config(PROMPTS)


def test_every_broken_file_is_listed_together(config: Path, tmp_path: Path) -> None:
    _replace(config / "budget.yaml", 'monthly_breaker_usd: "50"', "monthly_breaker_usd: 50")
    _replace(
        config / "llm_batches.yaml", "max_requests_per_batch: 500", "max_requests_per_batch: 0"
    )
    (config / "zh_summary.yaml").unlink()
    _replace(config / "models.yaml", 'batch_multiplier: "0.5"', "batch_multiplier: 0.5")
    empty_prompts = tmp_path / "prompts"
    empty_prompts.mkdir()

    with pytest.raises(ConfigError) as caught:
        check_ai_config(empty_prompts)

    problems = caught.value.problems
    assert any(p.startswith("budget.yaml：monthly_breaker_usd：") for p in problems), problems
    assert any(p.startswith("llm_batches.yaml：max_requests_per_batch：") for p in problems)
    assert any(p.startswith("zh_summary.yaml：找不到檔案") for p in problems)
    assert any(p.startswith("models.yaml：batch_multiplier：") for p in problems)
    assert any(p.startswith("prompt 登記：") for p in problems)


def test_taxonomy_and_context_values_hash_registry_is_checked(config: Path) -> None:
    # 升了版卻沒登記雜湊。
    _replace(config / "taxonomy.yaml", "version: 2\n", "version: 99\n")
    _replace(config / "context_values.yaml", "version: 1\n", "version: 99\n")

    with pytest.raises(ConfigError) as caught:
        check_ai_config(PROMPTS)

    problems = caught.value.problems
    assert any(p.startswith("taxonomy.yaml：") for p in problems), problems
    assert any(p.startswith("context_values.yaml：") for p in problems), problems


def test_missing_prompt_folder_is_reported(config: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        check_ai_config(tmp_path / "nowhere")

    assert caught.value.problems == [f"prompt 登記：{tmp_path / 'nowhere'}：prompt 資料夾不存在"]


def _problems(out: str) -> list[str]:
    for line in out.splitlines():
        if line.startswith("{"):
            record = json.loads(line)
            if record.get("event") == "設定錯誤，無法啟動":
                return [str(problem) for problem in record["problems"]]
    return []


def test_worker_does_not_start_with_broken_ai_config(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (config / "llm_batches.yaml").unlink()
    monkeypatch.setattr(worker_main, "build_worker", _fail_if_called)

    assert worker_main.main() == 1

    problems = _problems(capsys.readouterr().out)
    assert any(p.startswith("llm_batches.yaml：找不到檔案") for p in problems), problems
    assert "radar_app" not in str(problems)


@pytest.mark.parametrize("args", [["version"], ["enqueue", "sweep"], ["costs"]])
def test_cli_command_does_not_run_with_broken_ai_config(
    config: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    _replace(config / "llm_batches.yaml", "month_start_hold_hours: 8", "month_start_hold_hours: 99")
    monkeypatch.setattr(cli, "build_job_enqueuer", _fail_if_called)
    monkeypatch.setattr(cli, "build_database", _fail_if_called)

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 1
    assert "AI 設定錯誤，沒有執行" in result.stderr
    assert "- llm_batches.yaml：month_start_hold_hours：" in result.stderr
    assert result.stdout == ""


def test_cli_runs_with_repo_config(config: Path) -> None:
    result = runner.invoke(cli.app, ["version"])

    assert result.exit_code == 0, result.output
