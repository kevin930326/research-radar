"""`config/budget.yaml` 與 `load_budget_config`：真的設定讀得過；每種錯誤都在讀取時擋下，
訊息是「檔名：欄位路徑：原因」、每條一行、不含設定值。"""

from decimal import Decimal
from pathlib import Path

import pytest

from radar.config import BUDGET_FILE, ConfigError, load_budget_config
from radar.domain.ai_outputs import LlmTask
from radar.domain.budget import BudgetConfig
from tests.llm_support import ROOT

REAL = (ROOT / "config" / BUDGET_FILE).read_text(encoding="utf-8")


def _load(tmp_path: Path, text: str) -> BudgetConfig:
    (tmp_path / BUDGET_FILE).write_text(text, encoding="utf-8")
    return load_budget_config({"RADAR_CONFIG_DIR": str(tmp_path)})


def _problems(tmp_path: Path, text: str) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, text)
    problems = caught.value.problems
    assert problems
    assert all("\n" not in problem for problem in problems)
    assert all(problem.startswith(f"{BUDGET_FILE}：") for problem in problems)
    return problems


def test_real_config_matches_nfr_3() -> None:
    config = load_budget_config({"RADAR_CONFIG_DIR": str(ROOT / "config")})

    assert config.workspace_limit_usd == Decimal("70")
    assert config.monthly_breaker_usd == Decimal("50")
    assert set(config.task_budgets_usd) == set(LlmTask)
    # 文件沒有寫各功能的數字：全部留 null（只受熔斷線管），不猜。
    assert all(config.task_budget(task) is None for task in LlmTask)


def test_real_config_documents_sources_and_null_meaning() -> None:
    assert "NFR-3" in REAL
    assert "ADR-0009" in REAL
    assert "null＝這個工作沒有個別上限，只受熔斷線管" in REAL
    assert "數字待 Kevin 定" in REAL


def test_task_budget_string_is_read_as_decimal(tmp_path: Path) -> None:
    config = _load(tmp_path, REAL.replace("  zh_summary: null", '  zh_summary: "1.5"'))

    assert config.task_budget(LlmTask.ZH_SUMMARY) == Decimal("1.5")
    assert config.task_budget(LlmTask.CLASSIFY) is None


def test_float_breaker_is_rejected(tmp_path: Path) -> None:
    problems = _problems(
        tmp_path, REAL.replace('monthly_breaker_usd: "50"', "monthly_breaker_usd: 50.0")
    )

    assert len(problems) == 1
    assert problems[0].startswith(f"{BUDGET_FILE}：monthly_breaker_usd：")
    assert "字串" in problems[0]


def test_integer_limit_is_rejected(tmp_path: Path) -> None:
    problems = _problems(
        tmp_path, REAL.replace('workspace_limit_usd: "70"', "workspace_limit_usd: 70")
    )

    assert problems[0].startswith(f"{BUDGET_FILE}：workspace_limit_usd：")


def test_float_task_budget_is_rejected(tmp_path: Path) -> None:
    problems = _problems(tmp_path, REAL.replace("  classify: null", "  classify: 1.0"))

    assert len(problems) == 1
    assert problems[0].startswith(f"{BUDGET_FILE}：task_budgets_usd.classify：")


@pytest.mark.parametrize("bad", ['"-1"', '"abc"', '"NaN"', '"Infinity"', '""'])
def test_bad_amount_strings_are_rejected(tmp_path: Path, bad: str) -> None:
    problems = _problems(tmp_path, REAL.replace("  zh_summary: null", f"  zh_summary: {bad}"))

    assert len(problems) == 1
    assert problems[0].startswith(f"{BUDGET_FILE}：task_budgets_usd.zh_summary：")
    # 設定值不出現在訊息裡。
    assert "abc" not in problems[0]


def test_negative_breaker_is_rejected(tmp_path: Path) -> None:
    problems = _problems(
        tmp_path, REAL.replace('monthly_breaker_usd: "50"', 'monthly_breaker_usd: "-50"')
    )

    assert problems[0].startswith(f"{BUDGET_FILE}：monthly_breaker_usd：")


def test_missing_task_is_rejected(tmp_path: Path) -> None:
    problems = _problems(tmp_path, REAL.replace("  eval_judge: null\n", ""))

    assert problems == [
        f"{BUDGET_FILE}：task_budgets_usd："
        "要列出全部六個工作（沒有個別上限寫 null），缺：eval_judge"
    ]


def test_extra_task_is_rejected(tmp_path: Path) -> None:
    problems = _problems(tmp_path, REAL + "  embed: null\n")

    assert len(problems) == 1
    assert problems[0].startswith(f"{BUDGET_FILE}：task_budgets_usd.embed")


def test_missing_top_level_key_is_rejected(tmp_path: Path) -> None:
    problems = _problems(tmp_path, REAL.replace('workspace_limit_usd: "70"\n', ""))

    assert problems == [f"{BUDGET_FILE}：workspace_limit_usd：Field required"]


def test_extra_top_level_key_is_rejected(tmp_path: Path) -> None:
    problems = _problems(tmp_path, REAL + 'daily_breaker_usd: "5"\n')

    assert len(problems) == 1
    assert problems[0].startswith(f"{BUDGET_FILE}：daily_breaker_usd：")


@pytest.mark.parametrize("breaker", ['"70"', '"80"'])
def test_breaker_not_below_workspace_limit_is_rejected(tmp_path: Path, breaker: str) -> None:
    problems = _problems(
        tmp_path, REAL.replace('monthly_breaker_usd: "50"', f"monthly_breaker_usd: {breaker}")
    )

    assert problems == [
        f"{BUDGET_FILE}：（最上層）：monthly_breaker_usd 要低於 workspace_limit_usd"
    ]


def test_duplicate_key_is_rejected(tmp_path: Path) -> None:
    problems = _problems(tmp_path, REAL + 'monthly_breaker_usd: "40"\n')

    assert any("monthly_breaker_usd" in problem and "兩次" in problem for problem in problems)


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_budget_config({"RADAR_CONFIG_DIR": str(tmp_path)})

    assert caught.value.problems[0].startswith(f"{BUDGET_FILE}：找不到檔案")
