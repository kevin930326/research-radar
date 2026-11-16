"""重跑被拒收的 AI 結果（`services/run_retry.py`、閘門的 `retry_of`，Story 2.12、ADR-0044）對真的
PostgreSQL：

- 確認後只重跑被拒收的那一項：成功變 `active`，再被拒收就是新的一列 `rejected`。
- 沒有被拒收的（有 `active`、還沒有結果、找不到論文）只印原因、不呼叫模型。
- `retry_of` 以外的路徑（互動服務、試跑、閘門本身、Batch 規劃）遇到 `rejected` 一律跳過。
- 兩個重跑同時跑、或同一個確認過的 id 重跑完再送：只呼叫一次模型、只多一筆 run。
- 中斷後下一次重跑照樣能跑；已經存下輸出的接續、不重付。
- 閘門鎖內 `retry_of` 的四種情況，評估不能同時帶 `retry_of`。
- 互動與 Batch 的 run 都記下送出時用的摘要來源。

模型一律用依 schema 回覆的替身（`tests/trial_support.py`，假回覆不是真的錄的），不連網路；論文與
回覆都是自己編的。
"""

import threading
import uuid
from decimal import Decimal

import pytest

import radar.services.ai_results as ai_results
import radar.services.llm_gateway as llm_gateway
from radar.adapters.fake.jobs import FakeJobQueue
from radar.domain.admin_runs import RetryBlock, RetryItemStatus, RetryPlan, RetryReport
from radar.domain.ai_outputs import InputKind, LlmTask
from radar.domain.eval_split import EvalSplit
from radar.domain.llm import LlmConfigurationError
from radar.domain.permissions import PermissionDeniedError
from radar.domain.trial_runs import TrialItemStatus, parse_trial_targets
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.services.llm_batches import llm_job, plan_llm_batches
from radar.services.llm_gateway import GatewayOutcome, GatewayResult, run_llm_job
from radar.services.run_retry import plan_retry, run_retry
from radar.services.trial_runs import estimate_trial, resolve_trial_papers, run_trial
from radar.services.zh_summary import run_zh_summary
from tests.batch_support import NOW, batch_setup
from tests.database import TestDatabase, migrate
from tests.llm_support import execute, set_abstract, table_counts
from tests.trial_support import (
    BANKING_ABSTRACT,
    WAIT,
    SchemaReplyProvider,
    banking_paper,
    doi_in,
    good_replies,
    reply,
    trial_setup,
)

SYSTEM = SystemViewer()
# 不是 JSON：每個工作都會拒收（llm_malformed_response）。假回覆，不是真的錄的。
BAD = reply("研究問題：行動銀行的採用")
HEADERS = {
    LlmTask.ZH_SUMMARY: "zh_summary",
    LlmTask.CLASSIFY: "classification",
    LlmTask.EXTRACT_MODEL: "research_model",
}


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=4)


@pytest.fixture
def paper_id(database: Database) -> uuid.UUID:
    return banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)


def _gateway(
    database: Database,
    provider: SchemaReplyProvider,
    paper_id: uuid.UUID,
    task: LlmTask,
    *,
    input_kind: InputKind = InputKind.ABSTRACT,
    retry_of: uuid.UUID | None = None,
    eval_run_id: uuid.UUID | None = None,
) -> GatewayResult:
    setup = trial_setup()
    return run_llm_job(
        SYSTEM,
        database,
        provider,
        setup.gateway,
        llm_job(setup, paper_id, task, input_kind),
        budget_allows=lambda _task: True,
        retry_of=retry_of,
        eval_run_id=eval_run_id,
    )


def _reject(database: Database, paper_id: uuid.UUID, task: LlmTask) -> None:
    result = _gateway(database, SchemaReplyProvider({task: BAD}), paper_id, task)
    assert result.outcome is GatewayOutcome.REJECTED


def _headers(db: TestDatabase, task: LlmTask) -> list[tuple[uuid.UUID, str]]:
    rows = execute(
        db,
        f"SELECT id, status FROM {HEADERS[task]} ORDER BY created_at, id",
    )
    return [(uuid.UUID(str(row[0])), str(row[1])) for row in rows]


def _plan(database: Database, paper_id: uuid.UUID, *tasks: LlmTask) -> RetryPlan:
    return plan_retry(SYSTEM, database, trial_setup(), paper_id, tasks, attempts=3)


def _retry(database: Database, provider: SchemaReplyProvider, plan: RetryPlan) -> RetryReport:
    return run_retry(SYSTEM, database, provider, trial_setup(), plan, clock=lambda: NOW)


