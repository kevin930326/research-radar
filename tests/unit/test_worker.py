"""worker 的監督迴圈：收到停止就取消、等它收尾；自己停了就隔一段時間重開（不連資料庫）。"""

import asyncio
import time
from typing import Any

import pytest
import structlog

from radar.adapters.jobs import job_id_middleware
from radar.domain.jobs import WORKER_HEARTBEAT_SECONDS, Queue
from radar.domain.settings import Settings
from radar.entrypoints.logging_config import configure_logging
from radar.entrypoints.worker.main import supervise, worker_options

SETTINGS = Settings.model_validate({"database_url": "postgresql://radar_app@db:5432/radar"})


def test_stop_cancels_running_worker_and_waits_for_cleanup() -> None:
    events: list[str] = []

    async def run_once() -> None:
        events.append("start")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # Procrastinate 收到取消時會等手上的工作做完；這裡用一小段等待代表收尾。
            await asyncio.sleep(0.05)
            events.append("cleaned-up")
            raise

    async def scenario() -> None:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.1, stop.set)
        await supervise(run_once, stop=stop, restart_interval=60)

    started = time.monotonic()
    asyncio.run(scenario())

    assert events == ["start", "cleaned-up"]
    assert time.monotonic() - started < 5


def test_failed_start_is_logged_and_restarted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    calls: list[int] = []

    async def run_once() -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            raise ConnectionError("could not connect to radar_app:s3cret@db")
        await asyncio.sleep(60)

    async def scenario() -> None:
        stop = asyncio.Event()

        async def stop_after_restart() -> None:
            while len(calls) < 2:
                await asyncio.sleep(0.01)
            stop.set()

        stopper = asyncio.create_task(stop_after_restart())
        await supervise(run_once, stop=stop, restart_interval=0.05)
        await stopper

    asyncio.run(scenario())

    assert calls == [0, 1]
    out = capsys.readouterr().out
    assert '"error_type": "ConnectionError"' in out
    assert "s3cret" not in out


def test_worker_that_returns_by_itself_is_restarted() -> None:
    calls: list[int] = []

    async def run_once() -> None:
        calls.append(len(calls))
        if len(calls) < 3:
            return
        await asyncio.sleep(60)

    async def scenario() -> None:
        stop = asyncio.Event()

        async def stop_after_third() -> None:
            while len(calls) < 3:
                await asyncio.sleep(0.01)
            stop.set()

        stopper = asyncio.create_task(stop_after_third())
        await supervise(run_once, stop=stop, restart_interval=0.01)
        await stopper

    asyncio.run(scenario())

    assert calls == [0, 1, 2]


def test_stop_during_restart_wait_returns_promptly() -> None:
    async def run_once() -> None:
        raise ConnectionError("down")

    async def scenario() -> None:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.1, stop.set)
        # 重開的間隔很長；停止要在等待中途就生效，不用等滿。
        await supervise(run_once, stop=stop, restart_interval=60)

    started = time.monotonic()
    asyncio.run(scenario())

    assert time.monotonic() - started < 5


def test_worker_options() -> None:
    settings = Settings.model_validate(
        {
            "database_url": "postgresql://radar_app@db:5432/radar",
            "worker_concurrency": 3,
            "worker_shutdown_graceful_timeout": 12,
            "worker_stalled_seconds": 90,
        }
    )

    options = worker_options(settings)

    assert options["queues"] == ["ingest", "ai", "pdf", "maintenance"]
    assert set(options["queues"]) == {queue.value for queue in Queue}
    assert options["concurrency"] == 3
    assert options["shutdown_graceful_timeout"] == 12
    assert options["install_signal_handlers"] is False
    assert options["delete_jobs"] == "never"
    assert options["update_heartbeat_interval"] == WORKER_HEARTBEAT_SECONDS
    assert options["stalled_worker_timeout"] == 90
    assert options["worker_middleware"] == [job_id_middleware]


def test_default_worker_settings() -> None:
    assert SETTINGS.worker_concurrency == 2
    # 判定 worker 不在的秒數要比報到間隔長得多，避免把活著的 worker 的工作重跑。
    assert SETTINGS.worker_stalled_seconds >= 3 * WORKER_HEARTBEAT_SECONDS


class _FakeJob:
    id = 42


class _FakeContext:
    job = _FakeJob()


def test_job_id_middleware_binds_job_id_for_sync_work_in_thread(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    log = structlog.get_logger("test")

    def sync_task() -> str:
        log.info("工作中")
        return "done"

    async def call_next() -> Any:
        # 和 Procrastinate 跑同步工作一樣：丟到別的執行緒。
        return await asyncio.to_thread(sync_task)

    async def scenario() -> Any:
        result = await job_id_middleware(call_next, _FakeContext(), None)  # type: ignore[arg-type]
        log.info("工作外")
        return result

    assert asyncio.run(scenario()) == "done"
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert '"job_id": 42' in lines[0]
    assert '"job_id": null' in lines[1]
