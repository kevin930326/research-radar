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
   量級、`12M`／`10k` 這種字母縮寫的量級讀不懂不取、科學記號算出完整的值、`= - 0.42` 是
   負數）；英文名的每個英文詞與數字、繁中標籤裡的英文詞與數字都要在輸入出現（2.4 的編造判準）
   → `research_model_unsupported_content`。
9. 同一句（來源句子的雜湊相同：完整版與片段算同一句）、同一條關係（因、果相同）出現兩次而模型
   給的值（證據類型、支持與否、統計數字；不是這裡改過的值）不同
   → `research_model_conflicting_hypothesis`。

**不拒收、改值並計數**：

- 情境值不在清單 → 空值，`dropped_context_value_count`。
- 國家、產業的值在清單，但它的 `terms` 都不在輸入（或只落在 `excluded_terms` 裡，例如北韓全名裡
  的 Republic of Korea）→ 空值，`unsupported_context_value_count`（FR-3 擷取規則 4：不推測）。
- 證據類型不是實證檢定卻判了支持與否（含 `not_stated`）→ `not_determined`；實證檢定卻給
  `not_determined` → `not_stated`；判定了（supported 等）卻沒有可以採信的結果原文 `result_quote`
  （有文字、在輸入裡、寫出了結果、對得上這條關係：提到因或果的名稱或縮寫，或寫了原句的假設代號，
  或說全部假設都怎樣而原句是一個假設；見 `_result_quote`）→ `not_stated`；
  `corrected_support_count`（擷取規則 1、2）。只數存下來的假設。
- p 值不是輸入寫出的確切值（只寫 `p < .01` 這種門檻）→ 空值；選填原文欄位只有標點（`-`）→ 空值；
  `cleared_value_count`。
- 同一句、同一條關係、值都一樣的重複只留第一筆（擷取規則 3）。

