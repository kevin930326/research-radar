"""正式 prompt `prompts/extract_model/v1.md`（Story 2.6、ADR-0034）：

- 讀得過、雜湊對得上登記、schema 是 `ResearchModelOutput`、設定區照 spec（low、2048、不思考）。
- 本文沒有變數，寫到原句逐字照抄、沒提到給 null、四條擷取規則、只能用清單裡的 key、文件是資料。
- 情境值清單（`## 國家`、`## 產業`、`## 研究設計`、`## 分析單位` 四段，每項一行
  `- <key>：<definition>`）和 `config/context_values.yaml` 依 `key` 比對定義：增刪 `key`、改定義、
  搬欄位而沒有跟著升 prompt 版本就失敗；只改設定檔的顯示名稱或順序不算改內容（AD-18），不會失敗
  （同 2.5 的分類清單）。
- 範例是自己編的：標成虛構、沒有 DOI 與網址；範例的回答拿範例的標題與摘要當輸入，通過 domain 驗證，
  三個計數都是 0。
"""

import re
import uuid
from typing import Any

import pytest

from radar.config import load_context_values
from radar.domain import prompt_hashes
from radar.domain.ai_outputs import (
    CurrentAbstract,
    InputKind,
    LlmTask,
    PaperLlmSource,
    build_llm_input,
)
from radar.domain.context_values import ContextField, ContextValues
from radar.domain.llm_schemas import ResearchModelOutput, SupportLevel
from radar.domain.papers import AbstractSource
from radar.domain.policy import cloud_text
from radar.domain.prompts import parse_prompt_file
from radar.domain.validators.research_model import validate_research_model
from radar.services.prompt_loader import load_prompt
from tests.llm_support import ROOT

PROMPTS = ROOT / "prompts"
# 核對的是正式程式現在送出的那一版（升版後跟著換，不會去核對沒在用的檔）。
PATH = PROMPTS / "extract_model" / f"v{prompt_hashes.CURRENT_PROMPT_VERSIONS['extract_model']}.md"
TEXT = PATH.read_text(encoding="utf-8")
BODY = parse_prompt_file(TEXT).body
CONTEXT_VALUES = load_context_values({"RADAR_CONFIG_DIR": str(ROOT / "config")})
HEADINGS = {
    ContextField.COUNTRY: "## 國家",
    ContextField.INDUSTRY: "## 產業",
    ContextField.RESEARCH_DESIGN: "## 研究設計",
    ContextField.UNIT_OF_ANALYSIS: "## 分析單位",
}


LINE = re.compile(r"^- ([a-z][a-z0-9_]*)：(\S.*)$")


