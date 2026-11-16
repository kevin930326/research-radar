"""每月預算的讀取與閘門的預算檢查（NFR-3、ADR-0009、ADR-0031）。只讀，不改任何資料。

- `read_budget_status`：在呼叫端開好的交易裡，用**同一句 SQL** 讀本月（台北時間）帳本與尚未結束的
  Batch 預留（`repo/llm_batches.list_llm_spending_and_reservations_by_task`），組成
  `BudgetStatus`。「帳本＋預留」只在這裡算：互動的 `BudgetGuard`、Batch 的規劃與送出（在全域
  Batch 預算鎖裡呼叫它）都用它（ADR-0041）。
- `get_budget_status`：一個短交易包 `read_budget_status`。`radar costs`、`BudgetGuard` 與 AI 區塊
  共用；之後問答（Epic 5）也用它查「只檢索」。
- `BudgetGuard`：交給 `run_llm_job(budget_allows=...)` 的預算檢查。正式程式呼叫閘門時一定傳它
  （`tests/arch/test_budget_guard_wiring.py` 守），測試可以傳 lambda。

預算檢查是「呼叫前看帳本＋Batch 預留」，帳在呼叫後才記，同時在途的互動呼叫彼此看不到（熔斷線與每個
功能預算都一樣；Batch 規劃在預算鎖裡扣掉的預留則互動看得到，Batch 也看得到已記的互動帳）：
超支上限＝同時在途的呼叫數（worker 加上所有呼叫閘門的 CLI 等行程）× 單次最大費用（約 0.042 美元），
例如 10 個在途約 0.42 美元（ADR-0031）。這個上限只在帳都記得進去、沒有工作打開
`allow_over_input_limit` 時成立（ADR-0031 決定 6）。Anthropic workspace 的 70 美元上限是第二道。
"""

from collections.abc import Callable
from datetime import UTC, datetime

import structlog

from radar.domain.ai_outputs import LlmTask
from radar.domain.budget import BudgetConfig, BudgetStatus, build_budget_status, month_window
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database, DbConnection
from radar.repo.llm_batches import list_llm_spending_and_reservations_by_task

log = structlog.get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def read_budget_status(
    viewer: SystemViewer, connection: DbConnection, config: BudgetConfig, *, now: datetime
) -> BudgetStatus:
    """（呼叫端開好的交易裡）`now` 所在的台北月份的預算狀態：帳本＋尚未結束的 Batch 預留，同一句
    SQL 讀（同一個快照）。

    Raises:
        ValueError: `now` 沒有時區（還沒查資料庫）。
        TypeError: `viewer` 不是 `SystemViewer`（repo 的讀取函式在碰連線之前丟）。
    """
    window = month_window(now)
    spent, reserved = list_llm_spending_and_reservations_by_task(
        viewer, connection, window.start, window.end
    )
    return build_budget_status(config, window, spent, reserved)


def get_budget_status(
    viewer: SystemViewer, database: Database, config: BudgetConfig, *, now: datetime
) -> BudgetStatus:
    """`now` 所在的台北月份的預算狀態（一個短交易包 `read_budget_status`）。

    Raises:
        ValueError: `now` 沒有時區。
        TypeError: `viewer` 不是 `SystemViewer`（repo 的讀取函式在碰連線之前丟）。
    """
    month_window(now)  # 沒有時區時在開交易之前就丟
    with database.transaction() as connection:
        return read_budget_status(viewer, connection, config, now=now)


class BudgetGuard:
    """閘門第 2 步的預算檢查：`guard(task)` 回這個工作現在可不可以呼叫模型。

    每次呼叫都重讀帳本與 Batch 預留（不快取），所以下個月（台北時間）一到、或 Batch 結果匯入釋放
    預留後自然恢復。「帳本＋預留」到達熔斷線或功能預算就不准（ADR-0041：Batch 已排、結果還沒回來
    的錢先扣掉，互動不會把同一份剩餘再花一次）。不准時記一筆警告日誌（工作代碼、本月合計、預留、
    是熔斷還是功能預算用完），閘門回 `DEFERRED_BUDGET`。
    """

    def __init__(
        self,
        database: Database,
        config: BudgetConfig,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._database = database
        self._config = config
        self._clock = clock

    def __call__(self, task: LlmTask) -> bool:
        """這個工作現在可不可以呼叫模型。

        Raises:
            ValueError: 時鐘回的時間沒有時區。
        """
        task = LlmTask(task)
        status = get_budget_status(SystemViewer(), self._database, self._config, now=self._clock())
        if status.allows(task):
            return True
        log.warning(
            "預算不准，LLM 工作延後",
            task=task.value,
            month=status.window.label,
            total_usd=str(status.total_usd),
            reserved_usd=str(status.reserved_usd),
            breaker_tripped=status.breaker_tripped,
            task_exhausted=status.task_exhausted(task),
        )
        return False
