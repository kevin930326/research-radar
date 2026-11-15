"""`config/zh_summary.yaml` 與 `load_zh_summary_rules`：真的設定讀得過；每種錯誤都在讀取時擋下，
訊息是「檔名：欄位路徑：原因」、每條一行、不含設定值。"""

from pathlib import Path

import pytest

from radar.config import ZH_SUMMARY_FILE, ConfigError, load_zh_summary_rules
from radar.domain.validators.zh_summary import ZhSummaryRules
from tests.llm_support import ROOT

REAL = (ROOT / "config" / ZH_SUMMARY_FILE).read_text(encoding="utf-8")


def _load(tmp_path: Path, text: str) -> ZhSummaryRules:
    (tmp_path / ZH_SUMMARY_FILE).write_text(text, encoding="utf-8")
    return load_zh_summary_rules({"RADAR_CONFIG_DIR": str(tmp_path)})


def _problems(tmp_path: Path, text: str) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, text)
    problems = caught.value.problems
    assert problems
    assert all("\n" not in problem for problem in problems)
    assert all(problem.startswith(f"{ZH_SUMMARY_FILE}：") for problem in problems)
    return problems


def test_real_config_has_the_prd_defaults() -> None:
    rules = load_zh_summary_rules({"RADAR_CONFIG_DIR": str(ROOT / "config")})

    assert rules == ZhSummaryRules(
        max_chars_per_field=80, translation_min_abstract_sentences=5, min_cjk_percent=50
    )


def test_real_config_documents_its_sources() -> None:
    assert "FR-14" in REAL
    assert "ADR-0032" in REAL


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_zh_summary_rules({"RADAR_CONFIG_DIR": str(tmp_path)})

    assert caught.value.problems[0].startswith(f"{ZH_SUMMARY_FILE}：找不到檔案")


def test_missing_key_is_rejected(tmp_path: Path) -> None:
    text = REAL.replace("min_cjk_percent: 50\n", "")

    problems = _problems(tmp_path, text)

    assert problems == [f"{ZH_SUMMARY_FILE}：min_cjk_percent：Field required"]


def test_extra_key_is_rejected(tmp_path: Path) -> None:
    problems = _problems(tmp_path, REAL + "max_sentences: 3\n")

    assert len(problems) == 1
    assert problems[0].startswith(f"{ZH_SUMMARY_FILE}：max_sentences：")


def test_duplicate_key_is_rejected(tmp_path: Path) -> None:
    problems = _problems(tmp_path, REAL + "max_chars_per_field: 90\n")

    assert "寫了兩次" in problems[0]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_chars_per_field", "0"),
        ("max_chars_per_field", "-1"),
        ("max_chars_per_field", "80.0"),
        ("max_chars_per_field", '"80"'),
        ("max_chars_per_field", "true"),
        ("translation_min_abstract_sentences", "0"),
        ("translation_min_abstract_sentences", "1"),
        ("translation_min_abstract_sentences", "5.5"),
        ("min_cjk_percent", "0"),
        ("min_cjk_percent", "101"),
        ("min_cjk_percent", "-50"),
        ("min_cjk_percent", "0.5"),
        ("min_cjk_percent", "null"),
    ],
)
def test_bad_values_are_rejected(tmp_path: Path, key: str, value: str) -> None:
    lines = [
        f"{key}: {value}" if line.startswith(f"{key}:") else line for line in REAL.splitlines()
    ]

    problems = _problems(tmp_path, "\n".join(lines) + "\n")

    assert len(problems) == 1
    assert problems[0].startswith(f"{ZH_SUMMARY_FILE}：{key}：")
    assert value not in problems[0].removeprefix(f"{ZH_SUMMARY_FILE}：{key}：")


def test_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    problems = _problems(tmp_path, "- 80\n")

    assert "mapping" in problems[0]
