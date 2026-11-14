"""領域設定（`config/journals.yaml`、`config/openalex.yaml`）的讀取與驗證（AD-18）。

真的 `config/` 要通過、正好 14 本；每種錯誤的訊息都含檔名與欄位路徑。
"""

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from radar.config import ConfigError, config_dir, load_domain_config

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"

# Story 1.6 的 14 本期刊：spikes/oq-02/output/results.json 各筆的 source.issn_l。
EXPECTED_ISSN_L = {
    "misq": "0276-7783",
    "isr": "1047-7047",
    "jmis": "0742-1222",
    "jais": "1536-9323",
    "ejis": "0960-085X",
    "isj": "1350-1917",
    "jit": "0268-3962",
    "jsis": "0963-8687",
    "dss": "0167-9236",
    "im": "0378-7206",
    "io": "1471-7727",
    "pmj": "1938-9507",
    "tem": "0018-9391",
    "database": "0095-0033",
}


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    """真的 config/ 複製一份到暫存資料夾，測試改它。"""
    target = tmp_path / "config"
    shutil.copytree(CONFIG, target)
    return target


def _load(directory: Path) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        load_domain_config({"RADAR_CONFIG_DIR": str(directory)})
    return caught.value.problems


def _replace(path: Path, old: str, new: str, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, count), encoding="utf-8")


def test_real_config_passes_with_the_14_journals() -> None:
    config = load_domain_config({"RADAR_CONFIG_DIR": str(CONFIG)})

    journals = config.journals
    assert journals.start_year == 2016
    assert len(journals.journals) == 14
    assert {journal.key: journal.issn_l for journal in journals.journals} == EXPECTED_ISSN_L
    assert all(journal.name.strip() for journal in journals.journals)

    openalex = config.openalex
    assert openalex.base_url == "https://api.openalex.org"
    assert openalex.per_page == 100
    assert openalex.quota.daily_budget_usd == Decimal("1.0")
    assert openalex.quota.default_request_cost_usd == Decimal("0.0001")
    assert isinstance(openalex.quota.stop_at_ratio, Decimal)
    assert openalex.quota.stop_at_usd == Decimal("0.8")


def test_config_dir_defaults_to_config_in_working_directory() -> None:
    assert config_dir({}) == Path("config")
    assert config_dir({"RADAR_CONFIG_DIR": ""}) == Path("config")
    assert config_dir({"RADAR_CONFIG_DIR": "/x/y"}) == Path("/x/y")


def test_models_are_frozen() -> None:
    config = load_domain_config({"RADAR_CONFIG_DIR": str(CONFIG)})

    with pytest.raises(ValueError, match="frozen"):
        config.openalex.per_page = 5


def test_missing_files_each_get_a_problem(tmp_path: Path) -> None:
    problems = _load(tmp_path)

    assert len(problems) == 2
    assert problems[0].startswith("journals.yaml：找不到檔案")
    assert problems[1].startswith("openalex.yaml：找不到檔案")


def test_invalid_yaml(config_copy: Path) -> None:
    (config_copy / "openalex.yaml").write_text("base_url: [unclosed\n", encoding="utf-8")

    problems = _load(config_copy)

    assert len(problems) == 1
    assert problems[0].startswith("openalex.yaml：不是合法的 YAML")


def test_top_level_must_be_a_mapping(config_copy: Path) -> None:
    (config_copy / "journals.yaml").write_text("- a\n- b\n", encoding="utf-8")

    assert _load(config_copy) == ["journals.yaml：最上層要是「鍵: 值」的對照（mapping）"]


def test_wrong_issn_l_names_file_and_field_path(config_copy: Path) -> None:
    # 第 3 本（index 2）的 issn_l 改壞。
    _replace(config_copy / "journals.yaml", "issn_l: 0742-1222", "issn_l: abc")

    problems = _load(config_copy)

    assert len(problems) == 1
    assert problems[0].startswith("journals.yaml：journals.2.issn_l：")
    assert "ISSN-L" in problems[0]


def test_duplicate_mapping_key_is_an_error(config_copy: Path) -> None:
    _replace(
        config_copy / "journals.yaml", "    name: MIS Quarterly\n", "    name: A\n    name: B\n"
    )

    problems = _load(config_copy)

    assert len(problems) == 1
    assert problems[0].startswith("journals.yaml：journals.0.name（第 ")
    assert "寫了兩次" in problems[0]


def test_duplicate_top_level_key_is_an_error(config_copy: Path) -> None:
    path = config_copy / "openalex.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "per_page: 50\n", encoding="utf-8")

    problems = _load(config_copy)

    assert len(problems) == 1
    assert problems[0].startswith("openalex.yaml：per_page（第 ")


def test_duplicate_journal_key(config_copy: Path) -> None:
    _replace(config_copy / "journals.yaml", "key: isr", "key: misq")

    problems = _load(config_copy)

    assert len(problems) == 1
    assert problems[0].startswith("journals.yaml：journals：")
    assert "journals.1.key 和 journals.0.key 重複" in problems[0]


