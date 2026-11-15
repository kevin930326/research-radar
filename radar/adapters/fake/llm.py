"""`LlmProvider` 的替身：照順序回準備好的回覆，不連網路。

- `replies`：每次呼叫用掉一個。是 `Completion` 就走和真實 adapter 同一個 `interpret_completion`
  （拒答、截斷、格式不對丟同一批錯誤）；是 `LlmError` 就原樣丟出（模擬逾時、限流、金鑰錯）。
- 入口同樣先 `snapshot_llm_request`（檢查後重建副本）：傳 `str` 會丟 `NotCloudSafeError`，
  而且不會記進 `requests`、不會用掉回覆。`requests` 記的是重建的副本（和原請求相等）。
- 每次合法的請求記在 `requests`。回覆用完還被呼叫時丟 `FakeRepliesExhaustedError`。
- `fake_completion(text, ...)` 產生一個 `Completion`（內容是測試自己編的）。
- `submit_batch`（Story 2.9）：`batch_replies` 每次用掉一個；是字串就當成供應商的 batch ID 回傳，
  是例外就原樣丟出（模擬 429、逾時、回應看不懂）。入口同樣先檢查、重建每一筆
  （`snapshot_llm_batch_request`）；合法的呼叫記在 `batch_requests`（每次一個 tuple）。回覆用完
  還被呼叫時丟 `FakeRepliesExhaustedError`。
- `retrieve_batch`／`batch_results`（Story 2.11）：`batch_statuses[供應商 batch ID]` 是依序要回的
  狀態（`LlmBatchStatus`，或要丟的 `LlmError`）；每次查用掉一個，只剩最後一個時一直回它。
  `batch_result_lines[供應商 batch ID]` 是結果的每一行（`LlmBatchResult`，或下載到這裡要丟的例外，
  模擬斷線）；每次下載都從頭回整份。沒準備的 ID：查狀態丟 404 的 `LlmConfigurationError`、下載丟
  `FakeRepliesExhaustedError`。查過的 ID 記在 `retrieved`、下載過的記在 `downloaded`。
  `fake_batch_status(...)` 產生一個狀態。假的狀態與結果都不是真的錄的，形狀照官方文件。
"""

from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from radar.domain.llm import (
    BatchProcessingStatus,
    BatchRequestCounts,
    Completion,
    CompletionBlock,
    LlmBatchRequest,
    LlmBatchResult,
    LlmBatchStatus,
    LlmConfigurationError,
    LlmError,
    LlmRequest,
    LlmResult,
    LlmUsage,
    interpret_completion,
    snapshot_llm_batch_request,
    snapshot_llm_request,
)

FAKE_MODEL = "claude-haiku-5-5"
_DAY = timedelta(hours=24)
_HOUR = timedelta(hours=1)


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


def fake_batch_status(
    provider_batch_id: str,
    processing_status: BatchProcessingStatus = BatchProcessingStatus.ENDED,
    *,
    created_at: datetime = datetime(2026, 11, 15, 4, 0, tzinfo=UTC),
    expires_at: datetime | None = None,
    ended_at: datetime | None = None,
    results_available: bool | None = None,
    request_counts: BatchRequestCounts | None = None,
) -> LlmBatchStatus:
    """一個假的批次狀態（不是真的錄的，形狀照官方文件）。`expires_at` 預設建立＋24 小時；
    `results_available` 預設「已結束就有」；`ended_at` 預設已結束時是建立＋1 小時；
    `request_counts` 預設不給（匯入就不核對結果的行數）。"""
    ended = processing_status is BatchProcessingStatus.ENDED
    return LlmBatchStatus(
        provider_batch_id=provider_batch_id,
        processing_status=processing_status,
        created_at=created_at,
        expires_at=expires_at or created_at + _DAY,
        ended_at=ended_at or (created_at + _HOUR if ended else None),
        results_available=ended if results_available is None else results_available,
        request_counts=request_counts,
    )


class FakeLlmProvider:
    """照順序回準備好的回覆。"""

    def __init__(
        self,
        replies: Iterable[Completion | LlmError] = (),
        *,
        batch_replies: Iterable[str | BaseException] = (),
    ) -> None:
        self._replies: deque[Completion | LlmError] = deque(replies)
        self.requests: list[LlmRequest] = []
        self._batch_replies: deque[str | BaseException] = deque(batch_replies)
        self.batch_requests: list[tuple[LlmBatchRequest, ...]] = []
        self.batch_statuses: dict[str, list[LlmBatchStatus | LlmError]] = {}
        self.batch_result_lines: dict[str, list[LlmBatchResult | BaseException]] = {}
        self.retrieved: list[str] = []
        self.downloaded: list[str] = []

    def retrieve_batch(self, provider_batch_id: str) -> LlmBatchStatus:
        self.retrieved.append(provider_batch_id)
        queue = self.batch_statuses.get(provider_batch_id)
        if not queue:
            raise LlmConfigurationError("HTTP 404", status=404)
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, LlmError):
            raise reply
        return reply

    def batch_results(self, provider_batch_id: str) -> Iterator[LlmBatchResult]:
        if provider_batch_id not in self.batch_result_lines:
            raise FakeRepliesExhaustedError(f"FakeLlmProvider 沒有準備 {provider_batch_id} 的結果")
        lines = list(self.batch_result_lines[provider_batch_id])
        self.downloaded.append(provider_batch_id)
        return self._lines(lines)

    @staticmethod
    def _lines(lines: list[LlmBatchResult | BaseException]) -> Iterator[LlmBatchResult]:
        for line in lines:
            if isinstance(line, BaseException):
                raise line
            yield line

    def add_batch(self, *replies: str | BaseException) -> None:
        """再準備幾個 `submit_batch` 的回覆（batch ID 或例外，接在後面）。"""
        self._batch_replies.extend(replies)

    def submit_batch(self, requests: Sequence[LlmBatchRequest]) -> str:
        if isinstance(requests, (str, bytes, bytearray)) or not isinstance(requests, Sequence):
            raise TypeError("submit_batch 收 LlmBatchRequest 的序列")
        checked = tuple(snapshot_llm_batch_request(item) for item in requests)
        if not checked:
            raise ValueError("submit_batch 至少要有一筆請求")
        if len({item.custom_id for item in checked}) != len(checked):
            raise ValueError("同一批的 custom_id 不能重複")
        self.batch_requests.append(checked)
        if not self._batch_replies:
            raise FakeRepliesExhaustedError(
                "FakeLlmProvider 沒有準備好的 submit_batch 回覆"
                f"（這是第 {len(self.batch_requests)} 次呼叫）"
            )
        reply = self._batch_replies.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def add(self, *replies: Completion | LlmError) -> None:
        """再準備幾個回覆（接在後面）。"""
        self._replies.extend(replies)

    @property
    def remaining(self) -> int:
        """還沒用掉的回覆數。"""
        return len(self._replies)

    def complete(self, request: LlmRequest) -> LlmResult:
        checked = snapshot_llm_request(request)
        self.requests.append(checked)
        if not self._replies:
            raise FakeRepliesExhaustedError(
                f"FakeLlmProvider 沒有準備好的回覆（這是第 {len(self.requests)} 次呼叫）"
            )
        reply = self._replies.popleft()
        if isinstance(reply, LlmError):
            raise reply
        return interpret_completion(checked, reply)
