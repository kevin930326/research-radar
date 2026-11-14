"""Anthropic 的 `LlmProvider`（AD-8、AD-9、ADR-0009、ADR-0029）：全專案唯一的 Anthropic 連接點。

- 入口先 `snapshot_llm_request`（檢查後重建副本，之後只讀副本）：不是 `LlmRequest`、或裡面
  有不是 `CloudSafeText` 的文字，在送出任何請求前丟 `NotCloudSafeError`；`model` 不在白名單、
  `output_schema` 不是模型類別也送不出去。
- 請求：`POST {base_url}/v1/messages`，系統提示是 `system_prompt(request)`（固定聲明開頭），
  單一 user 訊息、每個 `CloudSafeText` 一個 text 區塊。不給工具；不送 temperature、top_p、
  top_k 與 assistant prefill（Haiku 5.5 會回 400）。effort、結構化輸出放 `output_config`，
  關思考是 `thinking={"type": "disabled"}`。
- SDK client 明確帶 `api_key` 與 `base_url`（沒帶時 SDK 會讀 `ANTHROPIC_API_KEY`、
  `ANTHROPIC_BASE_URL` 與設定檔；明確帶了就不讀這幾個），`max_retries=0`（重試只在這裡用 stamina
  做）、`open_telemetry=False`。注意：`anthropic==2.7.0` 不管帶什麼都還是會讀
  `ANTHROPIC_CUSTOM_HEADERS`（設了就多送那些標頭）與 `ANTHROPIC_WEBHOOK_SIGNING_KEY`
  （ADR-0029 取捨）。
- 回應：用 `with_raw_response` 拿原始本文與 `request-id`，自己用 Pydantic 驗形狀
  （`extra="ignore"`）轉成 `Completion`，交給 `interpret_completion` 判讀。
- 錯誤對應（一律 `from None`，訊息只有狀態碼或錯誤類別，不含 key、文件內容與模型輸出）：
  - 401、403、404 → `LlmConfigurationError`；400、413、422 與其他 4xx → `LlmInvalidRequestError`；
  - 408 與讀寫逾時 → `LlmTimeoutError`；429 → `LlmRateLimitedError`；409、5xx（含 529）→
    `LlmServerError`；連不上 → `LlmConnectionError`。
- 重試：只重試 `LlmTransientError`，最多 `attempts` 次（含第一次）；帶 `Retry-After` 時不超過
  `retry_after_max` 秒就照它等，超過就不再重試、丟出最後一次的錯誤（不提早重送）。
  `deadline`（秒）是含所有重試的總時限（交給 stamina 的 `timeout`），預設 None 不限。
"""

import json
from types import TracebackType
from typing import Final, Self

import anthropic
import httpx2
import stamina
from anthropic.types import OutputConfigParam, TextBlockParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from radar.adapters.heartbeat import parse_retry_after
from radar.domain.llm import (
    Completion,
    CompletionBlock,
    LlmConfigurationError,
    LlmConnectionError,
    LlmError,
    LlmInvalidRequestError,
    LlmMalformedResponseError,
    LlmRateLimitedError,
    LlmRequest,
    LlmResult,
    LlmServerError,
    LlmTimeoutError,
    LlmTransientError,
    LlmUsage,
    interpret_completion,
    snapshot_llm_request,
    system_prompt,
)
from radar.domain.policy import cloud_text

ANTHROPIC_BASE_URL: Final = "https://api.anthropic.com"
CONFIGURATION_STATUSES: Final = frozenset({401, 403, 404})
SERVER_STATUSES: Final = frozenset({409})


# --- 回應形狀（只取用得到的欄位，其餘忽略） ----------------------------------------------------


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)

    def to_domain(self) -> LlmUsage:
        return LlmUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens or 0,
            cache_read_input_tokens=self.cache_read_input_tokens or 0,
        )


