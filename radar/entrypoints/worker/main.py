"""worker 行程的進入點：Procrastinate 的 worker，外面包一層監督迴圈（ADR-0019）。

- 四條佇列、同時跑 `worker_concurrency` 個工作；`pdf` 的工作排入時帶同一把鎖。
- 收到 SIGTERM／SIGINT：停止接新工作、等手上的做完（最多 `worker_shutdown_graceful_timeout` 秒），
  記「正常關機」（帶訊號名稱），退出碼 0。同步的工作超過時限仍會跑完（Procrastinate 只能取消
  非同步的工作）；Docker 在 `stop_grace_period` 後強制結束時，那個工作由 sweep 重試。
- 起不來（資料庫連不上、還沒遷移）或佇列中斷：記警告，隔 `worker_restart_interval` 秒重開；
  等待期間一樣可以被訊號停下。工作本身失敗由 Procrastinate 記成 failed，不會讓迴圈結束。
- 服務啟動時不遷移、不排任何工作（AD-15）。
"""

import asyncio
import contextlib
import signal
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from procrastinate.app import WorkerOptions

from radar.adapters.jobs import job_id_middleware
from radar.config import (
    ConfigError,
    WorkerConnections,
    heartbeat_urls,
    load_domain_config,
    load_settings,
    worker_connection_split,
)
from radar.domain.jobs import WORKER_HEARTBEAT_SECONDS, Queue
from radar.domain.settings import Settings
from radar.entrypoints.bootstrap import build_worker
from radar.entrypoints.logging_config import configure_logging
from radar.entrypoints.worker.tasks import register_tasks

log = structlog.get_logger(__name__)

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def worker_options(settings: Settings) -> WorkerOptions:
    """交給 Procrastinate worker 的參數。"""
    return {
        "queues": [queue.value for queue in Queue],
        "concurrency": settings.worker_concurrency,
        "shutdown_graceful_timeout": settings.worker_shutdown_graceful_timeout,
        # 訊號由這裡的監督迴圈處理，Procrastinate 不另外裝。
        "install_signal_handlers": False,
        # 先保留做完的工作紀錄（看得到 succeeded／failed）；清理舊紀錄是之後的事。
        "delete_jobs": "never",
        "update_heartbeat_interval": WORKER_HEARTBEAT_SECONDS,
        "stalled_worker_timeout": settings.worker_stalled_seconds,
        "worker_middleware": [job_id_middleware],
    }


def _error_fields(error: BaseException) -> dict[str, str]:
    """日誌只記例外的類別名稱（訊息可能含主機與帳號）；包了一層的另外記原因的類別。"""
    fields = {"error_type": type(error).__name__}
    cause = error.__cause__ or error.__context__
    if cause is not None:
        fields["cause_type"] = type(cause).__name__
    return fields


async def supervise(
    run_once: Callable[[], Coroutine[Any, Any, None]],
    *,
    stop: asyncio.Event,
    restart_interval: float,
) -> None:
    """反覆執行 `run_once`（開連線池＋跑 Procrastinate worker），直到 `stop` 被設起來。

    `stop` 設起來時取消 `run_once`：Procrastinate 收到取消會停止接新工作、等手上的做完。
    `run_once` 自己結束（丟例外或正常回傳）時記警告，隔 `restart_interval` 秒再開。
    """
    while not stop.is_set():
        worker = asyncio.create_task(run_once(), name="radar-worker")
        stop_wait = asyncio.create_task(stop.wait(), name="radar-worker-stop")
        await asyncio.wait({worker, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.warning("worker 停止時發生錯誤", service="worker", **_error_fields(exc))
            return
        stop_wait.cancel()

        error = worker.exception()
        fields = _error_fields(error) if error is not None else {}
        log.warning(
            "worker 停了（資料庫連不上、還沒遷移或佇列中斷），稍後重開",
            service="worker",
            retry_in_seconds=restart_interval,
            **fields,
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=restart_interval)


async def run_until_signal(settings: Settings, connections: WorkerConnections) -> str | None:
    """先接手 SIGTERM／SIGINT，再記「worker 啟動」、組 worker、跑到收到訊號，回傳訊號名稱。

    訊號處理先裝好才記啟動：看到「worker 啟動」之後送的訊號一定會走正常關機。
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    received: list[str] = []

    def request_stop(signum: signal.Signals) -> None:
        received.append(signum.name)
        stop.set()

    for signum in STOP_SIGNALS:
        loop.add_signal_handler(signum, request_stop, signum)

    try:
        log.info(
            "worker 啟動",
            service="worker",
            queues=[queue.value for queue in Queue],
            concurrency=settings.worker_concurrency,
            shutdown_graceful_timeout=settings.worker_shutdown_graceful_timeout,
            database_connections=connections.total,
        )
        parts = build_worker(
            settings,
            queue_pool_size=connections.queue_pool,
            app_pool_size=connections.app_pool,
            heartbeat_urls=heartbeat_urls(settings),
        )
        register_tasks(parts.job_app.app, parts, settings)

        async def run_once() -> None:
            async with parts.job_app.opened_async() as app:
                await app.run_worker_async(**worker_options(settings))

        await supervise(run_once, stop=stop, restart_interval=settings.worker_restart_interval)
    finally:
        for signum in STOP_SIGNALS:
            loop.remove_signal_handler(signum)
    return received[0] if received else None


def main() -> int:
    configure_logging()
    try:
        # 領域設定（config/*.yaml）錯了也不啟動（AD-18）。
        load_domain_config()
        settings = load_settings()
        connections = worker_connection_split(settings)
    except ConfigError as exc:
        log.error("設定錯誤，無法啟動", service="worker", problems=exc.problems)
        return 1

    received = asyncio.run(run_until_signal(settings, connections))
    log.info("正常關機", service="worker", signal=received)
    return 0
