"""Batch 送出測試共用的小工具（Story 2.9）：組 `LlmBatchSetup`、算論文某個工作的 `RunKey`、
直接寫有指定鍵的 run／表頭、查批次的列。

prompt 用 repo 的 `prompts/`（正式登記），設定用 repo 的 `config/`；論文、摘要都是測試自己編的。
provider 一律是 `FakeLlmProvider`（batch ID 是編的，不是真的錄的，形狀照官方文件）。
"""

import threading
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from radar.adapters.fake import FakeLlmProvider
from radar.config import load_context_values, load_taxonomy, load_zh_summary_rules
from radar.domain.ai_outputs import CallMode, LlmTask, RunKey
from radar.domain.budget import BudgetConfig
from radar.domain.llm import LlmBatchRequest
from radar.domain.llm_batches import LlmBatchConfig
from radar.domain.pricing import max_call_cost
from radar.domain.processing import ProcessingTask, plan_for
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.papers import find_paper_llm_source, find_paper_processing_facts
from radar.services.llm_batches import LlmBatchSetup, llm_job
from radar.services.llm_gateway import PreparedLlmRequest, prepare_llm_request
from radar.services.prompt_loader import load_prompt
from tests.database import TestDatabase
from tests.llm_support import budget_config, execute, gateway_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG_ENV = {"RADAR_CONFIG_DIR": str(ROOT / "config")}
# 台北 2026-11-15 12:00（不在月初窗口）。
NOW = datetime(2026, 11, 15, 4, 0, tzinfo=UTC)
BATCH_ID = "msgbatch_01FAKEbatchIdForTests"
WAIT = 20.0


def batch_setup(
    *,
    budget: BudgetConfig | None = None,
    max_requests: int = 500,
    hold_hours: int = 8,
) -> LlmBatchSetup:
    return LlmBatchSetup(
        gateway=gateway_config(ROOT / "prompts", call_mode=CallMode.BATCH),
        budget=budget or budget_config(),
        batches=LlmBatchConfig(
            max_requests_per_batch=max_requests, month_start_hold_hours=hold_hours
        ),
        zh_summary_rules=load_zh_summary_rules(CONFIG_ENV),
        taxonomy=load_taxonomy(CONFIG_ENV),
        context_values=load_context_values(CONFIG_ENV),
    )


def prepared(
    database: Database, setup: LlmBatchSetup, paper_id: uuid.UUID, task: LlmTask
) -> PreparedLlmRequest:
    """這篇論文這個工作現在會送的請求（和規劃算的一樣）。"""
    viewer = SystemViewer()
    with database.transaction() as connection:
        facts = find_paper_processing_facts(viewer, connection, paper_id)
        source = find_paper_llm_source(viewer, connection, paper_id, for_update=False)
    assert facts is not None
    assert source is not None
    input_kind = plan_for(facts).for_task(ProcessingTask(task.value)).input_kind
    assert input_kind is not None
    job = llm_job(setup, paper_id, task, input_kind)
    result = prepare_llm_request(
        setup.gateway, job, source, load_prompt(setup.gateway.prompts_root, task)
    )
    assert isinstance(result, PreparedLlmRequest), result
    return result


def item_cost(setup: LlmBatchSetup, item: PreparedLlmRequest) -> Decimal:
    return max_call_cost(
        setup.gateway.models,
        item.key.model,
        item.estimated_input_tokens,
        item.request.max_tokens,
        is_batch=True,
    )


def insert_run(
    db: TestDatabase,
    key: RunKey,
    *,
    status: str,
    failure_reason: str | None = None,
    input_kind: str = "abstract",
) -> uuid.UUID:
    """一筆指定鍵的 `derivation_run`（互動）。"""
    (row,) = execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
        " input_sha256, input_kind, params, code_version, call_mode, status, failure_reason,"
        " finished_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, 'test', 'interactive', %s, %s,"
        " CASE WHEN %s = 'running' THEN NULL ELSE now() END) RETURNING id",
        key.paper_id,
        key.task.value,
        key.prompt_version,
        "a" * 64,
        key.model,
        key.input_sha256,
        input_kind,
        status,
        failure_reason,
        status,
    )
    return uuid.UUID(str(row[0]))


