import threading
import time

from radar.entrypoints.worker.main import run_idle_loop


def test_idle_loop_returns_when_already_stopped() -> None:
    stop = threading.Event()
    stop.set()

    started = time.monotonic()
    run_idle_loop(stop, tick_seconds=5)

    assert time.monotonic() - started < 1


def test_idle_loop_stops_after_current_tick() -> None:
    stop = threading.Event()
    timer = threading.Timer(0.1, stop.set)
    timer.start()

    started = time.monotonic()
    run_idle_loop(stop, tick_seconds=0.05)

    assert stop.is_set()
    assert time.monotonic() - started < 2
