"""HttpHeartbeat：對設定的網址送 GET；沒設網址只記警告；送不出去只記錯誤、不丟例外。"""

from collections.abc import Callable, Iterator

import httpx2
import pytest
import stamina

from radar.adapters.heartbeat import HttpHeartbeat
from radar.entrypoints.logging_config import configure_logging

URL = "https://hc-ping.example/s3cret-TOKEN"


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Iterator[None]:
    # stamina 的測試模式：重試不等待，次數照設定（cap=True 只設上限）。
    with stamina.set_testing(True, attempts=100, cap=True):
        yield


def _transport(
    statuses: list[int], seen: list[httpx2.Request]
) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(statuses.pop(0) if statuses else 200)

    return handler


def _heartbeat(
    handler: Callable[[httpx2.Request], httpx2.Response], attempts: int = 3
) -> HttpHeartbeat:
    return HttpHeartbeat(
        {"sweep": URL},
        timeout=5,
        attempts=attempts,
        transport=httpx2.MockTransport(handler),
    )


def test_sends_one_get_to_the_checks_url(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    seen: list[httpx2.Request] = []

    _heartbeat(_transport([200], seen))("sweep")

    assert [(request.method, str(request.url)) for request in seen] == [("GET", URL)]
    out = capsys.readouterr().out
    assert "已送出心跳" in out
    assert "s3cret-TOKEN" not in out


def test_check_without_url_logs_warning_and_sends_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    seen: list[httpx2.Request] = []

    _heartbeat(_transport([], seen))("backup")

    assert seen == []
    assert "沒有設定心跳網址" in capsys.readouterr().out


def test_server_error_is_retried(capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[httpx2.Request] = []

    _heartbeat(_transport([503, 429, 200], seen))("sweep")

    assert len(seen) == 3


def test_gives_up_after_attempts_without_raising(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    seen: list[httpx2.Request] = []

    _heartbeat(_transport([500, 500, 500, 500], seen), attempts=2)("sweep")

    assert len(seen) == 2
    out = capsys.readouterr().out
    assert "心跳送出失敗" in out
    assert "s3cret-TOKEN" not in out


def test_client_error_is_not_retried(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    seen: list[httpx2.Request] = []

    _heartbeat(_transport([404], seen))("sweep")

    assert len(seen) == 1
    out = capsys.readouterr().out
    assert '"reason": "HTTP 404"' in out
    assert "s3cret-TOKEN" not in out


def test_redirect_is_a_failure_not_sent(capsys: pytest.CaptureFixture[str]) -> None:
    # httpx2 不跟著轉址：回 301 代表監看服務沒收到，不能記成「已送出」。
    configure_logging()
    seen: list[httpx2.Request] = []

    _heartbeat(_transport([301], seen))("sweep")

    assert len(seen) == 1
    out = capsys.readouterr().out
    assert '"reason": "HTTP 301"' in out
    assert "已送出心跳" not in out
    assert "s3cret-TOKEN" not in out


def test_network_error_is_retried_then_logged(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        raise httpx2.ConnectError(f"cannot reach {URL}", request=request)

    _heartbeat(handler)("sweep")

    assert len(calls) == 3
    out = capsys.readouterr().out
    assert '"reason": "ConnectError"' in out
    assert "s3cret-TOKEN" not in out
