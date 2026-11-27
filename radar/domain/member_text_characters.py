"""成員在一行輸入框寫的字（修正分類的理由、回報中文重點的說明）共用的「不收的字元」
（ADR-0063、ADR-0064）。

- 不收的字元類別：控制字元（Cc）、落單的代理字元（Cs）、行分隔符 U+2028（Zl）、段落分隔符
  U+2029（Zp）、格式字元（Cf：零寬空白 U+200B、BOM U+FEFF、雙向覆寫 U+202A～U+202E、
  U+2066～U+2069 等，看不見卻會改變顯示或比對）。
- 放行：零寬連接 U+200D（組合 emoji 要用它，它本身不改變文字方向）。

存不進資料庫的字（`check_storable_text`）、字數上限與「去掉空白後是空的」由各自的模組判斷。
LLM 輸出的檢查（`radar/domain/validators/text_checks.py` 的 `BIDI_OR_TAG` 等）是另一套規則，管的是
模型寫的字，不在這裡。
"""

import unicodedata
from typing import Final

REJECTED_CATEGORIES: Final = frozenset({"Cc", "Cs", "Zl", "Zp", "Cf"})
ZERO_WIDTH_JOINER: Final = "\u200d"
ALLOWED_FORMAT_CHARACTERS: Final = frozenset({ZERO_WIDTH_JOINER})


def is_rejected_character(char: str) -> bool:
    """`char`（一個字元）是不是一行輸入不收的字元（見模組說明）。"""
    if char in ALLOWED_FORMAT_CHARACTERS:
        return False
    return unicodedata.category(char) in REJECTED_CATEGORIES
