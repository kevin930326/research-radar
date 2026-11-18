"""`config/zh_summary.yaml` 與 `load_zh_summary_rules`：真的設定讀得過；每種錯誤都在讀取時擋下，
訊息是「檔名：欄位路徑：原因」、每條一行、不含設定值。"""

from pathlib import Path

import pytest

from radar.config import (
    ZH_SUMMARY_ACRONYMS_FILE,
    ZH_SUMMARY_FILE,
    ConfigError,
    load_zh_summary_rules,
)
from radar.domain.validators.zh_summary import (
    MAX_ACRONYMS,
    MAX_FULL_NAME_WORDS,
    MAX_FULL_NAMES_PER_ACRONYM,
    ZhSummaryRules,
)
from tests.llm_support import ROOT

REAL = (ROOT / "config" / ZH_SUMMARY_FILE).read_text(encoding="utf-8")
# PR 2：`load_zh_summary_rules` 也讀允許清單；這裡的測試只改門檻檔，允許清單用真的那一份。
REAL_ACRONYMS = (ROOT / "config" / ZH_SUMMARY_ACRONYMS_FILE).read_text(encoding="utf-8")


def _load(tmp_path: Path, text: str) -> ZhSummaryRules:
    (tmp_path / ZH_SUMMARY_FILE).write_text(text, encoding="utf-8")
    (tmp_path / ZH_SUMMARY_ACRONYMS_FILE).write_text(REAL_ACRONYMS, encoding="utf-8")
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

    # PR 2：真的設定另外帶允許清單（`tests/unit/test_zh_summary_acronyms.py` 核對內容）。
    assert rules == ZhSummaryRules(
        max_chars_per_field=150,
        translation_min_abstract_sentences=7,
        min_cjk_percent=50,
        acronyms=rules.acronyms,
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


# --- 允許清單的上下限（第一輪修正，Codex X2、審查 A6、B6）---------------------------------------


def _write_list(tmp_path: Path, entries: list[tuple[str, list[str]]]) -> None:
    (tmp_path / ZH_SUMMARY_FILE).write_text(REAL, encoding="utf-8")
    lines = ["version: 1", "acronyms:" if entries else "acronyms: []"]
    for acronym, names in entries:
        lines.append(f"  - acronym: {acronym}")
        lines.append("    full_names:")
        lines.extend(f"      - {name}" for name in names)
    (tmp_path / ZH_SUMMARY_ACRONYMS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


SAME_NAME = "（不分大小寫，空白、連字號、連在一起都算同一種寫法）"


def _tag(number: int) -> str:
    """每個號碼一個不重複、只有英文字母的詞（`aa`、`ab`…）。"""
    letters = "abcdefghijklmnopqrstuvwxyz"
    return letters[number // 26] + letters[number % 26]


def _words(count: int) -> str:
    return " ".join(f"word{chr(ord('a') + index)}" for index in range(count))


def test_the_real_list_is_within_the_limits() -> None:
    rules = load_zh_summary_rules({"RADAR_CONFIG_DIR": str(ROOT / "config")})
    words = [
        len(name.replace("-", " ").split()) for entry in rules.acronyms for name in entry.full_names
    ]

    # 現有最長的全名是 MULTIMOORA 的 11 個詞，上限 12。
    assert max(words) == 11
    assert max(words) <= MAX_FULL_NAME_WORDS == 12


@pytest.mark.parametrize(
    "entries",
    [
        [("ZZ", [_words(MAX_FULL_NAME_WORDS)])],
        [
            (
                "ZZ",
                [f"{_words(2)} name{chr(ord('a') + i)}" for i in range(MAX_FULL_NAMES_PER_ACRONYM)],
            )
        ],
        [(f"Z{i}", [f"alpha name{_tag(i)}"]) for i in range(MAX_ACRONYMS)],
        [("EEG", ["electroencephalography", "electroencephalogram"])],
        [("ZZ", ["abcdefghij"])],
        [("ZZ", ["a b"])],
        # 同一筆裡兩個不同的全名可以（SDT 兩種意思）。
        [("SDT", ["self determination theory", "signal detection theory"])],
    ],
)
def test_lists_at_the_limits_load(tmp_path: Path, entries: list[tuple[str, list[str]]]) -> None:
    _write_list(tmp_path, entries)

    rules = load_zh_summary_rules({"RADAR_CONFIG_DIR": str(tmp_path)})

    assert [entry.acronym for entry in rules.acronyms] == [acronym for acronym, _ in entries]


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        (
            [("ZZ", [_words(MAX_FULL_NAME_WORDS + 1)])],
            "acronyms.0.full_names：full_names 的每一項最多 12 個詞",
        ),
        (
            [("ZZ", [_words(MAX_FULL_NAME_WORDS).replace(" ", "-", 3) + " wordz"])],
            "acronyms.0.full_names：full_names 的每一項最多 12 個詞",
        ),
        (
            [
                (
                    "ZZ",
                    [
                        f"{_words(2)} name{chr(ord('a') + i)}"
                        for i in range(MAX_FULL_NAMES_PER_ACRONYM + 1)
                    ],
                )
            ],
            "acronyms.0.full_names：full_names 最多 12 個寫法",
        ),
        (
            [(f"Z{i}", [f"alpha name{_tag(i)}"]) for i in range(MAX_ACRONYMS + 1)],
            "acronyms：acronyms 最多 200 筆",
        ),
        (
            [("RBV", ["view"])],
            "acronyms.0.full_names：full_names 的每一項至少要兩個詞；只有一個詞時至少要 10 個字母",
        ),
        (
            [("ZZ", ["abcdefghi"])],
            "acronyms.0.full_names：full_names 的每一項至少要兩個詞；只有一個詞時至少要 10 個字母",
        ),
        (
            [
                ("SEM", ["structural equation modeling"]),
                ("SEMX", ["Structural-Equation modeling"]),
            ],
            "acronyms：SEM 與 SEMX 登記了同一個全名" + SAME_NAME,
        ),
        (
            [
                ("MCDM", ["multi criteria decision making"]),
                ("MCDA", ["multicriteria decision making"]),
            ],
            "acronyms：MCDM 與 MCDA 登記了同一個全名" + SAME_NAME,
        ),
    ],
)
def test_lists_beyond_the_limits_are_rejected(
    tmp_path: Path, entries: list[tuple[str, list[str]]], expected: str
) -> None:
    _write_list(tmp_path, entries)

    with pytest.raises(ConfigError) as caught:
        load_zh_summary_rules({"RADAR_CONFIG_DIR": str(tmp_path)})

    assert caught.value.problems == [f"{ZH_SUMMARY_ACRONYMS_FILE}：{expected}"]
