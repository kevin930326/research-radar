"""Batch 結果匯入測試共用的小工具（Story 2.11）：建一個已送出的批次、組假的結果行、查帳與表頭。

provider 一律是 `FakeLlmProvider`；假的批次狀態與結果行不是真的錄的，形狀照官方文件
（`docs/runs/epic-2b/api-reference.md`）。論文、摘要、模型回覆都是測試自己編的。
"""

import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from radar.adapters.fake import FakeLlmProvider, fake_batch_status
from radar.domain.ai_outputs import LlmTask
from radar.domain.llm import Completion, LlmBatchResult, LlmBatchResultKind, LlmBatchStatus
from radar.domain.llm_batches import BatchItemRun, LlmBatchPollResult
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.llm_batches import list_batch_items
from radar.services.llm_batches import (
    LlmBatchSetup,
    plan_llm_batches,
    poll_llm_batches,
    submit_llm_batch,
)
from tests.batch_support import BATCH_ID, NOW
from tests.database import TestDatabase
from tests.llm_support import create_paper, execute
from tests.trial_support import BANKING_ABSTRACT, BANKING_TITLE, REPLIES, reply

SYSTEM = SystemViewer()
# 供應商那邊結束的時間、我們去查的時間（都在 29 天保留期內）。
POLL_AT = NOW + timedelta(hours=2)


def banking_paper(database: Database, n: int = 1) -> uuid.UUID:
    """一篇有摘要的論文（三個工作都會排，回覆都過得了驗證）。"""
    return create_paper(
        database,
        doi=f"10.1/bank{n}",
        openalex_id=f"W{n}",
        title=BANKING_TITLE,
        abstract=BANKING_ABSTRACT,
    )


def submitted_batch(
    database: Database,
    setup: LlmBatchSetup,
    *,
    papers: Iterable[int] = (1,),
    batch_id: str = BATCH_ID,
) -> uuid.UUID:
    """建論文 → 規劃 → 送出（假 provider 回 `batch_id`），回 `batch_submission` 的 id。"""
    for n in papers:
        banking_paper(database, n)
    plan = plan_llm_batches(
        SYSTEM,
        database,
        setup,
        weekly_run_id=None,
        clock=lambda: NOW,
        enqueue=lambda *_: True,
        manual=True,
    )
    [submission_id] = plan.submission_ids
    sender = FakeLlmProvider(batch_replies=[batch_id])
    submit_llm_batch(SYSTEM, database, lambda: sender, setup, submission_id, clock=lambda: NOW)
    return submission_id


def batch_items(database: Database, submission_id: uuid.UUID) -> list[BatchItemRun]:
    with database.transaction() as connection:
        return list_batch_items(SYSTEM, connection, submission_id)


def by_task(database: Database, submission_id: uuid.UUID) -> dict[LlmTask, BatchItemRun]:
    """只有一篇論文的批次：工作 → item。"""
    found = batch_items(database, submission_id)
    result = {item.key.task: item for item in found}
    assert len(result) == len(found) == 3
    return result


def succeeded(item: BatchItemRun, completion: Completion | None = None) -> LlmBatchResult:
    """這個 item 成功的一行（沒給 `completion` 就用過得了驗證的回覆）。"""
    message = completion or reply(REPLIES[item.key.task])
    return LlmBatchResult(
        item.custom_id, LlmBatchResultKind.SUCCEEDED, completion=message, usage=message.usage
    )


def line(item: BatchItemRun, kind: LlmBatchResultKind, **values: Any) -> LlmBatchResult:
    return LlmBatchResult(item.custom_id, kind, **values)


def provider_with(
    lines: Iterable[LlmBatchResult | BaseException],
    *,
    status: LlmBatchStatus | None = None,
    batch_id: str = BATCH_ID,
    provider: FakeLlmProvider | None = None,
) -> FakeLlmProvider:
    """查 `batch_id` 時回 `status`（預設：已結束、有結果檔）、下載時回 `lines` 的假 provider。"""
    fake = provider or FakeLlmProvider()
    fake.batch_statuses[batch_id] = [status or fake_batch_status(batch_id)]
    fake.batch_result_lines[batch_id] = list(lines)
    return fake


def poll(
    database: Database,
    provider: FakeLlmProvider,
    setup: LlmBatchSetup,
    *,
    now: datetime = POLL_AT,
    ids: list[uuid.UUID] | None = None,
    opened: Callable[[], None] | None = None,
) -> LlmBatchPollResult:
    def open_provider() -> FakeLlmProvider:
        if opened is not None:
            opened()
        return provider

    return poll_llm_batches(SYSTEM, database, open_provider, setup, clock=lambda: now, ids=ids)


def ledger(db: TestDatabase) -> list[tuple[Any, ...]]:
    """（run_id, is_batch, cost_usd, output_reject_reason），依 run_id。"""
    return execute(
        db,
        "SELECT run_id, is_batch, cost_usd, output_reject_reason FROM llm_call ORDER BY run_id",
    )


def headers(db: TestDatabase) -> dict[uuid.UUID, tuple[str, str | None]]:
    """run_id → （表頭狀態, 拒收原因），三張表頭表一起看。"""
    rows = execute(
        db,
        "SELECT run_id, status, reject_reason FROM zh_summary"
        " UNION ALL SELECT run_id, status, reject_reason FROM classification"
        " UNION ALL SELECT run_id, status, reject_reason FROM research_model",
    )
    result = {uuid.UUID(str(run_id)): (status, reason) for run_id, status, reason in rows}
    assert len(result) == len(rows), "同一筆 run 寫了兩列表頭"
    return result


def run_states(db: TestDatabase, run_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, tuple[str, Any]]:
    """run_id → （狀態, 原因）。"""
    ids = list(run_ids)
    rows = execute(
        db, "SELECT id, status, failure_reason FROM derivation_run WHERE id = ANY(%s)", ids
    )
    return {uuid.UUID(str(run_id)): (status, reason) for run_id, status, reason in rows}


def submission_row(db: TestDatabase, submission_id: uuid.UUID) -> tuple[Any, ...]:
    """（status, provider_batch_id, failure_reason, ended_at 有沒有值, imported_at 有沒有值）。"""
    [row] = execute(
        db,
        "SELECT status, provider_batch_id, failure_reason, ended_at IS NOT NULL,"
        " imported_at IS NOT NULL FROM batch_submission WHERE id = %s",
        submission_id,
    )
    return row
