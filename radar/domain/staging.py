"""暫存表 `ingest_staging` 的領域型別（ADR-0014「寫入分兩段」、ADR-0022）。

解析好的結果先放暫存表、不動論文資料；最後一個交易再從暫存表讀出來套用。
同一次執行的暫存列共用一個 `batch_id`。
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from radar.domain.papers import ParsedPaper


class StagingKind(StrEnum):
    """哪一種工作放的暫存列（`ingest_staging.kind`）。"""

    REPARSE = "reparse"
    WEEKLY_UPDATE = "weekly_update"


@dataclass(frozen=True)
class StagingItem:
    """要放進暫存表的一篇：外部鍵、解析結果、排除規則判定的代碼（沒有符合是 None）。"""

    external_key: str
    paper: ParsedPaper
    excluded_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.paper, ParsedPaper):
            raise TypeError("paper 要是 ParsedPaper")
        if not isinstance(self.external_key, str) or not self.external_key:
            raise ValueError("external_key 要是非空字串")
        if self.excluded_reason is not None and (
            not isinstance(self.excluded_reason, str) or not self.excluded_reason
        ):
            raise ValueError("excluded_reason 要是 None 或非空字串")


@dataclass(frozen=True)
class StagedPaper:
    """從暫存表讀出來的一篇；`id` 是暫存列的主鍵（依它做 keyset 分頁）。"""

    id: uuid.UUID
    external_key: str
    paper: ParsedPaper
    excluded_reason: str | None
