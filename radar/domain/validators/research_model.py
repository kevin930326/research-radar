"""研究模型的正規化與 domain 驗證（PRD FR-3、NFR-4、Story 2.6、ADR-0034）。

模型輸出是不可信的輸入：Pydantic（`ResearchModelOutput`，萃取 schema v1）只管形狀，這裡管內容。
`validate_research_model` 依下面的順序檢查，第一個不過的就是原因代碼（整筆拒收、不寫內容）；全部
通過時回要存的 `ResearchModelContent`（`services/ai_results.py` 寫入前也用同一個函式）。

1. 存不進資料庫（NUL、單獨的代理字元、非有限的數字）→ `llm_output_unstorable`。
2. 任何字串含雙向控制字元、tag 字元或 HTML 實體；英文名或繁中標籤含標記或連結（和中文重點同一套
   判準）→ `research_model_unsafe_text`。
3. 英文名：正規化後 1～120 字、有英文字母、沒有漢字與控制或格式字元、`normalize_name` 不是空的；
   繁中標籤：1～30 字、有漢字、沒有簡體專用字與控制或格式字元；假設原句與構面定義至少 3 個詞
   → `research_model_invalid_text`。
4. **原文欄位**（假設原句、構面定義、研究限制、未來研究、量表來源、量表題目位置、效果類型）：去掉
   頭尾空白與 `.,;:` 後，在「檢查用副本＋casefold＋彎引號換直引號」上，必須是標題或摘要其中一段
   的子字串（開頭或結尾是字母、數字時，前後不能剛好接著字母或數字）
   → `research_model_quote_not_in_input`。
5. 兩個構面 `normalize_name` 後相同 → `research_model_duplicate_construct`。
6. 假設的因或果 `normalize_name` 後不是清單裡的構面，或因和果是同一個
   → `research_model_unknown_construct`。
7. 樣本數、題數 < 1 或超過 2^31 - 1；α、CR、AVE、p 不在 0～1；β 不是有限數
   → `research_model_number_out_of_range`。
8. 每個數字（樣本數、題數、α、CR、AVE、β、p）都要是輸入裡的數字（`structured_numbers`：只有完整
   量級、科學記號算出完整的值、`= - 0.42` 是負數）；英文名的每個英文詞與數字、繁中標籤裡的英文詞
   與數字都要在輸入出現（2.4 的編造判準）→ `research_model_unsupported_content`。
9. 同一句（來源句子的雜湊相同：完整版與片段算同一句）、同一條關係（因、果相同）出現兩次而值
   （證據類型、改過的支持與否、統計數字）不同 → `research_model_conflicting_hypothesis`。

**不拒收、改值並計數**：

- 情境值不在清單 → 空值，`dropped_context_value_count`。
- 國家、產業的值在清單，但它的 `terms` 都不在輸入（或只落在 `excluded_terms` 裡，例如北韓全名裡
  的 Republic of Korea）→ 空值，`unsupported_context_value_count`（FR-3 擷取規則 4：不推測）。
- 證據類型不是實證檢定卻判了支持與否（含 `not_stated`）→ `not_determined`；實證檢定卻給
  `not_determined` → `not_stated`；判定了（supported 等）卻沒有可以採信的結果原文 `result_quote`
  （有文字、在輸入裡、寫出了結果）→ `not_stated`；`corrected_support_count`（擷取規則 1、2）。
  只數存下來的假設。
- p 值不是輸入寫出的確切值（只寫 `p < .01` 這種門檻）→ 空值；選填原文欄位只有標點（`-`）→ 空值；
  `cleared_value_count`。
- 同一句、同一條關係、值都一樣的重複只留第一筆（擷取規則 3）。

沒提到（null、空白）的欄位存空值。判準的細節與抓不到的情況寫在 ADR-0034。
"""

import math
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from pydantic import BaseModel

