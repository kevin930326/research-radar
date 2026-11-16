"""人工標註金標準的試算表（PRD FR-41、FR-3、AR-63、Story 2.13、ADR-0045）的 domain 部分：抽樣、
三張表的版面、產生與讀回試算表、要印的文字。不碰資料庫、不碰檔案系統。

- **抽樣**（`draw_annotation_sample`）：候選＝算數論文（`counted_paper`）而且至少一個 DOI。先從
  有目前摘要的候選抽摘要層級，再從剩下的全部（含沒有摘要的）抽分類專用。分層只依（期刊
  ISSN-L, 出版年），各層名額用最大餘數法依候選數比例分配（`allocate_strata`，同分用 seed 的
  雜湊決定）；層內先排有效主題和興趣主題有交集的，再排其他，兩群各自依
  `annotation_sample_sha256(seed, "rank", DOI)` 排序。抽到的論文最後一律依同一個雜湊排序（列的
  順序不洩漏 AI 主題）。不用 `random`、不用 `hash()`。
- **版面**（`annotator_a_columns`、`annotator_b_columns`）：論文、構面、假設三張表的欄名與每欄能填
  什麼；選項值來自分類表、情境值清單與 FR-3 schema 的列舉，不寫死。多選的主題與方法每個 `key`
  一欄、勾 `yes`。試算表裡沒有任何 AI 產出。
- **產生**（`build_annotator_a_workbook`、`build_annotator_b_workbook`）：xlsx（`domain/xlsx.py`），
  有選項的欄一律資料驗證（下拉清單、`errorStyle="stop"`），數字欄 whole／decimal 驗證；另附
  `說明` 表。
- **讀回**：`read_sampled_papers` 只讀 A 論文表預填的欄（補抽與產生 B 用）；
  `parse_annotation_workbook` 是 Epic 8 匯入要直接用的解析函式：所有問題收齊後一次丟
  `AnnotationParseError`（工作表、Excel 列號、欄名＋欄字母、原因），訊息不帶儲存格內容。
- **DOI 欄**：一篇有多個 DOI 時寫成以「; 」分隔的全部 DOI（`split` 照 ADR-0042 由全部 DOI 算：
  任何一個在測試組就是測試組）。
"""

import re
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from fractions import Fraction
from typing import Final

from radar.domain.context_values import ContextField, ContextValues
from radar.domain.eval_split import EvalSplit, split_for_dois
from radar.domain.fingerprints import annotation_sample_sha256
from radar.domain.identifiers import InvalidIdentifierError, normalize_doi
from radar.domain.llm_schemas import EvidenceType, SupportLevel
from radar.domain.taxonomy import UNCLASSIFIED, Taxonomy, TaxonomyField
from radar.domain.text_table import render_table
from radar.domain.xlsx import (
    CellValue,
    ColumnValidation,
    ListRule,
    NumberRule,
    RangeListRule,
    Sheet,
    SheetValues,
    ValidationRule,
    WriteValue,
    XlsxError,
    clean_cell_text,
    column_letter,
    read_workbook,
    write_workbook,
)

# PRD FR-41 的篇數。
DEFAULT_ABSTRACT_COUNT: Final = 50
DEFAULT_CLASSIFICATION_COUNT: Final = 100
DEFAULT_SECOND_ANNOTATOR_COUNT: Final = 15

PAPERS_SHEET: Final = "論文"
CONSTRUCTS_SHEET: Final = "構面"
HYPOTHESES_SHEET: Final = "假設"
GUIDE_SHEET: Final = "說明"
# 構面與假設表預留資料驗證的列數：論文數 × 20，至少 200 列。
DETAIL_ROWS_PER_PAPER: Final = 20
MIN_DETAIL_ROWS: Final = 200
# 問題清單最多印幾條。
MAX_PRINTED_PROBLEMS: Final = 200
# 整數欄的上限（PostgreSQL `integer`，和研究模型的樣本數、題數一樣）。
MAX_INTEGER: Final = 2_147_483_647

YES: Final = "yes"
NO: Final = "no"
DONE: Final = "done"
DOI_SEPARATOR: Final = "; "
TOPIC_PREFIX: Final = "topic:"
METHOD_PREFIX: Final = "method:"

# 欄名（FR-3 schema 的欄位名就是試算表欄名）。
PAPER_ID: Final = "paper_id"
DOI: Final = "doi"
TITLE: Final = "title"
ABSTRACT: Final = "abstract"
SPLIT: Final = "split"
SET: Final = "set"
FULL_TEXT: Final = "full_text"
ANNOTATOR: Final = "annotator"
STATUS: Final = "status"
IS_REVIEW: Final = "is_review_or_meta_analysis"
SAMPLE_SIZE: Final = "sample_size"
LIMITATIONS: Final = "limitations"
FUTURE_RESEARCH: Final = "future_research"
NAME_EN: Final = "name_en"
LABEL_ZH: Final = "label_zh"
DEFINITION: Final = "definition"
SCALE_SOURCE: Final = "scale_source"
ITEM_LOCATION: Final = "item_location"
ITEM_COUNT: Final = "item_count"
CRONBACH_ALPHA: Final = "cronbach_alpha"
COMPOSITE_RELIABILITY: Final = "composite_reliability"
AVE: Final = "ave"
SENTENCE: Final = "sentence"
CAUSE: Final = "cause"
EFFECT: Final = "effect"
EVIDENCE_TYPE: Final = "evidence_type"
SUPPORT: Final = "support"
RESULT_QUOTE: Final = "result_quote"
BETA: Final = "beta"
P_VALUE: Final = "p_value"
EFFECT_TYPE: Final = "effect_type"
IN_ABSTRACT: Final = "in_abstract"

# A 論文表的研究模型欄（分類專用的論文不能填）。
PAPER_MODEL_COLUMNS: Final = (
    IS_REVIEW,
    *(field.value for field in ContextField),
    SAMPLE_SIZE,
    LIMITATIONS,
    FUTURE_RESEARCH,
)
# 產生時預填、讀回時才看的欄（其他欄有任何值就算「已經開始標註」）。
PREFILLED_COLUMNS: Final = frozenset({PAPER_ID, DOI, TITLE, ABSTRACT, SPLIT, SET, ANNOTATOR})

_NUMERIC_TEXT: Final = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_ZERO: Final = Decimal(0)
# 文字欄遇到數字格時，指數超過這個就不展開（`format(Decimal("1E+300000000"), "f")` 會吃光記憶體）。
_MAX_TEXT_EXPONENT: Final = 64
# DOI 欄的切法：「分號後面接空白」或空白（DOI 本身可以含分號）。
_DOI_SPLIT: Final = re.compile(r";\s+|\s+")
_ONE: Final = Decimal(1)


class Annotator(StrEnum):
    """標註者代號（不寫姓名）。"""

    A = "A"
    B = "B"


class SampleSet(StrEnum):
    """論文在哪一組：摘要層級（主題、方法、研究模型）或分類專用（只標主題與方法）。"""

    ABSTRACT = "abstract"
    CLASSIFICATION_ONLY = "classification_only"


class AnnotationLevel(StrEnum):
    """標註層級（Epic 8 依層級評估）：摘要層級、全文層級（摘要層級裡勾了 full_text）、分類專用。"""

    ABSTRACT = "abstract"
    FULL_TEXT = "full_text"
    CLASSIFICATION_ONLY = "classification_only"


_SET_LABEL: Final = {SampleSet.ABSTRACT: "摘要層級", SampleSet.CLASSIFICATION_ONLY: "分類專用"}
_SPLIT_LABEL: Final = {EvalSplit.TUNING: "調整組", EvalSplit.TEST: "測試組"}
_LEVEL_LABEL: Final = {
    AnnotationLevel.ABSTRACT: "摘要層級",
    AnnotationLevel.FULL_TEXT: "全文層級",
    AnnotationLevel.CLASSIFICATION_ONLY: "分類專用",
}


def detail_row_count(paper_count: int) -> int:
    """構面與假設表預留資料驗證的列數。"""
    return max(paper_count * DETAIL_ROWS_PER_PAPER, MIN_DETAIL_ROWS)


