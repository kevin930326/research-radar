"""`config/context_values.yaml` 與 `load_context_values`（Story 2.6、ADR-0034）：

- 真的設定讀得過，四個欄位都有項目，`version` 寫在最上面；國家與產業每項都有 `terms`。
- 內容（`key`、定義、`terms`、欄位）改了卻沒升版、升了版沒登記 → `ConfigError`。
- 只改顯示名稱或順序 → 照樣讀得過、內容雜湊不變。
- 每種格式錯都在讀取時擋下，訊息是「檔名：欄位路徑：原因」、每條一行。
"""

import re
from pathlib import Path

import pytest
import yaml

from radar.config import CONTEXT_VALUES_FILE, ConfigError, load_context_values
from radar.domain import context_values_hashes
from radar.domain.context_values import (
    TERM_FIELDS,
    ContextField,
    ContextValues,
    context_values_registry_problems,
)
from radar.domain.fingerprints import context_values_content_sha256
from tests.llm_support import ROOT

REAL_PATH = ROOT / "config" / CONTEXT_VALUES_FILE
REAL = REAL_PATH.read_text(encoding="utf-8")


def _real_data() -> dict[str, object]:
    data = yaml.safe_load(REAL)
    assert isinstance(data, dict)
    return data


def _write(tmp_path: Path, data: dict[str, object] | str) -> None:
    text = data if isinstance(data, str) else yaml.safe_dump(data, allow_unicode=True)
    (tmp_path / CONTEXT_VALUES_FILE).write_text(text, encoding="utf-8")


def _load(tmp_path: Path, data: dict[str, object] | str) -> ContextValues:
    _write(tmp_path, data)
    return load_context_values({"RADAR_CONFIG_DIR": str(tmp_path)})


def _problems(tmp_path: Path, data: dict[str, object] | str) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, data)
    problems = caught.value.problems
    assert problems
    assert all("\n" not in problem for problem in problems)
    assert all(problem.startswith(f"{CONTEXT_VALUES_FILE}：") for problem in problems)
    return problems


def _items(data: dict[str, object], name: str) -> list[dict[str, object]]:
    items = data[name]
    assert isinstance(items, list)
    return items


def _real() -> ContextValues:
    return load_context_values({"RADAR_CONFIG_DIR": str(ROOT / "config")})


# --- 真的設定 -----------------------------------------------------------------------------------


def test_real_context_values_load() -> None:
    values = _real()

    assert values.version == 1
    assert values.version_label == "1"
    for field in ContextField:
        assert values.keys(field), field
        assert len(set(values.keys(field))) == len(values.keys(field))
        for item in values.items(field):
            assert (item.terms is not None) == (field in TERM_FIELDS), item.key
    assert context_values_registry_problems(values) == []


def test_real_list_covers_the_prompt_example_and_common_values() -> None:
    values = _real()

    assert {"tw", "us", "cn"} <= set(values.keys(ContextField.COUNTRY))
    assert {"survey", "experiment", "case_study"} <= set(values.keys(ContextField.RESEARCH_DESIGN))
    assert {"individual", "organization"} <= set(values.keys(ContextField.UNIT_OF_ANALYSIS))
    tw = values.item(ContextField.COUNTRY, "tw")
    assert tw is not None
    assert tw.terms is not None
    assert "Taiwan" in tw.terms
    assert values.item(ContextField.COUNTRY, "usa") is None


def test_version_is_the_first_key_of_the_real_file() -> None:
    document = yaml.compose(REAL, Loader=yaml.SafeLoader)
    assert isinstance(document, yaml.MappingNode)
    first_key = document.value[0][0]

    assert first_key.value == "version"
    assert REAL.splitlines()[0] == "version: 1"


def test_real_file_documents_its_sources_and_the_bump_rule() -> None:
    for required in (
        "FR-3",
        "FR-21",
        "ADR-0012",
        "ADR-0034",
        "草案",
        "金標準",
        "context_values_hashes.py",
        "prompts/extract_model",
    ):
        assert required in REAL, required


