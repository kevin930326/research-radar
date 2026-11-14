"""`services/prompt_loader.py`：只從 `prompts/<工作>/v<N>.md` 讀檔，雜湊對登記才放行成
`prompt_template`。
"""

import json
from pathlib import Path

import pytest

from radar.domain.ai_outputs import LlmTask
from radar.domain.fingerprints import prompt_sha256
from radar.domain.llm_schemas import FakeResearchQuestion
from radar.domain.policy import ContentSource, cloud_source, cloud_text, is_cloud_safe_text
from radar.domain.prompts import PromptFormatError, PromptIntegrityError
from radar.services.prompt_loader import (
    current_prompt_version,
    load_prompt,
    registry_problems,
)
from tests.llm_support import make_prompts, prompt_text, register_prompts, write_prompt


def test_load_current_prompt(tmp_path: Path) -> None:
    root = make_prompts(tmp_path, LlmTask.ZH_SUMMARY)

    prompt = load_prompt(root, LlmTask.ZH_SUMMARY)

    assert prompt.version == 1
    assert prompt.settings.max_tokens == 1024
    assert prompt.schema_class is FakeResearchQuestion
    assert prompt.schema_sha256 is not None
    assert is_cloud_safe_text(prompt.template)
    assert cloud_source(prompt.template) is ContentSource.PROMPT_TEMPLATE
    assert cloud_text(prompt.template) == "請只根據文件找出研究問題，用 JSON 回答。"
    assert registry_problems(root) == []


def test_current_version_and_explicit_older_version(tmp_path: Path) -> None:
    write_prompt(tmp_path, LlmTask.ZH_SUMMARY, 1, prompt_text("舊版"))
    write_prompt(tmp_path, LlmTask.ZH_SUMMARY, 2, prompt_text("新版"))
    register_prompts(tmp_path)

    assert current_prompt_version(tmp_path, LlmTask.ZH_SUMMARY) == 2
    assert cloud_text(load_prompt(tmp_path, LlmTask.ZH_SUMMARY).template) == "新版"
    assert cloud_text(load_prompt(tmp_path, LlmTask.ZH_SUMMARY, 1).template) == "舊版"


def test_prompt_without_schema(tmp_path: Path) -> None:
    root = make_prompts(tmp_path, LlmTask.ZH_SUMMARY, text=prompt_text(output_schema=None))

    prompt = load_prompt(root, LlmTask.ZH_SUMMARY)

    assert prompt.schema_class is None
    assert prompt.schema_sha256 is None


def test_edited_template_without_new_version_is_rejected(tmp_path: Path) -> None:
    root = make_prompts(tmp_path, LlmTask.ZH_SUMMARY)
    write_prompt(root, LlmTask.ZH_SUMMARY, 1, prompt_text("偷偷改了本文"))

    with pytest.raises(PromptIntegrityError, match="升版"):
        load_prompt(root, LlmTask.ZH_SUMMARY)
    assert registry_problems(root) != []


def test_edited_settings_without_new_version_is_rejected(tmp_path: Path) -> None:
    root = make_prompts(tmp_path, LlmTask.ZH_SUMMARY)
    write_prompt(root, LlmTask.ZH_SUMMARY, 1, prompt_text(max_tokens=4096))

    with pytest.raises(PromptIntegrityError):
        load_prompt(root, LlmTask.ZH_SUMMARY)


def test_schema_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    root = make_prompts(tmp_path, LlmTask.ZH_SUMMARY)
    registry = json.loads((root / "registry.json").read_text())
    registry["zh_summary"]["versions"]["1"]["schema_sha256"] = "0" * 64
    (root / "registry.json").write_text(json.dumps(registry))

    with pytest.raises(PromptIntegrityError, match="schema"):
        load_prompt(root, LlmTask.ZH_SUMMARY)


def test_unknown_schema_name_is_a_format_error(tmp_path: Path) -> None:
    # 登記時算不出 schema 雜湊，所以手寫登記：檔案雜湊對得上，但 schema 名稱不存在。
    write_prompt(tmp_path, LlmTask.ZH_SUMMARY, 1, prompt_text(output_schema="NoSuchSchema"))
    digest = prompt_sha256((tmp_path / "zh_summary" / "v1.md").read_bytes())
    (tmp_path / "registry.json").write_text(
        json.dumps({"zh_summary": {"current": 1, "versions": {"1": {"sha256": digest}}}})
    )

    with pytest.raises(PromptFormatError, match="NoSuchSchema"):
        load_prompt(tmp_path, LlmTask.ZH_SUMMARY)


def test_schema_without_extra_forbid_is_rejected(tmp_path: Path) -> None:
    # FakeAnyObject 沒設 extra="forbid"：模型多給的欄位擋不下，所以不能當工作的 schema。
    write_prompt(tmp_path, LlmTask.ZH_SUMMARY, 1, prompt_text(output_schema="FakeAnyObject"))
    digest = prompt_sha256((tmp_path / "zh_summary" / "v1.md").read_bytes())
    (tmp_path / "registry.json").write_text(
        json.dumps({"zh_summary": {"current": 1, "versions": {"1": {"sha256": digest}}}})
    )

    with pytest.raises(PromptFormatError, match="forbid"):
        load_prompt(tmp_path, LlmTask.ZH_SUMMARY)


def test_missing_registration_file_or_task(tmp_path: Path) -> None:
    with pytest.raises(PromptIntegrityError, match="登記檔"):
        load_prompt(tmp_path, LlmTask.ZH_SUMMARY)

    make_prompts(tmp_path, LlmTask.ZH_SUMMARY)
    with pytest.raises(PromptIntegrityError, match="classify"):
        load_prompt(tmp_path, LlmTask.CLASSIFY)
    with pytest.raises(PromptIntegrityError, match="v9"):
        load_prompt(tmp_path, LlmTask.ZH_SUMMARY, 9)


def test_registered_file_that_is_missing(tmp_path: Path) -> None:
    root = make_prompts(tmp_path, LlmTask.ZH_SUMMARY)
    (root / "zh_summary" / "v1.md").unlink()

    with pytest.raises(PromptIntegrityError, match="找不到"):
        load_prompt(root, LlmTask.ZH_SUMMARY)


def test_registry_problems_lists_unregistered_and_misnamed_files(tmp_path: Path) -> None:
    root = make_prompts(tmp_path, LlmTask.ZH_SUMMARY)
    write_prompt(root, LlmTask.ZH_SUMMARY, 2, prompt_text("還沒登記"))
    (root / "zh_summary" / "draft.md").write_text("x", encoding="utf-8")
    (root / "not_a_task").mkdir()

    assert registry_problems(root) == [
        "not_a_task/：資料夾名稱不是工作代碼",
        "zh_summary/draft.md：檔名要是 v<N>.md",
        "zh_summary/v2.md：沒有登記在 registry.json",
    ]
