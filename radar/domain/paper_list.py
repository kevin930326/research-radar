"""找論文清單的層間資料、條件的解析與驗證、下一頁的位置（Story 3.13、AD-19、ADR-0011「點數字列出
論文」）。

- 條件（`PaperListFilters`）：不同欄位之間是「且」；期刊、年份給多個值是「或」；主題、方法給多個值
  是「且」（同時有這幾個）。`has_abstract` 是 None＝不限。`this_week`＝`first_snapshot_id` 是目前
  已發佈的快照（不用現在的時間算）。
- 範圍（`PaperListScope`）：`tracked` 是算數的論文（view `counted_paper`）；`lab` 是全實驗室看得到的
  論文（含不在 `counted_paper` 的）；`personal` 是自己的文獻庫。三種都不列被排除的文章。
- `parse_paper_list_filters` 把 API 收到的值換成條件：期刊代號換成 ISSN-L；不認得的期刊、主題、方法
  代碼，年份小於追蹤起始年或大於 9999，任一個多值參數超過 `MAX_FILTER_VALUES` 個值（去重之前數），
  都丟 `InvalidPaperListQueryError`。重複的值去重、排序，不算錯。
- 清單照 `publication_year DESC NULLS LAST, id DESC` 排，keyset 分頁：一頁 `PAPER_LIST_PAGE_SIZE`
  篇，下一頁的位置是這一頁最後一篇的（出版年, id），編成不透明的字串（寫法同 `domain/library`）。
"""

import base64
import binascii
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from radar.domain.domain_config import JournalsConfig
from radar.domain.paper_rows import PaperRowFacts
from radar.domain.snapshot import Snapshot, SnapshotStatus
from radar.domain.taxonomy import UNCLASSIFIED, Taxonomy, TaxonomyField

# 找論文清單一頁幾篇（前端的 `client-config` 也給這個值）。
PAPER_LIST_PAGE_SIZE: Final = 20
# 一個多值參數（期刊、年份、主題、方法）最多幾個值。
MAX_FILTER_VALUES: Final = 50
# 年份的上限（追蹤範圍沒有上限，只擋寫錯的值）。
MAX_YEAR: Final = 9999

# 下一頁位置字串的長度上限（正常的長度約 55 個字元；太長的直接當格式錯）。
_CURSOR_MAX_LENGTH: Final = 120
_CURSOR_SEPARATOR: Final = ","
# 出版年存成 PostgreSQL 的 integer：位置裡的年份超出這個範圍就不是我們產生的。
_INT4_MIN: Final = -(2**31)
_INT4_MAX: Final = 2**31 - 1


class PaperListScope(StrEnum):
    """清單從哪一群論文找。"""

    TRACKED = "tracked"  # 算數的論文（`counted_paper`）
    LAB = "lab"  # 全實驗室看得到的論文
    PERSONAL = "personal"  # 自己的文獻庫


class InvalidPaperListQueryError(ValueError):
    """清單的條件不合法（不認得的代碼、年份超出範圍、值太多）。訊息不含使用者送來的值。"""


@dataclass(frozen=True, slots=True)
class PaperListQuery:
    """API 收到的條件，還沒驗證（期刊是設定檔的代號）。"""

    journals: tuple[str, ...] = ()
    years: tuple[int, ...] = ()
    topics: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    has_abstract: bool | None = None
    this_week: bool = False
    scope: PaperListScope = PaperListScope.TRACKED


@dataclass(frozen=True, slots=True)
class PaperListFilters:
    """驗證過的條件：ISSN-L、年份、主題、方法都是排序、去重的 tuple（空的＝不限）。

    Raises:
        ValueError: 多值欄位沒有排序去重，或型別不對。
    """

    journal_issn_ls: tuple[str, ...] = ()
    years: tuple[int, ...] = ()
    topics: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    has_abstract: bool | None = None
    this_week: bool = False
    scope: PaperListScope = PaperListScope.TRACKED

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", PaperListScope(self.scope))
        for name in ("journal_issn_ls", "topics", "methods"):
            _check_sorted_unique(getattr(self, name), str, name)
        _check_sorted_unique(self.years, int, "years")
        if self.has_abstract is not None and type(self.has_abstract) is not bool:
            raise ValueError("has_abstract 要是 True、False 或 None")
        if type(self.this_week) is not bool:
            raise ValueError("this_week 要是 True 或 False")


def _check_sorted_unique(values: object, kind: type, name: str) -> None:
    if not isinstance(values, tuple) or not all(type(value) is kind for value in values):
        raise ValueError(f"{name} 要是 {kind.__name__} 的 tuple")
    if list(values) != sorted(set(values)):
        raise ValueError(f"{name} 要排序、不重複")


def _bounded(values: Sequence[object], name: str) -> None:
    if len(values) > MAX_FILTER_VALUES:
        raise InvalidPaperListQueryError(f"{name} 最多 {MAX_FILTER_VALUES} 個值")


