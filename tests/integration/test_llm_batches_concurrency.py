"""Batch 規劃與送出的並行（Story 2.9、ADR-0041）：真的用兩個執行緒、各自的 `Database`（各自的
連線池）製造並行。佇列鎖擋不住被誤判失聯後交給另一個 worker 的工作，所以靠資料層擋：

1. 兩個 worker 同時規劃同一份資料：每個鍵只有一筆 running、item 不重複、預留總額不超過剩餘預算
   （功能預算與熔斷線各測一次）。
2. 兩個 worker 同時送出同一批：provider 只被呼叫一次。
3. 互動不拿全域預算鎖時，最後確認只攔得住已入帳的花費，超支上限＝並行中的互動呼叫數×單次費用
   （Codex 複查 R1）。
4. 兩個 worker 同時取消同一批（預算調低後再等也放不下）：只有一個成功（Codex 複查 R2）。

provider 是 `FakeLlmProvider` 的子類（batch ID 是編的）；論文、摘要都是測試自己編的。
"""

import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

import radar.services.llm_batches as llm_batches
from radar.adapters.fake import FakeLlmProvider, fake_completion
from radar.adapters.fake.jobs import FakeJobQueue
from radar.domain.ai_outputs import InputKind, LlmTask
from radar.domain.budget import month_window
from radar.domain.errors import ErrorCode
from radar.domain.llm import LlmUsage
from radar.domain.llm_batches import (
    BatchSubmissionStatus,
    LlmBatchPlanResult,
    LlmBatchSubmitResult,
    SubmitOutcome,
)
from radar.domain.pricing import llm_call_cost
from radar.domain.viewer import SystemViewer
from radar.ports import LlmProvider
from radar.repo.db import Database
from radar.services.budget import BudgetGuard, get_budget_status
from radar.services.llm_batches import (
    LlmBatchSetup,
    llm_job,
    plan_llm_batches,
    submit_llm_batch,
)
from radar.services.llm_gateway import GatewayOutcome, run_llm_job
from tests.batch_support import (
    BATCH_ID,
    NOW,
    ROOT,
    WAIT,
    GatedBatchProvider,
    batch_setup,
    batch_status,
    item_cost,
    prepared,
)
from tests.database import TestDatabase, migrate
from tests.llm_support import (
    GatedProvider,
    budget_config,
    create_paper,
    execute,
    gateway_config,
    insert_ledger,
    models_config,
)

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


