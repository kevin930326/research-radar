"""改版預估與 Batch 規劃的重跑上限（`estimate_revision_reruns`、`PlanStop.RERUN_CAP_EXCEEDED`，
Story 2.12、ADR-0044）對真的 PostgreSQL：

- 預估（假設改版）：有現行結果的算「會重跑」、沒有的算「還沒有結果」；不寫任何列。
- 規劃：現行結果用的設定檔版本和現在不同、合計超過上限 → 整次不排、不寫任何列、記錯誤日誌；
  第一次全庫沒有現行結果時上限不影響；沒超過照常排。

模型一律用依 schema 回覆的替身（假回覆不是真的錄的），不連網路；論文與回覆都是自己編的。
"""

import dataclasses
import uuid
from decimal import Decimal
from typing import Any

import pytest
from structlog.testing import capture_logs

import radar.services.llm_batches as llm_batches
from radar.adapters.fake.jobs import FakeJobQueue
from radar.domain.ai_outputs import InputKind, LlmTask
from radar.domain.eval_split import EvalSplit
from radar.domain.llm_batches import LlmBatchPlanResult, PlanStop
from radar.domain.permissions import PermissionDeniedError
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.services.llm_batches import (
    LlmBatchSetup,
    estimate_revision_reruns,
    llm_job,
    plan_llm_batches,
)
from radar.services.llm_gateway import GatewayOutcome, run_llm_job
from tests.batch_support import NOW, batch_setup
from tests.database import TestDatabase, migrate
from tests.llm_support import execute
from tests.trial_support import (
    SchemaReplyProvider,
    banking_paper,
    doi_in,
    good_replies,
    reply,
    trial_setup,
)

SYSTEM = SystemViewer()
BOTH = (LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL)


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


def _setup(cap: str) -> LlmBatchSetup:
    setup = batch_setup()
    batches = setup.batches.model_copy(update={"revision_rerun_max_usd": Decimal(cap)})
    return dataclasses.replace(setup, batches=batches)


def _done(database: Database, paper_id: uuid.UUID) -> None:
    """互動跑完三個工作（都是 active）。"""
    setup = trial_setup()
    provider = SchemaReplyProvider(good_replies())
    for task in (LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL):
        result = run_llm_job(
            SYSTEM,
            database,
            provider,
            setup.gateway,
            llm_job(setup, paper_id, task, InputKind.ABSTRACT),
            budget_allows=lambda _task: True,
        )
        assert result.outcome is GatewayOutcome.ACTIVE


def _rows(db: TestDatabase) -> dict[str, int]:
    return {
        table: int(execute(db, f"SELECT count(*) FROM {table}")[0][0])
        for table in ("derivation_run", "batch_submission", "batch_item", "llm_call")
    }


def _plan(database: Database, setup: LlmBatchSetup) -> LlmBatchPlanResult:
    return plan_llm_batches(
        SYSTEM,
        database,
        setup,
        weekly_run_id=None,
        clock=lambda: NOW,
        enqueue=FakeJobQueue().enqueue,
        manual=True,
    )


def _old_taxonomy(db: TestDatabase) -> None:
    """模擬分類表已經改版：現行分類結果的 run 記的是舊版本，鍵也和現在不同
    （版本算進輸入指紋）。"""
    active_runs = "SELECT run_id FROM classification WHERE status = 'active'"
    execute(
        db,
        "UPDATE derivation_run SET input_sha256 = %s, taxonomy_version = 'old-version'"
        f" WHERE id IN ({active_runs})",
        "b" * 64,
    )
    execute(db, "UPDATE classification SET input_sha256 = %s WHERE status = 'active'", "b" * 64)


def test_estimate_counts_reruns_and_missing_results(db: TestDatabase, database: Database) -> None:
    done = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    _done(database, done)
    banking_paper(database, doi_in(EvalSplit.TUNING, "2"), 2)
    before = _rows(db)

    estimate = estimate_revision_reruns(
        SYSTEM, database, batch_setup(), tasks=BOTH, assume_changed=True
    )

    assert _rows(db) == before
    assert estimate.tasks == BOTH
    for task in BOTH:
        found = estimate.for_task(task)
        assert (found.rerun, found.no_result) == (1, 1), task
        assert found.max_cost_usd > 0
    assert estimate.cap_usd == Decimal(5)
    assert not estimate.exceeds_cap
    only = estimate_revision_reruns(
        SYSTEM, database, batch_setup(), tasks=[LlmTask.EXTRACT_MODEL], assume_changed=True
    )
    assert only.tasks == (LlmTask.EXTRACT_MODEL,)
    assert only.max_cost_usd == estimate.for_task(LlmTask.EXTRACT_MODEL).max_cost_usd


def test_estimate_without_a_revision_counts_nothing(db: TestDatabase, database: Database) -> None:
    _done(database, banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1))

    estimate = estimate_revision_reruns(
        SYSTEM, database, batch_setup(), tasks=BOTH, assume_changed=False
    )

    assert estimate.reruns == 0
    assert estimate.max_cost_usd == 0


@pytest.mark.parametrize("tasks", [[], [LlmTask.ZH_SUMMARY], [LlmTask.CLASSIFY, LlmTask.QA_ANSWER]])
def test_estimate_rejects_other_tasks(database: Database, tasks: list[LlmTask]) -> None:
    with pytest.raises(ValueError, match="tasks"):
        estimate_revision_reruns(SYSTEM, database, batch_setup(), tasks=tasks, assume_changed=True)


