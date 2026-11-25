"""`config/trend.yaml`、`load_trend_config` 與 `build_trend_params`（Story 3.15、ADR-0062）：
真的設定讀得過、值是 ADR-0011 寫的預設值；每種錯誤都在讀取時擋下（「檔名：欄位路徑：原因」）；
`params` 的追蹤起始年與時區來自 `journals.yaml` 與資料截止日的時區，不在 trend.yaml 另寫一份。"""

from pathlib import Path

import pytest

from radar.config import TREND_FILE, ConfigError, load_domain_config, load_trend_config
from radar.domain.contracts import TrendParams, build_trend_params
from radar.domain.trend_config import TrendConfig
from tests.llm_support import ROOT

REAL_ENV = {"RADAR_CONFIG_DIR": str(ROOT / "config")}
GOOD = """current_year_cutoff: "07-01"
growth_window_years: 3
baseline_years: 3
low_coverage_threshold: 0.5
"""


def _load(tmp_path: Path, text: str) -> TrendConfig:
    (tmp_path / TREND_FILE).write_text(text, encoding="utf-8")
    return load_trend_config({"RADAR_CONFIG_DIR": str(tmp_path)})


def _problems(tmp_path: Path, text: str) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, text)
    problems = caught.value.problems
    assert problems
    assert all(problem.startswith(f"{TREND_FILE}：") for problem in problems)
    return problems


def test_real_config_has_the_adr_0011_values() -> None:
    config = load_trend_config(REAL_ENV)

    assert config == TrendConfig(
        current_year_cutoff="07-01",
        growth_window_years=3,
        baseline_years=3,
        low_coverage_threshold=0.5,
    )


def test_params_take_start_year_and_timezone_from_elsewhere() -> None:
    params = build_trend_params(load_trend_config(REAL_ENV), load_domain_config(REAL_ENV).journals)

    assert params == TrendParams(
        timezone="Asia/Taipei",
        tracking_start_year=2016,
        current_year_cutoff="07-01",
        growth_window_years=3,
        baseline_years=3,
        low_coverage_threshold=0.5,
    )


@pytest.mark.parametrize(
    ("text", "where"),
    [
        (GOOD.replace('"07-01"', '"02-30"'), "current_year_cutoff"),
        (GOOD.replace('"07-01"', '"13-01"'), "current_year_cutoff"),
        (GOOD.replace('"07-01"', '"7-1"'), "current_year_cutoff"),
        (GOOD.replace("growth_window_years: 3", "growth_window_years: 0"), "growth_window_years"),
        (GOOD.replace("baseline_years: 3", 'baseline_years: "3"'), "baseline_years"),
        (GOOD.replace("0.5", "1.5"), "low_coverage_threshold"),
        (GOOD.replace("0.5", "-0.1"), "low_coverage_threshold"),
        (GOOD + "extra: 1\n", "extra"),
    ],
    ids=[
        "2 月 30 日",
        "13 月",
        "沒補零",
        "每組 0 年",
        "年數寫成字串",
        "門檻大於 1",
        "門檻負數",
        "多的鍵",
    ],
)
def test_bad_values_are_rejected_on_load(tmp_path: Path, text: str, where: str) -> None:
    problems = _problems(tmp_path, text)

    assert any(problem.startswith(f"{TREND_FILE}：{where}：") for problem in problems)


def test_leap_day_cutoff_is_allowed(tmp_path: Path) -> None:
    assert _load(tmp_path, GOOD.replace('"07-01"', '"02-29"')).current_year_cutoff == "02-29"


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_trend_config({"RADAR_CONFIG_DIR": str(tmp_path)})

    assert caught.value.problems[0].startswith(f"{TREND_FILE}：找不到檔案")
