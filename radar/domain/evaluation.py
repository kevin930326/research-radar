"""搜尋的評估題組（Story 4.4、FR-26、FR-27、FR-41、ADR-0005）：題目檔的驗證、命中率與 MRR 的算法、
印給 Kevin 看的報告、存進 `eval_run` 的結果。SQL 在 `repo/evaluation.py`，流程在
`services/evaluation.py`，指令在 `radar eval`（`entrypoints/cli/evaluation.py`）。

**題目檔**（CSV、UTF-8；Kevin 自己出的題目，不由 AI 產生；真的題目檔不進 repo）：一列＝一個「查詢＋
一篇正解」，欄位 `set`、`query_id`、`query`、`relevant_doi`、`note`（選填）。同一題有幾篇正解就寫
幾列。`parse_eval_queries_csv` 把整份檔當使用者輸入驗，有任何一個問題就整份不收（列出每個問題的列號
與原因，不印格子的內容），不寫任何東西：

- 編碼：一定要是 UTF-8（開頭的 BOM 可以有，Excel 存的 CSV 會帶）；不是就不收。檔案大小與列數有上限。
- 欄位：第一列是欄位名稱；少了必要的欄位、多了不認得的欄位、同一個欄位寫兩次都不收。
- 空列（每一格都是空白）跳過並計數；欄位數不對的列不收。
- `set` 只能是 `search`（搜尋，FR-26）或 `similar`（找相似論文，FR-27）。
- `query_id`：Kevin 自己取的編號，1～64 個字。
- `query`：`search` 是查詢字串，照搜尋的規則驗（`domain/retrieval.clean_search_query`：控制字元、
  看不見的格式字元不收、去頭尾空白、長度上限和搜尋一樣）；`similar` 是起點論文的 DOI。
- DOI（`relevant_doi`、`similar` 的 `query`）一律用 `identifiers.normalize_doi` 正規化，看不懂就
  不收。
- `note` 最多 500 個字；空的＝沒有。
- 同一題（`set`＋`query_id`）每一列的 `query` 要一樣；同一題同一篇正解寫兩列（正規化後相同）不收；
  `similar` 的正解不能是起點自己；每題的正解最多 5 篇（FR-26：標太多會讓命中變得太容易）。每列都有
  一篇正解，所以驗完的題目一定至少 1 篇；匯入時 DOI 對不到論文的正解跳過，跳到一篇都不剩的題目整題
  不匯入（`plan_eval_import`）。
- 公式開頭的字（`=`、`+`、`-`、`@`）不特別處理：格子只當文字存、比對、印出來，任何地方都不執行、
  不求值。

**命中率與 MRR**（`radar eval search`）：每題跑 `retrieve`（`tracked` 範圍、系統的 viewer），從
同一次的結果拆出三組排名，各自算前 10 名：

- 全文檢索腿（`text`）：只看在全文檢索腿裡的論文，照那條腿的名次排。
- 向量腿（`vector`）：只看在向量腿裡（相似度下限以上）的論文，照那條腿的名次排。
- 融合（`fused`）：`retrieve` 融合後的名次（搜尋真正會給成員看的順序）。

`retrieve` 回的是兩條腿各自前 N 名的聯集（每篇帶著兩條腿的名次），所以「只用一條腿」的前 10 名就是
那條腿名次 1～10 的論文，和那條腿單獨跑一樣，不用另外的查詢。命中＝前 10 名裡至少有一篇正解；命中
名次是第一篇正解的名次；MRR＝每題「1 ÷ 命中名次」（沒命中算 0）的平均。三組分開報，不合成一個分數。
`similar` 題組只有向量腿（找相似論文只用向量），只報 `vector`。「找不到相關的論文」的題數＝那一組
一篇結果都沒有的題數（成員看到的就是「找不到相關的論文」）。
"""

import csv
import io
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from fractions import Fraction
from typing import Final

from radar.domain.identifiers import InvalidIdentifierError, normalize_doi
from radar.domain.member_text_characters import is_rejected_character
from radar.domain.retrieval import InvalidSearchQueryError, RetrievalHit, clean_search_query

