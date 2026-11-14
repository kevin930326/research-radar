"""HttpHeartbeat 對本機的 HTTP 伺服器：真的送出 GET；伺服器先回 503 時會重試。"""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
import stamina

from radar.adapters.heartbeat import HttpHeartbeat


class _Recorder(BaseHTTPRequestHandler):
    paths: ClassVar[list[str]] = []
    statuses: ClassVar[list[int]] = []

    def do_GET(self) -> None:
        type(self).paths.append(self.path)
        status = type(self).statuses.pop(0) if type(self).statuses else 200
        self.send_response(status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *_args: object) -> None:
        pass  # 不印到 stderr


@pytest.fixture
def server() -> Iterator[tuple[str, type[_Recorder]]]:
    handler = type("Recorder", (_Recorder,), {"paths": [], "statuses": []})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", handler
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_sends_get_to_configured_url(server: tuple[str, type[_Recorder]]) -> None:
    base, handler = server

    HttpHeartbeat({"sweep": f"{base}/ping/abc"}, timeout=5, attempts=3)("sweep")

    assert handler.paths == ["/ping/abc"]


def test_retries_after_server_error(server: tuple[str, type[_Recorder]]) -> None:
    base, handler = server
    handler.statuses = [503]

    with stamina.set_testing(True, attempts=100, cap=True):
        HttpHeartbeat({"sweep": f"{base}/ping/abc"}, timeout=5, attempts=3)("sweep")

    assert handler.paths == ["/ping/abc", "/ping/abc"]