# --- 內容改了要升版 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    ["definition", "key", "terms", "add_item", "remove_item", "move_to_other_field"],
)
def test_content_changed_without_version_bump_is_refused(tmp_path: Path, change: str) -> None:
    data = _real_data()
    countries = _items(data, "countries")
    designs = _items(data, "research_designs")
    units = _items(data, "units_of_analysis")
    match change:
        case "definition":
            designs[0]["definition"] = "以線上或紙本問卷收集資料。"
        case "key":
            countries[0]["key"] = "taiwan"
        case "terms":
            countries[0]["terms"] = ["Taiwan", "Taiwanese", "ROC"]
        case "add_item":
            countries.append(
                {"key": "fr", "name": "法國", "definition": "法國（France）。", "terms": ["France"]}
            )
        case "remove_item":
            units.pop()
        case "move_to_other_field":
            units.append(designs.pop())

    problems = _problems(tmp_path, data)

    assert problems == [
        f"{CONTEXT_VALUES_FILE}：version：版本 1 的內容和登記的雜湊不一樣"
        "（key、定義、terms 或欄位改了就要升版，並登記新版本的雜湊）"
    ]


def test_bumped_version_without_registration_is_refused(tmp_path: Path) -> None:
    problems = _problems(tmp_path, _real_data() | {"version": 2})

    assert problems == [
        f"{CONTEXT_VALUES_FILE}：version：版本 2 沒有登記在 radar/domain/context_values_hashes.py"
        "（升版要一起登記內容雜湊）"
    ]


def test_bumped_and_registered_version_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _real_data() | {"version": 2}
    _items(data, "research_designs")[0]["definition"] = "以線上或紙本問卷收集資料。"
    changed = ContextValues.model_validate(data)
    monkeypatch.setattr(
        context_values_hashes,
        "CONTEXT_VALUES_CONTENT_HASHES",
        context_values_hashes.CONTEXT_VALUES_CONTENT_HASHES
        | {2: context_values_content_sha256(changed)},
    )

    assert _load(tmp_path, data).version == 2


def test_only_names_and_order_changed_still_loads_with_the_same_hash(tmp_path: Path) -> None:
    real = _real()
    data = _real_data()
    countries = _items(data, "countries")
    industries = _items(data, "industries")
    countries[0]["name"] = "臺灣"
    industries[1]["name"] = "零售"
    countries.reverse()
    industries.insert(0, industries.pop())

    loaded = _load(tmp_path, data)

    assert context_values_content_sha256(loaded) == context_values_content_sha256(real)
    assert loaded.keys(ContextField.COUNTRY) == tuple(reversed(real.keys(ContextField.COUNTRY)))


def test_terms_order_does_not_change_the_hash(tmp_path: Path) -> None:
    real = _real()
    data = _real_data()
    terms = _items(data, "countries")[0]["terms"]
    assert isinstance(terms, list)
    terms.reverse()

    loaded = _load(tmp_path, data)

    assert context_values_content_sha256(loaded) == context_values_content_sha256(real)


# --- 格式錯 -------------------------------------------------------------------------------------


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_context_values({"RADAR_CONFIG_DIR": str(tmp_path)})

    assert caught.value.problems[0].startswith(f"{CONTEXT_VALUES_FILE}：找不到檔案")


def test_missing_version_is_refused(tmp_path: Path) -> None:
    data = _real_data()
    del data["version"]

    assert _problems(tmp_path, data) == [f"{CONTEXT_VALUES_FILE}：version：Field required"]


@pytest.mark.parametrize("version", [0, -1, "1", 1.0, True])
def test_version_must_be_a_positive_integer(tmp_path: Path, version: object) -> None:
    problems = _problems(tmp_path, _real_data() | {"version": version})

    assert len(problems) == 1
    assert problems[0].startswith(f"{CONTEXT_VALUES_FILE}：version：")


