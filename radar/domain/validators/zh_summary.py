"""中文重點的正規化與 domain 驗證（PRD FR-14、NFR-4、Story 2.4、ADR-0032）。

模型輸出是不可信的輸入：Pydantic（`ZhSummaryOutput`）只管形狀，這裡管內容。

**正規化**（`normalize_zh_summary`，`services/ai_results.py` 寫入前也用同一個函式）：每欄 NFC、
連續空白縮成一個、去頭尾；結果是空的，或整欄只是「摘要未提及／未提及／無／N/A」這類字，存 NULL。

**拒收規則**（`validate_zh_summary`），依下面的順序檢查，第一個不過的就是原因代碼：

1. 存不進資料庫（NUL、單獨的代理字元）→ `llm_output_unstorable`。
2. 夾標記或連結：標籤樣的 `<`、欄位開頭的 `>`（完整的比較式如 `p < .05`、`>50%`、`x<y` 不算）、
   反引號、粗體、Markdown 連結、網址、裸網域 → `zh_summary_unsafe_text`。
3. 夾「摘要未提及…」這類講摘要本身的句子 → `zh_summary_placeholder_text`。
4. 任一欄正規化後的字元數超過 `max_chars_per_field` → `zh_summary_field_too_long`。
5. 不是繁中：沒有漢字、漢字比例低於 `min_cjk_percent`、或含簡體專用字 → `zh_summary_not_zh_hant`。
6. 逐句翻譯：摘要句數 ≥ `translation_min_abstract_sentences`，而且輸出句數 ≥ 摘要句數
   → `zh_summary_sentence_translation`。
7. 編造：有輸入（標題＋摘要）沒有的數字（含正負號、指數、百分比）或英文詞，或用了不支援的
   數字寫法（上標、分數字元、`^`、`×10`）→ `zh_summary_unsupported_content`。

第 2～7 條都在「檢查用副本」（`_check_copy`）上做：HTML 實體解碼、拿掉零寬等格式字元、NFKC、
各種文字的十進位數字換成 0～9、空白縮成一個。存進資料庫的仍是 `normalize_text` 的結果。

判準的細節與抓不到的情況寫在 ADR-0032。
"""

import html
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, DecimalException
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
# 「講摘要本身」的主詞。整欄比對（存 NULL）認得全部主詞；夾在內容裡的比對分兩種（見下面的
# `_EMBEDDED_PLACEHOLDER`）：只有「摘要」一定是在講摘要；原文、文中、本文、本研究、研究、作者、
# 文章、論文也可能是研究材料或研究結果的開頭（「原文未揭露姓名時仍可推得作者身分」「本文發現…」）。
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


def _strip_invisible(text: str) -> str:
    """HTML 實體解碼（最多三層，`&amp;lt;` 也解得開）並拿掉零寬空白等格式字元（Unicode Cf）。"""
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return "".join(char for char in text if unicodedata.category(char) != "Cf")


def _check_copy(text: str) -> str:
    """檢查用副本：`_strip_invisible` 之後全形逗號換成頓號、NFKC（全形變體換成半形）、各種文字
    的十進位數字（例如 `٩`）換成 0～9、連續空白縮成一個、去頭尾。只拿來檢查，不存。"""
    # NFKC 會把全形逗號「，」變成「,」，「120，350 名」就被當成千分位的 120350；先換成頓號。
    folded = unicodedata.normalize("NFKC", _strip_invisible(text).replace("，", "、"))
    folded = "".join(
        str(unicodedata.decimal(char)) if char.isdecimal() and not char.isascii() else char
        for char in folded
    )
    return _WHITESPACE.sub(" ", folded).strip()


def is_placeholder(text: str) -> bool:
    """整欄只是「摘要未提及／未提及／無／N/A」這類字（在檢查用副本上拿掉空白與標點、不分大小寫
    後比對）；拿掉之後什麼都不剩（例如只有「—」或零寬空白）也算。"""
    key = _NOT_WORD.sub("", _check_copy(text)).casefold()
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

