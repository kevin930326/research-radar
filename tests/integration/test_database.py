import time

from radar.repo.db import Database


def test_ping_returns_false_for_refused_address() -> None:
    database = Database("postgresql://radar@127.0.0.1:1/radar", connect_timeout=2)

    assert database.ping() is False


def test_ping_returns_false_within_timeout_for_unroutable_address() -> None:
    # 192.0.2.0/24 是保留給文件的位址（TEST-NET-1），不會有人回應。
    database = Database("postgresql://radar@192.0.2.1:5432/radar", connect_timeout=2)

    started = time.monotonic()
    assert database.ping() is False
    assert time.monotonic() - started < 10
