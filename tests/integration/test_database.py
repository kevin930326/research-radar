import time

from radar.repo.db import Database


def test_ping_returns_false_for_refused_address() -> None:
    database = Database("postgresql://radar@127.0.0.1:1/radar", connect_timeout=2)

    assert database.ping() is False


def test_ping_returns_false_within_timeout_for_silent_server(silent_port: int) -> None:
    # 沒把 connect_timeout 傳下去的話，libpq 會一直等下去，測試就不會結束。
    database = Database(f"postgresql://radar@127.0.0.1:{silent_port}/radar", connect_timeout=2)

    started = time.monotonic()
    assert database.ping() is False
    assert time.monotonic() - started < 10
