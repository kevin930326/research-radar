"""檢索的 domain 形狀（Story 4.1、FR-26、FR-27、ADR-0005）：範圍、設定值、查詢字串的驗證、結果與
錯誤。SQL 在 `repo/retrieval.py`，流程在 `services/retrieval.py`。

- 範圍（`RetrievalScope`）：`personal`、`lab`、`tracked` 和找論文清單（`PaperListScope`）同名同義；
  `demo` 是示範站有完整內容的論文（view `demo_full_paper`），只給 `PublicViewer`。清單的範圍型別不加
  `demo`（清單 API 不收它），檢索另外一個型別；`retrieval_scope_for` 把清單的範圍換成檢索的。
- 設定（`config/retrieval.yaml` → `RetrievalConfig`）：向量腿的相似度下限、每條腿取幾名、RRF 的
  k、每頁幾篇、問答取幾篇、查詢字串的長度上限。
- 查詢字串當使用者輸入驗（`clean_search_query`）：不是字串、有控制字元或看不見的格式字元
  （`domain/member_text_characters` 的共用清單）、去掉頭尾空白後是空的、超過長度上限都不收
  （`InvalidSearchQueryError`，訊息只有原因代號，不含查詢內容）。
- 日誌不記查詢內容，只記長度與有沒有中文（`query_log_facts`）。
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from radar.domain.errors import ErrorCode
from radar.domain.member_text_characters import ZERO_WIDTH_JOINER, is_rejected_character
from radar.domain.paper_list import PaperListScope

# 查詢字串長度上限的上限（設定檔寫錯成很大的數字時擋下）。
MAX_QUERY_LENGTH_LIMIT: Final = 2000


class RetrievalScope(StrEnum):
    """檢索從哪一群論文找（ADR-0005 第 2 點）。不管哪一種，都先套 viewer 的可見條件、再套範圍。"""

    PERSONAL = "personal"  # 這位成員的個人文獻庫
    LAB = "lab"  # 全實驗室看得到的論文（和清單的 `lab` 同一個條件）
    TRACKED = "tracked"  # 算數的論文（view `counted_paper`）
    DEMO = "demo"  # 示範站有完整內容的論文（view `demo_full_paper`），只給 `PublicViewer`


def retrieval_scope_for(scope: PaperListScope) -> RetrievalScope:
    """找論文清單的範圍 → 同名的檢索範圍（清單沒有 `demo`）。"""
    return RetrievalScope(PaperListScope(scope).value)


class RetrievalConfig(BaseModel):
    """`config/retrieval.yaml`（數字的意思見那份檔案的註解）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 向量腿的相似度下限（餘弦相似度）；初始值在 4.4 量過再調。
    vector_min_similarity: Decimal = Field(ge=0, lt=1, allow_inf_nan=False)
    # 每條腿取幾名（pgvector 的 `hnsw.ef_search` 上限是 1000）。
    leg_limit: int = Field(ge=1, le=1000, strict=True)
    rrf_k: int = Field(ge=1, le=1000, strict=True)
    page_size: int = Field(ge=1, le=100, strict=True)
    qa_paper_count: int = Field(ge=1, le=100, strict=True)
    query_max_length: int = Field(ge=1, le=MAX_QUERY_LENGTH_LIMIT, strict=True)

    @property
    def max_results(self) -> int:
        """融合後最多幾篇（兩條腿都取滿、沒有重疊時）。"""
        return 2 * self.leg_limit

    @property
    def vector_max_distance(self) -> Decimal:
        """相似度下限換成 pgvector 的餘弦距離上限（距離＝1－相似度）。"""
        return 1 - self.vector_min_similarity


# --- 查詢字串 ---------------------------------------------------------------------------------


class SearchQueryProblem(StrEnum):
    """查詢字串為什麼不收。"""

    NOT_TEXT = "query_not_text"  # 不是字串
    EMPTY = "query_empty"  # 空的，或去掉空白後是空的
    TOO_LONG = "query_too_long"  # 超過長度上限
    INVALID_CHARACTER = "query_invalid_character"  # 控制字元、看不見的格式字元、落單的代理字元


class InvalidSearchQueryError(ValueError):
    """查詢字串不收。訊息只有原因代號，不含查詢內容。"""

    def __init__(self, problem: SearchQueryProblem) -> None:
        self.problem = problem
        super().__init__(problem.value)


