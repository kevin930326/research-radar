"""`BudgetGuard` 與閘門對真的 PostgreSQL：spec 2.3 的 I/O 表（熔斷、差一點、功能預算、問答預算、
跨月、評估與 Batch 也算、並行超支）。

帳本是假的：測試直接用超級使用者寫 `derivation_run`＋`llm_call`（`created_at` 指定在要的時間），
不呼叫模型。閘門的模型一律用替身；時鐘由測試注入。每個測試一個全新、已遷移的資料庫。
"""

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from radar.adapters.fake import FakeLlmProvider, fake_completion
from radar.domain.ai_outputs import GatewayConfig, InputKind, LlmTask, OutputLang
from radar.domain.budget import BUDGET_TIMEZONE, BudgetConfig
from radar.domain.llm import LlmUsage
from radar.domain.pricing import llm_call_cost
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.services.budget import BudgetGuard, get_budget_status
from radar.services.llm_gateway import GatewayOutcome, GatewayResult, LlmJob, run_llm_job
from tests.database import TestDatabase, migrate
from tests.llm_support import (
    GatedProvider,
    create_paper,
    execute,
    gateway_config,
    make_prompts,
    models_config,
    table_counts,
)

TAIPEI = BUDGET_TIMEZONE
MODEL = "claude-haiku-5-5"
GOOD = '{"research_question": "團隊怎麼採用資訊系統？"}'
USAGE = LlmUsage(input_tokens=1200, output_tokens=300)
SYSTEM = SystemViewer()
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=TAIPEI)
NEXT_MONTH = datetime(2026, 12, 1, 0, 0, tzinfo=TAIPEI)
WAIT = 20.0


def budget_config(**budgets: str | None) -> BudgetConfig:
    """熔斷線 50、workspace 上限 70；沒給的工作沒有個別上限。"""
    task_budgets: dict[str, str | None] = {task.value: None for task in LlmTask}
    task_budgets.update(budgets)
    return BudgetConfig.model_validate(
        {"workspace_limit_usd": "70", "monthly_breaker_usd": "50", "task_budgets_usd": task_budgets}
    )


def insert_ledger(
    db: TestDatabase,
    paper_id: uuid.UUID,
    task: LlmTask,
    cost: str,
    at: datetime,
    *,
    is_batch: bool = False,
    eval_run_id: uuid.UUID | None = None,
) -> None:
    """假帳本：一筆做完的 `derivation_run` 加一筆 `llm_call`（`created_at`＝`at`）。"""
    run_id = execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
        " input_sha256, input_kind, params, code_version, call_mode, eval_run_id, status,"
        " finished_at, created_at)"
        " VALUES (%s, %s, 1, %s, %s, %s, 'abstract', '{}'::jsonb, 'test', %s, %s, 'succeeded',"
        " %s, %s) RETURNING id",
        paper_id,
        task.value,
        "a" * 64,
        MODEL,
        uuid.uuid4().hex * 2,
        "batch" if is_batch else "interactive",
        eval_run_id,
        at,
        at,
    )[0][0]
    execute(
        db,
        "INSERT INTO llm_call (run_id, input_tokens, output_tokens, cache_creation_input_tokens,"
        " cache_read_input_tokens, cost_usd, price_tier, is_batch, created_at)"
        " VALUES (%s, 0, 0, 0, 0, %s, 'standard', %s, %s)",
        run_id,
        Decimal(cost),
        is_batch,
        at,
    )


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=8)


@pytest.fixture
def config(tmp_path: Path) -> GatewayConfig:
    return gateway_config(make_prompts(tmp_path / "prompts", LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY))


@pytest.fixture
def paper_id(database: Database) -> uuid.UUID:
    return create_paper(database)


def _job(paper_id: uuid.UUID, task: LlmTask = LlmTask.ZH_SUMMARY) -> LlmJob:
    return LlmJob(
        paper_id,
        task,
        InputKind.ABSTRACT,
        OutputLang.ZH_HANT,
        taxonomy_version="tax-v1" if task is LlmTask.CLASSIFY else None,
    )


def _provider() -> FakeLlmProvider:
    return FakeLlmProvider([fake_completion(GOOD, usage=USAGE)])


def _run(
    database: Database,
    provider: object,
    config: GatewayConfig,
    job: LlmJob,
    guard: BudgetGuard,
) -> GatewayResult:
    return run_llm_job(SYSTEM, database, provider, config, job, budget_allows=guard)  # type: ignore[arg-type]


def _guard(
    database: Database, budget: BudgetConfig | None = None, at: datetime = NOW
) -> BudgetGuard:
    return BudgetGuard(database, budget or budget_config(), clock=lambda: at)


