"""Batch 送出的 domain 形狀（AD-8、AD-14、ADR-0004 決定第 4 點、ADR-0031 決定 15、ADR-0041、
Story 2.9）。

- `batch_submission` 的狀態（`BatchSubmissionStatus`）：`queued` → `submitting` → `submitted` →
  `ended` → `imported`；`failed`；`needs_attention`（需要人工處理）。2.9 只走到 `submitted`，
  `ended`／`imported` 由結果匯入（2.11）推進。狀態只經 `services/llm_batches.py` 改。
- 設定 `config/llm_batches.yaml`（`LlmBatchConfig`，只經 `radar/config.py` 讀）：每批最多幾個請求、
  台北每月 1 日幾點以前整次規劃延後（`in_month_start_hold`）。
- `SUBMITTING_STALE_AFTER`：停在 `submitting` 而且沒有供應商 batch ID 超過這麼久，就判定送出的
  工作已經不在了（送出不重試，最久約一次 `RADAR_ANTHROPIC_TIMEOUT`），標 `needs_attention`、
  不重送；sweep 的 `submitting` 那一列用同一個常數。
- 結果型別（`LlmBatchPlanResult`、`LlmBatchSubmitResult`）的 `stop`／`outcome` 值會寫進日誌。
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from radar.domain.ai_outputs import InputKind, LlmTask, PaperLlmSource, RunKey, RunStatus
from radar.domain.budget import BUDGET_TIMEZONE
from radar.domain.errors import ErrorCode
from radar.domain.processing import ProcessingFacts

# 停在 `submitting` 沒有 batch ID 多久算送出的工作已經不在（ADR-0041）。
SUBMITTING_STALE_AFTER: Final = timedelta(minutes=30)
# 停在 `queued` 多久由 sweep 補排送出（429 退回 `queued`、排入失敗、`ConfigError` 留著的列）。
QUEUED_STUCK_AFTER: Final = timedelta(hours=1)
# Anthropic 一批最多 100,000 個請求（docs/runs/epic-2b/api-reference.md 第一節）。
PROVIDER_MAX_REQUESTS: Final = 100_000
# 供應商 batch ID 的格式（和資料庫的 CHECK 一樣）。
PROVIDER_BATCH_ID_PATTERN: Final = r"^[A-Za-z0-9_-]{1,128}$"


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

    - `max_requests_per_batch`：一個 `batch_submission` 最多幾個請求（官方上限 100,000）。一批在
      一個交易裡建，每筆拿一把 advisory lock，所以不能太大（ADR-0041）。
    - `month_start_hold_hours`：台北時間每月 1 日 00:00 到這個鐘點以前，整次規劃延後（Anthropic
      的 workspace 上限可能還在用 UTC 的上個月，ADR-0031 取捨）；0 表示不延後。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_requests_per_batch: int = Field(ge=1, le=PROVIDER_MAX_REQUESTS, strict=True)
    month_start_hold_hours: int = Field(ge=0, le=24, strict=True)


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

    `custom_id` 就是 `item_id.hex`。
    """

    item_id: uuid.UUID
    run_id: uuid.UUID
    key: RunKey
    input_kind: InputKind
    run_status: RunStatus
    abstract_id: uuid.UUID | None
    estimated_max_cost_usd: Decimal

    @property
    def custom_id(self) -> str:
        return self.item_id.hex


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
    HELD_MONTH_START = "held_month_start"  # 台北每月 1 日 month_start_hold_hours 點以前：整次延後
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
    NEEDS_ATTENTION = "needs_attention"  # 停在 submitting 已過時限：標 needs_attention、不送
    REQUEUED = "requeued"  # 429：退回 queued，sweep 之後補排
    FAILED = "failed"  # 全部輸入都變了，或供應商明確拒絕：批次與 run 改 failed


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
