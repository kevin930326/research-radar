"""正式 prompt `prompts/zh_summary/v1.md`（Story 2.4、ADR-0032）：

- 讀得過、雜湊對得上登記、schema 是 `ZhSummaryOutput`、設定區照 spec（low、1024、不思考）。
- 本文沒有變數（不 `.format`），寫到四欄、null、繁中、不逐句翻譯、80 字。
- 範例是自己編的：標成虛構、沒有 DOI 與網址；範例的回答本身通過 domain 驗證。
"""

import re
import uuid

from radar.config import load_zh_summary_rules
from radar.domain import prompt_hashes
from radar.domain.ai_outputs import (
    CurrentAbstract,
    InputKind,
    LlmTask,
    PaperLlmSource,
    build_llm_input,
)
from radar.domain.llm_schemas import ZhSummaryOutput
from radar.domain.papers import AbstractSource
from radar.domain.policy import cloud_text
from radar.domain.prompts import parse_prompt_file
from radar.domain.validators.zh_summary import SIMPLIFIED_ONLY, validate_zh_summary
from radar.services.prompt_loader import load_prompt
from tests.llm_support import ROOT

PROMPTS = ROOT / "prompts"
PATH = PROMPTS / "zh_summary" / "v1.md"
TEXT = PATH.read_text(encoding="utf-8")
BODY = parse_prompt_file(TEXT).body


def test_real_prompt_loads_with_the_registered_hash_and_schema() -> None:
    prompt = load_prompt(PROMPTS, LlmTask.ZH_SUMMARY)

    assert prompt_hashes.CURRENT_PROMPT_VERSIONS["zh_summary"] == 1
    assert prompt.version == 1
    assert prompt.schema_class is ZhSummaryOutput
    assert prompt.settings.effort == "low"
    assert prompt.settings.max_tokens == 1024
    assert prompt.settings.disable_thinking is True
    assert cloud_text(prompt.template) == BODY.strip()


def test_body_has_no_template_variables() -> None:
    # JSON 範例的大括號只出現在範例那一行；沒有 `{名字}` 這種待填的變數。
    assert re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", BODY) is None
    assert "{{" not in BODY


def test_body_covers_the_rules() -> None:
    for required in (
        "research_question",
        "method",
        "theory",
        "findings",
        "null",
        "繁體中文",
        "不推測",
        "不要逐句翻譯",
        "80 字以內",
        "摘要未提及",
    ):
        assert required in BODY, required


def test_body_has_no_simplified_only_characters() -> None:
    assert [char for char in BODY if char in SIMPLIFIED_ONLY] == []


def test_example_is_made_up_and_has_no_doi_or_url() -> None:
    assert "虛構" in BODY
    assert re.search(r"\b10\.\d{4,9}/", BODY) is None
    assert re.search(r"(?i)https?://|www\.|doi", BODY) is None


def test_example_answer_passes_the_domain_validation() -> None:
    title_match = re.search(r"^標題：(.+)$", BODY, re.MULTILINE)
    abstract_match = re.search(r"^摘要：(.+)$", BODY, re.MULTILINE)
    assert title_match is not None
    assert abstract_match is not None
    answer = BODY.strip().splitlines()[-1]
    source = PaperLlmSource(
        uuid.uuid4(),
        title_match.group(1),
        CurrentAbstract(uuid.uuid4(), AbstractSource.OPENALEX, abstract_match.group(1)),
    )
    rules = load_zh_summary_rules({"RADAR_CONFIG_DIR": str(ROOT / "config")})

    content = validate_zh_summary(
        ZhSummaryOutput.model_validate_json(answer, strict=True),
        build_llm_input(source, InputKind.ABSTRACT),
        rules,
    )

    assert all(value is not None for value in content.fields().values())