def test_duplicate_issn_l_even_when_written_differently(config_copy: Path) -> None:
    # 寫法不同（沒有連字號、ISSN 前綴）但正規化後一樣，也算重複。
    _replace(config_copy / "journals.yaml", "issn_l: 1047-7047", "issn_l: ISSN 02767783")

    problems = _load(config_copy)

    assert len(problems) == 1
    assert "journals.1.issn_l 和 journals.0.issn_l 重複" in problems[0]


def test_extra_field_is_rejected(config_copy: Path) -> None:
    _replace(config_copy / "openalex.yaml", "per_page: 100", "per_page: 100\nsearch: true")

    problems = _load(config_copy)

    assert len(problems) == 1
    assert problems[0].startswith("openalex.yaml：search：")


def test_extra_field_inside_a_journal(config_copy: Path) -> None:
    _replace(
        config_copy / "journals.yaml", "    name: MIS Quarterly\n", "    name: M\n    doi: x\n"
    )

    problems = _load(config_copy)

    assert len(problems) == 1
    assert problems[0].startswith("journals.yaml：journals.0.doi：")


@pytest.mark.parametrize("key", ["MISQ", "1misq", "mis-q", "_misq", ""])
def test_journal_key_format(config_copy: Path, key: str) -> None:
    _replace(config_copy / "journals.yaml", "key: misq", f"key: '{key}'")

    problems = _load(config_copy)

    assert len(problems) == 1
    assert problems[0].startswith("journals.yaml：journals.0.key：")


def test_base_url_must_be_https(config_copy: Path) -> None:
    _replace(config_copy / "openalex.yaml", "https://api.openalex.org", "http://api.openalex.org")

    problems = _load(config_copy)

    assert len(problems) == 1
    assert problems[0].startswith("openalex.yaml：base_url：")
    assert "https://" in problems[0]


@pytest.mark.parametrize("ratio", ["0", "1.5", "-0.1"])
def test_stop_at_ratio_out_of_range(config_copy: Path, ratio: str) -> None:
    _replace(config_copy / "openalex.yaml", "stop_at_ratio: 0.8", f"stop_at_ratio: {ratio}")

    problems = _load(config_copy)

    assert len(problems) == 1
    assert problems[0].startswith("openalex.yaml：quota.stop_at_ratio：")


def test_problems_from_both_files_are_reported_together(config_copy: Path) -> None:
    _replace(config_copy / "journals.yaml", "start_year: 2016", "start_year: soon")
    _replace(config_copy / "openalex.yaml", "attempts: 5", "attempts: 0")

    problems = _load(config_copy)

    assert [problem.split("：")[:2] for problem in problems] == [
        ["journals.yaml", "start_year"],
        ["openalex.yaml", "retry.attempts"],
    ]


def test_wait_max_not_below_wait_initial(config_copy: Path) -> None:
    _replace(config_copy / "openalex.yaml", "wait_max_seconds: 30", "wait_max_seconds: 0.5")

    problems = _load(config_copy)

    assert len(problems) == 1
    assert problems[0].startswith("openalex.yaml：retry：")


def test_new_journal_only_needs_a_yaml_entry(config_copy: Path) -> None:
    # 「設定即資料」：新增期刊只改 journals.yaml。
    path = config_copy / "journals.yaml"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "  - key: new_journal\n    name: New Journal\n    issn_l: '12345679'\n",
        encoding="utf-8",
    )

    config = load_domain_config({"RADAR_CONFIG_DIR": str(config_copy)})

    assert config.journals.journals[-1].key == "new_journal"
    assert config.journals.journals[-1].issn_l == "1234-5679"


@pytest.mark.parametrize(("value", "ok"), [("10", True), ("10.5", False), ("11", False)])
def test_requests_per_second_capped_at_openalex_limit(
    config_copy: Path, value: str, ok: bool
) -> None:
    # OpenAlex 的上限是每秒 10 個請求。
    _replace(
        config_copy / "openalex.yaml", "requests_per_second: 5", f"requests_per_second: {value}"
    )

    if ok:
        config = load_domain_config({"RADAR_CONFIG_DIR": str(config_copy)})
        assert config.openalex.requests_per_second == 10
    else:
        problems = _load(config_copy)
        assert len(problems) == 1
        assert problems[0].startswith("openalex.yaml：requests_per_second：")


def test_cyclic_alias_is_reported_not_crashed(config_copy: Path) -> None:
    (config_copy / "journals.yaml").write_text(
        "start_year: 2016\njournals: &a [*a]\n", encoding="utf-8"
    )

    problems = _load(config_copy)

    assert len(problems) == 1
    assert problems[0].startswith("journals.yaml：")
    assert "循環參照" in problems[0]
