"""識別碼的正規化：全專案唯一的一份（AD-6、ADR-0020）。

三個函式都只收 `str`、回傳正規化後的 `str`，看不懂就丟 `InvalidIdentifierError`。
成功時做兩次等於做一次（`f(f(x)) == f(x)`，性質測試守著）。
"""

import re
import string
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import unquote_to_bytes

# 正規化後的識別碼最長幾個字元（DOI 沒有官方上限；超過這個長度幾乎一定是抓錯的資料）。
MAX_IDENTIFIER_LENGTH = 500


class IdentifierScheme(StrEnum):
    """`external_id.scheme` 的值。"""

    DOI = "doi"
    OPENALEX = "openalex"


class InvalidIdentifierError(ValueError):
    """看不懂的識別碼。訊息只帶（截短的）原值，不帶別的東西。"""

    def __init__(self, kind: str, value: str) -> None:
        shown = value if len(value) <= 80 else value[:80] + "…"
        super().__init__(f"看不懂的{kind}：{shown!r}")


# `doi:` 前綴（不分大小寫，冒號後可以有空白）。
_DOI_PREFIX = re.compile(r"doi:\s*", re.IGNORECASE)
# 可省略的 `http(s)://` ＋ 可省略的 `dx.`／`www.` ＋ `doi.org/`（不分大小寫）。
_DOI_URL = re.compile(r"(?:https?://)?(?:dx\.|www\.)?doi\.org/", re.IGNORECASE)
# 網址裡沒有編碼的 `?`、`#` 之後是查詢字串與錨點，不是 DOI 的一部分。
_URL_QUERY_OR_FRAGMENT = re.compile(r"[?#]")
# `10.<數字>(.<數字>)*/<至少一個字元>`；數字只認 ASCII 0–9。
_DOI_SHAPE = re.compile(r"10\.[0-9]+(?:\.[0-9]+)*/.+", re.DOTALL)
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"識別碼要是 str（現在是 {type(value).__name__}）")
    return value


def normalize_doi(value: str) -> str:
    """DOI → 小寫、沒有前綴的寫法，例如 `https://doi.org/10.1/ABC` → `10.1/abc`。

    1. 去頭尾空白。
    2. 去掉 `doi:` 前綴；或去掉 `https://doi.org/` 這類網址前綴（`http(s)://` 可省略），
       網址形式先切掉第一個沒有編碼的 `?` 或 `#` 之後的部分，再做 percent-decoding（`%2F` → `/`）；
       解出來不是 UTF-8、或原字串含編不成 UTF-8 的字元（單獨的代理字元）都算看不懂。
    3. 只把 ASCII 字母轉小寫（DOI 不分大小寫；非 ASCII 字元照原樣）。
    4. 結果要是 `10.<數字>(.<數字>)*/<至少一個字元>`、全部可印出、不含空白、最長
       `MAX_IDENTIFIER_LENGTH` 個字元。

    Raises:
        TypeError: 不是 `str`。
        InvalidIdentifierError: 看不懂。
    """
    text = _require_str(value).strip()
    if prefix := _DOI_PREFIX.match(text):
        text = text[prefix.end() :]
    elif url := _DOI_URL.match(text):
        try:
            path = _URL_QUERY_OR_FRAGMENT.split(text[url.end() :], maxsplit=1)[0]
            text = unquote_to_bytes(path).decode("utf-8")
        except UnicodeError:
            # 解出來不是 UTF-8（UnicodeDecodeError），或原字串本身就編不成 UTF-8、例如單獨的
            # 代理字元（unquote_to_bytes 先編碼時丟 UnicodeEncodeError）：都算看不懂。
            raise InvalidIdentifierError("DOI", value) from None
    text = text.translate(_ASCII_LOWER)
    if (
        not _DOI_SHAPE.fullmatch(text)
        or len(text) > MAX_IDENTIFIER_LENGTH
        or not text.isprintable()
        or any(character.isspace() for character in text)
    ):
        raise InvalidIdentifierError("DOI", value)
    return text


# 可省略的 `ISSN` 或 `ISSN-L` 前綴（不分大小寫），後面接空白或冒號。
_ISSN_PREFIX = re.compile(r"issn(?:-l)?(?:\s*:\s*|\s+)", re.IGNORECASE)
_ISSN_SHAPE = re.compile(r"([0-9]{4})-?([0-9]{3}[0-9Xx])")


def normalize_issn_l(value: str) -> str:
    """ISSN-L → `NNNN-NNNC`（`C` 是數字或大寫 `X`），例如 `ISSN: 0276778x` → `0276-778X`。

    去頭尾空白、可省略 `ISSN`／`ISSN-L` 前綴、有無連字號都收。只檢查格式，不驗檢查碼（ADR-0020）。

    Raises:
        TypeError: 不是 `str`。
        InvalidIdentifierError: 看不懂。
    """
    text = _require_str(value).strip()
    if prefix := _ISSN_PREFIX.match(text):
        text = text[prefix.end() :]
    match = _ISSN_SHAPE.fullmatch(text)
    if match is None:
        raise InvalidIdentifierError("ISSN-L", value)
    return f"{match[1]}-{match[2].upper()}"


# 可帶 `http(s)://(api.)openalex.org/` 與一段小寫的實體路徑（例如 `works/`）。
_OPENALEX_URL = re.compile(r"(?i:https?://(?:api\.)?openalex\.org/)(?:[a-z]+/)?")
_OPENALEX_SHAPE = re.compile(r"[A-Za-z][0-9]+")


def normalize_openalex_id(value: str) -> str:
    """OpenAlex ID → 大寫字母＋數字，例如 `https://openalex.org/works/w2741809807` → `W2741809807`。

    Raises:
        TypeError: 不是 `str`。
        InvalidIdentifierError: 看不懂。
    """
    text = _require_str(value).strip()
    if url := _OPENALEX_URL.match(text):
        text = text[url.end() :]
    if not _OPENALEX_SHAPE.fullmatch(text) or len(text) > MAX_IDENTIFIER_LENGTH:
        raise InvalidIdentifierError("OpenAlex ID", value)
    return text.upper()


_NORMALIZERS = {
    IdentifierScheme.DOI: normalize_doi,
    IdentifierScheme.OPENALEX: normalize_openalex_id,
}


@dataclass(frozen=True)
class ExternalId:
    """一個外部識別碼（`external_id` 的一列）。`value_norm` 必須已經正規化。"""

    scheme: IdentifierScheme
    value_norm: str

    def __post_init__(self) -> None:
        scheme = IdentifierScheme(self.scheme)
        object.__setattr__(self, "scheme", scheme)
        if _NORMALIZERS[scheme](self.value_norm) != self.value_norm:
            raise ValueError(f"{scheme} 識別碼還沒正規化：{self.value_norm!r}")
