"""Batch 規劃（`plan_llm_batches`，Story 2.9、ADR-0041）對真的 PostgreSQL：該排的排、不該排的
不排、月初窗口與熔斷整次不寫列、預算放不下的不建列、預留算進剩餘、重跑不建新列、批次大小、
排入送出在 commit 之後。

論文、摘要都是測試自己編的；prompt 與設定用 repo 的正式檔。
"""

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from structlog.testing import capture_logs

import radar.services.llm_batches as llm_batches
from radar.adapters.fake import FakeLlmProvider, fake_completion
from radar.adapters.fake.jobs import FakeJobQueue, QueuedJob
from radar.domain.ai_outputs import LlmTask
from radar.domain.fingerprints import LLM_BATCH_BUDGET_LOCK_KEY
from radar.domain.jobs import SUBMIT_LLM_BATCH, JobSpec
from radar.domain.llm import LlmUsage
from radar.domain.llm_batches import LlmBatchPlanResult, PlanStop
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.llm_batches import create_batch_item
from radar.services.classification import run_classification
from radar.services.llm_batches import LlmBatchSetup, plan_llm_batches
from radar.services.llm_gateway import GatewayOutcome, run_llm_job
from radar.services.research_model import run_research_model
from radar.services.zh_summary import run_zh_summary
from tests.batch_support import (
    NOW,
    ROOT,
    batch_setup,
    insert_pending_run,
    insert_run,
    insert_zh_header,
    item_cost,
    items,
    prepared,
    submissions,
)
from tests.database import TestDatabase, migrate
from tests.llm_support import (
    budget_config,
    create_paper,
    execute,
    gateway_config,
    insert_ledger,
)

SYSTEM = SystemViewer()
TAIPEI = ZoneInfo("Asia/Taipei")


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def weekly_run_id(db: TestDatabase) -> uuid.UUID:
    [(run_id,)] = execute(db, "INSERT INTO weekly_run (status) VALUES ('published') RETURNING id")
    return uuid.UUID(str(run_id))


def _paper(database: Database, n: int, abstract: str | None = "default") -> uuid.UUID:
    text = (
        None
        if abstract is None
        else f"We survey {n} remote teams about collaboration platforms ({abstract})."
    )
    return create_paper(
        database,
        doi=f"10.1/batch{n}",
        openalex_id=f"W{n}",
        title=f"Collaboration platforms in remote teams, part {n}",
        abstract=text,
    )


def _plan(
    database: Database,
    setup: LlmBatchSetup,
    *,
    queue: FakeJobQueue | None = None,
    weekly_run_id: uuid.UUID | None = None,
    now: datetime = NOW,
) -> LlmBatchPlanResult:
    return plan_llm_batches(
        SYSTEM,
        database,
        setup,
        weekly_run_id=weekly_run_id,
        clock=lambda: now,
        enqueue=(queue or FakeJobQueue()).enqueue,
    )


def _counts(db: TestDatabase) -> Mapping[str, int]:
    return {
        table: int(execute(db, f"SELECT count(*) FROM {table}")[0][0])
        for table in ("derivation_run", "batch_submission", "batch_item")
    }