# 題目檔的上限：大小（位元組）、資料列數（不含欄位名稱與空列）。
MAX_FILE_BYTES: Final = 1_000_000
MAX_DATA_ROWS: Final = 5_000
QUERY_ID_MAX_LENGTH: Final = 64
NOTE_MAX_LENGTH: Final = 500
# 每題最多幾篇正解（FR-26：1～5 篇，建議 2～3 篇）。
MAX_RELEVANT_PER_QUERY: Final = 5
# 看前幾名（FR-26：前 10 名命中率）。
TOP_N: Final = 10
# `--min-similarity` 最多給幾個值。
MAX_MIN_SIMILARITIES: Final = 10
# 一次最多印幾個題目檔的問題（其餘只印總數）。
MAX_PROBLEMS_SHOWN: Final = 50
# `eval_run.results` 的格式版本（Epic 8 擴充時加版本，不改舊的意思）。
RESULTS_FORMAT_VERSION: Final = 1

REQUIRED_COLUMNS: Final = ("set", "query_id", "query", "relevant_doi")
OPTIONAL_COLUMNS: Final = ("note",)
_UTF8_BOM: Final = b"\xef\xbb\xbf"


class EvalQuerySet(StrEnum):
    """題目屬於哪一組。"""

    SEARCH = "search"  # 搜尋：`query` 是一句查詢（FR-26）
    SIMILAR = "similar"  # 找相似論文：`query` 是起點論文的 DOI（FR-27）


class EvalRunKind(StrEnum):
    """`eval_run` 評的是什麼（Epic 8 會加 AI 產出的評估）。"""

    SEARCH = "search"  # `radar eval search`：搜尋與找相似論文的命中率


class EvalLeg(StrEnum):
    """從一次 `retrieve` 的結果拆出來的三組排名。"""

    TEXT = "text"  # 只用全文檢索腿
    VECTOR = "vector"  # 只用向量腿
    FUSED = "fused"  # 融合（RRF）


LEG_LABELS: Final[Mapping[EvalLeg, str]] = {
    EvalLeg.TEXT: "只用全文檢索腿",
    EvalLeg.VECTOR: "只用向量腿",
    EvalLeg.FUSED: "融合",
}
SET_LABELS: Final[Mapping[EvalQuerySet, str]] = {
    EvalQuerySet.SEARCH: "搜尋（search）",
    EvalQuerySet.SIMILAR: "找相似論文（similar，只有向量腿）",
}
# 每一組報哪幾組排名。
SET_LEGS: Final[Mapping[EvalQuerySet, tuple[EvalLeg, ...]]] = {
    EvalQuerySet.SEARCH: (EvalLeg.TEXT, EvalLeg.VECTOR, EvalLeg.FUSED),
    EvalQuerySet.SIMILAR: (EvalLeg.VECTOR,),
}


# --- 題目檔 -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvalRelevant:
    """一篇正解：正規化後的 DOI、備註（沒有是 None）、在題目檔的第幾列（從資料庫讀回來是 None）。"""

    doi: str
    note: str | None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class EvalQuestion:
    """一題：哪一組、Kevin 取的編號、查詢（`search` 是驗過的查詢字串，`similar` 是正規化後的 DOI）、
    正解（1～`MAX_RELEVANT_PER_QUERY` 篇，DOI 不重複）。

    Raises:
        ValueError: 正解是 0 篇或超過上限、DOI 重複，或 `similar` 的正解是起點自己。
    """

    query_set: EvalQuerySet
    query_id: str
    query: str
    relevant: tuple[EvalRelevant, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "query_set", EvalQuerySet(self.query_set))
        dois = [item.doi for item in self.relevant]
        if not 1 <= len(dois) <= MAX_RELEVANT_PER_QUERY:
            raise ValueError(f"每題的正解要是 1～{MAX_RELEVANT_PER_QUERY} 篇")
        if len(set(dois)) != len(dois):
            raise ValueError("同一題的正解 DOI 重複")
        if self.query_set is EvalQuerySet.SIMILAR and self.query in dois:
            raise ValueError("找相似論文的正解不能是起點自己")

    @property
    def key(self) -> tuple[EvalQuerySet, str]:
        return (self.query_set, self.query_id)