沒提到（null、空白）的欄位存空值。判準的細節與抓不到的情況寫在 ADR-0034。
"""

import bisect
import itertools
import math
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, replace
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
# 接數字，不算；`U.S.`、`U.K.`、`e.g.`、`i.e.`、`et al.`、`vs.` 的點也不算（確認審查；段落已經
# casefold）。代價：真的以這些縮寫結尾的句子會和下一句算成同一句。
_SENTENCE_END: Final = re.compile(
    r"(?<!\bu\.s)(?<!\bu\.k)(?<!\be\.g)(?<!\bi\.e)(?<!\bet al)(?<!\bvs)"
    r"[.!?][\"')\]]*(?=\s|$)"
)
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
# is expected to、H1 posits、`H1:`、we test whether、is proposed to…）沒有統計量時不算；`H1:` 後面
# 直接接 supported 這類判定時是結果。
_RESULT_STATISTIC: Final = re.compile(
    r"(?<![a-z0-9])(?:p|β|beta|b|r|r2|t|f|z|χ2|chi2|or|coefficient)\s?(?:<=|>=|[=<>≤≥])"
)
_RESULT_WORD: Final = re.compile(
    r"(?<![a-z])(?:support(?:s|ed)?|confirm(?:s|ed)?|significant(?:ly)?|insignificant"
    r"|non-?significant|found|finds?|findings|show(?:s|ed|n)?|reveal(?:s|ed)?|indicate(?:s|d)?"
    r"|demonstrat(?:e|es|ed)|results?|rejected)(?![a-z])"
)
# 只提出假設的寫法分兩種（合起來就是 2.6 的判準，順序與內容沒變）：
# - 假設句：we hypothesize／expect／predict／propose／posit、is expected／proposed… to、
#   hypothesized that、H1 posits／predicts…、`H1:`（後面直接接判定的除外）。
# - 研究問題：we test／examine／investigate／explore／ask whether。
_HYPOTHESIS_STATEMENT: Final = re.compile(
    r"(?<![a-z])(?:"
    r"(?:we (?:hypothesi[sz]e|expect|predict|propose|posit)"
    r"|(?:is|are) (?:expected|predicted|hypothesi[sz]ed|proposed|posited) to"
    r"|(?:hypothesi[sz]ed|proposed|posited) that"
    r"|h[0-9]+[a-z]? (?:posits|proposes|predicts|states|suggests|argues|expects))(?![a-z])"
    r"|h[0-9]+[a-z]?\s?:(?!\s?(?:(?:is|was|were) )?(?:supported|not supported|partially supported"
    r"|rejected|confirmed)(?![a-z])))"
)
_RESEARCH_QUESTION: Final = re.compile(
    r"(?<![a-z])we (?:test|examine|investigate|explore|ask) whether(?![a-z])"
)
# 2.6 決定 14 選 B（Kevin 2026-11-15）：研究問題後面才接結果的（`we examine whether X and find that
# Y`）也算。研究問題本身常帶 significant、support、find 這類字（`we test whether trust
# significantly increases loyalty`、`whether users find the app useful`、`whether H1 is
# supported`），所以後面的結果要比結果用語更明確，而且要在另一個子句：
# - 明確的結果寫法：find／show／reveal／indicate／demonstrate／confirm… that、was／were（not、
#   partially…）supported／confirmed／rejected、find（no、strong…）support／evidence；
# - 它和研究問題之間要有斷點（逗號、分號、冒號、` - `、句尾、and、but、then），而且最後一個斷點到
#   結果寫法之間只能是空的或一個主詞（we、it、they、results、the findings、data、H1、this
#   hypothesis…，可以再接 also）。
_REPORTED_AFTER_QUESTION: Final = re.compile(
    r"(?<![a-z])(?:"
    r"(?:find|finds|found|show|shows|showed|shown|reveal|reveals|revealed|indicate"
    r"|indicates|indicated|demonstrate|demonstrates|demonstrated|confirm|confirms|confirmed) that"
    r"|(?:is|are|was|were|been) (?:(?:not|partially|fully|strongly|also) )?"
    r"(?:supported|confirmed|rejected)"
    r"|(?:find|finds|found) (?:(?:no|strong|partial|some|mixed|significant) )?(?:support|evidence)"
    r")(?![a-z])"
)
_CLAUSE_BREAK: Final = re.compile(r"[,;:]|\s-\s|[.!?](?=\s|$)|(?<![a-z])(?:and|but|then)(?![a-z])")
# `_REPORT_SUBJECT` 最長的寫法（`the findings also`、`these hypotheses also`）加上前後空白的上限。
_MAX_SUBJECT_CHARS: Final = 40
_REPORT_SUBJECT: Final = re.compile(
    r"(?:(?:we|it|they|h[0-9]+[a-z]?|(?:the|this) hypothesis|(?:the|these|our) hypotheses"
    r"|(?:the |our )?(?:results?|findings|data|analys[ie]s|evidence|estimates))(?: also)?)?"
)


# 寫出結果的子句在結果寫法（或統計量）之後的下一個句點、分號、冒號（或 `!`、`?`）結束（Codex 複查
# R1：一路取到結果原文結尾時，後面的無關敘述——`…, and we find that satisfaction affects loyalty;
# the questionnaire measures PU and trust`——會替另一條關係背書）。後面直接接數字的點（`0.42`、
# `p < .01`）不是句點；結果寫法後面直接接的冒號（`we find that: …`）也不算結束，冒號後面才是找到的
# 內容。
_RESULT_CLAUSE_END: Final = re.compile(r"[;:]|[.!?](?=\s|$)")
_LEADING_COLONS: Final = re.compile(r"[\s:]*")


def _clause_end(quote: str, marker_end: int) -> int:
    """結果寫法結束在 `marker_end` 的子句，結束的位置（見 `_RESULT_CLAUSE_END`；緊接在結果寫法後面的
    冒號與空白先跳過）。"""
    begin = _LEADING_COLONS.match(quote, marker_end)
    found = _RESULT_CLAUSE_END.search(quote, begin.end() if begin else marker_end)
    return found.start() if found else len(quote)


def _result_after_question(
    quote: str, question_end: int, *, by_words: bool
) -> tuple[int, int] | None:
    """研究問題（結束在 `question_end`）後面另一個子句寫出的結果：回（那個子句的開頭＝最後一個斷點
    的結尾, 結果寫法或統計量的結尾），沒有就是 None。`by_words` 時先找明確的結果寫法（見
    `_REPORTED_AFTER_QUESTION` 上面的說明）；再找研究問題後面的統計量，子句開頭是統計量前面最後一個
    斷點（沒有斷點時是研究問題的結尾，例如 `we examine whether PU affects trust (β = 0.42)`）。斷點
    只找一次，每個結果寫法用二分搜尋找它前面最後一個斷點。"""
    break_ends = [match.end() for match in _CLAUSE_BREAK.finditer(quote, question_end)]
    if by_words:
        for marker in _REPORTED_AFTER_QUESTION.finditer(quote, question_end):
            # 結束位置不超過結果寫法開頭的斷點裡，最後一個。
            position = bisect.bisect_right(break_ends, marker.start())
            # 斷點到結果寫法太遠就不可能只是主詞，不切出來看（Codex 主審查第 6 條：同一個斷點後面
            # 接大量結果寫法時，每次都切一段越來越長的字串會變成平方時間）。
            if position == 0 or marker.start() - break_ends[position - 1] > _MAX_SUBJECT_CHARS:
                continue
            subject = quote[break_ends[position - 1] : marker.start()].strip()
            if _REPORT_SUBJECT.fullmatch(subject):
                return break_ends[position - 1], marker.end()
    statistic = _RESULT_STATISTIC.search(quote, question_end)
    if statistic is None:
        return None
    position = bisect.bisect_right(break_ends, statistic.start())
    return (break_ends[position - 1] if position else question_end), statistic.end()


@dataclass(frozen=True, slots=True)
class _ReportedPart:
    """結果原文裡寫出檢定結果的那一段（`quote_key` 寫法裡的位置）。`question` 是那段前面的研究問題
    的開頭（結果寫在研究問題後面時；否則 None）。"""

    start: int
    end: int
    question: int | None = None


def _reported_part(quote: str) -> _ReportedPart | None:
    """`quote`（`quote_key` 的寫法）寫出檢定結果的那一段；沒寫出結果是 None。

    - 沒有研究問題：有統計量，或有結果用語、而且沒有假設句寫法（同 2.6）→ 整段。
    - 有研究問題（we examine whether…；Codex 主審查第 1 條）：最後一個研究問題後面另一個子句寫了
      明確的結果（有假設句寫法時只看統計量）→ 從那個子句的開頭到結果寫法之後的下一個句點、分號或
      冒號（`_clause_end`；Codex 複查 R1），研究問題與後面的句子都不算在裡面，免得它們提到的構面替
      另一條關係的結果背書；研究問題後面沒有、前面有統計量 → 研究問題前面那段。
    """
    questions = list(_RESEARCH_QUESTION.finditer(quote))
    words = _RESULT_WORD.search(quote) is not None and _HYPOTHESIS_STATEMENT.search(quote) is None
    if not questions:
        return _ReportedPart(0, len(quote)) if words or _RESULT_STATISTIC.search(quote) else None
    last = questions[-1]
    found = _result_after_question(quote, last.end(), by_words=words)
    if found is not None:
        start, marker_end = found
        return _ReportedPart(start, _clause_end(quote, marker_end), last.start())
    if _RESULT_STATISTIC.search(quote, 0, last.start()):
        return _ReportedPart(0, last.start())
    return None


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


def _p_value_is_exact(
    value: Decimal, sentence: str, result_quote: str | None, input_text: str
) -> bool:
    """模型給的 p 值是輸入寫出的**確切值**：依序看假設原句、（可以採信的）結果原文、整個輸入，
    第一個寫了這個 p 值的地方算數；寫成 `p = 值`（或 `p: 值`）才算，只寫門檻（`p < .01`）或根本
    沒寫成 p 值都不算。"""
    for text in (check_copy(sentence), check_copy(result_quote or ""), input_text):
        comparators = _p_comparators(value, text)
        if comparators:
            return bool(comparators & _EXACT_P)
    return False


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


# --- 結果原文對得上這條關係（2.6 第 12 點；決定 14 選 B 放寬，Kevin 2026-11-15） ------------------

# 首字母縮寫常跳過的虛詞（和 `normalize_name` 的同一組）。
_SKIPPABLE: Final = frozenset({"a", "an", "and", "for", "in", "of", "on", "the", "to", "with"})
# 縮寫的寫法：字母開頭、2～15 個字母或數字，至少兩個大寫字母（`PU`、`TR`、`eWOM`、`PEOU`）。
# 要兩個大寫，所以 `It`、`Trust`、`H1` 都不是縮寫。
_ABBREVIATION_TOKEN: Final = re.compile(r"(?<![0-9A-Za-z])[A-Za-z][A-Za-z0-9]{1,14}(?![0-9A-Za-z])")
# 輸入裡「名稱 (縮寫)」的括號；名稱取括號前同一段最多 200 個字。
_DEFINED_ABBREVIATION: Final = re.compile(r"\(([A-Za-z][A-Za-z0-9]{1,14})\)")
_DEFINITION_WINDOW: Final = 200
# 最多看輸入裡前 200 個縮寫括號（摘要通常只有幾個；擋住故意塞滿括號的輸入拖慢驗證，多的只是
# 少認得幾個縮寫，錯在少判）。
_MAX_DEFINITIONS: Final = 200
# 假設代號：H1、H2a、H3b（`quote_key` 的寫法，已經 casefold）；H 後面最多兩位數字、最多一個字母。
_HYPOTHESIS_LABEL: Final = re.compile(r"(?<![a-z0-9])h[0-9]{1,2}[a-z]?(?![a-z0-9])")
# 代號與「全部假設」那兩條，結果原文要**直接陳述**結果（Codex 主審查第 2、3 條）：只在整段任何地方
# 有判定字不夠（`H1 was tested to determine whether the effect was significant` 不是結果）。直接陳述
# 是下面三種寫法之一，`{target}` 是代號清單（`H1`、`H1 and H2`、`H1, H2, and H3`）或「全部假設」：
# - `{target}`（可以接一個括號，例如統計量）＋（`:`、is／was／are／were／has been…）＋（not、
#   partially、fully…）＋ supported／confirmed／rejected／accepted／significant…，或
#   `{target}` received／gained（no、strong…）support；
# - 主詞（we、it、they、（the／our）results／findings／data／analysis／evidence／estimates…）＋
#   （also、strongly…）＋ support(s)／supported／confirm…／reject… ＋ `{target}`，或主詞＋
#   find／provide／offer／lend…（no、strong…）support for／to ＋ `{target}`；
# - （strong、partial…）support is／was found／obtained／observed／provided for ＋ `{target}`。
# 每一種都要在**子句開頭**（Codex 複查 R2）：前面只能是段落開頭、`,` `;` `:` `(`、句點，或這些
# 標點後面的 and／but／thus；或在這樣的開頭先寫「本研究的主詞＋find／show／reveal／indicate／
# demonstrate／confirm＋that」（`The results show that H1 was supported`）。其他外層的主詞或動詞
# 一律不算：`We dispute that H1 was supported`、`Other researchers report that all hypotheses were
# supported`、`It is not the case that H1 was supported`、`Prior studies support H1`。
_TARGET_PARENTHETICAL: Final = r"(?: ?\([^()]{0,80}\))?"
_TARGET_VERDICT: Final = (
    r" ?(?:: ?|- )?(?:(?:is|was|are|were|has been|have been) )?"
    r"(?:(?:not|partially|fully|strongly|also|largely) )?"
    r"(?:supported|confirmed|rejected|accepted|significant|non-?significant|insignificant)(?![a-z])"
)
_SUPPORT_AMOUNT: Final = r"(?:(?:no|strong|partial|some|empirical|full|significant|mixed|clear) )?"
_CLAUSE_START: Final = (
    r"(?:^|(?<=[,;:(])|(?<=[,;:(.] )"
    r"|(?<=^and )|(?<=^but )|(?<=^thus )"
    r"|(?<=[,;:.] and )|(?<=[,;:.] but )|(?<=[,;:.] thus )"
    r"|(?<=[,;:]and )|(?<=[,;:]but )|(?<=[,;:]thus ))"
)
# 這篇研究自己的主詞（`Prior findings` 的 findings 不在子句開頭，所以不算）。
_STUDY_SUBJECT: Final = (
    r"(?:we|it|they|(?:(?:the|our|these) (?:(?:empirical|statistical|main|overall|structural)"
    r" )?)?(?:results?|findings|data|analys[ie]s|evidence|estimates|tests?))"
    r"(?: (?:also|strongly|fully|partially|largely|clearly))?"
)
_STUDY_REPORTS_THAT: Final = (
    rf"{_STUDY_SUBJECT} (?:find|finds|found|show|shows|showed|reveal|reveals|revealed|indicate"
    r"|indicates|indicated|demonstrate|demonstrates|demonstrated|confirm|confirms|confirmed) that "
)
_STATEMENT_START: Final = rf"{_CLAUSE_START}(?:{_STUDY_REPORTS_THAT})?"
_HYPOTHESES_WORD: Final = r"(?:(?:the )?hypothes[ie]s )?"


def _direct_result(target: str) -> re.Pattern[str]:
    """直接陳述 `target`（不能有捕捉組）結果的寫法（見上面的說明）；`target` 在每個寫法裡是唯一的
    捕捉組。"""
    return re.compile(
        rf"{_STATEMENT_START}(?:"
        rf"({target}){_TARGET_PARENTHETICAL}{_TARGET_VERDICT}"
        rf"|({target}){_TARGET_PARENTHETICAL} (?:receive[sd]?|gain(?:s|ed)?) {_SUPPORT_AMOUNT}"
        r"support(?![a-z])"
        rf"|{_STUDY_SUBJECT} (?:supports?|supported|confirms?|confirmed|rejects?|rejected) "
        rf"{_HYPOTHESES_WORD}({target})"
        rf"|{_STUDY_SUBJECT} (?:find|finds|found|provides?|provided|offers?|offered|lends?|lent"
        rf"|yields?|yielded|shows?|showed) {_SUPPORT_AMOUNT}support (?:for|to) {_HYPOTHESES_WORD}"
        rf"({target})"
        rf"|{_SUPPORT_AMOUNT}support (?:is|was) (?:also )?(?:found|obtained|observed|provided) for "
        rf"{_HYPOTHESES_WORD}({target})"
        r")"
    )


_LABEL_TEXT: Final = r"h[0-9]{1,2}[a-z]?"
# 代號清單最多 30 個代號（不設上限時，`H1 and H1 and … tested` 每個開頭都吃到結尾再退回來，變成平方
# 時間；超過 30 個的只是後面的代號認不到，錯在少判）。
_LABEL_LIST: Final = (
    rf"(?<![a-z0-9]){_LABEL_TEXT}(?:(?:, ?(?:and )?| and | ?& ?){_LABEL_TEXT}){{0,29}}(?![a-z0-9])"
)
_DIRECT_LABEL_RESULT: Final = _direct_result(_LABEL_LIST)
# 「全部假設」：all hypotheses、all of the hypotheses、all the proposed hypotheses。
_ALL_HYPOTHESES_TEXT: Final = r"(?<![a-z])all (?:of )?(?:the |our )?(?:[a-z]+ )?hypotheses(?![a-z])"
_DIRECT_ALL_RESULT: Final = _direct_result(_ALL_HYPOTHESES_TEXT)
# 代號清單的前後接範圍符號（`H1-H3`、`H1 to H3`、`H1 through H3`）：範圍不展開，兩端也不算。
_RANGE_AFTER: Final = re.compile(r" ?(?:-|to |through ) ?h?[0-9]")
_RANGE_BEFORE: Final = re.compile(r"h?[0-9]{1,2}[a-z]? ?(?:-|to|through) ?$")
# 還沒有結果、或不是這篇研究的結果的敘述（Codex 主審查第 2、3 條）：寫出結果的那段有這些字時，代號
# 與全部假設都不算（`… to determine whether …`、`… to see if …`、`Whether H1 was supported
# remains unknown`、`Future studies will test …`、`In prior work, support was found for H1`）。
# 寧可少判。
_UNSETTLED: Final = re.compile(
    r"(?<![a-z])(?:whether|if|unless|to (?:determine|test|examine|assess|evaluate|investigate"
    r"|explore|see|check|verify|establish|ascertain|identify|confirm|validate)|remains?|remained"
    r"|unclear|unknown|untested|unresolved|yet|future|will|would|could|might|may|should|can"
    r"|prior|previous|previously|earlier|past|literature|extant)(?![a-z])"
)
# 有這些字就不套「全部假設」（`not all`、`all but H3`、`all hypotheses except H2 were supported`、
# `all hypotheses were supported, but…`、`partially`…）：不確定是不是每一條都一樣，保守不判。
_ALL_QUALIFIER: Final = re.compile(
    r"(?<![a-z])(?:not|but|except|exception|excluding|other than|apart from|besides|however"
    r"|although|though|whereas|while|partially|partly|mostly|most|some|only|few|several|none"
    r"|neither|nor|mixed)(?![a-z])"
)


def _is_abbreviation(token: str) -> bool:
    return sum(char.isupper() for char in token) >= 2


def _initials(name_en: str) -> set[str]:
    """英文名的首字母縮寫（casefold）：全部的詞、或跳過虛詞；括號裡的字不算詞；至少 2 個字母。
    連字號也分詞（`Electronic Word-of-Mouth` → `ewom`）。"""
    words = _WORD.findall(check_copy(re.sub(r"\([^()]*\)", " ", name_en)).casefold())
    found = {
        "".join(word[0] for word in pool)
        for pool in (words, [word for word in words if word not in _SKIPPABLE])
    }
    return {initials for initials in found if len(initials) >= 2}


def _without_parentheses(name_en: str) -> str:
    """英文名拿掉括號（和裡面的字）之後的正規化名稱；剩下只有標點時是空字串。"""
    try:
        return normalize_name(re.sub(r"\([^()]*\)", " ", normalize_text(name_en)))
    except ValueError:
        return ""


# 「名稱 (縮寫)」的名稱最多看括號前幾個詞。
_MAX_DEFINED_WORDS: Final = 8


def _abbreviates(abbreviation: str, words: list[str]) -> bool:
    """`abbreviation`（casefold）是 `words`（正規化名稱的詞）的縮寫：第一個詞不是虛詞；每個不是虛詞
    的詞依序用掉縮寫的下一個字（等於它的第一個字母），可以再用掉同一個詞後面照順序出現的字
    （`trust` → `tr`、`loyalty` → `loy`、`electronic wordofmouth` → `ewom`）；虛詞可以用、可以
    不用；縮寫的字剛好用完。"""
    if not words or words[0] in _SKIPPABLE:
        return False
    seen: dict[tuple[int, int], bool] = {}

    def fits(word_index: int, used: int) -> bool:
        if word_index == len(words):
            return used == len(abbreviation)
        key = (word_index, used)
        if key not in seen:
            word = words[word_index]
            reachable: list[int] = []
            if used < len(abbreviation) and abbreviation[used] == word[0]:
                reachable.append(used + 1)
                position = 1
                for char in abbreviation[used + 1 :]:
                    position = word.find(char, position) + 1
                    if position == 0:
                        break
                    reachable.append(reachable[-1] + 1)
            if word in _SKIPPABLE:
                reachable.append(used)
            seen[key] = any(fits(word_index + 1, next_used) for next_used in reachable)
        return seen[key]

    return fits(0, 0)


def _strict_initials(abbreviation: str, words: list[str]) -> bool:
    """`abbreviation`（casefold）是 `words` 的**嚴格首字母**：第一個詞不是虛詞；每個不是虛詞的詞剛好
    貢獻它的第一個字母，虛詞貢獻第一個字母或不貢獻（`system satisfaction` → `ss`、`perceived ease
    of use` → `peou`、`trust in transactions` → `tt`）。"""
    if not words or words[0] in _SKIPPABLE:
        return False
    reachable = {0}
    for word in words:
        following = {
            used + 1
            for used in reachable
            if used < len(abbreviation) and abbreviation[used] == word[0]
        }
        reachable = following | reachable if word in _SKIPPABLE else following
        if not reachable:
            return False
    return len(abbreviation) in reachable


def _defined_name(abbreviation: str, before: str) -> str | None:
    """輸入裡「名稱 (縮寫)」的名稱：括號前面（`before`）正規化後，最後 1～`_MAX_DEFINED_WORDS` 個詞
    裡縮寫得出來的那一段。縮寫字尾多一個小寫 s 也可以（`ICTs`）。

    - 有**嚴格首字母**對得上的（`_strict_initials`）→ 取最長的那一段：`system satisfaction (SS)`
      是 system satisfaction，不是 satisfaction（Opus 確認審查 M2：只取最短的一段時，縮寫字母全部
      落在最後一個詞裡就只剩那個詞，模型漏列較長的名稱時會錯綁）；
    - 沒有嚴格的，只有一段用寬鬆的寫法（`_abbreviates`：同一個詞可以多貢獻後面的字）對得上 → 那
      一段（`trust (TR)`、`loyalty (LOY)`、`electronic word-of-mouth (eWOM)`）；
    - 兩段以上都只有寬鬆的寫法對得上（`sales satisfaction (SAS)`：satisfaction 與 sales
      satisfaction 都對得上）→ 分不出名稱，None。找不到也是 None。"""
    try:
        words = normalize_name(before).split()
    except ValueError:  # 前面只有標點
        return None
    candidates = [abbreviation.casefold()]
    if len(abbreviation) > 2 and abbreviation.endswith("s"):
        candidates.append(abbreviation[:-1].casefold())
    strict: list[str] | None = None
    loose: list[list[str]] = []
    for count in range(1, min(len(words), _MAX_DEFINED_WORDS) + 1):
        tail = words[-count:]
        if any(_strict_initials(candidate, tail) for candidate in candidates):
            strict = tail
        elif any(_abbreviates(candidate, tail) for candidate in candidates):
            loose.append(tail)
    if strict is not None:
        return " ".join(strict)
    return " ".join(loose[0]) if len(loose) == 1 else None


def _construct_abbreviations(
    output: ResearchModelOutput, input_text: str
) -> dict[str, frozenset[str]]:
    """每個構面（正規化名稱）可以代表它的縮寫（casefold）：

    - 英文名的首字母（`Perceived usefulness` → `pu`；`Trust` 只有一個字母，沒有）；但輸入已經把
      這個縮寫定義成別的名稱（`price uncertainty (PU)`，而這個構面不叫 price uncertainty）時不給
      （Opus 確認審查 L2）；
    - 輸入裡寫了「名稱 (縮寫)」，而且那個名稱（`_defined_name`）和這個構面的名稱**完全一樣**
      （正規化名稱，或英文名拿掉括號後的正規化名稱：`Trust (TR)` 也看 `trust`）。Codex 主審查
      第 4 條：只看「括號前面以構面名稱結尾」時，模型漏列較長的名稱（`satisfaction with trust
      (ST)` 而模型只列 Trust）就會把 ST 錯給 Trust；現在 ST 的名稱是 satisfaction with trust，
      和 Trust 不一樣，不給。

    縮寫一定要出現在輸入裡（首字母的縮寫也要結果原文或原句真的寫出來才用得上）；模型自己在英文名
    括號裡寫的縮寫不算，免得模型改名字就能把別的句子算成這條關係的結果。
    """
    initials: dict[str, set[str]] = {}
    spellings: dict[str, set[str]] = {}
    owners: dict[str, set[str]] = {}
    for construct in output.constructs:
        name = normalize_name(normalize_text(construct.name_en))
        initials.setdefault(name, set()).update(_initials(construct.name_en))
        for spelled in (name, _without_parentheses(construct.name_en)):
            if spelled:
                spellings.setdefault(name, set()).add(spelled)
                owners.setdefault(spelled, set()).add(name)
    definitions = (
        match for match in _DEFINED_ABBREVIATION.finditer(input_text) if _is_abbreviation(match[1])
    )
    # 縮寫（casefold）→ 輸入把它定義成的名稱。
    defined_names: dict[str, set[str]] = {}
    for match in itertools.islice(definitions, _MAX_DEFINITIONS):
        line_start = input_text.rfind("\n", 0, match.start()) + 1
        before = input_text[max(line_start, match.start() - _DEFINITION_WINDOW) : match.start()]
        defined = _defined_name(match[1], before)
        if defined is not None:
            defined_names.setdefault(match[1].casefold(), set()).add(defined)
    found: dict[str, set[str]] = {}
    for name, own in initials.items():
        # 輸入把這個首字母定義成別的名稱時，不從首字母給（Opus 確認審查 L2；輸入的定義優先）。
        found[name] = {
            abbreviation
            for abbreviation in own
            if not defined_names.get(abbreviation, set()) - spellings.get(name, set())
        }
    for abbreviation, names in defined_names.items():
        for defined in names:
            for name in owners.get(defined, ()):
                found[name].add(abbreviation)
    return {name: frozenset(abbreviations) for name, abbreviations in found.items()}


def _mentions_construct(
    text: str, construct: str, abbreviations: dict[str, frozenset[str]]
) -> bool:
    """`text` 提到這個構面（正規化名稱）：名稱 `normalize_name` 之後整個詞對得上，或用了它的縮寫
    （`_construct_abbreviations`；`text` 裡要寫成至少兩個大寫字母的樣子）。"""
    if not construct:
        return False
    try:
        name = f" {normalize_name(text)} "
    except ValueError:  # 只有標點的字串；`_quote_text` 已擋下，保險起見
        return False
    if f" {construct} " in name:
        return True
    known = abbreviations.get(construct, frozenset())
    return any(
        _is_abbreviation(token) and token.casefold() in known
        for token in _ABBREVIATION_TOKEN.findall(check_copy(text))
    )


def _mentions_relation(
    text: str, cause: str, effect: str, abbreviations: dict[str, frozenset[str]]
) -> bool:
    """`text` 提到這條關係的因或果（名稱或縮寫，見 `_mentions_construct`）。"""
    return any(_mentions_construct(text, construct, abbreviations) for construct in (cause, effect))


def _sentence_label(sentence: str) -> str | None:
    """假設原句裡的代號：剛好只有一個（可以出現多次）時回它，沒有或有好幾個不同的回 None（好幾個
    時分不出這條關係是哪一個，保守不判）。"""
    labels = set(_HYPOTHESIS_LABEL.findall(quote_key(sentence)))
    return labels.pop() if len(labels) == 1 else None


def _directly_reported(pattern: re.Pattern[str], quote: str) -> list[str]:
    """`quote` 裡直接陳述結果的目標（代號清單或「全部假設」的原文）；前後接範圍符號的代號清單不算。
    寫出結果的那段有還沒有結果的敘述（`_UNSETTLED`）時一個都不算。"""
    if _UNSETTLED.search(quote):
        return []
    found: list[str] = []
    for match in pattern.finditer(quote):
        index = next(group for group in range(1, (pattern.groups or 0) + 1) if match[group])
        start, end = match.span(index)
        if _RANGE_AFTER.match(quote, end) or _RANGE_BEFORE.search(
            quote[max(0, start - 12) : start]
        ):
            continue
        found.append(match[index])
    return found


def _matches_label(quote: str, sentence: str) -> bool:
    """寫出結果的那段直接陳述了這條關係的代號的結果：原句只有一個代號，而且它在直接陳述結果的代號
    清單裡（`H1 was supported`、`Results support H1 and H2`、`We find support for H1`；見
    `_direct_result`）。代號要完全一樣（H1 不等於 H1a、H10）；`H1-H3` 這種範圍不展開，兩端也
    不算。"""
    label = _sentence_label(sentence)
    if label is None:
        return False
    return any(
        label in _HYPOTHESIS_LABEL.findall(labels)
        for labels in _directly_reported(_DIRECT_LABEL_RESULT, quote)
    )


def _matches_all_hypotheses(quote: str, sentence: str) -> bool:
    """寫出結果的那段直接陳述「全部假設」的結果（`All hypotheses were supported`、`The results
    support all hypotheses`；見 `_direct_result`），而且這條關係的原句是一個假設：原句有代號，或有
    假設句的寫法（we hypothesize、is expected to、H1 posits…；只寫 `we examine whether` 的研究問題
    不算）。有 not、except、but、partially 這類字時不套。"""
    if _ALL_QUALIFIER.search(quote) is not None or not _directly_reported(
        _DIRECT_ALL_RESULT, quote
    ):
        return False
    key = quote_key(sentence)
    return bool(_HYPOTHESIS_LABEL.search(key) or _HYPOTHESIS_STATEMENT.search(key))


# 研究問題後面寫出結果的子句只用這些字（代名詞、判定字、統計量，沒有任何構面名稱或代號）時，算是指回
# 研究問題講的那條關係（`we examine whether X affects Y, and we find that it does`）；有別的字
# （`… and we find that satisfaction affects loyalty`、`… and H2 was supported`）就不看研究問題。
_REFERS_BACK: Final = frozenset(
    {
        "we", "it", "they", "this", "these", "that", "those", "the", "our", "a", "an", "also",
        "so", "indeed", "then", "and", "does", "do", "did", "is", "are", "was", "were", "be",
        "been", "has", "have", "had", "holds", "hold", "held", "true", "case", "not", "no",
        "find", "finds", "found", "show", "shows", "showed", "shown", "reveal", "reveals",
        "revealed", "indicate", "indicates", "indicated", "demonstrate", "demonstrates",
        "demonstrated", "confirm", "confirms", "confirmed", "supported", "rejected", "support",
        "evidence", "strong", "strongly", "partial", "partially", "fully", "some", "mixed",
        "significant", "significantly", "statistically", "positive", "positively", "negative",
        "negatively", "hypothesis", "relationship", "relation", "effect", "link", "association",
        "result", "results", "findings", "data", "analysis", "analyses", "estimates", "for", "as",
        "expected", "β", "beta", "b", "p", "r", "r2", "t", "f", "z",
    }
)  # fmt: skip


_WHETHER: Final = re.compile(r"(?<![a-z])whether(?![a-z])")


def _refers_back(clause: str) -> bool:
    return all(word in _REFERS_BACK or word.isdigit() for word in _WORD.findall(clause))


def _result_quote(
    hypothesis: HypothesisOutput,
    quote_segments: tuple[str, ...],
    cause: str,
    effect: str,
    abbreviations: dict[str, frozenset[str]],
) -> str | None:
    """可以採信的結果原文：有文字、正規化後在輸入裡、寫出了檢定結果（`_reported_part`），而且寫出
    結果的那段（有研究問題時不含研究問題）對得上這條關係（`cause`、`effect` 已正規化），下面任何
    一種：

    1. 提到這條關係的因或果：名稱或縮寫（`_mentions_relation`）；
    2. 寫了這條關係的假設代號（`_matches_label`）；
    3. 說全部假設都怎樣，而且這條關係是一個假設（`_matches_all_hypotheses`）。

    2、3 另外要原句**同時**提到這條關係的因和果（名稱或縮寫），確定「這條關係就是那個假設」
    （只提到其中一個時，可能是同一個構面的另一條假設）。
    """
    text = _quote_text(hypothesis.result_quote)
    if text is None:
        return None
    quote = quote_key(text).strip(_QUOTE_TRIM)
    if not _quote_found(quote, quote_segments):
        return None
    reported = _reported_part(quote)
    if reported is None:
        return None
    start, end = reported.start, reported.end
    # 同樣位置、保留大小寫的寫法（認縮寫要看大寫）。casefold 改變長度時（少見的字）位置對不上，只用
    # casefold 的寫法：縮寫認不出來，錯在少判。
    cased = check_copy(text).translate(_STRAIGHT_QUOTES).strip(_QUOTE_TRIM)
    if len(cased) != len(quote):
        cased = quote
    if _mentions_relation(cased[start:end], cause, effect, abbreviations):
        return text
    # 研究問題後面的結果只用代名詞指回去時（`_REFERS_BACK`），看那個研究問題提到的關係；研究問題裡
    # 有第二個 whether（`… whether X affects Y and whether Z affects W, and find that it does`）時
    # 分不出指的是哪一個，不看。
    if (
        reported.question is not None
        and _refers_back(quote[start:end])
        and len(_WHETHER.findall(quote, reported.question, start)) == 1
        and _mentions_relation(cased[reported.question : start], cause, effect, abbreviations)
    ):
        return text
    sentence = hypothesis.sentence
    if not all(
        _mentions_construct(sentence, construct, abbreviations) for construct in (cause, effect)
    ):
        return None
    part = quote[start:end].strip(_QUOTE_TRIM)
    if _matches_label(part, sentence) or _matches_all_hypotheses(part, sentence):
        return text
    return None


@dataclass(frozen=True, slots=True)
class _KeptHypothesis:
    """存下來的一條假設和它的計數（改了支持與否、清了幾個值）；`quote_downgraded`：因為沒有可以
    採信的結果原文才改成 `not_stated`。"""

    content: HypothesisContent
    changed: bool
    cleared: int
    quote_downgraded: bool


def _hypotheses_content(
    output: ResearchModelOutput, quote_segments: tuple[str, ...], input_text: str
) -> tuple[tuple[HypothesisContent, ...], int, int]:
    """改過支持與否、清掉只寫門檻的 p 值、去掉重複的假設，和（改了幾條支持與否, 清了幾個值）
    （只數存下來的）。

    - 判定了支持與否（擷取規則 1、2 改過之後）卻沒有可以採信的結果原文 → `not_stated`（擷取規則
      1「報告了結果才判定」），算進改了幾條。
    - p 值不是輸入寫出的確切值（只寫 `p < .01` 這種門檻）→ 空值；效果類型只有標點 → 空值；都算進
      清了幾個。
    - 同一句（來源句子相同，片段與完整版算同一句）、同一條關係只留一筆：模型給的值一樣時留第一
      筆，但第一筆的結果原文不能採信、後面的可以時換成後面那筆（序號不變）。

    Raises:
        OutputRejectedError: 同一句、同一條關係，模型給的值（證據類型、支持與否、統計數字）互相
            衝突；比的是模型原本給的值，不是這裡改過的值。
    """
    kept: list[_KeptHypothesis] = []
    seen: dict[tuple[str, str, str], tuple[tuple[object, ...], int]] = {}
    abbreviations = _construct_abbreviations(output, input_text)
    for index, hypothesis in enumerate(output.hypotheses):
        cause = normalize_name(hypothesis.cause)
        effect = normalize_name(hypothesis.effect)
        support, changed = _corrected_support(hypothesis)
        result_quote = None
        quote_downgraded = False
        if support in DECIDED_SUPPORT:
            result_quote = _result_quote(hypothesis, quote_segments, cause, effect, abbreviations)
            if result_quote is None:
                support, changed, quote_downgraded = SupportLevel.NOT_STATED, True, True
        statistics = hypothesis.statistics
        beta = _optional_decimal(None if statistics is None else statistics.beta)
        p_value = _optional_decimal(None if statistics is None else statistics.p_value)
        p_cleared = p_value is not None and not _p_value_is_exact(
            p_value, hypothesis.sentence, result_quote, input_text
        )
        if p_cleared:
            p_value = None
        sample_size = None if statistics is None else statistics.sample_size
        raw_effect_type = None if statistics is None else statistics.effect_type
        effect_type = _quote_text(raw_effect_type)
        sentence = normalize_text(hypothesis.sentence)
        source = _source_sentence(quote_key(sentence).strip(_QUOTE_TRIM), quote_segments)
        sentence_sha256 = hypothesis_sentence_sha256(source)
        key = (sentence_sha256, cause, effect)
        original = (
            hypothesis.evidence_type,
            hypothesis.support,
            None if statistics is None else statistics.beta,
            None if statistics is None else statistics.p_value,
            sample_size,
            _optional_text(raw_effect_type),
        )
        entry = _KeptHypothesis(
            content=HypothesisContent(
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
            ),
            changed=changed,
            cleared=int(p_cleared) + int(_is_punctuation_only(raw_effect_type)),
            quote_downgraded=quote_downgraded,
        )
        if key in seen:
            first_values, position = seen[key]
            if first_values != original:
                raise _reject(
                    ErrorCode.RESEARCH_MODEL_CONFLICTING_HYPOTHESIS,
                    f"hypotheses[{index}] 和前面同一句、同一條關係的值不一樣",
                )
            if kept[position].quote_downgraded and not quote_downgraded:
                ordinal = kept[position].content.ordinal
                kept[position] = replace(entry, content=replace(entry.content, ordinal=ordinal))
            continue
        seen[key] = (original, len(kept))
        kept.append(entry)
    return (
        tuple(entry.content for entry in kept),
        sum(entry.changed for entry in kept),
        sum(entry.cleared for entry in kept),
    )


def _cleared_quote_count(output: ResearchModelOutput) -> int:
    """論文與構面層級只有標點、清成空值的選填原文欄位數（假設的效果類型在 `_hypotheses_content`
    數，只數存下來的；假設原句必填、另外要 3 個詞，不在這裡）。"""
    values: list[str | None] = [output.limitations, output.future_research]
    for construct in output.constructs:
        values.append(construct.definition)
        if construct.measurement is not None:
            values += [construct.measurement.scale_source, construct.measurement.item_location]
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
    hypotheses, corrected, cleared = _hypotheses_content(output, quote_segments, input_text)
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
        cleared_value_count=cleared + _cleared_quote_count(output),
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