# 完整的比較式（先認出來、換成空白，再找標記）：比較符號後面接數字（`p < .05`、`>50%`、`β > 0`），
# 或兩邊各是一個英文字母（`x<y`、`a > b`；右邊後面不能再接字母，到下一個漢字或頓號之前的 40 個
# 字內也不能有 `>`，所以 `x<b>`、`a<b class=x>` 不算）。
_COMPARISON: Final = re.compile(
    r"[<>]=?\s?[-+−]?(?:[0-9]|\.[0-9])"
    r"|(?<![A-Za-z])[A-Za-z]\s?[<>]=?\s?[A-Za-z](?![A-Za-z>])(?![^<>\u2e80-\u9fff、]{0,40}>)"
)
# 標記與連結（在拿掉比較式的檢查用副本上找）：`<` 後面接標籤樣的字元（`<b>`、`</div`、`<!--`、
# `<?`）；欄位開頭的 `>`（Markdown 引言）；反引號；粗體與刪除線（`**`、`__`、`~~`）；行內連結
# `](`、參照式連結 `][` 與 `[名稱]: 網址`；任何 `://`、`www.`、`mailto:`、`javascript:`。漢字在
# Python 的正規式裡也是 `\w`，所以「見https://…」這種寫法不能靠 `\b`，改用不接英文字母的前後文。
_UNSAFE: Final = re.compile(
    r"<(?=[/!?A-Za-z])|^>|`|\*\*|__|~~|\]\(|\]\[|\[[^\[\]]{1,80}\]:"
    r"|://|(?<![A-Za-z0-9])www\.|(?<![A-Za-z])(?:mailto|javascript):",
    re.IGNORECASE,
)
# 裸網域（`osf.io`、`example.org`、`data.gov.tw`），即使摘要裡也有：網域只認小寫的常見頂級網域，
# 所以 `ASP.NET`、`Node.js` 這類技術名稱不算。開頭不能接在字母、數字、`.`、`-` 後面，每個網域
# 只從開頭試一次（不會平方級回溯）。
_TLDS: Final = (
    "com|org|net|edu|gov|mil|int|info|biz|xyz|app|dev|site|online|tech|io|ai|co|me|tv|ly"
    "|tw|cn|hk|jp|kr|sg|uk|de|fr|nl|ch|se|it|es|eu|us|ca|au|br|ru|in"
)
_BARE_DOMAIN: Final = re.compile(
    rf"(?<![A-Za-z0-9.\-])(?:[A-Za-z0-9\-]++\.)+(?:{_TLDS})(?![A-Za-z0-9\-])"
)
# 欄位裡夾的「講摘要本身」的句子：
# - 「摘要」後面 8 個字內接否定詞、再 4 個字內接「提及、說明…」；「未在摘要中…」。
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
    rf"|(?:原文|文中|{_PAPER_SUBJECTS}){_NEGATION}{_META_VERBS}(?:任何|相關|具體)?(?:使用|採用)?(?:的)?"
    rf"(?:{_RESEARCH_ELEMENTS}|(?=[、,.。；;！？!?)]|$))"
    r"|(?:未|沒有|沒|並未)(?:在|於)摘要"
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
# 數字（在檢查用副本上找）：正負號（前面不是字母、數字、`.`、`)`；`3-5` 的 `-` 不是負號）、
# 千分位（1,200）合併、APA 寫法的 .05 是 0.05、指數（1e-3）、百分比（`%`、`percent`、`per cent`）。
# 值是（Decimal, 是不是百分比），所以 -0.35 ≠ 0.35、0.35 ≠ 0.35%、1e-3 ≠ 1e3。
_NUMBER: Final = re.compile(
    r"(?P<sign>(?<![0-9A-Za-z.)\]])[-+−–])?"
    r"(?P<mantissa>[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?|(?<![0-9])\.[0-9]+)"
    r"(?P<exponent>[eE][-+−]?[0-9]+)?"
    r"(?P<percent>\s?(?:%|(?i:percent|per cent)(?![A-Za-z])))?"
)
_NUMBER_CONTEXT: Final = Context(prec=100, Emax=MAX_EMAX, Emin=MIN_EMIN)
# 不支援、會讓數值意思走樣的寫法（輸出有就拒收）：上標與下標數字、分數字元與分數斜線（NFKC 前
# 才看得到）；`^`、千分比、`×10` 這種科學記號（NFKC 後）。
_UNSUPPORTED_NUMBER_RAW: Final = re.compile(r"[²³¹⁰-₟¼-¾⅐-⅟⁄]")
_UNSUPPORTED_NUMBER: Final = re.compile(r"\^|‰|‱|×\s?10(?![0-9.,])")
# 摘要的句子結尾：一串 `.!?` 後面接空白。開頭不能接在 `.!?` 後面、整串用佔有式量詞，所以一長串
# 句點後面沒有空白時只掃一次（原本的 `[.!?]+\s+` 會平方級回溯）。
_ABSTRACT_SENTENCE_END: Final = re.compile(r"(?<![.!?])[.!?]++\s+")
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


# 數值：（Decimal, 是不是百分比）。
type _Number = tuple[Decimal, bool]


def _numbers(text: str) -> tuple[set[_Number], bool]:
    """檢查用副本裡的數字 →（數值集合, 有沒有讀不懂的數字，例如指數大到算不出來）。"""
    found: set[_Number] = set()
    unreadable = False
    for match in _NUMBER.finditer(text):
        sign = "-" if match["sign"] in ("-", "−", "–") else ""
        exponent = (match["exponent"] or "").replace("−", "-")
        try:
            value = Decimal(sign + match["mantissa"].replace(",", "") + exponent)
            found.add((value.normalize(_NUMBER_CONTEXT), match["percent"] is not None))
        except DecimalException:
            unreadable = True
    return found, unreadable


