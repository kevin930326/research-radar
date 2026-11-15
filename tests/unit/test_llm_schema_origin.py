"""`output_schema` 的類別只能來自 `radar/domain/llm_schemas.py` 的最上層（Opus 第三次確認審查）。

函式裡用沒放行的文字臨時組的類別（`Literal[tuple(names)]`、`Field(description=變數)`、變數預設值、
`Enum("X", {...})`）、別的模組的類別、`__module__`／`__qualname__` 亂改的類別，建立請求時拒收；
請求建好後才換成這種類別，Anthropic adapter 的假 transport 收到 0 個請求。類別建好後才改
`__doc__`：docstring 一律不送，請求裡沒有那段文字。文字全是自己編的假內容。
"""

import json
from enum import Enum
from typing import Any, Literal

import httpx2
import pytest
from pydantic import BaseModel, ConfigDict, Field

from radar.adapters.anthropic import AnthropicLlmProvider
from radar.adapters.fake import FakeLlmProvider, fake_completion
from radar.domain import llm_schemas
from radar.domain.llm import LlmRequest
from radar.domain.llm_schemas import FakeAnyObject, FakeResearchQuestion
from radar.domain.policy import CloudPolicy, CloudSafeText, ContentSource, clear_for_cloud
from tests.anthropic_cassette import cassette_response

POLICY = CloudPolicy()
PAYWALLED = "（假）上傳 PDF 抽出的構面 PAYWALLED-MARKER-3R"
MARKER = "PAYWALLED-MARKER-3R"
REJECTED = r"llm_schemas\.py 最上層"


def _safe(text: str, source: ContentSource = ContentSource.TITLE) -> CloudSafeText:
    return clear_for_cloud(text, source, None, POLICY)


def _request(**changes: Any) -> LlmRequest:
    values: dict[str, Any] = {
        "model": "claude-haiku-5-5",
        "system": _safe("請整理。", ContentSource.PROMPT_TEMPLATE),
        "content": (_safe("（假）標題"),),
        "max_tokens": 1024,
        "output_schema": FakeResearchQuestion,
    }
    values.update(changes)
    return LlmRequest(**values)


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


# --- 執行期用文字組出來的類別 ------------------------------------------------------------------


def _literal_from_names(names: tuple[str, ...]) -> type[BaseModel]:
    class Dynamic(BaseModel):
        category: Literal[names]  # type: ignore[valid-type]

    return Dynamic


def _description_from_variable(text: str) -> type[BaseModel]:
    class Dynamic(BaseModel):
        concept: str = Field(description=text)

    return Dynamic


def _default_from_variable(text: str) -> type[BaseModel]:
    class Dynamic(BaseModel):
        concept: str = text

    return Dynamic


def _enum_function(text: str) -> type[BaseModel]:
    # 模擬 Enum 函式寫法：成員的值是執行期的文字。
    factory: Any = Enum
    category: Any = factory("Category", {"A": text})

    class Dynamic(BaseModel):
        value: category

    return Dynamic


DYNAMIC_FACTORIES = [
    _literal_from_names,
    _description_from_variable,
    _default_from_variable,
    _enum_function,
]
DYNAMIC_IDS = ["Literal[tuple(names)]", "Field(description=變數)", "變數預設值", "Enum 函式寫法"]


def _build(factory: Any) -> type[BaseModel]:
    built: type[BaseModel] = (
        factory((PAYWALLED,)) if factory is _literal_from_names else factory(PAYWALLED)
    )
    return built


@pytest.mark.parametrize("factory", DYNAMIC_FACTORIES, ids=DYNAMIC_IDS)
def test_class_built_at_runtime_is_rejected(factory: Any) -> None:
    dynamic = _build(factory)
    # 前提：這些類別真的會把文字放進 schema。
    assert MARKER in json.dumps(dynamic.model_json_schema(), ensure_ascii=False)

    with pytest.raises(TypeError, match=REJECTED) as info:
        _request(output_schema=dynamic)

    assert MARKER not in f"{info.value}{info.value!r}"


@pytest.mark.parametrize("factory", DYNAMIC_FACTORIES, ids=DYNAMIC_IDS)
def test_class_swapped_in_after_creation_sends_nothing(factory: Any) -> None:
    request = _request()
    object.__setattr__(request, "output_schema", _build(factory))
    recorder = Recorder()

    with pytest.raises(TypeError):
        _anthropic(recorder).complete(request)
    assert recorder.requests == []

    fake = FakeLlmProvider([fake_completion('{"research_question": "x"}')])
    with pytest.raises(TypeError):
        fake.complete(request)
    assert fake.requests == []
    assert fake.remaining == 1


class OutsideSchemaModule(BaseModel):
    """測試模組最上層的類別：寫死的，但不在指定模組，照樣拒收。"""

    model_config = ConfigDict(extra="forbid")

    research_question: str


def test_class_outside_the_schema_module_is_rejected() -> None:
    with pytest.raises(TypeError, match=REJECTED):
        _request(output_schema=OutsideSchemaModule)


def test_forged_module_and_qualname_are_rejected() -> None:
    dynamic = _description_from_variable(PAYWALLED)
    dynamic.__module__ = "radar.domain.llm_schemas"
    dynamic.__qualname__ = "FakeResearchQuestion"

    # 名字對得上模組，但模組裡那個名字指向的不是它。
    with pytest.raises(TypeError, match=REJECTED):
        _request(output_schema=dynamic)


def test_nested_class_in_the_schema_module_is_rejected() -> None:
    nested = _default_from_variable(PAYWALLED)
    nested.__module__ = "radar.domain.llm_schemas"
    nested.__qualname__ = "FakeAnyObject.Nested"

    with pytest.raises(TypeError, match=REJECTED):
        _request(output_schema=nested)


def test_classes_in_the_schema_module_are_accepted() -> None:
    for cls in (FakeAnyObject, FakeResearchQuestion):
        assert _request(output_schema=cls).output_schema is cls
    assert vars(llm_schemas)["FakeResearchQuestion"] is FakeResearchQuestion


# --- 類別建好後改 __doc__ ----------------------------------------------------------------------


def test_doc_changed_after_definition_is_never_sent() -> None:
    original = FakeResearchQuestion.__doc__
    FakeResearchQuestion.__doc__ = PAYWALLED
    try:
        request = _request()
        recorder = Recorder()
        _anthropic(recorder).complete(request)
    finally:
        FakeResearchQuestion.__doc__ = original

    assert len(recorder.requests) == 1
    body = recorder.requests[0].content.decode()
    assert MARKER not in body
    schema = json.loads(body)["output_config"]["format"]["schema"]
    assert "description" not in schema
    assert "title" not in schema


def test_docstrings_are_not_part_of_the_schema() -> None:
    schema = _request().output_json_schema()

    assert schema == {
        "additionalProperties": False,
        "properties": {"research_question": {"type": "string"}},
        "required": ["research_question"],
        "type": "object",
    }
