"""Batch 送出的 domain 形狀（AD-8、AD-14、ADR-0004 決定第 4 點、ADR-0031 決定 15、ADR-0041、
Story 2.9）。

- `batch_submission` 的狀態（`BatchSubmissionStatus`）：`queued` → `submitting` → `submitted` →
  `ended` → `imported`；`failed`；`needs_attention`（需要人工處理）。2.9 走到 `submitted`，
  `ended`／`imported` 由結果匯入（2.11）推進。狀態只經 `services/llm_batches.py` 改。
- 設定 `config/llm_batches.yaml`（`LlmBatchConfig`，只經 `radar/config.py` 讀）：每批最多幾個請求、
  台北每月 1 日幾點以前整次規劃延後（`in_month_start_hold`）、`radar batches wait` 多久查一次。
- 結果匯入（Story 2.11、ADR-0043）：供應商的保留與處理期限（`PROVIDER_RESULTS_RETENTION`、
  `PROVIDER_PROCESSING_WINDOW`）、每週更新進 `ai_done` 的時限（`AI_DONE_AFTER`）、一項的匯入結果
  （`BatchItemOutcome`）、一次 poll 的結果與要印的字（`LlmBatchPollResult`、`render_poll_*`）、
  `radar batches create` 的預估（`LlmBatchEstimate`）與 `resolve` 的結果。
- `SUBMITTING_STALE_AFTER`：停在 `submitting` 而且沒有供應商 batch ID 超過這麼久，就判定送出的
  工作已經不在了（送出不重試，最久約一次 `RADAR_ANTHROPIC_TIMEOUT`），標 `needs_attention`、
  不重送；sweep 的 `submitting` 那一列用同一個常數。
- `batch_never_fits`：送出前最後確認超過預算時，分「再等可能放得下」（延後）與「再等也放不下」
  （取消、釋放預留）。
- 結果型別（`LlmBatchPlanResult`、`LlmBatchSubmitResult`）的 `stop`／`outcome` 值會寫進日誌。
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from radar.domain.ai_outputs import InputKind, LlmTask, PaperLlmSource, RunKey, RunStatus
from radar.domain.budget import BUDGET_TIMEZONE, BudgetConfig, BudgetStatus, format_usd
from radar.domain.errors import ErrorCode
from radar.domain.llm import BatchProcessingStatus, LlmBatchStatus
from radar.domain.processing import ProcessingFacts
from radar.domain.text_table import render_table

# 停在 `submitting` 沒有 batch ID 多久算送出的工作已經不在（ADR-0041）。
SUBMITTING_STALE_AFTER: Final = timedelta(minutes=30)
# 停在 `queued` 多久由 sweep 補排送出（429 退回 `queued`、排入失敗、`ConfigError` 留著的列）。
QUEUED_STUCK_AFTER: Final = timedelta(hours=1)
# Anthropic 一批最多 100,000 個請求（docs/runs/epic-2b/api-reference.md 第一節）。
PROVIDER_MAX_REQUESTS: Final = 100_000
# `max_requests_per_batch` 的上限（比官方的 100,000 小很多）：一批在一個交易裡建，一個交易最多
# 嘗試 2 × 上限筆、每筆一把 advisory lock；PostgreSQL 共用鎖表預設約 6,400 格（ADR-0041）。
MAX_REQUESTS_PER_BATCH: Final = 1_000
# 送出時等全域 Batch 預算鎖最多幾毫秒（`lock_timeout`）；等不到就延後（`batch_budget_lock_busy`），
# sweep 之後補排。比正式的 `statement_timeout`（`database_connect_timeout`，預設 3 秒）短，所以
# 通常是 `lock_timeout`（SQLSTATE 55P03）先到；`statement_timeout`（57014）先到時也照同樣處理。
SUBMIT_BUDGET_LOCK_TIMEOUT_MS: Final = 2_000
# 供應商 batch ID 的格式（和資料庫的 CHECK 一樣）。
PROVIDER_BATCH_ID_PATTERN: Final = r"^[A-Za-z0-9_-]{1,128}$"
# Anthropic 的結果從建立批次起保留 29 天，之後批次還查得到、結果不能下載；24 小時內沒處理完的請求
# 過期（`expires_at`＝`created_at`＋24 小時）。來源：https://platform.claude.com/docs/en/
# build-with-claude/batch-processing（2026-11-15 讀，docs/runs/epic-2b/api-reference.md 第一節）。
PROVIDER_RESULTS_RETENTION: Final = timedelta(days=29)
PROVIDER_PROCESSING_WINDOW: Final = timedelta(hours=24)
# 每週更新發佈後滿這麼久，就算這一週的 Batch 還沒都結束也進 `ai_done`（AD-14「這一週的 Batch 都結束
# 了，或發佈後已滿 24 小時」、ADR-0004 決定第 10 點）。
AI_DONE_AFTER: Final = timedelta(hours=24)


class BatchSubmissionStatus(StrEnum):
    """`batch_submission.status`（ADR-0004 決定第 2 點）。"""

    QUEUED = "queued"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    ENDED = "ended"
    IMPORTED = "imported"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"


class LlmBatchConfig(BaseModel):
    """`config/llm_batches.yaml`。

    - `weekly_planning_enabled`：每週更新發佈後要不要自動規劃 Batch。全庫第一次 AI 要由 Kevin
      先試 20 篇再手動跑（epics Story 2.11），結果匯入也在 2.11，所以先關著（ADR-0041）。
    - `max_requests_per_batch`：一個 `batch_submission` 最多幾個請求（1～`MAX_REQUESTS_PER_BATCH`，
      官方上限 100,000）。一批在一個交易裡建，每筆拿一把 advisory lock，所以不能太大（ADR-0041）。
    - `month_start_hold_hours`：台北時間每月 1 日 00:00 到這個鐘點以前，整次規劃延後（Anthropic
      的 workspace 上限可能還在用 UTC 的上個月，ADR-0031 取捨）；0 表示不延後。
    - `poll_interval_minutes`：`radar batches wait` 每幾分鐘查一次供應商（預設 10）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    weekly_planning_enabled: bool = Field(strict=True)
    max_requests_per_batch: int = Field(ge=1, le=MAX_REQUESTS_PER_BATCH, strict=True)
    month_start_hold_hours: int = Field(ge=0, le=24, strict=True)
    # `radar batches wait` 每幾分鐘查一次（1～1,440；Story 2.11）。有預設值，舊的設定檔照樣讀得
    # 進來。
    poll_interval_minutes: int = Field(default=10, ge=1, le=1440, strict=True)


