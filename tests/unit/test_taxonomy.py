"""`config/taxonomy.yaml` 與 `load_taxonomy`（Story 2.5、ADR-0033）：

- 真的設定讀得過，`key` 是 PRD FR-15 的清單（6 個主題、5 個方法；不看顯示名稱與順序），
  `version` 寫在最上面。
- 內容（`key`、定義、欄位）改了卻沒升版、升了版沒登記 → `ConfigError`。
- 只改顯示名稱或順序 → 照樣讀得過、內容雜湊不變。
- 每種格式錯都在讀取時擋下，訊息是「檔名：欄位路徑：原因」、每條一行。
"""

from pathlib import Path

import pytest
import yaml

from radar.config import TAXONOMY_FILE, ConfigError, load_taxonomy
from radar.domain import taxonomy_hashes
from radar.domain.fingerprints import taxonomy_content_sha256
from radar.domain.taxonomy import Taxonomy, TaxonomyField, taxonomy_registry_problems
from tests.llm_support import ROOT

REAL_PATH = ROOT / "config" / TAXONOMY_FILE
REAL = REAL_PATH.read_text(encoding="utf-8")

# PRD FR-15 的清單，只核對 `key`（不寫死顯示名稱與順序：只改這兩樣不算改內容，AD-18）。
PRD_TOPIC_KEYS = frozenset(
    {
        "erp",
        "supply_chain",
        "e_commerce",
        "it_project_management",
        "information_economics",
        "ai_data_analytics",
    }
)
PRD_METHOD_KEYS = frozenset(
    {"survey_sem", "interview_case_study", "experiment", "data_analysis_ml", "literature_review"}
)


def _real_data() -> dict[str, object]:
    data = yaml.safe_load(REAL)
    assert isinstance(data, dict)
    return data


def _write(tmp_path: Path, data: dict[str, object] | str) -> None:
    text = data if isinstance(data, str) else yaml.safe_dump(data, allow_unicode=True)
    (tmp_path / TAXONOMY_FILE).write_text(text, encoding="utf-8")


def _load(tmp_path: Path, data: dict[str, object] | str) -> Taxonomy:
    _write(tmp_path, data)
    return load_taxonomy({"RADAR_CONFIG_DIR": str(tmp_path)})


def _problems(tmp_path: Path, data: dict[str, object] | str) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, data)
    problems = caught.value.problems
    assert problems
    assert all("\n" not in problem for problem in problems)
    assert all(problem.startswith(f"{TAXONOMY_FILE}：") for problem in problems)
    return problems


def _items(data: dict[str, object], name: str) -> list[dict[str, str]]:
    items = data[name]
    assert isinstance(items, list)
    return items


# --- 真的設定 -----------------------------------------------------------------------------------


def test_real_taxonomy_is_the_prd_list() -> None:
    taxonomy = load_taxonomy({"RADAR_CONFIG_DIR": str(ROOT / "config")})

    assert taxonomy.version == 1
    assert taxonomy.version_label == "1"
    assert set(taxonomy.keys(TaxonomyField.TOPIC)) == PRD_TOPIC_KEYS
    assert set(taxonomy.keys(TaxonomyField.METHOD)) == PRD_METHOD_KEYS
    assert taxonomy_registry_problems(taxonomy) == []


def test_version_is_the_first_key_of_the_real_file() -> None:
    document = yaml.compose(REAL, Loader=yaml.SafeLoader)
    assert isinstance(document, yaml.MappingNode)
    first_key = document.value[0][0]

    assert first_key.value == "version"
    assert REAL.splitlines()[0] == "version: 1"


def test_real_file_documents_its_sources_and_the_bump_rule() -> None:
    for required in (
        "FR-15",
        "FR-36",
        "ADR-0033",
        "金標準",
        "taxonomy_hashes.py",
        "prompts/classify",
    ):
        assert required in REAL, required


# --- 內容改了要升版 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    ["definition", "key", "add_item", "remove_item", "move_to_other_field"],
)
def test_content_changed_without_version_bump_is_refused(tmp_path: Path, change: str) -> None:
    data = _real_data()
    topics = _items(data, "topics")
    methods = _items(data, "methods")
    match change:
        case "definition":
            topics[0]["definition"] = topics[0]["definition"] + "包含企業內部的系統整合。"
        case "key":
            topics[0]["key"] = "enterprise_systems"
        case "add_item":
            topics.append({"key": "fintech", "name": "金融科技", "definition": "金融科技。"})
        case "remove_item":
            methods.pop()
        case "move_to_other_field":
            methods.append(topics.pop())

    problems = _problems(tmp_path, data)

    assert problems == [
        f"{TAXONOMY_FILE}：version：版本 1 的內容和登記的雜湊不一樣"
        "（key、定義或欄位改了就要升版，並登記新版本的雜湊）"
    ]


def test_bumped_version_without_registration_is_refused(tmp_path: Path) -> None:
    data = _real_data() | {"version": 2}

    problems = _problems(tmp_path, data)

    assert problems == [
        f"{TAXONOMY_FILE}：version：版本 2 沒有登記在 radar/domain/taxonomy_hashes.py"
        "（升版要一起登記內容雜湊）"
    ]


