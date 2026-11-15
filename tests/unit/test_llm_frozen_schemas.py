"""凍結的 schema 登記表（Opus 第四次確認審查）：`radar/domain/llm.py` import 時就把
`llm_schemas.py` 每個模型的 JSON schema 產生好、凍結起來，送出只用凍結的字串。

- 巢狀列舉與巢狀模型的 `title`、`description`（docstring）不送。
- import 之後才改 `model_config["json_schema_extra"]`、欄位預設值（加 `model_rebuild`）、
  `__doc__`，送出的 schema 都不變（先證明同樣的改法會讓 pydantic 重新產生的 schema 帶上文字）。
- import 之後才 `setattr` 進模組的類別不在登記表裡，拒收。
- 模組裡有 `TypedDict`、沒建好的模型、名字對不上的類別，建登記表時就報錯。

文字全是自己編的假內容。
"""

import json
from collections.abc import Iterator, Mapping
from enum import StrEnum
from types import ModuleType
from typing import Any, TypedDict

import httpx2
import pytest
from pydantic import BaseModel

from radar.adapters.anthropic import AnthropicLlmProvider
from radar.domain import llm, llm_schemas
from radar.domain.llm import LlmRequest
from radar.domain.llm_schemas import (
    FakeAnyObject,
    FakeItem,
    FakeKind,
    FakeNested,
    FakeResearchQuestion,
)
from radar.domain.policy import CloudPolicy, CloudSafeText, ContentSource, clear_for_cloud
from tests.anthropic_cassette import cassette_response
from tests.prompt_support import template_text

POLICY = CloudPolicy()
PAYWALLED = "（假）資料庫挑出的標題 PAYWALLED-MARKER-8K"
MARKER = "PAYWALLED-MARKER-8K"
NESTED_SCHEMA = {
    "$defs": {
        "FakeItem": {
            "additionalProperties": False,
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "type": "object",
        },
        "FakeKind": {"enum": ["theory", "method"], "type": "string"},
    },
    "additionalProperties": False,
    "properties": {
        "item": {"$ref": "#/$defs/FakeItem"},
        "kind": {"$ref": "#/$defs/FakeKind"},
        "tags": {"default": [], "items": {"type": "string"}, "type": "array"},
    },
    "required": ["kind", "item"],
    "type": "object",
}


def _safe(text: str, source: ContentSource = ContentSource.TITLE) -> CloudSafeText:
    return clear_for_cloud(text, source, None, POLICY)


def _request(output_schema: object = FakeNested) -> LlmRequest:
    return LlmRequest(
        model="claude-haiku-5-5",
        system=template_text("請整理。"),
        content=(_safe("（假）標題"),),
        max_tokens=1024,
        output_schema=output_schema,  # type: ignore[arg-type]
    )


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return cassette_response("ok_json")


def _sent_schema(request: LlmRequest) -> str:
    """經 Anthropic adapter（假 transport）送出，回請求本文裡的 schema（JSON 字串）。"""
    recorder = Recorder()
    provider = AnthropicLlmProvider(
        "sk-ant-FAKE-KEY-for-tests-only",
        timeout=30,
        attempts=1,
        retry_wait_initial=1.0,
        retry_wait_max=10.0,
        retry_wait_jitter=1.0,
        retry_after_max=60,
        transport=httpx2.MockTransport(recorder),
    )
    provider.complete(request)
    assert len(recorder.requests) == 1
    body = json.loads(recorder.requests[0].content)
    return json.dumps(body["output_config"]["format"]["schema"], ensure_ascii=False)


# --- 登記表的內容 ------------------------------------------------------------------------------


def test_registry_holds_every_model_in_the_schema_module() -> None:
    frozen: Mapping[int, tuple[type[BaseModel], str]] = llm._FROZEN_SCHEMAS
    classes = {entry[0] for entry in frozen.values()}

    assert classes == {FakeAnyObject, FakeResearchQuestion, FakeItem, FakeNested}
    assert all(cls.__pydantic_complete__ for cls in classes)
    # 列舉只能當欄位型別，不能直接當 output_schema。
    with pytest.raises(TypeError, match=r"llm_schemas\.py 最上層"):
        _request(FakeKind)