def test_plans_what_plan_for_and_should_run_allow(
    db: TestDatabase, database: Database, weekly_run_id: uuid.UUID
) -> None:
    setup = batch_setup()
    old = _paper(database, 1)
    no_abstract = _paper(database, 2, abstract=None)
    excluded = _paper(database, 3)
    execute(db, "UPDATE paper SET excluded_reason = 'editors_comments' WHERE id = %s", excluded)
    active = _paper(database, 4)
    insert_zh_header(db, prepared(database, setup, active, LlmTask.ZH_SUMMARY).key, "active")
    rejected = _paper(database, 5)
    insert_zh_header(db, prepared(database, setup, rejected, LlmTask.ZH_SUMMARY).key, "rejected")
    failed = _paper(database, 6)
    failed_key = prepared(database, setup, failed, LlmTask.ZH_SUMMARY).key
    insert_run(db, failed_key, status="failed", failure_reason="llm_timeout")
    queue = FakeJobQueue()

    result = _plan(database, setup, queue=queue, weekly_run_id=weekly_run_id)

    assert result.stop is PlanStop.COMPLETED
    # 舊的 3＋沒摘要只做分類 1＋已有 active 的另外兩個 2＋rejected 的另外兩個 2＋上次失敗的 3。
    assert result.planned == 11
    assert result.skipped == 2
    assert (result.resumable, result.deferred_budget) == (0, 0)
    assert (result.no_input, result.blocked, result.too_large) == (0, 0, 0)
    [(submission_id, run_ref, status, batch_id, count, cost, reason)] = submissions(db)
    assert result.submission_ids == [submission_id]
    assert (run_ref, status, batch_id, count, reason) == (weekly_run_id, "queued", None, 11, None)
    rows = items(db)
    assert len(rows) == 11
    assert {(row[3], row[4], row[5]) for row in rows} == {("running", "batch", None)}
    assert cost == sum((row[6] for row in rows), Decimal(0)) == result.estimated_max_cost_usd
    assert cost > 0
    planned_papers = {
        (paper_id, task)
        for paper_id, task in execute(
            db,
            "SELECT r.paper_id, r.task FROM batch_item AS i"
            " JOIN derivation_run AS r ON r.id = i.run_id",
        )
    }
    assert (no_abstract, "classify") in planned_papers
    assert (no_abstract, "zh_summary") not in planned_papers
    assert not {task for paper_id, task in planned_papers if paper_id == excluded}
    assert (active, "zh_summary") not in planned_papers
    assert (rejected, "zh_summary") not in planned_papers
    assert (failed, "zh_summary") in planned_papers
    assert {task for paper_id, task in planned_papers if paper_id == old} == {
        "zh_summary",
        "classify",
        "extract_model",
    }
    assert queue.queued == [QueuedJob(SUBMIT_LLM_BATCH, {"batch_submission_id": submission_id})]


def test_month_start_hold_writes_nothing(db: TestDatabase, database: Database) -> None:
    _paper(database, 1)
    queue = FakeJobQueue()

    with capture_logs() as logs:
        result = _plan(
            database, batch_setup(), queue=queue, now=datetime(2026, 12, 1, 7, 59, tzinfo=TAIPEI)
        )

    assert result.stop is PlanStop.HELD_MONTH_START
    assert result.planned == 0
    assert _counts(db) == {"derivation_run": 0, "batch_submission": 0, "batch_item": 0}
    assert queue.queued == []
    assert [log["event"] for log in logs] == ["月初窗口，Batch 規劃延後"]


def test_after_the_hold_window_plans_normally(db: TestDatabase, database: Database) -> None:
    _paper(database, 1)

    result = _plan(database, batch_setup(), now=datetime(2026, 12, 1, 8, 0, tzinfo=TAIPEI))

    assert result.stop is PlanStop.COMPLETED
    assert result.planned == 3


def test_tripped_breaker_writes_nothing(db: TestDatabase, database: Database) -> None:
    paper_id = _paper(database, 1)
    insert_ledger(db, paper_id, LlmTask.QA_ANSWER, "50", NOW)
    queue = FakeJobQueue()

    result = _plan(database, batch_setup(), queue=queue)

    assert result.stop is PlanStop.BUDGET_TRIPPED
    assert _counts(db)["batch_item"] == 0
    assert _counts(db)["batch_submission"] == 0
    assert queue.queued == []


def test_task_budget_fits_only_some_items(db: TestDatabase, database: Database) -> None:
    setup = batch_setup()
    papers = [_paper(database, n) for n in range(1, 4)]
    costs = [item_cost(setup, prepared(database, setup, p, LlmTask.ZH_SUMMARY)) for p in papers]
    # 中文重點的功能預算只放得下最大的那一筆（其餘兩筆計 deferred_budget、不建列）。
    budget = max(costs)
    tight = batch_setup(budget=budget_config(zh_summary=str(budget)))

    result = _plan(database, tight)

    assert result.planned == 2 * 3 + 1
    assert result.deferred_budget == 2
    planned_zh = execute(
        db,
        "SELECT count(*) FROM batch_item AS i JOIN derivation_run AS r ON r.id = i.run_id"
        " WHERE r.task = 'zh_summary'",
    )
    assert planned_zh == [(1,)]
    assert execute(db, "SELECT count(*) FROM derivation_run WHERE task = 'zh_summary'") == [(1,)]


