"""`LlmRequest` 裡不是 `CloudSafeText` 的欄位也送不出沒放行的文字（Opus 確認審查 77caba7）：

- `output_schema` 只收 Pydantic 模型類別（不收 dict／Mapping），建立時產生 JSON schema 並凍結，
  建好之後改不到；送出的 schema 每次由類別重新產生。
- `model` 只收白名單裡的 ID。
- `check_llm_request` 看真實型別（不用 isinstance），`LlmRequest` 不能繼承；adapter 用
  `snapshot_llm_request` 重建的副本，之後只讀副本。

付費文字放進 schema 或 model（建立時或建立後硬改），Anthropic adapter 的假 transport 收到 0 個
請求或收到的請求裡沒有那段文字；錯誤訊息不帶文字。文字全是自己編的假內容。
"""

import json
from types import MappingProxyType
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

from radar.adapters.anthropic import AnthropicLlmProvider
from radar.adapters.fake import FakeLlmProvider, fake_completion
from radar.domain.llm import (
    ALLOWED_MODELS,
    LlmRequest,
    NotCloudSafeError,
    check_llm_request,
    snapshot_llm_request,
)
from radar.domain.llm_schemas import FakeResearchQuestion as Answer
from radar.domain.policy import CloudPolicy, CloudSafeText, ContentSource, clear_for_cloud
from tests.anthropic_cassette import cassette_response

POLICY = CloudPolicy()
PAYWALLED = "（假）付費全文不該送出 PAYWALLED-MARKER-5T"
MARKER = "PAYWALLED-MARKER-5T"


def _safe(text: str, source: ContentSource = ContentSource.TITLE) -> CloudSafeText:
    return clear_for_cloud(text, source, None, POLICY)


def _request(**changes: Any) -> LlmRequest:
    values: dict[str, Any] = {
        "model": "claude-haiku-5-5",
        "system": _safe("請整理。", ContentSource.PROMPT_TEMPLATE),
        "content": (_safe("（假）標題"),),
        "max_tokens": 1024,
        "output_schema": Answer,
    }
    values.update(changes)
    return LlmRequest(**values)


def _assert_clean(error: BaseException) -> None:
    assert MARKER not in f"{error}{error!r}{error.args!r}"


class Recorder:
    """MockTransport 的處理函式：記下請求，回一個合法的 JSON 回應。"""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return cassette_response("ok_json")


def _anthropic(recorder: Recorder) -> AnthropicLlmProvider:
    return AnthropicLlmProvider(
        "sk-ant-FAKE-KEY-for-tests-only",
        timeout=30,
        attempts=1,
        retry_wait_initial=1.0,
        retry_wait_max=10.0,
        retry_wait_jitter=1.0,
        retry_after_max=60,
        transport=httpx2.MockTransport(recorder),
    )


def _sends_nothing(request: LlmRequest, error: type[Exception]) -> None:
    """Anthropic adapter 與替身都擋下這個請求：0 個請求、替身不記錄也不消耗回覆。"""
    recorder = Recorder()
    with pytest.raises(error) as info:
        _anthropic(recorder).complete(request)
    assert recorder.requests == []
    _assert_clean(info.value)

    fake = FakeLlmProvider([fake_completion('{"research_question": "x"}')])
    with pytest.raises(error):
        fake.complete(request)
    assert fake.requests == []
    assert fake.remaining == 1


# --- output_schema ---------------------------------------------------------------------------


class Disguised:
    """讓 `__class__` 回 `type`，假裝自己是類別。"""

    @property  # type: ignore[misc]
    def __class__(self) -> type:
        return type

    @staticmethod
    def model_json_schema(**_: object) -> dict[str, object]:
        return {"description": PAYWALLED}


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "description": PAYWALLED},
        MappingProxyType({"type": "object", "enum": [PAYWALLED]}),
        json.dumps({"description": PAYWALLED}),
        BaseModel,
        Answer(research_question=PAYWALLED),
        Disguised(),
        dict,
    ],
    ids=[
        "dict",
        "MappingProxyType",
        "JSON 字串",
        "BaseModel 本身",
        "模型實例",
        "偽裝類別",
        "非模型類別",
    ],
)
def test_output_schema_only_accepts_model_classes(schema: object) -> None:
    with pytest.raises(TypeError, match="Pydantic 模型類別") as info:
        _request(output_schema=schema)

    _assert_clean(info.value)


def test_schema_swapped_after_creation_sends_nothing() -> None:
    request = _request()
    object.__setattr__(request, "output_schema", {"description": PAYWALLED})

    _sends_nothing(request, TypeError)


