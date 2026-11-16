"""模型、價格與輸入 token 估計（AD-8、ADR-0009）：費用與估計只在這裡算。

設定來自 `config/models.yaml`（只經 `radar/config.py` 讀）。金額一律 `Decimal`：

- 設定檔裡的價格與倍率寫成字串（YAML 的浮點數會失真），驗證時不收 float。
- 每次呼叫的費用四捨五入到 6 位小數（對應資料庫的 `numeric(12,6)`，單位美元）。
- `總輸入 = input + cache_creation + cache_read`；超過 `long_context_threshold_tokens`（100,000）
  時整次呼叫改用長上下文的價格（輸入與輸出都換）。
- 快取讀取 = 輸入價 × `cache_read_multiplier`；快取寫入 = 輸入價 × `cache_write_multiplier`
  （回應不分 5 分鐘／1 小時，本專案不設快取，保守按 1 小時的 2 倍算）。
- Batch：整次乘 `batch_multiplier`（五折）。
- `max_call_cost`：還沒呼叫前的保守上限（估計輸入 token × 輸入價＋`max_tokens` × 輸出價），Batch
  送出前預留預算用（Story 2.9、ADR-0041）；無條件進位到 6 位小數。

輸入 token 估計是離線的保守估計（不呼叫 `count_tokens`）：UTF-8 位元組數 ÷ `bytes_per_token`
＋固定開銷，無條件進位。
"""

import math
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from radar.domain.ai_outputs import LlmTask
from radar.domain.llm import ALLOWED_MODELS, LlmUsage

# 費用的精度：6 位小數（`numeric(12,6)`）。
COST_QUANTUM: Final = Decimal("0.000001")
TOKENS_PER_PRICE_UNIT: Final = Decimal(1_000_000)


def _decimal_from_string(value: object) -> Decimal:
    """設定檔的金額與倍率：只收字串（YAML 浮點數會失真），必須是有限、不小於 0 的數字。"""
    if not isinstance(value, str):
        raise ValueError("金額與倍率要寫成字串（加引號），不收數字或其他型別")
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        raise ValueError("不是合法的數字") from None
    if not number.is_finite() or number < 0:
        raise ValueError("必須是有限、不小於 0 的數字")
    return number


type ConfigDecimal = Annotated[Decimal, BeforeValidator(_decimal_from_string)]


class PriceTier(StrEnum):
    """這次呼叫用的是哪一級價格（`llm_call.price_tier`）。"""

    STANDARD = "standard"
    LONG_CONTEXT = "long_context"


class ModelPrice(BaseModel):
    """一個模型的價格（美元／每百萬 tokens）與退役日註記。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 官方寫「不早於」這一天（ADR-0009 第 5 點）；到時照換模型流程換。
    retires_not_before: date | None = None
    input_usd_per_mtok: ConfigDecimal
    output_usd_per_mtok: ConfigDecimal
    long_context_input_usd_per_mtok: ConfigDecimal
    long_context_output_usd_per_mtok: ConfigDecimal


class TaskModel(BaseModel):
    """一種工作用哪個模型；`allow_over_input_limit` 為真才可以送超過 `max_input_tokens` 的輸入。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    allow_over_input_limit: bool = Field(default=False, strict=True)


class TokenEstimate(BaseModel):
    """離線估計輸入 token 的常數（保守估計：寧可估多，不可估少）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bytes_per_token: ConfigDecimal
    overhead_tokens: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _positive(self) -> Self:
        if self.bytes_per_token <= 0:
            raise ValueError("bytes_per_token 要大於 0")
        return self


class ModelsConfig(BaseModel):
    """`config/models.yaml`。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verified_on: date
    cross_checked_on: date | None = None
    source_url: str = Field(pattern=r"^https://\S+$")
    long_context_threshold_tokens: int = Field(gt=0, strict=True)
    max_input_tokens: int = Field(gt=0, strict=True)
    cache_read_multiplier: ConfigDecimal
    cache_write_multiplier: ConfigDecimal
    batch_multiplier: ConfigDecimal
    token_estimate: TokenEstimate
    models: Mapping[str, ModelPrice]
    tasks: Mapping[LlmTask, TaskModel]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        unknown_models = sorted(set(self.models) - ALLOWED_MODELS)
        if unknown_models:
            raise ValueError(
                "models 只能列模型白名單（ALLOWED_MODELS）裡的模型：" + "、".join(unknown_models)
            )
        missing = sorted(task.value for task in set(LlmTask) - set(self.tasks))
        if missing:
            raise ValueError("tasks 要列出全部六個工作，缺：" + "、".join(missing))
        for task, choice in self.tasks.items():
            if choice.model not in self.models:
                raise ValueError(f"tasks.{task.value}.model 沒有在 models 列出價格")
        if self.batch_multiplier > 1:
            raise ValueError("batch_multiplier 不能大於 1")
        return self

    def task_model(self, task: LlmTask) -> TaskModel:
        """這個工作現在該用的模型設定。"""
        return self.tasks[LlmTask(task)]