# --- 熔斷、差一點 ---------------------------------------------------------------------------------


def test_breaker_reached_defers_every_task_without_writing(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "30.000000", NOW - timedelta(days=3))
    insert_ledger(db, paper_id, LlmTask.EXTRACT_MODEL, "20.000000", NOW - timedelta(days=1))
    guard = _guard(database)
    before = table_counts(db)
    provider = _provider()

    assert not any(guard(task) for task in LlmTask)
    with capture_logs() as logs:
        result = _run(database, provider, config, _job(paper_id), guard)

    assert result.outcome is GatewayOutcome.DEFERRED_BUDGET
    assert result.run_id is None
    assert provider.requests == []
    assert table_counts(db) == before
    status = get_budget_status(SYSTEM, database, budget_config(), now=NOW)
    assert status.total_usd == Decimal("50.000000")
    assert status.breaker_tripped
    assert status.retrieval_only
    # 不准時記一筆警告（只有工作代碼、月份、金額與原因，沒有秘密）。
    (entry,) = [log for log in logs if log["event"] == "預算不准，LLM 工作延後"]
    assert entry["log_level"] == "warning"
    assert entry["task"] == "zh_summary"
    assert entry["month"] == "2026-11"
    assert entry["total_usd"] == "50.000000"
    assert entry["breaker_tripped"] is True


def test_breaker_lifts_next_month(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "50", NOW)

    assert _run(database, _provider(), config, _job(paper_id), _guard(database)).outcome is (
        GatewayOutcome.DEFERRED_BUDGET
    )
    # 台北時間下個月一到：沒有任何開關要關，帳本的加總自然歸零。
    result = _run(database, _provider(), config, _job(paper_id), _guard(database, at=NEXT_MONTH))

    assert result.outcome is GatewayOutcome.ACTIVE
    assert not get_budget_status(SYSTEM, database, budget_config(), now=NEXT_MONTH).retrieval_only


def test_just_below_breaker_runs(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "49.999999", NOW)
    provider = _provider()

    result = _run(database, provider, config, _job(paper_id), _guard(database))

    assert result.outcome is GatewayOutcome.ACTIVE
    assert len(provider.requests) == 1


def test_denied_budget_does_not_read_the_prompt(
    db: TestDatabase, database: Database, paper_id: uuid.UUID, tmp_path: Path
) -> None:
    # prompt 資料夾不存在：預算那一步就擋下，根本沒有去讀。
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "50", NOW)
    config = gateway_config(tmp_path / "missing-prompts")

    result = _run(database, _provider(), config, _job(paper_id), _guard(database))

    assert result.outcome is GatewayOutcome.DEFERRED_BUDGET


# --- 功能預算 ---------------------------------------------------------------------------------


def test_task_budget_reached_blocks_only_that_task(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    budget = budget_config(zh_summary="1")
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "1", NOW)
    guard = _guard(database, budget)
    summary_provider = _provider()
    classify_provider = _provider()

    summary = _run(database, summary_provider, config, _job(paper_id), guard)
    classify = _run(database, classify_provider, config, _job(paper_id, LlmTask.CLASSIFY), guard)

    assert summary.outcome is GatewayOutcome.DEFERRED_BUDGET
    assert summary_provider.requests == []
    assert classify.outcome is GatewayOutcome.ACTIVE
    assert len(classify_provider.requests) == 1
    status = get_budget_status(SYSTEM, database, budget, now=NOW)
    assert status.task_exhausted(LlmTask.ZH_SUMMARY)
    assert not status.breaker_tripped
    assert not status.retrieval_only


def test_qa_budget_reached_means_retrieval_only_while_others_run(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    budget = budget_config(qa_answer="0.5")
    insert_ledger(db, paper_id, LlmTask.QA_ANSWER, "0.5", NOW)
    guard = _guard(database, budget)

    assert get_budget_status(SYSTEM, database, budget, now=NOW).retrieval_only
    assert not guard(LlmTask.QA_ANSWER)
    assert guard(LlmTask.ZH_SUMMARY)
    assert _run(database, _provider(), config, _job(paper_id), guard).outcome is (
        GatewayOutcome.ACTIVE
    )


# --- 月份邊界、評估與 Batch ---------------------------------------------------------------


def test_last_second_of_october_does_not_count_in_november(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    october_end = datetime(2026, 10, 31, 23, 59, 59, tzinfo=TAIPEI)
    november_start = datetime(2026, 11, 1, 0, 0, tzinfo=TAIPEI)
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "50", october_end)

    assert not _guard(database, at=october_end)(LlmTask.ZH_SUMMARY)
    assert _guard(database, at=november_start)(LlmTask.ZH_SUMMARY)
    status = get_budget_status(SYSTEM, database, budget_config(), now=november_start)
    assert status.total_usd == Decimal(0)


def test_window_includes_start_and_excludes_end(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    start = datetime(2026, 11, 1, 0, 0, tzinfo=TAIPEI)
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "1", start)  # 算
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "2", NEXT_MONTH)  # 不算（12 月的第一刻）
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "4", start - timedelta(microseconds=1))

    status = get_budget_status(SYSTEM, database, budget_config(), now=NOW)

    assert status.total_usd == Decimal("1")


