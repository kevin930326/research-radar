"""構面名稱的正規化（ADR-0012 第 1、2 點、AD-16、Story 2.6）。

`normalize_name` 的結果是 `construct.normalized_name`（唯一鍵）：正規化後相同的名稱從一開始就是
同一個構面，不需要人審。步驟固定：

1. NFKC（全形換半形、相容字換成一般寫法）。
2. 拿掉縮寫的括號：括號裡 1～15 個字元、沒有空白、至少一個大寫字母，**而且是前面幾個詞的首字母**
   （不分大小寫；字尾可以多一個小寫 s；可以跳過 and、of 這類虛詞），例如
   `Perceived Usefulness (PU)`、`Electronic Word-of-Mouth (eWOM)`、
   `Information and Communication Technologies (ICTs)`。
   不是首字母的括號（`Trust (Vendor)`、`Adoption (B2B)`、`Intention (UTAUT2)`）、全小寫的括號
   （`(perceived)`）照一般標點處理、內容留著：留著頂多多一列構面（Epic 10 再合併），拿掉卻可能把
   不同的構面併成一列、拆不回來。
3. casefold（比 lower 更徹底的不分大小寫）。
4. 拿掉撇號與各種連字號（`'`、`’`、`‘`、`ʼ`、Unicode Pd 類、減號、軟連字號）：`e-commerce` 與
   `ecommerce`、`users'` 與 `users` 一樣。
5. 其餘標點與符號（Unicode P*、S* 類）換成空白。
6. 空白縮成一個、去頭尾。
7. 再做一次 NFKC。

**不做字根還原**（`trusting` 不會變成 `trust`）：會把不同的概念誤併，而且拆不回來（ADR-0012）。
"""

import re
import unicodedata
from typing import Final

# 可能是縮寫的括號：1～15 個非空白、非括號字元（NFKC 之後、casefold 之前找）；是不是縮寫另外看
# 有沒有大寫字母、是不是前面幾個詞的首字母（`_is_abbreviation_of`）。
_PARENTHESES: Final = re.compile(r"\(([^()\s]{1,15})\)")
# 前文裡的詞：連續的字母或數字（連字號、撇號、空白、標點都算分隔）。
_WORDS: Final = re.compile(r"[^\W_]+")
# 首字母縮寫常跳過的虛詞（`Information and Communication Technology (ICT)`）。
_SKIPPABLE: Final = frozenset({"a", "an", "and", "for", "in", "of", "on", "the", "to", "with"})
# 撇號（直的、彎的、修飾字母撇號）、減號、軟連字號；連字號另外用 Unicode 分類 Pd 判斷。
_APOSTROPHES_AND_HYPHENS: Final = frozenset(
    {"'", chr(0x2018), chr(0x2019), chr(0x02BC), chr(0x2212), chr(0x00AD)}
)
_WHITESPACE: Final = re.compile(r"\s+")


def _fold(char: str) -> str:
    """第 4、5 步：撇號與連字號拿掉，其餘標點與符號換成空白。"""
    if char in _APOSTROPHES_AND_HYPHENS:
        return ""
    category = unicodedata.category(char)
    if category == "Pd":
        return ""
    if category[0] in ("P", "S"):
        return " "
    return char


def _is_abbreviation_of(inside: str, before: str) -> bool:
    """括號裡的 `inside` 是 `before` 最後幾個詞的首字母（不分大小寫；字尾可以多一個小寫 s；
    可以跳過虛詞）。"""
    if not any(char.isupper() for char in inside):
        return False
    letters = [char for char in inside if char.isalnum()]
    if not letters:  # 例如 `(🅰)`：算大寫、卻不是字母或數字（Codex 審查）
        return False
    words = _WORDS.findall(before)
    content_words = [word for word in words if word.casefold() not in _SKIPPABLE]
    candidates = [letters]
    if len(letters) > 1 and inside.endswith("s"):
        candidates.append(letters[:-1])
    return any(
        len(candidate) <= len(pool)
        and all(
            word[0].casefold() == char.casefold()
            for word, char in zip(pool[-len(candidate) :], candidate, strict=True)
        )
        for candidate in candidates
        for pool in (words, content_words)
    )


def _drop_abbreviations(text: str) -> str:
    """第 2 步：前面幾個詞的首字母縮寫括號換成空白，其他括號原樣留著。"""
    return _PARENTHESES.sub(
        lambda match: (
            " " if _is_abbreviation_of(match.group(1), text[: match.start()]) else match.group(0)
        ),
        text,
    )


def normalize_name(name: str) -> str:
    """構面名稱的正規化（步驟見模組說明）。結果可能是空字串（例如名稱只有標點）。

    Raises:
        TypeError: `name` 不是字串。
    """
    if not isinstance(name, str):
        raise TypeError("normalize_name 只收字串")
    text = unicodedata.normalize("NFKC", name)
    text = _drop_abbreviations(text)
    text = text.casefold()
    text = "".join(_fold(char) for char in text)
    text = _WHITESPACE.sub(" ", text).strip()
    return unicodedata.normalize("NFKC", text)