class LlmCost(BaseModel):
    """一次呼叫的費用與價格級別。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cost_usd: Decimal
    price_tier: PriceTier


def total_input_tokens(usage: LlmUsage) -> int:
    """官方的算法：輸入＝一般輸入＋快取寫入＋快取讀取（包含系統提示）。"""
    return usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens


def llm_call_cost(config: ModelsConfig, model: str, usage: LlmUsage, *, is_batch: bool) -> LlmCost:
    """依價格表算一次呼叫的費用（美元，四捨五入到 6 位小數）。

    Raises:
        KeyError: `model` 不在價格表。
        ValueError: token 數是負的。
    """
    counts = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_creation_input_tokens,
        usage.cache_read_input_tokens,
    )
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("token 數必須是不小於 0 的整數")
    price = config.models[model]
    if total_input_tokens(usage) > config.long_context_threshold_tokens:
        tier = PriceTier.LONG_CONTEXT
        input_price = price.long_context_input_usd_per_mtok
        output_price = price.long_context_output_usd_per_mtok
    else:
        tier = PriceTier.STANDARD
        input_price = price.input_usd_per_mtok
        output_price = price.output_usd_per_mtok
    raw = (
        Decimal(usage.input_tokens) * input_price
        + Decimal(usage.cache_creation_input_tokens) * input_price * config.cache_write_multiplier
        + Decimal(usage.cache_read_input_tokens) * input_price * config.cache_read_multiplier
        + Decimal(usage.output_tokens) * output_price
    ) / TOKENS_PER_PRICE_UNIT
    if is_batch:
        raw *= config.batch_multiplier
    return LlmCost(cost_usd=raw.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP), price_tier=tier)


def max_call_cost(
    config: ModelsConfig,
    model: str,
    estimated_input_tokens: int,
    max_tokens: int,
    *,
    is_batch: bool,
) -> Decimal:
    """一次呼叫最多花多少（美元，無條件進位到 6 位小數）：估計的輸入 token（`estimate_input_tokens`
    的保守估計）× 輸入價＋`max_tokens`（輸出最多這麼多）× 輸出價；估計輸入超過
    `long_context_threshold_tokens` 時兩邊都改用長上下文價；Batch 再乘 `batch_multiplier`。

    不設快取（本專案不用 prompt 快取），所以沒有快取寫入的 2 倍。

    Raises:
        KeyError: `model` 不在價格表。
        ValueError: `estimated_input_tokens` 不是 ≥ 0 的整數，或 `max_tokens` 不是 ≥ 1 的整數。
    """
    if type(estimated_input_tokens) is not int or estimated_input_tokens < 0:
        raise ValueError("estimated_input_tokens 要是不小於 0 的整數")
    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("max_tokens 要是不小於 1 的整數")
    price = config.models[model]
    if estimated_input_tokens > config.long_context_threshold_tokens:
        input_price = price.long_context_input_usd_per_mtok
        output_price = price.long_context_output_usd_per_mtok
    else:
        input_price = price.input_usd_per_mtok
        output_price = price.output_usd_per_mtok
    raw = (
        Decimal(estimated_input_tokens) * input_price + Decimal(max_tokens) * output_price
    ) / TOKENS_PER_PRICE_UNIT
    if is_batch:
        raw *= config.batch_multiplier
    return raw.quantize(COST_QUANTUM, rounding=ROUND_CEILING)


def estimate_input_tokens(texts: Sequence[str], estimate: TokenEstimate) -> int:
    """這些文字（系統提示＋每個區塊）送出去大約是幾個 token：保守估計，無條件進位。

    UTF-8 位元組總數 ÷ `bytes_per_token` ＋ `overhead_tokens`。
    """
    total_bytes = sum(len(text.encode("utf-8")) for text in texts)
    return math.ceil(Decimal(total_bytes) / estimate.bytes_per_token) + estimate.overhead_tokens
