import socket
import time
from collections.abc import Iterator

import pytest

from radar.repo.db import Database


@pytest.fixture
def silent_port() -> Iterator[int]:
    """本機一個只 listen、不回應的埠：連得上，但永遠等不到資料庫的回覆。"""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port: int = server.getsockname()[1]
        yield port


def test_ping_returns_false_for_refused_address() -> None:
    database = Database("postgresql://radar@127.0.0.1:1/radar", connect_timeout=2)

    assert database.ping() is False


def test_ping_returns_false_within_timeout_for_silent_server(silent_port: int) -> None:
    # 沒把 connect_timeout 傳下去的話，libpq 會一直等下去，測試就不會結束。
    database = Database(f"postgresql://radar@127.0.0.1:{silent_port}/radar", connect_timeout=2)

    started = time.monotonic()
    assert database.ping() is False
    assert time.monotonic() - started < 10