def test_bumped_and_registered_version_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _real_data() | {"version": 2}
    _items(data, "topics")[0]["definition"] = "企業資源規劃系統。"
    changed = Taxonomy.model_validate(data)
    monkeypatch.setattr(
        taxonomy_hashes,
        "TAXONOMY_CONTENT_HASHES",
        taxonomy_hashes.TAXONOMY_CONTENT_HASHES | {2: taxonomy_content_sha256(changed)},
    )

    assert _load(tmp_path, data).version == 2


def test_only_names_and_order_changed_still_loads_with_the_same_hash(tmp_path: Path) -> None:
    real = load_taxonomy({"RADAR_CONFIG_DIR": str(ROOT / "config")})
    data = _real_data()
    topics = _items(data, "topics")
    methods = _items(data, "methods")
    topics[0]["name"] = "企業資源規劃"
    methods[1]["name"] = "質性研究"
    topics.reverse()
    methods.insert(0, methods.pop())

    loaded = _load(tmp_path, data)

    assert taxonomy_content_sha256(loaded) == taxonomy_content_sha256(real)
    assert loaded.keys(TaxonomyField.TOPIC) == tuple(reversed(real.keys(TaxonomyField.TOPIC)))


# --- 格式錯 -------------------------------------------------------------------------------------


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_taxonomy({"RADAR_CONFIG_DIR": str(tmp_path)})

    assert caught.value.problems[0].startswith(f"{TAXONOMY_FILE}：找不到檔案")


def test_missing_version_is_refused(tmp_path: Path) -> None:
    data = _real_data()
    del data["version"]

    assert _problems(tmp_path, data) == [f"{TAXONOMY_FILE}：version：Field required"]


@pytest.mark.parametrize("version", [0, -1, "1", 1.0, True])
def test_version_must_be_a_positive_integer(tmp_path: Path, version: object) -> None:
    problems = _problems(tmp_path, _real_data() | {"version": version})

    assert len(problems) == 1
    assert problems[0].startswith(f"{TAXONOMY_FILE}：version：")


def test_duplicate_key_in_a_field_is_refused(tmp_path: Path) -> None:
    data = _real_data()
    topics = _items(data, "topics")
    topics.append(dict(topics[0]))

    assert _problems(tmp_path, data) == [f"{TAXONOMY_FILE}：topics：key erp 寫了兩次"]


def test_same_key_in_both_fields_is_refused(tmp_path: Path) -> None:
    data = _real_data()
    _items(data, "methods").append({"key": "erp", "name": "ERP", "definition": "企業系統。"})

    assert _problems(tmp_path, data) == [
        f"{TAXONOMY_FILE}：（最上層）：key 不能同時出現在 topics 與 methods：erp"
    ]


def test_unclassified_is_reserved(tmp_path: Path) -> None:
    data = _real_data()
    _items(data, "topics")[2]["key"] = "unclassified"

    assert _problems(tmp_path, data) == [
        f"{TAXONOMY_FILE}：topics.2.key：unclassified 是保留字，不能當 key"
    ]


@pytest.mark.parametrize("key", ["ERP", "supply-chain", "1st", "", " erp", "電商"])
def test_bad_key_is_refused(tmp_path: Path, key: str) -> None:
    data = _real_data()
    _items(data, "methods")[0]["key"] = key

    problems = _problems(tmp_path, data)

    assert problems == [
        f"{TAXONOMY_FILE}：methods.0.key：key 要是小寫英文開頭、只有小寫英文、數字與底線"
    ]


def test_extra_fields_are_refused(tmp_path: Path) -> None:
    data = _real_data() | {"journals": []}
    _items(data, "topics")[0]["color"] = "blue"

    problems = _problems(tmp_path, data)

    assert sorted(problems) == sorted(
        [
            f"{TAXONOMY_FILE}：journals：Extra inputs are not permitted",
            f"{TAXONOMY_FILE}：topics.0.color：Extra inputs are not permitted",
        ]
    )


@pytest.mark.parametrize("field", ["name", "definition"])
def test_missing_or_blank_item_text_is_refused(tmp_path: Path, field: str) -> None:
    data = _real_data()
    del _items(data, "topics")[0][field]
    _items(data, "methods")[0][field] = "   "

    problems = _problems(tmp_path, data)

    assert problems == [
        f"{TAXONOMY_FILE}：topics.0.{field}：Field required",
        f"{TAXONOMY_FILE}：methods.0.{field}：{field} 不能是空白",
    ]


def test_definition_must_be_one_line(tmp_path: Path) -> None:
    data = _real_data()
    _items(data, "topics")[0]["definition"] = "第一行\n第二行"

    assert _problems(tmp_path, data) == [
        f"{TAXONOMY_FILE}：topics.0.definition：definition 要寫成一行（prompt 的分類清單一項一行）"
    ]


def test_empty_field_is_refused(tmp_path: Path) -> None:
    problems = _problems(tmp_path, _real_data() | {"methods": []})

    assert len(problems) == 1
    assert problems[0].startswith(f"{TAXONOMY_FILE}：methods：")


def test_number_as_name_is_refused(tmp_path: Path) -> None:
    data = _real_data()
    _items(data, "topics")[0]["name"] = 2024  # type: ignore[assignment]

    assert _problems(tmp_path, data) == [
        f"{TAXONOMY_FILE}：topics.0.name：Input should be a valid string"
    ]


def test_duplicate_yaml_key_is_refused(tmp_path: Path) -> None:
    problems = _problems(tmp_path, REAL + "version: 2\n")

    assert len(problems) == 1
    assert "version" in problems[0]
    assert "寫了兩次" in problems[0]
