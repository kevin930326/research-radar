"""結構化輸出裡的數字一定有限（Codex 審查 PR #54 第 2 點）：`json.loads` 會把 `1e400` 解成
`inf`，`interpret_completion` 要當成格式不對，不能放進 `LlmResult.parsed`。模型輸出是自己編的。
"""

import math
from typing import Any

import pytest

from radar.adapters.fake import FakeLlmProvider, fake_completion
from radar.domain.errors import ErrorCode
from radar.domain.llm import LlmMalformedResponseError, LlmRequest, interpret_completion
from radar.domain.llm_schemas import FakeAnyObject as AnyObject
from radar.domain.policy import CloudPolicy, CloudSafeText, ContentSource, clear_for_cloud

POLICY = CloudPolicy()


def _safe(text: str, source: ContentSource = ContentSource.TITLE) -> CloudSafeText:
    return clear_for_cloud(text, source, None, POLICY)


JSON_REQUEST = LlmRequest(
    model="claude-haiku-5-5",
    system=_safe("請回 JSON。", ContentSource.PROMPT_TEMPLATE),
    content=(_safe("（假）標題"),),
    max_tokens=1024,
    output_schema=AnyObject,
)


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1e400}',
        '{"a": -1e400}',
        '{"a": 1E+999}',
        '{"a": {"b": [1, 2, {"c": 1e400}]}}',
        '{"a": [[[-1e400]]]}',
        '{"a": "1e400", "b": [0.5, -2e308]}',
    ],
    ids=["正溢位", "負溢位", "大寫指數", "巢狀物件裡", "巢狀陣列裡", "字串旁的負溢位"],
)
def test_overflowing_numbers_are_malformed(text: str) -> None:
    with pytest.raises(LlmMalformedResponseError, match="非有限") as info:
        interpret_completion(JSON_REQUEST, fake_completion(text))

    assert info.value.code is ErrorCode.LLM_MALFORMED_RESPONSE
    assert info.value.response_id == "msg_fake_0001"


def test_fake_llm_raises_the_same_error_for_overflow() -> None:
    fake = FakeLlmProvider([fake_completion('{"score": 1e400}')])

    with pytest.raises(LlmMalformedResponseError):
        fake.complete(JSON_REQUEST)


def test_large_but_finite_numbers_and_strings_are_kept() -> None:
    text = (
        '{"max": 1.7976931348623157e308, "tiny": 1e-400, "big_int": 1'
        + "0" * 400
        + ', "s": "1e400"}'
    )

    result = interpret_completion(JSON_REQUEST, fake_completion(text))

    parsed: Any = result.parsed
    assert parsed["max"] == 1.7976931348623157e308
    assert parsed["tiny"] == 0.0
    assert parsed["big_int"] == 10**400
    assert parsed["s"] == "1e400"


def test_deep_but_finite_json_is_kept() -> None:
    depth = 900
    text = '{"a": ' + "[" * depth + "1.5" + "]" * depth + "}"

    result = interpret_completion(JSON_REQUEST, fake_completion(text))

    value: Any = result.parsed
    assert value is not None
    node = value["a"]
    for _ in range(depth):
        node = node[0]
    assert node == 1.5
    assert math.isfinite(node)
