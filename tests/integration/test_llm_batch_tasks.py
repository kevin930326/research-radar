"""worker 登記的 Batch 工作與 sweep（Story 2.9、ADR-0041）對真的 PostgreSQL：

- `plan_llm_batches(weekly_run_id)`：執行時讀 AI 設定、不建 provider；建好的批次排入送出。
- `submit_llm_batch(batch_submission_id)`：建 provider 時缺金鑰 `ConfigError` 原樣丟出、列維持
  `queued`；送完關掉 provider；工作層不重試。
- sweep：停在 `queued` 超過 1 小時、`submitting` 超過 30 分鐘的批次重新排入送出。

不跑 Procrastinate worker；provider 是假的（batch ID 是編的）；環境變數只用測試值。
"""

import dataclasses
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

import pytest

import radar.entrypoints.worker.tasks as worker_tasks
from radar.adapters.fake import FakeHeartbeat, FakeLlmProvider
from radar.adapters.fake.jobs import FakeJobQueue, QueuedJob
from radar.config import ConfigError, load_llm_batch_config
from radar.domain.jobs import PLAN_LLM_BATCHES, SUBMIT_LLM_BATCH
from radar.domain.llm import LlmBatchRequest
from radar.domain.llm_batches import LlmBatchConfig, in_month_start_hold
from radar.domain.settings import Settings
from radar.domain.sweep import SWEEP_REGISTRY
from radar.entrypoints.bootstrap import WorkerParts, build_worker
from radar.repo.db import Database
from radar.services.sweep import sweep
from tests.batch_support import BATCH_ID, batch_status
from tests.database import ROOT, TestDatabase, migrate
from tests.llm_support import create_paper, execute


class _ClosingFake(FakeLlmProvider):
    """可以 `with` 的假 provider；記下有沒有關。"""

    def __init__(self, replies: Sequence[str | BaseException]) -> None:
        super().__init__(batch_replies=replies)
        self.closed = False

    def submit_batch(self, requests: Sequence[LlmBatchRequest]) -> str:
        assert not self.closed
        return super().submit_batch(requests)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _parts(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch, queue: FakeJobQueue, **settings: Any
) -> tuple[WorkerParts, Settings]:
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(ROOT / "config"))
    worker_settings = Settings.model_validate({"database_url": db.app_url, **settings})
    parts = build_worker(worker_settings, queue_pool_size=1, app_pool_size=1, heartbeat_urls={})
    parts = dataclasses.replace(parts, enqueue=queue.enqueue, send_heartbeat=FakeHeartbeat())
    worker_tasks.register_tasks(parts.job_app.app, parts, worker_settings)
    return parts, worker_settings


def _task(parts: WorkerParts, name: str) -> Callable[..., None]:
    func: Callable[..., None] = parts.job_app.app.tasks[name].func
    return func


def _enable_weekly_planning(monkeypatch: pytest.MonkeyPatch) -> None:
    """repo 的設定在 Story 2.11 以前關著每週規劃；這裡讀完照樣打開，其他值不變。"""

    def load_enabled() -> LlmBatchConfig:
        return load_llm_batch_config().model_copy(update={"weekly_planning_enabled": True})

    monkeypatch.setattr(worker_tasks, "load_llm_batch_config", load_enabled)


def _planned(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch, queue: FakeJobQueue, **settings: Any
) -> tuple[WorkerParts, str]:
    if in_month_start_hold(datetime.now(UTC), 8):
        pytest.skip("現在是台北每月 1 日 08:00 以前（月初窗口），規劃工作照設計整次延後")
    create_paper(Database(db.app_url, connect_timeout=5, pool_size=1))
    [(weekly_run_id,)] = execute(
        db, "INSERT INTO weekly_run (status) VALUES ('published') RETURNING id"
    )
    parts, _ = _parts(db, monkeypatch, queue, **settings)
    _enable_weekly_planning(monkeypatch)
    _task(parts, PLAN_LLM_BATCHES.name)(weekly_run_id=str(weekly_run_id))
    [(submission_id, run_ref)] = execute(db, "SELECT id, weekly_run_id FROM batch_submission")
    assert run_ref == weekly_run_id
    return parts, str(submission_id)