from radar.domain.ai_outputs import (
    DECIDED_SUPPORT,
    ConstructMentionContent,
    HypothesisContent,
    LlmInput,
    OutputRejectedError,
    ResearchModelContent,
    check_output_storable,
    unwrap_document,
)
from radar.domain.constructs import normalize_name
from radar.domain.context_values import (
    TERM_FIELDS,
    ContextField,
    ContextValueItem,
    ContextValues,
)
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import hypothesis_sentence_sha256
from radar.domain.llm_schemas import (
    ConstructOutput,
    EvidenceType,
    HypothesisOutput,
    ResearchModelOutput,
    SupportLevel,
)
from radar.domain.validators.text_checks import (
    BIDI_OR_TAG,
    ENGLISH_TOKEN,
    HTML_ENTITY,
    NUMBER_CONTEXT,
    SIMPLIFIED_ONLY,
    InputVocabulary,
    Number,
    check_copy,
    decimal_of,
    has_markup,
    has_unsupported_number,
    is_han,
    normalize_text,
    numbers,
    structured_numbers,
)

MAX_NAME_EN_CHARS: Final = 120
MAX_LABEL_ZH_CHARS: Final = 30
MIN_QUOTE_WORDS: Final = 3
# 整數欄位（樣本數、題數）存成 PostgreSQL 的 integer，最大 2^31 - 1。
MAX_INTEGER: Final = 2**31 - 1
# 原文欄位比對前去掉的頭尾字元。
_QUOTE_TRIM: Final = " .,;:"
# 彎引號換成直引號（單引號類、雙引號類）。
_STRAIGHT_QUOTES: Final = str.maketrans(
    {chr(code): "'" for code in (0x2018, 0x2019, 0x201A, 0x201B, 0x2032)}
    | {chr(code): '"' for code in (0x201C, 0x201D, 0x201E, 0x201F, 0x2033)}
)
_WORD: Final = re.compile(r"[^\W_]+")
_ASCII_LETTER: Final = re.compile(r"[A-Za-z]")


def _reject(code: ErrorCode, detail: str) -> OutputRejectedError:
    return OutputRejectedError(code, detail)


def _optional_text(value: str | None) -> str | None:
    """要存的文字：`normalize_text` 之後是空的就當沒提到（None）。"""
    if value is None:
        return None
    text = normalize_text(value)
    return text or None


def _quote_text(value: str | None) -> str | None:
    """原文欄位要存的文字：沒提到（null、空白）或正規化後沒有任何文字或數字（只有標點，例如
    `"-"`）都是 None。"""
    text = _optional_text(value)
    if text is None or not any(char.isalnum() for char in text):
        return None
    return text


def _is_punctuation_only(value: str | None) -> bool:
    """有寫字、但沒有任何文字或數字（要清成空值並計數的那種）。"""
    return _optional_text(value) is not None and _quote_text(value) is None


def quote_key(text: str) -> str:
    """原文欄位比對用的寫法：檢查用副本、casefold、彎引號換直引號。"""
    return check_copy(text).casefold().translate(_STRAIGHT_QUOTES)


def _locate_quote(quote: str, segments: tuple[str, ...]) -> tuple[int, int, int] | None:
    """`quote` 在 `segments`（標題、摘要各一段，已是 `quote_key` 的寫法）第一個對得上的位置
    （第幾段, 開頭, 結尾）；開頭（結尾）是字母或數字時，前（後）一個字不能也是字母或數字
    （`positive` 不算在 `positively` 裡）。找不到是 None。"""
    if not quote:
        return None
    check_start = quote[0].isalnum()
    check_end = quote[-1].isalnum()
    for index, segment in enumerate(segments):
        start = segment.find(quote)
        while start != -1:
            end = start + len(quote)
            start_ok = not check_start or start == 0 or not segment[start - 1].isalnum()
            end_ok = not check_end or end == len(segment) or not segment[end].isalnum()
            if start_ok and end_ok:
                return index, start, end
            start = segment.find(quote, start + 1)
    return None


def _quote_found(quote: str, segments: tuple[str, ...]) -> bool:
    return _locate_quote(quote, segments) is not None


# 句子的結尾：`.`、`!`、`?`（可以再接引號或右括號），後面是空白或段落結尾。`.01`、`3.5` 的點後面
# 接數字，不算。
_SENTENCE_END: Final = re.compile(r"[.!?][\"')\]]*(?=\s|$)")
_SENTENCE_TRIM: Final = " .,;:!?"


