"""識別碼正規化的性質測試（AD-6、ADR-0020）。

對三個函式都測：
- 冪等：任意輸入只要成功，`f(f(x)) == f(x)`
  （任意文字，加上「已知前綴＋任意文字」與造出來的合法寫法）。
- 同一個識別碼的各種寫法（大小寫、前綴、網址、連字號、頭尾空白）正規化後相同。
- 輸出的形狀不變。
- 任意輸入只會成功或丟 `InvalidIdentifierError`，不會丟別的例外。
"""

import contextlib
import re
import string
from collections.abc import Callable
from urllib.parse import quote

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from radar.domain.identifiers import (
    MAX_IDENTIFIER_LENGTH,
    ExternalId,
    IdentifierScheme,
    InvalidIdentifierError,
    normalize_doi,
    normalize_issn_l,
    normalize_openalex_id,
)

PROPERTY = settings(max_examples=300, deadline=None)

DOI_SHAPE = re.compile(r"10\.[0-9]+(?:\.[0-9]+)*/\S+")
ISSN_SHAPE = re.compile(r"[0-9]{4}-[0-9]{3}[0-9X]")
OPENALEX_SHAPE = re.compile(r"[A-Z][0-9]+")

# 可印出、不是空白的字元（`str.isprintable()` 為 False 的類別都排除；Zs 是各種空白）。
PRINTABLE_NON_SPACE = st.characters(
    exclude_categories=("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs")
)
WHITESPACE = st.text(alphabet=" \t\n\r　 ", max_size=3)

DOI_PREFIXES = (
    "doi:",
    "DOI:",
    "doi: ",
    "Doi:\t",
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://www.doi.org/",
    "HTTPS://DOI.ORG/",
    "doi.org/",
    "DX.DOI.ORG/",
)
ISSN_PREFIXES = ("", "ISSN ", "issn ", "ISSN:", "ISSN: ", "Issn :", "ISSN-L ", "issn-l: ")
# 網址後面可能帶的查詢字串與錨點（貼上的連結常有），正規化時要切掉。
URL_TAILS = ("", "?utm_source=x", "#section", "?a=1&b=2#c", "?", "#")
OPENALEX_PREFIXES = (
    "",
    "https://openalex.org/",
    "http://openalex.org/works/",
    "https://api.openalex.org/works/",
    "HTTPS://API.OPENALEX.ORG/authors/",
)

DIGITS = st.text(alphabet=string.digits, min_size=1, max_size=6)


@st.composite
def canonical_dois(draw: st.DrawFn) -> str:
    """正規化後的 DOI：`10.<數字>(.<數字>)*/<可印出、非空白>`，ASCII 字母全小寫。"""
    registrant = ".".join(draw(st.lists(DIGITS, min_size=1, max_size=3)))
    suffix = draw(st.text(alphabet=PRINTABLE_NON_SPACE, min_size=1, max_size=40))
    return f"10.{registrant}/{suffix}".translate(
        str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
    )


def _flip_ascii_case(draw: st.DrawFn, text: str) -> str:
    flags = draw(st.lists(st.booleans(), min_size=len(text), max_size=len(text)))
    return "".join(
        character.upper() if flag and character in string.ascii_lowercase else character
        for character, flag in zip(text, flags, strict=True)
    )


@st.composite
def doi_spellings(draw: st.DrawFn) -> tuple[str, str]:
    """（正規化後的 DOI, 同一個 DOI 的另一種寫法）。"""
    canonical = draw(canonical_dois())
    spelled = _flip_ascii_case(draw, canonical)
    prefix = draw(st.sampled_from(("", *DOI_PREFIXES)))
    if "doi.org/" in prefix.lower():
        # 網址形式會切掉 `?`、`#` 之後的部分再做 percent-decoding，所以 DOI 本身先編碼
        # （`safe` 裡不放 `?`、`#`、`%`，它們一定編成 `%3F`、`%23`、`%25`），再接上查詢字串或錨點。
        safe = draw(st.sampled_from(("", "/", "/:;()<>")))
        spelled = quote(spelled, safe=safe) + draw(st.sampled_from(URL_TAILS))
    return canonical, draw(WHITESPACE) + prefix + spelled + draw(WHITESPACE)


