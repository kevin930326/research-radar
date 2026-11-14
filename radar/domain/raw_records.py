"""原始紀錄（AD-25、ADR-0014）：`PaperSource` 抓回來、還沒解析的東西，與每次 API 呼叫的紀錄。

- `RawRecord.content` 是回應原文裡那一篇的原樣位元組（不重新序列化、不挑欄位）；
  `sha256` 算在這份位元組上，存檔時壓縮成 `compress_raw` 的結果。
- `ApiCall` 是一次 HTTP 嘗試（含重試的每一次），寫進 `api_call` 記額度（FR-10、AD-29）。
"""

import gzip
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from functools import cached_property

from radar.domain.identifiers import normalize_issn_l, normalize_openalex_id

# numeric(12,6) 放得下的上限（不含）。
MAX_USD = Decimal(1_000_000)
_OPERATION = re.compile(r"[a-z][a-z0-9_]*")


class RawSource(StrEnum):
    """原始資料的來源（`raw_fetch.source`、`api_call.source`、物件鍵的第二段）。"""

    OPENALEX = "openalex"


def _require_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} 要是 datetime（現在是 {type(value).__name__}）")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 要帶時區")


def _check_usd(value: object, name: str, *, optional: bool) -> None:
    if value is None and optional:
        return
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} 要是 Decimal（現在是 {type(value).__name__}）")
    if not value.is_finite() or not Decimal(0) <= value < MAX_USD:
        raise ValueError(f"{name} 要是 0 到 {MAX_USD} 之間的有限數字（現在是 {value}）")


@dataclass(frozen=True)
class JournalQuery:
    """依一本期刊抓的條件：ISSN-L（已正規化）與起始年（含）。"""

    issn_l: str
    start_year: int

    def __post_init__(self) -> None:
        if normalize_issn_l(self.issn_l) != self.issn_l:
            raise ValueError(f"issn_l 還沒正規化：{self.issn_l!r}")
        if (
            not isinstance(self.start_year, int)
            or isinstance(self.start_year, bool)
            or not 1000 <= self.start_year <= 9999
        ):
            raise ValueError(f"start_year 要是 1000–9999 的整數（現在是 {self.start_year!r}）")


@dataclass(frozen=True)
class RawRecord:
    """一篇論文的原始內容。

    - `external_key`：正規化後的 OpenAlex ID（`W` 開頭）。
    - `content`：回應原文裡那一篇的原樣位元組，不可為空。
    - `fetched_at`：收到那一頁回應的時間，帶時區。
    """

    source: RawSource
    external_key: str
    content: bytes
    fetched_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", RawSource(self.source))
        if not isinstance(self.external_key, str):
            raise TypeError("external_key 要是 str")
        if normalize_openalex_id(
            self.external_key
        ) != self.external_key or not self.external_key.startswith("W"):
            raise ValueError(f"external_key 要是正規化後的論文 ID（W…）：{self.external_key!r}")
        if not isinstance(self.content, bytes):
            raise TypeError(f"content 要是 bytes（現在是 {type(self.content).__name__}）")
        if not self.content:
            raise ValueError("content 不能是空的")
        _require_aware(self.fetched_at, "fetched_at")

    @cached_property
    def sha256(self) -> str:
        """未壓縮的 `content` 的 SHA-256（64 位小寫十六進位）。"""
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class ApiCall:
    """一次 HTTP 嘗試的紀錄。連線錯誤（沒有回應）時 `http_status` 是 None、`cost_usd` 是 0。

    `remaining_usd`／`limit_usd`：對方回報的今日剩餘額度與上限，看不懂就是 None。
    """

    source: RawSource
    operation: str
    http_status: int | None
    cost_usd: Decimal
    remaining_usd: Decimal | None
    limit_usd: Decimal | None
    called_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", RawSource(self.source))
        if not isinstance(self.operation, str) or not _OPERATION.fullmatch(self.operation):
            raise ValueError(f"operation 要是小寫識別字：{self.operation!r}")
        if self.http_status is not None and (
            not isinstance(self.http_status, int)
            or isinstance(self.http_status, bool)
            or not 100 <= self.http_status <= 599
        ):
            raise ValueError(f"http_status 要是 None 或 100–599：{self.http_status!r}")
        _check_usd(self.cost_usd, "cost_usd", optional=False)
        _check_usd(self.remaining_usd, "remaining_usd", optional=True)
        _check_usd(self.limit_usd, "limit_usd", optional=True)
        _require_aware(self.called_at, "called_at")


@dataclass(frozen=True)
class SourcePage:
    """一頁的結果：這一頁的原始紀錄、下一頁的 cursor（None＝這本抓完）、這一頁每次 HTTP 嘗試。"""

    records: tuple[RawRecord, ...]
    next_cursor: str | None
    calls: tuple[ApiCall, ...] = field(default=())

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "calls", tuple(self.calls))
        if not all(isinstance(record, RawRecord) for record in self.records):
            raise TypeError("records 只能放 RawRecord")
        if not all(isinstance(call, ApiCall) for call in self.calls):
            raise TypeError("calls 只能放 ApiCall")
        if self.next_cursor is not None and (
            not isinstance(self.next_cursor, str) or not self.next_cursor
        ):
            raise ValueError("next_cursor 要是 None 或非空字串")


class PaperSourceError(Exception):
    """抓取失敗。`calls` 是失敗前每次 HTTP 嘗試的紀錄（呼叫端照樣寫進 `api_call`）。

    訊息只有固定的原因（HTTP 狀態碼、錯誤類別），不含網址與 key。
    """

    def __init__(self, reason: str, *, calls: Sequence[ApiCall] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.calls = tuple(calls)


class QuotaReachedError(PaperSourceError):
    """重試途中對方回報今天的額度已用到停下的比例（或剩 0）：不再重試。

    不算抓取失敗：呼叫端把 `calls` 寫進 `api_call` 後照「額度停下」處理（ADR-0021）。
    """


@dataclass(frozen=True)
class ApiUsage:
    """某個時間點之後的 API 用量。

    - `local_cost_usd`：本地 `api_call.cost_usd` 的總和（只看得到這個程式的呼叫）。
    - `reported_used_usd`：最新一筆對方回報的「上限 − 剩餘」，加上那一筆之後本地記的
      `cost_usd`（之後的呼叫可能沒帶額度標頭）；也算到同一把 key 的其他用途。沒有回報時是 None。
    """

    local_cost_usd: Decimal
    reported_used_usd: Decimal | None

    @property
    def used_usd(self) -> Decimal:
        """兩者取較大的（ADR-0021）。"""
        if self.reported_used_usd is None:
            return self.local_cost_usd
        return max(self.local_cost_usd, self.reported_used_usd)


def compress_raw(content: bytes) -> bytes:
    """存進物件儲存的格式：gzip，`mtime=0`，所以同樣的內容一定壓出同樣的位元組。"""
    return gzip.compress(content, mtime=0)


def decompress_raw(data: bytes) -> bytes:
    """`compress_raw` 的反向。"""
    return gzip.decompress(data)
