"""中文重點的正規化與 domain 驗證（PRD FR-14、NFR-4、Story 2.4、ADR-0032）。

模型輸出是不可信的輸入：Pydantic（`ZhSummaryOutput`）只管形狀，這裡管內容。

**正規化**（`normalize_zh_summary`，`services/ai_results.py` 寫入前也用同一個函式）：每欄 NFC、
連續空白縮成一個、去頭尾；結果是空的，或整欄只是「摘要未提及／未提及／無／N/A」這類字，存 NULL。

**拒收規則**（`validate_zh_summary`），依下面的順序檢查，第一個不過的就是原因代碼：

1. 存不進資料庫（NUL、單獨的代理字元）→ `llm_output_unstorable`。
2. 夾標記或連結：標籤樣的 `<`、欄位開頭的 `>`、反引號、`](`、網址 → `zh_summary_unsafe_text`。
3. 夾「摘要未提及…」這類講摘要本身的句子 → `zh_summary_placeholder_text`。
4. 任一欄正規化後的字元數超過 `max_chars_per_field` → `zh_summary_field_too_long`。
5. 不是繁中：沒有漢字、漢字比例低於 `min_cjk_percent`、或含簡體專用字 → `zh_summary_not_zh_hant`。
6. 逐句翻譯：摘要句數 ≥ `translation_min_abstract_sentences`，而且輸出句數 ≥ 摘要句數
   → `zh_summary_sentence_translation`。
7. 編造：有輸入（標題＋摘要）沒有的阿拉伯數字或英文詞 → `zh_summary_unsupported_content`。

判準的細節與抓不到的情況寫在 ADR-0032。
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from radar.domain.ai_outputs import (
    DocumentType,
    LlmInput,
    OutputRejectedError,
    ZhSummaryContent,
    check_output_storable,
    unwrap_document,
)
from radar.domain.errors import ErrorCode
from radar.domain.llm_schemas import ZhSummaryOutput

FIELDS: Final = ("research_question", "method", "theory", "findings")


class ZhSummaryRules(BaseModel):
    """`config/zh_summary.yaml` 的門檻（只經 `radar/config.py` 讀）。三個鍵都要寫，只收整數。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 每欄正規化後最多幾個字元（PRD FR-14：預設 80）。
    max_chars_per_field: int = Field(strict=True, ge=1)
    # 摘要至少幾句才檢查逐句翻譯；1 句的摘要任何輸出都會被當成翻譯，所以最少 2。
    translation_min_abstract_sentences: int = Field(strict=True, ge=2)
    # 每欄漢字至少佔（漢字＋英文詞）的百分之幾。
    min_cjk_percent: int = Field(strict=True, ge=1, le=100)


# --- 正規化 -------------------------------------------------------------------------------

_WHITESPACE: Final = re.compile(r"\s+")
# 比對「整欄只是佔位字」時先拿掉的字元：空白、標點、底線（`\W` 不含漢字、字母、數字）。
_NOT_WORD: Final = re.compile(r"[\W_]+")
# 「講摘要本身」的主詞：摘要、原文、文中一定是在講摘要的文字；本文、本研究、研究、作者、文章、
# 論文也常是真的研究結果的開頭（「本文發現…」），所以只有否定詞緊接在後面才算（見下面的
# `_EMBEDDED_PLACEHOLDER`）。整欄比對與夾帶比對用同一份主詞清單。
_ABSTRACT_SUBJECTS: Final = r"(?:摘要|原文|文中)"
_PAPER_SUBJECTS: Final = r"(?:本文|本研究|研究|作者|文章|論文)"
_NEGATION: Final = r"(?:中|裡|內)?(?:並|均|皆|也)?(?:未|沒有|沒|無|不)(?:明確|特別)?"
_META_VERBS: Final = r"(?:提及|提到|說明|描述|指出|交代|報告|載明|詳述|揭露|涉及)"
_PLACEHOLDER_ZH: Final = re.compile(
    rf"(?:{_ABSTRACT_SUBJECTS}|{_PAPER_SUBJECTS})?{_NEGATION}"
    rf"{_META_VERBS}?(?:任何)?(?:相關)?(?:使用|採用)?(?:的)?"
    r"(?:理論|理論框架|方法|研究方法|研究問題|問題|發現|結果|資訊|資料|內容)?"
    r"|不詳|未知|不明|從缺|不適用"
)
_PLACEHOLDER_EN: Final = re.compile(
    r"na|none|null|nil|unknown|notapplicable"
    r"|not(?:mentioned|specified|reported|stated|available|provided|given)(?:intheabstract|inabstract)?"
)


def normalize_text(value: str) -> str:
    """NFC、連續空白（含全形空白、換行）縮成一個半形空白、去頭尾。"""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", value)).strip()


