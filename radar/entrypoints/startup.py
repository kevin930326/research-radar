"""worker 與 CLI 啟動時的 AI 設定檢查（AD-18、Story 2.9、ADR-0041）。

`check_ai_config` 一次檢查 AI 工作要用的全部設定，有問題就把每一條一起列出、丟 `ConfigError`，
呼叫端拒絕啟動：

- prompt 檔與 `domain/prompt_hashes.py` 的登記（`registry_problems`）。
- `models.yaml`、`budget.yaml`、`zh_summary.yaml`、`taxonomy.yaml`（含雜湊登記）、
  `context_values.yaml`（含雜湊登記）、`llm_batches.yaml`。

只讀設定檔與 prompt 檔，不讀秘密、不連線；訊息只有檔名、欄位路徑與原因。api 不接（之後由主對話
決定）。
"""

from collections.abc import Callable
from pathlib import Path

from radar.config import (
    ConfigError,
    load_budget_config,
    load_context_values,
    load_llm_batch_config,
    load_models_config,
    load_taxonomy,
    load_zh_summary_rules,
)
from radar.entrypoints.bootstrap import DEFAULT_PROMPTS_DIR
from radar.services.prompt_loader import registry_problems

_LOADERS: tuple[Callable[[], object], ...] = (
    load_models_config,
    load_budget_config,
    load_zh_summary_rules,
    load_taxonomy,
    load_context_values,
    load_llm_batch_config,
)


def check_ai_config(prompts_root: Path = DEFAULT_PROMPTS_DIR) -> None:
    """檢查 AI 設定（見模組說明），全部問題一起回報。

    Raises:
        ConfigError: 任何一項有問題；`problems` 列出每一條。
    """
    problems = [f"prompt 登記：{problem}" for problem in registry_problems(prompts_root)]
    for load in _LOADERS:
        try:
            load()
        except ConfigError as exc:
            problems.extend(exc.problems)
    if problems:
        raise ConfigError(problems)