class EvalQueriesFileError(ValueError):
    """題目檔不收：`problems` 是每個問題（列號與原因，不含格子的內容）。"""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__(f"題目檔有 {len(self.problems)} 個問題")


@dataclass(frozen=True, slots=True)
class ParsedEvalQueries:
    """驗過的題目檔：題目（照第一次出現的順序）、跳過的空列數。"""

    questions: tuple[EvalQuestion, ...]
    blank_lines: int


def _decode(data: bytes) -> str:
    if data.startswith(_UTF8_BOM):
        data = data[len(_UTF8_BOM) :]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvalQueriesFileError(
            [f"題目檔不是 UTF-8（第 {exc.start + 1:,} 個位元組讀不懂）；請另存成 UTF-8 的 CSV"]
        ) from None


def _has_rejected_character(value: str) -> bool:
    return any(is_rejected_character(char) for char in value)


def _header(row: Sequence[str]) -> tuple[dict[str, int], list[str]]:
    problems: list[str] = []
    positions: dict[str, int] = {}
    known = set(REQUIRED_COLUMNS) | set(OPTIONAL_COLUMNS)
    for index, raw in enumerate(row):
        name = raw.strip()
        if name not in known:
            problems.append(f"第 1 列：第 {index + 1} 欄的欄位名稱不認得（只收 {_column_list()}）")
        elif name in positions:
            problems.append(f"第 1 列：欄位 {name} 寫了兩次")
        else:
            positions[name] = index
    problems += [f"第 1 列：少了欄位 {name}" for name in REQUIRED_COLUMNS if name not in positions]
    return positions, problems


def _column_list() -> str:
    return "、".join([*REQUIRED_COLUMNS, *(f"{name}（選填）" for name in OPTIONAL_COLUMNS)])


@dataclass
class _Draft:
    query_set: EvalQuerySet
    query_id: str
    query: str
    first_line: int
    relevant: list[EvalRelevant] = field(default_factory=list)


def _parse_row(
    line: int, cells: Mapping[str, str], *, query_max_length: int
) -> tuple[EvalQuerySet, str, str, EvalRelevant] | list[str]:
    problems: list[str] = []
    set_text = cells["set"].strip()
    query_set: EvalQuerySet | None = None
    try:
        query_set = EvalQuerySet(set_text)
    except ValueError:
        problems.append(f"第 {line} 列：set 只能是 search 或 similar")

    query_id = cells["query_id"].strip()
    if _has_rejected_character(cells["query_id"]):
        problems.append(f"第 {line} 列：query_id 有控制字元或看不見的格式字元")
    elif not query_id:
        problems.append(f"第 {line} 列：query_id 是空的")
    elif len(query_id) > QUERY_ID_MAX_LENGTH:
        problems.append(f"第 {line} 列：query_id 超過 {QUERY_ID_MAX_LENGTH} 個字")

    query = ""
    if query_set is EvalQuerySet.SEARCH:
        try:
            query = clean_search_query(cells["query"], max_length=query_max_length)
        except InvalidSearchQueryError as exc:
            problems.append(f"第 {line} 列：query 不收（{exc.problem.value}）")
    elif query_set is EvalQuerySet.SIMILAR:
        try:
            query = normalize_doi(cells["query"])
        except InvalidIdentifierError:
            problems.append(f"第 {line} 列：similar 的 query 要是起點論文的 DOI，這個看不懂")

    doi = ""
    try:
        doi = normalize_doi(cells["relevant_doi"])
    except InvalidIdentifierError:
        problems.append(f"第 {line} 列：relevant_doi 看不懂（要是 DOI）")

    raw_note = cells.get("note", "")
    note = raw_note.strip()
    if _has_rejected_character(raw_note):
        problems.append(f"第 {line} 列：note 有控制字元或看不見的格式字元")
    elif len(note) > NOTE_MAX_LENGTH:
        problems.append(f"第 {line} 列：note 超過 {NOTE_MAX_LENGTH} 個字")

    if problems or query_set is None:
        return problems
    return query_set, query_id, query, EvalRelevant(doi=doi, note=note or None, line=line)


