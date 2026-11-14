"""prompt 檔與登記（`radar/domain/prompt_hashes.py`）一定對得上（AD-8、ADR-0030「依此更正」）：

- 真的 `prompts/`：`registry_problems` 是空的（正式登記的 `zh_summary` v1 與檔案對得上）。
- 登記只有一份：「改了沒升版就失敗」核對的、和 policy 放行模板時核對的是同一張表
  （`domain/prompt_hashes.py`），這裡也證明 policy 真的只認那張表。
- 在 tmp 示範：改了模板或設定區卻沒升版（沒有新增 `v<N+1>.md`、也沒更新登記）會被抓到，
  `load_prompt` 也拒絕讀；schema 改了卻沒升版、只寫了檔沒登記、資料夾或檔名不對也會被抓到。
"""

from pathlib import Path

import pytest

from radar.domain import prompt_hashes
from radar.domain.ai_outputs import LlmTask
from radar.domain.errors import ErrorCode
from radar.domain.policy import CloudClearanceError, clear_prompt_file
from radar.domain.prompts import PromptIntegrityError
from radar.services.prompt_loader import load_prompt, registry_problems
from tests.llm_support import ROOT, make_prompts, prompt_text, write_prompt


def test_real_prompts_match_registry() -> None:
    assert registry_problems(ROOT / "prompts") == []


@pytest.fixture
def prompts(tmp_path: Path) -> Path:
    root = make_prompts(tmp_path / "prompts", LlmTask.ZH_SUMMARY)
    assert registry_problems(root) == []
    return root


def test_template_changed_without_version_bump_fails(prompts: Path) -> None:
    write_prompt(prompts, LlmTask.ZH_SUMMARY, 1, prompt_text("偷偷改了說明。"))

    problems = registry_problems(prompts)

    assert len(problems) == 1
    with pytest.raises(PromptIntegrityError):
        load_prompt(prompts, LlmTask.ZH_SUMMARY)


def test_settings_changed_without_version_bump_fails(prompts: Path) -> None:
    write_prompt(prompts, LlmTask.ZH_SUMMARY, 1, prompt_text(max_tokens=2048))

    assert len(registry_problems(prompts)) == 1
    with pytest.raises(PromptIntegrityError):
        load_prompt(prompts, LlmTask.ZH_SUMMARY)


def test_unregistered_file_and_bad_names_are_reported(prompts: Path) -> None:
    write_prompt(prompts, LlmTask.ZH_SUMMARY, 2, prompt_text("第二版，還沒登記。"))
    (prompts / LlmTask.ZH_SUMMARY.value / "draft.md").write_text("草稿", encoding="utf-8")
    (prompts / "summary").mkdir()

    assert registry_problems(prompts) == [
        "summary/：資料夾名稱不是工作代碼",
        "zh_summary/draft.md：檔名要是 v<N>.md",
        "zh_summary/v2.md：沒有登記在 prompt_hashes.py",
    ]


def test_policy_checks_the_same_table_the_registry_test_checks(prompts: Path) -> None:
    content = (prompts / LlmTask.ZH_SUMMARY.value / "v1.md").read_bytes()
    assert clear_prompt_file(LlmTask.ZH_SUMMARY.value, 1, content)

    # 從同一張表拿掉這一版：registry_problems 與 policy 同時不認。
    prompt_hashes.PROMPT_FILE_HASHES = {}

    with pytest.raises(CloudClearanceError) as info:
        clear_prompt_file(LlmTask.ZH_SUMMARY.value, 1, content)
    assert info.value.reason is ErrorCode.CLOUD_PROMPT_UNREGISTERED
    assert registry_problems(prompts) != []


def test_schema_changed_without_version_bump_fails(prompts: Path) -> None:
    # 例如改了 llm_schemas.py 裡 Field(description=...)：凍結 schema 字串的雜湊就和登記的不一樣。
    file_hash, _ = prompt_hashes.PROMPT_FILE_HASHES[LlmTask.ZH_SUMMARY.value][1]
    prompt_hashes.PROMPT_FILE_HASHES = {LlmTask.ZH_SUMMARY.value: {1: (file_hash, "0" * 64)}}

    assert len(registry_problems(prompts)) == 1
    with pytest.raises(PromptIntegrityError, match="schema"):
        load_prompt(prompts, LlmTask.ZH_SUMMARY)


def test_current_version_must_be_registered(prompts: Path) -> None:
    prompt_hashes.CURRENT_PROMPT_VERSIONS = {LlmTask.ZH_SUMMARY.value: 3, "summary": 1}

    assert registry_problems(prompts) == [
        "CURRENT_PROMPT_VERSIONS 的 summary 不是工作代碼",
        "zh_summary 的目前版本 v3 沒有登記雜湊",
    ]