def _source_sentence(quote: str, segments: tuple[str, ...]) -> str:
    """原句所在的來源句子：從原句開頭往前到上一個句子結尾、從原句結尾往後到下一個句子結尾（原句跨
    兩句就涵蓋兩句）；同一句的完整版與片段得到同一個來源句子（FR-3 擷取規則 3）。`quote` 一定在
    輸入裡（第 4 步已核對）。"""
    located = _locate_quote(quote, segments)
    if located is None:  # 第 4 步已擋下；保險起見用原句本身
        return quote
    index, start, end = located
    segment = segments[index]
    begin = 0
    for match in _SENTENCE_END.finditer(segment, 0, start):
        begin = match.end()
    following = _SENTENCE_END.search(segment, end)
    finish = following.end() if following is not None else len(segment)
    return segment[begin:finish].strip(_SENTENCE_TRIM) or quote


# 寫出檢定結果的跡象（`result_quote` 要有其中一種）：統計量（β、p、t、r、F…接比較符號），或結果
# 用語（supported、significant、found、show…）。只是提出假設的寫法（we hypothesize、we expect、
# is expected to…）沒有統計量時不算。
_RESULT_STATISTIC: Final = re.compile(
    r"(?<![a-z0-9])(?:p|β|beta|b|r|r2|t|f|z|χ2|chi2|or|coefficient)\s?(?:<=|>=|[=<>≤≥])"
)
_RESULT_WORD: Final = re.compile(
    r"(?<![a-z])(?:support(?:s|ed)?|confirm(?:s|ed)?|significant(?:ly)?|insignificant"
    r"|non-?significant|found|finds?|findings|show(?:s|ed|n)?|reveal(?:s|ed)?|indicate(?:s|d)?"
    r"|demonstrat(?:e|es|ed)|results?|rejected)(?![a-z])"
)
_HYPOTHESIS_ONLY: Final = re.compile(
    r"(?<![a-z])(?:we (?:hypothesi[sz]e|expect|predict|propose|posit)"
    r"|(?:is|are) (?:expected|predicted|hypothesi[sz]ed) to|hypothesi[sz]ed that)(?![a-z])"
)


def _reports_result(quote: str) -> bool:
    """`quote`（`quote_key` 的寫法）寫出了檢定結果：有統計量；或有結果用語、而且不是只提出假設。"""
    if _RESULT_STATISTIC.search(quote):
        return True
    return _RESULT_WORD.search(quote) is not None and _HYPOTHESIS_ONLY.search(quote) is None


# p 值的寫法：`p = .03`、`p < .001`、`p-value = 0.03`、`p ≤ .05`（在檢查用副本上找，不分大小寫）。
_P_VALUE: Final = re.compile(
    r"(?<![A-Za-z])[pP](?:\s?-?\s?values?)?\s?(?P<op><=|>=|[=:<>≤≥])\s?"
    r"(?P<number>(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)"
)
_EXACT_P: Final = frozenset({"=", ":"})


def _p_comparators(value: Decimal, text: str) -> set[str]:
    """`text`（檢查用副本）裡寫成 p 值、而且數值等於 `value` 的比較符號。"""
    found: set[str] = set()
    for match in _P_VALUE.finditer(text):
        number = decimal_of(match["number"])
        if number is not None and number == value:
            found.add(match["op"])
    return found


def _p_value_is_exact(value: Decimal, sentence: str, input_text: str) -> bool:
    """模型給的 p 值是輸入寫出的**確切值**：先看假設原句，原句沒有這個 p 值才看整個輸入；寫成
    `p = 值`（或 `p: 值`）才算，只寫門檻（`p < .01`）或根本沒寫成 p 值都不算。"""
    comparators = _p_comparators(value, check_copy(sentence)) or _p_comparators(value, input_text)
    return bool(comparators & _EXACT_P)


def _has_control_or_format(text: str) -> bool:
    return any(unicodedata.category(char) in ("Cc", "Cf") for char in text)


