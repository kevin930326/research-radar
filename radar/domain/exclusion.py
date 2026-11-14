"""排除規則：把非研究文章（編者的話、勘誤等）標記出來、不刪除（PRD FR-8、OQ-02）。

規則在 `config/exclusion_rules.yaml`；增減規則只改那個檔，之後跑 `radar reparse` 生效。
比對前標題與規則字串都做同一個正規化（`normalize_title`）；依檔案順序，第一條符合的勝出。
"""

import re
import unicodedata

from radar.domain.domain_config import ExclusionRulesConfig
from radar.domain.papers import ParsedPaper

# 單引號一類的彎引號（含 U+02BC 修飾字母撇號）都換成 ASCII 的 `'`。
_CURLY_QUOTES = str.maketrans(dict.fromkeys("‘’‚‛ʼ", "'"))
_WHITESPACE = re.compile(r"\s+")


def normalize_title(value: str) -> str:
    """比對用的標題：NFKC、彎引號換成 `'`、casefold、連續空白縮成一個、去頭尾空白。"""
    text = unicodedata.normalize("NFKC", value).translate(_CURLY_QUOTES).casefold()
    return _WHITESPACE.sub(" ", text).strip()


def exclusion_reason(paper: ParsedPaper, rules: ExclusionRulesConfig) -> str | None:
    """`paper` 符合的第一條規則的代碼；都不符合回 None。

    一條規則符合＝正規化後的標題等於 `title_equals` 的其中一個，或以 `title_starts_with` 的
    其中一個開頭（要在字的邊界：開頭那段之後的下一個字元不是字母或數字）。
    """
    title = normalize_title(paper.title)
    for rule in rules.rules:
        if any(title == normalize_title(value) for value in rule.title_equals):
            return rule.reason
        prefixes = rule.title_starts_with
        if any(_starts_with_word(title, normalize_title(value)) for value in prefixes):
            return rule.reason
    return None


def _starts_with_word(title: str, prefix: str) -> bool:
    """`title` 等於 `prefix`，或以它開頭且下一個字元不是字母或數字（`About Our Authorship`
    不算以 `About Our Authors` 開頭）。
    """
    if not title.startswith(prefix):
        return False
    rest = title[len(prefix) :]
    return not rest or not rest[0].isalnum()
