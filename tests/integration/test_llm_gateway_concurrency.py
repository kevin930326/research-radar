"""LLM 閘門的並行情境（ADR-0009）：用 `GatedProvider` 讓呼叫停在「等模型」，控制先後。

1. 同一份輸入同時跑：只呼叫模型一次、只記一筆帳。
2. 舊輸入的結果晚到：存 `superseded`，新的維持 `active`（兩筆同時放行也一樣）；sweep 判定逾時後
   才回來的結果也是 `superseded`，帳照記、run 維持 `failed`。
3. 評估不擋平常的流程，平常的流程在跑時評估也不受限。

每個測試一個全新、已遷移的資料庫；模型用替身，不連網路。
"""

import threading
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from radar.adapters.fake import FakeLlmProvider, fake_completion
from radar.domain.ai_outputs import GatewayConfig, InputKind, LlmTask, OutputLang
from radar.domain.llm import LlmUsage
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.services.llm_gateway import (
    GatewayOutcome,
    GatewayResult,
    LlmJob,
    expire_stale_runs,
    run_llm_job,
)
from tests.database import TestDatabase, migrate
from tests.llm_support import (
    GatedProvider,
    create_paper,
    execute,
    gateway_config,
    make_prompts,
    set_abstract,
)

GOOD = '{"research_question": "團隊怎麼採用資訊系統？"}'
USAGE = LlmUsage(input_tokens=1200, output_tokens=300)
SYSTEM = SystemViewer()
WAIT = 20.0


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=8)


@pytest.fixture
def config(tmp_path: Path) -> GatewayConfig:
    return gateway_config(make_prompts(tmp_path / "prompts", LlmTask.ZH_SUMMARY))


@pytest.fixture
def paper_id(database: Database) -> uuid.UUID:
    return create_paper(database)


def _job(paper_id: uuid.UUID) -> LlmJob:
    return LlmJob(paper_id, LlmTask.ZH_SUMMARY, InputKind.ABSTRACT, OutputLang.ZH_HANT)


def _gated(replies: int = 1, **kwargs: object) -> GatedProvider:
    inner = FakeLlmProvider([fake_completion(GOOD, usage=USAGE) for _ in range(replies)])
    return GatedProvider(inner, **kwargs)  # type: ignore[arg-type]


class Worker:
    """在另一個執行緒跑 `run_llm_job`，收結果或例外。"""

    def __init__(
        self,
        database: Database,
        provider: GatedProvider,
        config: GatewayConfig,
        job: LlmJob,
        *,
        eval_run_id: uuid.UUID | None = None,
        start_gate: threading.Barrier | None = None,
    ) -> None:
        self.result: GatewayResult | None = None
        self.error: BaseException | None = None
        self.done = threading.Event()

        def target() -> None:
            try:
                if start_gate is not None:
                    start_gate.wait(WAIT)
                self.result = run_llm_job(
                    SYSTEM,
                    database,
                    provider,
                    config,
                    job,
                    budget_allows=lambda _task: True,
                    eval_run_id=eval_run_id,
                )
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()

    def join(self) -> GatewayResult:
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "執行緒沒有在時限內結束"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT
    while not condition():
        assert time.monotonic() < deadline, "等不到條件成立"
        time.sleep(0.01)


def _statuses(db: TestDatabase) -> dict[uuid.UUID, str]:
    return {row[0]: row[1] for row in execute(db, "SELECT run_id, status FROM zh_summary")}


# --- 1. 同一份輸入同時跑 --------------------------------------------------------------------------