class _Block(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    type: str = Field(min_length=1)
    text: str | None = None

    @model_validator(mode="after")
    def _text_block_has_text(self) -> Self:
        if self.type == "text" and self.text is None:
            raise ValueError("text 區塊沒有 text")
        return self


class _StopDetails(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    type: str
    category: str | None = None


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    id: str = Field(min_length=1)
    type: str = Field(pattern=r"^message$")
    role: str = Field(pattern=r"^assistant$")
    model: str = Field(min_length=1)
    content: list[_Block]
    stop_reason: str | None
    stop_details: _StopDetails | None = None
    usage: _Usage


def to_completion(body: bytes, request_id: str | None) -> Completion:
    """把 200 回應的本文轉成 `Completion`。

    Raises:
        LlmMalformedResponseError: 不是合法 JSON 或缺必要欄位；本文裡的 `usage`、`id` 驗得過時
            照樣帶上，讓閘門記帳。訊息只有欄位路徑與錯誤種類，不含內容。
    """
    try:
        data = json.loads(body)
    except ValueError, RecursionError:
        raise LlmMalformedResponseError("回應不是合法的 JSON", request_id=request_id) from None
    if not isinstance(data, dict):
        raise LlmMalformedResponseError("回應不是 JSON 物件", request_id=request_id)
    try:
        message = _Message.model_validate(data)
    except ValidationError as exc:
        problems = "、".join(
            f"{'.'.join(str(part) for part in error['loc']) or '（最上層）'}：{error['type']}"
            for error in exc.errors(include_input=False, include_url=False, include_context=False)
        )
        response_id = data.get("id")
        raise LlmMalformedResponseError(
            f"回應格式不對（{problems}）",
            request_id=request_id,
            response_id=response_id if isinstance(response_id, str) else None,
            usage=_best_effort_usage(data.get("usage")),
        ) from None
    category = (
        message.stop_details.category
        if message.stop_details is not None and message.stop_details.type == "refusal"
        else None
    )
    return Completion(
        response_id=message.id,
        model=message.model,
        stop_reason=message.stop_reason,
        blocks=tuple(
            CompletionBlock(type=block.type, text=block.text if block.type == "text" else None)
            for block in message.content
        ),
        usage=message.usage.to_domain(),
        request_id=request_id,
        refusal_category=category,
        raw=data,
    )


def _best_effort_usage(value: object) -> LlmUsage | None:
    try:
        return _Usage.model_validate(value).to_domain()
    except ValidationError:
        return None


# --- SDK 例外 → domain 錯誤 ------------------------------------------------------------------


def to_llm_error(exc: anthropic.AnthropicError) -> LlmError:
    """SDK 的例外換成 domain 的錯誤。只取狀態碼、`request-id` 與 `Retry-After`，不取訊息。"""
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        request_id = exc.request_id
        detail = f"HTTP {status}"
        retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
        if status in CONFIGURATION_STATUSES or status < 400:
            return LlmConfigurationError(detail, status=status, request_id=request_id)
        if status == 408:
            return LlmTimeoutError(
                detail, status=status, request_id=request_id, retry_after=retry_after
            )
        if status == 429:
            return LlmRateLimitedError(
                detail, status=status, request_id=request_id, retry_after=retry_after
            )
        if status in SERVER_STATUSES or status >= 500:
            return LlmServerError(
                detail, status=status, request_id=request_id, retry_after=retry_after
            )
        return LlmInvalidRequestError(detail, status=status, request_id=request_id)
    if isinstance(exc, anthropic.APITimeoutError):
        return LlmTimeoutError("逾時")
    if isinstance(exc, anthropic.APIConnectionError):
        return LlmConnectionError("連線錯誤")
    if isinstance(exc, anthropic.APIResponseValidationError):
        return LlmMalformedResponseError("回應格式不對（SDK 驗證）")
    # 例如讀不到憑證（CredentialsError）：不是重試能解決的，停下來讓人處理。
    return LlmConfigurationError(f"SDK 錯誤（{type(exc).__name__}）")


# --- adapter -------------------------------------------------------------------------------


class AnthropicLlmProvider:
    """呼叫 Anthropic Messages API 的 `LlmProvider`。用完呼叫 `close()`（或用 `with`）。"""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float,
        attempts: int,
        retry_wait_initial: float,
        retry_wait_max: float,
        retry_wait_jitter: float,
        retry_after_max: float,
        deadline: float | None = None,
        base_url: str = ANTHROPIC_BASE_URL,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("api_key 不能是空的")
        if attempts < 1:
            raise ValueError("attempts 至少是 1")
        if deadline is not None and deadline <= 0:
            raise ValueError("deadline 要大於 0 或是 None")
        self._deadline = deadline
        self._base_url = base_url
        self._attempts = attempts
        self._retry_wait_initial = retry_wait_initial
        self._retry_wait_max = retry_wait_max
        self._retry_wait_jitter = retry_wait_jitter
        self._retry_after_max = retry_after_max
        http_client = (
            None if transport is None else httpx2.Client(transport=transport, timeout=timeout)
        )
        self._client = anthropic.Anthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
            open_telemetry=False,
            http_client=http_client,
        )

    def __repr__(self) -> str:
        # 不印 key。
        return f"AnthropicLlmProvider(base_url={self._base_url!r})"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def complete(self, request: LlmRequest) -> LlmResult:
        """送一次請求，回判讀好的結果（錯誤見 `radar.ports.LlmProvider.complete`）。"""
        # 檢查後重建一份副本，之後只讀這一份（檢查與送出之間沒有時間差）。
        checked = snapshot_llm_request(request)
        for attempt in stamina.retry_context(
            on=self._backoff,
            attempts=self._attempts,
            # 含所有重試的總時限（AD-8 的硬性時間上限）；超過就不再重試，丟出最後一次的錯誤。
            # 正在進行的那一次嘗試不會被打斷（最壞是 deadline + timeout 秒）。None＝不限。
            timeout=self._deadline,
            wait_initial=self._retry_wait_initial,
            wait_max=self._retry_wait_max,
            wait_jitter=self._retry_wait_jitter,
        ):
            with attempt:
                completion = self._create_once(checked)
        return interpret_completion(checked, completion)

    def _backoff(self, error: Exception) -> bool | float:
        """stamina 的退避判斷：不重試回 False；照一般退避回 True；照 `Retry-After` 回秒數。

        對方要等的超過 `retry_after_max`：回 False，不等也不提早重送，最後一次的錯誤照常往外丟。
        """
        if not isinstance(error, LlmTransientError):
            return False
        if error.retry_after is None:
            return True
        if error.retry_after > self._retry_after_max:
            return False
        return error.retry_after

    def _create_once(self, request: LlmRequest) -> Completion:
        """送一次請求並轉成 `Completion`；失敗時丟 domain 的錯誤。"""
        output_config: OutputConfigParam = {}
        if request.effort is not None:
            output_config["effort"] = request.effort
        schema = request.output_json_schema()
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        # 文字一律經 policy 的 cloud_text 拿：號碼不在放行登記裡（被硬改過）就丟錯、不送出。
        content: list[TextBlockParam] = [
            {"type": "text", "text": cloud_text(block)} for block in request.content
        ]
        try:
            raw = self._client.messages.with_raw_response.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=system_prompt(request),
                messages=[{"role": "user", "content": content}],
                output_config=output_config if output_config else anthropic.omit,
                thinking={"type": "disabled"} if request.disable_thinking else anthropic.omit,
            )
            body = raw.read()
        except anthropic.AnthropicError as exc:
            raise to_llm_error(exc) from None
        except httpx2.TimeoutException:
            # 讀本文時才逾時（SDK 沒有包起來的）。
            raise LlmTimeoutError("讀取回應逾時") from None
        except httpx2.HTTPError as exc:
            # 讀本文時才斷線之類（SDK 沒有包起來的）：當成連線錯誤，可以重試。
            raise LlmConnectionError(f"連線錯誤（{type(exc).__name__}）") from None
        return to_completion(body, raw.request_id)