def parse_eval_queries_csv(data: bytes, *, query_max_length: int) -> ParsedEvalQueries:
    """驗整份題目檔（規則見模組說明）。`query_max_length` 是搜尋的查詢長度上限
    （`RetrievalConfig.query_max_length`，和搜尋一樣）。

    Raises:
        EvalQueriesFileError: 有任何一個問題（全部列出）。
    """
    if not isinstance(data, bytes):
        raise TypeError("題目檔要是 bytes")
    if len(data) > MAX_FILE_BYTES:
        raise EvalQueriesFileError([f"題目檔超過 {MAX_FILE_BYTES:,} 位元組"])
    text = _decode(data)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    problems: list[str] = []
    drafts: dict[tuple[EvalQuerySet, str], _Draft] = {}
    seen: dict[tuple[EvalQuerySet, str, str], int] = {}
    blank_lines = 0
    data_rows = 0
    try:
        header = next(reader, None)
        if header is None or not any(cell.strip() for cell in header):
            raise EvalQueriesFileError([f"第 1 列要是欄位名稱：{_column_list()}"])
        positions, header_problems = _header(header)
        if header_problems:
            raise EvalQueriesFileError(header_problems)
        for row in reader:
            line = reader.line_num
            if not any(cell.strip() for cell in row):
                blank_lines += 1
                continue
            data_rows += 1
            if data_rows > MAX_DATA_ROWS:
                problems.append(f"題目檔超過 {MAX_DATA_ROWS:,} 列，後面的沒有看")
                break
            if len(row) != len(header):
                problems.append(f"第 {line} 列：有 {len(row)} 欄，欄位名稱是 {len(header)} 欄")
                continue
            cells = {name: row[index] for name, index in positions.items()}
            parsed = _parse_row(line, cells, query_max_length=query_max_length)
            if isinstance(parsed, list):
                problems += parsed
                continue
            query_set, query_id, query, relevant = parsed
            key = (query_set, query_id)
            draft = drafts.get(key)
            if draft is None:
                draft = drafts[key] = _Draft(query_set, query_id, query, first_line=line)
            elif draft.query != query:
                problems.append(
                    f"第 {line} 列：同一題（{query_set.value} {query_id}）的 query 和第"
                    f" {draft.first_line} 列不一樣"
                )
                continue
            if (earlier := seen.get((query_set, query_id, relevant.doi))) is not None:
                problems.append(f"第 {line} 列：和第 {earlier} 列重複（同一題、同一篇正解）")
                continue
            seen[(query_set, query_id, relevant.doi)] = line
            if query_set is EvalQuerySet.SIMILAR and relevant.doi == query:
                problems.append(f"第 {line} 列：找相似論文的正解不能是起點自己")
                continue
            draft.relevant.append(relevant)
    except csv.Error:
        problems.append(f"第 {reader.line_num} 列：CSV 格式不對（例如引號沒有成對）")

    for draft in drafts.values():
        if len(draft.relevant) > MAX_RELEVANT_PER_QUERY:
            problems.append(
                f"第 {draft.first_line} 列起的那一題（{draft.query_set.value} {draft.query_id}）"
                f"有 {len(draft.relevant)} 篇正解，最多 {MAX_RELEVANT_PER_QUERY} 篇"
            )
    if not problems and not drafts:
        problems.append("題目檔沒有任何題目")
    if problems:
        raise EvalQueriesFileError(problems)
    return ParsedEvalQueries(
        questions=tuple(
            EvalQuestion(d.query_set, d.query_id, d.query, tuple(d.relevant))
            for d in drafts.values()
        ),
        blank_lines=blank_lines,
    )