def test_unfinished_batch_reservation_counts_against_the_breaker(
    db: TestDatabase, database: Database
) -> None:
    setup = batch_setup()
    first = _paper(database, 1)
    _plan(database, setup)
    reserved = execute(db, "SELECT sum(estimated_max_cost_usd) FROM batch_item")[0][0]
    second = _paper(database, 2)
    second_cost = min(
        item_cost(setup, prepared(database, setup, second, task))
        for task in (LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL)
    )
    # 帳本記到「熔斷線 − 預留 − 最便宜一筆的一半」：不算預留時放得下，算了就放不下。
    spent = Decimal(50) - reserved - (second_cost / 2).quantize(Decimal("0.000001"))
    insert_ledger(db, first, LlmTask.QA_ANSWER, str(spent), NOW)

    result = _plan(database, setup)

    assert result.planned == 0
    # 預算先檢查（在問 should_run 之前），所以第一篇已在跑的三筆也計 deferred_budget（ADR-0041）。
    assert result.deferred_budget == 6
    assert execute(db, "SELECT count(*) FROM derivation_run WHERE paper_id = %s", second) == [(0,)]


def test_finished_runs_release_their_reservation(db: TestDatabase, database: Database) -> None:
    setup = batch_setup()
    first = _paper(database, 1)
    _plan(database, setup)
    reserved = execute(db, "SELECT sum(estimated_max_cost_usd) FROM batch_item")[0][0]
    insert_ledger(db, first, LlmTask.QA_ANSWER, str(Decimal(50) - reserved), NOW)
    _paper(database, 2)
    assert _plan(database, setup).planned == 0
    # 結果匯入（2.11）把 run 改掉後預留自然釋放；這裡直接把 run 改成 failed 模擬。
    execute(
        db,
        "UPDATE derivation_run SET status = 'failed', failure_reason = 'llm_timeout',"
        " finished_at = now() WHERE call_mode = 'batch'",
    )

    result = _plan(database, setup)

    # 釋放的剛好是第一篇三筆的預留；第二篇（同樣長短的摘要）三筆放得下，第一篇失敗的三筆也再排。
    assert result.planned + result.deferred_budget == 6
    assert result.planned >= 3


def test_rerun_with_same_input_creates_nothing(db: TestDatabase, database: Database) -> None:
    setup = batch_setup()
    for n in range(1, 4):
        _paper(database, n)
    queue = FakeJobQueue()
    first = _plan(database, setup, queue=queue)
    before = _counts(db)

    second = _plan(database, setup, queue=queue)

    assert first.planned == 9
    assert second.planned == 0
    assert second.skipped == 9
    assert second.submission_ids == []
    assert _counts(db) == before
    assert len(queue.queued) == 1


def test_resumable_pending_run_is_left_for_interactive(
    db: TestDatabase, database: Database
) -> None:
    setup = batch_setup()
    paper_id = _paper(database, 1)
    pending = insert_pending_run(db, prepared(database, setup, paper_id, LlmTask.ZH_SUMMARY).key)

    result = _plan(database, setup)

    assert result.resumable == 1
    assert result.planned == 2
    assert execute(
        db, "SELECT status, failure_reason FROM derivation_run WHERE id = %s", pending
    ) == [("failed", "llm_result_pending")]


def test_batches_are_split_by_max_requests(db: TestDatabase, database: Database) -> None:
    for n in range(1, 5):
        _paper(database, n)
    queue = FakeJobQueue()

    result = _plan(database, batch_setup(max_requests=5), queue=queue)

    assert result.planned == 12
    rows = submissions(db)
    assert [row[4] for row in rows] == [5, 5, 2]
    assert [row[0] for row in rows] == result.submission_ids
    assert [job.ids["batch_submission_id"] for job in queue.queued] == result.submission_ids
    for row in rows:
        assert len(items(db, row[0])) == row[4]
        assert row[5] == sum((item[6] for item in items(db, row[0])), Decimal(0))