def clean_search_query(value: object, *, max_length: int) -> str:
    """驗查詢字串，回去掉頭尾空白的字。先看字元再去空白（頭尾的控制字元也不收）。

    Raises:
        InvalidSearchQueryError: 不收的原因（見模組說明）。
    """
    if not isinstance(value, str):
        raise InvalidSearchQueryError(SearchQueryProblem.NOT_TEXT)
    if any(is_rejected_character(char) for char in value):
        raise InvalidSearchQueryError(SearchQueryProblem.INVALID_CHARACTER)
    text = value.strip()
    # 只有空白與零寬連接（U+200D 本身看不見）也算空的。
    if not text.replace(ZERO_WIDTH_JOINER, "").strip():
        raise InvalidSearchQueryError(SearchQueryProblem.EMPTY)
    if len(text) > max_length:
        raise InvalidSearchQueryError(SearchQueryProblem.TOO_LONG)
    return text


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF  # CJK 擴充 A
        or 0x4E00 <= code <= 0x9FFF  # CJK 基本
        or 0xF900 <= code <= 0xFAFF  # 相容表意字
        or 0x20000 <= code <= 0x3134F  # 擴充 B～G
    )


def query_log_facts(text: str) -> dict[str, object]:
    """查詢字串可以寫進日誌的事實：長度、有沒有中文字（不寫內容）。"""
    return {"query_length": len(text), "query_has_cjk": any(_is_cjk(char) for char in text)}


# --- 結果 -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FusedMatch:
    """融合後的一篇（`repo/retrieval.search_papers` 回的，已照融合後的順序排好）：在向量腿、
    全文檢索腿的名次（1 起；那條腿沒有這篇是 None，兩個不會都是 None）。"""

    paper_id: uuid.UUID
    vector_rank: int | None
    text_rank: int | None

    def __post_init__(self) -> None:
        if self.vector_rank is None and self.text_rank is None:
            raise ValueError("至少要在一條腿裡")
        for rank in (self.vector_rank, self.text_rank):
            if rank is not None and (type(rank) is not int or rank < 1):
                raise ValueError("名次要是 ≥ 1 的整數")


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """一頁裡的一篇：論文 ID、融合後的名次（1 起，跨頁連續）、兩條腿各自的名次。"""

    paper_id: uuid.UUID
    rank: int
    vector_rank: int | None
    text_rank: int | None


@dataclass(frozen=True, slots=True)
class RetrievalPage:
    """`retrieve` 的結果：這一頁（融合後名次 `offset + 1` 起最多 `k` 篇）與 `total`（算得上結果的
    篇數，最多 `RetrievalConfig.max_results`；和翻得到的篇數一致）。只有 ID 與名次，不含內容。

    Raises:
        ValueError: 名次不是從 `offset + 1` 連續、篇數超過 `k`，或 `total` 比翻得到的少。
    """

    hits: tuple[RetrievalHit, ...]
    total: int
    offset: int
    k: int

    def __post_init__(self) -> None:
        if type(self.total) is not int or self.total < 0:
            raise ValueError("total 要是非負整數")
        if len(self.hits) > self.k:
            raise ValueError("篇數超過 k")
        if [hit.rank for hit in self.hits] != list(
            range(self.offset + 1, self.offset + 1 + len(self.hits))
        ):
            raise ValueError("名次要從 offset + 1 連續")
        if self.hits and self.hits[-1].rank > self.total:
            raise ValueError("total 比翻得到的篇數少")


# --- 錯誤 -------------------------------------------------------------------------------------


class SearchQuotaExhaustedError(Exception):
    """Workers AI 今天的額度用完了，查詢沒有轉成向量（沒有送出）。不降級成只用關鍵字。"""

    code: ClassVar[ErrorCode] = ErrorCode.SEARCH_QUOTA_EXHAUSTED

    def __init__(self) -> None:
        super().__init__(self.code.value)


class SearchEmbeddingUnavailableError(Exception):
    """查詢沒能轉成向量（Workers AI 連不上、回了錯、回應不對，或等額度鎖逾時）。不降級成只用關鍵字。
    訊息只有原因代號。"""

    code: ClassVar[ErrorCode] = ErrorCode.SEARCH_EMBEDDING_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__(self.code.value)


class RetrievalAnchorNotFoundError(LookupError):
    """找相似論文的起點不存在、看不到或是被排除的文章（三種走同一條路，不透露論文存在；
    api 回 404）。"""

    def __init__(self) -> None:
        super().__init__("找不到這篇論文")