def _strings(value: object, path: str = "") -> Iterator[tuple[str, str]]:
    """輸出（`model_dump` 的結果）裡每個字串值與它的欄位路徑。"""
    stack: list[tuple[str, object]] = [(path, value)]
    while stack:
        where, item = stack.pop()
        if isinstance(item, str):
            yield where, item
        elif isinstance(item, dict):
            stack.extend(
                (f"{where}.{key}" if where else str(key), sub) for key, sub in item.items()
            )
        elif isinstance(item, list):
            stack.extend((f"{where}[{index}]", sub) for index, sub in enumerate(item))


def _quote_fields(output: ResearchModelOutput) -> Iterator[tuple[str, str | None]]:
    """有值的原文欄位（欄位路徑, 值）。"""
    for name in ("limitations", "future_research"):
        yield name, getattr(output, name)
    for index, construct in enumerate(output.constructs):
        where = f"constructs[{index}]"
        yield f"{where}.definition", construct.definition
        if construct.measurement is not None:
            yield f"{where}.measurement.scale_source", construct.measurement.scale_source
            yield f"{where}.measurement.item_location", construct.measurement.item_location
    for index, hypothesis in enumerate(output.hypotheses):
        where = f"hypotheses[{index}]"
        yield f"{where}.sentence", hypothesis.sentence
        if hypothesis.statistics is not None:
            yield f"{where}.statistics.effect_type", hypothesis.statistics.effect_type


def _numeric_fields(output: ResearchModelOutput) -> Iterator[tuple[str, int | float, bool]]:
    """有值的數字（欄位路徑, 值, 是不是 0～1 的比例）。整數另外要 ≥ 1。"""
    if output.context.sample_size is not None:
        yield "context.sample_size", output.context.sample_size, False
    for index, construct in enumerate(output.constructs):
        measurement = construct.measurement
        if measurement is None:
            continue
        where = f"constructs[{index}].measurement"
        if measurement.item_count is not None:
            yield f"{where}.item_count", measurement.item_count, False
        for name in ("cronbach_alpha", "composite_reliability", "ave"):
            value = getattr(measurement, name)
            if value is not None:
                yield f"{where}.{name}", value, True
    for index, hypothesis in enumerate(output.hypotheses):
        statistics = hypothesis.statistics
        if statistics is None:
            continue
        where = f"hypotheses[{index}].statistics"
        if statistics.beta is not None:
            yield f"{where}.beta", statistics.beta, False
        if statistics.p_value is not None:
            yield f"{where}.p_value", statistics.p_value, True
        if statistics.sample_size is not None:
            yield f"{where}.sample_size", statistics.sample_size, False


def _decimal(value: int | float) -> Decimal:
    """JSON 數字 → `Decimal`：浮點數照它最短的十進位寫法（`0.42` 就是 0.42，不是二進位的
    近似值）。"""
    return Decimal(value) if isinstance(value, int) else Decimal(repr(value))


def _optional_decimal(value: int | float | None) -> Decimal | None:
    return None if value is None else _decimal(value)


def _as_number(value: int | float) -> Number:
    return _decimal(value).normalize(NUMBER_CONTEXT), False


def _check_name_en(where: str, value: str) -> None:
    text = normalize_text(value)
    if not text or len(text) > MAX_NAME_EN_CHARS:
        raise _reject(ErrorCode.RESEARCH_MODEL_INVALID_TEXT, f"{where} 要是 1～120 字")
    if _has_control_or_format(text) or any(is_han(char) for char in text):
        raise _reject(ErrorCode.RESEARCH_MODEL_INVALID_TEXT, f"{where} 有控制字元或漢字")
    if _ASCII_LETTER.search(check_copy(text)) is None or not normalize_name(text):
        raise _reject(ErrorCode.RESEARCH_MODEL_INVALID_TEXT, f"{where} 不是英文名稱")


def _check_label_zh(where: str, value: str) -> None:
    text = normalize_text(value)
    if not text or len(text) > MAX_LABEL_ZH_CHARS:
        raise _reject(ErrorCode.RESEARCH_MODEL_INVALID_TEXT, f"{where} 要是 1～30 字")
    if _has_control_or_format(text) or not any(is_han(char) for char in text):
        raise _reject(ErrorCode.RESEARCH_MODEL_INVALID_TEXT, f"{where} 沒有漢字或有控制字元")
    if any(char in SIMPLIFIED_ONLY for char in check_copy(text)):
        raise _reject(ErrorCode.RESEARCH_MODEL_INVALID_TEXT, f"{where} 有簡體專用字")


