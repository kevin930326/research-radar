"""每月預算：熔斷線、各功能預算、「只檢索」（NFR-3、ADR-0009、ADR-0031）。

純函式與資料型別；設定來自 `config/budget.yaml`（只經 `radar/config.py` 讀），帳本加總由
`repo/llm_ledger.py` 讀、`services/budget.py` 組起來。

- 金額一律 `Decimal`；設定檔金額寫成字串（重用 `domain/pricing.py` 的驗證，不收 float）。
- 「本月」是台北時間的月份：[當月 1 日 00:00, 下月 1 日 00:00)。時鐘由呼叫端注入；不帶時區的
  時間丟 `ValueError`（換算月份會猜錯時區）。
- 到達即擋：本月合計 ≥ 熔斷線 → 全部工作不准；某工作本月花費 ≥ 它的功能預算 → 只有那個工作不准；
  功能預算 `None` 表示沒有個別上限（只受熔斷線管）。
- 「只檢索」＝`qa_answer` 不准（熔斷或問答自己的預算用完）。它是由帳本算出來的，不存成開關；
  下個月一到自然恢復。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Final, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator, model_validator

from radar.domain.ai_outputs import LlmTask
from radar.domain.pricing import ConfigDecimal, _decimal_from_string
from radar.domain.text_table import render_table

# 預算的月份用台北時間（ADR-0031）。
BUDGET_TIMEZONE: Final = ZoneInfo("Asia/Taipei")
# `radar costs` 印的精度：4 位小數（四捨五入）。
DISPLAY_QUANTUM: Final = Decimal("0.0001")
_ZERO: Final = Decimal(0)


def _optional_decimal(value: object) -> Decimal | None:
    """功能預算：`null` 表示沒有個別上限；其他和金額一樣只收字串。"""
    if value is None:
        return None
    return _decimal_from_string(value)


type OptionalConfigDecimal = Annotated[Decimal | None, BeforeValidator(_optional_decimal)]


# --- 月份 ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class MonthWindow:
    """一個台北時間的月份：[`start`, `end`)，兩個都帶台北時區。"""

    start: datetime
    end: datetime

    @property
    def label(self) -> str:
        """`2026-11` 這種寫法。"""
        return f"{self.start.year:04d}-{self.start.month:02d}"


def month_window(now: datetime) -> MonthWindow:
    """`now` 換成台北時間後所在的月份。

    Raises:
        ValueError: `now` 沒有時區（naive datetime）。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 要帶時區（不能是 naive datetime）")
    local = now.astimezone(BUDGET_TIMEZONE)
    start = datetime(local.year, local.month, 1, tzinfo=BUDGET_TIMEZONE)
    if local.month == 12:
        end = datetime(local.year + 1, 1, 1, tzinfo=BUDGET_TIMEZONE)
    else:
        end = datetime(local.year, local.month + 1, 1, tzinfo=BUDGET_TIMEZONE)
    return MonthWindow(start=start, end=end)


# --- 設定 ---------------------------------------------------------------------------------