def test_enqueue_happens_after_commit_and_failure_is_only_a_warning(
    db: TestDatabase, database: Database
) -> None:
    _paper(database, 1)
    seen: list[tuple[str, int]] = []

    def enqueue(job: JobSpec, ids: Mapping[str, uuid.UUID]) -> bool:
        # 別的連線看得到已 commit 的批次與筆數。
        [(status, count)] = execute(
            db,
            "SELECT status, item_count FROM batch_submission WHERE id = %s",
            ids["batch_submission_id"],
        )
        seen.append((status, count))
        raise RuntimeError("queue down")

    with capture_logs() as logs:
        result = plan_llm_batches(
            SYSTEM, database, batch_setup(), weekly_run_id=None, clock=lambda: NOW, enqueue=enqueue
        )

    assert seen == [("queued", 3)]
    assert result.enqueue_failed == 1
    assert submissions(db)[0][2] == "queued"
    [warning] = [log for log in logs if log["event"].startswith("Batch 建好了，排入送出失敗")]
    assert warning["log_level"] == "warning"
    assert warning["error_type"] == "RuntimeError"


def test_no_candidates_creates_no_submission(db: TestDatabase, database: Database) -> None:
    _paper(database, 1, abstract=None)
    execute(db, "UPDATE paper SET excluded_reason = 'editors_comments'")
    queue = FakeJobQueue()

    result = _plan(database, batch_setup(), queue=queue)

    assert result.planned == 0
    assert submissions(db) == []
    assert queue.queued == []


def test_logs_have_counts_but_no_document_text(db: TestDatabase, database: Database) -> None:
    _paper(database, 1, abstract="SECRET-ABSTRACT-MARKER")

    with capture_logs() as logs:
        _plan(database, batch_setup())

    assert "SECRET-ABSTRACT-MARKER" not in str(logs)
    [done] = [log for log in logs if log["event"] == "Batch 規劃完成"]
    assert (done["planned"], done["stop"]) == (3, "completed")


def test_interactive_call_skips_a_key_already_running_in_a_batch(
    db: TestDatabase, database: Database
) -> None:
    setup = batch_setup()
    paper_id = _paper(database, 1)
    _plan(database, setup)
    job = prepared(database, setup, paper_id, LlmTask.ZH_SUMMARY).job

    result = run_llm_job(
        SYSTEM,
        database,
        FakeLlmProvider(),  # 沒有準備回覆：呼叫了就會丟錯
        gateway_config(ROOT / "prompts"),
        job,
        budget_allows=lambda _task: True,
    )

    assert result.outcome is GatewayOutcome.SKIPPED
    assert execute(db, "SELECT count(*) FROM derivation_run WHERE task = 'zh_summary'") == [(1,)]


def test_disabled_weekly_planning_writes_nothing(db: TestDatabase, database: Database) -> None:
    _paper(database, 1)
    queue = FakeJobQueue()

    with capture_logs() as logs:
        result = _plan(database, batch_setup(enabled=False), queue=queue)

    assert result == LlmBatchPlanResult(stop=PlanStop.DISABLED)
    assert _counts(db) == {"derivation_run": 0, "batch_submission": 0, "batch_item": 0}
    assert queue.queued == []
    assert [(log["event"], log["log_level"]) for log in logs] == [
        ("每週 Batch 規劃沒有開（Story 2.11），不排", "info")
    ]


def test_planning_that_crosses_into_the_month_start_hold_stops(
    db: TestDatabase, database: Database
) -> None:
    _paper(database, 1)
    now = [datetime(2026, 11, 30, 23, 59, tzinfo=TAIPEI)]

    def enqueue(job: JobSpec, ids: Mapping[str, uuid.UUID]) -> bool:
        # 第一批 commit 之後時鐘跨過台北午夜，進到 12 月 1 日的月初窗口。
        now[0] = datetime(2026, 12, 1, 0, 30, tzinfo=TAIPEI)
        return True

    result = plan_llm_batches(
        SYSTEM,
        database,
        batch_setup(max_requests=1),
        weekly_run_id=None,
        clock=lambda: now[0],
        enqueue=enqueue,
    )

    assert result.stop is PlanStop.HELD_MONTH_START
    assert result.planned == 1
    assert len(submissions(db)) == 1
    assert _counts(db)["batch_item"] == 1