def test_utc_clock_in_october_is_november_in_taipei(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_ledger(db, paper_id, LlmTask.CLASSIFY, "3", datetime(2026, 11, 1, 0, 30, tzinfo=TAIPEI))

    status = get_budget_status(
        SYSTEM, database, budget_config(), now=datetime(2026, 10, 31, 16, 0, tzinfo=UTC)
    )

    assert status.window.label == "2026-11"
    assert status.by_task[LlmTask.CLASSIFY] == Decimal("3")


def test_eval_and_batch_costs_count(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    insert_ledger(db, paper_id, LlmTask.EVAL_JUDGE, "10", NOW, eval_run_id=uuid.uuid4())
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "15", NOW, eval_run_id=uuid.uuid4())
    insert_ledger(db, paper_id, LlmTask.CLASSIFY, "25", NOW, is_batch=True)

    status = get_budget_status(SYSTEM, database, budget_config(), now=NOW)

    assert status.by_task[LlmTask.EVAL_JUDGE] == Decimal("10")
    assert status.by_task[LlmTask.ZH_SUMMARY] == Decimal("15")
    assert status.by_task[LlmTask.CLASSIFY] == Decimal("25")
    assert status.breaker_tripped
    assert _run(database, _provider(), config, _job(paper_id), _guard(database)).outcome is (
        GatewayOutcome.DEFERRED_BUDGET
    )


def test_naive_clock_is_rejected_before_any_write(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    guard = BudgetGuard(database, budget_config(), clock=lambda: datetime(2026, 11, 15, 12, 0))
    before = table_counts(db)
    provider = _provider()

    with pytest.raises(ValueError, match="時區"):
        _run(database, provider, config, _job(paper_id), guard)

    assert provider.requests == []
    assert table_counts(db) == before


# --- 並行：同時在途的呼叫彼此看不到 ------------------------------------------------------


class _Worker:
    def __init__(self, target: Callable[[], GatewayResult]) -> None:
        self.result: GatewayResult | None = None
        self.error: BaseException | None = None

        def run() -> None:
            try:
                self.result = target()
            except BaseException as exc:
                self.error = exc

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self) -> GatewayResult:
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "執行緒沒有在時限內結束"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def test_concurrent_calls_both_pass_the_check_and_overshoot_is_bounded(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    """記錄已知的行為（ADR-0031）：兩個呼叫都在帳記下之前通過檢查，兩個都會呼叫；
    超支上限＝同時在途的呼叫數 × 單次費用。帳記下之後的呼叫就被擋下。

    閘門記的 `llm_call.created_at` 是資料庫真的現在，所以這個測試的時鐘與假帳本也用真的現在。
    """
    other_paper = create_paper(database, doi="10.1/def", openalex_id="W2")
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, "49.9998", datetime.now(UTC))
    guard = BudgetGuard(database, budget_config(), clock=lambda: datetime.now(UTC))
    first = GatedProvider(_provider())
    second = GatedProvider(_provider())

    workers = [
        _Worker(lambda: _run(database, first, config, _job(paper_id), guard)),
        _Worker(lambda: _run(database, second, config, _job(other_paper), guard)),
    ]
    # 兩個都已經停在「等模型」：都通過了預算檢查。
    assert first.entered.wait(WAIT)
    assert second.entered.wait(WAIT)
    first.release.set()
    second.release.set()
    results = [worker.join() for worker in workers]

    assert [result.outcome for result in results] == [GatewayOutcome.ACTIVE] * 2
    assert (first.calls, second.calls) == (1, 1)
    per_call = llm_call_cost(models_config(), MODEL, USAGE, is_batch=False).cost_usd
    total = get_budget_status(SYSTEM, database, budget_config(), now=datetime.now(UTC)).total_usd
    assert total == Decimal("49.9998") + 2 * per_call
    assert Decimal(50) < total <= Decimal(50) + 2 * per_call
    assert not guard(LlmTask.ZH_SUMMARY)