class BudgetConfig(BaseModel):
    """`config/budget.yaml`。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_limit_usd: ConfigDecimal
    monthly_breaker_usd: ConfigDecimal
    task_budgets_usd: Mapping[LlmTask, OptionalConfigDecimal]

    @field_validator("task_budgets_usd")
    @classmethod
    def _all_tasks(
        cls, value: Mapping[LlmTask, Decimal | None]
    ) -> Mapping[LlmTask, Decimal | None]:
        missing = [task.value for task in LlmTask if task not in value]
        if missing:
            raise ValueError("要列出全部六個工作（沒有個別上限寫 null），缺：" + "、".join(missing))
        return value

    @model_validator(mode="after")
    def _breaker_below_workspace(self) -> Self:
        if self.monthly_breaker_usd >= self.workspace_limit_usd:
            raise ValueError("monthly_breaker_usd 要低於 workspace_limit_usd")
        return self

    def task_budget(self, task: LlmTask) -> Decimal | None:
        """這個工作的功能預算；None 表示沒有個別上限。"""
        return self.task_budgets_usd[LlmTask(task)]


# --- 判定 ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetStatus:
    """某個月份的預算狀態（由帳本加總算出，不存）。

    - `by_task`：六個工作都在，沒花的是 0。
    - `total_usd`：`by_task` 的合計。
    """

    window: MonthWindow
    config: BudgetConfig
    by_task: Mapping[LlmTask, Decimal]
    total_usd: Decimal

    @property
    def breaker_usd(self) -> Decimal:
        return self.config.monthly_breaker_usd

    @property
    def breaker_tripped(self) -> bool:
        """本月合計到達熔斷線（到達即擋）。"""
        return self.total_usd >= self.config.monthly_breaker_usd

    def task_exhausted(self, task: LlmTask) -> bool:
        """這個工作本月花費到達它的功能預算；沒有個別上限的永遠是 False。"""
        budget = self.config.task_budget(task)
        return budget is not None and self.by_task[LlmTask(task)] >= budget

    def allows(self, task: LlmTask) -> bool:
        """這個工作現在可不可以呼叫模型。"""
        return not self.breaker_tripped and not self.task_exhausted(task)

    @property
    def retrieval_only(self) -> bool:
        """問答（`qa_answer`）不准：熔斷或問答自己的預算用完（Epic 5 讀這個）。"""
        return not self.allows(LlmTask.QA_ANSWER)

    @property
    def remaining_usd(self) -> Decimal:
        """離熔斷線還有多少；已到達是 0。"""
        return max(self.config.monthly_breaker_usd - self.total_usd, _ZERO)


def build_budget_status(
    config: BudgetConfig, window: MonthWindow, costs: Mapping[LlmTask, Decimal]
) -> BudgetStatus:
    """把帳本依工作的加總組成預算狀態；沒出現的工作算 0。

    Raises:
        ValueError: 有不認得的工作代碼，或金額不是 `Decimal`、不是有限數字、是負的。
    """
    if set(costs) - set(LlmTask):
        raise ValueError("費用裡有不認得的工作代碼")
    by_task: dict[LlmTask, Decimal] = {}
    for task in LlmTask:
        amount = costs.get(task, _ZERO)
        if type(amount) is not Decimal or not amount.is_finite() or amount < 0:
            raise ValueError(f"{task.value} 的費用要是有限、不小於 0 的 Decimal")
        by_task[task] = amount
    return BudgetStatus(
        window=window, config=config, by_task=by_task, total_usd=sum(by_task.values(), _ZERO)
    )


# --- 輸出 ---------------------------------------------------------------------------------


def format_usd(amount: Decimal) -> str:
    """金額固定 4 位小數（四捨五入，half-up），例如 `1.2346`。"""
    return f"{amount.quantize(DISPLAY_QUANTUM, rounding=ROUND_HALF_UP):f}"


def render_cost_report(status: BudgetStatus) -> list[str]:
    """`radar costs` 要印的每一行（單位美元，4 位小數）。"""
    lines = [f"{status.window.label} 費用（台北時間，美元）", ""]
    body: list[list[str]] = []
    for task in LlmTask:
        budget = status.config.task_budget(task)
        body.append(
            [
                task.value,
                format_usd(status.by_task[task]),
                "未設" if budget is None else format_usd(budget),
                "是" if status.allows(task) else "否",
            ]
        )
    body.append(["合計", format_usd(status.total_usd), "", ""])
    lines += render_table(["工作", "費用", "功能預算", "可呼叫"], body, align="lrrl")
    lines += [
        "",
        f"熔斷線：{format_usd(status.breaker_usd)}",
        f"離熔斷線：{format_usd(status.remaining_usd)}",
        f"只檢索：{'是' if status.retrieval_only else '否'}",
    ]
    return lines