def test_task_ledger_spending_counts_against_the_task_budget(
    db: TestDatabase, database: Database
) -> None:
    setup = batch_setup()
    paper_id = _paper(database, 1)
    cost = item_cost(setup, prepared(database, setup, paper_id, LlmTask.ZH_SUMMARY))
    # 中文重點本月已花「功能預算 − 這一筆的一半」：熔斷線還很寬，只有功能預算放不下。
    insert_ledger(
        db,
        paper_id,
        LlmTask.ZH_SUMMARY,
        str(Decimal(1) - (cost / 2).quantize(Decimal("0.000001"))),
        NOW,
    )

    result = _plan(database, batch_setup(budget=budget_config(zh_summary="1")))

    assert (result.planned, result.deferred_budget) == (2, 1)
    planned_zh = execute(
        db,
        "SELECT count(*) FROM batch_item AS i JOIN derivation_run AS r ON r.id = i.run_id"
        " WHERE r.task = 'zh_summary'",
    )
    assert planned_zh == [(0,)]


def test_candidates_past_the_attempt_cap_carry_over_to_the_next_batch(
    db: TestDatabase, database: Database
) -> None:
    _paper(database, 1)
    _plan(database, batch_setup())
    second = _paper(database, 2)

    # 一批 1 筆、一個交易最多嘗試 2 筆：第一篇已在跑的 3 筆超過上限，第二篇要留到之後的批次。
    result = _plan(database, batch_setup(max_requests=1))

    assert (result.skipped, result.planned) == (3, 3)
    assert len(result.submission_ids) == 3
    planned = execute(
        db,
        "SELECT r.paper_id FROM batch_item AS i JOIN derivation_run AS r ON r.id = i.run_id"
        " WHERE i.batch_submission_id = ANY(%s)",
        result.submission_ids,
    )
    assert [row[0] for row in planned] == [second] * 3