def test_estimate_needs_a_system_actor(database: Database) -> None:
    with pytest.raises(PermissionDeniedError):
        estimate_revision_reruns(
            MemberViewer(uuid.uuid4(), is_admin=True),
            database,
            batch_setup(),
            tasks=BOTH,
            assume_changed=True,
        )


def test_planning_stops_when_reruns_exceed_the_cap(db: TestDatabase, database: Database) -> None:
    _done(database, banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1))
    banking_paper(database, doi_in(EvalSplit.TUNING, "2"), 2)  # 還沒有結果：本來會排
    _old_taxonomy(db)
    before = _rows(db)

    with capture_logs() as logs:
        result = _plan(database, _setup("0"))

    assert result.stop is PlanStop.RERUN_CAP_EXCEEDED
    assert (result.planned, result.submission_ids) == (0, [])
    assert _rows(db) == before
    [error] = [log for log in logs if log["log_level"] == "error"]
    assert "revision_rerun_max_usd" in error["event"]
    assert error["cap_usd"] == "0"
    # 日誌只有計數與金額，沒有論文內容。
    assert "mobile banking" not in str(logs).lower()


def test_planning_under_the_cap_runs_normally(db: TestDatabase, database: Database) -> None:
    _done(database, banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1))
    banking_paper(database, doi_in(EvalSplit.TUNING, "2"), 2)
    _old_taxonomy(db)

    result = _plan(database, _setup("5"))

    assert result.stop is PlanStop.COMPLETED
    # 第二篇的三個工作，加上第一篇改版後的分類。
    assert result.planned == 4


@pytest.mark.parametrize("new_key_state", ["rejected", "running_batch"])
def test_reruns_planning_will_not_schedule_are_not_capped(
    db: TestDatabase, database: Database, new_key_state: str
) -> None:
    # 改版後新鍵已經被拒收（舊版本的 active 會一直留著），或已經排進 Batch 還在跑：規劃不會再排，
    # 上限也不算它，不然每次規劃都會卡在上限。
    paper_id = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    _done(database, paper_id)
    _old_taxonomy(db)
    if new_key_state == "rejected":
        setup = trial_setup()
        rejected = run_llm_job(
            SYSTEM,
            database,
            SchemaReplyProvider({LlmTask.CLASSIFY: reply("不是 JSON")}),
            setup.gateway,
            llm_job(setup, paper_id, LlmTask.CLASSIFY, InputKind.ABSTRACT),
            budget_allows=lambda _task: True,
        )
        assert rejected.outcome is GatewayOutcome.REJECTED
    else:
        assert _plan(database, _setup("5")).planned == 1
    before = _rows(db)

    result = _plan(database, _setup("0"))

    assert result.stop is PlanStop.COMPLETED
    assert result.planned == 0
    assert _rows(db) == before


def test_first_full_run_is_not_affected_by_the_cap(db: TestDatabase, database: Database) -> None:
    banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    banking_paper(database, doi_in(EvalSplit.TUNING, "2"), 2)

    result = _plan(database, _setup("0"))

    assert result.stop is PlanStop.COMPLETED
    assert result.planned == 6


# --- 審查後第 4 點：規劃只排估過的改版重跑 ---------------------------------------------------


def _fail_running_batch_runs_after_the_estimate(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限檢查估完之後，那個在跑的改版工作（Batch run）失敗、沒有產生新結果。"""
    real = llm_batches.estimate_revision_reruns

    def then_failed(*args: Any, **kwargs: Any) -> Any:
        estimate = real(*args, **kwargs)
        execute(
            db,
            "UPDATE derivation_run SET status = 'failed', failure_reason = 'batch_item_errored',"
            " finished_at = now() WHERE status = 'running' AND call_mode = 'batch'",
        )
        return estimate

    monkeypatch.setattr(llm_batches, "estimate_revision_reruns", then_failed)


def test_rerun_that_fails_after_the_estimate_is_not_planned(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper_id = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    _done(database, paper_id)
    _old_taxonomy(db)
    assert _plan(database, _setup("5")).planned == 1  # 改版重跑已經排進 Batch、還在跑
    _fail_running_batch_runs_after_the_estimate(db, monkeypatch)
    before = _rows(db)

    result = _plan(database, _setup("5"))

    # 估的時候它還在跑（不算進上限），估完才失敗：不在清單裡，這次不排。
    assert result.stop is PlanStop.COMPLETED
    assert (result.planned, result.rerun_unestimated) == (0, 1)
    assert _rows(db) == before


def test_zero_cap_plans_no_rerun_even_if_state_changes_after_the_estimate(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    _done(database, done)
    _old_taxonomy(db)
    assert _plan(database, _setup("5")).planned == 1
    banking_paper(database, doi_in(EvalSplit.TUNING, "2"), 2)  # 還沒有結果：照常排
    _fail_running_batch_runs_after_the_estimate(db, monkeypatch)

    result = _plan(database, _setup("0"))

    assert result.stop is PlanStop.COMPLETED
    assert result.rerun_unestimated == 1
    assert result.planned == 3  # 只有第二篇的三個工作，改版重跑一筆都沒排
    planned_tasks = execute(
        db,
        "SELECT r.paper_id, r.task FROM batch_item AS i JOIN derivation_run AS r"
        " ON r.id = i.run_id WHERE r.status = 'running'",
    )
    assert {row[0] for row in planned_tasks} == {
        uuid.UUID(str(row[0])) for row in execute(db, "SELECT id FROM paper WHERE id <> %s", done)
    }
