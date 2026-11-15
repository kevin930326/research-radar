"""2.4 抽出的共用文字檢查裡的數字解析（`radar/domain/validators/text_checks.py`），Story 2.6 Codex
主審查後補：

- 負號和數字隔一個空白（`β = - 0.42`）讀成負數，不再讀成 0.42。
- 科學記號（`4.2 × 10^-2`、`10⁻²`、`10^-3`）：`numbers` 整段不取（不把 4.2、10、2 當成輸入裡的
  數字）；`structured_numbers` 算出完整的值（0.042）。
- `structured_numbers`（研究模型的結構化數值用）只有完整量級：`12 million` 只有 12000000。
- 輸出（`numbers` 沒有 `keep_unscaled`）裡有科學記號或 `^`，一律回報讀不懂：`9.9 x 10-7`、
  `9.9 * 10-7`、`9.9·10+3` 這種 `has_unsupported_number` 沒擋到的寫法也拒收（確認審查）。
- 2.4 中文重點相對 a5bbc1f：只放寬一種——同一個負數的等價寫法（輸入 `= - 0.42`、輸出 `-0.42`，
  或反過來）；其他都是變嚴（錯的正號、科學記號的碎片、輸出裡任何科學記號）。2.4 原本的測試
  斷言都沒改。
"""

import time
from decimal import Decimal

import pytest

from radar.domain.ai_outputs import OutputRejectedError
from radar.domain.errors import ErrorCode
from radar.domain.validators.text_checks import (
    InputVocabulary,
    Number,
    check_copy,
    numbers,
    structured_numbers,
)
from tests.unit.test_zh_summary_validator import _numbers_only


def _n(value: str, *, percent: bool = False) -> Number:
    return Decimal(value).normalize(), percent


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("β = - 0.42", {_n("-0.42")}),
        ("(- 0.42)", {_n("-0.42")}),
        ("β: - .42", {_n("-0.42")}),
        ("β < - 0.1", {_n("-0.1")}),
        ("3 - 5 years", {_n("3"), _n("5")}),  # 範圍，不是負號
        ("β = -0.42", {_n("-0.42")}),
    ],
)
def test_spaced_minus_after_an_equals_sign_is_a_minus(text: str, expected: set[Number]) -> None:
    assert numbers(check_copy(text))[0] == expected


@pytest.mark.parametrize(
    "text",
    ["β = 4.2 × 10^-2", "β = 4.2 x 10^-2", "β = 4.2 × 10⁻²", "β = 4.2·10^−2", "β = 10^-3", "2^10"],
)
def test_scientific_notation_gives_no_fragments(text: str) -> None:
    assert numbers(check_copy(text))[0] == set()


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("β = 4.2 × 10^-2", "0.042"),
        ("β = 4.2 x 10^-2", "0.042"),
        ("β = 4.2 × 10⁻²", "0.042"),
        ("β = -4.2 × 10^-2", "-0.042"),
        ("p = 10^-3", "0.001"),
        ("N = 1.5 × 10^3", "1500"),
    ],
)
def test_structured_numbers_read_the_whole_scientific_value(text: str, value: str) -> None:
    found = structured_numbers(check_copy(text))

    assert _n(value) in found
    assert _n("4.2") not in found
    assert _n("10") not in found


@pytest.mark.parametrize(
    ("text", "present", "absent"),
    [
        ("N = 12 million", "12000000", "12"),
        ("1.2 thousand users", "1200", "1.2"),
        ("12 萬名使用者", "120000", "12"),
        ("two hundred users", "200", "2"),
        ("β = - 0.42", "-0.42", "0.42"),
    ],
)
def test_structured_numbers_keep_only_the_full_magnitude(
    text: str, present: str, absent: str
) -> None:
    found = structured_numbers(check_copy(text))

    assert _n(present) in found
    assert _n(absent) not in found


def test_input_vocabulary_of_zh_summary_still_keeps_unscaled_numbers() -> None:
    # 2.4 原本的行為不變：中文重點可以寫「12 million」裡的 12。
    vocabulary = InputVocabulary.build(check_copy("N = 12 million"))

    assert {_n("12"), _n("12000000")} <= vocabulary.numbers


# --- 2.4 中文重點：更嚴的兩種、原本誤拒的一種 ------------------------------------------------


@pytest.mark.parametrize(
    ("method", "abstract"),
    [
        ("效果為 0.42", "The effect was β = - 0.42."),
        ("效果為 4.2", "The effect was β = 4.2 × 10^-2."),
        ("效果為 10", "The effect was β = 4.2 × 10^-2."),
    ],
)
def test_zh_summary_no_longer_accepts_a_wrong_sign_or_a_fragment(
    method: str, abstract: str
) -> None:
    with pytest.raises(OutputRejectedError) as caught:
        _numbers_only(method, abstract)

    assert caught.value.code is ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT


