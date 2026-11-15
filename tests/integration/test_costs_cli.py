"""`radar costs` 對真的 PostgreSQL（`CliRunner`＋環境變數；spec 2.3）。

假帳本：zh_summary 1.23456、classify 0.5（本月，台北時間），另有上個月與評估、Batch 的帳；輸出和預期
的每一行完全相同。另有：熔斷時、設定錯、連不上資料庫。指令只讀資料庫（帳本列數不變）。不連網路；
環境變數只用測試值。
"""

import logging
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

import radar.entrypoints.cli as cli
from radar.domain.ai_outputs import LlmTask
from radar.domain.budget import BUDGET_TIMEZONE
from radar.repo.db import Database
from tests.database import ROOT, TestDatabase, migrate
from tests.llm_support import create_paper, insert_ledger, table_counts

TAIPEI = BUDGET_TIMEZONE
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=TAIPEI)

runner = CliRunner()


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def paper_id(db: TestDatabase) -> uuid.UUID:
    return create_paper(Database(db.app_url, connect_timeout=5, pool_size=1))


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    custom = tmp_path / "config"
    shutil.copytree(ROOT / "config", custom)
    return custom


@pytest.fixture
def env(db: TestDatabase, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """指令要用的環境變數與固定的時鐘（台北 2026-11-15 12:00）。"""
    monkeypatch.setenv("DATABASE_URL", db.app_url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    return config_dir


def _costs() -> Result:
    """執行 `radar costs`；結束後把 root logger 的 handler 換回原本的（見 test_coverage_cli）。"""
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        return runner.invoke(cli.app, ["costs"])
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)


def _stdout_lines(output: str) -> list[str]:
    # 日誌是 JSON（以 { 開頭）；報表本身不是。
    return [line for line in output.splitlines() if not line.startswith("{")]


def test_costs_prints_this_month_by_task(db: TestDatabase, env: Path, paper_id: uuid.UUID) -> None:
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "1.23456", NOW - timedelta(days=2))
    insert_ledger(db, paper_id, LlmTask.CLASSIFY, "0.5", datetime(2026, 11, 1, tzinfo=TAIPEI))
    # 上個月（台北 10/31 23:59:59）與下個月的帳不算。
    october_end = datetime(2026, 10, 31, 23, 59, 59, tzinfo=TAIPEI)
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "9", october_end)
    insert_ledger(db, paper_id, LlmTask.CLASSIFY, "9", datetime(2026, 12, 1, tzinfo=TAIPEI))
    before = table_counts(db)

    result = _costs()

    assert result.exit_code == 0, result.output
    assert _stdout_lines(result.stdout) == [
        "2026-11 費用（台北時間，美元）",
        "",
        "工作             費用  功能預算  可呼叫",
        "zh_summary     1.2346      未設  是",
        "classify       0.5000      未設  是",
        "extract_model  0.0000      未設  是",
        "merge_judge    0.0000      未設  是",
        "qa_answer      0.0000      未設  是",
        "eval_judge     0.0000      未設  是",
        "合計           1.7346",
        "",
        "熔斷線：50.0000",
        "離熔斷線：48.2654",
        "只檢索：否",
    ]
    assert result.stderr == ""
    assert table_counts(db) == before


def test_costs_when_breaker_is_tripped(db: TestDatabase, env: Path, paper_id: uuid.UUID) -> None:
    insert_ledger(db, paper_id, LlmTask.EVAL_JUDGE, "20", NOW, eval_run_id=uuid.uuid4())
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "30.00005", NOW, is_batch=True)

    result = _costs()

    assert result.exit_code == 0, result.output
    lines = _stdout_lines(result.stdout)
    assert "zh_summary     30.0001      未設  否" in lines
    assert "eval_judge     20.0000      未設  否" in lines
    assert "合計           50.0001" in lines
    assert lines[-3:] == ["熔斷線：50.0000", "離熔斷線：0.0000", "只檢索：是"]


def test_costs_shows_task_budgets(db: TestDatabase, env: Path, paper_id: uuid.UUID) -> None:
    budget = env / "budget.yaml"
    budget.write_text(
        budget.read_text(encoding="utf-8").replace("  qa_answer: null", '  qa_answer: "0.25"'),
        encoding="utf-8",
    )
    insert_ledger(db, paper_id, LlmTask.QA_ANSWER, "0.25", NOW)

    result = _costs()

    assert result.exit_code == 0, result.output
    lines = _stdout_lines(result.stdout)
    assert "qa_answer      0.2500    0.2500  否" in lines
    assert "zh_summary     0.0000      未設  是" in lines
    assert lines[-1] == "只檢索：是"


def test_budget_config_error_exits_1_without_touching_the_database(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = env / "budget.yaml"
    budget.write_text(
        budget.read_text(encoding="utf-8").replace(
            'monthly_breaker_usd: "50"', "monthly_breaker_usd: 50.0"
        ),
        encoding="utf-8",
    )
    built: list[object] = []
    monkeypatch.setattr(cli, "build_database", lambda *a, **k: built.append(a))

    result = _costs()

    assert result.exit_code == 1
    assert "設定錯誤，沒有讀取費用" in result.stderr
    assert "budget.yaml：monthly_breaker_usd：" in result.stderr
    assert _stdout_lines(result.stdout) == []
    assert built == []


def test_missing_budget_file_exits_1(env: Path) -> None:
    (env / "budget.yaml").unlink()

    result = _costs()

    assert result.exit_code == 1
    assert "budget.yaml：找不到檔案" in result.stderr


def test_unreachable_database_prints_only_the_error_type(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "postgresql://radar_app:secret-password@127.0.0.1:9/radar"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))

    result = _costs()

    assert result.exit_code == 1
    assert "讀取費用失敗（" in result.stderr
    assert "secret-password" not in result.output
    assert "127.0.0.1:9" not in result.output
    assert _stdout_lines(result.stdout) == []
