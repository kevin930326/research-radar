import socket
from collections.abc import Iterator

import pytest


@pytest.fixture
def silent_port() -> Iterator[int]:
    """本機一個只 listen、不回應的埠：連得上，但永遠等不到資料庫的回覆。"""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port: int = server.getsockname()[1]
        yield port
