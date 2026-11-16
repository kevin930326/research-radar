"""worker 登記的向量化工作（`embed_papers`，`register_tasks` 裡的那個函式）對真的 PostgreSQL
（Story 2.8）：

- 照設定檔與 bootstrap 建 provider（這裡換成假的 `FakeEmbeddingProvider`，不連網路），跑完關掉。
- 缺 Cloudflare 的 account id 或 token：`ConfigError` 原樣往上丟，一篇都不寫。
- 有一批失敗：工作丟錯（Procrastinate 記成失敗、不重試）；額度停下不算失敗。
- 每週更新的套用段在 worker 裡會帶著 `enqueue`：發佈快照後排入 `embed_papers`。

不跑 Procrastinate worker；假模型 `fake-embedding`（8 維）由超級使用者登記；環境變數只用測試值。
"""

import dataclasses
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

import radar.entrypoints.worker.tasks as worker_tasks
from radar.adapters.fake import FakeEmbeddingProvider, FakeHeartbeat
from radar.adapters.fake.jobs import FakeJobQueue, QueuedJob
from radar.config import ConfigError
from radar.domain.embeddings import EmbeddingRequestError, EmbeddingResponseError
from radar.domain.jobs import EMBED_PAPERS, RUN_WEEKLY_UPDATE
from radar.domain.policy import CloudSafeText, cloud_text
from radar.domain.settings import Settings
from radar.entrypoints.bootstrap import WorkerParts, build_worker
from radar.repo.db import Database
from tests.database import APP_ROLE, OWNER_ROLE, ROOT, TestDatabase, migrate
from tests.llm_support import create_paper, execute

MODEL = "fake-embedding"


class _ClosingFake(FakeEmbeddingProvider):
    """可以 `with` 的假 provider；記下有沒有關。"""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(8, model_id=MODEL)
        self.closed = False
        self.fail = fail

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        if self.fail:
            raise EmbeddingResponseError("回應的 success 不是 true", http_status=200)
        return super().embed_documents(texts)

    def __enter__(self) -> _ClosingFake:
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
    execute(
        fresh_database,
        "INSERT INTO embedding_model (model_id, provider, dimensions) VALUES (%s, 'fake', 8)",
        MODEL,
    )
    return fresh_database


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    custom = tmp_path / "config"
    shutil.copytree(ROOT / "config", custom)
    path = custom / "embeddings.yaml"
    text = path.read_text(encoding="utf-8")
    text = text.replace('model_id: "@cf/qwen/qwen3-embedding-0.6b"', f'model_id: "{MODEL}"')
    text = text.replace("dimensions: 1024", "dimensions: 8")
    path.write_text(text, encoding="utf-8")
    return custom


def _parts(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch, config_dir: Path, **settings: Any
) -> tuple[WorkerParts, Settings]:
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))
    worker_settings = Settings.model_validate({"database_url": db.app_url, **settings})
    parts = build_worker(worker_settings, queue_pool_size=1, app_pool_size=1, heartbeat_urls={})
    return parts, worker_settings


def _task(
    db: TestDatabase,
    monkeypatch: pytest.MonkeyPatch,
    config_dir: Path,
    name: str = EMBED_PAPERS.name,
    *,
    queue: FakeJobQueue | None = None,
    **settings: Any,
) -> Callable[..., None]:
    parts, worker_settings = _parts(db, monkeypatch, config_dir, **settings)
    if queue is not None:
        parts = dataclasses.replace(parts, enqueue=queue.enqueue, send_heartbeat=FakeHeartbeat())
    worker_tasks.register_tasks(parts.job_app.app, parts, worker_settings)
    func: Callable[..., None] = parts.job_app.app.tasks[name].func
    return func


def _use_fake(monkeypatch: pytest.MonkeyPatch, provider: _ClosingFake) -> list[Any]:
    built: list[Any] = []

    def build(settings: Settings, config: Any) -> _ClosingFake:
        built.append(config.model_id)
        return provider

    monkeypatch.setattr(worker_tasks, "build_embedding_provider", build)
    return built


def _count(db: TestDatabase, table: str) -> int:
    return int(execute(db, f"SELECT count(*) FROM {table}")[0][0])