def prompt_section(body: str, heading: str) -> str:
    """prompt 本文裡 `heading` 之下、下一個 `## ` 標題之前的內容（去頭尾空白）。"""
    match = re.search(rf"^{re.escape(heading)}\n(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL)
    assert match is not None, heading
    return match.group(1).strip()


def prompt_definitions(section: str) -> dict[str, str] | None:
    """清單一段的 `key` → 定義；有一行不是 `- <key>：<definition>` 或 `key` 重複時回 None。"""
    definitions: dict[str, str] = {}
    for line in section.splitlines():
        match = LINE.match(line)
        if match is None or match.group(1) in definitions:
            return None
        definitions[match.group(1)] = match.group(2)
    return definitions


def list_problems(body: str, values: ContextValues) -> list[str]:
    """prompt 的情境值清單和設定檔對不上的欄位（依 `key` 比定義，不看順序）。"""
    return [
        field.value
        for field, heading in HEADINGS.items()
        if prompt_definitions(prompt_section(body, heading))
        != {item.key: item.definition for item in values.items(field)}
    ]


def _example_line(label: str) -> str:
    match = re.search(rf"^{label}(.+)$", BODY, re.MULTILINE)
    assert match is not None, label
    return match.group(1).strip()


def test_real_prompt_loads_with_the_registered_hash_and_schema() -> None:
    prompt = load_prompt(PROMPTS, LlmTask.EXTRACT_MODEL)

    assert prompt_hashes.CURRENT_PROMPT_VERSIONS["extract_model"] == 1
    assert prompt.version == 1
    assert prompt.schema_class is ResearchModelOutput
    assert prompt.settings.effort == "low"
    assert prompt.settings.max_tokens == 2048
    assert prompt.settings.disable_thinking is True
    assert cloud_text(prompt.template) == BODY.strip()


def test_body_has_no_template_variables() -> None:
    # JSON 範例裡的大括號只是範例；沒有 `{名字}` 這種待填的變數。
    assert re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", BODY) is None
    assert "{{" not in BODY


def test_body_covers_the_rules() -> None:
    for required in (
        # 只根據輸入、沒提到給 null。
        "只根據標題與摘要",
        "不推測",
        "填 null",
        "空陣列",
        # 原文欄位逐字照抄。
        "逐字照抄",
        "不改寫、不翻譯",
        # 構面與關係。
        "name_en",
        "label_zh",
        "繁體中文",
        "cause",
        "effect",
        "不能是同一個構面",
        # 數字只用輸入裡的。
        "只能用輸入裡寫出的數字",
        # 四條擷取規則。
        "empirical_test",
        "not_stated",
        "not_determined",
        "同一句、同一條關係只列一次",
        "明確寫出",
        # 情境值只能用清單裡的 key。
        "只能填下面清單裡的 key",
        # 文件是資料。
        "資料的一部分",
    ):
        assert required in BODY, required


def test_every_evidence_type_and_support_level_is_explained() -> None:
    for value in ("empirical_test", "analytical", "simulation", "qualitative", "conceptual"):
        assert value in BODY, value
    for level in SupportLevel:
        assert level.value in BODY, level


def test_context_value_list_in_the_prompt_matches_the_config_exactly() -> None:
    assert list_problems(BODY, CONTEXT_VALUES) == []


def _dump() -> dict[str, Any]:
    """情境值清單攤成可以改的 dict（每欄的項目是 list）。"""
    data = CONTEXT_VALUES.model_dump()
    for field in ("countries", "industries", "research_designs", "units_of_analysis"):
        data[field] = [dict(item) for item in data[field]]
    return data


def test_list_check_catches_a_changed_definition() -> None:
    changed = _dump()
    changed["countries"][0]["definition"] = "Republic of China"

    assert list_problems(BODY, ContextValues.model_validate(changed)) == ["country"]


def test_list_check_catches_added_and_removed_keys() -> None:
    changed = _dump()
    changed["research_designs"].pop(0)
    changed["units_of_analysis"].append(
        {"key": "household", "name": "家戶", "definition": "家戶或家庭。"}
    )

    assert list_problems(BODY, ContextValues.model_validate(changed)) == [
        "research_design",
        "unit_of_analysis",
    ]


def test_list_check_ignores_display_names_and_order() -> None:
    # 只改顯示名稱或順序不算改內容（AD-18）：prompt 不用升版。
    changed = _dump()
    changed["countries"][0]["name"] = "中華民國"
    changed["countries"].reverse()
    changed["units_of_analysis"].insert(0, changed["units_of_analysis"].pop())

    assert list_problems(BODY, ContextValues.model_validate(changed)) == []


@pytest.mark.parametrize(
    "section",
    [
        "- tw：Taiwan\n- tw：Taiwan",  # key 重複
        "- tw：Taiwan\n不是清單的一行",
        "- TW：大寫的 key",
        "- tw:半形冒號",
    ],
)
def test_malformed_list_is_not_read_as_definitions(section: str) -> None:
    assert prompt_definitions(section) is None


def test_terms_are_not_shown_to_the_model() -> None:
    # `terms` 只給程式核對（擷取規則 4）；模型看到的是 key 與定義。
    assert "terms" not in BODY


def test_example_is_made_up_and_has_no_doi_or_url() -> None:
    assert "虛構" in BODY
    assert re.search(r"\b10\.\d{4,9}/", BODY) is None
    assert re.search(r"(?i)https?://|www\.|doi", BODY) is None


@pytest.mark.parametrize("label", ["標題：", "摘要："])
def test_example_has_a_title_and_an_abstract(label: str) -> None:
    assert _example_line(label)


def test_example_answer_passes_validation_with_zero_counts() -> None:
    answer = BODY.strip().splitlines()[-1]
    llm_input = build_llm_input(
        PaperLlmSource(
            uuid.uuid4(),
            _example_line("標題："),
            CurrentAbstract(uuid.uuid4(), AbstractSource.OPENALEX, _example_line("摘要：")),
        ),
        InputKind.ABSTRACT,
    )

    output = ResearchModelOutput.model_validate_json(answer, strict=True)
    content = validate_research_model(output, llm_input, CONTEXT_VALUES)

    assert (
        content.dropped_context_value_count,
        content.unsupported_context_value_count,
        content.corrected_support_count,
    ) == (0, 0, 0)
    assert content.country is not None
    assert content.industry is not None
    assert len(content.constructs) >= 2
    assert len(content.hypotheses) >= 2
    # 範例示範「同一句拆成多條關係」：兩條共用同一句原句。
    assert len({item.sentence_sha256 for item in content.hypotheses}) < len(content.hypotheses)