# --- 確認後重跑 -------------------------------------------------------------------------------


def test_rejected_result_is_retried_once_and_becomes_active(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    _reject(database, paper_id, LlmTask.CLASSIFY)
    [(rejected_id, status)] = _headers(db, LlmTask.CLASSIFY)
    assert status == "rejected"

    plan = _plan(database, paper_id, LlmTask.CLASSIFY)

    (item,) = plan.items
    assert item.retryable
    assert item.rejected_id == rejected_id
    assert item.reject_reason == "llm_malformed_response"
    assert item.max_cost_usd is not None
    assert item.max_cost_usd > 0
    assert plan.max_cost_usd == item.max_cost_usd * 3
    before = table_counts(db)

    provider = SchemaReplyProvider(good_replies())
    report = _retry(database, provider, plan)

    assert [(i.task, i.status) for i in report.items] == [
        (LlmTask.CLASSIFY, RetryItemStatus.ACTIVE)
    ]
    assert report.exit_code == 0
    assert report.cost_usd > 0
    assert provider.total_calls == 1
    assert [status for _, status in _headers(db, LlmTask.CLASSIFY)] == ["rejected", "active"]
    after = table_counts(db)
    assert after["derivation_run"] == before["derivation_run"] + 1
    assert after["llm_call"] == before["llm_call"] + 1


def test_retry_that_is_rejected_again_adds_a_new_rejected_row(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    _reject(database, paper_id, LlmTask.ZH_SUMMARY)
    plan = _plan(database, paper_id, LlmTask.ZH_SUMMARY)

    report = _retry(database, SchemaReplyProvider({LlmTask.ZH_SUMMARY: BAD}), plan)

    [item] = report.items
    assert (item.status, item.reason) == (RetryItemStatus.REJECTED, "llm_malformed_response")
    assert report.exit_code == 0
    headers = _headers(db, LlmTask.ZH_SUMMARY)
    assert [status for _, status in headers] == ["rejected", "rejected"]
    # 下一次計畫指向最新的那一列。
    (again,) = _plan(database, paper_id, LlmTask.ZH_SUMMARY).items
    assert again.rejected_id == headers[-1][0]


def test_only_rejected_tasks_are_retried_and_others_print_reasons(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    _reject(database, paper_id, LlmTask.CLASSIFY)
    done = _gateway(database, SchemaReplyProvider(good_replies()), paper_id, LlmTask.ZH_SUMMARY)
    assert done.outcome is GatewayOutcome.ACTIVE

    plan = _plan(database, paper_id, LlmTask.EXTRACT_MODEL, LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY)

    assert [(i.task, i.block, i.detail) for i in plan.items] == [
        (LlmTask.ZH_SUMMARY, RetryBlock.HAS_ACTIVE, None),
        (LlmTask.CLASSIFY, None, None),
        (LlmTask.EXTRACT_MODEL, RetryBlock.NO_REJECTED, None),
    ]
    provider = SchemaReplyProvider(good_replies())
    report = _retry(database, provider, plan)
    assert [(i.task, i.status) for i in report.items] == [
        (LlmTask.CLASSIFY, RetryItemStatus.ACTIVE)
    ]
    assert dict(provider.calls) == {LlmTask.CLASSIFY: 1}


def test_nothing_to_retry_calls_no_model_and_writes_nothing(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    before = table_counts(db)
    missing = uuid.uuid4()

    plan = _plan(database, paper_id, LlmTask.CLASSIFY)
    gone = _plan(database, missing, LlmTask.CLASSIFY, LlmTask.ZH_SUMMARY)

    assert [(i.block, i.detail) for i in plan.items] == [(RetryBlock.NO_REJECTED, None)]
    assert [i.block for i in gone.items] == [RetryBlock.NOT_FOUND, RetryBlock.NOT_FOUND]
    provider = SchemaReplyProvider(good_replies())
    assert _retry(database, provider, plan).items == ()
    assert _retry(database, provider, gone).items == ()
    assert provider.total_calls == 0
    assert table_counts(db) == before


def test_plan_reports_a_running_key(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    _reject(database, paper_id, LlmTask.CLASSIFY)
    provider = SchemaReplyProvider(good_replies(), gated=True)
    worker = threading.Thread(
        target=_retry, args=(database, provider, _plan(database, paper_id, LlmTask.CLASSIFY))
    )
    worker.start()
    try:
        assert provider.entered.wait(WAIT)
        (item,) = _plan(database, paper_id, LlmTask.CLASSIFY).items
        assert item.block is RetryBlock.RUNNING
    finally:
        provider.release.set()
        worker.join(WAIT)


def test_retry_needs_a_system_actor(database: Database, paper_id: uuid.UUID) -> None:
    member = MemberViewer(uuid.uuid4(), is_admin=True)  # 管理者成員也不行：只有系統工作
    with pytest.raises(PermissionDeniedError):
        plan_retry(member, database, trial_setup(), paper_id, [LlmTask.CLASSIFY], attempts=1)
    with pytest.raises(PermissionDeniedError):
        run_retry(
            member,
            database,
            SchemaReplyProvider(good_replies()),
            trial_setup(),
            RetryPlan(paper_id, (), 1),
            clock=lambda: NOW,
        )


@pytest.mark.parametrize("tasks", [[], [LlmTask.MERGE_JUDGE]])
def test_plan_rejects_bad_tasks(
    database: Database, paper_id: uuid.UUID, tasks: list[LlmTask]
) -> None:
    with pytest.raises(ValueError, match="tasks"):
        plan_retry(SYSTEM, database, trial_setup(), paper_id, tasks, attempts=1)


# --- 其他路徑仍然跳過 rejected ---------------------------------------------------------------


def test_other_paths_still_skip_a_rejected_result(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    for task in HEADERS:
        _reject(database, paper_id, task)
    before = table_counts(db)
    provider = SchemaReplyProvider(good_replies())
    setup = trial_setup()

    # 互動服務。
    zh = run_zh_summary(
        SYSTEM,
        database,
        provider,
        setup.gateway,
        setup.budget,
        setup.zh_summary_rules,
        paper_id,
        clock=lambda: NOW,
    )
    assert zh.result is not None
    assert zh.result.outcome is GatewayOutcome.SKIPPED
    # 閘門本身（沒帶 retry_of）。
    assert _gateway(database, provider, paper_id, LlmTask.CLASSIFY).outcome is (
        GatewayOutcome.SKIPPED
    )
    # 試跑。
    selection = resolve_trial_papers(SYSTEM, database, parse_trial_targets([str(paper_id)]))
    trial = run_trial(
        SYSTEM,
        database,
        provider,
        setup,
        selection,
        clock=lambda: NOW,
        estimate=estimate_trial(SYSTEM, database, setup, selection, attempts=1),
    )
    assert {item.status for item in trial.items} == {TrialItemStatus.SKIPPED}
    # Batch 規劃。
    planned = plan_llm_batches(
        SYSTEM,
        database,
        batch_setup(),
        weekly_run_id=None,
        clock=lambda: NOW,
        enqueue=FakeJobQueue().enqueue,
    )
    assert (planned.planned, planned.skipped) == (0, 3)

    assert provider.total_calls == 0
    assert table_counts(db) == before


# --- 同時重跑、重送同一個 id ------------------------------------------------------------------


def test_two_concurrent_retries_call_the_model_once(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    _reject(database, paper_id, LlmTask.CLASSIFY)
    plan = _plan(database, paper_id, LlmTask.CLASSIFY)
    runs_before = table_counts(db)["derivation_run"]
    provider = SchemaReplyProvider(good_replies(), gated=True)
    results: dict[str, RetryReport] = {}

    def first() -> None:
        results["first"] = _retry(database, provider, plan)

    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert provider.entered.wait(WAIT)
        second = _retry(database, provider, plan)
    finally:
        provider.release.set()
        worker.join(WAIT)

    assert [i.status for i in second.items] == [RetryItemStatus.SKIPPED]
    assert second.exit_code == 0
    first_report = results["first"]
    assert [i.status for i in first_report.items] == [RetryItemStatus.ACTIVE]
    assert provider.total_calls == 1
    assert table_counts(db)["derivation_run"] == runs_before + 1


@pytest.mark.parametrize("second_reply", ["good", "bad"])
def test_resending_the_same_confirmed_id_is_skipped(
    db: TestDatabase, database: Database, paper_id: uuid.UUID, second_reply: str
) -> None:
    _reject(database, paper_id, LlmTask.CLASSIFY)
    plan = _plan(database, paper_id, LlmTask.CLASSIFY)
    replies = good_replies() if second_reply == "good" else {LlmTask.CLASSIFY: BAD}
    _retry(database, SchemaReplyProvider(replies), plan)
    before = table_counts(db)
    provider = SchemaReplyProvider(good_replies())

    again = _retry(database, provider, plan)

    assert [i.status for i in again.items] == [RetryItemStatus.SKIPPED]
    assert provider.total_calls == 0
    assert table_counts(db) == before


# --- 確認後變了、設定錯、論文不見 -----------------------------------------------------------


def test_input_changed_after_confirmation_is_not_retried(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    _reject(database, paper_id, LlmTask.CLASSIFY)
    plan = _plan(database, paper_id, LlmTask.CLASSIFY)
    set_abstract(db, paper_id, BANKING_ABSTRACT + " We also interviewed managers.")
    before = table_counts(db)
    provider = SchemaReplyProvider(good_replies())

    report = _retry(database, provider, plan)

    assert [i.status for i in report.items] == [RetryItemStatus.INPUT_CHANGED]
    assert report.exit_code == 1
    assert provider.total_calls == 0
    assert table_counts(db) == before


def test_configuration_error_stops_remaining_items(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    _reject(database, paper_id, LlmTask.ZH_SUMMARY)
    _reject(database, paper_id, LlmTask.CLASSIFY)
    plan = _plan(database, paper_id, LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY)
    assert len(plan.retryable) == 2
    replies = good_replies()
    replies[LlmTask.ZH_SUMMARY] = LlmConfigurationError("HTTP 401", status=401)
    provider = SchemaReplyProvider(replies)

    report = _retry(database, provider, plan)

    assert [(i.task, i.status, i.reason, i.http_status) for i in report.items] == [
        (LlmTask.ZH_SUMMARY, RetryItemStatus.ERROR, "llm_configuration", 401)
    ]
    assert report.exit_code == 1
    assert provider.total_calls == 1


def test_paper_deleted_after_plan_is_not_found(
    db: TestDatabase,
    database: Database,
    paper_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reject(database, paper_id, LlmTask.CLASSIFY)
    plan = _plan(database, paper_id, LlmTask.CLASSIFY)
    before = table_counts(db)
    # 論文有外鍵牽著，測試不真的刪：讓閘門讀論文時找不到（和刪掉之後看到的一樣）。
    monkeypatch.setattr(llm_gateway, "find_paper_llm_source", lambda *_a, **_k: None)
    provider = SchemaReplyProvider(good_replies())

    report = _retry(database, provider, plan)

    assert [i.status for i in report.items] == [RetryItemStatus.NOT_FOUND]
    assert report.exit_code == 1
    assert provider.total_calls == 0
    assert table_counts(db) == before


# --- 中斷與接續 -------------------------------------------------------------------------------


def test_interrupted_retry_can_run_again(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    _reject(database, paper_id, LlmTask.CLASSIFY)
    plan = _plan(database, paper_id, LlmTask.CLASSIFY)

    with pytest.raises(KeyboardInterrupt):
        _retry(database, SchemaReplyProvider({LlmTask.CLASSIFY: KeyboardInterrupt()}), plan)

    statuses = execute(
        db,
        "SELECT status, failure_reason FROM derivation_run WHERE task = 'classify'"
        " ORDER BY created_at, id",
    )
    assert statuses[-1] == ("failed", "llm_gateway_error")
    provider = SchemaReplyProvider(good_replies())
    report = _retry(database, provider, plan)
    assert [i.status for i in report.items] == [RetryItemStatus.ACTIVE]
    assert provider.total_calls == 1


def test_saved_output_is_resumed_without_paying_again(
    db: TestDatabase,
    database: Database,
    paper_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reject(database, paper_id, LlmTask.CLASSIFY)
    plan = _plan(database, paper_id, LlmTask.CLASSIFY)
    real = ai_results.record_llm_result
    calls = {"n": 0}

    def fail_once(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("寫入失敗（測試）")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(llm_gateway, "record_llm_result", fail_once)
    first = _retry(database, SchemaReplyProvider(good_replies()), plan)
    assert [(i.status, i.reason) for i in first.items] == [(RetryItemStatus.ERROR, "RuntimeError")]
    assert first.exit_code == 1
    paid = table_counts(db)

    provider = SchemaReplyProvider(good_replies())
    again = _retry(database, provider, plan)

    assert [i.status for i in again.items] == [RetryItemStatus.ACTIVE]
    assert again.cost_usd == Decimal(0)
    assert provider.total_calls == 0
    after = table_counts(db)
    assert (after["llm_call"], after["derivation_run"]) == (
        paid["llm_call"],
        paid["derivation_run"],
    )


# --- 閘門鎖內的 retry_of ----------------------------------------------------------------------


def test_retry_of_with_no_header_is_skipped(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    # 審查後第 3 點：帶了 retry_of 就一定核對；這組鍵查不到任何表頭（確認過的拒收列被移除、或亂給
    # 的 id）時略過、不呼叫（原本預期照常付費執行，和需求相反）。
    provider = SchemaReplyProvider(good_replies())
    result = _gateway(database, provider, paper_id, LlmTask.CLASSIFY, retry_of=uuid.uuid4())
    assert result.outcome is GatewayOutcome.SKIPPED
    assert provider.total_calls == 0


def test_retry_of_does_not_override_an_active_result(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    _reject(database, paper_id, LlmTask.CLASSIFY)
    [(rejected_id, _)] = _headers(db, LlmTask.CLASSIFY)
    # 拒收後輸入沒變卻有了 active（例如別的流程寫進來）：直接把狀態改掉模擬。
    execute(
        db,
        "UPDATE classification SET status = 'active', reject_reason = NULL WHERE id = %s",
        rejected_id,
    )
    provider = SchemaReplyProvider(good_replies())

    result = _gateway(database, provider, paper_id, LlmTask.CLASSIFY, retry_of=rejected_id)

    assert result.outcome is GatewayOutcome.SKIPPED
    assert provider.total_calls == 0


def test_retry_of_must_be_the_latest_rejected_header(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    _reject(database, paper_id, LlmTask.CLASSIFY)
    [(older, _)] = _headers(db, LlmTask.CLASSIFY)
    _gateway(
        database,
        SchemaReplyProvider({LlmTask.CLASSIFY: BAD}),
        paper_id,
        LlmTask.CLASSIFY,
        retry_of=older,
    )
    [_, (latest, status)] = _headers(db, LlmTask.CLASSIFY)
    assert status == "rejected"
    provider = SchemaReplyProvider(good_replies())

    for stale in (older, uuid.uuid4()):
        result = _gateway(database, provider, paper_id, LlmTask.CLASSIFY, retry_of=stale)
        assert result.outcome is GatewayOutcome.SKIPPED
    assert provider.total_calls == 0

    result = _gateway(database, provider, paper_id, LlmTask.CLASSIFY, retry_of=latest)
    assert result.outcome is GatewayOutcome.ACTIVE
    assert provider.total_calls == 1


def test_retry_of_whose_header_is_no_longer_rejected_is_skipped(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    # 審查後第 3 點：確認時那一列 rejected 之後被改成別的狀態（這裡直接改成 superseded 模擬）：
    # 它已經不擋平常流程，但 retry_of 照樣要核對「仍是 rejected」，略過、不呼叫。
    _reject(database, paper_id, LlmTask.CLASSIFY)
    [(rejected_id, _)] = _headers(db, LlmTask.CLASSIFY)
    execute(
        db,
        "UPDATE classification SET status = 'superseded', reject_reason = NULL WHERE id = %s",
        rejected_id,
    )
    provider = SchemaReplyProvider(good_replies())

    result = _gateway(database, provider, paper_id, LlmTask.CLASSIFY, retry_of=rejected_id)

    assert result.outcome is GatewayOutcome.SKIPPED
    assert provider.total_calls == 0


def test_eval_run_cannot_carry_retry_of(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    before = table_counts(db)
    with pytest.raises(ValueError, match="retry_of"):
        _gateway(
            database,
            SchemaReplyProvider(good_replies()),
            paper_id,
            LlmTask.CLASSIFY,
            retry_of=uuid.uuid4(),
            eval_run_id=uuid.uuid4(),
        )
    assert table_counts(db) == before


# --- 送出時記下摘要來源 -----------------------------------------------------------------------


def test_runs_record_the_abstract_source(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    provider = SchemaReplyProvider(good_replies())
    abstract = _gateway(database, provider, paper_id, LlmTask.ZH_SUMMARY)
    title = _gateway(database, provider, paper_id, LlmTask.CLASSIFY, input_kind=InputKind.TITLE)
    other = banking_paper(database, doi_in(EvalSplit.TUNING, "2"), 2)

    plan_llm_batches(
        SYSTEM,
        database,
        batch_setup(),
        weekly_run_id=None,
        clock=lambda: NOW,
        enqueue=FakeJobQueue().enqueue,
    )

    def source(run_id: uuid.UUID | None) -> object:
        return execute(db, "SELECT abstract_source FROM derivation_run WHERE id = %s", run_id)[0][0]

    assert source(abstract.run_id) == "openalex"
    assert source(title.run_id) is None
    batch_runs = execute(
        db,
        "SELECT task, input_kind, abstract_source FROM derivation_run"
        " WHERE call_mode = 'batch' AND paper_id = %s ORDER BY task",
        other,
    )
    assert batch_runs == [
        ("classify", "abstract", "openalex"),
        ("extract_model", "abstract", "openalex"),
        ("zh_summary", "abstract", "openalex"),
    ]