def test_interrupted_planning_transaction_leaves_nothing_and_rerun_plans_once(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _paper(database, 1)
    calls = 0

    def create_then_break(*args: Any) -> uuid.UUID:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted")
        return create_batch_item(*args)

    monkeypatch.setattr(llm_batches, "create_batch_item", create_then_break)
    queue = FakeJobQueue()
    with pytest.raises(RuntimeError, match="interrupted"):
        _plan(database, batch_setup(), queue=queue)
    monkeypatch.undo()

    assert _counts(db) == {"derivation_run": 0, "batch_submission": 0, "batch_item": 0}
    assert queue.queued == []

    result = _plan(database, batch_setup(), queue=queue)

    assert result.planned == 3
    assert _counts(db) == {"derivation_run": 3, "batch_submission": 1, "batch_item": 3}
    assert execute(
        db, "SELECT count(DISTINCT (task, paper_id)) FROM derivation_run WHERE status = 'running'"
    ) == [(3,)]
    assert len(queue.queued) == 1


# 互動服務的假回應（不是真的錄的；內容照這篇編的摘要寫，過得了各自的驗證）。
_BANKING_TITLE = "Perceived usefulness and trust in mobile banking adoption"
_BANKING_SENTENCE = "Perceived usefulness positively affects trust (β = 0.42, p = .01)."
_BANKING_ABSTRACT = (
    "Based on a survey of 200 bank customers in Taiwan, we test a model of mobile banking "
    "adoption. Perceived usefulness is defined as the degree to which users believe the app "
    f"improves their banking. {_BANKING_SENTENCE} "
    "Interviews with managers suggest that trust shapes loyalty. "
    "Future research should test other countries."
)
_ZH_REPLY: dict[str, Any] = {
    "research_question": "知覺有用性如何影響行動銀行的信任？",
    "method": "問卷調查台灣 200 名銀行客戶，並訪談經理人。",
    "theory": None,
    "findings": "知覺有用性正向影響信任；訪談顯示信任影響忠誠度。",
}
_CLASSIFY_REPLY: dict[str, Any] = {"topics": ["fintech"], "methods": ["survey_sem"]}
_MODEL_REPLY: dict[str, Any] = {
    "is_review_or_meta_analysis": False,
    "context": {
        "country": "tw",
        "industry": "financial_services",
        "research_design": "survey",
        "unit_of_analysis": "individual",
        "sample_size": 200,
    },
    "limitations": None,
    "future_research": "Future research should test other countries.",
    "constructs": [
        {
            "name_en": "Perceived usefulness",
            "label_zh": "知覺有用性",
            "definition": "the degree to which users believe the app improves their banking",
            "measurement": None,
        },
        {"name_en": "Trust", "label_zh": "信任", "definition": None, "measurement": None},
    ],
    "hypotheses": [
        {
            "sentence": _BANKING_SENTENCE,
            "cause": "Perceived usefulness",
            "effect": "Trust",
            "evidence_type": "empirical_test",
            "support": "supported",
            "result_quote": _BANKING_SENTENCE,
            "statistics": {
                "beta": 0.42,
                "p_value": 0.01,
                "sample_size": 200,
                "effect_type": None,
            },
        }
    ],
}


def _reply(fields: dict[str, Any]) -> Any:
    return fake_completion(
        json.dumps(fields, ensure_ascii=False),
        usage=LlmUsage(input_tokens=900, output_tokens=120),
    )


def test_keys_done_by_the_interactive_services_are_not_batched_again(
    db: TestDatabase, database: Database
) -> None:
    # Batch 的 llm_job 要和互動服務組出一樣的 RunKey，不然 Batch 會為做過的工作再付一次錢。
    setup = batch_setup()
    paper_id = create_paper(
        database,
        doi="10.1/banking",
        openalex_id="W77",
        title=_BANKING_TITLE,
        abstract=_BANKING_ABSTRACT,
    )
    interactive = gateway_config(ROOT / "prompts")
    budget = budget_config()
    zh = run_zh_summary(
        SYSTEM,
        database,
        FakeLlmProvider([_reply(_ZH_REPLY)]),
        interactive,
        budget,
        setup.zh_summary_rules,
        paper_id,
        clock=lambda: NOW,
    )
    classified = run_classification(
        SYSTEM,
        database,
        FakeLlmProvider([_reply(_CLASSIFY_REPLY)]),
        interactive,
        budget,
        setup.taxonomy,
        paper_id,
        clock=lambda: NOW,
    )
    model = run_research_model(
        SYSTEM,
        database,
        FakeLlmProvider([_reply(_MODEL_REPLY)]),
        interactive,
        budget,
        setup.context_values,
        paper_id,
        clock=lambda: NOW,
    )
    outcomes = [run.result.outcome if run.result else None for run in (zh, classified, model)]
    assert outcomes == [GatewayOutcome.ACTIVE] * 3

    result = _plan(database, setup)

    assert (result.planned, result.skipped) == (0, 3)
    assert _counts(db)["batch_item"] == 0
    assert submissions(db) == []


def test_waiting_for_the_budget_lock_across_midnight_writes_nothing(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex 主審查第 2 條：開始時還在 11/30 23:59，等全域預算鎖等到台北 12/1 00:30（月初窗口）。
    _paper(database, 1)
    now = [datetime(2026, 11, 30, 23, 59, tzinfo=TAIPEI)]
    original = Database.transaction_lock

    def slow_lock(connection: Any, key: int) -> None:
        original(connection, key)
        if key == LLM_BATCH_BUDGET_LOCK_KEY:
            now[0] = datetime(2026, 12, 1, 0, 30, tzinfo=TAIPEI)

    monkeypatch.setattr(Database, "transaction_lock", staticmethod(slow_lock))
    queue = FakeJobQueue()

    result = plan_llm_batches(
        SYSTEM,
        database,
        batch_setup(),
        weekly_run_id=None,
        clock=lambda: now[0],
        enqueue=queue.enqueue,
    )

    assert result.stop is PlanStop.HELD_MONTH_START
    assert (result.planned, result.deferred_budget) == (0, 0)
    assert _counts(db) == {"derivation_run": 0, "batch_submission": 0, "batch_item": 0}
    assert queue.queued == []


def test_reservations_that_reach_the_breaker_stop_planning_up_front(
    db: TestDatabase, database: Database
) -> None:
    # 第 1 條：規劃一開始的熔斷檢查也算預留（和互動的 BudgetGuard 同一個計算）。
    setup = batch_setup()
    first = _paper(database, 1)
    _plan(database, setup)
    reserved = execute(db, "SELECT sum(estimated_max_cost_usd) FROM batch_item")[0][0]
    insert_ledger(db, first, LlmTask.QA_ANSWER, str(Decimal(50) - reserved), NOW)
    _paper(database, 2)

    result = _plan(database, setup)

    assert result.stop is PlanStop.BUDGET_TRIPPED
    assert (result.planned, result.deferred_budget) == (0, 0)
