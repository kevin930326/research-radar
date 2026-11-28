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
- 找論文清單 API 的搜尋與找相似論文（`GET /api/v1/papers` 的 `q`、`similar_to`，Story 4.1 PR B、
  ADR-0050 的 2026-11 更新）：下一頁的位置是融合後名次的位移（`RetrievalCursor`，和清單的 keyset
  位置是兩種寫法，互相不收）；一頁的形狀是 `PaperSearchPage`。
"""

import base64
import binascii
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from radar.domain.errors import ErrorCode
from radar.domain.member_text_characters import ZERO_WIDTH_JOINER, is_rejected_character
from radar.domain.paper_list import PaperListScope
from radar.domain.paper_rows import PaperRowFacts
from radar.domain.snapshot import Snapshot, SnapshotStatus

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

    `snapshot`：排名時（`this_week` 篩選用）讀到的已發佈快照；還沒有快照、或沒有讀（例如直接組的
    結果）是 None。回應的 `snapshot` 要用這一個，篩選與回應才對得上（PR B 審查 M3）。

    Raises:
        ValueError: 名次不是從 `offset + 1` 連續、篇數超過 `k`，或 `total` 比翻得到的少。
    """

    hits: tuple[RetrievalHit, ...]
    total: int
    offset: int
    k: int
    snapshot: Snapshot | None = None

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


# --- 找論文清單 API 的搜尋與找相似論文（Story 4.1 PR B）-----------------------------------------

# 位移的上限：`RetrievalConfig.max_results` 最大是 2 × 1000（`leg_limit` 的上限）；超過的一定不是
# 我們產生的。
MAX_RETRIEVAL_OFFSET: Final = 2 * 1000
# 位置字串的長度上限（正常的長度約 12 個字元；太長的直接當格式錯）。
_RETRIEVAL_CURSOR_MAX_LENGTH: Final = 40
_RETRIEVAL_CURSOR_SEPARATOR: Final = ":"


class RetrievalMode(StrEnum):
    """這一頁是哪一種檢索：搜尋（`q`）或找相似論文（`similar_to`）。寫進下一頁的位置，拿搜尋的位置
    去翻找相似論文（或反過來）就不收。"""

    SEARCH = "search"
    SIMILAR = "similar"


@dataclass(frozen=True, slots=True)
class RetrievalCursor:
    """檢索的下一頁從哪裡開始：哪一種檢索、融合後名次的位移（下一頁第一篇是第 `offset + 1` 名）。

    Raises:
        ValueError: `offset` 不是 1 到 `MAX_RETRIEVAL_OFFSET` 的整數（第一頁沒有位置）。
    """

    mode: RetrievalMode
    offset: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", RetrievalMode(self.mode))
        if type(self.offset) is not int or not 1 <= self.offset <= MAX_RETRIEVAL_OFFSET:
            raise ValueError(f"offset 要是 1 到 {MAX_RETRIEVAL_OFFSET} 的整數")


def encode_retrieval_cursor(cursor: RetrievalCursor) -> str:
    """位置 → 不透明的字串（URL 安全的 base64、沒有 `=`；和清單的 keyset 位置用不同的分隔符號）。"""
    plain = f"{cursor.mode.value}{_RETRIEVAL_CURSOR_SEPARATOR}{cursor.offset}"
    return base64.urlsafe_b64encode(plain.encode("ascii")).decode("ascii").rstrip("=")


def decode_retrieval_cursor(value: str, *, mode: RetrievalMode) -> RetrievalCursor:
    """`encode_retrieval_cursor` 的反向；只收它產生的寫法（重新編碼要一模一樣），而且要是 `mode`
    這一種檢索的位置。清單的 keyset 位置（`domain/paper_list.encode_paper_list_cursor`）一定不收。

    Raises:
        ValueError: 不是 `encode_retrieval_cursor` 產生的字串，或是另一種檢索的位置。
    """
    if not isinstance(value, str) or not 0 < len(value) <= _RETRIEVAL_CURSOR_MAX_LENGTH:
        raise ValueError("下一頁的位置格式不對")
    try:
        padded = value + "=" * (-len(value) % 4)
        plain = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
        mode_text, offset_text = plain.split(_RETRIEVAL_CURSOR_SEPARATOR)
        if not offset_text.isdigit() or not offset_text.isascii():
            raise ValueError("位移不是數字")
        cursor = RetrievalCursor(mode=RetrievalMode(mode_text), offset=int(offset_text))
        same = encode_retrieval_cursor(cursor) == value
    except (UnicodeError, binascii.Error, ValueError) as exc:
        raise ValueError("下一頁的位置格式不對") from exc
    if not same or cursor.mode is not RetrievalMode(mode):
        raise ValueError("下一頁的位置格式不對")
    return cursor


@dataclass(frozen=True, slots=True)
class PaperSearchPage:
    """搜尋或找相似論文的一頁（照融合後的名次）：這一頁的論文列、下一頁的位置（沒有下一頁是 None）、
    `total`（`RetrievalPage.total`，算得上結果的篇數）、目前已發佈的快照（一個都還沒有是 None）。

    論文列和名次是兩個交易讀的（中間有一篇被排除時那一篇讀不到、跳過），所以不檢查篇數對得上
    `total`。

    Raises:
        ValueError: `total` 不是非負整數、快照還沒發佈。
    """

    rows: tuple[PaperRowFacts, ...]
    next_cursor: str | None
    total: int
    snapshot: Snapshot | None

    def __post_init__(self) -> None:
        if type(self.total) is not int or self.total < 0:
            raise ValueError("total 要是非負整數")
        if self.snapshot is not None and (
            self.snapshot.status is not SnapshotStatus.PUBLISHED or self.snapshot.as_of is None
        ):
            raise ValueError("快照要是已發佈的")