# --- 匯入時與跑題時的跳過 -----------------------------------------------------------------------


class EvalSkipReason(StrEnum):
    """一篇正解或一整題為什麼跳過。"""

    RELEVANT_NOT_FOUND = "relevant_not_found"  # 正解的 DOI 對不到論文（只跳過這一篇）
    ANCHOR_NOT_FOUND = "anchor_not_found"  # 找相似論文的起點 DOI 對不到論文（整題跳過）
    NO_RELEVANT_LEFT = "no_relevant_left"  # 正解都對不到論文（整題跳過）
    ANCHOR_UNAVAILABLE = "anchor_unavailable"  # 跑題時起點看不到或被排除了（整題跳過）


SKIP_LABELS: Final[Mapping[EvalSkipReason, str]] = {
    EvalSkipReason.RELEVANT_NOT_FOUND: "正解的 DOI 對不到論文，這一篇跳過",
    EvalSkipReason.ANCHOR_NOT_FOUND: "起點論文的 DOI 對不到論文，整題跳過",
    EvalSkipReason.NO_RELEVANT_LEFT: "正解都對不到論文，整題跳過",
    EvalSkipReason.ANCHOR_UNAVAILABLE: "起點論文被排除或看不到，整題跳過",
}


@dataclass(frozen=True, slots=True)
class EvalSkip:
    """跳過的一篇正解或一整題：哪一題、哪個 DOI（整題跳過且不是 DOI 的問題時是 None）、原因、第幾列
    （跑題時是 None）。"""

    query_set: EvalQuerySet
    query_id: str
    doi: str | None
    reason: EvalSkipReason
    line: int | None = None


def plan_eval_import(
    questions: Iterable[EvalQuestion], found_dois: frozenset[str]
) -> tuple[tuple[EvalQuestion, ...], tuple[EvalSkip, ...]]:
    """照「哪些 DOI 對得到論文」（`found_dois`，正規化後）決定要匯入的題目：對不到的正解跳過；
    `similar` 的起點對不到、或正解一篇都不剩的題目整題跳過。回（要匯入的題目, 跳過的）。"""
    kept: list[EvalQuestion] = []
    skips: list[EvalSkip] = []
    for question in questions:
        if question.query_set is EvalQuerySet.SIMILAR and question.query not in found_dois:
            first = question.relevant[0].line
            skips.append(
                EvalSkip(
                    question.query_set,
                    question.query_id,
                    question.query,
                    EvalSkipReason.ANCHOR_NOT_FOUND,
                    first,
                )
            )
            continue
        relevant = []
        for item in question.relevant:
            if item.doi in found_dois:
                relevant.append(item)
            else:
                skips.append(
                    EvalSkip(
                        question.query_set,
                        question.query_id,
                        item.doi,
                        EvalSkipReason.RELEVANT_NOT_FOUND,
                        item.line,
                    )
                )
        if not relevant:
            skips.append(
                EvalSkip(
                    question.query_set,
                    question.query_id,
                    None,
                    EvalSkipReason.NO_RELEVANT_LEFT,
                    question.relevant[0].line,
                )
            )
            continue
        kept.append(
            EvalQuestion(question.query_set, question.query_id, question.query, tuple(relevant))
        )
    return tuple(kept), tuple(skips)


def eval_doi_targets(questions: Iterable[EvalQuestion]) -> frozenset[str]:
    """這些題目要查的全部 DOI（正解與 `similar` 的起點，正規化後）。"""
    dois: set[str] = set()
    for question in questions:
        dois.update(item.doi for item in question.relevant)
        if question.query_set is EvalQuerySet.SIMILAR:
            dois.add(question.query)
    return frozenset(dois)


# --- 相似度下限 ---------------------------------------------------------------------------------