def in_month_start_hold(now: datetime, hours: int) -> bool:
    """`now` 換成台北時間後是不是每月 1 日 00:00～`hours` 點以前（整次 Batch 規劃要延後）。

    Raises:
        ValueError: `now` 沒有時區，或 `hours` 不是 0～24 的整數。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 要帶時區（不能是 naive datetime）")
    if type(hours) is not int or not 0 <= hours <= 24:
        raise ValueError("hours 要是 0～24 的整數")
    local = now.astimezone(BUDGET_TIMEZONE)
    return local.day == 1 and local.hour < hours


_ZERO: Final = Decimal(0)


def batch_never_fits(
    config: BudgetConfig,
    tasks: Iterable[LlmTask],
    own: Mapping[LlmTask, Decimal],
    ahead: Mapping[LlmTask, Decimal],
) -> bool:
    """送出前最後確認已經超過預算時，這批是不是「再等也放不下」（ADR-0041 決定 12）。

    `own`：這批還是 `running` 的 run 的預留；`ahead`：排在它前面（建立時間較早；同時建立的比 id）
    、還在 `queued` 的其他批次的預留；都依工作加總，沒有的工作算 0。

    放不下＝就算本月帳歸零（下個月）、已送出的批次都結束釋放，「前面的 queued 批次＋這批」仍超過：
    - 熔斷線：`ahead` 合計＋`own` 合計 > `monthly_breaker_usd`；或
    - `tasks`（這批要送的工作）裡任何一個有功能預算的：該工作的 `ahead`＋`own` > 功能預算。
    包含「這批自己的預留就超過熔斷線或功能預算」。排在後面的 queued 批次與已送出、待人工確認的批次
    不算：前者讓位給這批，後者結果匯入就會釋放（可能等得到）。帳本不算：下個月歸零。

    Raises:
        ValueError: 金額不是有限、不小於 0 的 `Decimal`，或有不認得的工作代碼。
    """
    own_by_task = _amounts(own)
    ahead_by_task = _amounts(ahead)
    total = sum(own_by_task.values(), _ZERO) + sum(ahead_by_task.values(), _ZERO)
    if total > config.monthly_breaker_usd:
        return True
    for task in tasks:
        budget = config.task_budget(task)
        if budget is not None and own_by_task[task] + ahead_by_task[task] > budget:
            return True
    return False


def _amounts(amounts: Mapping[LlmTask, Decimal]) -> dict[LlmTask, Decimal]:
    if set(amounts) - set(LlmTask):
        raise ValueError("預留裡有不認得的工作代碼")
    by_task: dict[LlmTask, Decimal] = {}
    for task in LlmTask:
        amount = amounts.get(task, _ZERO)
        if type(amount) is not Decimal or not amount.is_finite() or amount < 0:
            raise ValueError(f"{task.value} 的預留要是有限、不小於 0 的 Decimal")
        by_task[task] = amount
    return by_task


@dataclass(frozen=True, slots=True)
class PaperAiSource:
    """Batch 規劃看的一篇論文：處理事實（`plan_for` 用）與組 LLM 輸入的資料列。由
    `repo/papers.list_paper_ai_sources` 讀。"""

    facts: ProcessingFacts
    source: PaperLlmSource


@dataclass(frozen=True, slots=True)
class BatchSubmission:
    """讀出來的一筆 `batch_submission`。"""

    id: uuid.UUID
    weekly_run_id: uuid.UUID | None
    status: BatchSubmissionStatus
    provider_batch_id: str | None
    item_count: int
    estimated_max_cost_usd: Decimal
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class BatchItemRun:
    """一筆 `batch_item` 與它的 `derivation_run`（送出前重建請求、核對鍵用）。

    `custom_id` 就是 `item_id.hex`。`batch_submission_id`／`provider_batch_id` 是它所屬的批次與讀出
    當下那批的供應商 batch ID（`repo/llm_batches.list_batch_items` 一起讀；匯入的每個寫入交易拿它們
    核對批次還在匯入同一個 ID，Story 2.11 審查後）。
    """

    item_id: uuid.UUID
    run_id: uuid.UUID
    key: RunKey
    input_kind: InputKind
    run_status: RunStatus
    abstract_id: uuid.UUID | None
    estimated_max_cost_usd: Decimal
    batch_submission_id: uuid.UUID | None = None
    provider_batch_id: str | None = None

    @property
    def custom_id(self) -> str:
        return self.item_id.hex


class BatchImportStoppedError(Exception):
    """匯入途中發現這批已經不在匯入同一個供應商 batch ID（被改成 `needs_attention`、人工確認改了
    ID、或已經不是 `ended`）：這一批停止、不再寫任何列（Story 2.11 審查後）。訊息只有批次 id。"""

    def __init__(self, batch_submission_id: uuid.UUID | None) -> None:
        self.batch_submission_id = batch_submission_id
        super().__init__(f"批次 {batch_submission_id} 已經不在匯入中，停止匯入")


class BatchSubmitUncertainError(Exception):
    """送出時不知道供應商有沒有建好批次（逾時、斷線、5xx、回應看不懂、非預期的例外）：批次已標
    `needs_attention`（標不上時停在 `submitting`，之後照過期規則標），不自動重送。

    訊息只有批次 id 與錯誤類別，不含金鑰與文件內容。
    """

    def __init__(self, batch_submission_id: uuid.UUID, error_type: str) -> None:
        self.batch_submission_id = batch_submission_id
        self.error_type = error_type
        super().__init__(
            f"Batch 送出結果不確定（{error_type}），批次 {batch_submission_id} 要人工確認"
        )


class PlanStop(StrEnum):
    """一次規劃怎麼結束。"""

    COMPLETED = "completed"  # 看完全部論文（放不下的計 deferred_budget）
    DISABLED = "disabled"  # 設定關掉了每週自動規劃（weekly_planning_enabled: false）：整次不排
    # 台北每月 1 日 month_start_hold_hours 點以前：整次延後（規劃途中跨進窗口也在下一批前停下）
    HELD_MONTH_START = "held_month_start"
    BUDGET_TRIPPED = "budget_tripped"  # 已熔斷：整次不排


@dataclass
class LlmBatchPlanResult:
    """一次 `plan_llm_batches` 的結果（計數的單位是「一篇論文的一個工作」）。

    - `planned`：建了 `batch_item`（與 running 的 batch run）的。
    - `skipped`：鎖裡 `should_run` 說不用跑（已有 `active`、平常流程的 `rejected`，或已經有
      running）。
    - `resumable`：有 `llm_result_pending` 可接續（留給互動接續，不重付）。
    - `deferred_budget`：保守上限放不下剩餘預算或功能預算。
    - `no_input`／`blocked`／`too_large`：組請求時擋下。
    - `submission_ids`：這次建的 `batch_submission`。
    - `enqueue_failed`：建好了但排入送出失敗的批次數（sweep 之後補排）。
    """

    stop: PlanStop = PlanStop.COMPLETED
    planned: int = 0
    skipped: int = 0
    resumable: int = 0
    deferred_budget: int = 0
    no_input: int = 0
    blocked: int = 0
    too_large: int = 0
    enqueue_failed: int = 0
    estimated_max_cost_usd: Decimal = Decimal(0)
    submission_ids: list[uuid.UUID] = field(default_factory=list)


class SubmitOutcome(StrEnum):
    """一次 `submit_llm_batch` 的結果。"""

    SUBMITTED = "submitted"  # 送出成功，拿到供應商 batch ID
    NOTHING_TO_DO = "nothing_to_do"  # 已經送過、已失敗或已標 needs_attention：不做事
    LOST_RACE = "lost_race"  # 另一個送出者先把 queued 改走了
    NOT_STALE = "not_stale"  # 停在 submitting、還沒過時限：不動、不送
    # 月初窗口、已熔斷、帳本＋預留超過預算（再等可能放得下），或等預算鎖逾時：不動（維持 queued）、
    # 不送，sweep 之後補排
    HELD = "held"
    NEEDS_ATTENTION = "needs_attention"  # 停在 submitting 已過時限：標 needs_attention、不送
    REQUEUED = "requeued"  # 429：退回 queued，sweep 之後補排
    # 全部輸入都變了、供應商明確拒絕，或預算調低後再等也放不下（`batch_budget_cannot_fit`，不送）：
    # 批次與 run 改 failed
    FAILED = "failed"


@dataclass(frozen=True)
class LlmBatchSubmitResult:
    """一次 `submit_llm_batch` 的結果。

    - `status`：做完之後這筆 `batch_submission` 的狀態（就我們所知）。
    - `sent`：送出的請求數；`input_changed`：送出前核對對不上、改 failed 的 run 數。
    - `reason`：失敗或標 `needs_attention` 的原因代碼。
    """

    outcome: SubmitOutcome
    status: BatchSubmissionStatus
    provider_batch_id: str | None = None
    sent: int = 0
    input_changed: int = 0
    reason: ErrorCode | None = None


# 會走 Batch 的三個工作（有表頭、`plan_for` 會回答的 LLM 工作；ADR-0004 決定第 8 點）。
BATCH_TASKS: Final = (LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL)


# --- 結果匯入（Story 2.11、ADR-0043） --------------------------------------------------------


def results_expired(status: LlmBatchStatus, now: datetime) -> bool:
    """供應商的結果是不是已經過了保留期（`now` 晚於建立時間＋29 天）。過了就不下載，還在跑的項目
    改 `failed`（`batch_results_unavailable`）。

    Raises:
        ValueError: `now` 沒有時區。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 要帶時區（不能是 naive datetime）")
    return now > status.created_at + PROVIDER_RESULTS_RETENTION


