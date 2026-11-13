"""worker 行程的進入點。

目前是閒置迴圈：每一輪檢查有沒有收到關機訊號；收到 SIGTERM／SIGINT 就做完這一輪、
記一筆「正常關機」、以退出碼 0 結束。Story 1.4 換成 Procrastinate 的 worker，
屆時 `worker_shutdown_graceful_timeout` 交給它的 `shutdown_graceful_timeout`。
"""

import signal
import threading
import time
from types import FrameType

import structlog

from radar.config import ConfigError, load_settings
from radar.entrypoints.logging_config import configure_logging

log = structlog.get_logger(__name__)

TICK_SECONDS = 0.5


def run_idle_loop(stop: threading.Event, tick_seconds: float = TICK_SECONDS) -> None:
    """一直跑到 `stop` 被設起來；每一輪做完才檢查，所以不會做到一半就停。"""
    while not stop.is_set():
        time.sleep(tick_seconds)


def main() -> int:
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("設定錯誤，無法啟動", service="worker", problems=exc.problems)
        return 1

    stop = threading.Event()
    received: list[str] = []

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        # 訊號處理函式裡只設旗標，不寫日誌（避免在寫日誌途中被打斷時重入）。
        received.append(signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    log.info(
        "worker 啟動",
        service="worker",
        shutdown_graceful_timeout=settings.worker_shutdown_graceful_timeout,
    )
    run_idle_loop(stop)
    log.info("正常關機", service="worker", signal=received[0] if received else None)
    return 0
