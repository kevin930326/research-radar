"""LLM 與向量化的替身（`radar/adapters/fake/`）：丟和 Anthropic adapter 同一批錯誤類別、
執行期擋 `str`、`FakeEmbeddingProvider` 的固定向量。
"""

import math
from typing import Any

import pytest

from radar.adapters.fake import (
    FakeEmbeddingProvider,
    FakeLlmProvider,
    FakeRepliesExhaustedError,
    fake_completion,
)
from radar.domain.llm import (
    LlmConfigurationError,
    LlmError,
    LlmMalformedResponseError,
    LlmRateLimitedError,
    LlmRefusalError,
    LlmRejectedError,
    LlmRequest,
    LlmServerError,
    LlmTimeoutError,
    LlmTruncatedError,
    NotCloudSafeError,
)
from radar.domain.policy import CloudPolicy, CloudSafeText, ContentSource, clear_for_cloud
from radar.ports import EmbeddingProvider, LlmProvider

POLICY = CloudPolicy()


def _safe(text: str, source: ContentSource = ContentSource.TITLE) -> CloudSafeText:
    return clear_for_cloud(text, source, None, POLICY)


def _request(**changes: Any) -> LlmRequest:
    values: dict[str, Any] = {
        "model": "claude-haiku-5-5",
        "system": _safe("請整理。", ContentSource.PROMPT_TEMPLATE),
        "content": (_safe("（假）標題"),),
        "max_tokens": 1024,
    }
    values.update(changes)
    return LlmRequest(**values)


# --- FakeLlmProvider -------------------------------------------------------------------------


def test_fake_llm_is_an_llm_provider_and_records_requests() -> None:
    fake = FakeLlmProvider([fake_completion('{"a": 1}', thinking=True)])
    provider: LlmProvider = fake
    request = _request(output_schema={"type": "object"})

    result = provider.complete(request)

    assert result.parsed == {"a": 1}
    assert fake.requests == [request]
    assert fake.remaining == 0


@pytest.mark.parametrize(
    ("completion", "error_type"),
    [
        (fake_completion(None, stop_reason="refusal", refusal_category="bio"), LlmRefusalError),
        (fake_completion('{"a": ', stop_reason="max_tokens"), LlmTruncatedError),
        (fake_completion("x", stop_reason="model_context_window_exceeded"), LlmTruncatedError),
        (fake_completion("x", stop_reason="tool_use"), LlmMalformedResponseError),
        (fake_completion(None, thinking=True), LlmMalformedResponseError),
        (fake_completion("不是 JSON"), LlmMalformedResponseError),
    ],
    ids=["拒答", "截斷", "超出上下文", "tool_use", "沒有 text", "不是 JSON"],
)
def test_fake_llm_raises_the_same_rejected_errors_as_the_adapter(
    completion: Any, error_type: type[LlmRejectedError]
) -> None:
    fake = FakeLlmProvider([completion])

    with pytest.raises(error_type) as info:
        fake.complete(_request(output_schema={"type": "object"}))

    assert isinstance(info.value, LlmRejectedError)
    assert info.value.usage == completion.usage


@pytest.mark.parametrize(
    "error",
    [
        LlmTimeoutError("逾時"),
        LlmRateLimitedError("HTTP 429", status=429, retry_after=7.0),
        LlmServerError("HTTP 529", status=529),
        LlmConfigurationError("HTTP 401", status=401),
    ],
)
def test_fake_llm_raises_prepared_errors_as_is(error: LlmError) -> None:
    fake = FakeLlmProvider([error])

    with pytest.raises(type(error)) as info:
        fake.complete(_request())

    assert info.value is error


def test_fake_llm_rejects_str_before_recording_or_consuming() -> None:
    fake = FakeLlmProvider([fake_completion("x")])
    wrong: Any = "直接傳字串"

    with pytest.raises(NotCloudSafeError):
        fake.complete(wrong)

    assert fake.requests == []
    assert fake.remaining == 1


def test_fake_llm_without_replies_raises_a_clear_error() -> None:
    fake = FakeLlmProvider()

    with pytest.raises(FakeRepliesExhaustedError, match="沒有準備好的回覆"):
        fake.complete(_request())


def test_fake_llm_replies_are_used_in_order() -> None:
    fake = FakeLlmProvider([fake_completion("一")])
    fake.add(fake_completion("二"))

    assert [fake.complete(_request()).text for _ in range(2)] == ["一", "二"]


# --- FakeEmbeddingProvider -------------------------------------------------------------------


def test_fake_embedding_is_an_embedding_provider() -> None:
    provider: EmbeddingProvider = FakeEmbeddingProvider(dimensions=4, model_id="m")

    assert provider.model_id == "m"
    assert len(provider.embed_query(_safe("查詢"))) == 4


def test_fake_embedding_vectors_are_fixed_and_unit_length() -> None:
    first = FakeEmbeddingProvider(dimensions=12)
    second = FakeEmbeddingProvider(dimensions=12)
    texts = [_safe("（假）標題\n\n（假）摘要"), _safe("另一篇")]

    vectors = first.embed_documents(texts)

    assert vectors == second.embed_documents(texts)
    assert len(vectors) == 2
    assert vectors[0] != vectors[1]
    assert all(len(vector) == 12 for vector in vectors)
    assert all(math.isclose(math.hypot(*vector), 1.0) for vector in vectors)
    assert first.document_calls == [tuple(texts)]


def test_fake_embedding_query_and_document_vectors_differ() -> None:
    fake = FakeEmbeddingProvider()
    text = _safe("同一段文字")

    assert fake.embed_query(text) != fake.embed_documents([text])[0]
    assert fake.query_calls == [text]


@pytest.mark.parametrize(
    "texts",
    ["直接傳字串", ["直接傳字串"], [_safe("一"), "二"], None],
    ids=["str 當序列", "list 裡是 str", "混在一起", "None"],
)
def test_fake_embedding_rejects_non_cloud_safe_documents(texts: Any) -> None:
    fake = FakeEmbeddingProvider()

    with pytest.raises(NotCloudSafeError):
        fake.embed_documents(texts)

    assert fake.document_calls == []


def test_fake_embedding_rejects_str_query() -> None:
    fake = FakeEmbeddingProvider()
    wrong: Any = "直接傳字串"

    with pytest.raises(NotCloudSafeError):
        fake.embed_query(wrong)

    assert fake.query_calls == []