def test_concurrent_same_input_calls_model_once(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    provider = _gated(replies=4)
    start = threading.Barrier(4)
    workers = [
        Worker(database, provider, config, _job(paper_id), start_gate=start) for _ in range(4)
    ]

    # 搶輸的三個不呼叫模型、直接結束；搶到的那個停在等模型。
    _wait_until(lambda: sum(worker.done.is_set() for worker in workers) == 3)
    assert provider.entered.is_set()
    provider.release.set()
    results = [worker.join() for worker in workers]

    outcomes = sorted(result.outcome.value for result in results)
    assert outcomes == ["active", "skipped", "skipped", "skipped"]
    assert provider.calls == 1
    assert execute(db, "SELECT count(*) FROM llm_call") == [(1,)]
    assert execute(db, "SELECT status FROM derivation_run") == [("succeeded",)]


# --- 2. 舊輸入的結果晚到 --------------------------------------------------------------------------


def test_old_input_result_arriving_after_new_one_is_superseded(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    old_provider = _gated()
    old = Worker(database, old_provider, config, _job(paper_id))
    assert old_provider.entered.wait(WAIT)

    # 舊的還在等模型時摘要換了，新輸入跑完寫成 active。
    set_abstract(db, paper_id, "A newer abstract about digital platforms.")
    new = Worker(database, _gated(gated=False), config, _job(paper_id)).join()
    assert new.outcome is GatewayOutcome.ACTIVE

    old_provider.release.set()
    late = old.join()

    assert late.outcome is GatewayOutcome.SUPERSEDED
    assert _statuses(db) == {new.run_id: "active", late.run_id: "superseded"}
    assert execute(db, "SELECT count(*) FROM llm_call") == [(2,)]


@pytest.mark.parametrize("release_order", ["old-first", "new-first", "together"])
def test_old_and_new_results_released_concurrently_keep_new_active(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    paper_id: uuid.UUID,
    release_order: str,
) -> None:
    old_provider = _gated()
    old = Worker(database, old_provider, config, _job(paper_id))
    assert old_provider.entered.wait(WAIT)
    set_abstract(db, paper_id, "A newer abstract about digital platforms.")
    new_provider = _gated()
    new = Worker(database, new_provider, config, _job(paper_id))
    assert new_provider.entered.wait(WAIT)

    match release_order:
        case "old-first":
            old_provider.release.set()
            old.done.wait(WAIT)
            new_provider.release.set()
        case "new-first":
            new_provider.release.set()
            new.done.wait(WAIT)
            old_provider.release.set()
        case _:
            old_provider.release.set()
            new_provider.release.set()
    late, fresh = old.join(), new.join()

    assert late.outcome is GatewayOutcome.SUPERSEDED
    assert fresh.outcome is GatewayOutcome.ACTIVE
    assert _statuses(db) == {fresh.run_id: "active", late.run_id: "superseded"}


def test_result_after_concurrent_sweep_is_superseded_and_run_stays_failed(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    provider = _gated()
    worker = Worker(database, provider, config, _job(paper_id))
    assert provider.entered.wait(WAIT)

    # 等模型的期間 sweep 判定逾時（把建立時間往前推，模擬已經過了時限）。
    execute(db, "UPDATE derivation_run SET created_at = now() - interval '1 hour'")
    assert expire_stale_runs(SYSTEM, database, timedelta(minutes=15)) == 1
    provider.release.set()
    result = worker.join()

    assert result.outcome is GatewayOutcome.SUPERSEDED
    assert execute(db, "SELECT status, failure_reason FROM derivation_run") == [
        ("failed", "llm_run_expired")
    ]
    assert execute(db, "SELECT count(*) FROM llm_call") == [(1,)]
    assert _statuses(db) == {result.run_id: "superseded"}

    # 逾時的 run 不擋下一次：同一份輸入可以再跑，寫成 active。
    again = Worker(database, _gated(gated=False), config, _job(paper_id)).join()
    assert again.outcome is GatewayOutcome.ACTIVE


# --- 3. 評估與平常的流程互不阻擋 ------------------------------------------------------------------


def test_eval_runs_while_normal_run_is_running(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    normal_provider = _gated()
    normal = Worker(database, normal_provider, config, _job(paper_id))
    assert normal_provider.entered.wait(WAIT)

    eval_provider = _gated(gated=False)
    evaluated = Worker(
        database, eval_provider, config, _job(paper_id), eval_run_id=uuid.uuid4()
    ).join()
    assert evaluated.outcome is GatewayOutcome.CANDIDATE
    assert eval_provider.calls == 1

    normal_provider.release.set()
    result = normal.join()

    assert result.outcome is GatewayOutcome.ACTIVE
    assert _statuses(db) == {result.run_id: "active", evaluated.run_id: "candidate"}


def test_running_eval_does_not_block_normal_run(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    eval_provider = _gated()
    evaluated = Worker(database, eval_provider, config, _job(paper_id), eval_run_id=uuid.uuid4())
    assert eval_provider.entered.wait(WAIT)

    normal = Worker(database, _gated(gated=False), config, _job(paper_id)).join()
    assert normal.outcome is GatewayOutcome.ACTIVE

    eval_provider.release.set()
    candidate = evaluated.join()

    assert candidate.outcome is GatewayOutcome.CANDIDATE
    assert _statuses(db) == {normal.run_id: "active", candidate.run_id: "candidate"}