def test_duplicate_key_in_a_field_is_refused(tmp_path: Path) -> None:
    data = _real_data()
    designs = _items(data, "research_designs")
    designs.append(dict(designs[0]))

    problems = _problems(tmp_path, data)

    assert len(problems) == 1
    assert "key survey 寫了兩次" in problems[0]


@pytest.mark.parametrize("name", ["countries", "industries"])
def test_country_or_industry_without_terms_is_refused(tmp_path: Path, name: str) -> None:
    data = _real_data()
    item = _items(data, name)[0]
    del item["terms"]

    problems = _problems(tmp_path, data)

    assert len(problems) == 1
    assert f"{item['key']} 要有 terms" in problems[0]


@pytest.mark.parametrize("name", ["research_designs", "units_of_analysis"])
def test_design_or_unit_with_terms_is_refused(tmp_path: Path, name: str) -> None:
    data = _real_data()
    item = _items(data, name)[0]
    item["terms"] = ["questionnaire"]

    problems = _problems(tmp_path, data)

    assert len(problems) == 1
    assert f"{item['key']} 不能有 terms" in problems[0]


@pytest.mark.parametrize(
    ("terms", "reason"),
    [
        ([], "terms 不能是空的"),
        (["Taiwan", "taiwan"], "terms 有重複"),
        ([" Taiwan"], "前後不能有空白"),
        (["   "], "terms 不能是空白"),
        (["Tai\nwan"], "要寫成一行"),
    ],
)
def test_bad_terms_are_refused(tmp_path: Path, terms: list[str], reason: str) -> None:
    data = _real_data()
    _items(data, "countries")[0]["terms"] = terms

    problems = _problems(tmp_path, data)

    assert len(problems) == 1
    assert problems[0].startswith(f"{CONTEXT_VALUES_FILE}：countries.0.terms")
    assert reason in problems[0]


def test_term_must_be_a_string(tmp_path: Path) -> None:
    data = _real_data()
    _items(data, "countries")[0]["terms"] = ["Taiwan", 886]

    problems = _problems(tmp_path, data)

    assert len(problems) == 1
    assert problems[0].startswith(f"{CONTEXT_VALUES_FILE}：countries.0.terms.1：")


@pytest.mark.parametrize("key", ["TW", "u-s", "1st", "", " tw", "台灣"])
def test_bad_key_is_refused(tmp_path: Path, key: str) -> None:
    data = _real_data()
    _items(data, "countries")[0]["key"] = key

    problems = _problems(tmp_path, data)

    assert problems == [
        f"{CONTEXT_VALUES_FILE}：countries.0.key：key 要是小寫英文開頭、只有小寫英文、數字與底線"
    ]


def test_extra_fields_are_refused(tmp_path: Path) -> None:
    data = _real_data() | {"regions": []}
    _items(data, "industries")[0]["color"] = "blue"

    problems = _problems(tmp_path, data)

    assert sorted(problems) == sorted(
        [
            f"{CONTEXT_VALUES_FILE}：regions：Extra inputs are not permitted",
            f"{CONTEXT_VALUES_FILE}：industries.0.color：Extra inputs are not permitted",
        ]
    )


@pytest.mark.parametrize("field", ["name", "definition"])
def test_missing_or_blank_item_text_is_refused(tmp_path: Path, field: str) -> None:
    data = _real_data()
    del _items(data, "research_designs")[0][field]
    _items(data, "units_of_analysis")[0][field] = "   "

    problems = _problems(tmp_path, data)

    assert problems == [
        f"{CONTEXT_VALUES_FILE}：research_designs.0.{field}：Field required",
        f"{CONTEXT_VALUES_FILE}：units_of_analysis.0.{field}：{field} 不能是空白",
    ]


def test_definition_must_be_one_line(tmp_path: Path) -> None:
    data = _real_data()
    _items(data, "research_designs")[0]["definition"] = "第一行\n第二行"

    assert _problems(tmp_path, data) == [
        f"{CONTEXT_VALUES_FILE}：research_designs.0.definition："
        "definition 要寫成一行（prompt 的情境值清單一項一行）"
    ]


