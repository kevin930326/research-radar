"""`services/prompt_loader.py`：只從 `prompts/<工作>/v<N>.md` 讀檔，交給 policy 核對
`domain/prompt_hashes.py` 的雜湊，對得上才放行成 `prompt_template`。
"""

import os
from pathlib import Path

import pytest

from radar.domain import prompt_hashes
from radar.domain.ai_outputs import LlmTask
from radar.domain.fingerprints import prompt_sha256
from radar.domain.llm_schemas import FakeResearchQuestion
from radar.domain.policy import ContentSource, cloud_source, cloud_text, is_cloud_safe_text
from radar.domain.prompts import PromptFormatError, PromptIntegrityError
from radar.services.prompt_loader import (
    MAX_PROMPT_BYTES,
    OUTPUT_SCHEMAS,
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

    assert current_prompt_version(LlmTask.ZH_SUMMARY) == 2
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
    file_hash, _ = prompt_hashes.PROMPT_FILE_HASHES["zh_summary"][1]
    prompt_hashes.PROMPT_FILE_HASHES = {"zh_summary": {1: (file_hash, "0" * 64)}}

    with pytest.raises(PromptIntegrityError, match="schema"):
        load_prompt(root, LlmTask.ZH_SUMMARY)


def _register_by_hand(root: Path, schema_sha256: str | None = None) -> None:
    digest = prompt_sha256((root / "zh_summary" / "v1.md").read_bytes())
    prompt_hashes.CURRENT_PROMPT_VERSIONS = {"zh_summary": 1}
    prompt_hashes.PROMPT_FILE_HASHES = {"zh_summary": {1: (digest, schema_sha256)}}


def test_unknown_schema_name_is_a_format_error(tmp_path: Path) -> None:
    # 登記時算不出 schema 雜湊，所以手寫登記：檔案雜湊對得上，但 schema 名稱不存在。
    write_prompt(tmp_path, LlmTask.ZH_SUMMARY, 1, prompt_text(output_schema="NoSuchSchema"))
    _register_by_hand(tmp_path)

    with pytest.raises(PromptFormatError, match="OUTPUT_SCHEMAS"):
        load_prompt(tmp_path, LlmTask.ZH_SUMMARY)


def test_schema_outside_the_output_schema_table_is_rejected(tmp_path: Path) -> None:
    # FakeAnyObject 在 llm_schemas.py 裡、也在凍結登記表裡，但沒設 extra="forbid"，不在對照表。
    write_prompt(tmp_path, LlmTask.ZH_SUMMARY, 1, prompt_text(output_schema="FakeAnyObject"))
    _register_by_hand(tmp_path)

    with pytest.raises(PromptFormatError, match="OUTPUT_SCHEMAS"):
        load_prompt(tmp_path, LlmTask.ZH_SUMMARY)


def test_every_output_schema_forbids_extra_fields() -> None:
    # 模型多給的欄位要被擋下，所以工作用的 schema 一律 extra="forbid"。
    assert OUTPUT_SCHEMAS
    for name, cls in OUTPUT_SCHEMAS.items():
        assert cls.__name__ == name
        assert cls.model_config.get("extra") == "forbid", name


def test_missing_registration(tmp_path: Path) -> None:
    with pytest.raises(PromptIntegrityError, match="zh_summary"):
        load_prompt(tmp_path, LlmTask.ZH_SUMMARY)

    make_prompts(tmp_path, LlmTask.ZH_SUMMARY)
    with pytest.raises(PromptIntegrityError, match="classify"):
        load_prompt(tmp_path, LlmTask.CLASSIFY)
    with pytest.raises(PromptIntegrityError, match="v9"):
        load_prompt(tmp_path, LlmTask.ZH_SUMMARY, 9)


def test_file_that_exists_but_is_not_registered(tmp_path: Path) -> None:
    # 檔案在、登記沒有：policy 不放行（雜湊登記不從參數收）。
    root = make_prompts(tmp_path, LlmTask.ZH_SUMMARY)
    write_prompt(root, LlmTask.ZH_SUMMARY, 2, prompt_text("還沒登記"))

    with pytest.raises(PromptIntegrityError, match="cloud_prompt_unregistered"):
        load_prompt(root, LlmTask.ZH_SUMMARY, 2)


def test_symlinked_prompt_file_is_not_read(tmp_path: Path) -> None:
    root = make_prompts(tmp_path / "prompts", LlmTask.ZH_SUMMARY)
    real = root / "zh_summary" / "v1.md"
    outside = tmp_path / "outside.md"
    outside.write_bytes(real.read_bytes())
    real.unlink()
    os.symlink(outside, real)

    with pytest.raises(PromptIntegrityError, match="讀不出"):
        load_prompt(root, LlmTask.ZH_SUMMARY)


def test_symlinked_task_directory_is_not_read(tmp_path: Path) -> None:
    root = make_prompts(tmp_path / "prompts", LlmTask.ZH_SUMMARY)
    moved = tmp_path / "elsewhere"
    (root / "zh_summary").rename(moved)
    os.symlink(moved, root / "zh_summary")

    with pytest.raises(PromptIntegrityError, match="符號連結"):
        load_prompt(root, LlmTask.ZH_SUMMARY)


def test_prompt_path_is_not_a_regular_file(tmp_path: Path) -> None:
    root = make_prompts(tmp_path, LlmTask.ZH_SUMMARY)
    (root / "zh_summary" / "v2.md").mkdir()

    with pytest.raises(PromptIntegrityError):
        load_prompt(root, LlmTask.ZH_SUMMARY, 2)


def test_oversized_prompt_file_is_not_read(tmp_path: Path) -> None:
    root = make_prompts(tmp_path, LlmTask.ZH_SUMMARY)
    (root / "zh_summary" / "v1.md").write_bytes(b"x" * (MAX_PROMPT_BYTES + 1))

    with pytest.raises(PromptIntegrityError, match="太大"):
        load_prompt(root, LlmTask.ZH_SUMMARY)


@pytest.mark.parametrize("version", [0, -1, True])
def test_version_must_be_a_positive_int(tmp_path: Path, version: int) -> None:
    with pytest.raises(ValueError, match="版本"):
        load_prompt(tmp_path, LlmTask.ZH_SUMMARY, version)


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
        "zh_summary/v2.md：沒有登記在 prompt_hashes.py",
    ]


def test_registry_problems_reports_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_dir"

    assert registry_problems(missing) == [f"{missing}：prompt 資料夾不存在"]