def _check_seed(seed: int) -> None:
    if type(seed) is not int:
        raise ValueError("seed 要是整數")


def _check_count(name: str, value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} 要是 0 以上的整數")


def check_interest(interest: Iterable[str], taxonomy: Taxonomy) -> tuple[str, ...]:
    """興趣主題：去重、排序；每個都要是分類表的主題 `key`。

    Raises:
        ValueError: 有不是主題 `key` 的值（訊息只列出可以用的 `key`）。
    """
    keys = set(taxonomy.keys(TaxonomyField.TOPIC))
    wanted = set(interest)
    if not wanted <= keys:
        raise ValueError(f"--interest 只能是分類表的主題 key：{'、'.join(sorted(keys))}")
    return tuple(sorted(wanted))


# --- 抽樣 ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnnotationPaperFacts:
    """一篇算數論文抽樣要看的事實（repo 逐頁讀出；不含標題與摘要）。"""

    paper_id: uuid.UUID
    journal_issn_l: str | None
    publication_year: int | None
    has_abstract: bool
    dois: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnnotationPaperText:
    """產生試算表要的論文文字：標題、目前的摘要（沒有是 None）、DOI。"""

    title: str
    abstract: str | None
    dois: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnnotationCandidate:
    """抽樣的候選：`doi` 是最小的正規化 DOI，`dois` 是全部（排好序）；`topics` 是有效主題
    （還沒有分類結果是空的）。"""

    paper_id: uuid.UUID
    doi: str
    dois: tuple[str, ...]
    journal_issn_l: str | None
    year: int | None
    has_abstract: bool
    topics: tuple[str, ...]

    @classmethod
    def from_facts(
        cls, facts: AnnotationPaperFacts, topics: Iterable[str] = ()
    ) -> AnnotationCandidate | None:
        """由 repo 讀出的事實組候選；沒有 DOI 回 None（不抽）。

        Raises:
            InvalidIdentifierError: 有看不懂的 DOI。
        """
        dois = tuple(sorted({normalize_doi(doi) for doi in facts.dois}))
        if not dois:
            return None
        return cls(
            paper_id=facts.paper_id,
            doi=dois[0],
            dois=dois,
            journal_issn_l=facts.journal_issn_l,
            year=facts.publication_year,
            has_abstract=facts.has_abstract,
            topics=tuple(sorted(set(topics))),
        )

    @property
    def stratum(self) -> str:
        """分層的鍵：`"<ISSN-L>:<出版年>"`。"""
        year = "" if self.year is None else str(self.year)
        return f"{self.journal_issn_l or ''}:{year}"


def allocate_strata(sizes: Mapping[str, int], total: int, seed: int) -> dict[str, int]:
    """最大餘數法：依各層候選數的比例分配 `total` 個名額。先給每層比例的整數部分，剩下的名額依
    小數部分由大到小給（同分依 `annotation_sample_sha256(seed, "stratum", 層)` 排，再比層的名稱）。
    `total` 不小於全部候選數時每層全拿；每層不會超過該層的候選數。

    Raises:
        ValueError: `total`、某層的數量不是 0 以上的整數，或 `seed` 不是整數。
    """
    _check_seed(seed)
    _check_count("total", total)
    for size in sizes.values():
        _check_count("每層的數量", size)
    population = sum(sizes.values())
    if total >= population:
        return dict(sizes)
    quotas: dict[str, int] = {}
    remainders: dict[str, Fraction] = {}
    for key, size in sizes.items():
        exact = Fraction(size * total, population)
        quotas[key] = exact.numerator // exact.denominator
        remainders[key] = exact - quotas[key]
    left = total - sum(quotas.values())
    order = sorted(
        sizes,
        key=lambda key: (-remainders[key], annotation_sample_sha256(seed, "stratum", key), key),
    )
    for key in order[:left]:
        quotas[key] += 1
    return quotas


@dataclass(frozen=True)
class AnnotationDraw:
    """`draw_annotation_sample` 的結果：兩組抽到的候選（依雜湊排序）與各池的候選數。"""

    abstract: tuple[AnnotationCandidate, ...]
    classification: tuple[AnnotationCandidate, ...]
    abstract_pool: int
    classification_pool: int


def _pick(
    pool: Sequence[AnnotationCandidate],
    count: int,
    seed: int,
    interest: frozenset[str],
    rank: Mapping[uuid.UUID, str],
) -> list[AnnotationCandidate]:
    strata: dict[str, list[AnnotationCandidate]] = {}
    for candidate in pool:
        strata.setdefault(candidate.stratum, []).append(candidate)
    quotas = allocate_strata({key: len(members) for key, members in strata.items()}, count, seed)
    picked: list[AnnotationCandidate] = []
    for key, members in strata.items():
        members.sort(
            key=lambda candidate: (
                not interest.intersection(candidate.topics),
                rank[candidate.paper_id],
                candidate.doi,
            )
        )
        picked.extend(members[: quotas[key]])
    picked.sort(key=lambda candidate: (rank[candidate.paper_id], candidate.doi))
    return picked


def draw_annotation_sample(
    candidates: Iterable[AnnotationCandidate],
    *,
    seed: int,
    interest: Iterable[str],
    abstract_count: int,
    classification_count: int,
    exclude_ids: Iterable[uuid.UUID] = (),
    exclude_dois: Iterable[str] = (),
) -> AnnotationDraw:
    """抽摘要層級 `abstract_count` 篇（只從有摘要的）與分類專用 `classification_count` 篇（從剩下
    的全部）；兩組不重疊。`exclude_ids`／`exclude_dois` 裡的論文（補抽時既有試算表的）不抽。和
    候選的輸入順序無關。

    Raises:
        ValueError: `seed` 不是整數，或數量不是 0 以上的整數。
    """
    _check_seed(seed)
    _check_count("abstract_count", abstract_count)
    _check_count("classification_count", classification_count)
    excluded_ids = frozenset(exclude_ids)
    excluded_dois = frozenset(exclude_dois)
    wanted = frozenset(interest)
    pool = [
        candidate
        for candidate in candidates
        if candidate.paper_id not in excluded_ids and not excluded_dois.intersection(candidate.dois)
    ]
    rank = {
        candidate.paper_id: annotation_sample_sha256(seed, "rank", candidate.doi)
        for candidate in pool
    }
    abstract_pool = [candidate for candidate in pool if candidate.has_abstract]
    abstract = _pick(abstract_pool, abstract_count, seed, wanted, rank)
    chosen = {candidate.paper_id for candidate in abstract}
    rest = [candidate for candidate in pool if candidate.paper_id not in chosen]
    classification = _pick(rest, classification_count, seed, wanted, rank)
    return AnnotationDraw(
        abstract=tuple(abstract),
        classification=tuple(classification),
        abstract_pool=len(abstract_pool),
        classification_pool=len(rest),
    )


# --- 試算表的列 ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnnotationRow:
    """論文表預填的一列：論文 ID、DOI（全部、排好序）、標題、摘要、組別、哪一組。"""

    paper_id: uuid.UUID
    dois: tuple[str, ...]
    title: str
    abstract: str | None
    split: EvalSplit
    sample_set: SampleSet

    @property
    def doi(self) -> str:
        """最小的 DOI。"""
        return self.dois[0]


def annotation_row(
    candidate: AnnotationCandidate, text: AnnotationPaperText, sample_set: SampleSet
) -> AnnotationRow:
    """抽到的候選＋文字 → 一列（組別由候選的全部 DOI 算）。"""
    return AnnotationRow(
        paper_id=candidate.paper_id,
        dois=candidate.dois,
        title=text.title,
        abstract=text.abstract,
        split=split_for_dois(candidate.dois),
        sample_set=sample_set,
    )


def merge_annotation_rows(
    existing: Sequence[AnnotationRow], new: Sequence[AnnotationRow]
) -> tuple[AnnotationRow, ...]:
    """補抽後的列：摘要層級在前、分類專用在後；同一組裡既有列照原順序在前，新列（已依雜湊排序）
    接在後面。"""
    merged: list[AnnotationRow] = []
    for sample_set in SampleSet:
        merged += [row for row in existing if row.sample_set is sample_set]
        merged += [row for row in new if row.sample_set is sample_set]
    return tuple(merged)