def parse_min_similarities(values: Sequence[str]) -> tuple[Decimal, ...]:
    """`--min-similarity` 的值：每個要是 0（含）到 1（不含）的十進位數字，最多
    `MAX_MIN_SIMILARITIES` 個；去重後由小到大。

    Raises:
        ValueError: 不是數字、不在範圍內、或給太多個（訊息不含原值以外的東西）。
    """
    if len(values) > MAX_MIN_SIMILARITIES:
        raise ValueError(f"--min-similarity 最多給 {MAX_MIN_SIMILARITIES} 個")
    parsed: set[Decimal] = set()
    for raw in values:
        text = raw.strip()
        try:
            value = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"--min-similarity 要是數字（{text[:20]!r} 看不懂）") from None
        if not value.is_finite() or not Decimal(0) <= value < Decimal(1):
            raise ValueError("--min-similarity 要在 0（含）到 1（不含）之間")
        parsed.add(value.normalize() if value != 0 else Decimal(0))
    return tuple(sorted(parsed))


# --- 命中率與 MRR -----------------------------------------------------------------------------


def leg_rankings(hits: Sequence[RetrievalHit]) -> dict[EvalLeg, tuple[uuid.UUID, ...]]:
    """一次 `retrieve` 的全部結果 → 三組排名的前 `TOP_N` 篇（見模組說明）。`hits` 要是融合後的全部
    結果（`k` 給到 `max_results`），只用一條腿的排名才完整。"""
    fused = sorted(hits, key=lambda hit: hit.rank)
    vector = sorted((hit for hit in hits if hit.vector_rank is not None), key=_vector_rank)
    text = sorted((hit for hit in hits if hit.text_rank is not None), key=_text_rank)
    return {
        EvalLeg.TEXT: tuple(hit.paper_id for hit in text[:TOP_N]),
        EvalLeg.VECTOR: tuple(hit.paper_id for hit in vector[:TOP_N]),
        EvalLeg.FUSED: tuple(hit.paper_id for hit in fused[:TOP_N]),
    }


def _vector_rank(hit: RetrievalHit) -> int:
    assert hit.vector_rank is not None
    return hit.vector_rank


def _text_rank(hit: RetrievalHit) -> int:
    assert hit.text_rank is not None
    return hit.text_rank


def first_hit_rank(ranked: Sequence[uuid.UUID], relevant: Iterable[uuid.UUID]) -> int | None:
    """`ranked`（前 `TOP_N` 篇）裡第一篇正解的名次（1 起）；沒有是 None。"""
    wanted = set(relevant)
    for index, paper_id in enumerate(ranked[:TOP_N]):
        if paper_id in wanted:
            return index + 1
    return None


@dataclass(frozen=True, slots=True)
class EvalLegOutcome:
    """一題在一組排名的結果：命中名次（沒命中是 None）、這一組有沒有任何結果。"""

    rank: int | None
    has_results: bool

    def __post_init__(self) -> None:
        if self.rank is not None and (type(self.rank) is not int or not 1 <= self.rank <= TOP_N):
            raise ValueError(f"命中名次要是 1～{TOP_N}")
        if self.rank is not None and not self.has_results:
            raise ValueError("沒有結果就不會命中")


@dataclass(frozen=True, slots=True)
class EvalQuestionOutcome:
    """一題在一個相似度下限的結果：每組排名各一個。"""

    query_id: str
    legs: Mapping[EvalLeg, EvalLegOutcome]


def question_outcome(
    query_id: str,
    hits: Sequence[RetrievalHit],
    relevant: Iterable[uuid.UUID],
    legs: Sequence[EvalLeg],
) -> EvalQuestionOutcome:
    """一題的結果（`legs` 是這一組要報的排名）。"""
    wanted = frozenset(relevant)
    rankings = leg_rankings(hits)
    return EvalQuestionOutcome(
        query_id=query_id,
        legs={
            leg: EvalLegOutcome(
                rank=first_hit_rank(rankings[leg], wanted), has_results=bool(rankings[leg])
            )
            for leg in legs
        },
    )


