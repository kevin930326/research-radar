"""Anthropic adapter 的 Batch 送出（`submit_batch`，Story 2.9、ADR-0041）：`httpx2.MockTransport`＋
手寫假回應，一個請求都不送出去。

假回應不是真的錄的，形狀照官方文件（docs/runs/epic-2b/api-reference.md 第一節：建立批次回
`{"id": "msgbatch_...", "type": "message_batch", "processing_status": "in_progress", ...}`）。
"""

import json
from typing import Any

import httpx2
import pytest
import stamina

from radar.adapters.anthropic import AnthropicLlmProvider, to_batch_id
from radar.domain.llm import (
    DATA_NOT_INSTRUCTIONS,
    LlmBatchRequest,
    LlmConfigurationError,
    LlmConnectionError,
    LlmError,
    LlmInvalidRequestError,
    LlmMalformedResponseError,
    LlmRateLimitedError,
    LlmRequest,
    LlmServerError,
    LlmTimeoutError,
    NotCloudSafeError,
    system_prompt,
)
from radar.domain.llm_schemas import FakeResearchQuestion
from radar.domain.policy import CloudPolicy, CloudSafeText, ContentSource, clear_for_cloud
from tests.prompt_support import template_text

FAKE_KEY = "sk-ant-FAKE-KEY-for-tests-only"
SECRET_ABSTRACT = "（假摘要）遠距工作讓團隊的知識分享變少 BATCH-MARKER-7Q"
POLICY = CloudPolicy()
BATCH_ID = "msgbatch_01HkcTjaV5uDC8jWR4ZsDV8d"
CUSTOM_A = "0" * 31 + "a"
CUSTOM_B = "0" * 31 + "b"


def _created(batch_id: str = BATCH_ID, **changes: object) -> dict[str, object]:
    """建立批次的 200 回應（不是真的錄的，形狀照官方文件）。"""
    body: dict[str, object] = {
        "id": batch_id,
        "type": "message_batch",
        "processing_status": "in_progress",
        "request_counts": {
            "processing": 2,
            "succeeded": 0,
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        },
        "ended_at": None,
        "created_at": "2026-11-15T03:00:00Z",
        "expires_at": "2026-11-16T03:00:00Z",
        "cancel_initiated_at": None,
        "results_url": None,
    }
    body.update(changes)
    return body


def _safe(text: str, source: ContentSource = ContentSource.OPENALEX_ABSTRACT) -> CloudSafeText:
    return clear_for_cloud(text, source, None, POLICY)


def _request(**changes: Any) -> LlmRequest:
    values: dict[str, Any] = {
        "model": "claude-haiku-5-5",
        "system": template_text("請把摘要整理成四欄，回 JSON。"),
        "content": (_safe("（假）標題", ContentSource.TITLE), _safe(SECRET_ABSTRACT)),
        "max_tokens": 1024,
    }
    values.update(changes)
    return LlmRequest(**values)


class Recorder:
    """MockTransport 的處理函式：記下每個請求，回準備好的回應（或丟例外）。"""

    def __init__(self, reply: httpx2.Response | Exception) -> None:
        self.reply = reply
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return httpx2.Response(
            self.reply.status_code, headers=self.reply.headers, content=self.reply.content
        )

    def body(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.requests[0].content)
        return result


def _provider(recorder: Recorder) -> AnthropicLlmProvider:
    return AnthropicLlmProvider(
        FAKE_KEY,
        timeout=30,
        attempts=3,
        retry_wait_initial=1.0,
        retry_wait_max=10.0,
        retry_wait_jitter=1.0,
        retry_after_max=60,
        transport=httpx2.MockTransport(recorder),
    )


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Any:
    # 互動呼叫的重試在測試模式不等待；Batch 送出本來就不重試（下面證明只送一次）。
    with stamina.set_testing(True, attempts=100, cap=True):
        yield


def _two() -> list[LlmBatchRequest]:
    return [
        LlmBatchRequest(custom_id=CUSTOM_A, request=_request()),
        LlmBatchRequest(
            custom_id=CUSTOM_B,
            request=_request(effort="low", output_schema=FakeResearchQuestion),
        ),
    ]