def _number_word_kind(word: str) -> tuple[str | None, int]:
    if word in _UNIT_WORDS:
        value = _UNIT_WORDS[word]
        return ("teen" if value >= 10 else "unit"), value
    if word in _TENS_WORDS:
        return "tens", _TENS_WORDS[word]
    if word in _SCALE_WORDS:
        return word, _SCALE_WORDS[word]
    if word == "and":
        return "and", 0
    return None, 0


# 英文數字詞組裡，每一種詞後面可以接哪幾種（None＝詞組的開頭）。
_NUMBER_WORD_FOLLOWS: Final[Mapping[str | None, frozenset[str]]] = {
    None: frozenset({"unit", "teen", "tens", "hundred", "thousand"}),
    "unit": frozenset({"hundred", "thousand"}),
    "teen": frozenset({"hundred", "thousand"}),
    "tens": frozenset({"unit", "thousand"}),
    "hundred": frozenset({"unit", "teen", "tens", "thousand", "and"}),
    "thousand": frozenset({"unit", "teen", "tens", "and"}),
    "and": frozenset({"unit", "teen", "tens"}),
}


def _number_word_values(words: list[str]) -> set[_Number]:
    """輸入裡的英文數字詞換成數字：一整個詞組只留它的值，組成的詞不另外算。
    「three hundred」只有 300（沒有 3、100）、「twenty five」只有 25、「two thousand」只有 2000、
    「three hundred and twenty」只有 320；first～tenth 是 1～10；詞組後面接 `percent`／`per cent`
    時是百分比。"""
    values: set[_Number] = set()
    total = current = 0
    last: str | None = None
    in_phrase = False

    def flush(index: int) -> None:
        nonlocal in_phrase
        if in_phrase:
            following = words[index : index + 2]
            percent = following[:1] == ["percent"] or following == ["per", "cent"]
            values.add((Decimal(total + current), percent))
        in_phrase = False

    for index, word in enumerate(words):
        kind, value = _number_word_kind(word)
        if in_phrase and kind in _NUMBER_WORD_FOLLOWS[last]:
            pass  # 接在目前的詞組後面
        elif kind in _NUMBER_WORD_FOLLOWS[None]:
            flush(index)  # 新的詞組（「three four」是兩個詞組）
            total = current = 0
            in_phrase = True
        else:
            flush(index)
            if word in _ORDINAL_WORDS:
                values.add((Decimal(_ORDINAL_WORDS[word]), False))
            continue
        if kind == "hundred":
            current = (current or 1) * value
        elif kind == "thousand":
            total += (current or 1) * value
            current = 0
        elif kind != "and":
            current += value
        last = kind
    flush(len(words))
    return values


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

    numbers: frozenset[_Number]
    words: frozenset[str]
    initials: tuple[str, ...]

    @classmethod
    def build(cls, text: str) -> _InputVocabulary:
        """`text` 是檢查用副本。"""
        words = [word.lower() for word in _ENGLISH_WORD.findall(text)]
        variants: set[str] = set()
        for word in words:
            variants |= _variants(word)
        content = [word for word in words if word not in _FUNCTION_WORDS]
        return cls(
            numbers=frozenset(_numbers(text)[0] | _number_word_values(words)),
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
    """`text` 是檢查用副本。"""
    han = sum(1 for char in text if _is_han(char))
    words = len(_ENGLISH_WORD.findall(text))
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

    for name in FIELDS:
        value = getattr(output, name)
        if value is not None and _has_markup(value):
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

    input_text, abstract = _input_texts(llm_input)
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

    vocabulary = _InputVocabulary.build(input_text)
    for name, text in checked.items():
        if _UNSUPPORTED_NUMBER_RAW.search(_strip_invisible(present[name])) or (
            _UNSUPPORTED_NUMBER.search(text)
        ):
            raise _reject(ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT, f"{name} 有不支援的數字寫法")
        numbers, unreadable = _numbers(text)
        if unreadable or not numbers <= vocabulary.numbers:
            raise _reject(ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT, f"{name} 有輸入沒有的數字")
        for match in _ENGLISH_TOKEN.finditer(text):
            if not vocabulary.knows_token(match.group(0)):
                raise _reject(
                    ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT, f"{name} 有輸入沒有的英文詞"
                )
    return content


def _has_markup(value: str) -> bool:
    """標記或連結：先把完整的比較式（`p < .05`、`>50%`、`x<y`）換成空白，再找標記與裸網域。"""
    text = _COMPARISON.sub(" ", _check_copy(value))
    return _UNSAFE.search(text) is not None or _BARE_DOMAIN.search(text) is not None


@dataclass(frozen=True)
class ZhSummaryValidator:
    """交給閘門 `LlmJob(validate_output=...)` 的驗證函式（符合 `OutputValidator` 的簽名）。"""

    rules: ZhSummaryRules

    def __call__(self, output: BaseModel | str, llm_input: LlmInput) -> None:
        validate_zh_summary(output, llm_input, self.rules)