@dataclass(frozen=True, slots=True)
class EvalLegSummary:
    """一組排名的整體：題數、命中題數、MRR（精確分數）、沒有任何結果的題數。"""

    questions: int
    hits: int
    mrr: Fraction
    no_results: int

    @property
    def hit_rate(self) -> Fraction:
        return Fraction(self.hits, self.questions) if self.questions else Fraction(0)


def summarize_leg(outcomes: Sequence[EvalQuestionOutcome], leg: EvalLeg) -> EvalLegSummary:
    """`outcomes` 在 `leg` 這組排名的整體數字（沒有題目時都是 0）。"""
    chosen = [outcome.legs[leg] for outcome in outcomes]
    reciprocal = sum(
        (Fraction(1, item.rank) for item in chosen if item.rank is not None), Fraction(0)
    )
    return EvalLegSummary(
        questions=len(chosen),
        hits=sum(1 for item in chosen if item.rank is not None),
        mrr=reciprocal / len(chosen) if chosen else Fraction(0),
        no_results=sum(1 for item in chosen if not item.has_results),
    )


@dataclass(frozen=True, slots=True)
class EvalSetResult:
    """一組題目在一個相似度下限的結果（照題目的順序）。"""

    query_set: EvalQuerySet
    outcomes: tuple[EvalQuestionOutcome, ...]

    @property
    def legs(self) -> tuple[EvalLeg, ...]:
        return SET_LEGS[self.query_set]

    def summary(self, leg: EvalLeg) -> EvalLegSummary:
        return summarize_leg(self.outcomes, leg)


@dataclass(frozen=True, slots=True)
class EvalFloorResult:
    """一個相似度下限的結果：有題目的每一組。"""

    min_similarity: Decimal
    sets: tuple[EvalSetResult, ...]


@dataclass(frozen=True, slots=True)
class SearchEvalReport:
    """`radar eval search` 一次的結果。

    - `snapshot_number`／`snapshot_id`：第一題排名時讀到的已發佈快照（還沒有快照是 None）；跑到一半
      換了快照時 `snapshot_changed` 是 True（這次的數字混了兩個快照，要重跑）。
    - `query_count`：實際跑的題數（兩組加起來，不含整題跳過的）。
    """

    eval_run_id: uuid.UUID
    model_id: str
    snapshot_id: uuid.UUID | None
    snapshot_number: int | None
    snapshot_changed: bool
    query_count: int
    floors: tuple[EvalFloorResult, ...]
    skips: tuple[EvalSkip, ...]
    started_at: datetime
    finished_at: datetime


