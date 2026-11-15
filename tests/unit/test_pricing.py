"""`domain/pricing.py` 的費用計算（ADR-0009、`config/models.yaml`）：金額一律 Decimal、6 位小數。"""

from decimal import Decimal

import pytest

from radar.domain.llm import LlmUsage
from radar.domain.pricing import PriceTier, llm_call_cost, total_input_tokens
from tests.llm_support import models_config

MODEL = "claude-haiku-5-5"
CONFIG = models_config()


def _cost(usage: LlmUsage, *, is_batch: bool = False) -> tuple[Decimal, PriceTier]:
    cost = llm_call_cost(CONFIG, MODEL, usage, is_batch=is_batch)
    return cost.cost_usd, cost.price_tier


def test_standard_price_input_and_output() -> None:
    # 2,200 輸入 × 0.10／百萬 ＋ 300 輸出 × 0.50／百萬 = 0.00022 + 0.00015
    assert _cost(LlmUsage(input_tokens=2200, output_tokens=300)) == (
        Decimal("0.000370"),
        PriceTier.STANDARD,
    )


def test_cost_is_decimal_with_six_places() -> None:
    cost, _tier = _cost(LlmUsage(input_tokens=1, output_tokens=0))

    assert isinstance(cost, Decimal)
    assert cost == Decimal("0.000000")  # 0.0000001 四捨五入到 6 位
    assert cost.as_tuple().exponent == -6


def test_rounding_is_half_up() -> None:
    # 5 輸入 × 0.10 / 1e6 = 0.0000005 → 0.000001
    assert _cost(LlmUsage(input_tokens=5, output_tokens=0))[0] == Decimal("0.000001")


def test_cache_read_and_write_multipliers() -> None:
    usage = LlmUsage(
        input_tokens=1000,
        output_tokens=0,
        cache_creation_input_tokens=1000,
        cache_read_input_tokens=1000,
    )
    # 一般 1000×0.10 ＋ 快取寫入 1000×0.10×2 ＋ 快取讀取 1000×0.10×0.1 = 100+200+10 → /1e6
    assert _cost(usage) == (Decimal("0.000310"), PriceTier.STANDARD)


def test_threshold_counts_all_input_including_cache() -> None:
    usage = LlmUsage(input_tokens=60_000, output_tokens=1000, cache_read_input_tokens=40_001)
    assert total_input_tokens(usage) == 100_001

    cost, tier = _cost(usage)

    assert tier is PriceTier.LONG_CONTEXT
    # 長上下文價：輸入 0.50、輸出 2.50；快取讀取 0.50×0.1
    expected = (
        Decimal(60_000) * Decimal("0.50")
        + Decimal(40_001) * Decimal("0.50") * Decimal("0.1")
        + Decimal(1000) * Decimal("2.50")
    ) / Decimal(1_000_000)
    assert cost == expected.quantize(Decimal("0.000001"))


def test_exactly_at_threshold_is_still_standard() -> None:
    assert _cost(LlmUsage(input_tokens=100_000, output_tokens=0))[1] is PriceTier.STANDARD
    assert _cost(LlmUsage(input_tokens=100_001, output_tokens=0))[1] is PriceTier.LONG_CONTEXT


def test_batch_is_half_price() -> None:
    usage = LlmUsage(input_tokens=2200, output_tokens=300)

    assert _cost(usage, is_batch=True)[0] == Decimal("0.000185")


def test_negative_tokens_are_rejected() -> None:
    with pytest.raises(ValueError, match="token"):
        _cost(LlmUsage(input_tokens=-1, output_tokens=0))


def test_unknown_model_is_rejected() -> None:
    with pytest.raises(KeyError):
        llm_call_cost(
            CONFIG, "claude-unknown", LlmUsage(input_tokens=1, output_tokens=1), is_batch=False
        )
