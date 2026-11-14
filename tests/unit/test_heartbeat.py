"""HttpHeartbeat：對設定的網址送 GET；沒設網址只記警告；送不出去只記錯誤、不丟例外。"""

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import httpx2
import pytest
import stamina

from radar.adapters.heartbeat import HttpHeartbeat, parse_retry_after
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


# --- 重試的等待時間（不真的睡：攔下 time.sleep，記下每次要等幾秒）----------------------------


def _responses(
    replies: list[httpx2.Response], seen: list[httpx2.Request]
) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return replies.pop(0) if replies else httpx2.Response(200)

    return handler


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """關掉 stamina 的測試模式（它會把等待歸零），改成攔下 sleep、只記秒數。"""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    with stamina.set_testing(False):
        yield slept


def _timed_heartbeat(
    handler: Callable[[httpx2.Request], httpx2.Response], *, attempts: int = 3
) -> HttpHeartbeat:
    return HttpHeartbeat(
        {"sweep": URL},
        timeout=5,
        attempts=attempts,
        wait_initial=2.0,
        wait_max=3.0,
        retry_after_max=30.0,
        wait_jitter=0.0,
        transport=httpx2.MockTransport(handler),
    )


def test_429_waits_the_seconds_given_by_retry_after(waits: list[float]) -> None:
    seen: list[httpx2.Request] = []
    replies = [httpx2.Response(429, headers={"Retry-After": "7"})]

    _timed_heartbeat(_responses(replies, seen))("sweep")

    assert len(seen) == 2
    assert waits == [7.0]


def test_retry_after_is_capped(waits: list[float]) -> None:
    # 對方給極大值（一天）時最多等 retry_after_max 秒，不卡住 worker。
    seen: list[httpx2.Request] = []
    replies = [httpx2.Response(429, headers={"Retry-After": "86400"})]

    _timed_heartbeat(_responses(replies, seen))("sweep")

    assert len(seen) == 2
    assert waits == [30.0]


def test_without_retry_after_uses_configured_backoff(waits: list[float]) -> None:
    # 從 wait_initial（2）開始每次加倍、最多 wait_max（3）；次數照 attempts。
    seen: list[httpx2.Request] = []
    replies = [httpx2.Response(503), httpx2.Response(429), httpx2.Response(500)]

    _timed_heartbeat(_responses(replies, seen), attempts=4)("sweep")

    assert len(seen) == 4
    assert waits == [2.0, 3.0, 3.0]


def test_unreadable_retry_after_falls_back_to_backoff(waits: list[float]) -> None:
    seen: list[httpx2.Request] = []
    replies = [httpx2.Response(429, headers={"Retry-After": "soon"})]

    _timed_heartbeat(_responses(replies, seen))("sweep")

    assert waits == [2.0]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("0", 0.0),
        (" 12 ", 12.0),
        ("-5", None),
        ("1.5", None),
        ("soon", None),
        ("²", None),
        ("Sat, 14 Nov 2026 00:01:40 GMT", 100.0),
        ("Fri, 13 Nov 2026 23:00:00 GMT", 0.0),
    ],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    now = datetime(2026, 11, 14, 0, 0, 0, tzinfo=UTC)

    assert parse_retry_after(header, now=now) == expected
