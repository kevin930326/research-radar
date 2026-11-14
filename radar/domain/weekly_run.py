"""每週更新的狀態與每本期刊的進度（AD-14、ADR-0004 決定第 2 點、ADR-0025）。

- `weekly_run`：一次每週更新一列；同時只能有一筆 `fetching`（部分唯一索引）。
- `weekly_run_journal`：這一次每週更新裡每本期刊抓到哪一頁、過濾前後各幾篇（續跑用）。

Story 1.8 只用到 `fetching`；套用、發佈快照（`published`）在 1.9，`failed` 在 1.11。
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType


class WeeklyRunStatus(StrEnum):
    """`weekly_run.status` 的五種值（ADR-0004 決定第 2 點）。"""

    FETCHING = "fetching"  # 抓取中
    PUBLISHED = "published"  # 快照已發佈
    AI_DONE = "ai_done"  # 這一週的 AI 處理完成，或已等滿 24 小時
    EXPORTED = "exported"  # 示範站已匯出
    FAILED = "failed"  # 抓取失敗


@dataclass(frozen=True)
class WeeklyRun:
    """一次每週更新。`fetch_finished_at` 是 None 代表還沒全部抓完。"""

    id: uuid.UUID
    status: WeeklyRunStatus
    fetch_finished_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", WeeklyRunStatus(self.status))


def _check_count(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} 要是 ≥0 的整數（現在是 {value!r}）")


def _frozen_years(value: Mapping[int, int]) -> Mapping[int, int]:
    years: dict[int, int] = {}
    for year, count in value.items():
        if not isinstance(year, int) or isinstance(year, bool):
            raise ValueError(f"fetched_by_year 的鍵要是整數年份（現在是 {year!r}）")
        _check_count(count, f"fetched_by_year[{year}]")
        years[year] = count
    return MappingProxyType(dict(sorted(years.items())))


@dataclass(frozen=True)
class PageCounts:
    """一頁（或多頁加總）的篇數。

    - `fetched`：過濾前的篇數（這一頁抓到幾篇）；`fetched_by_year`：其中依解析出的出版年分的
      篇數（解析失敗、沒有出版年的不分年）。
    - `staged`：放進暫存表的篇數（含被排除規則標記的）；`excluded`：其中有 `excluded_reason` 的。
    - `out_of_scope`：收錄過濾擋下的；`unparseable`：解析失敗或 OpenAlex ID 對不上外部鍵的。
    """

    fetched: int = 0
    fetched_by_year: Mapping[int, int] = field(default_factory=lambda: MappingProxyType({}))
    staged: int = 0
    excluded: int = 0
    out_of_scope: int = 0
    unparseable: int = 0

    def __post_init__(self) -> None:
        for name in ("fetched", "staged", "excluded", "out_of_scope", "unparseable"):
            _check_count(getattr(self, name), name)
        object.__setattr__(self, "fetched_by_year", _frozen_years(self.fetched_by_year))

    def plus(self, other: PageCounts) -> PageCounts:
        """兩份篇數相加（每年的篇數逐年相加）。"""
        years = dict(self.fetched_by_year)
        for year, count in other.fetched_by_year.items():
            years[year] = years.get(year, 0) + count
        return PageCounts(
            fetched=self.fetched + other.fetched,
            fetched_by_year=years,
            staged=self.staged + other.staged,
            excluded=self.excluded + other.excluded,
            out_of_scope=self.out_of_scope + other.out_of_scope,
            unparseable=self.unparseable + other.unparseable,
        )


@dataclass(frozen=True)
class JournalProgress:
    """一本期刊在這一次每週更新裡的進度。

    - `next_cursor`：下次要送的 cursor；None 且 `finished` 為假＝從第一頁開始。
    - `finished`：這本已經抓完（最後一頁也暫存好了）。
    - `pages`：已經暫存好的頁數；其餘篇數見 `PageCounts`。
    """

    journal_key: str
    next_cursor: str | None
    finished: bool
    pages: int
    fetched: int
    fetched_by_year: Mapping[int, int]
    staged: int
    excluded: int
    out_of_scope: int
    unparseable: int

    def __post_init__(self) -> None:
        if not isinstance(self.journal_key, str) or not self.journal_key:
            raise ValueError("journal_key 要是非空字串")
        if self.next_cursor is not None and (
            not isinstance(self.next_cursor, str) or not self.next_cursor
        ):
            raise ValueError("next_cursor 要是 None 或非空字串")
        for name in ("pages", "fetched", "staged", "excluded", "out_of_scope", "unparseable"):
            _check_count(getattr(self, name), name)
        object.__setattr__(self, "fetched_by_year", _frozen_years(self.fetched_by_year))