def test_nested_enum_and_model_docstrings_are_not_sent() -> None:
    assert json.loads(_sent_schema(_request())) == NESTED_SCHEMA


# --- import 之後才改類別 ----------------------------------------------------------------------


@pytest.fixture
def restore_classes() -> Iterator[None]:
    docs = {cls: cls.__doc__ for cls in (FakeNested, FakeItem, FakeKind)}
    config = FakeNested.model_config.copy()
    default = FakeNested.model_fields["tags"].default
    yield
    for cls, doc in docs.items():
        cls.__doc__ = doc
    FakeNested.model_config = config
    FakeNested.model_fields["tags"].default = default
    FakeNested.model_rebuild(force=True)


@pytest.mark.usefixtures("restore_classes")
def test_model_config_changed_after_import_is_not_sent() -> None:
    FakeNested.model_config["json_schema_extra"] = {"examples": [PAYWALLED]}
    FakeNested.model_rebuild(force=True)
    # 前提：pydantic 重新產生的 schema 真的帶上了那段文字。
    assert MARKER in json.dumps(FakeNested.model_json_schema(), ensure_ascii=False)

    sent = _sent_schema(_request())

    assert MARKER not in sent
    assert json.loads(sent) == NESTED_SCHEMA


@pytest.mark.usefixtures("restore_classes")
def test_default_changed_after_import_is_not_sent() -> None:
    FakeNested.model_fields["tags"].default = (PAYWALLED,)
    FakeNested.model_rebuild(force=True)
    assert MARKER in json.dumps(FakeNested.model_json_schema(), ensure_ascii=False)

    sent = _sent_schema(_request())

    assert MARKER not in sent
    assert json.loads(sent) == NESTED_SCHEMA


@pytest.mark.usefixtures("restore_classes")
def test_doc_changed_after_import_is_not_sent() -> None:
    for cls in (FakeNested, FakeItem, FakeKind):
        cls.__doc__ = PAYWALLED
    FakeNested.model_rebuild(force=True)

    sent = _sent_schema(_request())

    assert MARKER not in sent
    assert json.loads(sent) == NESTED_SCHEMA


def test_class_added_to_the_module_after_import_is_rejected() -> None:
    class Late(BaseModel):
        concept: str = PAYWALLED

    Late.__module__ = llm_schemas.__name__
    Late.__qualname__ = "Late"
    llm_schemas.Late = Late  # type: ignore[attr-defined]
    try:
        with pytest.raises(TypeError, match=r"llm_schemas\.py 最上層") as info:
            _request(Late)
    finally:
        delattr(llm_schemas, "Late")

    assert MARKER not in str(info.value)


# --- 建登記表時就報錯的情況 --------------------------------------------------------------------


def _module(**classes: Any) -> ModuleType:
    module = ModuleType("tests_fake_llm_schemas")
    for name, cls in classes.items():
        cls.__module__ = module.__name__
        cls.__qualname__ = name
        setattr(module, name, cls)
    return module


def test_typed_dict_in_the_schema_module_is_an_error() -> None:
    class Shape(TypedDict):
        concept: str

    with pytest.raises(RuntimeError, match="BaseModel 或 Enum"):
        llm._freeze_schemas(_module(Shape=Shape))


def test_incomplete_model_is_an_error() -> None:
    # 型別標註引用了不存在的型別，pydantic 建不好這個模型。
    early: Any = type("Early", (BaseModel,), {"__annotations__": {"later": "Later"}})

    with pytest.raises(RuntimeError, match="沒有建好"):
        llm._freeze_schemas(_module(Early=early))


def test_name_and_qualname_mismatch_is_an_error() -> None:
    class Shown(BaseModel):
        concept: str

    module = _module(Shown=Shown)
    vars(module)["Alias"] = Shown

    with pytest.raises(RuntimeError, match="對不上"):
        llm._freeze_schemas(module)


def test_enums_and_imported_names_are_skipped() -> None:
    class Kind(StrEnum):
        A = "a"

    class Shape(BaseModel):
        kind: Kind

    module = _module(Kind=Kind, Shape=Shape)
    vars(module)["BaseModel"] = BaseModel

    frozen = llm._freeze_schemas(module)

    assert [entry[0] for entry in frozen.values()] == [Shape]