@dataclass(frozen=True)
class TopicCounts:
    """各組的主題計數：`by_set` 是主題 `key`（含 `unclassified`）→ 篇數；`pending` 是還沒有分類
    結果的篇數。一篇可以算進多個主題。"""

    by_set: Mapping[SampleSet, Mapping[str, int]]
    pending: Mapping[SampleSet, int]


def count_topics(
    rows: Sequence[AnnotationRow], topics_by_paper: Mapping[uuid.UUID, Sequence[str]]
) -> TopicCounts:
    """依有效主題計數：沒有分類結果 → `pending`；有分類結果但沒有主題（或只有 `unclassified`）→
    `unclassified`。"""
    by_set: dict[SampleSet, Counter[str]] = {sample_set: Counter() for sample_set in SampleSet}
    pending: Counter[SampleSet] = Counter()
    for row in rows:
        topics = topics_by_paper.get(row.paper_id)
        if topics is None:
            pending[row.sample_set] += 1
            continue
        keys = {key for key in topics if key != UNCLASSIFIED}
        by_set[row.sample_set].update(keys or {UNCLASSIFIED})
    return TopicCounts(
        by_set={sample_set: dict(counter) for sample_set, counter in by_set.items()},
        pending={sample_set: pending[sample_set] for sample_set in SampleSet},
    )


@dataclass(frozen=True)
class AnnotationSampleResult:
    """一次產生（或補抽）A 試算表的結果：試算表的全部列與要印的數字。"""

    seed: int
    interest: tuple[str, ...]
    rows: tuple[AnnotationRow, ...]
    existing_count: int
    requested: Mapping[SampleSet, int]
    drawn: Mapping[SampleSet, int]
    pool: Mapping[SampleSet, int]
    skipped_without_doi: int
    topic_counts: TopicCounts


# --- 版面 ---------------------------------------------------------------------------------


class ColumnKind(StrEnum):
    """一欄能填什麼。"""

    TEXT = "text"
    CHOICE = "choice"
    WHOLE = "whole"
    DECIMAL = "decimal"
    PAPER_REF = "paper_ref"  # 論文表的 paper_id
    CONSTRUCT_REF = "construct_ref"  # 構面表的 name_en（同一篇）


@dataclass(frozen=True, slots=True)
class AnnotationColumn:
    """一欄：名稱、種類、意思；選項（`options`，`labels` 是說明表裡每個選項的顯示名稱與定義）、
    數字範圍、欄寬、要不要自動換行、什麼時候填。"""

    name: str
    kind: ColumnKind
    meaning: str
    options: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    width: float = 14.0
    wrap: bool = False
    when: str = ""


@dataclass(frozen=True)
class AnnotationLayout:
    """一位標註者的版面：三張表各自的欄。"""

    annotator: Annotator
    papers: tuple[AnnotationColumn, ...]
    constructs: tuple[AnnotationColumn, ...]
    hypotheses: tuple[AnnotationColumn, ...]

    def sheets(self) -> tuple[tuple[str, tuple[AnnotationColumn, ...]], ...]:
        """（工作表名稱, 欄）照試算表裡的順序。"""
        return (
            (PAPERS_SHEET, self.papers),
            (CONSTRUCTS_SHEET, self.constructs),
            (HYPOTHESES_SHEET, self.hypotheses),
        )


def _text(
    name: str, meaning: str, *, width: float = 18.0, wrap: bool = False, when: str = ""
) -> AnnotationColumn:
    return AnnotationColumn(name, ColumnKind.TEXT, meaning, width=width, wrap=wrap, when=when)


def _choice(
    name: str,
    meaning: str,
    options: Sequence[str],
    labels: Sequence[str] = (),
    *,
    width: float = 14.0,
    when: str = "",
) -> AnnotationColumn:
    return AnnotationColumn(
        name,
        ColumnKind.CHOICE,
        meaning,
        options=tuple(options),
        labels=tuple(labels),
        width=width,
        when=when,
    )


def _whole(name: str, meaning: str, *, when: str = "") -> AnnotationColumn:
    return AnnotationColumn(
        name,
        ColumnKind.WHOLE,
        meaning,
        minimum=_ONE,
        maximum=Decimal(MAX_INTEGER),
        width=12.0,
        when=when,
    )


def _decimal_column(
    name: str, meaning: str, *, unit: bool = True, when: str = ""
) -> AnnotationColumn:
    return AnnotationColumn(
        name,
        ColumnKind.DECIMAL,
        meaning,
        minimum=_ZERO if unit else None,
        maximum=_ONE if unit else None,
        width=12.0,
        when=when,
    )


_YES_NO: Final = (YES, NO)
_FULL_TEXT_WHEN: Final = "摘要沒寫就留空；全文層級依全文填"


def _taxonomy_columns(taxonomy: Taxonomy) -> list[AnnotationColumn]:
    columns: list[AnnotationColumn] = []
    for field, prefix, label in (
        (TaxonomyField.TOPIC, TOPIC_PREFIX, "主題"),
        (TaxonomyField.METHOD, METHOD_PREFIX, "方法"),
    ):
        for item in taxonomy.items(field):
            columns.append(
                _choice(
                    f"{prefix}{item.key}",
                    f"{label}：{item.name}（{item.definition}）",
                    (YES,),
                    width=10.0,
                    when="符合就填 yes；空白＝不符合",
                )
            )
    return columns


def _paper_base_columns(annotator: Annotator) -> list[AnnotationColumn]:
    columns = [
        _text(PAPER_ID, "論文 ID（不要改）", width=38.0),
        _text(DOI, "DOI；多個時以「; 」分隔（不要改）", width=26.0),
        _text(TITLE, "標題", width=40.0, wrap=True),
        _text(ABSTRACT, "摘要（沒有摘要是空白）", width=80.0, wrap=True),
    ]
    if annotator is Annotator.A:
        columns += [
            _choice(
                SPLIT,
                "調整組／測試組（由 DOI 算，不要改）",
                [split.value for split in EvalSplit],
                ["tuning：調整組", "test：測試組"],
            ),
            _choice(
                SET,
                "哪一組（不要改）",
                [sample_set.value for sample_set in SampleSet],
                ["abstract：摘要層級", "classification_only：分類專用，只標主題與方法"],
                width=18.0,
            ),
            _choice(
                FULL_TEXT,
                "要不要做全文層級",
                _YES_NO,
                when="只有 set=abstract 的論文可以填 yes；空白＝no",
            ),
        ]
    columns += [
        _choice(ANNOTATOR, "標註者代號（不寫姓名）", [item.value for item in Annotator]),
        _choice(STATUS, "標完填 done", (DONE,), when="空白＝還沒標"),
    ]
    return columns


def _context_column(field: ContextField, values: ContextValues, meaning: str) -> AnnotationColumn:
    items = values.items(field)
    return _choice(
        field.value,
        meaning,
        [item.key for item in items],
        [f"{item.key}：{item.name}（{item.definition}）" for item in items],
        width=16.0,
        when="論文明確寫出才填",
    )


def _construct_base(*, last_width: float = 30.0) -> list[AnnotationColumn]:
    return [
        AnnotationColumn(PAPER_ID, ColumnKind.PAPER_REF, "這個構面屬於哪一篇", width=38.0),
        _text(NAME_EN, "構面英文名稱（論文裡的寫法）", width=28.0),
        _text(LABEL_ZH, "繁體中文標籤", width=16.0),
        _text(DEFINITION, "定義（原文照抄）", width=last_width, wrap=True),
    ]