@st.composite
def issn_spellings(draw: st.DrawFn) -> tuple[str, str]:
    digits = draw(st.text(alphabet=string.digits, min_size=7, max_size=7))
    check = draw(st.sampled_from([*string.digits, "X", "x"]))
    canonical = f"{digits[:4]}-{digits[4:]}{check.upper()}"
    hyphen = draw(st.sampled_from(("", "-")))
    prefix = draw(st.sampled_from(ISSN_PREFIXES))
    spelled = f"{prefix}{digits[:4]}{hyphen}{digits[4:]}{check}"
    return canonical, draw(WHITESPACE) + spelled + draw(WHITESPACE)


@st.composite
def openalex_spellings(draw: st.DrawFn) -> tuple[str, str]:
    letter = draw(st.sampled_from(string.ascii_uppercase))
    digits = draw(st.text(alphabet=string.digits, min_size=1, max_size=12))
    spelled_letter = draw(st.sampled_from((letter, letter.lower())))
    prefix = draw(st.sampled_from(OPENALEX_PREFIXES))
    return f"{letter}{digits}", draw(WHITESPACE) + prefix + spelled_letter + digits + draw(
        WHITESPACE
    )


def _arbitrary(
    prefixes: tuple[str, ...], spellings: st.SearchStrategy[tuple[str, str]]
) -> st.SearchStrategy[str]:
    """任意文字、已知前綴接任意文字、合法寫法：都拿來試冪等與「只會丟 InvalidIdentifierError」。"""
    return st.one_of(
        st.text(),
        st.builds(lambda prefix, rest: prefix + rest, st.sampled_from(prefixes), st.text()),
        spellings.map(lambda pair: pair[1]),
    )


ARBITRARY_DOI = _arbitrary(
    (*DOI_PREFIXES, "10.", "10.1/", "https://doi.org/10.1/"), doi_spellings()
)
ARBITRARY_ISSN = _arbitrary(("ISSN ", "ISSN-L ", "1234", "1234-"), issn_spellings())
ARBITRARY_OPENALEX = _arbitrary((*OPENALEX_PREFIXES, "W", "w"), openalex_spellings())


def _check_idempotent(normalize: Callable[[str], str], value: str, shape: re.Pattern[str]) -> None:
    try:
        once = normalize(value)
    except InvalidIdentifierError:
        return
    assert normalize(once) == once
    assert shape.fullmatch(once), once


# --- DOI ----------------------------------------------------------------------------------


@PROPERTY
@given(ARBITRARY_DOI)
@example("10.1/ABC")
@example("https://doi.org/10.1/%2541")
@example("https://doi.org/10.1/a%3Fb?x=1")
@example("doi:10.1/%41")
def test_doi_normalize_is_idempotent(value: str) -> None:
    _check_idempotent(normalize_doi, value, DOI_SHAPE)


@PROPERTY
@given(doi_spellings())
def test_doi_spellings_normalize_to_same(pair: tuple[str, str]) -> None:
    canonical, spelled = pair
    assert normalize_doi(spelled) == canonical


@PROPERTY
@given(ARBITRARY_DOI)
def test_doi_output_shape(value: str) -> None:
    try:
        result = normalize_doi(value)
    except InvalidIdentifierError:
        return
    assert DOI_SHAPE.fullmatch(result)
    assert len(result) <= MAX_IDENTIFIER_LENGTH
    assert result.isprintable()
    assert not any(character in string.ascii_uppercase for character in result)


