"""`radar runs trial`（Story 2.10）測試共用的小工具：依 schema 回覆的模型替身、試跑設定、
找得到落在調整組／測試組的 DOI、造論文。

假回覆不是真的錄的，形狀照官方文件（`radar/adapters/fake/llm.py` 的 `fake_completion`）；
內容沿用 `tests/integration/test_llm_batches_plan.py` 編的行動銀行論文與三個回覆（都過得了驗證）。
prompt 用 repo 的 `prompts/`（正式登記），設定用 repo 的 `config/`。
"""

import json
import threading
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from radar.adapters.fake import fake_completion
from radar.config import load_context_values, load_taxonomy, load_zh_summary_rules
from radar.domain.ai_outputs import LlmTask
from radar.domain.budget import BudgetConfig
from radar.domain.eval_split import EvalSplit, split_for_doi
from radar.domain.llm import (
    Completion,
    LlmBatchRequest,
    LlmRequest,
    LlmResult,
    LlmUsage,
    interpret_completion,
    snapshot_llm_request,
)
from radar.repo.db import Database
from radar.services.prompt_loader import load_prompt
from radar.services.trial_runs import TrialSetup
from tests.integration.test_llm_batches_plan import (
    _BANKING_ABSTRACT,
    _BANKING_TITLE,
    _CLASSIFY_REPLY,
    _MODEL_REPLY,
    _ZH_REPLY,
)
from tests.llm_support import budget_config, create_paper, gateway_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG_ENV = {"RADAR_CONFIG_DIR": str(ROOT / "config")}
BANKING_TITLE = _BANKING_TITLE
BANKING_ABSTRACT = _BANKING_ABSTRACT
REPLIES: Mapping[LlmTask, dict[str, Any]] = {
    LlmTask.ZH_SUMMARY: _ZH_REPLY,
    LlmTask.CLASSIFY: _CLASSIFY_REPLY,
    LlmTask.EXTRACT_MODEL: _MODEL_REPLY,
}
WAIT = 20.0

type Reply = Completion | BaseException


def reply(fields: Mapping[str, Any] | str, *, output_tokens: int = 120) -> Completion:
    """一個假回覆（`fields` 是 dict 就轉 JSON；是字串就原樣當模型回的文字）。"""
    text = fields if isinstance(fields, str) else json.dumps(fields, ensure_ascii=False)
    return fake_completion(
        text,
        thinking=True,
        usage=LlmUsage(input_tokens=900, output_tokens=output_tokens),
        raw={
            "id": "msg_fake_trial",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [{"type": "thinking"}, {"type": "text"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "stop_details": None,
            "usage": {
                "input_tokens": 900,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    )


def good_replies() -> dict[LlmTask, Reply]:
    return {task: reply(fields) for task, fields in REPLIES.items()}


class SchemaReplyProvider:
    """依請求的結構化輸出 schema（＝哪個工作）回覆的模型替身；多個執行緒共用也安全。

    - `replies`：工作 → 回覆（`Completion` 走和真 adapter 一樣的 `interpret_completion`；例外原樣
      丟出，例如 `LlmTimeoutError`、`KeyboardInterrupt`）。每次呼叫都用同一個回覆。
    - `gated`：每次呼叫先通知 `entered`、等 `release` 才回覆（並行測試用）。
    - `calls`：各工作被呼叫幾次。
    """

    def __init__(self, replies: Mapping[LlmTask, Reply], *, gated: bool = False) -> None:
        schemas: dict[type[BaseModel], LlmTask] = {}
        for task in replies:
            schema = load_prompt(ROOT / "prompts", task).schema_class
            assert schema is not None
            schemas[schema] = task
        self._task_of = schemas
        self._replies = dict(replies)
        self.calls: Counter[LlmTask] = Counter()
        self.entered = threading.Event()
        self.release = threading.Event()
        if not gated:
            self.release.set()
        self._lock = threading.Lock()

    @property
    def total_calls(self) -> int:
        with self._lock:
            return sum(self.calls.values())

    def complete(self, request: LlmRequest) -> LlmResult:
        checked = snapshot_llm_request(request)
        assert checked.output_schema is not None
        task = self._task_of[checked.output_schema]
        with self._lock:
            self.calls[task] += 1
        self.entered.set()
        if not self.release.wait(WAIT):
            raise AssertionError("SchemaReplyProvider 等不到 release")
        answer = self._replies[task]
        if isinstance(answer, BaseException):
            raise answer
        return interpret_completion(checked, answer)

    def submit_batch(self, requests: Sequence[LlmBatchRequest]) -> str:
        raise AssertionError("試跑不送 Batch")


def trial_setup(budget: BudgetConfig | None = None) -> TrialSetup:
    return TrialSetup(
        gateway=gateway_config(ROOT / "prompts"),
        budget=budget or budget_config(),
        zh_summary_rules=load_zh_summary_rules(CONFIG_ENV),
        taxonomy=load_taxonomy(CONFIG_ENV),
        context_values=load_context_values(CONFIG_ENV),
    )


def doi_in(split: EvalSplit, prefix: str, *, skip: int = 0) -> str:
    """`10.<prefix>/<n>` 裡第 `skip`+1 個落在 `split` 的 DOI（切分是固定的，所以結果固定）。"""
    found = 0
    for n in range(10_000):
        doi = f"10.{prefix}/trial{n}"
        if split_for_doi(doi) is split:
            if found == skip:
                return doi
            found += 1
    raise AssertionError("找不到落在這一組的 DOI")


def banking_paper(
    database: Database, doi: str, n: int, *, abstract: str | None = BANKING_ABSTRACT
) -> uuid.UUID:
    """一篇過得了三個驗證的行動銀行論文（`n` 讓 OpenAlex ID 不重複）。"""
    return create_paper(
        database, doi=doi, openalex_id=f"W{9000 + n}", title=BANKING_TITLE, abstract=abstract
    )
