"""構面名稱的正規化 `normalize_name`（ADR-0012 第 1、2 點、Story 2.6）：

- 每一步各有例子：NFKC、像縮寫的括號、casefold、撇號與連字號、其餘標點、空白。
- 不做字根還原。
- 性質（hypothesis）：做兩次和做一次一樣；結果沒有大寫、頭尾沒有空白、沒有連續空白。
"""

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from radar.domain.constructs import normalize_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # NFKC：全形換半形。
        ("Ｔｒｕｓｔ", "trust"),
        ("Perceived　Usefulness", "perceived usefulness"),
        # 前面幾個詞的首字母縮寫括號拿掉（不分大小寫、可多一個小寫 s、連字號隔開的也算詞）。
        ("Perceived Usefulness (PU)", "perceived usefulness"),
        ("Perceived Usefulness (pU)", "perceived usefulness"),
        ("Electronic Word-of-Mouth (eWOM)", "electronic wordofmouth"),
        (
            "Information and Communication Technologies (ICTs)",
            "information and communication technologies",
        ),
        ("Perceived Usefulness (PU) of Apps", "perceived usefulness of apps"),
        # 不是首字母的括號留著（拿掉可能把不同構面併成一列）。
        ("Trust (Vendor)", "trust vendor"),
        ("Satisfaction (Overall)", "satisfaction overall"),
        ("E-commerce Adoption (B2B)", "ecommerce adoption b2b"),
        ("Intention (UTAUT2) to Use", "intention utaut2 to use"),
        ("Trust (TR-1)", "trust tr1"),
        ("Usefulness (PE)", "usefulness pe"),
        ("(PU)", "pu"),
        # 全小寫、有空白或太長的括號不算縮寫，括號換成空白、內容留著。
        ("Usefulness (perceived)", "usefulness perceived"),
        ("Perceived Usefulness (pu)", "perceived usefulness pu"),
        ("Trust (in Vendor)", "trust in vendor"),
        ("Trust (ABCDEFGHIJKLMNOP)", "trust abcdefghijklmnop"),
        # casefold。
        ("TRUST", "trust"),
        ("Straße Quality", "strasse quality"),
        # 撇號與連字號拿掉。
        ("E-Commerce Trust", "ecommerce trust"),
        ("e‐commerce trust", "ecommerce trust"),
        ("e–commerce trust", "ecommerce trust"),
        ("Users' Satisfaction", "users satisfaction"),
        ("Users’ Satisfaction", "users satisfaction"),
        ("Self\u00adEfficacy", "selfefficacy"),
        # 其餘標點與符號換成空白。
        ("Trust/Distrust", "trust distrust"),
        ("Trust & Commitment", "trust commitment"),
        ("Trust: Vendor", "trust vendor"),
        ("Trust + Commitment", "trust commitment"),
        # 空白縮成一個、去頭尾。
        ("  perceived \t usefulness \n", "perceived usefulness"),
        # 不做字根還原。
        ("Trusting Beliefs", "trusting beliefs"),
        # 只有標點 → 空字串。
        ("--", ""),
        ("", ""),
    ],
)
def test_normalize_name_examples(name: str, expected: str) -> None:
    assert normalize_name(name) == expected


def test_same_construct_written_differently_normalizes_the_same() -> None:
    variants = [
        "Perceived Usefulness",
        "perceived usefulness",
        "Perceived Usefulness (PU)",
        "  PERCEIVED   USEFULNESS ",
        "Perceived-Usefulness",
    ]

    assert {normalize_name(name) for name in variants} == {
        "perceived usefulness",
        "perceivedusefulness",
    }
    assert normalize_name("Perceived-Usefulness") == "perceivedusefulness"


def test_different_concepts_stay_apart() -> None:
    assert normalize_name("Trust") != normalize_name("Trusting Beliefs")
    assert normalize_name("Perceived Risk") != normalize_name("Perceived Usefulness")


def test_non_string_is_refused() -> None:
    with pytest.raises(TypeError, match="只收字串"):
        normalize_name(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="只收字串"):
        normalize_name(b"trust")  # type: ignore[arg-type]


# utf-8 編得出來的字元（不含單獨的代理字元）。
_NAME_ALPHABET = st.characters(codec="utf-8")


# 括號裡的字：故意多放「算大寫、卻不是字母或數字」的字（🅰、Ⓐ、Ⅻ）與各種符號。
_PAREN_ALPHABET = st.one_of(
    st.sampled_from("🅰🅱ⒶⓐⅫⅰ\u00b2\u2070ﬁẞ'-.&/"),
    st.characters(codec="utf-8", exclude_characters="()"),
)


@settings(max_examples=500)
@given(
    st.lists(st.text(alphabet=_NAME_ALPHABET, max_size=6), max_size=3).map(" ".join),
    st.text(alphabet=_PAREN_ALPHABET, min_size=1, max_size=15),
)
@example("Trust", "🅰")
@example("Trust", "🅰B")
@example("", "Ⓐ")
def test_normalize_name_never_raises_on_parenthesised_text(before: str, inside: str) -> None:
    # Codex 審查：`Trust (🅰)` 原本丟 ValueError（候選字母是空的）。不可信的名稱只會被正規化。
    result = normalize_name(f"{before} ({inside})")

    assert normalize_name(result) == result


def test_uppercase_symbol_in_parentheses_is_treated_as_punctuation() -> None:
    # 🅰 是符號（Unicode So）：不是縮寫，照其他符號換成空白。
    assert normalize_name("Trust (🅰)") == "trust"


@settings(max_examples=500)
@given(st.text(alphabet=_NAME_ALPHABET, max_size=40))
@example("Perceived Usefulness (PU)")
@example("(A(B)C)")
@example("Ⅻ (ﬁ) Ｓ")
@example("x (ẞ)")
def test_normalize_name_is_idempotent(name: str) -> None:
    once = normalize_name(name)

    assert normalize_name(once) == once


@settings(max_examples=300)
@given(st.text(alphabet=_NAME_ALPHABET, max_size=40))
def test_normalized_name_has_no_case_or_outer_or_double_spaces(name: str) -> None:
    result = normalize_name(name)

    assert result == result.strip()
    assert "  " not in result
    assert result.casefold() == result


@pytest.mark.parametrize("name", ["ὐ", "Trust ὐ", "ὒ ΰ ᾈ", "ǅ ẞ ﬃ"])
def test_letters_that_nfkc_recomposes_stay_casefolded(name: str) -> None:
    # 確認審查：U+1F50（ὐ）casefold 會拆開、最後的 NFKC 又合回分大小寫的字，結果原本不是 casefold。
    result = normalize_name(name)

    assert result.casefold() == result
    assert normalize_name(result) == result
