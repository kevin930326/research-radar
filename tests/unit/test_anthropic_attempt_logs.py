"""Anthropic adapter 每次失敗的嘗試記一筆警告日誌（Story 2.10、ADR-0042）：只有操作、錯誤類別、
狀態碼、錯誤本文的 `error.type`（只收 `^[a-z_]{1,64}$`）與頂層鍵（只收英數底線）；不含錯誤訊息、
金鑰、送出的文件與回應內容。重試的每一次都記；成功的那次不記。

`httpx2.MockTransport`＋手寫的錯誤本文（不是真的錄的，形狀照官方文件
`{"type": "error", "error": {"type": ..., "message": ...}, "request_id": ...}`），一個請求都不
送出去。stamina 自己的重試日誌（2.1 起就有）不在這裡檢查。
"""

from collections.abc import Iterator, MutableMapping, Sequence
from typing import Any

import httpx2
import pytest
import stamina
from structlog.testing import capture_logs

from radar.adapters.anthropic import AnthropicLlmProvider
from radar.domain.llm import (
    LlmBatchRequest,
    LlmConfigurationError,
    LlmConnectionError,
    LlmError,
    LlmRateLimitedError,
    LlmRequest,
    LlmTimeoutError,
)
from radar.domain.policy import CloudPolicy, ContentSource, clear_for_cloud
from tests.anthropic_cassette import cassette_response
from tests.prompt_support import template_text

FAKE_KEY = "sk-ant-FAKE-KEY-for-tests-only"
SECRET_ABSTRACT = "（假摘要）不能出現在日誌 LOG-MARKER-4K"
SECRET_MESSAGE = "error message text LOG-MESSAGE-7Z"
ALLOWED_KEYS = {
    "event",
    "log_level",
    "operation",
    "error_class",
    "http_status",
    "api_error_type",
    "body_keys",
}


def _request() -> LlmRequest:
    policy = CloudPolicy()
    return LlmRequest(
        model="claude-haiku-5-5",
        system=template_text("請把摘要整理成四欄，回 JSON。"),
        content=(clear_for_cloud(SECRET_ABSTRACT, ContentSource.OPENALEX_ABSTRACT, None, policy),),
        max_tokens=1024,
    )


def _error(status: int, body: object) -> httpx2.Response:
    return httpx2.Response(status, json=body)


def _provider(*replies: httpx2.Response | Exception) -> AnthropicLlmProvider:
    queue = list(replies)

    def handler(_request: httpx2.Request) -> httpx2.Response:
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return AnthropicLlmProvider(
        FAKE_KEY,
        timeout=30,
        attempts=3,
        retry_wait_initial=1.0,
        retry_wait_max=10.0,
        retry_wait_jitter=1.0,
        retry_after_max=60,
        transport=httpx2.MockTransport(handler),
    )


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Iterator[None]:
    with stamina.set_testing(True, attempts=100, cap=True):
        yield


def _failures(logs: Sequence[MutableMapping[str, Any]]) -> list[MutableMapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == "Anthropic 請求失敗"]


def _assert_clean(entries: Sequence[MutableMapping[str, Any]]) -> None:
    for entry in entries:
        assert set(entry) <= ALLOWED_KEYS
        text = repr(entry)
        for secret in (FAKE_KEY, SECRET_ABSTRACT, "LOG-MARKER-4K", SECRET_MESSAGE, "req_"):
            assert secret not in text


def test_each_failed_attempt_is_logged_and_success_is_not() -> None:
    rate_limited = {
        "type": "error",
        "error": {"type": "rate_limit_error", "message": SECRET_MESSAGE},
        "request_id": "req_secret_1",
    }
    provider = _provider(
        _error(429, rate_limited),
        _error(529, {"type": "error", "error": {"type": "overloaded_error"}}),
        cassette_response("ok_text"),
    )

    with capture_logs() as logs:
        provider.complete(_request())

    failures = _failures(logs)
    assert failures == [
        {
            "event": "Anthropic 請求失敗",
            "log_level": "warning",
            "operation": "messages",
            "error_class": "LlmRateLimitedError",
            "http_status": 429,
            "api_error_type": "rate_limit_error",
            "body_keys": ["error", "request_id", "type"],
        },
        {
            "event": "Anthropic 請求失敗",
            "log_level": "warning",
            "operation": "messages",
            "error_class": "LlmServerError",
            "http_status": 529,
            "api_error_type": "overloaded_error",
            "body_keys": ["error", "type"],
        },
    ]
    _assert_clean(_failures(logs))


def test_odd_error_types_and_keys_are_masked() -> None:
    body = {
        "type": "error",
        "error": {"type": "Weird-Type " + SECRET_MESSAGE, "message": SECRET_MESSAGE},
        "has space": 1,
        "x" * 65: 2,
    }
    provider = _provider(_error(401, body))

    with capture_logs() as logs, pytest.raises(LlmConfigurationError):
        provider.complete(_request())

    [failure] = _failures(logs)
    assert failure["api_error_type"] == "<?>"
    assert failure["body_keys"] == ["<?>", "<?>", "error", "type"]
    assert failure["http_status"] == 401
    _assert_clean(_failures(logs))


def test_non_json_body_and_connection_errors() -> None:
    provider = _provider(
        httpx2.Response(503, content=b"<html>" + SECRET_MESSAGE.encode() + b"</html>"),
        httpx2.ConnectError("boom " + SECRET_MESSAGE),
        httpx2.ReadTimeout("slow " + SECRET_MESSAGE),
    )

    with capture_logs() as logs, pytest.raises(Exception, match="逾時"):
        provider.complete(_request())

    failures = _failures(logs)
    assert [(f["error_class"], f["http_status"]) for f in failures] == [
        ("LlmServerError", 503),
        ("LlmConnectionError", None),
        ("LlmTimeoutError", None),
    ]
    assert all(f["api_error_type"] is None and f["body_keys"] is None for f in failures)
    _assert_clean(_failures(logs))


def test_batch_submission_failure_is_logged_once() -> None:
    provider = _provider(
        _error(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "x"}})
    )
    requests = [LlmBatchRequest(custom_id="0" * 31 + "a", request=_request())]

    with capture_logs() as logs, pytest.raises(LlmRateLimitedError):
        provider.submit_batch(requests)

    [failure] = _failures(logs)
    assert failure["operation"] == "batches"
    assert failure["api_error_type"] == "rate_limit_error"
    _assert_clean(_failures(logs))


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (httpx2.ReadTimeout("slow " + SECRET_MESSAGE), LlmTimeoutError),
        (httpx2.ConnectError("boom " + SECRET_MESSAGE), LlmConnectionError),
    ],
)
def test_batch_transport_failure_is_logged_once(
    raised: Exception, expected: type[LlmError]
) -> None:
    provider = _provider(raised)
    requests = [LlmBatchRequest(custom_id="0" * 31 + "a", request=_request())]

    with capture_logs() as logs, pytest.raises(expected):
        provider.submit_batch(requests)

    [failure] = _failures(logs)
    assert failure["operation"] == "batches"
    assert failure["error_class"] == expected.__name__
    assert failure["http_status"] is None
    _assert_clean(_failures(logs))