def test_two_planners_never_reserve_more_than_the_breaker_allows(db: TestDatabase) -> None:
    probe = batch_setup()
    papers = _papers(_database(db))
    costs = [
        item_cost(probe, prepared(_database(db), probe, p, task))
        for p in papers
        for task in (LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL)
    ]
    # 沒有功能預算，只有熔斷線 50：本月帳記到只剩大約一半的預留放得下。
    left = sum(sorted(costs)[: len(costs) // 2], Decimal(0))
    insert_ledger(db, papers[0], LlmTask.QA_ANSWER, str(Decimal(50) - left), NOW)
    setup = batch_setup(max_requests=2)
    gate = threading.Barrier(2)

    first = _Worker(_plan(db, setup), gate)
    second = _Worker(_plan(db, setup), gate)
    results = [first.join(), second.join()]

    [(reserved,)] = execute(db, "SELECT COALESCE(sum(estimated_max_cost_usd), 0) FROM batch_item")
    assert reserved <= left
    assert sum(result.planned for result in results) >= 1
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
        return submit_llm_batch(
            SYSTEM, _database(db), lambda: provider, setup, submission_id, clock=lambda: NOW
        )

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


# --- Codex 複查 R1、R2（第二輪） ------------------------------------------------------------------

MODEL = "claude-haiku-5-5"
GOOD = '{"research_question": "團隊怎麼採用資訊系統？"}'
USAGE = LlmUsage(input_tokens=1200, output_tokens=300)


def _no_provider() -> LlmProvider:
    raise AssertionError("這條路不該建 provider")


def _reserved_by_submission(db: TestDatabase, submission_id: uuid.UUID) -> Decimal:
    [(value,)] = execute(
        db,
        "SELECT COALESCE(sum(i.estimated_max_cost_usd), 0) FROM batch_item AS i"
        " JOIN derivation_run AS r ON r.id = i.run_id"
        " WHERE i.batch_submission_id = %s AND r.status = 'running'",
        submission_id,
    )
    assert isinstance(value, Decimal)
    return value


def test_final_check_stops_only_spending_booked_before_it_and_overshoot_is_one_call(
    db: TestDatabase,
) -> None:
    """R1（ADR-0041 決定 12）：互動不拿全域預算鎖。交錯順序「互動通過預算檢查 → Batch A 最後確認
    通過、送出 → 互動才記帳」時，A 擋不到還沒入帳的互動，總額超過熔斷線；超過的量不超過並行中的
    互動呼叫數（這裡 1）× 單次費用。互動入帳之後才做最後確認的 Batch B 就被延後（只攔得住
    已入帳的）。

    閘門記的 `llm_call.created_at` 是資料庫真的現在，所以時鐘與假帳本也用真的現在。互動用正式的
    prompt（和 Batch 同一份登記），假回覆不是合格的中文重點，所以是拒收；拒收前已記帳（閘門
    第 7 步）。
    """
    now = datetime.now(UTC)
    if month_window(now) != month_window(now + timedelta(minutes=2)):
        pytest.skip("正好跨台北的月份邊界：閘門記的帳會落在下個月")
    clock = lambda: datetime.now(UTC)  # noqa: E731
    setup = batch_setup(max_requests=3, hold_hours=0)
    database = _database(db)
    _papers(database)
    plan = plan_llm_batches(
        SYSTEM, database, setup, weekly_run_id=None, clock=clock, enqueue=lambda *_: True
    )
    first, second, *rest = plan.submission_ids
    reserved = sum((_reserved_by_submission(db, s) for s in plan.submission_ids), Decimal(0))
    per_call = llm_call_cost(models_config(), MODEL, USAGE, is_batch=False).cost_usd
    gap = (per_call / 2).quantize(Decimal("0.000001"))
    assert Decimal(0) < gap < per_call
    paper = create_paper(database, doi="10.1/interactive", openalex_id="W999")
    # 帳本＋全部預留＝熔斷線−半次互動：互動通過檢查，Batch 最後確認也通過。
    insert_ledger(db, paper, LlmTask.QA_ANSWER, str(Decimal(50) - reserved - gap), now)
    guard = BudgetGuard(database, budget_config(), clock=clock)
    gated = GatedProvider(FakeLlmProvider([fake_completion(GOOD, usage=USAGE)]))
    config = gateway_config(ROOT / "prompts")
    job = llm_job(setup, paper, LlmTask.ZH_SUMMARY, InputKind.ABSTRACT)
    interactive = _Worker(
        lambda: run_llm_job(SYSTEM, _database(db), gated, config, job, budget_allows=guard),
        threading.Barrier(1),
    )
    assert gated.entered.wait(WAIT)  # 互動已通過預算檢查、在等模型

    provider = FakeLlmProvider(batch_replies=[BATCH_ID])
    sent = submit_llm_batch(SYSTEM, database, lambda: provider, setup, first, clock=clock)
    gated.release.set()
    assert interactive.join().outcome is GatewayOutcome.REJECTED
    assert gated.calls == 1
    late = submit_llm_batch(SYSTEM, database, _no_provider, setup, second, clock=clock)

    assert sent.outcome is SubmitOutcome.SUBMITTED
    assert late == LlmBatchSubmitResult(
        SubmitOutcome.HELD, BatchSubmissionStatus.QUEUED, reason=ErrorCode.BATCH_BUDGET_EXCEEDED
    )
    status = get_budget_status(SYSTEM, database, budget_config(), now=datetime.now(UTC))
    assert status.committed_usd == Decimal(50) - gap + per_call
    assert Decimal(50) < status.committed_usd <= Decimal(50) + 1 * per_call
    assert all(batch_status(db, s)[0] == "queued" for s in [second, *rest])


def test_two_workers_cancelling_the_same_batch_cancel_it_once(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2：兩個 worker 同時處理同一批、都查核完才進預算鎖：只有一個取消成功（條件式 UPDATE），
    另一個搶輸、照實回報；都不建 provider。"""
    setup = batch_setup()
    database = _database(db)
    _papers(database)
    plan = plan_llm_batches(
        SYSTEM, database, setup, weekly_run_id=None, clock=lambda: NOW, enqueue=lambda *_: True
    )
    [submission_id] = plan.submission_ids
    lowered = budget_config().model_copy(
        update={"monthly_breaker_usd": _reserved_by_submission(db, submission_id) / 2}
    )
    low_setup = batch_setup(budget=lowered)
    both_checked = threading.Barrier(2)
    original = llm_batches._check_items

    def check_then_wait(*args: Any) -> Any:
        checked = original(*args)
        both_checked.wait(WAIT)  # 兩個都查核完，才一起去拿預算鎖
        return checked

    monkeypatch.setattr(llm_batches, "_check_items", check_then_wait)

    def submit() -> LlmBatchSubmitResult:
        return submit_llm_batch(
            SYSTEM, _database(db), _no_provider, low_setup, submission_id, clock=lambda: NOW
        )

    gate = threading.Barrier(2)
    workers = [_Worker(submit, gate), _Worker(submit, gate)]
    results = sorted((worker.join() for worker in workers), key=lambda r: r.outcome.value)

    assert results == [
        LlmBatchSubmitResult(
            SubmitOutcome.FAILED,
            BatchSubmissionStatus.FAILED,
            reason=ErrorCode.BATCH_BUDGET_CANNOT_FIT,
        ),
        LlmBatchSubmitResult(SubmitOutcome.LOST_RACE, BatchSubmissionStatus.FAILED),
    ]
    assert batch_status(db, submission_id)[:3] == ("failed", None, "batch_budget_cannot_fit")
    assert execute(
        db,
        "SELECT r.status, r.failure_reason, count(*) FROM batch_item AS i"
        " JOIN derivation_run AS r ON r.id = i.run_id GROUP BY 1, 2",
    ) == [("failed", "batch_budget_cannot_fit", PAPERS * 3)]
