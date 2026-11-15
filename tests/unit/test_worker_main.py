"""worker 的 main() 怎麼組零件：連線數的拆法、心跳網址、卡住工作的重試上限、工作層重試
（不連資料庫、不跑 Procrastinate worker；環境變數只用測試值）。
"""

from datetime import timedelta
from functools import partial
from typing import Any

import procrastinate
import pytest

import radar.entrypoints.worker.main as worker_main
from radar.adapters.heartbeat import HttpHeartbeat
from radar.adapters.jobs import retry_stalled_jobs
from radar.domain.jobs import SWEEP
from radar.domain.settings import Settings
from radar.entrypoints.bootstrap import build_worker
from radar.entrypoints.worker import tasks as worker_tasks
from radar.entrypoints.worker.tasks import register_tasks
from radar.services.llm_gateway import expire_stale_runs

APP_URL = "postgresql://radar_app@db:5432/radar"
HEARTBEAT_URL = "https://hc.example.test/ping/abc"


class _StopBeforeRunningError(Exception):
    """組完零件就停，不真的跑 worker。"""


def _record_build(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    def fake_build(_settings: Settings, **kwargs: Any) -> Any:
        recorded.append(kwargs)
        raise _StopBeforeRunningError

    monkeypatch.setattr(worker_main, "build_worker", fake_build)
    monkeypatch.setenv("DATABASE_URL", APP_URL)
    for name in ("RADAR_WORKER_DATABASE_POOL_SIZE", "RADAR_HEARTBEAT_URL_SWEEP"):
        monkeypatch.delenv(name, raising=False)
    return recorded


def test_worker_uses_default_connection_split(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record_build(monkeypatch)

    with pytest.raises(_StopBeforeRunningError):
        worker_main.main()

    assert recorded == [{"queue_pool_size": 2, "app_pool_size": 3, "heartbeat_urls": {}}]


def test_worker_follows_pool_size_override_and_heartbeat_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _record_build(monkeypatch)
    monkeypatch.setenv("RADAR_WORKER_DATABASE_POOL_SIZE", "9")
    monkeypatch.setenv("RADAR_HEARTBEAT_URL_SWEEP", HEARTBEAT_URL)

    with pytest.raises(_StopBeforeRunningError):
        worker_main.main()

    assert recorded == [
        {"queue_pool_size": 2, "app_pool_size": 6, "heartbeat_urls": {"sweep": HEARTBEAT_URL}}
    ]


def test_worker_pool_too_small_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record_build(monkeypatch)
    monkeypatch.setenv("RADAR_WORKER_DATABASE_POOL_SIZE", "2")

    assert worker_main.main() == 1
    assert recorded == []


SETTINGS = Settings.model_validate(
    {
        "database_url": APP_URL,
        "worker_stalled_seconds": 90,
        "job_max_retries": 5,
        "job_retry_wait": 7,
    }
)


def test_retry_stalled_uses_stalled_seconds_and_retry_limit() -> None:
    parts = build_worker(SETTINGS, queue_pool_size=2, app_pool_size=3, heartbeat_urls={})

    retry_stalled = parts.retry_stalled
    assert isinstance(retry_stalled, partial)
    assert retry_stalled.func is retry_stalled_jobs
    assert retry_stalled.keywords == {"seconds_since_heartbeat": 90, "max_retries": 5}


def test_sweep_task_retry_strategy_follows_settings() -> None:
    parts = build_worker(SETTINGS, queue_pool_size=2, app_pool_size=3, heartbeat_urls={})
    register_tasks(parts.job_app.app, parts, SETTINGS)

    strategy = parts.job_app.app.tasks[SWEEP.name].retry_strategy
    assert isinstance(strategy, procrastinate.RetryStrategy)
    assert (strategy.max_attempts, strategy.wait, strategy.linear_wait) == (5, 7, 7)


def test_heartbeat_retry_waits_follow_settings() -> None:
    settings = Settings.model_validate(
        {
            "database_url": APP_URL,
            "heartbeat_attempts": 4,
            "heartbeat_retry_wait_initial": 0.5,
            "heartbeat_retry_wait_max": 20,
            "heartbeat_retry_after_max": 120,
        }
    )
    parts = build_worker(settings, queue_pool_size=2, app_pool_size=3, heartbeat_urls={})

    heartbeat = parts.send_heartbeat
    assert isinstance(heartbeat, HttpHeartbeat)
    assert (
        heartbeat._attempts,
        heartbeat._wait_initial,
        heartbeat._wait_max,
        heartbeat._retry_after_max,
    ) == (4, 0.5, 20.0, 120)


def test_heartbeat_constructor_defaults_match_settings_defaults() -> None:
    # 預設值寫在兩處（設定與 adapter 的建構子），要一致。
    defaults = Settings.model_validate({"database_url": APP_URL})
    heartbeat = HttpHeartbeat({}, timeout=5, attempts=3)

    assert (heartbeat._wait_initial, heartbeat._wait_max, heartbeat._retry_after_max) == (
        defaults.heartbeat_retry_wait_initial,
        defaults.heartbeat_retry_wait_max,
        defaults.heartbeat_retry_after_max,
    )


def test_sweep_task_expires_stale_llm_runs_with_configured_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # sweep 是唯一把卡住的互動 derivation_run 改成 failed 的地方：一定要傳 expire_runs。
    captured: dict[str, Any] = {}

    def fake_sweep(*args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(worker_tasks, "sweep", fake_sweep)
    parts = build_worker(SETTINGS, queue_pool_size=2, app_pool_size=3, heartbeat_urls={})
    register_tasks(parts.job_app.app, parts, SETTINGS)

    parts.job_app.app.tasks[SWEEP.name].func()

    expire_runs = captured["expire_runs"]
    assert isinstance(expire_runs, partial)
    assert expire_runs.func is expire_stale_runs
    assert expire_runs.args[1] is parts.database
    assert expire_runs.args[2] == timedelta(seconds=SETTINGS.llm_run_stale_after)