ZH_CONTENT = {
    "research_question": "遠距團隊如何採用協作平台？",
    "method": "問卷調查",
    "theory": "科技接受模型",
    "findings": None,
}


def insert_zh_header(db: TestDatabase, key: RunKey, status: str) -> None:
    """這組鍵的中文重點表頭（`active` 或 `rejected`），連同一筆做完的 run。"""
    run_id = insert_run(db, key, status="succeeded")
    content = ZH_CONTENT if status == "active" else {}
    columns = "".join(f", {name}" for name in content)
    placeholders = "".join(", %s" for _ in content)
    execute(
        db,
        "INSERT INTO zh_summary (paper_id, run_id, input_kind, input_sha256, lang, status,"
        f" reject_reason{columns})"
        f" SELECT paper_id, id, input_kind, input_sha256, 'zh-Hant', %s, %s{placeholders}"
        " FROM derivation_run WHERE id = %s",
        status,
        "llm_output_invalid" if status == "rejected" else None,
        *content.values(),
        run_id,
    )


def insert_pending_run(db: TestDatabase, key: RunKey) -> uuid.UUID:
    """已經付過錢、只差寫結果的 run（`llm_result_pending`＋存了輸出的 `llm_call`）。"""
    run_id = insert_run(db, key, status="failed", failure_reason="llm_result_pending")
    execute(
        db,
        "INSERT INTO llm_call (run_id, input_tokens, output_tokens, cache_creation_input_tokens,"
        " cache_read_input_tokens, cost_usd, price_tier, is_batch, output_text)"
        " VALUES (%s, 1, 1, 0, 0, 0.000001, 'standard', false, '{}')",
        run_id,
    )
    return run_id


def submissions(db: TestDatabase) -> list[tuple[Any, ...]]:
    """（id, weekly_run_id, status, provider_batch_id, item_count, estimated_max_cost_usd,
    failure_reason），依建立順序。"""
    return execute(
        db,
        "SELECT id, weekly_run_id, status, provider_batch_id, item_count,"
        " estimated_max_cost_usd, failure_reason FROM batch_submission ORDER BY created_at, id",
    )


def items(db: TestDatabase, submission_id: uuid.UUID | None = None) -> list[tuple[Any, ...]]:
    """（item id, run id, task, run 狀態, call_mode, run 原因, 預留），依 item id。"""
    where = "" if submission_id is None else " WHERE i.batch_submission_id = %s"
    params: tuple[object, ...] = () if submission_id is None else (submission_id,)
    return execute(
        db,
        "SELECT i.id, r.id, r.task, r.status, r.call_mode, r.failure_reason,"
        " i.estimated_max_cost_usd"
        " FROM batch_item AS i JOIN derivation_run AS r ON r.id = i.run_id"
        f"{where} ORDER BY i.id",
        *params,
    )


def batch_status(db: TestDatabase, submission_id: uuid.UUID) -> tuple[Any, ...]:
    """（status, provider_batch_id, failure_reason, item_count, submitted_at 有沒有值）。"""
    [row] = execute(
        db,
        "SELECT status, provider_batch_id, failure_reason, item_count, submitted_at IS NOT NULL"
        " FROM batch_submission WHERE id = %s",
        submission_id,
    )
    return row


class GatedBatchProvider(FakeLlmProvider):
    """`submit_batch` 先通知 `entered`、等 `release` 才回覆；呼叫次數有鎖（兩個執行緒共用）。

    `during_call`：等完之後、回覆之前在呼叫的執行緒裡執行（例如把批次標 `needs_attention`）。
    """

    def __init__(
        self,
        replies: Sequence[str | BaseException] = (BATCH_ID,),
        *,
        gated: bool = False,
        during_call: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(batch_replies=replies)
        self.entered = threading.Event()
        self.release = threading.Event()
        if not gated:
            self.release.set()
        self.during_call = during_call
        self.calls = 0
        self._lock = threading.Lock()

    def submit_batch(self, requests: Sequence[LlmBatchRequest]) -> str:
        with self._lock:
            self.calls += 1
        self.entered.set()
        if not self.release.wait(WAIT):
            raise AssertionError("GatedBatchProvider 等不到 release")
        if self.during_call is not None:
            self.during_call()
        with self._lock:
            return super().submit_batch(requests)
