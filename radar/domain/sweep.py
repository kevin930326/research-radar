"""`sweep` 的登記清單：哪張狀態表停在哪個狀態太久要重新排入哪個工作（AD-14、ADR-0004 決定第 9 點）。

新增狀態表的 Story 必須在 `SWEEP_REGISTRY` 加一列，否則架構測試失敗
（資料庫裡有 `status` 欄的表，不是在清單裡，就是在例外清單裡寫明理由）。
重新排入時工作的參數是 `<表名>_id`，值是那一列的 `id`。

登記一張表時，後面的 Story 必須做到：
- `time_column` 是 NOT NULL（時間是 NULL 的列永遠不會被找到）。
- 表的 `id` 是 UUID（工作參數只收 UUID）。
- `stuck_after` 要比工作最長的執行時間加上所有重試的等待還長：排隊鎖只擋「排隊中」的同一件事，
  工作還在「執行中」時，那一列會被再排一次。
- 工作最後放棄（failed）時，要把那一列移出 `start_status`；否則每次 sweep 都會再排一次。
- 工作登記的參數剛好是 `id_parameter`（架構測試檢查）。
"""

import re
from dataclasses import dataclass
from datetime import timedelta

from radar.domain.jobs import RUN_WEEKLY_UPDATE, JobSpec

# 表名與欄名直接組進 SQL，所以只收小寫識別字（PostgreSQL 的名稱上限 63 個字元）；
# 開頭要是英文字母，`<表名>_id` 才會是合法的工作參數名稱。
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,62}")

# 一張表一次最多重新排入幾列；剩下的下一輪（15 分鐘後）再排。
SWEEP_BATCH_LIMIT = 500


@dataclass(frozen=True)
class SweepEntry:
    """一張狀態表的掃描規則：`status_column` 是 `start_status`、而且 `time_column` 早於
    「現在減 `stuck_after`」的列，重新排入 `job`。

    登記時要符合模組說明列的條件（`time_column` NOT NULL、`id` 是 UUID、`stuck_after`
    夠長、工作放棄時把列移出 `start_status`）。
    """

    table: str
    status_column: str
    start_status: str
    time_column: str
    job: JobSpec
    stuck_after: timedelta

    def __post_init__(self) -> None:
        for label, name in (
            ("表名", self.table),
            ("狀態欄", self.status_column),
            ("時間欄", self.time_column),
        ):
            if not _IDENTIFIER.fullmatch(name):
                raise ValueError(f"{label}只能是小寫識別字：{name!r}")
        if not self.start_status:
            raise ValueError("起始狀態不能是空字串")
        if self.stuck_after <= timedelta(0):
            raise ValueError("多久算卡住要大於 0")

    @property
    def id_parameter(self) -> str:
        """重新排入時工作的參數名稱。"""
        return f"{self.table}_id"


# `upload_item` 等之後的狀態表由各自的 Story 加進來。
SWEEP_REGISTRY: tuple[SweepEntry, ...] = (
    # 每週更新（Story 1.8、ADR-0025）：`updated_at` 每抓完一頁、額度停下、失敗時都更新，活著的
    # 工作通常不會被當成卡住（一頁平常幾秒）。最壞時（物件存取每次都照 `Retry-After` 等到上限、
    # 重試用完）一頁可能超過 2 小時，sweep 會多排一筆；但佇列鎖讓它等前一筆做完、接著看到已保存
    # 的進度，不會兩個同時抓（ADR-0025 理由 4）。
    # 失敗會累計（跨工作重試、sweep 補排、下一週 cron 接著同一筆）；到上限
    # （`weekly_update_max_failures`）那一筆改成 `failed`、移出 `fetching`，sweep 不再補排
    # （Story 1.11、ADR-0028）。額度停下不算失敗，仍是 `fetching`，每 2 小時補排一次接著抓。
    SweepEntry(
        "weekly_run", "status", "fetching", "updated_at", RUN_WEEKLY_UPDATE, timedelta(hours=2)
    ),
)
