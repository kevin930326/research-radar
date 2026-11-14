"""每月預算的讀取與閘門的預算檢查（NFR-3、ADR-0009、ADR-0031）。只讀，不改任何資料。

- `get_budget_status`：一個短交易讀本月（台北時間）帳本依工作的加總，組成 `BudgetStatus`。
  `radar costs` 與 `BudgetGuard` 共用；之後問答（Epic 5）也用它查「只檢索」。
- `BudgetGuard`：交給 `run_llm_job(budget_allows=...)` 的預算檢查。正式程式呼叫閘門時一定傳它
  （`tests/arch/test_budget_guard_wiring.py` 守），測試可以傳 lambda。

預算檢查是「呼叫前看帳本」，帳在呼叫後才記，同時在途的呼叫彼此看不到（熔斷線與每個功能預算都一樣）：
超支上限＝同時在途的呼叫數（worker 加上所有呼叫閘門的 CLI 等行程）× 單次最大費用（約 0.042 美元），
例如 10 個在途約 0.42 美元（ADR-0031）。Anthropic workspace 的 70 美元上限是第二道。
"""

from collections.abc import Callable
from datetime import UTC, datetime

import structlog

from radar.domain.ai_outputs import LlmTask
from radar.domain.budget import BudgetConfig, BudgetStatus, build_budget_status, month_window
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.llm_ledger import list_llm_costs_by_task

log = structlog.get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def get_budget_status(
    viewer: SystemViewer, database: Database, config: BudgetConfig, *, now: datetime
) -> BudgetStatus:
    """`now` 所在的台北月份的預算狀態（一個短交易讀帳本）。

    Raises:
        ValueError: `now` 沒有時區。
        TypeError: `viewer` 不是 `SystemViewer`（repo 的讀取函式在碰連線之前丟）。
    """
    window = month_window(now)
    with database.transaction() as connection:
        costs = list_llm_costs_by_task(viewer, connection, window.start, window.end)
    return build_budget_status(config, window, costs)


class BudgetGuard:
    """閘門第 2 步的預算檢查：`guard(task)` 回這個工作現在可不可以呼叫模型。

    每次呼叫都重讀帳本（不快取），所以下個月（台北時間）一到自然恢復。不准時記一筆警告日誌
    （工作代碼、本月合計、是熔斷還是功能預算用完），閘門回 `DEFERRED_BUDGET`。
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
            breaker_tripped=status.breaker_tripped,
            task_exhausted=status.task_exhausted(task),
        )
        return False