def test_embed_task_embeds_and_closes_the_provider(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:
    create_paper(Database(db.app_url, connect_timeout=5, pool_size=1))
    task = _task(db, monkeypatch, config_dir)
    provider = _ClosingFake()
    built = _use_fake(monkeypatch, provider)

    task()

    assert built == [MODEL]
    assert provider.closed
    assert len(provider.document_calls) == 1
    assert _count(db, "embedding") == 1


def test_missing_cloudflare_settings_raise_config_error(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:
    create_paper(Database(db.app_url, connect_timeout=5, pool_size=1))
    task = _task(db, monkeypatch, config_dir)

    with pytest.raises(ConfigError) as caught:
        task()

    assert caught.value.problems == [
        "缺少環境變數 RADAR_CLOUDFLARE_ACCOUNT_ID",
        "缺少環境變數 RADAR_CLOUDFLARE_API_TOKEN",
    ]
    assert _count(db, "embedding") == 0
    assert _count(db, "derivation_run") == 0


def test_failed_batch_makes_the_job_fail(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:
    create_paper(Database(db.app_url, connect_timeout=5, pool_size=1))
    task = _task(db, monkeypatch, config_dir)
    provider = _ClosingFake(fail=True)
    _use_fake(monkeypatch, provider)

    with pytest.raises(worker_tasks.EmbeddingJobFailedError, match="embed_response_invalid"):
        task()

    assert provider.closed
    assert _count(db, "embedding") == 0


def test_quota_stop_ends_the_job_normally(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:
    create_paper(Database(db.app_url, connect_timeout=5, pool_size=1))
    path = config_dir / "embeddings.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "daily_free_neurons: 10000", "daily_free_neurons: 1"
        ),
        encoding="utf-8",
    )
    execute(
        db,
        "INSERT INTO api_call (source, operation, http_status, cost_usd, estimated_input_tokens,"
        " called_at) VALUES ('workers_ai', 'embed_documents', 200, 0, 100000, now())",
    )
    task = _task(db, monkeypatch, config_dir)
    provider = _ClosingFake()
    _use_fake(monkeypatch, provider)

    task()

    assert provider.document_calls == []
    assert _count(db, "embedding") == 0


def test_weekly_apply_in_the_worker_enqueues_embeddings(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:
    # 已經抓完、還沒套用的一筆：worker 的 run_weekly_update 只讀追蹤範圍、套用、發佈，再排入向量化。
    with db.connect(APP_ROLE) as connection:
        row = connection.execute(
            "INSERT INTO weekly_run (status) VALUES ('fetching') RETURNING id"
        ).fetchone()
    assert row is not None
    run_id = row[0]
    with db.connect(OWNER_ROLE) as connection:
        connection.execute(
            "UPDATE weekly_run SET fetch_finished_at = now() WHERE id = %s", (run_id,)
        )
    queue = FakeJobQueue()
    task = _task(db, monkeypatch, config_dir, RUN_WEEKLY_UPDATE.name, queue=queue)

    task(weekly_run_id=str(run_id))

    assert queue.queued == [QueuedJob(EMBED_PAPERS, {})]


def test_embed_task_is_not_retried_by_the_queue(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:
    # 暫時性錯誤 adapter 已經重試過，設定錯重跑也一樣：工作層不重試，留給下一次排入或回填指令。
    parts, worker_settings = _parts(db, monkeypatch, config_dir, job_max_retries=3)
    worker_tasks.register_tasks(parts.job_app.app, parts, worker_settings)
    tasks = parts.job_app.app.tasks

    assert tasks[EMBED_PAPERS.name].retry_strategy is None
    assert tasks[EMBED_PAPERS.name].queue == "ai"
    assert tasks[RUN_WEEKLY_UPDATE.name].retry_strategy is not None


class _RejectsPaperOne(_ClosingFake):
    """送出的這一批裡有論文 1 就回請求不合法（400），其他照常（審查後的修正，Codex 中 4）。"""

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        vectors = super().embed_documents(texts)
        if any("Paper 1 " in cloud_text(text) for text in texts):
            raise EmbeddingRequestError("HTTP 400", http_status=400)
        return vectors


def test_single_rejected_paper_makes_the_job_fail_after_finishing_the_rest(
    db: TestDatabase, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:
    database = Database(db.app_url, connect_timeout=5, pool_size=1)
    for n in (1, 2):
        create_paper(
            database,
            doi=f"10.1/p{n}",
            openalex_id=f"W{n}",
            title=f"Paper {n} on platform adoption",
            abstract=f"Abstract {n}: teams adopt platforms.",
        )
    task = _task(db, monkeypatch, config_dir)
    provider = _RejectsPaperOne()
    _use_fake(monkeypatch, provider)

    with pytest.raises(worker_tasks.EmbeddingJobFailedError, match="embed_request_invalid"):
        task()

    assert provider.closed
    assert _count(db, "embedding") == 1