def _check_word_count(where: str, value: str | None, *, required: bool) -> None:
    text = _optional_text(value) if required else _quote_text(value)
    if text is None and not required:
        return
    if text is None or len(_WORD.findall(check_copy(text))) < MIN_QUOTE_WORDS:
        raise _reject(ErrorCode.RESEARCH_MODEL_INVALID_TEXT, f"{where} 不到 3 個詞")


def _check_invented(where: str, value: str, vocabulary: InputVocabulary) -> None:
    """英文名、繁中標籤：數字與英文詞都要在輸入出現（2.4 的編造判準）。"""
    text = normalize_text(value)
    checked = check_copy(text)
    if has_unsupported_number(text, checked):
        raise _reject(ErrorCode.RESEARCH_MODEL_UNSUPPORTED_CONTENT, f"{where} 有不支援的數字寫法")
    found, unreadable = numbers(checked)
    if unreadable or not found <= vocabulary.numbers:
        raise _reject(ErrorCode.RESEARCH_MODEL_UNSUPPORTED_CONTENT, f"{where} 有輸入沒有的數字")
    for match in ENGLISH_TOKEN.finditer(checked):
        if not vocabulary.knows_token(match.group(0)):
            raise _reject(
                ErrorCode.RESEARCH_MODEL_UNSUPPORTED_CONTENT, f"{where} 有輸入沒有的英文詞"
            )


def _term_spans(term: str, segment: str) -> list[tuple[int, int]]:
    """`term` 在 `segment`（`quote_key` 的寫法）出現的位置：不分大小寫、前後不接英文字母或數字，
    字尾可以多一個 s 或 es；y 結尾的也可以寫成 ies（factory → factories）。"""
    folded = quote_key(term)
    plural = f"|{re.escape(folded[:-1])}ies" if folded.endswith("y") else ""
    pattern = re.compile(rf"(?<![0-9a-z])(?:{re.escape(folded)}(?:s|es)?{plural})(?![0-9a-z])")
    return [match.span() for match in pattern.finditer(segment)]


def _mentions_item(item: ContextValueItem, segments: tuple[str, ...]) -> bool:
    """輸入（每段已是 `quote_key` 的寫法）明確寫出這個國家或產業：`terms` 其中一個出現，而且不是
    落在 `excluded_terms`（較長的別的地名，例如北韓全名裡的 Republic of Korea）裡面。較長的
    `terms` 優先：排除字樣本身落在更長的 `terms` 裡（People's Republic of China 裡的
    Republic of China）時不算排除。"""
    for segment in segments:
        own = [span for term in item.terms or () for span in _term_spans(term, segment)]
        excluded = [
            span for term in item.excluded_terms or () for span in _term_spans(term, segment)
        ]
        active = [
            (start, end)
            for start, end in excluded
            if not any(
                own_start <= start and end <= own_end and own_end - own_start > end - start
                for own_start, own_end in own
            )
        ]
        if any(
            not any(start <= own_start and own_end <= end for start, end in active)
            for own_start, own_end in own
        ):
            return True
    return False


def _resolve_context(
    output: ResearchModelOutput, context_values: ContextValues, quote_segments: tuple[str, ...]
) -> tuple[dict[ContextField, str | None], int, int]:
    """情境欄位 →（要存的 key 或 None, 清單外丟掉幾個, 國家與產業輸入沒寫出幾個）。"""
    resolved: dict[ContextField, str | None] = {}
    dropped = unsupported = 0
    for field in ContextField:
        value: str | None = getattr(output.context, field.value)
        resolved[field] = None
        if value is None:
            continue
        item = context_values.item(field, value)
        if item is None:
            dropped += 1
            continue
        if field in TERM_FIELDS and not _mentions_item(item, quote_segments):
            unsupported += 1
            continue
        resolved[field] = value
    return resolved, dropped, unsupported