def is_placeholder(text: str) -> bool:
    """整欄只是「摘要未提及／未提及／無／N/A」這類字（拿掉空白與標點、不分大小寫後比對）；
    拿掉之後什麼都不剩（例如只有「—」）也算。"""
    key = _NOT_WORD.sub("", text).casefold()
    return (
        not key
        or _PLACEHOLDER_ZH.fullmatch(key) is not None
        or _PLACEHOLDER_EN.fullmatch(key) is not None
    )


def _normalize_field(value: str | None) -> str | None:
    if value is None:
        return None
    text = normalize_text(value)
    if not text or is_placeholder(text):
        return None
    return text


def normalize_zh_summary(output: ZhSummaryOutput) -> ZhSummaryContent:
    """四欄正規化成要存的內容：沒提到（null、空白、只有佔位字）的欄位是 None。

    不做拒收判斷；要存的輸出一定先經過 `validate_zh_summary`。

    Raises:
        TypeError: `output` 不是 `ZhSummaryOutput`。
        ValueError: 有欄位存不進資料庫（先經 `validate_zh_summary` 就不會發生）。
    """
    if not isinstance(output, ZhSummaryOutput):
        raise TypeError("normalize_zh_summary 只收 ZhSummaryOutput")
    return ZhSummaryContent(
        research_question=_normalize_field(output.research_question),
        method=_normalize_field(output.method),
        theory=_normalize_field(output.theory),
        findings=_normalize_field(output.findings),
    )


# --- 判準 ---------------------------------------------------------------------------------

# 標記與連結：`<` 只在後面接標籤樣的字元（`<b>`、`</div`、`<!--`、`<?`）時算，`p < .05` 這種統計
# 寫法不算；`>` 只在欄位開頭算（Markdown 的引言）。漢字在 Python 的正規式裡也是 `\w`，所以
# 「見https://…」這種黏在漢字後面的寫法不能靠 `\b`，改用不接英文字母的前後文判斷。
_UNSAFE: Final = re.compile(
    r"<(?=[/!?A-Za-z])|^>|`|\]\(|://|(?<![A-Za-z0-9])www\.|(?<![A-Za-z])(?:mailto|javascript):",
    re.IGNORECASE,
)
# 欄位裡夾的「講摘要本身」的句子。摘要、原文、文中後面 8 個字內接否定詞、再 4 個字內接「提及、
# 說明…」就算；本文、本研究、研究、作者、文章、論文要否定詞緊接在後面（只能隔「中、並、也、明確」
# 這類字）才算，「本文探討使用者為何不揭露個人資訊」不算。只寫「未提及」也不算（「受訪者未提及
# 隱私疑慮」可能是真的發現）。
_EMBEDDED_PLACEHOLDER: Final = re.compile(
    rf"{_ABSTRACT_SUBJECTS}[^。！？!?；;]{{0,8}}?(?:未|沒有|沒|無|並未|不)[^。！？!?；;]{{0,4}}?"
    rf"{_META_VERBS}"
    rf"|{_PAPER_SUBJECTS}{_NEGATION}{_META_VERBS}"
    r"|(?:未|沒有|沒|並未)(?:在|於)(?:摘要|原文|文中)"
    r"|(?<![A-Za-z])not\s+(?:mentioned|specified|reported|stated)(?![A-Za-z])"
    r"|(?<![A-Za-z])n/a(?![A-Za-z])",
    re.IGNORECASE,
)
# 只出現在簡體的常用字（每個都不在 Big5／CP950 裡，單元測試核對）。
SIMPLIFIED_ONLY: Final = frozenset(
    "这们过为发现说时会对进关个实动将应经结论数题问学业产务资统释验证响显级际观处设计认识变让还"
    "从样点间长无电脑条绩组织员参测试调术运营销费购买卖东车门见简语读写给该没决质较选择专环随态势"
    "历众虑"
)
_ENGLISH_WORD: Final = re.compile(r"[A-Za-z]+")
_ENGLISH_TOKEN: Final = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")
# 阿拉伯數字：千分位（1,200）合併；小數點後的位數照寫；APA 寫法的 .05 是 0.05。
_NUMBER: Final = re.compile(
    r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?|(?<![0-9])\.[0-9]+"
)
_ABSTRACT_SENTENCE_END: Final = re.compile(r"[.!?]+\s+")
_OUTPUT_SENTENCE_END: Final = re.compile(r"[。！？!?]+")
_HAS_WORD: Final = re.compile(r"\w")
# 首字母縮寫可以跳過的虛詞（例如 Theory of Planned Behavior → TPB）。
_FUNCTION_WORDS: Final = frozenset(
    {"of", "and", "the", "for", "in", "on", "to", "a", "an", "with", "by", "at", "from", "or"}
)
_UNIT_NAMES: Final = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen"
    " fifteen sixteen seventeen eighteen nineteen"
)
_UNIT_WORDS: Final[Mapping[str, int]] = {
    word: value for value, word in enumerate(_UNIT_NAMES.split())
}
_TENS_WORDS: Final[Mapping[str, int]] = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_TENS_VALUES: Final = frozenset(_TENS_WORDS.values())
_ORDINAL_NAMES: Final = "first second third fourth fifth sixth seventh eighth ninth tenth"
_ORDINAL_WORDS: Final[Mapping[str, int]] = {
    word: value for value, word in enumerate(_ORDINAL_NAMES.split(), start=1)
}
_SCALE_WORDS: Final[Mapping[str, int]] = {"hundred": 100, "thousand": 1000}