def _hypothesis_base() -> list[AnnotationColumn]:
    return [
        AnnotationColumn(PAPER_ID, ColumnKind.PAPER_REF, "這條關係屬於哪一篇", width=38.0),
        _text(SENTENCE, "提到這條關係的原句（原文照抄）", width=40.0, wrap=True),
        AnnotationColumn(
            CAUSE, ColumnKind.CONSTRUCT_REF, "因：同一篇某個構面的 name_en", width=24.0
        ),
        AnnotationColumn(
            EFFECT, ColumnKind.CONSTRUCT_REF, "果：同一篇某個構面的 name_en", width=24.0
        ),
        _choice(
            EVIDENCE_TYPE,
            "證據類型",
            [item.value for item in EvidenceType],
            [
                "empirical_test：實證檢定",
                "analytical：分析模型推導",
                "simulation：模擬",
                "qualitative：質性研究",
                "conceptual：概念性論述",
            ],
            width=16.0,
        ),
        _choice(
            SUPPORT,
            "支持與否",
            [item.value for item in SupportLevel],
            [
                "supported：支持",
                "not_supported：不支持",
                "partially_supported：部分支持",
                "not_stated：沒寫結果",
                "not_determined：不判斷",
            ],
            width=18.0,
            when="不是 empirical_test 時只能填 not_determined",
        ),
        _text(RESULT_QUOTE, "寫出檢定結果的原文（原文照抄）", width=36.0, wrap=True),
    ]


def annotator_a_columns(taxonomy: Taxonomy, context_values: ContextValues) -> AnnotationLayout:
    """標註者 A 的版面（論文、構面、假設三張表的欄）。"""
    papers = [
        *_paper_base_columns(Annotator.A),
        *_taxonomy_columns(taxonomy),
        _choice(IS_REVIEW, "是不是文獻回顧或後設分析", _YES_NO, width=12.0),
        _context_column(ContextField.COUNTRY, context_values, "國家"),
        _context_column(ContextField.INDUSTRY, context_values, "產業"),
        _context_column(ContextField.RESEARCH_DESIGN, context_values, "研究設計"),
        _context_column(ContextField.UNIT_OF_ANALYSIS, context_values, "分析單位"),
        _whole(SAMPLE_SIZE, "樣本數 N"),
        _text(LIMITATIONS, "研究限制（原文照抄）", width=30.0, wrap=True),
        _text(FUTURE_RESEARCH, "未來研究建議（原文照抄）", width=30.0, wrap=True),
    ]
    constructs = [
        *_construct_base(),
        _text(SCALE_SOURCE, "量表來源", width=20.0, when=_FULL_TEXT_WHEN),
        _text(ITEM_LOCATION, "題目在哪裡（例如附錄 A）", width=18.0, when=_FULL_TEXT_WHEN),
        _whole(ITEM_COUNT, "題數", when=_FULL_TEXT_WHEN),
        _decimal_column(CRONBACH_ALPHA, "Cronbach's α（0～1）", when=_FULL_TEXT_WHEN),
        _decimal_column(COMPOSITE_RELIABILITY, "組合信度 CR（0～1）", when=_FULL_TEXT_WHEN),
        _decimal_column(AVE, "平均變異萃取量 AVE（0～1）", when=_FULL_TEXT_WHEN),
    ]
    hypotheses = [
        *_hypothesis_base(),
        _decimal_column(BETA, "路徑係數 β", unit=False),
        _decimal_column(P_VALUE, "p 值（0～1，只填確切的值）"),
        _whole(SAMPLE_SIZE, "這條關係的樣本數 N"),
        _text(EFFECT_TYPE, "效果類型（例如中介、調節）", width=18.0),
        _choice(
            IN_ABSTRACT,
            "這條關係摘要裡有沒有寫",
            _YES_NO,
            when="只在全文層級（full_text=yes）的論文填",
        ),
    ]
    return AnnotationLayout(Annotator.A, tuple(papers), tuple(constructs), tuple(hypotheses))


def annotator_b_columns(taxonomy: Taxonomy) -> AnnotationLayout:
    """標註者 B 的版面：只有主題、方法、構面與假設（沒有 A 的任何答案欄）。"""
    papers = [*_paper_base_columns(Annotator.B), *_taxonomy_columns(taxonomy)]
    return AnnotationLayout(
        Annotator.B,
        tuple(papers),
        tuple(_construct_base(last_width=40.0)),
        tuple(_hypothesis_base()),
    )


# --- 產生 ---------------------------------------------------------------------------------


def _column_index(columns: Sequence[AnnotationColumn], name: str) -> int:
    return next(index for index, column in enumerate(columns) if column.name == name)


def _rule(
    column: AnnotationColumn, layout: AnnotationLayout, paper_last: int, detail_last: int
) -> ValidationRule | None:
    match column.kind:
        case ColumnKind.CHOICE:
            return ListRule(column.options)
        case ColumnKind.WHOLE:
            return NumberRule(
                "whole",
                None if column.minimum is None else int(column.minimum),
                None if column.maximum is None else int(column.maximum),
            )
        case ColumnKind.DECIMAL:
            return NumberRule("decimal", column.minimum, column.maximum)
        case ColumnKind.PAPER_REF:
            return RangeListRule(
                PAPERS_SHEET, _column_index(layout.papers, PAPER_ID), 2, paper_last
            )
        case ColumnKind.CONSTRUCT_REF:
            return RangeListRule(
                CONSTRUCTS_SHEET, _column_index(layout.constructs, NAME_EN), 2, detail_last
            )
        case ColumnKind.TEXT:
            return None


def _data_sheet(
    name: str,
    columns: Sequence[AnnotationColumn],
    body: Sequence[Sequence[WriteValue]],
    last_row: int,
    layout: AnnotationLayout,
    paper_last: int,
    detail_last: int,
) -> Sheet:
    validations = [
        ColumnValidation(index, 2, last_row, rule)
        for index, column in enumerate(columns)
        if (rule := _rule(column, layout, paper_last, detail_last)) is not None
    ]
    return Sheet(
        name=name,
        rows=[[column.name for column in columns], *body],
        widths=[column.width for column in columns],
        wrap_columns=frozenset(index for index, column in enumerate(columns) if column.wrap),
        validations=validations,
    )


_GUIDE_NOTES: Final = (
    "標註者只寫代號 A／B，任何地方都不寫姓名。",
    "status 填 done 才算標完；空白＝還沒標。",
    "主題與方法每個 key 一欄，符合的填 yes；status=done 而主題全空白＝unclassified。",
    "構面與假設一列一個；paper_id 從下拉選，假設的因、果要是同一篇構面表裡的 name_en。",
    "split、set、paper_id、doi、title、abstract 是產生時填好的，不要改。",
)


def _options_text(column: AnnotationColumn) -> str:
    if column.labels:
        return "\n".join(column.labels)
    if column.options:
        return "／".join(column.options)
    match column.kind:
        case ColumnKind.WHOLE:
            return "1 以上的整數"
        case ColumnKind.DECIMAL:
            if column.minimum is None:
                return "數字"
            return f"{column.minimum}～{column.maximum} 的數字"
        case ColumnKind.PAPER_REF:
            return "論文表的 paper_id"
        case ColumnKind.CONSTRUCT_REF:
            return "構面表同一篇的 name_en"
        case _:
            return "文字"


def _guide_sheet(layout: AnnotationLayout) -> Sheet:
    rows: list[list[WriteValue]] = [["工作表", "欄", "意思", "可填的值", "什麼時候填"]]
    rows += [["（全部）", None, note, None, None] for note in _GUIDE_NOTES]
    for sheet_name, columns in layout.sheets():
        rows += [
            [sheet_name, column.name, column.meaning, _options_text(column), column.when or None]
            for column in columns
        ]
    return Sheet(
        name=GUIDE_SHEET,
        rows=rows,
        widths=[10.0, 30.0, 44.0, 60.0, 36.0],
        wrap_columns=frozenset({2, 3, 4}),
    )


def _paper_values(row: AnnotationRow, layout: AnnotationLayout) -> list[WriteValue]:
    prefilled: dict[str, WriteValue] = {
        PAPER_ID: str(row.paper_id),
        DOI: DOI_SEPARATOR.join(row.dois),
        TITLE: row.title,
        ABSTRACT: row.abstract,
        ANNOTATOR: layout.annotator.value,
    }
    if layout.annotator is Annotator.A:
        prefilled[SPLIT] = row.split.value
        prefilled[SET] = row.sample_set.value
    return [prefilled.get(column.name) for column in layout.papers]


