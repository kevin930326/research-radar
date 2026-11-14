"""正式 prompt `prompts/classify/v1.md`（Story 2.5、ADR-0033）：

- 讀得過、雜湊對得上登記、schema 是 `ClassificationOutput`、設定區照 spec（low、512、不思考）。
- 本文沒有變數，寫到多選、只能用清單裡的 key、都不符合給空陣列、只有標題照標題判斷、文件是資料。
- 分類清單（`## 主題`、`## 方法` 兩段，每項一行 `- <key>：<definition>`，照設定檔順序）和
  `config/taxonomy.yaml` 一字不差：分類表改了沒有跟著升 prompt 版本就失敗。
- 範例是自己編的：標成虛構、沒有 DOI 與網址；範例的回答只用清單裡的 key。
"""

import re

import pytest

from radar.config import load_taxonomy
from radar.domain import prompt_hashes
from radar.domain.ai_outputs import LlmTask
from radar.domain.llm_schemas import ClassificationOutput
from radar.domain.policy import cloud_text
from radar.domain.prompts import parse_prompt_file
from radar.domain.taxonomy import UNCLASSIFIED, Taxonomy, TaxonomyField, TaxonomyItem
from radar.domain.validators.classification import normalize_classification
from radar.services.prompt_loader import load_prompt
from tests.llm_support import ROOT

PROMPTS = ROOT / "prompts"
# 核對的是正式程式現在送出的那一版（升版後跟著換，不會去核對沒在用的檔）。
PATH = PROMPTS / "classify" / f"v{prompt_hashes.CURRENT_PROMPT_VERSIONS['classify']}.md"
TEXT = PATH.read_text(encoding="utf-8")
BODY = parse_prompt_file(TEXT).body
TAXONOMY = load_taxonomy({"RADAR_CONFIG_DIR": str(ROOT / "config")})
HEADINGS = {TaxonomyField.TOPIC: "## 主題", TaxonomyField.METHOD: "## 方法"}


def expected_section(items: tuple[TaxonomyItem, ...]) -> str:
    """設定檔的一個欄位寫成 prompt 裡的清單：每項一行 `- <key>：<definition>`，照設定檔順序。"""
    return "\n".join(f"- {item.key}：{item.definition}" for item in items)


def prompt_section(body: str, heading: str) -> str:
    """prompt 本文裡 `heading` 之下、下一個 `## ` 標題之前的內容（去頭尾空白）。"""
    match = re.search(rf"^{re.escape(heading)}\n(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL)
    assert match is not None, heading
    return match.group(1).strip()


def list_problems(body: str, taxonomy: Taxonomy) -> list[str]:
    """prompt 的分類清單和分類表對不上的欄位。"""
    return [
        field.value
        for field, heading in HEADINGS.items()
        if prompt_section(body, heading) != expected_section(taxonomy.items(field))
    ]


def test_real_prompt_loads_with_the_registered_hash_and_schema() -> None:
    prompt = load_prompt(PROMPTS, LlmTask.CLASSIFY)

    assert prompt_hashes.CURRENT_PROMPT_VERSIONS["classify"] == 1
    assert prompt.version == 1
    assert prompt.schema_class is ClassificationOutput
    assert prompt.settings.effort == "low"
    assert prompt.settings.max_tokens == 512
    assert prompt.settings.disable_thinking is True
    assert cloud_text(prompt.template) == BODY.strip()


def test_body_has_no_template_variables() -> None:
    # JSON 範例與規則裡的大括號只是範例；沒有 `{名字}` 這種待填的變數。
    assert re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", BODY) is None
    assert "{{" not in BODY


def test_body_covers_the_rules() -> None:
    for required in (
        "topics",
        "methods",
        "多選",
        "只能用下面清單裡的 key",
        "空陣列",
        "只有標題",
        "照標題判斷",
        "不推測",
        "資料的一部分",
    ):
        assert required in BODY, required


def test_taxonomy_list_in_the_prompt_matches_the_config_exactly() -> None:
    assert list_problems(BODY, TAXONOMY) == []


def test_list_check_catches_a_changed_definition_or_order() -> None:
    # 設定檔改了定義（沒同步改 prompt）或換了順序，核對都會失敗。
    changed = TAXONOMY.model_dump()
    changed["topics"][0]["definition"] = "企業資源規劃系統。"
    changed["methods"] = changed["methods"][::-1]

    assert list_problems(BODY, Taxonomy.model_validate(changed)) == ["topic", "method"]


def test_unclassified_is_not_offered_to_the_model() -> None:
    assert UNCLASSIFIED not in BODY


def test_example_is_made_up_and_has_no_doi_or_url() -> None:
    assert "虛構" in BODY
    assert re.search(r"\b10\.\d{4,9}/", BODY) is None
    assert re.search(r"(?i)https?://|www\.|doi", BODY) is None


@pytest.mark.parametrize("label", ["標題：", "摘要："])
def test_example_has_a_title_and_an_abstract(label: str) -> None:
    assert re.search(rf"^{label}.+$", BODY, re.MULTILINE) is not None


def test_example_answer_uses_only_listed_keys() -> None:
    answer = BODY.strip().splitlines()[-1]

    output = ClassificationOutput.model_validate_json(answer, strict=True)
    content = normalize_classification(output, TAXONOMY)

    assert content.dropped_label_count == 0
    assert UNCLASSIFIED not in content.topics
    assert content.methods != ()