def _ratio_text(value: Fraction) -> str:
    """分數 → 4 位小數的字串（存進 JSON 用，不用浮點數）。"""
    return str((Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.0001")))


def eval_results_json(report: SearchEvalReport) -> dict[str, object]:
    """存進 `eval_run.results` 的內容：每個下限、每組、每組排名的整體數字與每題的命中名次，以及
    跳過的正解與題目。只有編號、DOI 與數字，不放查詢內容。"""
    return {
        "format_version": RESULTS_FORMAT_VERSION,
        "top_n": TOP_N,
        "snapshot_changed": report.snapshot_changed,
        "floors": [
            {
                "min_similarity": str(floor.min_similarity),
                "sets": {
                    result.query_set.value: {
                        "summary": {
                            leg.value: {
                                "questions": summary.questions,
                                "hits": summary.hits,
                                "hit_rate": _ratio_text(summary.hit_rate),
                                "mrr": _ratio_text(summary.mrr),
                                "no_results": summary.no_results,
                            }
                            for leg in result.legs
                            for summary in (result.summary(leg),)
                        },
                        "questions": [
                            {
                                "query_id": outcome.query_id,
                                "ranks": {leg.value: outcome.legs[leg].rank for leg in result.legs},
                                "has_results": {
                                    leg.value: outcome.legs[leg].has_results for leg in result.legs
                                },
                            }
                            for outcome in result.outcomes
                        ],
                    }
                    for result in floor.sets
                },
            }
            for floor in report.floors
        ],
        "skips": [
            {
                "set": skip.query_set.value,
                "query_id": skip.query_id,
                "doi": skip.doi,
                "reason": skip.reason.value,
            }
            for skip in report.skips
        ],
    }


# --- 印出來 ----------------------------------------------------------------------------------


def _percent(value: Fraction) -> str:
    return f"{float(value) * 100:.1f}%"


def _rank_text(outcome: EvalLegOutcome) -> str:
    if outcome.rank is not None:
        return f"第 {outcome.rank} 名"
    return "沒命中" if outcome.has_results else "沒有結果"


def render_search_eval(report: SearchEvalReport, *, label: Callable[[str], str] = str) -> list[str]:
    """一次評估印給 Kevin 看的報告。`label` 把資料字串（題目編號、DOI）轉成能安全印到終端機的字
    （CLI 傳 `admin_runs.terminal_text`）。"""
    snapshot = (
        "還沒有已發佈的快照"
        if report.snapshot_number is None
        else f"第 {report.snapshot_number} 號"
    )
    lines = [
        f"搜尋評估（eval_run {report.eval_run_id}）",
        f"向量模型：{report.model_id}",
        f"快照：{snapshot}",
        f"相似度下限：{'、'.join(str(floor.min_similarity) for floor in report.floors)}",
        f"題數：{report.query_count}（範圍 tracked；看前 {TOP_N} 名）",
    ]
    if report.snapshot_changed:
        lines.append("注意：跑到一半換了快照，這次的數字混了兩個快照，請重跑")
    for floor in report.floors:
        lines += ["", f"== 相似度下限 {floor.min_similarity} =="]
        for result in floor.sets:
            lines.append(f"[{SET_LABELS[result.query_set]}] {len(result.outcomes)} 題")
            for leg in result.legs:
                summary = result.summary(leg)
                lines.append(
                    f"  {LEG_LABELS[leg]}：命中 {summary.hits}/{summary.questions}"
                    f"（{_percent(summary.hit_rate)}），MRR {float(summary.mrr):.3f}，"
                    f"找不到相關的論文 {summary.no_results} 題"
                )
            lines.append("  每題：")
            for outcome in result.outcomes:
                parts = "；".join(
                    f"{LEG_LABELS[leg]} {_rank_text(outcome.legs[leg])}" for leg in result.legs
                )
                lines.append(f"    {label(outcome.query_id)}：{parts}")
    if report.skips:
        lines += ["", "跳過的："]
        lines += [_skip_line(skip, label) for skip in report.skips]
    return lines


def _skip_line(skip: EvalSkip, show: Callable[[str], str]) -> str:
    where = f"第 {skip.line} 列 " if skip.line is not None else ""
    doi = f" {show(skip.doi)}" if skip.doi is not None else ""
    return f"  {where}{skip.query_set.value} {show(skip.query_id)}{doi}：{SKIP_LABELS[skip.reason]}"


@dataclass(frozen=True, slots=True)
class EvalImportResult:
    """匯入的結果：存了幾題、幾篇正解（各組）、拿掉了幾題舊的、跳過的、空列數。"""

    imported: Mapping[EvalQuerySet, int]
    relevant: int
    replaced: int
    skips: tuple[EvalSkip, ...]
    blank_lines: int


def render_eval_import(result: EvalImportResult, *, label: Callable[[str], str] = str) -> list[str]:
    """匯入結果印給 Kevin 看的幾行（`label` 同 `render_search_eval`）。"""
    counts = "、".join(
        f"{query_set.value} {result.imported.get(query_set, 0)} 題" for query_set in EvalQuerySet
    )
    lines = [
        f"已匯入評估題目：{counts}，正解共 {result.relevant} 篇"
        f"（整份取代原本的 {result.replaced} 題）"
    ]
    if result.blank_lines:
        lines.append(f"跳過空列 {result.blank_lines} 列")
    if result.skips:
        lines.append(f"跳過的（{len(result.skips)} 筆）：")
        lines += [_skip_line(skip, label) for skip in result.skips]
    return lines
