"""Anthropic adapter 的 Batch 查詢與結果下載（`retrieve_batch`、`batch_results`，Story 2.11、
ADR-0043）：`httpx2.MockTransport`＋手寫假回應，一個請求都不送出去。

假回應與結果行不是真的錄的，形狀照官方文件（docs/runs/epic-2b/api-reference.md：查批次回
`message_batch` 物件；結果是 JSONL，每行 `{"custom_id": ..., "result": {"type": "succeeded" |
"errored" | "canceled" | "expired", ...}}`）。
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
import stamina
from structlog.testing import capture_logs

from radar.adapters.anthropic import AnthropicLlmProvider, to_batch_result, to_batch_status
from radar.domain.llm import (
    BatchProcessingStatus,
    LlmBatchResultKind,
    LlmConfigurationError,
    LlmConnectionError,
    LlmMalformedResponseError,
    LlmServerError,
    LlmTimeoutError,
)

FAKE_KEY = "sk-ant-FAKE-KEY-for-tests-only"
BATCH_ID = "msgbatch_01HkcTjaV5uDC8jWR4ZsDV8d"
CUSTOM = "0" * 31 + "a"
SECRET_OUTPUT = "（假輸出）RESULT-MARKER-9Z"
RESULTS_URL = f"https://api.anthropic.com/v1/messages/batches/{BATCH_ID}/results"


def _batch(**changes: object) -> dict[str, object]:
    """查批次的 200 回應（不是真的錄的，形狀照官方文件）。"""
    body: dict[str, object] = {
        "id": BATCH_ID,
        "type": "message_batch",
        "processing_status": "ended",
        "request_counts": {
            "processing": 0,
            "succeeded": 1,
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        },
        "ended_at": "2026-11-15T04:00:00Z",
        "created_at": "2026-11-15T03:00:00Z",
        "expires_at": "2026-11-16T03:00:00Z",
        "archived_at": None,
        "cancel_initiated_at": None,
        "results_url": RESULTS_URL,
    }
    body.update(changes)
    return body


def _message(text: str = SECRET_OUTPUT, **changes: object) -> dict[str, object]:
    message: dict[str, object] = {
        "id": "msg_fake_batch_0001",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 900,
            "output_tokens": 120,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    message.update(changes)
    return message


def _line(result: dict[str, object], custom_id: object = CUSTOM) -> str:
    return json.dumps({"custom_id": custom_id, "result": result}, ensure_ascii=False)


type Handler = Callable[[httpx2.Request], httpx2.Response]


class Router:
    """MockTransport 的處理函式：依網址回應（查批次、下載結果），記下每個請求。"""

    def __init__(
        self,
        batch: httpx2.Response | Exception,
        results: httpx2.Response | Exception | None = None,
    ) -> None:
        self.batch = batch
        self.results = results
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.results if request.url.path.endswith("/results") else self.batch
        if reply is None:
            raise AssertionError("沒有準備結果檔")
        if isinstance(reply, Exception):
            raise reply
        return httpx2.Response(reply.status_code, headers=reply.headers, content=reply.content)


def _provider(router: Router) -> AnthropicLlmProvider:
    return AnthropicLlmProvider(
        FAKE_KEY,
        timeout=30,
        attempts=3,
        retry_wait_initial=1.0,
        retry_wait_max=10.0,
        retry_wait_jitter=1.0,
        retry_after_max=60,
        transport=httpx2.MockTransport(router),
    )


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Any:
    with stamina.set_testing(True, attempts=100, cap=True):
        yield


def _json(body: object, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, json=body, headers={"request-id": "req_fake_batch"})


# --- retrieve_batch ---------------------------------------------------------------------------


def test_retrieve_batch_reads_status_and_times() -> None:
    router = Router(_json(_batch()))

    with _provider(router) as provider:
        status = provider.retrieve_batch(BATCH_ID)

    assert status.provider_batch_id == BATCH_ID
    assert status.processing_status is BatchProcessingStatus.ENDED
    assert status.created_at == datetime(2026, 11, 15, 3, 0, tzinfo=UTC)
    assert status.expires_at == datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
    assert status.results_available is True
    [request] = router.requests
    assert request.method == "GET"
    assert request.url.path == f"/v1/messages/batches/{BATCH_ID}"


@pytest.mark.parametrize("processing", ["in_progress", "canceling"])
def test_retrieve_batch_without_results_url(processing: str) -> None:
    body = _batch(processing_status=processing, ended_at=None, results_url=None)

    with _provider(Router(_json(body))) as provider:
        status = provider.retrieve_batch(BATCH_ID)

    assert status.processing_status is BatchProcessingStatus(processing)
    assert status.ended_at is None
    assert status.results_available is False


def test_retrieve_404_is_a_configuration_error_with_status() -> None:
    body = {"type": "error", "error": {"type": "not_found_error", "message": "secret detail"}}
    router = Router(_json(body, 404))

    with _provider(router) as provider, pytest.raises(LlmConfigurationError) as caught:
        provider.retrieve_batch(BATCH_ID)

    assert caught.value.status == 404
    assert len(router.requests) == 1  # 設定錯不重試
    assert "secret detail" not in str(caught.value)


def test_retrieve_5xx_is_retried_then_raised() -> None:
    router = Router(_json({"type": "error", "error": {"type": "api_error"}}, 503))

    with _provider(router) as provider, capture_logs() as logs, pytest.raises(LlmServerError):
        provider.retrieve_batch(BATCH_ID)

    assert len(router.requests) == 3
    assert {log["operation"] for log in logs if "operation" in log} == {"batches.retrieve"}


def test_retrieve_timeout_and_connection_errors() -> None:
    with _provider(Router(httpx2.ReadTimeout("slow"))) as slow, pytest.raises(LlmTimeoutError):
        slow.retrieve_batch(BATCH_ID)
    with _provider(Router(httpx2.ConnectError("down"))) as down, pytest.raises(LlmConnectionError):
        down.retrieve_batch(BATCH_ID)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps(_batch(processing_status="weird")).encode(),
        json.dumps(_batch(created_at="2026-11-15T03:00:00")).encode(),  # 沒有時區
        json.dumps(_batch(id="has space")).encode(),
    ],
)
def test_unreadable_status_body_is_malformed(body: bytes) -> None:
    with pytest.raises(LlmMalformedResponseError):
        to_batch_status(body, "req_fake")


def test_bad_batch_id_is_refused_before_sending() -> None:
    router = Router(_json(_batch()))

    with _provider(router) as provider, pytest.raises(ValueError, match="格式不對"):
        provider.retrieve_batch("../../etc")

    assert router.requests == []


# --- batch_results ----------------------------------------------------------------------------


def test_batch_results_streams_each_line() -> None:
    lines = [
        _line({"type": "succeeded", "message": _message()}),
        "",
        _line(
            {
                "type": "errored",
                "error": {"type": "error", "error": {"type": "invalid_request_error"}},
            },
            custom_id="0" * 31 + "b",
        ),
        _line({"type": "canceled"}, custom_id="0" * 31 + "c"),
        _line({"type": "expired"}, custom_id="0" * 31 + "d"),
    ]
    router = Router(_json(_batch()), httpx2.Response(200, content="\n".join(lines).encode()))

    with _provider(router) as provider:
        results = list(provider.batch_results(BATCH_ID))

    assert [result.kind for result in results] == [
        LlmBatchResultKind.SUCCEEDED,
        LlmBatchResultKind.ERRORED,
        LlmBatchResultKind.CANCELED,
        LlmBatchResultKind.EXPIRED,
    ]
    first = results[0]
    assert first.custom_id == CUSTOM
    assert first.completion is not None
    assert first.usage == first.completion.usage
    assert first.completion.usage.output_tokens == 120
    assert results[1].error_type == "invalid_request_error"
    assert [request.url.path for request in router.requests] == [
        f"/v1/messages/batches/{BATCH_ID}",
        f"/v1/messages/batches/{BATCH_ID}/results",
    ]


def test_batch_results_sends_nothing_until_iterated() -> None:
    router = Router(_json(_batch()), httpx2.Response(200, content=b""))

    with _provider(router) as provider:
        results = provider.batch_results(BATCH_ID)
        assert router.requests == []
        assert list(results) == []


def test_batch_results_without_results_url_is_an_error() -> None:
    router = Router(_json(_batch(processing_status="in_progress", results_url=None)))

    with _provider(router) as provider, pytest.raises(LlmConfigurationError):
        list(provider.batch_results(BATCH_ID))


def test_batch_results_http_errors_are_mapped() -> None:
    router = Router(_json(_batch()), _json({"type": "error", "error": {"type": "api_error"}}, 500))

    with _provider(router) as provider, pytest.raises(LlmServerError):
        list(provider.batch_results(BATCH_ID))


def test_batch_results_connection_lost_midway() -> None:
    router = Router(_json(_batch()), httpx2.RemoteProtocolError("peer closed"))

    with _provider(router) as provider, pytest.raises(LlmConnectionError):
        list(provider.batch_results(BATCH_ID))


# --- 一行結果的解析 ---------------------------------------------------------------------------


def test_succeeded_with_bad_message_is_malformed_and_keeps_usage() -> None:
    bad = _message(content="not a list")

    result = to_batch_result(_line({"type": "succeeded", "message": bad}))

    assert result.kind is LlmBatchResultKind.MALFORMED
    assert result.custom_id == CUSTOM
    assert result.completion is None
    assert result.usage is not None
    assert result.usage.input_tokens == 900


@pytest.mark.parametrize(
    ("raw", "custom_id"),
    [
        ("not json", None),
        ("[1, 2]", None),
        (_line({"type": "mystery"}), CUSTOM),
        (json.dumps({"custom_id": CUSTOM}), CUSTOM),
    ],
)
def test_unreadable_lines(raw: str, custom_id: str | None) -> None:
    result = to_batch_result(raw)

    assert result.kind is LlmBatchResultKind.UNREADABLE
    assert result.custom_id == custom_id


@pytest.mark.parametrize("custom_id", [123, "has space", "x" * 65, None])
def test_custom_id_outside_the_official_format_is_dropped(custom_id: object) -> None:
    result = to_batch_result(_line({"type": "canceled"}, custom_id=custom_id))

    assert result.kind is LlmBatchResultKind.CANCELED
    assert result.custom_id is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ({"type": "error", "error": {"type": "invalid_request_error"}}, "invalid_request_error"),
        ({"type": "api_error"}, "api_error"),
        ({"type": "error", "error": {"type": "Not Valid!"}}, None),
        ("oops", None),
    ],
)
def test_errored_error_type(error: object, expected: str | None) -> None:
    result = to_batch_result(_line({"type": "errored", "error": error}))

    assert result.kind is LlmBatchResultKind.ERRORED
    assert result.error_type == expected


def test_parsing_logs_nothing_about_the_content() -> None:
    with capture_logs() as logs:
        to_batch_result(_line({"type": "succeeded", "message": _message()}))
        to_batch_result(_line({"type": "succeeded", "message": _message(content=3)}))

    assert SECRET_OUTPUT not in repr(logs)