def _is_han(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x323AF
    )


def _nfkc(text: str) -> str:
    """比對數字與英文用：全形數字、字母換成半形。"""
    return unicodedata.normalize("NFKC", text)


def _input_texts(llm_input: LlmInput) -> tuple[str, str | None]:
    """（標題＋摘要合在一起的文字, 摘要；沒有摘要時是 None），已拿掉 `<document>` 包裝。"""
    parts: list[str] = []
    abstract: str | None = None
    for block in llm_input.blocks:
        text = unwrap_document(block)
        parts.append(text)
        if block.document_type is DocumentType.ABSTRACT:
            abstract = text
    return "\n".join(parts), abstract


def _numbers(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    # NFKC 會把全形逗號「，」變成「,」，「120，350 名」就被當成千分位的 120350；先換成頓號。
    for match in _NUMBER.finditer(_nfkc(text.replace("，", "、"))):
        try:
            found.add(Decimal(match.group(0).replace(",", "")).normalize())
        except InvalidOperation:  # pragma: no cover - 正規式只收得到合法的數字
            continue
    return found


def _number_word_values(words: list[str]) -> set[Decimal]:
    """輸入裡的英文數字詞換成數字：zero～nineteen、twenty～ninety、first～tenth、hundred、
    thousand；「twenty five」→ 25、「three hundred」→ 300、「two thousand」→ 2000。"""
    values: set[int] = set()
    previous: int | None = None
    for word in words:
        current: int | None = None
        if word in _UNIT_WORDS:
            current = _UNIT_WORDS[word]
            if previous is not None and previous in _TENS_VALUES and 0 < current < 10:
                values.add(current)
                current = previous + current
        elif word in _TENS_WORDS:
            current = _TENS_WORDS[word]
        elif word in _ORDINAL_WORDS:
            current = _ORDINAL_WORDS[word]
        elif word in _SCALE_WORDS:
            scale = _SCALE_WORDS[word]
            values.add(scale)
            if previous is not None and 0 < previous < 100:
                current = previous * scale
        if current is not None:
            values.add(current)
        previous = current
    return {Decimal(value) for value in values}


def _variants(word: str) -> set[str]:
    """容許複數字尾的差異：teams ↔ team、boxes ↔ box、studies ↔ study。"""
    found = {word}
    if word.endswith("ies") and len(word) > 3:
        found.add(word[:-3] + "y")
    if word.endswith("es") and len(word) > 2:
        found.add(word[:-2])
    if word.endswith("s") and len(word) > 1:
        found.add(word[:-1])
    return found


@dataclass(frozen=True)
class _InputVocabulary:
    """輸入（標題＋摘要）裡可以拿來比對的東西。"""

    numbers: frozenset[Decimal]
    words: frozenset[str]
    initials: tuple[str, ...]

    @classmethod
    def build(cls, text: str) -> _InputVocabulary:
        words = [word.lower() for word in _ENGLISH_WORD.findall(_nfkc(text))]
        variants: set[str] = set()
        for word in words:
            variants |= _variants(word)
        content = [word for word in words if word not in _FUNCTION_WORDS]
        return cls(
            numbers=frozenset(_numbers(text) | _number_word_values(words)),
            words=frozenset(variants),
            initials=(
                "".join(word[0] for word in words),
                "".join(word[0] for word in content),
            ),
        )

    def knows_word(self, word: str) -> bool:
        """這個英文詞（2 個字母以上）在輸入出現過，或是輸入裡連續幾個詞的首字母。"""
        lowered = word.lower()
        if _variants(lowered) & self.words:
            return True
        return any(lowered in initials for initials in self.initials)

    def knows_token(self, token: str) -> bool:
        """`PLS-SEM` 這種連字號串：每一段都認得，或整串去掉連字號是首字母縮寫。"""
        parts = [part for part in token.split("-") if len(part) >= 2]
        if not parts:
            return True
        if all(self.knows_word(part) for part in parts):
            return True
        joined = token.replace("-", "").lower()
        return any(joined in initials for initials in self.initials)


def _reject(code: ErrorCode, detail: str) -> OutputRejectedError:
    return OutputRejectedError(code, detail)


def _check_not_zh_hant(name: str, text: str, rules: ZhSummaryRules) -> None:
    folded = _nfkc(text)
    han = sum(1 for char in folded if _is_han(char))
    words = len(_ENGLISH_WORD.findall(folded))
    if han == 0:
        raise _reject(ErrorCode.ZH_SUMMARY_NOT_ZH_HANT, f"{name} 沒有漢字")
    if han * 100 < rules.min_cjk_percent * (han + words):
        raise _reject(ErrorCode.ZH_SUMMARY_NOT_ZH_HANT, f"{name} 的漢字比例太低")
    if any(char in SIMPLIFIED_ONLY for char in text):
        raise _reject(ErrorCode.ZH_SUMMARY_NOT_ZH_HANT, f"{name} 有簡體專用字")


def count_abstract_sentences(abstract: str) -> int:
    """摘要的句數：以 `.`、`!`、`?` 後面接空白切開，數非空的段落。"""
    return sum(1 for part in _ABSTRACT_SENTENCE_END.split(abstract.strip()) if part.strip())


def count_output_sentences(texts: Iterable[str]) -> int:
    """輸出的句數：每欄以 `。！？!?` 切開、數有字的段落；每個非空欄位至少算一句。"""
    total = 0
    for text in texts:
        parts = [part for part in _OUTPUT_SENTENCE_END.split(text) if _HAS_WORD.search(part)]
        total += max(1, len(parts))
    return total


def validate_zh_summary(
    output: BaseModel | str, llm_input: LlmInput, rules: ZhSummaryRules
) -> ZhSummaryContent:
    """驗證中文重點（規則與順序見模組說明），通過時回正規化後要存的內容。

    Raises:
        OutputRejectedError: 不通過；`code` 是原因代碼，訊息只有欄位名稱與規則，不含輸出內容。
        TypeError: `output` 不是 `ZhSummaryOutput`（prompt 的 schema 設錯，是程式的錯）。
    """
    if not isinstance(output, ZhSummaryOutput):
        raise TypeError("中文重點的驗證只收 ZhSummaryOutput")
    check_output_storable(output.model_dump(mode="json"))

    raw = {name: getattr(output, name) for name in FIELDS}
    for name, value in raw.items():
        if value is not None and _UNSAFE.search(normalize_text(value)):
            raise _reject(ErrorCode.ZH_SUMMARY_UNSAFE_TEXT, f"{name} 含標記或連結")

    content = normalize_zh_summary(output)
    present = {name: value for name, value in content.fields().items() if value is not None}

    for name, text in present.items():
        if _EMBEDDED_PLACEHOLDER.search(text):
            raise _reject(ErrorCode.ZH_SUMMARY_PLACEHOLDER_TEXT, f"{name} 夾了講摘要本身的句子")
    for name, text in present.items():
        if len(text) > rules.max_chars_per_field:
            raise _reject(
                ErrorCode.ZH_SUMMARY_FIELD_TOO_LONG,
                f"{name} 有 {len(text)} 字，上限 {rules.max_chars_per_field}",
            )
    for name, text in present.items():
        _check_not_zh_hant(name, text, rules)

    input_text, abstract = _input_texts(llm_input)
    if abstract is not None:
        abstract_sentences = count_abstract_sentences(abstract)
        if (
            abstract_sentences >= rules.translation_min_abstract_sentences
            and count_output_sentences(present.values()) >= abstract_sentences
        ):
            raise _reject(
                ErrorCode.ZH_SUMMARY_SENTENCE_TRANSLATION,
                f"摘要 {abstract_sentences} 句，輸出句數不少於摘要",
            )

    vocabulary = _InputVocabulary.build(input_text)
    for name, text in present.items():
        if not _numbers(text) <= vocabulary.numbers:
            raise _reject(ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT, f"{name} 有輸入沒有的數字")
        for match in _ENGLISH_TOKEN.finditer(_nfkc(text)):
            if not vocabulary.knows_token(match.group(0)):
                raise _reject(
                    ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT, f"{name} 有輸入沒有的英文詞"
                )
    return content


@dataclass(frozen=True)
class ZhSummaryValidator:
    """交給閘門 `LlmJob(validate_output=...)` 的驗證函式（符合 `OutputValidator` 的簽名）。"""

    rules: ZhSummaryRules

    def __call__(self, output: BaseModel | str, llm_input: LlmInput) -> None:
        validate_zh_summary(output, llm_input, self.rules)