def _build(rows: Sequence[AnnotationRow], layout: AnnotationLayout) -> bytes:
    paper_last = max(len(rows) + 1, 2)
    detail_last = detail_row_count(len(rows)) + 1
    sheets = [
        _data_sheet(
            PAPERS_SHEET,
            layout.papers,
            [_paper_values(row, layout) for row in rows],
            paper_last,
            layout,
            paper_last,
            detail_last,
        ),
        _data_sheet(
            CONSTRUCTS_SHEET, layout.constructs, [], detail_last, layout, paper_last, detail_last
        ),
        _data_sheet(
            HYPOTHESES_SHEET, layout.hypotheses, [], detail_last, layout, paper_last, detail_last
        ),
        _guide_sheet(layout),
    ]
    return write_workbook(sheets).data


def build_annotator_a_workbook(
    rows: Sequence[AnnotationRow], taxonomy: Taxonomy, context_values: ContextValues
) -> bytes:
    """標註者 A 的空白試算表：`rows` 預填論文 ID、DOI、標題、摘要、組別與哪一組，annotator 填 A，
    其他欄空白。

    Raises:
        XlsxError: 選項寫不進下拉清單（例如合起來超過 255 字元）。
    """
    return _build(rows, annotator_a_columns(taxonomy, context_values))


def build_annotator_b_workbook(rows: Sequence[AnnotationRow], taxonomy: Taxonomy) -> bytes:
    """標註者 B 的空白試算表：只預填論文 ID、DOI、標題、摘要，annotator 填 B（沒有組別、沒有 A 的
    任何答案）。

    Raises:
        XlsxError: 選項寫不進下拉清單。
    """
    return _build(rows, annotator_b_columns(taxonomy))


def annotation_truncated_cells(rows: Sequence[AnnotationRow]) -> int:
    """寫進試算表時會被截斷的格數（標題或摘要超過 Excel 一格的上限）。"""
    return sum(
        clean_cell_text(text)[1]
        for row in rows
        for text in (row.title, row.abstract or "", DOI_SEPARATOR.join(row.dois))
    )


# --- 讀回：問題 ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnnotationProblem:
    """試算表的一個問題：工作表、Excel 列號、欄（「欄名（欄字母）」）、原因。檔案本身讀不了時
    工作表、列、欄可以是 None。不帶儲存格內容。"""

    sheet: str | None
    row: int | None
    column: str | None
    reason: str


class AnnotationParseError(ValueError):
    """試算表讀不了或有問題；`problems` 是全部問題。"""

    def __init__(self, problems: Sequence[AnnotationProblem]) -> None:
        self.problems = tuple(problems)
        super().__init__(f"標註試算表有 {len(self.problems)} 個問題")


def _column_label(name: str, index: int) -> str:
    return f"{name}（{column_letter(index)}）"


