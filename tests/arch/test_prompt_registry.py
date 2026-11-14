"""prompt 檔與登記（`prompts/registry.json`）一定對得上（AD-8、ADR-0030）：

- 真的 `prompts/`：`registry_problems` 是空的（2.2 還沒有正式的 prompt，登記是 `{}`）。
- 在 tmp 示範：改了模板或設定區卻沒升版（沒有新增 `v<N+1>.md`、也沒更新登記）會被抓到，
  `load_prompt` 也拒絕讀；只寫了檔沒登記、資料夾或檔名不對也會被抓到。
"""

from pathlib import Path

import pytest

from radar.domain.ai_outputs import LlmTask
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
        "zh_summary/v2.md：沒有登記在 registry.json",
    ]