def results_incomplete(status: LlmBatchStatus, read: int) -> int | None:
    """結果檔是不是沒讀完：供應商的 `request_counts` 說有幾項結果（`finished`），這次只讀到 `read`
    項（不重複的 custom_id，加上連 custom_id 都讀不到的行）就回應該有的項數；讀夠了，或供應商沒給
    計數（只有替身會這樣）回 None。沒讀完時不判缺項，批次留 `ended` 下次重讀。

    Raises:
        ValueError: `read` 是負數。
    """
    if read < 0:
        raise ValueError("read 不能是負數")
    counts = status.request_counts
    if counts is None or read >= counts.finished:
        return None
    return counts.finished


def batch_overdue(status: LlmBatchStatus, now: datetime) -> bool:
    """還沒結束、但已經過了供應商的處理期限（`expires_at`）：照文件應該要結束了，標「逾期」。

    Raises:
        ValueError: `now` 沒有時區。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 要帶時區（不能是 naive datetime）")
    return status.processing_status is not BatchProcessingStatus.ENDED and now > status.expires_at


class BatchItemOutcome(StrEnum):
    """結果裡一項匯入完的樣子（`llm_gateway.import_llm_batch_result` 回的）。值會寫進日誌。"""

    ACTIVE = "active"  # 表頭 active
    SUPERSEDED = "superseded"  # 表頭 superseded（prompt 升版、換模型、論文被排除或輸入種類變了）
    REJECTED = "rejected"  # 表頭 rejected（拒答、截斷、格式、驗證不過、請求不合法）
    # run failed：errored（非 invalid_request）、canceled、expired、看不懂的一行；不寫表頭
    FAILED = "failed"
    INPUT_CHANGED = "input_changed"  # 等待期間輸入變了：記帳、run failed，不寫表頭
    ALREADY_DONE = "already_done"  # run 已經不是 running（匯入過、別人匯入中）：什麼都不寫
    # 驗證時出了非預期的錯：帳照記，run 改 failed（llm_result_pending，之後由互動接續）
    ERROR = "error"


@dataclass
class BatchImportCounts:
    """一批這次匯入的計數（單位是「結果裡的一項」）。

    - `outcomes`：照 `BatchItemOutcome` 計。
    - `unknown_custom_ids`：custom_id 不屬於這批（或不是 32 hex）；`unreadable_lines`：連
      custom_id 都讀不到的行。兩者都只計數、不寫任何列。
    - `missing`：結果下載完、這批還沒出現（改 `batch_result_missing`）的項目；`unavailable`：結果
      拿不到時改 `batch_results_unavailable` 的項目。
    """

    outcomes: dict[BatchItemOutcome, int] = field(default_factory=dict)
    unknown_custom_ids: int = 0
    unreadable_lines: int = 0
    missing: int = 0
    unavailable: int = 0

    def add(self, outcome: BatchItemOutcome) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

    def count(self, outcome: BatchItemOutcome) -> int:
        return self.outcomes.get(outcome, 0)

    @property
    def imported(self) -> int:
        """這次寫了表頭或結束了 run 的項目數（不含 `already_done`）。"""
        return sum(
            n
            for outcome, n in self.outcomes.items()
            if outcome is not BatchItemOutcome.ALREADY_DONE
        )


@dataclass
class BatchPollRow:
    """一次 poll 裡一個批次的結果。

    - `status`：做完之後這筆 `batch_submission` 的狀態（就我們所知）。
    - `provider_status`：供應商那邊的 `processing_status`（沒查到時是 None）。
    - `overdue`：還沒結束、但已經過了 `expires_at`。
    - `error`：這批出錯時的錯誤類別（與狀態碼），不含訊息；沒有是 None。
    - `stopped`：匯入途中這批被改走（`needs_attention`、換了供應商 batch ID 或已不是 `ended`），
      停止、沒有再寫。
    """

    batch_submission_id: uuid.UUID
    status: BatchSubmissionStatus
    provider_status: BatchProcessingStatus | None = None
    counts: BatchImportCounts = field(default_factory=BatchImportCounts)
    overdue: bool = False
    error: str | None = None
    stopped: bool = False


@dataclass
class LlmBatchPollResult:
    """一次 `poll_llm_batches` 的結果。

    - `rows`：這次查的每個批次（`submitted`／`ended`）。
    - `needs_attention`：做完之後全庫 `needs_attention` 的批次數。
    - `ai_done`：這次進 `ai_done` 的 `weekly_run`。
    """

    rows: list[BatchPollRow] = field(default_factory=list)
    needs_attention: int = 0
    ai_done: list[uuid.UUID] = field(default_factory=list)

    @property
    def errors(self) -> int:
        """出錯的批次數，加上驗證時出非預期錯誤（`error`）的項目數。"""
        return sum(
            (row.error is not None) + row.counts.count(BatchItemOutcome.ERROR) for row in self.rows
        )

    @property
    def healthy(self) -> bool:
        """沒有錯誤、也沒有 `needs_attention`：worker 才送 `poll-batches` 心跳。"""
        return self.errors == 0 and self.needs_attention == 0


_FINISHED: Final = frozenset({BatchSubmissionStatus.IMPORTED, BatchSubmissionStatus.FAILED})


def is_finished(status: BatchSubmissionStatus) -> bool:
    """`imported`／`failed`：`radar batches wait` 不用再等的批次。"""
    return status in _FINISHED


def render_poll_row(row: BatchPollRow) -> str:
    """`radar batches poll` 每個批次一行：批次 id、狀態、供應商狀態、這次匯入的各結果計數、逾期。"""
    provider = "—" if row.provider_status is None else row.provider_status.value
    parts = [f"{row.batch_submission_id}  {row.status.value}  供應商 {provider}"]
    counts = row.counts
    shown = [
        f"{outcome.value} {counts.count(outcome)}"
        for outcome in BatchItemOutcome
        if counts.count(outcome)
    ]
    for label, value in (
        ("missing", counts.missing),
        ("unavailable", counts.unavailable),
        ("不認得的 custom_id", counts.unknown_custom_ids),
        ("讀不懂的行", counts.unreadable_lines),
    ):
        if value:
            shown.append(f"{label} {value}")
    if shown:
        parts.append("、".join(shown))
    if row.overdue:
        parts.append("逾期")
    if row.stopped:
        parts.append("停止匯入（批次狀態變了）")
    if row.error is not None:
        parts.append(f"錯誤：{row.error}")
    return "  ".join(parts)


def render_poll_summary(result: LlmBatchPollResult) -> list[str]:
    """`radar batches poll` 最後的幾行：needs_attention 數、這次進 `ai_done` 的每週更新。"""
    lines: list[str] = []
    if not result.rows:
        lines.append("沒有要查的批次（submitted／ended）")
    lines.append(f"needs_attention：{result.needs_attention} 批")
    if result.needs_attention:
        lines.append(
            "要人工確認：radar batches resolve <批次 id> --provider-batch-id <ID> 或 --fail"
        )
    for weekly_run_id in result.ai_done:
        lines.append(f"每週更新 {weekly_run_id} → ai_done")
    return lines


def render_wait_progress(finished: int, running: int) -> str:
    """`radar batches wait` 每次檢查印的一行。"""
    return f"已結束 {finished} 批、還在跑 {running} 批"


@dataclass
class LlmBatchEstimate:
    """`radar batches create --estimate`：照規劃同樣的檢查，現在會排幾筆（不寫任何列）。

    - `by_task`：各工作會排的筆數；`estimated_max_cost_usd`：它們的保守上限（Batch 價）合計。
    - `skipped`：已經有 running、`active` 或平常流程的 `rejected`；`resumable`：有可接續的
      `llm_result_pending`（留給互動接續）；`no_input`／`blocked`／`too_large`：組請求時擋下。
    預估不看預算（規劃時放不下的會計 `deferred_budget`）。
    """

    by_task: dict[LlmTask, int] = field(default_factory=dict)
    estimated_max_cost_usd: Decimal = Decimal(0)
    skipped: int = 0
    resumable: int = 0
    no_input: int = 0
    blocked: int = 0
    too_large: int = 0

    @property
    def planned(self) -> int:
        return sum(self.by_task.values())


def render_batch_estimate(estimate: LlmBatchEstimate, status: BudgetStatus) -> list[str]:
    """`radar batches create` 送出前印的預估：各工作筆數、Batch 價的保守上限合計、本月預算剩餘。"""
    body = [[task.value, f"{estimate.by_task.get(task, 0):,}"] for task in BATCH_TASKS]
    body.append(["合計", f"{estimate.planned:,}"])
    lines = ["預估要排的 Batch（每篇論文的每個工作一筆）", ""]
    lines += render_table(["工作", "筆數"], body, align="lr")
    lines += [
        "",
        f"保守上限（Batch 價）：{format_usd(estimate.estimated_max_cost_usd)} 美元",
        f"略過（已有結果或在跑）：{estimate.skipped:,}；留給互動接續：{estimate.resumable:,}",
        f"擋下：沒有輸入 {estimate.no_input:,}、放行擋下 {estimate.blocked:,}、"
        f"太長 {estimate.too_large:,}",
        f"本月（{status.window.label}，台北時間）：已花 {format_usd(status.total_usd)}、"
        f"Batch 預留 {format_usd(status.reserved_usd)}、離熔斷線 {format_usd(status.remaining_usd)}"
        f"（熔斷線 {format_usd(status.breaker_usd)}）",
    ]
    if status.breaker_tripped:
        lines.append("已熔斷：規劃不會排任何批次")
    elif estimate.estimated_max_cost_usd > status.remaining_usd:
        lines.append("保守上限超過離熔斷線的金額：放不下的會計 deferred_budget，這次不排")
    return lines


def render_queued_batches(
    queued: Iterable[BatchSubmission], estimate: LlmBatchEstimate
) -> list[str]:
    """`radar batches create` 確認前印：已經排好、還沒送出的批次（這次一起送），與這次確認的合計
    上限（已排好的＋新排的）。"""
    rows = list(queued)
    queued_cost = sum((row.estimated_max_cost_usd for row in rows), _ZERO)
    lines: list[str] = []
    if rows:
        lines += ["", f"已排好、還沒送出的批次：{len(rows)} 批（這次一起送）", ""]
        body = [
            [str(row.id), f"{row.item_count:,}", format_usd(row.estimated_max_cost_usd)]
            for row in rows
        ]
        lines += render_table(["批次", "筆數", "保守上限"], body, align="lrr")
    queued_items = sum(row.item_count for row in rows)
    split = f"（新排 {estimate.planned:,}＋已排好 {queued_items:,}）" if rows else ""
    lines += [
        "",
        f"這次會送出：{estimate.planned + queued_items:,} 筆{split}，保守上限合計 "
        f"{format_usd(estimate.estimated_max_cost_usd + queued_cost)} 美元",
    ]
    return lines


class ResolveOutcome(StrEnum):
    """`radar batches resolve` 的結果。"""

    RESUMED = "resumed"  # needs_attention → submitted（記下 batch ID）
    FAILED = "failed"  # needs_attention → failed，還在跑的 run 改 failed
    NOT_NEEDS_ATTENTION = "not_needs_attention"  # 這批不是 needs_attention：不改
    ID_IN_USE = "id_in_use"  # 這個供應商 batch ID 已經是別批的：不改


@dataclass(frozen=True)
class BatchResolveResult:
    """一次 `resolve_llm_batch` 的結果：`status` 是做完之後這批的狀態；`released_runs` 是 `--fail`
    改成 failed 的 run 數。"""

    outcome: ResolveOutcome
    status: BatchSubmissionStatus
    released_runs: int = 0