def _corrected_support(hypothesis: HypothesisOutput) -> tuple[SupportLevel, bool]:
    """FR-3 擷取規則 1、2：（要存的支持與否, 有沒有改）。"""
    empirical = hypothesis.evidence_type is EvidenceType.EMPIRICAL_TEST
    if not empirical and hypothesis.support is not SupportLevel.NOT_DETERMINED:
        return SupportLevel.NOT_DETERMINED, True
    if empirical and hypothesis.support is SupportLevel.NOT_DETERMINED:
        return SupportLevel.NOT_STATED, True
    return hypothesis.support, False


def _construct_content(construct: ConstructOutput) -> ConstructMentionContent:
    measurement = construct.measurement

    def number(name: str) -> Decimal | None:
        value = None if measurement is None else getattr(measurement, name)
        return None if value is None else _decimal(value)

    return ConstructMentionContent(
        normalized_name=normalize_name(normalize_text(construct.name_en)),
        name_en=normalize_text(construct.name_en),
        label_zh=normalize_text(construct.label_zh),
        definition=_quote_text(construct.definition),
        scale_source=None if measurement is None else _quote_text(measurement.scale_source),
        item_location=None if measurement is None else _quote_text(measurement.item_location),
        item_count=None if measurement is None else measurement.item_count,
        cronbach_alpha=number("cronbach_alpha"),
        composite_reliability=number("composite_reliability"),
        ave=number("ave"),
    )


def _result_quote(hypothesis: HypothesisOutput, quote_segments: tuple[str, ...]) -> str | None:
    """可以採信的結果原文：有文字、正規化後在輸入裡、而且寫出了檢定結果（`_reports_result`）。"""
    text = _quote_text(hypothesis.result_quote)
    if text is None:
        return None
    quote = quote_key(text).strip(_QUOTE_TRIM)
    if not _quote_found(quote, quote_segments) or not _reports_result(quote):
        return None
    return text


def _hypotheses_content(
    output: ResearchModelOutput, quote_segments: tuple[str, ...], input_text: str
) -> tuple[tuple[HypothesisContent, ...], int, int]:
    """改過支持與否、清掉只寫門檻的 p 值、去掉重複的假設，和（改了幾條支持與否, 清了幾個 p 值）
    （只數存下來的）。

    - 判定了支持與否（擷取規則 1、2 改過之後）卻沒有可以採信的結果原文 → `not_stated`（擷取規則
      1「報告了結果才判定」），算進改了幾條。
    - p 值不是輸入寫出的確切值（只寫 `p < .01` 這種門檻）→ 空值，算進清了幾個。
    - 同一句（來源句子相同，片段與完整版算同一句）、同一條關係只留第一筆。

    Raises:
        OutputRejectedError: 同一句、同一條關係的值互相衝突。
    """
    kept: list[HypothesisContent] = []
    seen: dict[tuple[str, str, str], tuple[object, ...]] = {}
    corrected = cleared = 0
    for index, hypothesis in enumerate(output.hypotheses):
        support, changed = _corrected_support(hypothesis)
        result_quote = None
        if support in DECIDED_SUPPORT:
            result_quote = _result_quote(hypothesis, quote_segments)
            if result_quote is None:
                support, changed = SupportLevel.NOT_STATED, True
        statistics = hypothesis.statistics
        beta = _optional_decimal(None if statistics is None else statistics.beta)
        p_value = _optional_decimal(None if statistics is None else statistics.p_value)
        p_cleared = p_value is not None and not _p_value_is_exact(
            p_value, hypothesis.sentence, input_text
        )
        if p_cleared:
            p_value = None
        sample_size = None if statistics is None else statistics.sample_size
        effect_type = None if statistics is None else _quote_text(statistics.effect_type)
        sentence = normalize_text(hypothesis.sentence)
        source = _source_sentence(quote_key(sentence).strip(_QUOTE_TRIM), quote_segments)
        sentence_sha256 = hypothesis_sentence_sha256(source)
        cause = normalize_name(hypothesis.cause)
        effect = normalize_name(hypothesis.effect)
        key = (sentence_sha256, cause, effect)
        values = (hypothesis.evidence_type, support, beta, p_value, sample_size, effect_type)
        if key in seen:
            if seen[key] != values:
                raise _reject(
                    ErrorCode.RESEARCH_MODEL_CONFLICTING_HYPOTHESIS,
                    f"hypotheses[{index}] 和前面同一句、同一條關係的值不一樣",
                )
            continue
        seen[key] = values
        corrected += int(changed)
        cleared += int(p_cleared)
        kept.append(
            HypothesisContent(
                ordinal=len(kept) + 1,
                sentence=sentence,
                sentence_sha256=sentence_sha256,
                cause=cause,
                effect=effect,
                evidence_type=hypothesis.evidence_type,
                support=support,
                beta=beta,
                p_value=p_value,
                sample_size=sample_size,
                effect_type=effect_type,
                result_quote=result_quote,
            )
        )
    return tuple(kept), corrected, cleared