@pytest.mark.parametrize(
    "name", ["countries", "industries", "research_designs", "units_of_analysis"]
)
def test_empty_or_missing_field_is_refused(tmp_path: Path, name: str) -> None:
    problems = _problems(tmp_path, _real_data() | {name: []})

    assert len(problems) == 1
    assert problems[0].startswith(f"{CONTEXT_VALUES_FILE}：{name}：")

    data = _real_data()
    del data[name]
    assert _problems(tmp_path, data) == [f"{CONTEXT_VALUES_FILE}：{name}：Field required"]


def test_duplicate_yaml_key_is_refused(tmp_path: Path) -> None:
    problems = _problems(tmp_path, REAL + "version: 2\n")

    assert len(problems) == 1
    assert "version" in problems[0]
    assert "寫了兩次" in problems[0]


# --- excluded_terms（Codex 主審查後補：較長的別的地名） -----------------------------------------


def _contains_word(longer: str, shorter: str) -> bool:
    pattern = rf"(?<![0-9a-z]){re.escape(shorter.casefold())}(?![0-9a-z])"
    return re.search(pattern, longer.casefold()) is not None


def test_real_list_excludes_north_korea_and_other_longer_names() -> None:
    values = _real()
    kr = values.item(ContextField.COUNTRY, "kr")
    cn = values.item(ContextField.COUNTRY, "cn")
    assert kr is not None
    assert kr.excluded_terms is not None
    assert cn is not None
    assert cn.excluded_terms is not None
    assert "Democratic People's Republic of Korea" in kr.excluded_terms
    assert "Republic of China" in cn.excluded_terms


def test_no_term_of_one_item_hides_inside_a_term_of_another() -> None:
    # 一個項目的 terms 不能是另一個項目 terms 裡的完整字詞（否則寫出長的那個也會算成短的那個）；
    # 有這種情況就要在短的那一項加 excluded_terms。
    values = _real()
    for field in TERM_FIELDS:
        items = values.items(field)
        for item in items:
            for other in items:
                if other.key == item.key:
                    continue
                for term in item.terms or ():
                    for longer in other.terms or ():
                        excluded = item.excluded_terms or ()
                        assert not _contains_word(longer, term) or any(
                            _contains_word(longer, ex) for ex in excluded
                        ), (item.key, term, other.key, longer)


@pytest.mark.parametrize("name", ["research_designs", "units_of_analysis"])
def test_design_or_unit_with_excluded_terms_is_refused(tmp_path: Path, name: str) -> None:
    data = _real_data()
    item = _items(data, name)[0]
    item["excluded_terms"] = ["online survey"]

    problems = _problems(tmp_path, data)

    assert len(problems) == 1
    assert str(item["key"]) in problems[0]
    assert "不能有 excluded_terms" in problems[0]


@pytest.mark.parametrize(
    ("excluded", "reason"),
    [
        (["Mainland"], "要包含某個 terms"),
        (["Taiwanese"], "和 terms 一樣"),
        (["Taiwanization"], "要包含某個 terms"),  # 只是字首，不是完整字詞
        ([], "excluded_terms 不能是空的"),
        (["Taiwan Strait", "taiwan strait"], "excluded_terms 有重複"),
    ],
)
def test_bad_excluded_terms_are_refused(tmp_path: Path, excluded: list[str], reason: str) -> None:
    data = _real_data()
    _items(data, "countries")[0]["excluded_terms"] = excluded

    problems = _problems(tmp_path, data)

    assert len(problems) == 1
    assert reason in problems[0]


def test_excluded_terms_change_the_content_hash() -> None:
    data = _real_data()
    before = context_values_content_sha256(ContextValues.model_validate(data))
    _items(data, "countries")[0]["excluded_terms"] = ["Taiwan Strait"]
    after = context_values_content_sha256(ContextValues.model_validate(data))

    assert before != after
