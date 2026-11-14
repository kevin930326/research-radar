"""快照：某一次每週更新完成後「資料庫收進了哪些論文」（AD-19、ADR-0011「快照」、ADR-0026）。

- 只由 `services/weekly_update.py` 建立與發佈；第一次匯入就是它的第一次執行（快照 1）。
- `number` 從 1 遞增；`as_of` 是發佈的時間（`clock_timestamp()`），沒發佈是 None。
- `created_at` 是套用交易開始的時間（`now()`），所以 `as_of − created_at` 就是套用花的時間。
- 資料截止日＝`as_of` 換成台北時間的日期（ADR-0011「資料截止日」）：用 UTC 的日期會在台北時間
  7 月 1 日 00:00 到 07:59 之間判斷錯。
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo

# 資料截止日用的時區（ADR-0011「資料截止日」）。
DATA_TIMEZONE: Final = ZoneInfo("Asia/Taipei")


class SnapshotStatus(StrEnum):
    """`snapshot.status` 的三種值（ADR-0011「快照」第 1 點）。"""

    BUILDING = "building"  # 建立中（套用交易還沒結束）
    PUBLISHED = "published"  # 已發佈
    FAILED = "failed"  # 失敗（1.11 才會寫）


@dataclass(frozen=True)
class Snapshot:
    """一個快照。"""

    id: uuid.UUID
    number: int
    weekly_run_id: uuid.UUID
    status: SnapshotStatus
    as_of: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", SnapshotStatus(self.status))
        if not isinstance(self.number, int) or isinstance(self.number, bool) or self.number < 1:
            raise ValueError(f"number 要是 ≥1 的整數（現在是 {self.number!r}）")

    @property
    def apply_duration(self) -> timedelta | None:
        """套用花的時間（`as_of − created_at`）；還沒發佈是 None。"""
        if self.status is not SnapshotStatus.PUBLISHED or self.as_of is None:
            return None
        return self.as_of - self.created_at


def data_cutoff_date(as_of: datetime) -> date:
    """資料截止日：`as_of` 換成台北時間的日期（ADR-0011「資料截止日」）。

    Raises:
        ValueError: `as_of` 沒有時區（naive datetime）。
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of 要帶時區（不能是 naive datetime）")
    return as_of.astimezone(DATA_TIMEZONE).date()