def _cleared_quote_count(output: ResearchModelOutput) -> int:
    """只有標點、清成空值的選填原文欄位數（假設原句必填、另外要 3 個詞，不在這裡）。"""
    values: list[str | None] = [output.limitations, output.future_research]
    for construct in output.constructs:
        values.append(construct.definition)
        if construct.measurement is not None:
            values += [construct.measurement.scale_source, construct.measurement.item_location]
    for hypothesis in output.hypotheses:
        if hypothesis.statistics is not None:
            values.append(hypothesis.statistics.effect_type)
    return sum(_is_punctuation_only(value) for value in values)


def validate_research_model(
    output: BaseModel | str, llm_input: LlmInput, context_values: ContextValues
) -> ResearchModelContent:
    """驗證研究模型（規則與順序見模組說明），通過時回要存的內容。

    Raises:
        OutputRejectedError: 不通過；`code` 是原因代碼，訊息只有欄位路徑與規則，不含輸出內容。
        TypeError: `output` 不是 `ResearchModelOutput`，或 `context_values` 不是 `ContextValues`
            （程式的錯）。
    """
    if not isinstance(output, ResearchModelOutput):
        raise TypeError("研究模型的驗證只收 ResearchModelOutput")
    if not isinstance(context_values, ContextValues):
        raise TypeError("研究模型的驗證要有情境值清單（ContextValues）")
    # 1. 存得進資料庫。
    check_output_storable(output.model_dump(mode="json"))

    raw_segments = tuple(unwrap_document(block) for block in llm_input.blocks)
    checked_segments = tuple(check_copy(text) for text in raw_segments)
    input_text = "\n".join(checked_segments)
    quote_segments = tuple(quote_key(text) for text in raw_segments)

    # 2. 看不見的字、標記與連結。
    for where, string in _strings(output.model_dump(mode="json")):
        if BIDI_OR_TAG.search(string) or HTML_ENTITY.search(string):
            raise _reject(
                ErrorCode.RESEARCH_MODEL_UNSAFE_TEXT, f"{where} 含雙向控制字元或 HTML 實體"
            )
    for index, construct in enumerate(output.constructs):
        for name in ("name_en", "label_zh"):
            if has_markup(getattr(construct, name), input_text):
                raise _reject(
                    ErrorCode.RESEARCH_MODEL_UNSAFE_TEXT, f"constructs[{index}].{name} 含標記或連結"
                )

    # 3. 英文名、繁中標籤的格式；原句與定義的詞數。
    for index, construct in enumerate(output.constructs):
        where = f"constructs[{index}]"
        _check_name_en(f"{where}.name_en", construct.name_en)
        _check_label_zh(f"{where}.label_zh", construct.label_zh)
        _check_word_count(f"{where}.definition", construct.definition, required=False)
    for index, hypothesis in enumerate(output.hypotheses):
        _check_word_count(f"hypotheses[{index}].sentence", hypothesis.sentence, required=True)

    # 4. 原文欄位要出現在輸入裡（只有標點的選填欄位當沒提到，清成空值並計數）。
    for where, quote in _quote_fields(output):
        text = _quote_text(quote) if not where.endswith(".sentence") else _optional_text(quote)
        if text is None:
            continue
        if not _quote_found(quote_key(text).strip(_QUOTE_TRIM), quote_segments):
            raise _reject(ErrorCode.RESEARCH_MODEL_QUOTE_NOT_IN_INPUT, f"{where} 不在輸入裡")

    # 5、6. 構面不重複；假設指到清單裡的兩個不同構面。
    names: set[str] = set()
    for index, construct in enumerate(output.constructs):
        name = normalize_name(normalize_text(construct.name_en))
        if name in names:
            raise _reject(
                ErrorCode.RESEARCH_MODEL_DUPLICATE_CONSTRUCT, f"constructs[{index}] 和前面同名"
            )
        names.add(name)
    for index, hypothesis in enumerate(output.hypotheses):
        cause = normalize_name(hypothesis.cause)
        effect = normalize_name(hypothesis.effect)
        if cause not in names or effect not in names or cause == effect:
            raise _reject(
                ErrorCode.RESEARCH_MODEL_UNKNOWN_CONSTRUCT,
                f"hypotheses[{index}] 的因或果不是清單裡的構面，或因和果相同",
            )

    # 7. 數字範圍。
    for where, value, is_ratio in _numeric_fields(output):
        if isinstance(value, int):
            ok = 1 <= value <= MAX_INTEGER
        else:
            ok = math.isfinite(value) and (not is_ratio or 0 <= value <= 1)
        if not ok:
            raise _reject(ErrorCode.RESEARCH_MODEL_NUMBER_OUT_OF_RANGE, f"{where} 超出範圍")

    # 8. 編造：數字與英文詞都要在輸入出現。
    vocabulary = InputVocabulary.build(input_text)
    for index, construct in enumerate(output.constructs):
        _check_invented(f"constructs[{index}].name_en", construct.name_en, vocabulary)
        _check_invented(f"constructs[{index}].label_zh", construct.label_zh, vocabulary)
    # 結構化的數字只對照完整量級（「12 million」只有 12000000），負號、科學記號完整讀出。
    sources = structured_numbers(input_text)
    for where, value, _ in _numeric_fields(output):
        if _as_number(value) not in sources:
            raise _reject(ErrorCode.RESEARCH_MODEL_UNSUPPORTED_CONTENT, f"{where} 不是輸入裡的數字")

    # 不拒收：情境值；9. 擷取規則 1～3（衝突才拒收）。
    context, dropped, unsupported = _resolve_context(output, context_values, quote_segments)
    hypotheses, corrected, cleared_p = _hypotheses_content(output, quote_segments, input_text)
    return ResearchModelContent(
        is_review_or_meta_analysis=output.is_review_or_meta_analysis,
        country=context[ContextField.COUNTRY],
        industry=context[ContextField.INDUSTRY],
        research_design=context[ContextField.RESEARCH_DESIGN],
        unit_of_analysis=context[ContextField.UNIT_OF_ANALYSIS],
        sample_size=output.context.sample_size,
        limitations=_quote_text(output.limitations),
        future_research=_quote_text(output.future_research),
        dropped_context_value_count=dropped,
        unsupported_context_value_count=unsupported,
        corrected_support_count=corrected,
        cleared_value_count=cleared_p + _cleared_quote_count(output),
        constructs=tuple(_construct_content(construct) for construct in output.constructs),
        hypotheses=hypotheses,
    )


@dataclass(frozen=True)
class ResearchModelValidator:
    """交給閘門 `LlmJob(validate_output=...)` 的驗證函式（符合 `OutputValidator` 的簽名）。

    輸出不是 `ResearchModelOutput` 就拒收（`llm_output_invalid`，prompt 的 schema 設錯）；是的話跑
    一次 `validate_research_model`（不過就拒收）。
    """

    context_values: ContextValues

    def __call__(self, output: BaseModel | str, llm_input: LlmInput) -> None:
        if not isinstance(output, ResearchModelOutput):
            raise OutputRejectedError(ErrorCode.LLM_OUTPUT_INVALID, "輸出不是 ResearchModelOutput")
        validate_research_model(output, llm_input, self.context_values)