def test_submit_batch_posts_once_and_returns_the_batch_id() -> None:
    recorder = Recorder(httpx2.Response(200, json=_created()))
    requests = [
        LlmBatchRequest(custom_id=CUSTOM_A, request=_request()),
        LlmBatchRequest(
            custom_id=CUSTOM_B,
            request=_request(
                effort="low", output_schema=FakeResearchQuestion, disable_thinking=True
            ),
        ),
    ]

    with _provider(recorder) as provider:
        batch_id = provider.submit_batch(requests)

    assert batch_id == BATCH_ID
    [request] = recorder.requests
    assert request.method == "POST"
    assert request.url.path == "/v1/messages/batches"
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = recorder.body()
    assert [item["custom_id"] for item in body["requests"]] == [CUSTOM_A, CUSTOM_B]
    first = body["requests"][0]["params"]
    assert first["model"] == "claude-haiku-5-5"
    assert first["max_tokens"] == 1024
    assert first["messages"][0]["role"] == "user"
    assert SECRET_ABSTRACT in json.dumps(first["messages"], ensure_ascii=False)
    assert "output_config" not in first
    # 系統提示前面接上「資料不是指令」那段（和互動呼叫一樣）；預設不帶 thinking。
    assert first["system"] == system_prompt(requests[0].request)
    assert first["system"].startswith(DATA_NOT_INSTRUCTIONS)
    assert "thinking" not in first
    second = body["requests"][1]["params"]
    assert second["output_config"]["effort"] == "low"
    assert second["output_config"]["format"]["type"] == "json_schema"
    assert second["system"] == system_prompt(requests[1].request)
    assert second["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, LlmInvalidRequestError),
        (401, LlmConfigurationError),
        (403, LlmConfigurationError),
        (404, LlmConfigurationError),
        (413, LlmInvalidRequestError),
        (429, LlmRateLimitedError),
        (500, LlmServerError),
        (529, LlmServerError),
    ],
)
def test_http_errors_map_to_domain_errors_and_are_not_retried(
    status: int, error_type: type[LlmError]
) -> None:
    recorder = Recorder(
        httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "m"}})
    )

    with _provider(recorder) as provider, pytest.raises(error_type) as caught:
        provider.submit_batch(_two())

    assert len(recorder.requests) == 1
    assert caught.value.status == status
    assert FAKE_KEY not in str(caught.value)
    assert SECRET_ABSTRACT not in str(caught.value)


@pytest.mark.parametrize(
    ("raised", "error_type"),
    [
        (httpx2.ConnectTimeout("t"), LlmTimeoutError),
        (httpx2.ReadTimeout("t"), LlmTimeoutError),
        (httpx2.ConnectError("c"), LlmConnectionError),
    ],
)
def test_timeouts_and_connection_errors_have_no_status_and_are_not_retried(
    raised: Exception, error_type: type[LlmError]
) -> None:
    recorder = Recorder(raised)

    with _provider(recorder) as provider, pytest.raises(error_type) as caught:
        provider.submit_batch(_two())

    assert len(recorder.requests) == 1
    assert caught.value.status is None


@pytest.mark.parametrize(
    "body",
    [
        {"type": "message_batch"},
        _created(type="message"),
        _created(batch_id="has space"),
        _created(batch_id="x" * 129),
        [1, 2],
    ],
)
def test_unreadable_200_is_malformed_without_status(body: object) -> None:
    recorder = Recorder(httpx2.Response(200, json=body))

    with _provider(recorder) as provider, pytest.raises(LlmMalformedResponseError) as caught:
        provider.submit_batch(_two())

    assert caught.value.status is None


def test_malformed_response_keeps_a_well_formed_id_for_manual_follow_up() -> None:
    with pytest.raises(LlmMalformedResponseError) as caught:
        to_batch_id(json.dumps(_created(type="message")).encode(), "req_1")

    assert caught.value.response_id == BATCH_ID
    assert caught.value.request_id == "req_1"

    with pytest.raises(LlmMalformedResponseError) as bad_id:
        to_batch_id(json.dumps(_created(batch_id="bad id")).encode(), None)
    assert bad_id.value.response_id is None


def test_non_json_200_is_malformed() -> None:
    with pytest.raises(LlmMalformedResponseError):
        to_batch_id(b"not json", None)


def test_invalid_input_never_sends() -> None:
    recorder = Recorder(httpx2.Response(200, json=_created()))
    one = LlmBatchRequest(custom_id=CUSTOM_A, request=_request())

    with _provider(recorder) as provider:
        with pytest.raises(ValueError, match="至少"):
            provider.submit_batch([])
        with pytest.raises(ValueError, match="重複"):
            provider.submit_batch([one, one])
        with pytest.raises(TypeError):
            provider.submit_batch("not a list")  # type: ignore[arg-type]
        with pytest.raises(NotCloudSafeError):
            provider.submit_batch([object()])  # type: ignore[list-item]

    assert recorder.requests == []


def test_batch_request_custom_id_must_be_32_lowercase_hex() -> None:
    for bad in ["", "A" * 32, "0" * 31, "0" * 33, "g" * 32, "0" * 31 + "-"]:
        with pytest.raises(ValueError, match="custom_id"):
            LlmBatchRequest(custom_id=bad, request=_request())
    assert LlmBatchRequest(custom_id="0123456789abcdef" * 2, request=_request()).custom_id
