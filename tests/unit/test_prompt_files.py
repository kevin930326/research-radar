"""prompt 檔的格式（`domain/prompts.py`）：`---` 包住的 `key: value` 設定區＋本文，與登記檔。"""

import pytest

from radar.domain.ai_outputs import LlmTask
from radar.domain.prompts import (
    PromptFormatError,
    PromptIntegrityError,
    PromptRegistry,
    PromptSettings,
    parse_prompt_file,
)
from tests.llm_support import prompt_text

SHA = "a" * 64


def test_parse_settings_and_body() -> None:
    parsed = parse_prompt_file(prompt_text("第一行\n\n第二行"))

    assert parsed.settings == PromptSettings(
        effort="low", max_tokens=1024, disable_thinking=False, output_schema="FakeResearchQuestion"
    )
    assert parsed.body == "第一行\n\n第二行"


def test_optional_settings_default() -> None:
    parsed = parse_prompt_file("---\nmax_tokens: 2048\n---\n本文\n")

    assert parsed.settings == PromptSettings(max_tokens=2048)
    assert parsed.settings.effort is None
    assert parsed.settings.output_schema is None
    assert parsed.settings.disable_thinking is False


def test_crlf_and_comments_and_blank_lines_in_settings() -> None:
    text = "---\r\n# 註解\r\n\r\nmax_tokens: 10\r\ndisable_thinking: true\r\n---\r\n本文\r\n"

    parsed = parse_prompt_file(text)

    assert parsed.settings.disable_thinking is True
    assert parsed.body == "本文"


def test_body_keeps_template_text_without_variables() -> None:
    # 本文原樣保留（不做 .format），大括號也不展開。
    parsed = parse_prompt_file("---\nmax_tokens: 10\n---\n請回答 {question}\n")

    assert parsed.body == "請回答 {question}"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("max_tokens: 10\n---\n本文", "第一行"),
        ("---\nmax_tokens: 10\n本文", "結尾"),
        ("---\nmax_tokens: 10\n---\n   \n", "本文是空的"),
        ("---\nmax_tokens: 10\nmax_tokens: 20\n---\n本文", "寫了兩次"),
        ("---\nmax_tokens: 10\ntemperature: 0\n---\n本文", "temperature"),
        ("---\nmax_tokens: lots\n---\n本文", "max_tokens"),
        ("---\nmax_tokens: 0\n---\n本文", "max_tokens"),
        ("---\nmax_tokens: 10\neffort: extreme\n---\n本文", "effort"),
        ("---\nmax_tokens: 10\ndisable_thinking: maybe\n---\n本文", "disable_thinking"),
        ("---\nmax_tokens: 10\noutput_schema: a.b\n---\n本文", "output_schema"),
        ("---\nnot a setting\n---\n本文", "key: value"),
        ("---\neffort: low\n---\n本文", "max_tokens"),
    ],
)
def test_bad_prompt_files_are_rejected(text: str, message: str) -> None:
    with pytest.raises(PromptFormatError, match=message):
        parse_prompt_file(text)


def test_registry_parses_versions() -> None:
    registry = PromptRegistry.parse(
        '{"zh_summary": {"current": 2, "versions": {'
        f'"1": {{"sha256": "{SHA}", "schema_sha256": null}},'
        f'"2": {{"sha256": "{SHA}", "schema_sha256": "{SHA}"}}'
        "}}}"
    )

    entry = registry.entry(LlmTask.ZH_SUMMARY)
    assert entry.current == 2
    assert sorted(entry.versions) == [1, 2]
    assert entry.versions[2].schema_sha256 == SHA


def test_empty_registry_has_no_tasks() -> None:
    registry = PromptRegistry.parse("{}")

    assert dict(registry.tasks) == {}
    with pytest.raises(PromptIntegrityError, match="classify"):
        registry.entry(LlmTask.CLASSIFY)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"unknown_task": {"current": 1, "versions": {}}}',
        f'{{"zh_summary": {{"current": 2, "versions": {{"1": {{"sha256": "{SHA}"}}}}}}}}',
        '{"zh_summary": {"current": 1, "versions": {"1": {"sha256": "short"}}}}',
        f'{{"zh_summary": {{"current": 1, "versions": {{"1": {{"sha256": "{SHA}", "x": 1}}}}}}}}',
        f'{{"zh_summary": {{"current": "1", "versions": {{"1": {{"sha256": "{SHA}"}}}}}}}}',
    ],
)
def test_bad_registry_is_rejected(raw: str) -> None:
    with pytest.raises(PromptFormatError):
        PromptRegistry.parse(raw)