def test_frozen_schema_text_tampered_after_creation_is_not_sent() -> None:
    request = _request()
    object.__setattr__(request, "_output_json", json.dumps({"description": PAYWALLED}))
    recorder = Recorder()

    _anthropic(recorder).complete(request)

    # adapter 用重建的副本，schema 由類別重新產生，硬改的字串不會送出。
    assert len(recorder.requests) == 1
    body = recorder.requests[0].content.decode()
    assert MARKER not in body
    assert json.loads(body)["output_config"]["format"]["schema"] == {
        "additionalProperties": False,
        "properties": {"research_question": {"type": "string"}},
        "required": ["research_question"],
        "type": "object",
    }


def test_output_json_schema_returns_a_fresh_copy() -> None:
    request = _request()
    first = request.output_json_schema()
    assert first is not None
    first["description"] = PAYWALLED

    assert request.output_json_schema() == {
        "additionalProperties": False,
        "properties": {"research_question": {"type": "string"}},
        "required": ["research_question"],
        "type": "object",
    }


def test_without_schema_nothing_is_frozen() -> None:
    assert _request(output_schema=None).output_json_schema() is None


# --- model -----------------------------------------------------------------------------------


class LookalikeStr(str):
    """值和白名單一樣，但不是 `str` 本身（可以改寫比較與雜湊）。"""


@pytest.mark.parametrize(
    "model",
    [PAYWALLED, "claude-opus-5-5", " claude-haiku-5-5", LookalikeStr("claude-haiku-5-5"), None, 1],
    ids=["付費文字", "沒登記的模型", "前面有空白", "str 子類別", "None", "整數"],
)
def test_model_must_be_an_allowed_id(model: object) -> None:
    with pytest.raises(ValueError, match="登記過的模型") as info:
        _request(model=model)

    _assert_clean(info.value)


def test_allowed_models_are_only_haiku_for_now() -> None:
    assert frozenset({"claude-haiku-5-5"}) == ALLOWED_MODELS


def test_model_swapped_after_creation_sends_nothing() -> None:
    request = _request()
    object.__setattr__(request, "model", PAYWALLED)

    _sends_nothing(request, ValueError)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("effort", LookalikeStr("low"), ValueError),
        ("effort", PAYWALLED, ValueError),
        ("disable_thinking", 1, TypeError),
        ("max_tokens", 1024.0, TypeError),
    ],
    ids=[
        "effort 是 str 子類別",
        "effort 是付費文字",
        "disable_thinking 是整數",
        "max_tokens 是浮點數",
    ],
)
def test_other_fields_swapped_after_creation_send_nothing(
    field: str, value: object, error: type[Exception]
) -> None:
    request = _request()
    object.__setattr__(request, field, value)

    _sends_nothing(request, error)


def test_content_swapped_to_a_tuple_subclass_sends_nothing() -> None:
    class ShiftyTuple(tuple[object, ...]):
        pass

    request = _request()
    object.__setattr__(request, "content", ShiftyTuple((_safe("（假）標題"),)))

    _sends_nothing(request, NotCloudSafeError)


# --- LlmRequest 的真實型別與副本 ------------------------------------------------------------


class DisguisedRequest:
    """讓 `__class__` 回 `LlmRequest`，欄位是付費文字。"""

    model = PAYWALLED
    system = PAYWALLED
    content = (PAYWALLED,)
    max_tokens = 1024
    effort = None
    disable_thinking = False
    output_schema = None

    @property  # type: ignore[misc]
    def __class__(self) -> type:
        return LlmRequest


def test_disguised_request_fools_isinstance_but_not_the_check() -> None:
    disguised: Any = DisguisedRequest()

    assert isinstance(disguised, LlmRequest)
    with pytest.raises(NotCloudSafeError) as info:
        check_llm_request(disguised)
    _assert_clean(info.value)
    _sends_nothing(disguised, NotCloudSafeError)


def test_request_cannot_be_subclassed() -> None:
    base: Any = LlmRequest

    with pytest.raises(TypeError, match="繼承"):
        type("Sub", (base,), {})


def test_snapshot_is_a_rebuilt_equal_copy() -> None:
    request = _request(effort="low", disable_thinking=True)

    snapshot = snapshot_llm_request(request)

    assert snapshot is not request
    assert type(snapshot) is LlmRequest
    assert snapshot == request
    assert snapshot.output_json_schema() == request.output_json_schema()


def test_fake_records_the_snapshot() -> None:
    fake = FakeLlmProvider([fake_completion('{"research_question": "x"}')])
    request = _request()

    fake.complete(request)

    assert fake.requests == [request]
    assert fake.requests[0] is not request