@pytest.mark.parametrize("text", ["9.9 x 10-7", "9.9 X 10-7", "9.9 * 10-7", "9.9·10+3", "10^-3"])
def test_scientific_notation_in_output_is_unreadable(text: str) -> None:
    # 確認審查：整段不取數字之後沒東西可核對；輸出（沒有 keep_unscaled）要回報讀不懂。
    assert numbers(check_copy(text))[1] is True
    assert numbers(check_copy(text), keep_unscaled=True)[1] is False


@pytest.mark.parametrize("text", ["β = 0.42", "3 - 5 years", "a 2 x 10 grid", "10-15 minutes"])
def test_ordinary_numbers_in_output_are_readable(text: str) -> None:
    assert numbers(check_copy(text))[1] is False


@pytest.mark.parametrize(
    ("method", "abstract"),
    [
        ("效果為 9.9 x 10-7", "The effect was small."),
        ("效果為 9.9 * 10-7", "The effect was small."),
        ("效果為 9.9·10+3", "The effect was small."),
        # 輸入寫了同一個科學記號也不放行（a5bbc1f 時碎片都對得上會過；這裡變嚴）。
        ("效果為 9.9 x 10-7", "The effect was 9.9 x 10-7."),
    ],
)
def test_zh_summary_rejects_scientific_notation_in_the_output(method: str, abstract: str) -> None:
    with pytest.raises(OutputRejectedError) as caught:
        _numbers_only(method, abstract)

    assert caught.value.code is ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT


def test_zh_summary_accepts_the_correct_sign_of_a_spaced_minus() -> None:
    assert _numbers_only("效果為 -0.42", "The effect was β = - 0.42.").method == "效果為 -0.42"


@pytest.mark.parametrize("unit", ["^", "1^", "a^b ", "10^", "× 10^-", "4.2×10^-2 "])
def test_long_adversarial_scientific_text_is_quick(unit: str) -> None:
    text = check_copy(unit * 16_000)
    started = time.perf_counter()

    numbers(text, keep_unscaled=True)
    structured_numbers(text)

    assert time.perf_counter() - started < 2


# --- 0-A（2.6 決定 13 選 B，Kevin 2026-11-15）：字母縮寫的量級讀不懂 ---------------------------


@pytest.mark.parametrize(
    ("text", "unreadable"),
    [
        ("12M users", ("12", "12000000")),
        ("12m users", ("12", "12000000")),
        ("12 M users", ("12", "12000000")),
        ("12 m users", ("12", "12000000")),
        ("10k users", ("10", "10000")),
        ("10K users", ("10", "10000")),
        ("10 k users", ("10", "10000")),
        ("10 K users", ("10", "10000")),
        ("3.5K users", ("3.5", "3500")),
        ("3.5k users", ("3.5", "3500")),
        ("3.5 K users", ("3.5", "3500")),
        ("2B transactions", ("2", "2000000000")),
        ("2b transactions", ("2", "2000000000")),
        ("2 B transactions", ("2", "2000000000")),
        ("5bn transactions", ("5", "5000000000")),
        ("5 BN transactions", ("5", "5000000000")),
        ("1,200K users", ("1200", "1200000", "200")),
        ("$.5M in funding", ("0.5", "500000", "5")),
        ("a $12M fund (N = 12M).", ("12", "12000000")),
    ],
)
def test_structured_numbers_skip_abbreviated_magnitudes(
    text: str, unreadable: tuple[str, ...]
) -> None:
    # 讀不出量級：不留縮寫前面的數字，也不猜成乘開的值（整段不取，同科學記號）。
    found = structured_numbers(check_copy(text))

    for value in unreadable:
        assert _n(value) not in found, value


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("12 months", "12"),  # m 後面還有字母：不是縮寫
        ("5 MB of data", "5"),
        ("Study 2 B2B firms", "2"),
        ("H2b was supported in 200 firms", "200"),
        ("H2b was supported", "2"),  # 代號裡的 2 照舊讀得到（前面接字母，不是縮寫）
        ("B2B firms", "2"),
        ("N = 12 million", "12000000"),
        ("12M users and 12 other firms", "12"),  # 同一個數字在別處寫得清楚就照常
        ("p < .05 in 300 firms", "300"),
        ("312 K-12 teachers", "312"),  # 字母後面接連字號：不是縮寫
        ("200 M-Turk workers", "200"),
    ],
)
def test_structured_numbers_still_read_ordinary_numbers_next_to_letters(
    text: str, value: str
) -> None:
    assert _n(value) in structured_numbers(check_copy(text))


def test_abbreviated_magnitude_does_not_change_the_zh_summary_vocabulary() -> None:
    # 只影響研究模型的結構化數值；中文重點的字彙（`numbers(keep_unscaled=True)`）照舊。
    vocabulary = InputVocabulary.build(check_copy("12M users"))

    assert _n("12") in vocabulary.numbers


@pytest.mark.parametrize("unit", ["1k", "1 k ", "1,000K ", ".5M ", "9" * 50 + "M "])
def test_long_abbreviated_magnitude_text_is_quick(unit: str) -> None:
    text = check_copy(unit * 16_000)
    started = time.perf_counter()

    structured_numbers(text)

    assert time.perf_counter() - started < 2