def _stripped(value: CellValue) -> CellValue:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _number(value: CellValue) -> Decimal | None:
    """數字格或能完整轉成數字的字串 → Decimal；其他（含 bool）是 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if _NUMERIC_TEXT.fullmatch(value) is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:  # pragma: no cover - 上面的寫法一定轉得成
        return None


class _Table:
    """一張表：表頭（欄名 → 欄號）與資料列；表頭的問題記進 `problems`。"""

    def __init__(
        self,
        sheet: str,
        values: SheetValues | None,
        columns: Sequence[AnnotationColumn],
        problems: list[AnnotationProblem],
    ) -> None:
        self.sheet = sheet
        self.index: dict[str, int] = {}
        self.ok = False
        self.rows: list[tuple[int, tuple[CellValue, ...]]] = []
        if values is None:
            problems.append(AnnotationProblem(sheet, None, None, "缺少這張工作表"))
            return
        header = values[0] if values else ()
        expected = {column.name for column in columns}
        unnamed: set[int] = set()
        before = len(problems)
        for position, cell in enumerate(header):
            name = _stripped(cell)
            if name is None:
                unnamed.add(position)
                continue
            if not isinstance(name, str) or name not in expected:
                problems.append(
                    AnnotationProblem(sheet, 1, column_letter(position), "不認得的欄名")
                )
                continue
            if name in self.index:
                problems.append(
                    AnnotationProblem(sheet, 1, _column_label(name, position), "欄名重複")
                )
                continue
            self.index[name] = position
        for column in columns:
            if column.name not in self.index:
                problems.append(AnnotationProblem(sheet, 1, column.name, "缺少這一欄"))
        unnamed |= set(range(len(header), max((len(row) for row in values), default=0)))
        for offset, row in enumerate(values[1:], start=2):
            cells = tuple(_stripped(cell) for cell in row)
            if all(cell is None for cell in cells):
                continue
            for position in sorted(unnamed):
                if position < len(cells) and cells[position] is not None:
                    problems.append(
                        AnnotationProblem(
                            sheet, offset, column_letter(position), "這一欄沒有欄名卻有值"
                        )
                    )
            self.rows.append((offset, cells))
        self.ok = len(problems) == before


class _Row:
    """讀一列的值；問題記進共用的清單。"""

    def __init__(
        self,
        table: _Table,
        number: int,
        cells: tuple[CellValue, ...],
        problems: list[AnnotationProblem],
    ) -> None:
        self.table = table
        self.number = number
        self.cells = cells
        self.problems = problems
        self.failed = False

    def raw(self, name: str) -> CellValue:
        position = self.table.index.get(name)
        if position is None or position >= len(self.cells):
            return None
        return self.cells[position]

    def has(self, name: str) -> bool:
        return self.raw(name) is not None

    def problem(self, name: str, reason: str) -> None:
        self.failed = True
        position = self.table.index.get(name)
        column = name if position is None else _column_label(name, position)
        self.problems.append(AnnotationProblem(self.table.sheet, self.number, column, reason))

    def text(self, name: str, *, required: bool = False) -> str | None:
        value = self.raw(name)
        if value is None:
            if required:
                self.problem(name, "必填")
            return None
        if isinstance(value, bool):
            self.problem(name, "要是文字")
            return None
        if isinstance(value, Decimal):
            if value.is_finite() and abs(value.adjusted()) > _MAX_TEXT_EXPONENT:
                # `1E+300000000` 展開會變成幾億字元；不展開，請標註者改成文字。
                self.problem(name, "要是文字")
                return None
            return format(value, "f")
        return value

    def choice(self, name: str, options: Sequence[str], *, required: bool = False) -> str | None:
        value = self.raw(name)
        if value is None:
            if required:
                self.problem(name, "必填")
            return None
        if isinstance(value, str):
            for option in options:
                if value.casefold() == option.casefold():
                    return option
        self.problem(name, "不在下拉清單裡")
        return None

    def flag(self, name: str) -> bool | None:
        value = self.choice(name, _YES_NO)
        return None if value is None else value == YES

    def integer(self, name: str) -> int | None:
        value = self.raw(name)
        if value is None:
            return None
        number = _number(value)
        if number is None or not number.is_finite() or number != number.to_integral_value():
            self.problem(name, "要是整數")
            return None
        if not 1 <= number <= MAX_INTEGER:
            self.problem(name, f"要是 1～{MAX_INTEGER} 的整數")
            return None
        return int(number)

    def decimal(self, name: str, *, unit: bool) -> Decimal | None:
        value = self.raw(name)
        if value is None:
            return None
        number = _number(value)
        if number is None or not number.is_finite():
            self.problem(name, "要是數字")
            return None
        if unit and not _ZERO <= number <= _ONE:
            self.problem(name, "要在 0～1 之間")
            return None
        return number

    def paper_id(self, *, required: bool = True) -> uuid.UUID | None:
        value = self.text(PAPER_ID, required=required)
        if value is None:
            return None
        try:
            return uuid.UUID(value)
        except ValueError:
            self.problem(PAPER_ID, "不是論文 ID")
            return None

    def dois(self) -> tuple[str, ...] | None:
        value = self.text(DOI, required=True)
        if value is None:
            return None
        # 只在「分號後面接空白」或空白處切：DOI 本身可以含分號（例如舊式 SICI DOI）。
        parts = _DOI_SPLIT.split(value.strip())
        try:
            dois = tuple(sorted({normalize_doi(part) for part in parts}))
        except InvalidIdentifierError:
            self.problem(DOI, "不是看得懂的 DOI")
            return None
        if any(not part for part in parts) or not dois:
            self.problem(DOI, "不是看得懂的 DOI")
            return None
        return dois


def _read_sheets(data: bytes) -> Mapping[str, SheetValues]:
    try:
        return read_workbook(data)
    except XlsxError as exc:
        raise AnnotationParseError([AnnotationProblem(None, None, None, str(exc))]) from None


# --- 讀回：預填的欄（補抽、產生 B） ---------------------------------------------------------


@dataclass(frozen=True)
class SampledWorkbook:
    """既有 A 試算表預填的列，以及是不是已經有任何標註值（預填欄以外有值、或構面／假設表有列）。"""

    rows: tuple[AnnotationRow, ...]
    annotated: bool


_SAMPLED_COLUMNS: Final = (PAPER_ID, DOI, TITLE, ABSTRACT, SPLIT, SET, ANNOTATOR, STATUS, FULL_TEXT)


def read_sampled_papers(data: bytes) -> SampledWorkbook:
    """讀既有的 A 試算表：論文表的 paper_id、doi、title、abstract、split、set（驗證這幾欄），並回報
    是否已經有任何標註值。不需要分類表（主題與方法欄不驗）。

    Raises:
        AnnotationParseError: 讀不了、不是 A 的版面、預填欄有問題（paper_id 重複、DOI 看不懂、split
            和 DOI 算出來的不同、set 不對）。
    """
    sheets = _read_sheets(data)
    problems: list[AnnotationProblem] = []
    values = sheets.get(PAPERS_SHEET)
    if values is None:
        raise AnnotationParseError([AnnotationProblem(PAPERS_SHEET, None, None, "缺少這張工作表")])
    header = [_stripped(cell) for cell in (values[0] if values else ())]
    names = [name for name in header if isinstance(name, str)]
    missing = [name for name in _SAMPLED_COLUMNS if name not in names]
    if missing:
        raise AnnotationParseError(
            [
                AnnotationProblem(PAPERS_SHEET, 1, name, "缺少這一欄（不是標註者 A 的試算表）")
                for name in missing
            ]
        )
    # 主題、方法等其他欄不驗（不需要分類表）；表頭的問題丟掉，只用來找欄號。
    table = _Table(PAPERS_SHEET, values, [_text(name, name) for name in names], [])
    prefilled = {table.index[name] for name in PREFILLED_COLUMNS if name in table.index}
    annotated = False
    rows: list[AnnotationRow] = []
    seen: dict[uuid.UUID, int] = {}
    for number, cells in table.rows:
        row = _Row(table, number, cells, problems)
        if any(
            cell is not None for position, cell in enumerate(cells) if position not in prefilled
        ):
            annotated = True
        paper_id = row.paper_id()
        dois = row.dois()
        title = row.text(TITLE) or ""
        abstract = row.text(ABSTRACT)
        split = row.choice(SPLIT, [item.value for item in EvalSplit], required=True)
        sample_set = row.choice(SET, [item.value for item in SampleSet], required=True)
        annotator = row.choice(ANNOTATOR, [item.value for item in Annotator], required=True)
        if annotator is not None and annotator != Annotator.A.value:
            row.problem(ANNOTATOR, "不是標註者 A 的試算表")
        if paper_id is not None:
            if paper_id in seen:
                row.problem(PAPER_ID, f"paper_id 重複（第 {seen[paper_id]} 列也有）")
            else:
                seen[paper_id] = number
        if dois is not None and split is not None and split_for_dois(dois).value != split:
            row.problem(SPLIT, "和 DOI 算出來的組別不同")
        if row.failed or paper_id is None or dois is None or split is None or sample_set is None:
            continue
        rows.append(
            AnnotationRow(paper_id, dois, title, abstract, EvalSplit(split), SampleSet(sample_set))
        )
    if problems:
        raise AnnotationParseError(problems)
    for detail in (CONSTRUCTS_SHEET, HYPOTHESES_SHEET):
        if any(
            any(_stripped(cell) is not None for cell in row) for row in sheets.get(detail, ())[1:]
        ):
            annotated = True
    return SampledWorkbook(tuple(rows), annotated)


def select_second_annotator(
    rows: Sequence[AnnotationRow], count: int, seed: int
) -> tuple[AnnotationRow, ...]:
    """標註者 B 的論文：只從摘要層級的列，依 `annotation_sample_sha256(seed, "second_annotator",
    最小的 DOI)` 排序取前 `count` 篇（不足時全部）。

    Raises:
        ValueError: `count` 不是 1 以上的整數，或 `seed` 不是整數。
    """
    _check_seed(seed)
    if type(count) is not int or count < 1:
        raise ValueError("count 要是 1 以上的整數")
    pool = [row for row in rows if row.sample_set is SampleSet.ABSTRACT]
    pool.sort(
        key=lambda row: (annotation_sample_sha256(seed, "second_annotator", row.doi), row.doi)
    )
    return tuple(pool[:count])


# --- 讀回：解析（Epic 8 匯入用） -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnnotatedPaper:
    """論文表的一列。`split` 一律由 DOI 算；`done` 是 status 填了 done；`topics`／`methods` 是排序好
    的 `key`（status=done 而 `topics` 是空的＝unclassified）。B 的版面沒有的欄是 None（`full_text`
    是 False、`sample_set` 是摘要層級）。"""

    paper_id: uuid.UUID
    doi: str
    dois: tuple[str, ...]
    split: EvalSplit
    sample_set: SampleSet
    full_text: bool
    annotator: Annotator
    done: bool
    topics: tuple[str, ...]
    methods: tuple[str, ...]
    is_review_or_meta_analysis: bool | None
    country: str | None
    industry: str | None
    research_design: str | None
    unit_of_analysis: str | None
    sample_size: int | None
    limitations: str | None
    future_research: str | None

    @property
    def levels(self) -> tuple[AnnotationLevel, ...]:
        """這篇屬於哪些標註層級。"""
        if self.sample_set is SampleSet.CLASSIFICATION_ONLY:
            return (AnnotationLevel.CLASSIFICATION_ONLY,)
        if self.full_text:
            return (AnnotationLevel.ABSTRACT, AnnotationLevel.FULL_TEXT)
        return (AnnotationLevel.ABSTRACT,)


@dataclass(frozen=True, slots=True)
class AnnotatedConstruct:
    """構面表的一列（B 的版面沒有的欄是 None）。"""

    paper_id: uuid.UUID
    name_en: str
    label_zh: str | None
    definition: str | None
    scale_source: str | None
    item_location: str | None
    item_count: int | None
    cronbach_alpha: Decimal | None
    composite_reliability: Decimal | None
    ave: Decimal | None


@dataclass(frozen=True, slots=True)
class AnnotatedHypothesis:
    """假設表的一列（B 的版面沒有的欄是 None）。"""

    paper_id: uuid.UUID
    sentence: str
    cause: str
    effect: str
    evidence_type: EvidenceType
    support: SupportLevel
    result_quote: str | None
    beta: Decimal | None
    p_value: Decimal | None
    sample_size: int | None
    effect_type: str | None
    in_abstract: bool | None


@dataclass(frozen=True)
class AnnotationWorkbook:
    """解析好的試算表。"""

    annotator: Annotator
    papers: tuple[AnnotatedPaper, ...]
    constructs: tuple[AnnotatedConstruct, ...]
    hypotheses: tuple[AnnotatedHypothesis, ...]


def _name_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _detect_layout(
    values: SheetValues | None, taxonomy: Taxonomy, context_values: ContextValues
) -> AnnotationLayout:
    layout_a = annotator_a_columns(taxonomy, context_values)
    layout_b = annotator_b_columns(taxonomy)
    header = {_stripped(cell) for cell in (values[0] if values else ())}
    names_a = {column.name for column in layout_a.papers}
    names_b = {column.name for column in layout_b.papers}
    if header == names_b or (header != names_a and not header & {SPLIT, SET, FULL_TEXT}):
        return layout_b
    return layout_a


@dataclass
class _PaperInfo:
    sample_set: SampleSet
    full_text: bool
    # 全文層級而且 status 是 done：每一條假設都要填 in_abstract（算漏失率要用）。
    in_abstract_required: bool
    constructs: dict[str, tuple[int, str]]  # 正規化後的 name_en → (列號, 構面表上的 name_en)


def _parse_paper(
    row: _Row, layout: AnnotationLayout, taxonomy: Taxonomy, context_values: ContextValues
) -> tuple[uuid.UUID | None, AnnotatedPaper | None, _PaperInfo]:
    """一列論文 → (paper_id, 論文, 構面與假設表核對用的資訊)。這一列有問題時論文是 None；資訊裡的
    哪一組照填的值（看不懂當摘要層級），全文層級當成是（不讓別的問題連帶誤報 in_abstract）。"""
    is_a = layout.annotator is Annotator.A
    paper_id = row.paper_id()
    dois = row.dois()
    annotator = row.choice(ANNOTATOR, [item.value for item in Annotator], required=True)
    if annotator is not None and annotator != layout.annotator.value:
        row.problem(ANNOTATOR, f"和這份試算表的版面（標註者 {layout.annotator.value}）不符")
    done = row.choice(STATUS, (DONE,)) == DONE
    topics = tuple(
        sorted(
            key
            for key in taxonomy.keys(TaxonomyField.TOPIC)
            if row.choice(f"{TOPIC_PREFIX}{key}", (YES,)) == YES
        )
    )
    methods = tuple(
        sorted(
            key
            for key in taxonomy.keys(TaxonomyField.METHOD)
            if row.choice(f"{METHOD_PREFIX}{key}", (YES,)) == YES
        )
    )
    sample_set = SampleSet.ABSTRACT
    full_text = False
    model: dict[str, object] = dict.fromkeys(PAPER_MODEL_COLUMNS)
    if is_a:
        split = row.choice(SPLIT, [item.value for item in EvalSplit], required=True)
        if dois is not None and split is not None and split_for_dois(dois).value != split:
            row.problem(SPLIT, "和 DOI 算出來的組別不同")
        chosen_set = row.choice(SET, [item.value for item in SampleSet], required=True)
        sample_set = SampleSet(chosen_set) if chosen_set is not None else SampleSet.ABSTRACT
        full_text = bool(row.flag(FULL_TEXT))
        if full_text and chosen_set == SampleSet.CLASSIFICATION_ONLY:
            row.problem(FULL_TEXT, "分類專用的論文不做全文層級")
        model[IS_REVIEW] = row.flag(IS_REVIEW)
        for field in ContextField:
            model[field.value] = row.choice(field.value, context_values.keys(field))
        model[SAMPLE_SIZE] = row.integer(SAMPLE_SIZE)
        model[LIMITATIONS] = row.text(LIMITATIONS)
        model[FUTURE_RESEARCH] = row.text(FUTURE_RESEARCH)
        if chosen_set == SampleSet.CLASSIFICATION_ONLY:
            for name in PAPER_MODEL_COLUMNS:
                if row.has(name):
                    row.problem(name, "分類專用的論文不填研究模型欄")
    info = _PaperInfo(sample_set, full_text or row.failed, full_text and done, {})
    if row.failed or paper_id is None or dois is None:
        return paper_id, None, info
    paper = AnnotatedPaper(
        paper_id=paper_id,
        doi=dois[0],
        dois=dois,
        split=split_for_dois(dois),
        sample_set=sample_set,
        full_text=full_text,
        annotator=layout.annotator,
        done=done,
        topics=topics,
        methods=methods,
        is_review_or_meta_analysis=_optional_bool(model[IS_REVIEW]),
        country=_optional_str(model[ContextField.COUNTRY.value]),
        industry=_optional_str(model[ContextField.INDUSTRY.value]),
        research_design=_optional_str(model[ContextField.RESEARCH_DESIGN.value]),
        unit_of_analysis=_optional_str(model[ContextField.UNIT_OF_ANALYSIS.value]),
        sample_size=_optional_int(model[SAMPLE_SIZE]),
        limitations=_optional_str(model[LIMITATIONS]),
        future_research=_optional_str(model[FUTURE_RESEARCH]),
    )
    return paper_id, paper, info


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _detail_paper(
    row: _Row, papers: Mapping[uuid.UUID, _PaperInfo] | None
) -> tuple[uuid.UUID | None, _PaperInfo | None]:
    paper_id = row.paper_id()
    if paper_id is None or papers is None:
        return paper_id, None
    info = papers.get(paper_id)
    if info is None:
        row.problem(PAPER_ID, "paper_id 不在論文表")
    return paper_id, info


def parse_annotation_workbook(
    data: bytes, taxonomy: Taxonomy, context_values: ContextValues
) -> AnnotationWorkbook:
    """解析填好的標註試算表（A 或 B 的版面，依論文表的欄名判斷；欄的順序不拘）。全空白的列略過、
    字串去頭尾空白、選項不分大小寫；整數欄收整數或整數值的小數，數字字串能完整轉成數字也收。

    Raises:
        AnnotationParseError: 檔案讀不了，或有任何問題（全部收齊後一次丟）。
    """
    sheets = _read_sheets(data)
    problems: list[AnnotationProblem] = []
    layout = _detect_layout(sheets.get(PAPERS_SHEET), taxonomy, context_values)
    is_a = layout.annotator is Annotator.A
    paper_sheet = _Table(PAPERS_SHEET, sheets.get(PAPERS_SHEET), layout.papers, problems)
    construct_sheet = _Table(
        CONSTRUCTS_SHEET, sheets.get(CONSTRUCTS_SHEET), layout.constructs, problems
    )
    hypothesis_sheet = _Table(
        HYPOTHESES_SHEET, sheets.get(HYPOTHESES_SHEET), layout.hypotheses, problems
    )

    papers: list[AnnotatedPaper] = []
    infos: dict[uuid.UUID, _PaperInfo] | None = None
    if paper_sheet.ok:
        infos = {}
        seen: dict[uuid.UUID, int] = {}
        for number, cells in paper_sheet.rows:
            row = _Row(paper_sheet, number, cells, problems)
            paper_id, paper, paper_info = _parse_paper(row, layout, taxonomy, context_values)
            if paper_id is None:
                continue
            if paper_id in seen:
                row.problem(PAPER_ID, f"paper_id 重複（第 {seen[paper_id]} 列也有）")
                continue
            seen[paper_id] = number
            # 這一列有別的問題也算「在論文表」，構面與假設不會被誤報成不在論文表。
            infos[paper_id] = paper_info
            if paper is not None:
                papers.append(paper)

    constructs: list[AnnotatedConstruct] = []
    if construct_sheet.ok:
        for number, cells in construct_sheet.rows:
            row = _Row(construct_sheet, number, cells, problems)
            paper_id, info = _detail_paper(row, infos)
            if info is not None and info.sample_set is SampleSet.CLASSIFICATION_ONLY:
                row.problem(PAPER_ID, "分類專用的論文不能有構面列")
            name_en = row.text(NAME_EN, required=True)
            label_zh = row.text(LABEL_ZH)
            definition = row.text(DEFINITION)
            scale_source = row.text(SCALE_SOURCE) if is_a else None
            item_location = row.text(ITEM_LOCATION) if is_a else None
            item_count = row.integer(ITEM_COUNT) if is_a else None
            alpha = row.decimal(CRONBACH_ALPHA, unit=True) if is_a else None
            reliability = row.decimal(COMPOSITE_RELIABILITY, unit=True) if is_a else None
            ave = row.decimal(AVE, unit=True) if is_a else None
            if info is not None and name_en is not None:
                key = _name_key(name_en)
                if key in info.constructs:
                    row.problem(
                        NAME_EN, f"同一篇的構面名稱重複（第 {info.constructs[key][0]} 列也有）"
                    )
                else:
                    info.constructs[key] = (number, name_en)
            if row.failed or paper_id is None or name_en is None:
                continue
            constructs.append(
                AnnotatedConstruct(
                    paper_id=paper_id,
                    name_en=name_en,
                    label_zh=label_zh,
                    definition=definition,
                    scale_source=scale_source,
                    item_location=item_location,
                    item_count=item_count,
                    cronbach_alpha=alpha,
                    composite_reliability=reliability,
                    ave=ave,
                )
            )

    hypotheses: list[AnnotatedHypothesis] = []
    if hypothesis_sheet.ok:
        for number, cells in hypothesis_sheet.rows:
            row = _Row(hypothesis_sheet, number, cells, problems)
            paper_id, info = _detail_paper(row, infos)
            if info is not None and info.sample_set is SampleSet.CLASSIFICATION_ONLY:
                row.problem(PAPER_ID, "分類專用的論文不能有假設列")
            sentence = row.text(SENTENCE, required=True)
            cause = row.text(CAUSE, required=True)
            effect = row.text(EFFECT, required=True)
            evidence = row.choice(
                EVIDENCE_TYPE, [item.value for item in EvidenceType], required=True
            )
            support = row.choice(SUPPORT, [item.value for item in SupportLevel], required=True)
            result_quote = row.text(RESULT_QUOTE)
            beta = row.decimal(BETA, unit=False) if is_a else None
            p_value = row.decimal(P_VALUE, unit=True) if is_a else None
            sample_size = row.integer(SAMPLE_SIZE) if is_a else None
            effect_type = row.text(EFFECT_TYPE) if is_a else None
            in_abstract = row.flag(IN_ABSTRACT) if is_a else None
            if info is not None and construct_sheet.ok:
                # 比對不分大小寫、空白合併；存成構面表上的寫法，Epic 8 才能精確對上。
                matched: list[str | None] = []
                for name, value in ((CAUSE, cause), (EFFECT, effect)):
                    found = None if value is None else info.constructs.get(_name_key(value))
                    if value is not None and found is None:
                        row.problem(name, "不是同一篇的構面（要等於構面表的 name_en）")
                    matched.append(value if found is None else found[1])
                cause, effect = matched
            if cause is not None and effect is not None and _name_key(cause) == _name_key(effect):
                row.problem(EFFECT, "因和果不能是同一個構面")
            if (
                evidence is not None
                and evidence != EvidenceType.EMPIRICAL_TEST
                and support is not None
                and support != SupportLevel.NOT_DETERMINED
            ):
                row.problem(SUPPORT, "不是實證檢定（empirical_test）時只能填 not_determined")
            if is_a and row.has(IN_ABSTRACT) and info is not None and not info.full_text:
                row.problem(IN_ABSTRACT, "不是全文層級的論文不填 in_abstract")
            if is_a and not row.has(IN_ABSTRACT) and info is not None and info.in_abstract_required:
                row.problem(IN_ABSTRACT, "全文層級的論文要填")
            if (
                row.failed
                or paper_id is None
                or sentence is None
                or cause is None
                or effect is None
                or evidence is None
                or support is None
            ):
                continue
            hypotheses.append(
                AnnotatedHypothesis(
                    paper_id=paper_id,
                    sentence=sentence,
                    cause=cause,
                    effect=effect,
                    evidence_type=EvidenceType(evidence),
                    support=SupportLevel(support),
                    result_quote=result_quote,
                    beta=beta,
                    p_value=p_value,
                    sample_size=sample_size,
                    effect_type=effect_type,
                    in_abstract=in_abstract,
                )
            )

    if problems:
        raise AnnotationParseError(problems)
    return AnnotationWorkbook(layout.annotator, tuple(papers), tuple(constructs), tuple(hypotheses))


# --- 輸出文字 -----------------------------------------------------------------------------


def _split_text(rows: Iterable[AnnotationRow]) -> str:
    counts = Counter(row.split for row in rows)
    return "、".join(f"{_SPLIT_LABEL[split]} {counts[split]}" for split in EvalSplit)


def render_annotation_sample(result: AnnotationSampleResult, *, truncated_cells: int) -> list[str]:
    """產生 A 試算表後要印的：seed、興趣主題、兩組篇數與調整組／測試組、候選池數、沒 DOI 略過數、
    不足時的提示、截斷數。不印標題與摘要。"""
    interest = "、".join(result.interest) if result.interest else "沒有"
    lines = [f"seed {result.seed}；興趣主題：{interest}"]
    if result.existing_count:
        lines.append(f"既有的試算表：{result.existing_count} 篇（照原順序保留）")
    for sample_set in SampleSet:
        rows = [row for row in result.rows if row.sample_set is sample_set]
        label = _SET_LABEL[sample_set]
        lines.append(
            f"{label}：{len(rows)} 篇（{_split_text(rows)}）；這次新抽 {result.drawn[sample_set]}"
            f" 篇，要 {result.requested[sample_set]} 篇，候選 {result.pool[sample_set]} 篇"
        )
        if result.drawn[sample_set] < result.requested[sample_set]:
            lines.append(
                f"{label}只有 {result.drawn[sample_set]} 篇候選，少於要的"
                f" {result.requested[sample_set]} 篇，全部放進來"
            )
    lines.append(f"沒有 DOI、沒有抽的算數論文：{result.skipped_without_doi} 篇")
    if truncated_cells:
        lines.append(f"有 {truncated_cells} 格超過 Excel 一格的上限，已截斷")
    return lines


def render_topic_counts(counts: TopicCounts, taxonomy: Taxonomy) -> list[str]:
    """各主題在兩組各幾篇（有效主題；人工修正優先）。"""
    names = {item.key: item.name for item in taxonomy.topics}
    found = {key for counter in counts.by_set.values() for key in counter}
    keys = [
        *taxonomy.keys(TaxonomyField.TOPIC),
        *sorted(found - set(names) - {UNCLASSIFIED}),
        UNCLASSIFIED,
    ]
    body = [
        [
            key,
            names.get(key, "沒有主題" if key == UNCLASSIFIED else "（不在分類表）"),
            *(str(counts.by_set[sample_set].get(key, 0)) for sample_set in SampleSet),
        ]
        for key in keys
    ]
    body.append(
        ["—", "還沒有分類結果", *(str(counts.pending[sample_set]) for sample_set in SampleSet)]
    )
    header = ["主題", "名稱", *(_SET_LABEL[sample_set] for sample_set in SampleSet)]
    lines = ["各主題篇數（AI 分類或人工修正後的主題）："]
    lines += render_table(header, body, align="llrr")
    lines.append("一篇可以有多個主題，各主題加起來會多於篇數。")
    return lines


def render_second_annotator(
    seed: int, selected: Sequence[AnnotationRow], available: int, requested: int
) -> list[str]:
    """產生 B 試算表後要印的：seed、從幾篇抽幾篇、調整組／測試組。"""
    lines = [
        f"seed {seed}：從摘要層級 {available} 篇抽 {len(selected)} 篇給標註者 B"
        f"（{_split_text(selected)}）"
    ]
    if available < requested:
        lines.append(f"摘要層級只有 {available} 篇，少於要的 {requested} 篇，全部放進來")
    return lines


def render_annotation_check(workbook: AnnotationWorkbook) -> list[str]:
    """`radar annotations check` 沒有問題時要印的：標註者、各層級已標／還沒標、構面與假設列數。"""
    lines = [f"標註者 {workbook.annotator.value}：沒有問題"]
    for level in AnnotationLevel:
        papers = [paper for paper in workbook.papers if level in paper.levels]
        if not papers and level is not AnnotationLevel.ABSTRACT:
            continue
        done = sum(paper.done for paper in papers)
        lines.append(f"{_LEVEL_LABEL[level]}：已標 {done} 篇、還沒標 {len(papers) - done} 篇")
    lines.append(f"構面 {len(workbook.constructs)} 列、假設 {len(workbook.hypotheses)} 列")
    return lines


def render_annotation_problems(problems: Sequence[AnnotationProblem]) -> list[str]:
    """問題清單（最多 `MAX_PRINTED_PROBLEMS` 條，其餘印「還有 N 條」）；不印儲存格內容。"""
    lines = [f"標註試算表有 {len(problems)} 個問題："]
    for problem in problems[:MAX_PRINTED_PROBLEMS]:
        where = [
            part
            for part in (
                problem.sheet,
                None if problem.row is None else f"第 {problem.row} 列",
                problem.column,
            )
            if part is not None
        ]
        prefix = " ".join(where)
        lines.append(f"- {prefix}：{problem.reason}" if prefix else f"- {problem.reason}")
    if len(problems) > MAX_PRINTED_PROBLEMS:
        lines.append(f"還有 {len(problems) - MAX_PRINTED_PROBLEMS} 條")
    return lines
