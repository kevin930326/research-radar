"""離線的輸入 token 估計（保守估計，`config/models.yaml` 的常數；不呼叫 count_tokens）。"""

from decimal import Decimal

from radar.domain.pricing import TokenEstimate, estimate_input_tokens
from tests.llm_support import models_config


def test_real_config_is_one_byte_per_token_plus_overhead() -> None:
    estimate = models_config().token_estimate

    assert estimate.bytes_per_token == Decimal("1")
    assert estimate_input_tokens(["abcd", "中文"], estimate) == 4 + 6 + estimate.overhead_tokens


def test_counts_utf8_bytes_not_characters() -> None:
    estimate = TokenEstimate(bytes_per_token="3", overhead_tokens=0)

    # 「研究」是 6 個位元組 → 2；「a」1 個位元組 → 無條件進位成 1。
    assert estimate_input_tokens(["研究"], estimate) == 2
    assert estimate_input_tokens(["a"], estimate) == 1
    assert estimate_input_tokens(["研究", "a"], estimate) == 3


def test_rounds_up() -> None:
    estimate = TokenEstimate(bytes_per_token="4", overhead_tokens=10)

    assert estimate_input_tokens(["x" * 9], estimate) == 3 + 10


def test_empty_input_is_just_overhead() -> None:
    estimate = TokenEstimate(bytes_per_token="2.5", overhead_tokens=7)

    assert estimate_input_tokens([], estimate) == 7


def test_conservative_estimate_is_never_below_byte_count_for_real_config() -> None:
    # 位元組級的 tokenizer 最壞一個位元組一個 token：估計不得少於位元組數。
    estimate = models_config().token_estimate
    text = "Information systems research 資訊系統研究 🚀" * 50

    assert estimate_input_tokens([text], estimate) >= len(text.encode("utf-8"))
