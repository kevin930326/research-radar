"""`LlmProvider` 的替身：照順序回準備好的回覆，不連網路。

- `replies`：每次呼叫用掉一個。是 `Completion` 就走和真實 adapter 同一個 `interpret_completion`
  （拒答、截斷、格式不對丟同一批錯誤）；是 `LlmError` 就原樣丟出（模擬逾時、限流、金鑰錯）。
- 入口同樣先 `check_llm_request`：傳 `str` 會丟 `NotCloudSafeError`，而且不會記進 `requests`、
  不會用掉回覆。
- 每次合法的請求記在 `requests`。回覆用完還被呼叫時丟 `FakeRepliesExhaustedError`。
- `fake_completion(text, ...)` 產生一個 `Completion`（內容是測試自己編的）。
"""

from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

from radar.domain.llm import (
    Completion,
    CompletionBlock,
    LlmError,
    LlmRequest,
    LlmResult,
    LlmUsage,
    check_llm_request,
    interpret_completion,
)

FAKE_MODEL = "claude-haiku-5-5"


class FakeRepliesExhaustedError(AssertionError):
    """替身沒有準備好的回覆了（測試少準備了回覆，或程式多呼叫了一次）。"""


def fake_completion(
    text: str | None,
    *,
    stop_reason: str | None = "end_turn",
    thinking: bool = False,
    usage: LlmUsage | None = None,
    response_id: str = "msg_fake_0001",
    request_id: str | None = "req_fake_0001",
    model: str = FAKE_MODEL,
    refusal_category: str | None = None,
    raw: Mapping[str, Any] | None = None,
) -> Completion:
    """一個假的 `Completion`。

    `text=None` 表示沒有 text 區塊；`thinking=True` 在前面放一個 `thinking` 區塊（Haiku 5.5 的
    思考預設開，回應可能先有它）。
    """
    blocks: list[CompletionBlock] = []
    if thinking:
        blocks.append(CompletionBlock(type="thinking"))
    if text is not None:
        blocks.append(CompletionBlock(type="text", text=text))
    return Completion(
        response_id=response_id,
        model=model,
        stop_reason=stop_reason,
        blocks=tuple(blocks),
        usage=usage or LlmUsage(input_tokens=100, output_tokens=20),
        request_id=request_id,
        refusal_category=refusal_category,
        raw=raw,
    )


class FakeLlmProvider:
    """照順序回準備好的回覆。"""

    def __init__(self, replies: Iterable[Completion | LlmError] = ()) -> None:
        self._replies: deque[Completion | LlmError] = deque(replies)
        self.requests: list[LlmRequest] = []

    def add(self, *replies: Completion | LlmError) -> None:
        """再準備幾個回覆（接在後面）。"""
        self._replies.extend(replies)

    @property
    def remaining(self) -> int:
        """還沒用掉的回覆數。"""
        return len(self._replies)

    def complete(self, request: LlmRequest) -> LlmResult:
        checked = check_llm_request(request)
        self.requests.append(checked)
        if not self._replies:
            raise FakeRepliesExhaustedError(
                f"FakeLlmProvider 沒有準備好的回覆（這是第 {len(self.requests)} 次呼叫）"
            )
        reply = self._replies.popleft()
        if isinstance(reply, LlmError):
            raise reply
        return interpret_completion(checked, reply)