def parse_paper_list_filters(
    query: PaperListQuery, journals: JournalsConfig, taxonomy: Taxonomy
) -> PaperListFilters:
    """API 收到的條件 → 驗證過的條件（期刊代號換成 ISSN-L，多值欄位去重、排序）。

    - 期刊：設定檔（`journals`）裡的代號。
    - 年份：`journals.start_year` 到 `MAX_YEAR`（含）。
    - 主題：分類表的主題 key，或 `unclassified`；方法：分類表的方法 key（方法沒有 `unclassified`）。

    Raises:
        InvalidPaperListQueryError: 任一個值不合法，或任一個多值欄位超過 `MAX_FILTER_VALUES` 個值
            （重複的也算）。
    """
    _bounded(query.journals, "journal")
    _bounded(query.years, "year")
    _bounded(query.topics, "topic")
    _bounded(query.methods, "method")
    issn_of = {journal.key: journal.issn_l for journal in journals.journals}
    if any(key not in issn_of for key in query.journals):
        raise InvalidPaperListQueryError("有不認得的期刊代號")
    if any(
        type(year) is not int or not journals.start_year <= year <= MAX_YEAR for year in query.years
    ):
        raise InvalidPaperListQueryError(f"年份要在 {journals.start_year} 到 {MAX_YEAR} 之間")
    topics = {*taxonomy.keys(TaxonomyField.TOPIC), UNCLASSIFIED}
    if any(key not in topics for key in query.topics):
        raise InvalidPaperListQueryError("有不認得的主題代碼")
    methods = set(taxonomy.keys(TaxonomyField.METHOD))
    if any(key not in methods for key in query.methods):
        raise InvalidPaperListQueryError("有不認得的方法代碼")
    return PaperListFilters(
        journal_issn_ls=tuple(sorted({issn_of[key] for key in query.journals})),
        years=tuple(sorted(set(query.years))),
        topics=tuple(sorted(set(query.topics))),
        methods=tuple(sorted(set(query.methods))),
        has_abstract=query.has_abstract,
        this_week=query.this_week,
        scope=PaperListScope(query.scope),
    )


@dataclass(frozen=True, slots=True)
class PaperListCursor:
    """清單的下一頁從哪裡開始：上一頁最後一篇的（出版年, id）；出版年可以是 None（排在最後）。

    Raises:
        ValueError: 年份不是整數或超出 PostgreSQL integer 的範圍、`paper_id` 不是 UUID。
    """

    year: int | None
    paper_id: uuid.UUID

    def __post_init__(self) -> None:
        if self.year is not None and (
            type(self.year) is not int or not _INT4_MIN <= self.year <= _INT4_MAX
        ):
            raise ValueError("year 要是 integer 範圍內的整數或 None")
        if not isinstance(self.paper_id, uuid.UUID):
            raise ValueError("paper_id 要是 UUID")


def encode_paper_list_cursor(cursor: PaperListCursor) -> str:
    """位置 → 不透明的字串（URL 安全的 base64、沒有 `=`）。出版年是 None 時寫成空字串。"""
    year = "" if cursor.year is None else str(cursor.year)
    plain = f"{year}{_CURSOR_SEPARATOR}{cursor.paper_id}"
    return base64.urlsafe_b64encode(plain.encode("ascii")).decode("ascii").rstrip("=")


def decode_paper_list_cursor(value: str) -> PaperListCursor:
    """`encode_paper_list_cursor` 的反向；只收它產生的寫法（重新編碼要一模一樣）。

    Raises:
        ValueError: 不是 `encode_paper_list_cursor` 產生的字串（太長、不是 base64、年份或 ID
            不對）。
    """
    if not isinstance(value, str) or not 0 < len(value) <= _CURSOR_MAX_LENGTH:
        raise ValueError("下一頁的位置格式不對")
    try:
        padded = value + "=" * (-len(value) % 4)
        plain = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
        year_text, paper_id_text = plain.split(_CURSOR_SEPARATOR)
        cursor = PaperListCursor(
            year=None if year_text == "" else int(year_text), paper_id=uuid.UUID(paper_id_text)
        )
        same = encode_paper_list_cursor(cursor) == value
    except (UnicodeError, binascii.Error, ValueError) as exc:
        raise ValueError("下一頁的位置格式不對") from exc
    if not same:
        raise ValueError("下一頁的位置格式不對")
    return cursor


@dataclass(frozen=True, slots=True)
class PaperListPage:
    """清單的一頁：這一頁的論文列（照排序）、下一頁的位置（沒有下一頁是 None）、符合條件的總篇數、
    目前已發佈的快照（一個都還沒有是 None）。

    總數和這一頁是同一個交易裡的兩句查詢（中間可能有每週更新發佈），所以不檢查兩者對不對得上。

    Raises:
        ValueError: 篇數超過一頁、總數不是非負整數、快照還沒發佈。
    """

    rows: tuple[PaperRowFacts, ...]
    next_cursor: str | None
    total: int
    snapshot: Snapshot | None

    def __post_init__(self) -> None:
        if len(self.rows) > PAPER_LIST_PAGE_SIZE:
            raise ValueError(f"一頁最多 {PAPER_LIST_PAGE_SIZE} 篇")
        if type(self.total) is not int or self.total < 0:
            raise ValueError("total 要是非負整數")
        if self.snapshot is not None and (
            self.snapshot.status is not SnapshotStatus.PUBLISHED or self.snapshot.as_of is None
        ):
            raise ValueError("快照要是已發佈的")
