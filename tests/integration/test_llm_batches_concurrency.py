"""Batch 規劃與送出的並行（Story 2.9、ADR-0041）：真的用兩個執行緒、各自的 `Database`（各自的
連線池）製造並行。佇列鎖擋不住被誤判失聯後交給另一個 worker 的工作，所以靠資料層擋：

1. 兩個 worker 同時規劃同一份資料：每個鍵只有一筆 running、item 不重複、預留總額不超過剩餘預算。
2. 兩個 worker 同時送出同一批：provider 只被呼叫一次。

provider 是 `FakeLlmProvider` 的子類（batch ID 是編的）；論文、摘要都是測試自己編的。
"""

import threading
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from radar.adapters.fake.jobs import FakeJobQueue
from radar.domain.ai_outputs import LlmTask
from radar.domain.llm_batches import LlmBatchPlanResult, LlmBatchSubmitResult, SubmitOutcome
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.services.llm_batches import LlmBatchSetup, plan_llm_batches, submit_llm_batch
from tests.batch_support import (
    BATCH_ID,
    NOW,
    WAIT,
    GatedBatchProvider,
    batch_setup,
    batch_status,
    item_cost,
    prepared,
)
from tests.database import TestDatabase, migrate
from tests.llm_support import budget_config, create_paper, execute

SYSTEM = SystemViewer()
PAPERS = 6


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


def _papers(database: Database) -> list[uuid.UUID]:
    return [
        create_paper(
            database,
            doi=f"10.1/concurrent{n}",
            openalex_id=f"W{n}",
            title=f"Collaboration platforms in remote teams, part {n}",
            abstract=f"We survey {n} remote teams about collaboration platforms.",
        )
        for n in range(1, PAPERS + 1)
    ]


class _Worker[T]:
    """在背景執行緒跑一次（模擬另一個 worker）；`gate` 讓兩個同時起跑。"""

    def __init__(self, work: Callable[[], T], gate: threading.Barrier) -> None:
        self.result: T | None = None
        self.error: BaseException | None = None

        def target() -> None:
            try:
                gate.wait(WAIT)
                self.result = work()
            except BaseException as exc:
                self.error = exc

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()

    def join(self) -> T:
        self.thread.join(WAIT * 2)
        assert not self.thread.is_alive(), "worker 沒有結束"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _plan(db: TestDatabase, setup: LlmBatchSetup) -> Callable[[], LlmBatchPlanResult]:
    database = _database(db)
    queue = FakeJobQueue()
    return lambda: plan_llm_batches(
        SYSTEM, database, setup, weekly_run_id=None, clock=lambda: NOW, enqueue=queue.enqueue
    )


def test_two_planners_at_once_claim_each_key_once(db: TestDatabase) -> None:
    setup = batch_setup(max_requests=4)
    _papers(_database(db))
    gate = threading.Barrier(2)

    first = _Worker(_plan(db, setup), gate)
    second = _Worker(_plan(db, setup), gate)
    results = [first.join(), second.join()]

    assert sum(result.planned for result in results) == PAPERS * 3
    assert sum(result.skipped for result in results) == PAPERS * 3
    assert execute(
        db,
        "SELECT count(*) FROM (SELECT paper_id, task, prompt_version, model, input_sha256"
        " FROM derivation_run WHERE status = 'running'"
        " GROUP BY 1, 2, 3, 4, 5 HAVING count(*) > 1) AS dup",
    ) == [(0,)]
    assert execute(db, "SELECT count(*) FROM derivation_run") == [(PAPERS * 3,)]
    assert execute(db, "SELECT count(*), count(DISTINCT run_id) FROM batch_item") == [
        (PAPERS * 3, PAPERS * 3)
    ]
    assert execute(db, "SELECT sum(item_count) FROM batch_submission") == [(PAPERS * 3,)]


def test_two_planners_never_reserve_more_than_the_task_budget(db: TestDatabase) -> None:
    probe = batch_setup()
    papers = _papers(_database(db))
    costs = [
        item_cost(probe, prepared(_database(db), probe, p, LlmTask.ZH_SUMMARY)) for p in papers
    ]
    # 中文重點的功能預算大約放得下一半；兩個 worker 同時規劃，加起來也不能超過。
    budget = sum(sorted(costs)[: PAPERS // 2], Decimal(0))
    setup = batch_setup(budget=budget_config(zh_summary=str(budget)), max_requests=2)
    gate = threading.Barrier(2)

    first = _Worker(_plan(db, setup), gate)
    second = _Worker(_plan(db, setup), gate)
    results = [first.join(), second.join()]

    [(reserved,)] = execute(
        db,
        "SELECT COALESCE(sum(i.estimated_max_cost_usd), 0) FROM batch_item AS i"
        " JOIN derivation_run AS r ON r.id = i.run_id WHERE r.task = 'zh_summary'",
    )
    assert reserved <= budget
    [(zh_runs,)] = execute(db, "SELECT count(*) FROM derivation_run WHERE task = 'zh_summary'")
    assert zh_runs >= 1
    assert sum(result.deferred_budget for result in results) > 0


def test_two_submitters_at_once_call_the_provider_once(db: TestDatabase) -> None:
    setup = batch_setup()
    database = _database(db)
    _papers(database)
    plan = plan_llm_batches(
        SYSTEM, database, setup, weekly_run_id=None, clock=lambda: NOW, enqueue=lambda *_: True
    )
    [submission_id] = plan.submission_ids
    provider = GatedBatchProvider(gated=True)
    gate = threading.Barrier(2)

    def submit() -> LlmBatchSubmitResult:
        return submit_llm_batch(SYSTEM, _database(db), provider, setup, submission_id)

    first = _Worker(submit, gate)
    second = _Worker(submit, gate)
    assert provider.entered.wait(WAIT)
    # 一個送出者停在 submit_batch 裡（列已是 submitting）；另一個必定搶輸或看到 submitting 未過期。
    deadline = time.monotonic() + WAIT
    while first.thread.is_alive() and second.thread.is_alive():
        assert time.monotonic() < deadline, "另一個送出者沒有結束"
        time.sleep(0.05)
    provider.release.set()
    outcomes: list[Any] = sorted(worker.join().outcome.value for worker in (first, second))

    assert provider.calls == 1
    assert len(provider.batch_requests) == 1
    assert SubmitOutcome.SUBMITTED.value in outcomes
    assert set(outcomes) - {SubmitOutcome.SUBMITTED.value} <= {
        SubmitOutcome.LOST_RACE.value,
        SubmitOutcome.NOT_STALE.value,
    }
    assert batch_status(db, submission_id)[:2] == ("submitted", BATCH_ID)