@pytest.mark.parametrize("value", ["10.1/ABC", "https://doi.org/10.1/abc", "doi:10.1/abc"])
def test_doi_spec_examples_are_the_same(value: str) -> None:
    assert normalize_doi(value) == "10.1/abc"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  DOI: 10.17705/1JAIS.00623 ", "10.17705/1jais.00623"),
        ("http://dx.doi.org/10.1/a%2Fb", "10.1/a/b"),
        ("https://www.doi.org/10.1.2/x", "10.1.2/x"),
        # 貼上的連結帶查詢字串或錨點：切掉；編碼過的 `?`、`#` 是 DOI 的一部分。
        ("https://doi.org/10.1/abc?utm_source=x", "10.1/abc"),
        ("https://doi.org/10.1/abc#top", "10.1/abc"),
        ("https://doi.org/10.1/a%3Fb%23c", "10.1/a?b#c"),
        # 沒有 http(s):// 的網址形式。
        ("doi.org/10.1/ABC", "10.1/abc"),
        ("www.doi.org/10.1/abc", "10.1/abc"),
        # 非 ASCII 字母不轉小寫；網址以外的 `%` 照原樣。
        ("10.1/ÄB%2F", "10.1/Äb%2f"),
    ],
)
def test_doi_examples(value: str, expected: str) -> None:
    assert normalize_doi(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "abc",
        "doi:",
        "https://doi.org/",
        "https://doi.org/?10.1/abc",
        "10.1/",
        "10./abc",
        "11.1/abc",
        "10.1/a b",
        "10.1/a​b",
        "https://doi.org/10.1/%ff",
        "doi:doi:10.1/abc",
        "10.１/abc",  # 全形數字不算
        "10.1/" + "a" * MAX_IDENTIFIER_LENGTH,
    ],
)
def test_doi_rejects_unreadable(value: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        normalize_doi(value)


@PROPERTY
@given(ARBITRARY_DOI)
def test_doi_raises_only_invalid_identifier_error(value: str) -> None:
    # 只吞文件寫明的例外；其他例外讓測試失敗。
    with contextlib.suppress(InvalidIdentifierError):
        normalize_doi(value)


# --- ISSN-L -------------------------------------------------------------------------------


@PROPERTY
@given(ARBITRARY_ISSN)
@example("0276-778x")
def test_issn_normalize_is_idempotent(value: str) -> None:
    _check_idempotent(normalize_issn_l, value, ISSN_SHAPE)


@PROPERTY
@given(issn_spellings())
def test_issn_spellings_normalize_to_same(pair: tuple[str, str]) -> None:
    canonical, spelled = pair
    assert normalize_issn_l(spelled) == canonical


@PROPERTY
@given(ARBITRARY_ISSN)
def test_issn_raises_only_invalid_identifier_error(value: str) -> None:
    # 只吞文件寫明的例外；其他例外讓測試失敗。
    with contextlib.suppress(InvalidIdentifierError):
        normalize_issn_l(value)


@pytest.mark.parametrize(
    "value",
    ["", "0276-778", "0276-77830", "0276_7783", "ISSN", "ISSN0276-7783", "X276-7783", "0276-778Y"],
)
def test_issn_rejects_unreadable(value: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        normalize_issn_l(value)


@pytest.mark.parametrize("value", ["ISSN-L 0276-7783", "issn-l:02767783", "ISSN-L: 0276-7783"])
def test_issn_l_prefix_is_accepted(value: str) -> None:
    assert normalize_issn_l(value) == "0276-7783"


def test_issn_does_not_verify_check_digit() -> None:
    # 0276-7783 的正確檢查碼是 3；格式對就收（ADR-0020）。
    assert normalize_issn_l("0276-7784") == "0276-7784"


# --- OpenAlex ID --------------------------------------------------------------------------


@PROPERTY
@given(ARBITRARY_OPENALEX)
@example("https://openalex.org/works/w2741809807")
def test_openalex_normalize_is_idempotent(value: str) -> None:
    _check_idempotent(normalize_openalex_id, value, OPENALEX_SHAPE)


@PROPERTY
@given(openalex_spellings())
def test_openalex_spellings_normalize_to_same(pair: tuple[str, str]) -> None:
    canonical, spelled = pair
    assert normalize_openalex_id(spelled) == canonical


@PROPERTY
@given(ARBITRARY_OPENALEX)
def test_openalex_raises_only_invalid_identifier_error(value: str) -> None:
    # 只吞文件寫明的例外；其他例外讓測試失敗。
    with contextlib.suppress(InvalidIdentifierError):
        normalize_openalex_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "W",
        "2741809807",
        "WW1",
        "W12a",
        "https://openalex.org/",
        "https://openalex.org/WORKS/W1",
        "https://example.org/W1",
    ],
)
def test_openalex_rejects_unreadable(value: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        normalize_openalex_id(value)


# --- 共通 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("normalize", [normalize_doi, normalize_issn_l, normalize_openalex_id])
def test_non_str_is_type_error(normalize: Callable[[object], str]) -> None:
    with pytest.raises(TypeError):
        normalize(b"10.1/abc")


def test_invalid_identifier_error_is_value_error() -> None:
    assert issubclass(InvalidIdentifierError, ValueError)


def test_external_id_requires_normalized_value() -> None:
    assert ExternalId(IdentifierScheme.DOI, "10.1/abc").value_norm == "10.1/abc"
    with pytest.raises(ValueError, match="正規化"):
        ExternalId(IdentifierScheme.DOI, "10.1/ABC")
    with pytest.raises(ValueError, match="正規化"):
        ExternalId(IdentifierScheme.OPENALEX, "w1")