def test_plan_task_builds_batches_and_enqueues_submit(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[object] = []
    monkeypatch.setattr(worker_tasks, "build_llm_provider", lambda *a: built.append(a))
    queue = FakeJobQueue()

    _, submission_id = _planned(db, monkeypatch, queue)

    assert built == []  # 規劃不建 provider
    assert [(job.job, str(job.ids["batch_submission_id"])) for job in queue.queued] == [
        (SUBMIT_LLM_BATCH, submission_id)
    ]
    assert execute(db, "SELECT count(*) FROM batch_item") == [(3,)]


def test_plan_task_with_the_repo_config_plans_nothing(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_paper(Database(db.app_url, connect_timeout=5, pool_size=1))
    [(weekly_run_id,)] = execute(
        db, "INSERT INTO weekly_run (status) VALUES ('published') RETURNING id"
    )
    queue = FakeJobQueue()
    parts, _ = _parts(db, monkeypatch, queue)

    _task(parts, PLAN_LLM_BATCHES.name)(weekly_run_id=str(weekly_run_id))

    # weekly_planning_enabled: false（ADR-0041 決定 11）：不寫列、不排送出。
    assert execute(db, "SELECT count(*) FROM batch_submission") == [(0,)]
    assert execute(db, "SELECT count(*) FROM derivation_run") == [(0,)]
    assert queue.queued == []


def test_submit_task_without_api_key_raises_config_error_and_keeps_queued(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = FakeJobQueue()
    parts, submission_id = _planned(db, monkeypatch, queue)

    with pytest.raises(ConfigError, match="RADAR_ANTHROPIC_API_KEY"):
        _task(parts, SUBMIT_LLM_BATCH.name)(batch_submission_id=submission_id)

    assert execute(db, "SELECT status FROM batch_submission") == [("queued",)]


def test_submit_task_submits_and_closes_the_provider(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ClosingFake([BATCH_ID])
    monkeypatch.setattr(worker_tasks, "build_llm_provider", lambda settings: provider)
    queue = FakeJobQueue()
    parts, submission_id = _planned(db, monkeypatch, queue)

    _task(parts, SUBMIT_LLM_BATCH.name)(batch_submission_id=submission_id)

    assert provider.closed
    assert len(provider.batch_requests) == 1
    [(status, batch_id)] = execute(db, "SELECT status, provider_batch_id FROM batch_submission")
    assert (status, batch_id) == ("submitted", BATCH_ID)


def test_submit_is_never_retried_by_the_queue(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts, _ = _parts(db, monkeypatch, FakeJobQueue(), job_max_retries=3)
    tasks = parts.job_app.app.tasks

    assert tasks[SUBMIT_LLM_BATCH.name].retry_strategy is None
    assert tasks[SUBMIT_LLM_BATCH.name].queue == "ai"
    assert tasks[PLAN_LLM_BATCHES.name].retry_strategy is not None
    assert tasks[PLAN_LLM_BATCHES.name].queue == "ai"


def _submission(db: TestDatabase, status: str, age: str) -> str:
    [(submission_id,)] = execute(
        db,
        "INSERT INTO batch_submission (status, item_count, estimated_max_cost_usd, updated_at)"
        f" VALUES (%s, 0, 0, now() - interval '{age}') RETURNING id",
        status,
    )
    return str(submission_id)


def test_sweep_requeues_stuck_queued_and_submitting_batches(db: TestDatabase) -> None:
    stuck_queued = _submission(db, "queued", "61 minutes")
    _submission(db, "queued", "59 minutes")
    stuck_submitting = _submission(db, "submitting", "31 minutes")
    _submission(db, "submitting", "29 minutes")
    queue = FakeJobQueue()

    sweep(
        Database(db.app_url, connect_timeout=5, pool_size=1),
        SWEEP_REGISTRY,
        queue.enqueue,
        queue.retry_stalled,
        FakeHeartbeat(),
    )

    assert sorted(str(job.ids["batch_submission_id"]) for job in queue.queued) == sorted(
        [stuck_queued, stuck_submitting]
    )
    assert {job.job for job in queue.queued} == {SUBMIT_LLM_BATCH}


def test_swept_stale_submitting_batch_becomes_needs_attention_without_sending(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ClosingFake([])
    monkeypatch.setattr(worker_tasks, "build_llm_provider", lambda settings: provider)
    queue = FakeJobQueue()
    parts, submission_id = _planned(db, monkeypatch, queue)
    execute(
        db,
        "UPDATE batch_submission SET status = 'submitting',"
        " updated_at = now() - interval '31 minutes'",
    )
    swept = FakeJobQueue()
    sweep(
        Database(db.app_url, connect_timeout=5, pool_size=1),
        SWEEP_REGISTRY,
        swept.enqueue,
        swept.retry_stalled,
        FakeHeartbeat(),
    )
    assert swept.queued == [
        QueuedJob(SUBMIT_LLM_BATCH, {"batch_submission_id": uuid.UUID(submission_id)})
    ]

    _task(parts, SUBMIT_LLM_BATCH.name)(batch_submission_id=submission_id)

    assert batch_status(db, uuid.UUID(submission_id))[:3] == (
        "needs_attention",
        None,
        "batch_submit_uncertain",
    )
    assert provider.batch_requests == []
