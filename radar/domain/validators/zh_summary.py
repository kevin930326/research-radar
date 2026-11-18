"""中文重點的正規化與 domain 驗證（PRD FR-14、NFR-4、Story 2.4、ADR-0032）。

模型輸出是不可信的輸入：Pydantic（`ZhSummaryOutput`）只管形狀，這裡管內容。

**正規化**（`normalize_zh_summary`，`services/ai_results.py` 寫入前也用同一個函式）：每欄 NFC、
連續空白縮成一個、去頭尾；結果是空的，或整欄只是「摘要未提及／未提及／無／N/A」這類字，存 NULL。

**拒收規則**（`validate_zh_summary`），依下面的順序檢查，第一個不過的就是原因代碼：

1. 存不進資料庫（NUL、單獨的代理字元）→ `llm_output_unstorable`。
2. 雙向控制字元、tag 字元、HTML 實體；夾標記或連結：標籤樣的 `<`、欄位開頭的 `>`（完整的比較式
   如 `p < .05`、`>50%`、`x<y` 不算）、反引號、粗體、Markdown 連結、網址、裸網域（輸入裡有的首字
   大寫品牌名稱如 `Amazon.com` 除外）→ `zh_summary_unsafe_text`。
3. 夾「摘要未提及…」這類講摘要本身的句子 → `zh_summary_placeholder_text`。
4. 任一欄正規化後的字元數超過 `max_chars_per_field` → `zh_summary_field_too_long`。
5. 不是繁中：沒有漢字、漢字比例低於 `min_cjk_percent`、或含簡體專用字 → `zh_summary_not_zh_hant`。
   漢字比例不算「中文名稱（英文）」括號裡的英文（緊接在漢字後面、有閉合、最外層而且不巢狀；整欄
   合計最多 8 個詞不算，超過的部分照算）。
6. 逐句翻譯：摘要句數 ≥ `translation_min_abstract_sentences`，而且輸出句數 ≥ 摘要句數
   → `zh_summary_sentence_translation`。
7. 編造：有輸入（標題＋摘要）沒有的數字（含正負號、指數、萬／億等單位、百分比）、字母開頭的英數詞
   （`B2B`、`N120`）或英文詞，或用了不支援的數字寫法（數字後面的上標、分數字元、`^`、`×10`、`፱`
   這類數字字元）→ `zh_summary_unsupported_content`。輸入用 `ExtendedVocabulary` 讀（2026-11-18）：
   空白千分位、月份英文名（只對「9 月」這種接「月」的數字）、年代（只對接「年代」的數字）、
   `10-40%`、`10% points`、輸出正數對輸入同值負數、英文詞的詞形變化、黏字、`vs`。

第 2～7 條都在「檢查用副本」（`_check_copy`）上做：HTML 實體解碼、拿掉零寬等格式字元、NFKC、
各種文字的十進位數字換成 0～9、破折號類換成 `-`、空白縮成一個。存進資料庫的仍是
`normalize_text` 的結果。

判準的細節與抓不到的情況寫在 ADR-0032。
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
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

# 共用的文字檢查在 `text_checks.py`（2.6 搬過去共用）；這裡照原本的名字用。編造判準用的是中文重點
# 專用的 `ExtendedVocabulary`（2026-11-18 把輸入讀得更完整），研究模型照舊用 `InputVocabulary`。
# `SIMPLIFIED_ONLY`、`normalize_text` 是公開名稱，從這個模組也拿得到。
from radar.domain.validators.text_checks import BIDI_OR_TAG as _BIDI_OR_TAG
from radar.domain.validators.text_checks import ENGLISH_TOKEN as _ENGLISH_TOKEN
from radar.domain.validators.text_checks import ENGLISH_WORD as _ENGLISH_WORD
from radar.domain.validators.text_checks import HTML_ENTITY as _HTML_ENTITY
from radar.domain.validators.text_checks import SIMPLIFIED_ONLY as SIMPLIFIED_ONLY
from radar.domain.validators.text_checks import ExtendedVocabulary as _ExtendedVocabulary
from radar.domain.validators.text_checks import check_copy as _check_copy
from radar.domain.validators.text_checks import has_markup as _has_markup
from radar.domain.validators.text_checks import has_unsupported_number as _has_unsupported_number
from radar.domain.validators.text_checks import is_han as _is_han
from radar.domain.validators.text_checks import normalize_text as normalize_text
from radar.domain.validators.text_checks import numbers as _numbers

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

# 比對「整欄只是佔位字」時先拿掉的字元：空白、標點、底線（`\W` 不含漢字、字母、數字）。
_NOT_WORD: Final = re.compile(r"[\W_]+")
# 「講摘要本身」的主詞。整欄比對（存 NULL）認得全部主詞；夾在內容裡的比對分兩種（見下面的
# `_EMBEDDED_PLACEHOLDER`）：只有「摘要」一定是在講摘要；原文、文中、本文、本研究、研究、作者、
# 文章、論文也可能是研究材料或研究結果的開頭（「原文未揭露姓名時仍可推得作者身分」「本文發現…」）。
_ABSTRACT_SUBJECTS: Final = r"(?:摘要|原文|文中)"
_PAPER_SUBJECTS: Final = r"(?:本文|本研究|研究|作者|文章|論文)"
_NEGATION: Final = r"(?:中|裡|內)?(?:並|均|皆|也)?(?:未|沒有|沒|無|不)(?:明確|特別)?"
_META_VERBS: Final = r"(?:提及|提到|提供|說明|描述|指出|交代|報告|載明|詳述|揭露|涉及)"
_PLACEHOLDER_ZH: Final = re.compile(
    rf"(?:{_ABSTRACT_SUBJECTS}|{_PAPER_SUBJECTS})?{_NEGATION}"
    rf"{_META_VERBS}?(?:任何)?(?:相關)?(?:所使用|所採用|所用|使用|採用)?(?:的)?"
    r"(?:理論|理論框架|方法|研究方法|研究問題|問題|發現|結果|資訊|資料|內容)?"
    r"|不詳|未知|不明|從缺|不適用"
)
_PLACEHOLDER_EN: Final = re.compile(
    r"na|none|null|nil|unknown|notapplicable"
    r"|not(?:mentioned|specified|reported|stated|available|provided|given)(?:intheabstract|inabstract)?"
)


# 欄位開頭的「欄位名稱＋冒號」（「理論：未提及」），整欄比對前先拿掉。
_FIELD_LABEL: Final = re.compile(
    r"^(?:研究問題|問題|研究方法|方法|理論框架|理論基礎|理論|研究發現|發現|研究結果|結果)\s?:"
)


def is_placeholder(text: str) -> bool:
    """整欄只是「摘要未提及／未提及／無／N/A」這類字（在檢查用副本上拿掉空白與標點、不分大小寫
    後比對）；拿掉之後什麼都不剩（例如只有「—」或零寬空白）也算。"""
    copy = _FIELD_LABEL.sub("", _check_copy(text), count=1)
    key = _NOT_WORD.sub("", copy).casefold()
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

# 欄位裡夾的「講摘要本身」的句子：
# - 「摘要」後面 8 個字內接否定詞、再 4 個字內接「提及、說明…」；「未在摘要中…」；「未在原文
#   （文中）中提及…」（「姓名未在原文中出現」不算）。
# - 原文、文中、本文、本研究、研究、作者、文章、論文：否定詞要緊接在後面（只能隔「中、並、也、
#   明確」這類字），接著「提及、說明…」，再接研究要素（樣本數、方法、理論…）或直接斷句。所以
#   「原文未揭露姓名時仍可推得作者身分」「作者未揭露身分的評論」「本文探討使用者為何不揭露個人
#   資訊」都不算。
# - 英文 `not mentioned`、`n/a`。
# 只寫「未提及」不算（「受訪者未提及隱私疑慮」可能是真的發現）。
_RESEARCH_ELEMENTS: Final = (
    r"(?:樣本數|樣本大小|樣本|受試者人數|研究方法|分析方法|方法|理論基礎|理論框架|理論"
    r"|研究問題|問題|研究發現|發現|研究結果|結果|資料來源|數據|變數|假設|研究模型|模型"
    r"|研究設計|研究對象|細節)"
)
_EMBEDDED_PLACEHOLDER: Final = re.compile(
    rf"摘要[^。！？!?；;]{{0,8}}?(?:未|沒有|沒|無|並未|不)[^。！？!?；;]{{0,4}}?{_META_VERBS}"
    rf"|(?:原文|文中|{_PAPER_SUBJECTS}){_NEGATION}{_META_VERBS}(?:任何|相關|具體)?(?:所使用|所採用|所用|使用|採用)?(?:的)?"
    rf"(?:{_RESEARCH_ELEMENTS}|(?=[、,.。；;！？!?)]|$))"
    r"|(?:未|沒有|沒|並未)(?:在|於)摘要"
    rf"|(?:未|沒有|沒|並未)(?:在|於)(?:原文|文中)(?:中|裡|內)?{_META_VERBS}"
    r"|(?<![A-Za-z])not\s+(?:mentioned|specified|reported|stated)(?![A-Za-z])"
    r"|(?<![A-Za-z])n/a(?![A-Za-z])",
    re.IGNORECASE,
)
# 摘要的句子結尾：一串 `.!?` 後面接空白。開頭不能接在 `.!?` 後面、整串用佔有式量詞，所以一長串
# 句點後面沒有空白時只掃一次（原本的 `[.!?]+\s+` 會平方級回溯）。
_ABSTRACT_SENTENCE_END: Final = re.compile(r"(?<![.!?])[.!?]++\s+")
_OUTPUT_SENTENCE_END: Final = re.compile(r"[。！？!?]+")
# 括號（全形括號在檢查用副本裡已經是半形）。
_PARENTHESIS: Final = re.compile(r"[()]")
# 每欄「中文名稱（英文）」括號裡的英文，合計最多幾個詞不算進漢字比例（「整合型科技接受模型（unified
# theory of acceptance and use of technology）」是 8 個）；超過的部分照算（審查 C7、A-3、B-6）。
_MAX_PARENTHESIZED_WORDS: Final = 8
_HAS_WORD: Final = re.compile(r"\w")


def _input_texts(llm_input: LlmInput) -> tuple[str, str | None]:
    """（標題＋摘要合在一起的檢查用副本, 摘要的檢查用副本；沒有摘要時是 None），已拿掉
    `<document>` 包裝。"""
    parts: list[str] = []
    abstract: str | None = None
    for block in llm_input.blocks:
        text = _check_copy(unwrap_document(block))
        parts.append(text)
        if block.document_type is DocumentType.ABSTRACT:
            abstract = text
    return "\n".join(parts), abstract


def _reject(code: ErrorCode, detail: str) -> OutputRejectedError:
    return OutputRejectedError(code, detail)


def _check_not_zh_hant(name: str, text: str, rules: ZhSummaryRules) -> None:
    """`text` 是檢查用副本。漢字比例不算「中文名稱（英文）」括號裡的英文
    （`_english_word_count`）。"""
    han = sum(1 for char in text if _is_han(char))
    words = _english_word_count(text)
    if han == 0:
        raise _reject(ErrorCode.ZH_SUMMARY_NOT_ZH_HANT, f"{name} 沒有漢字")
    if han * 100 < rules.min_cjk_percent * (han + words):
        raise _reject(ErrorCode.ZH_SUMMARY_NOT_ZH_HANT, f"{name} 的漢字比例太低")
    if any(char in SIMPLIFIED_ONLY for char in text):
        raise _reject(ErrorCode.ZH_SUMMARY_NOT_ZH_HANT, f"{name} 有簡體專用字")


def _english_word_count(text: str) -> int:
    """算漢字比例用的英文詞數。「中文名稱（英文）」括號（prompt 規則 3）裡的英文，整欄合計最多
    `_MAX_PARENTHESIZED_WORDS` 個詞不算，超過的部分照算。算數的括號要：緊接在漢字（或漢字＋
    一個空白）後面、有閉合、是最外層（不在別的括號裡）、裡面沒有別的括號（審查 C5）。其他括號
    （沒閉合、巢狀、前面不是漢字）裡的英文照算。只掃一次括號，線性時間。"""
    words = len(_ENGLISH_WORD.findall(text))
    exempt = 0
    depth = 0
    opened = 0
    nested = False
    for match in _PARENTHESIS.finditer(text):
        position = match.start()
        if match.group(0) == "(":
            if depth == 0:
                opened, nested = position, False
            else:
                nested = True
            depth += 1
            continue
        if depth == 0:
            continue  # 沒有對應開括號的 `)`
        depth -= 1
        if depth == 0 and not nested and _after_han(text, opened):
            exempt += len(_ENGLISH_WORD.findall(text, opened + 1, position))
    return words - min(exempt, _MAX_PARENTHESIZED_WORDS)


def _after_han(text: str, position: int) -> bool:
    """`position` 前面緊接漢字，或漢字＋一個空白。"""
    before = text[max(0, position - 2) : position]
    before = before[:-1] if before.endswith(" ") else before[-1:]
    return len(before) == 1 and _is_han(before)


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

    input_text, abstract = _input_texts(llm_input)
    for name in FIELDS:
        value = getattr(output, name)
        if value is None:
            continue
        if _BIDI_OR_TAG.search(value) or _HTML_ENTITY.search(value):
            raise _reject(ErrorCode.ZH_SUMMARY_UNSAFE_TEXT, f"{name} 含雙向控制字元或 HTML 實體")
        if _has_markup(value, input_text):
            raise _reject(ErrorCode.ZH_SUMMARY_UNSAFE_TEXT, f"{name} 含標記或連結")

    content = normalize_zh_summary(output)
    present = {name: value for name, value in content.fields().items() if value is not None}
    checked = {name: _check_copy(value) for name, value in present.items()}

    for name, text in checked.items():
        if _EMBEDDED_PLACEHOLDER.search(text):
            raise _reject(ErrorCode.ZH_SUMMARY_PLACEHOLDER_TEXT, f"{name} 夾了講摘要本身的句子")
    for name, text in present.items():
        if len(text) > rules.max_chars_per_field:
            raise _reject(
                ErrorCode.ZH_SUMMARY_FIELD_TOO_LONG,
                f"{name} 有 {len(text)} 字，上限 {rules.max_chars_per_field}",
            )
    for name, text in checked.items():
        _check_not_zh_hant(name, text, rules)

    if abstract is not None:
        abstract_sentences = count_abstract_sentences(abstract)
        if (
            abstract_sentences >= rules.translation_min_abstract_sentences
            and count_output_sentences(checked.values()) >= abstract_sentences
        ):
            raise _reject(
                ErrorCode.ZH_SUMMARY_SENTENCE_TRANSLATION,
                f"摘要 {abstract_sentences} 句，輸出句數不少於摘要",
            )

    vocabulary = _ExtendedVocabulary.build(input_text)
    for name, text in checked.items():
        if _has_unsupported_number(present[name], text):
            raise _reject(ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT, f"{name} 有不支援的數字寫法")
        number_text, unknown_alnum = vocabulary.number_text(text)
        if unknown_alnum is not None:
            raise _reject(ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT, f"{name} 有輸入沒有的英數詞")
        numbers, unreadable = _numbers(number_text)
        if unreadable or not all(vocabulary.knows_number(number) for number in numbers):
            raise _reject(ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT, f"{name} 有輸入沒有的數字")
        for match in _ENGLISH_TOKEN.finditer(text):
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
