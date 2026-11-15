"""Batch 的規劃、送出與結果匯入（AD-8、AD-14、ADR-0004 決定第 2、4 點、ADR-0031 決定 15、
ADR-0041、ADR-0043、Story 2.9、2.11）。`batch_submission` 的狀態只在這裡改。

規劃（`plan_llm_batches`，每週更新發佈快照後排入；`radar batches create` 以 `manual=True` 呼叫）：

0. 不是手動、而且設定 `weekly_planning_enabled` 是 false：不排、不寫任何列（先維持 false 的保守
   做法，待 Kevin 決定；全庫第一次由 `radar batches create` 手動送）。
1. 台北時間每月 1 日 00:00～`month_start_hold_hours` 點以前：整次延後，不寫任何列、不算失敗；
   每一批開始前再看一次，途中跨進窗口就停（已 commit 的批次留著）。
2. 已熔斷：整次不排。
2a. 分類表或情境值清單改版後要重跑的（`active` 的設定檔版本和現在不同）保守上限合計超過
   `revision_rerun_max_usd`：整次不排、不寫任何列、記錯誤日誌（`estimate_revision_reruns`，
   Story 2.12、ADR-0044）。第一次全庫沒有 `active`，不受影響。
3. 每個工作讀一次 prompt；依論文 id 分頁讀論文，每篇照 `plan_for` 要做的工作組 `LlmJob`，經閘門的
   `prepare_llm_request` 放行、算指紋、組請求、檢查大小（`NO_INPUT`／`BLOCKED`／`TOO_LARGE`
   計數略過）。
4. 每批一個短交易：先拿全域的 Batch 預算鎖，鎖裡再看一次月初窗口（等鎖可能跨過台北午夜；在窗口裡就
   整批不寫、停止規劃），再讀本月帳與尚未結束的 Batch 預留（`services/budget.read_budget_status`，
   和互動的 `BudgetGuard` 同一句 SQL）；逐筆用保守上限
   （`max_call_cost`）對照剩餘預算與功能預算，放不下的計 `deferred_budget`（不建列）；放得下的經
   `claim_batch_run` 取得資格（鎖→running→表頭→可接續→新增 batch run），成功就（第一筆時建
   `queued` 的 `batch_submission`）新增 `batch_item`、累加預留。批滿或讀完就記筆數與預留合計、
   commit，再排入 `submit_llm_batch`（排入失敗只記警告，sweep 補排）。
   一個交易最多嘗試 `2 × max_requests_per_batch` 筆（每筆一把 advisory lock，重跑時大多被略過，
   鎖表不能爆），所以一批可能少於上限。

送出（`submit_llm_batch`）：

- `submitted` 以後、`failed`、`needs_attention`：不做事。
- `submitting`（沒有 batch ID）：只有 `updated_at` 早於「現在減 `stale_after`」時改
  `needs_attention`（送出的工作已經不在），否則不動；兩種都不送（沒有冪等鍵，不重送）。
- `queued`：先看月初窗口與帳本熔斷（規劃之後可能變了），在窗口裡或已熔斷就不動、不送（回 `HELD`，
  列維持 `queued`，sweep 之後補排）。再每筆重讀論文、再問 `plan_for`（被排除或輸入種類變了也算
  對不上）、重建請求、核對 run 的鍵；對不上的 run 改 `failed`、不送。
  一個交易裡先拿全域 Batch 預算鎖（等超過 `lock_timeout_ms` 就整個交易退回，`HELD`／
  `batch_budget_lock_busy`），再看一次月初窗口（查核可能跨過午夜）與「帳本＋全部預留」（超過熔斷線
  或這批工作的功能預算時：再等也放不下〔`batch_never_fits`〕就在同一個交易把批次從 `queued` 改
  `failed`、這批的 run 改 `failed`〔`batch_budget_cannot_fit`，釋放預留〕；否則 `HELD`／
  `batch_budget_exceeded`，不寫任何列），都過了才
  條件式 `queued`→`submitting`（同一句改實際筆數），套用了才建 provider（`open_provider`，缺金鑰
  時整個交易退回、列還是 `queued`；過期標記、延後、全部對不上、搶輸這些只碰資料庫的路都不建），
  並把對不上的 run 改 `failed`；交易外經閘門的
  `send_llm_batch` 送出；拿到 ID 先記 info 日誌，再條件式 `submitting`→`submitted`。
- 錯誤：有 HTTP 回應的 4xx 一定沒建立：429 退回 `queued`（不丟），其他改 `failed`、run 也改
  `failed`，原例外丟出；送出任何請求前就丟的 `NotCloudSafeError` 與沒有狀態碼的
  `LlmConfigurationError` 也一定沒建立，照同樣處理。其餘（逾時、斷線、5xx、回應看不懂、一般的
  TypeError／ValueError 與其他非預期例外）不確定，標 `needs_attention`、丟
  `BatchSubmitUncertainError`；回應看不懂但帶了像樣的 batch ID 時一併記下。條件式更新沒套用時
  重讀狀態、照實回報（`LOST_RACE`）；重讀一律在那個交易結束之後（連線池只有一條時不會等自己）。
  寫回本身失敗時不重送，下一次看到 `submitting` 走過期規則。

預估（`estimate_llm_batches`，`radar batches create` 送出前印）：照規劃同樣的候選（`_candidates`），
每 200 筆開一個交易（只讀不寫）查會不會被略過（running → 表頭 → 可接續，和取得資格的順序一樣），加總
Batch 價的保守上限。不寫任何列、不建 provider、不看預算。

結果匯入（`poll_llm_batches`，worker 的 `poll_batches` 工作與 `radar batches poll`／`wait` 共用）：

- 列出 `submitted`／`ended` 的批次（可限定 id）；有要查的才呼叫 `open_provider()`（一次）。
- 每批經閘門的 `fetch_llm_batch` 查狀態：
  - 找不到（404）：`submitted`／`ended` → `needs_attention`（`batch_provider_not_found`），不丟。
  - 其他供應商錯誤：這批不動、記錯誤、繼續下一批（結果的 `errors` 加一）。
  - `in_progress`／`canceling`：不動；過了 `expires_at` 標「逾期」並記警告。
  - `ended`：條件式 `submitted` → `ended`（記 `ended_at`；上次中斷已經是 `ended` 的照樣往下做）。
    過了 29 天保留期或沒有結果檔 → 還 `running` 的 run 全部 `failed`
    （`batch_results_unavailable`）；
    否則串流結果，用 `custom_id` 對回 item，逐項經閘門的 `import_llm_batch_result` 匯入（不認得的
    custom_id 與讀不到 custom_id 的行只計數）；串流讀完後還 `running` 的 → `batch_result_missing`。
    最後條件式 `ended` → `imported`（同一句確認這批已經沒有 `running` 的 run）。串流途中斷線：記成
    這批的錯誤，批次留 `ended`，下一次接著做（已匯入的項目 run 已經不是 `running`，不重記帳）。
  - 單一批次裡的非預期錯誤（例如 prompt 版本讀不出來）：記錯誤類別、這批留在原狀態、繼續下一批
    （結果的 `errors` 加一）。
- 最後 `weekly_update.mark_weekly_runs_ai_done`、讀全庫 `needs_attention` 的筆數。

人工確認（`resolve_llm_batch`，`radar batches resolve`）：只對 `needs_attention`。給供應商 batch ID
→ `submitted`（記 ID、清原因；ID 已經是別批的就不改）；`fail` → `failed`
（`batch_resolved_failed`），
同一個交易把這批還 `running` 的 run 改 `failed`（同代碼，釋放預留）。不呼叫供應商。

日誌與錯誤只帶 id、計數、原因代碼與錯誤類別，不帶金鑰、文件內容與模型輸出。
"""

import re
import uuid
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol

import structlog

from radar.domain.admin_runs import RevisionEstimate
from radar.domain.ai_outputs import (
    CallMode,
    GatewayConfig,
    InputKind,
    LlmTask,
    OutputLang,
    RunStatus,
)
from radar.domain.budget import BudgetConfig
from radar.domain.context_values import ContextValues
from radar.domain.errors import ErrorCode, LockWaitTimeoutError
from radar.domain.fingerprints import LLM_BATCH_BUDGET_LOCK_KEY
from radar.domain.jobs import SUBMIT_LLM_BATCH, Enqueue
from radar.domain.llm import (
    BatchProcessingStatus,
    LlmBatchRequest,
    LlmConfigurationError,
    LlmError,
    LlmMalformedResponseError,
    NotCloudSafeError,
)
from radar.domain.llm_batches import (
    BATCH_TASKS,
    PROVIDER_BATCH_ID_PATTERN,
    SUBMIT_BUDGET_LOCK_TIMEOUT_MS,
    SUBMITTING_STALE_AFTER,
    BatchImportCounts,
    BatchItemOutcome,
    BatchItemRun,
    BatchPollRow,
    BatchResolveResult,
    BatchSubmission,
    BatchSubmissionStatus,
    BatchSubmitUncertainError,
    LlmBatchConfig,
    LlmBatchEstimate,
    LlmBatchPlanResult,
    LlmBatchPollResult,
    LlmBatchSubmitResult,
    PlanStop,
    ResolveOutcome,
    SubmitOutcome,
    batch_never_fits,
    batch_overdue,
    in_month_start_hold,
    results_expired,
)
from radar.domain.permissions import Action, require
from radar.domain.pricing import max_call_cost
from radar.domain.processing import ProcessingTask, plan_for
from radar.domain.taxonomy import Taxonomy
from radar.domain.validators.classification import ClassificationValidator
from radar.domain.validators.research_model import ResearchModelValidator
from radar.domain.validators.zh_summary import ZhSummaryRules, ZhSummaryValidator
from radar.domain.viewer import SystemViewer, Viewer
from radar.ports import LlmProvider
from radar.repo.ai_outputs import find_active_config_versions, find_blocking_ai_result
from radar.repo.db import Database, DbConnection
from radar.repo.llm_batches import (
    count_batch_running_runs,
    count_batch_submissions,
    create_batch_item,
    create_batch_submission,
    find_batch_submission,
    list_batch_items,
    list_batch_reservations_ahead_by_task,
    list_batch_submissions,
    update_batch_submission_attention_on_not_found,
    update_batch_submission_ended,
    update_batch_submission_failed,
    update_batch_submission_imported,
    update_batch_submission_late_batch_id,
    update_batch_submission_needs_attention,
    update_batch_submission_requeued,
    update_batch_submission_resolved_failed,
    update_batch_submission_resumed,
    update_batch_submission_submitted,
    update_batch_submission_submitting,
    update_batch_submission_totals,
)
from radar.repo.llm_ledger import (
    find_resumable_derivation_run_id,
    find_running_derivation_run_id,
    update_derivation_run_failed,
    update_derivation_runs_failed_for_batch,
)
from radar.repo.papers import (
    find_paper_llm_source,
    find_paper_processing_facts,
    list_paper_ai_sources,
)
from radar.services.budget import get_budget_status, read_budget_status
from radar.services.llm_gateway import (
    BatchClaimOutcome,
    GatewayOutcome,
    GatewayResult,
    LlmJob,
    PreparedLlmRequest,
    claim_batch_run,
    fetch_llm_batch,
    import_llm_batch_result,
    iter_llm_batch_results,
    prepare_llm_request,
    send_llm_batch,
)
from radar.services.prompt_loader import LoadedPrompt, load_prompt
from radar.services.weekly_update import mark_weekly_runs_ai_done

log = structlog.get_logger(__name__)

# 依論文 id 分頁，一頁讀幾篇。
PAGE_SIZE: Final = 200
# 一個交易最多嘗試幾筆（`max_requests_per_batch` 的倍數）：每筆拿一把 advisory lock。
ATTEMPTS_PER_ITEM: Final = 2
_ZERO: Final = Decimal(0)
_BATCH_ID: Final = re.compile(PROVIDER_BATCH_ID_PATTERN)


@dataclass(frozen=True)
class LlmBatchSetup:
    """Batch 規劃與送出要的設定（worker 執行時讀檔組好；不含 provider）。

    Raises:
        ValueError: `gateway.call_mode` 不是 Batch。
    """

    gateway: GatewayConfig
    budget: BudgetConfig
    batches: LlmBatchConfig
    zh_summary_rules: ZhSummaryRules
    taxonomy: Taxonomy
    context_values: ContextValues

    def __post_init__(self) -> None:
        if self.gateway.call_mode is not CallMode.BATCH:
            raise ValueError("Batch 的閘門設定要用 call_mode=batch")


class LlmJobSettings(Protocol):
    """`llm_job` 要的三份設定（`LlmBatchSetup` 與 2.10 的 `TrialSetup` 都符合）。"""

    @property
    def zh_summary_rules(self) -> ZhSummaryRules: ...

    @property
    def taxonomy(self) -> Taxonomy: ...

    @property
    def context_values(self) -> ContextValues: ...


def llm_job(
    setup: LlmJobSettings, paper_id: uuid.UUID, task: LlmTask, input_kind: InputKind
) -> LlmJob:
    """三個 Batch 工作的 `LlmJob`（和 2.4～2.6 的 service 一樣的語言、驗證與設定版本）。2.10 的
    試跑預估也用它，所以指紋和實跑一樣。

    Raises:
        ValueError: `task` 不是 Batch 工作。
    """
    task = LlmTask(task)
    if task is LlmTask.ZH_SUMMARY:
        return LlmJob(
            paper_id=paper_id,
            task=task,
            input_kind=input_kind,
            lang=OutputLang.ZH_HANT,
            validate_output=ZhSummaryValidator(setup.zh_summary_rules),
        )
    if task is LlmTask.CLASSIFY:
        return LlmJob(
            paper_id=paper_id,
            task=task,
            input_kind=input_kind,
            lang=OutputLang.ZXX,
            validate_output=ClassificationValidator(setup.taxonomy),
            taxonomy_version=setup.taxonomy.version_label,
            taxonomy=setup.taxonomy,
        )
    if task is LlmTask.EXTRACT_MODEL:
        return LlmJob(
            paper_id=paper_id,
            task=task,
            input_kind=input_kind,
            lang=OutputLang.EN,
            validate_output=ResearchModelValidator(setup.context_values),
            context_values_version=setup.context_values.version_label,
            context_values=setup.context_values,
        )
    raise ValueError(f"{task.value} 不走 Batch")


# --- 規劃 ---------------------------------------------------------------------------------


def plan_llm_batches(
    actor: Viewer,
    database: Database,
    setup: LlmBatchSetup,
    *,
    weekly_run_id: uuid.UUID | None,
    clock: Callable[[], datetime],
    enqueue: Enqueue,
    manual: bool = False,
) -> LlmBatchPlanResult:
    """把 `plan_for` 要做、`should_run` 說要跑的 AI 工作排成 Batch（見模組說明）。`manual` 是
    `radar batches create` 手動規劃：不看 `weekly_planning_enabled`（月初窗口、熔斷照樣看）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: 時鐘回了沒有時區的時間。
        PromptIntegrityError, PromptFormatError: prompt 檔或登記不對（還沒寫任何列）。
        Exception: 資料庫錯誤原樣丟出（進行中那一批的交易退回；已 commit 的批次留著）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    result = LlmBatchPlanResult()
    if not manual and not setup.batches.weekly_planning_enabled:
        result.stop = PlanStop.DISABLED
        log.info("每週 Batch 規劃沒有開（Story 2.11），不排", weekly_run_id=_id_text(weekly_run_id))
        return result
    now = clock()
    if in_month_start_hold(now, setup.batches.month_start_hold_hours):
        result.stop = PlanStop.HELD_MONTH_START
        log.info(
            "月初窗口，Batch 規劃延後",
            weekly_run_id=_id_text(weekly_run_id),
            hold_hours=setup.batches.month_start_hold_hours,
        )
        return result
    status = get_budget_status(SystemViewer(), database, setup.budget, now=now)
    if status.breaker_tripped:
        result.stop = PlanStop.BUDGET_TRIPPED
        log.warning(
            "已熔斷，Batch 規劃不排",
            weekly_run_id=_id_text(weekly_run_id),
            month=status.window.label,
            total_usd=str(status.total_usd),
        )
        return result
    reruns = estimate_revision_reruns(
        actor, database, setup, tasks=REVISION_TASKS, assume_changed=False
    )
    if reruns.exceeds_cap:
        # 分類表或情境值清單改版後要重跑的超過上限：整次不排、不寫任何列，等 Kevin 決定
        # （ADR-0044）。
        result.stop = PlanStop.RERUN_CAP_EXCEEDED
        log.error(
            "改版後要重跑的保守上限超過 revision_rerun_max_usd，Batch 規劃整次不排",
            weekly_run_id=_id_text(weekly_run_id),
            reruns=reruns.reruns,
            estimated_max_cost_usd=str(reruns.max_cost_usd),
            cap_usd=str(reruns.cap_usd),
        )
        return result

    prompts = {task: load_prompt(setup.gateway.prompts_root, task) for task in BATCH_TASKS}
    candidates = _candidates(database, setup, prompts, result)
    capacity = setup.batches.max_requests_per_batch
    attempt_cap = ATTEMPTS_PER_ITEM * capacity
    pending: list[PreparedLlmRequest] = []
    exhausted = False
    while True:
        # 先把這一批要看的候選讀好（交易外），交易裡不再分頁讀：不在持鎖時等第二條連線。
        while not exhausted and len(pending) < attempt_cap:
            item = next(candidates, None)
            if item is None:
                exhausted = True
            else:
                pending.append(item)
        if not pending:
            break
        if in_month_start_hold(clock(), setup.batches.month_start_hold_hours):
            # 規劃途中跨進月初窗口：剩下的不排（不算失敗，下次再排）。
            result.stop = PlanStop.HELD_MONTH_START
            log.info(
                "規劃途中進入月初窗口，剩下的 Batch 延後",
                weekly_run_id=_id_text(weekly_run_id),
                hold_hours=setup.batches.month_start_hold_hours,
            )
            break
        used, submission_id = _plan_one_batch(
            actor, database, setup, pending, weekly_run_id, clock, result
        )
        if used is None:
            # 等預算鎖的時候跨進月初窗口（鎖裡重查）：這一批沒寫任何列，剩下的不排。
            result.stop = PlanStop.HELD_MONTH_START
            log.info(
                "取得預算鎖時已進入月初窗口，剩下的 Batch 延後",
                weekly_run_id=_id_text(weekly_run_id),
                hold_hours=setup.batches.month_start_hold_hours,
            )
            break
        pending = pending[used:]
        if submission_id is not None:
            _enqueue_submit(enqueue, submission_id, result)

    log.info(
        "Batch 規劃完成",
        weekly_run_id=_id_text(weekly_run_id),
        stop=result.stop.value,
        submissions=len(result.submission_ids),
        planned=result.planned,
        skipped=result.skipped,
        resumable=result.resumable,
        deferred_budget=result.deferred_budget,
        no_input=result.no_input,
        blocked=result.blocked,
        too_large=result.too_large,
        enqueue_failed=result.enqueue_failed,
        estimated_max_cost_usd=str(result.estimated_max_cost_usd),
    )
    return result


def _candidates(
    database: Database,
    setup: LlmBatchSetup,
    prompts: dict[LlmTask, LoadedPrompt],
    result: LlmBatchPlanResult,
) -> Iterator[PreparedLlmRequest]:
    """依論文 id 分頁讀論文，逐篇逐工作組好請求；擋下的計數、不產出。"""
    viewer = SystemViewer()
    after: uuid.UUID | None = None
    while True:
        with database.transaction() as connection:
            page = list_paper_ai_sources(viewer, connection, after_id=after, limit=PAGE_SIZE)
        if not page:
            return
        after = page[-1].source.paper_id
        for paper in page:
            plan = plan_for(paper.facts)
            for task in BATCH_TASKS:
                task_plan = plan.for_task(ProcessingTask(task.value))
                if task_plan.input_kind is None:
                    continue
                job = llm_job(setup, paper.source.paper_id, task, task_plan.input_kind)
                prepared = prepare_llm_request(setup.gateway, job, paper.source, prompts[task])
                if isinstance(prepared, GatewayResult):
                    _count_blocked(prepared, result)
                else:
                    yield prepared


def estimate_llm_batches(
    actor: Viewer, database: Database, setup: LlmBatchSetup
) -> LlmBatchEstimate:
    """現在規劃的話會排幾筆、保守上限多少（見模組說明「預估」）。不寫任何列。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        PromptIntegrityError, PromptFormatError: prompt 檔或登記不對。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    prompts = {task: load_prompt(setup.gateway.prompts_root, task) for task in BATCH_TASKS}
    blocked = LlmBatchPlanResult()
    estimate = LlmBatchEstimate()
    chunk: list[PreparedLlmRequest] = []
    for prepared in _candidates(database, setup, prompts, blocked):
        chunk.append(prepared)
        if len(chunk) >= PAGE_SIZE:
            _estimate_chunk(database, setup, chunk, estimate)
            chunk = []
    if chunk:
        _estimate_chunk(database, setup, chunk, estimate)
    estimate.no_input = blocked.no_input
    estimate.blocked = blocked.blocked
    estimate.too_large = blocked.too_large
    return estimate


def _estimate_chunk(
    database: Database,
    setup: LlmBatchSetup,
    chunk: list[PreparedLlmRequest],
    estimate: LlmBatchEstimate,
) -> None:
    """（只讀不寫的交易）這幾筆會不會被略過，會排的計進各工作的筆數與保守上限。"""
    viewer = SystemViewer()
    with database.transaction() as connection:
        for prepared in chunk:
            key = prepared.key
            if (
                find_running_derivation_run_id(viewer, connection, key, None) is not None
                or find_blocking_ai_result(viewer, connection, key) is not None
            ):
                estimate.skipped += 1
                continue
            if find_resumable_derivation_run_id(viewer, connection, key, None) is not None:
                estimate.resumable += 1
                continue
            estimate.by_task[key.task] = estimate.by_task.get(key.task, 0) + 1
            estimate.estimated_max_cost_usd += max_call_cost(
                setup.gateway.models,
                key.model,
                prepared.estimated_input_tokens,
                prepared.request.max_tokens,
                is_batch=True,
            )


def _count_blocked(blocked: GatewayResult, result: LlmBatchPlanResult) -> None:
    if blocked.outcome is GatewayOutcome.NO_INPUT:
        result.no_input += 1
    elif blocked.outcome is GatewayOutcome.BLOCKED:
        result.blocked += 1
    elif blocked.outcome is GatewayOutcome.TOO_LARGE:
        result.too_large += 1
    else:  # prepare_llm_request 只會回這三種
        raise RuntimeError(f"prepare_llm_request 回了沒預期的結果：{blocked.outcome.value}")


@dataclass
class _Remaining:
    """鎖裡算出的剩餘預算：熔斷線與各功能預算（None＝沒有個別上限）。"""

    total: Decimal
    by_task: dict[LlmTask, Decimal | None]

    def fits(self, task: LlmTask, cost: Decimal) -> bool:
        if self.total <= _ZERO or cost > self.total:
            return False
        task_left = self.by_task[task]
        return task_left is None or (task_left > _ZERO and cost <= task_left)

    def take(self, task: LlmTask, cost: Decimal) -> None:
        self.total -= cost
        task_left = self.by_task[task]
        if task_left is not None:
            self.by_task[task] = task_left - cost


def _remaining(connection: DbConnection, budget: BudgetConfig, now: datetime) -> _Remaining:
    """（全域 Batch 預算鎖裡）熔斷線−本月帳−尚未結束的 Batch 預留，各功能預算同理（和互動的
    `BudgetGuard` 同一個計算：`services/budget.read_budget_status`）。"""
    status = read_budget_status(SystemViewer(), connection, budget, now=now)
    by_task: dict[LlmTask, Decimal | None] = {}
    for task in BATCH_TASKS:
        task_budget = budget.task_budget(task)
        by_task[task] = None if task_budget is None else task_budget - status.task_committed(task)
    return _Remaining(total=status.breaker_usd - status.committed_usd, by_task=by_task)


def _plan_one_batch(
    actor: Viewer,
    database: Database,
    setup: LlmBatchSetup,
    pending: list[PreparedLlmRequest],
    weekly_run_id: uuid.UUID | None,
    clock: Callable[[], datetime],
    result: LlmBatchPlanResult,
) -> tuple[int | None, uuid.UUID | None]:
    """一個交易：預算鎖→重查月初窗口→逐筆放得下就取得資格、建 item。回（看了幾筆, 建的
    submission id）；拿到鎖時已在月初窗口（等鎖跨過台北午夜）就什麼都不寫、回 (None, None)。

    計數在交易 commit 之後才併進 `result`（交易退回時不算）。
    """
    capacity = setup.batches.max_requests_per_batch
    attempt_cap = ATTEMPTS_PER_ITEM * capacity
    tally = LlmBatchPlanResult()
    submission_id: uuid.UUID | None = None
    used = 0
    attempts = 0
    with database.transaction() as connection:
        Database.transaction_lock(connection, LLM_BATCH_BUDGET_LOCK_KEY)
        now = clock()
        if in_month_start_hold(now, setup.batches.month_start_hold_hours):
            # 等鎖可能等很久：拿到鎖之後再看一次，跨進窗口就整批不寫（交易沒有任何寫入）。
            return None, None
        remaining = _remaining(connection, setup.budget, now)
        for prepared in pending:
            if tally.planned >= capacity or attempts >= attempt_cap:
                break
            used += 1
            task = prepared.key.task
            cost = max_call_cost(
                setup.gateway.models,
                prepared.key.model,
                prepared.estimated_input_tokens,
                prepared.request.max_tokens,
                is_batch=True,
            )
            if not remaining.fits(task, cost):
                tally.deferred_budget += 1
                continue
            attempts += 1
            claim = claim_batch_run(actor, connection, setup.gateway, prepared)
            if claim.outcome is BatchClaimOutcome.SKIPPED:
                tally.skipped += 1
                continue
            if claim.outcome is BatchClaimOutcome.RESUMABLE:
                tally.resumable += 1
                continue
            if claim.run_id is None:  # CLAIMED 一定有 run_id
                raise RuntimeError("取得資格卻沒有 run_id")
            if submission_id is None:
                submission_id = create_batch_submission(connection, weekly_run_id)
            create_batch_item(
                connection, submission_id, claim.run_id, prepared.llm_input.abstract_id, cost
            )
            remaining.take(task, cost)
            tally.planned += 1
            tally.estimated_max_cost_usd += cost
        if submission_id is not None and not update_batch_submission_totals(
            connection, submission_id, tally.planned, tally.estimated_max_cost_usd
        ):
            raise RuntimeError("剛建的 batch_submission 已經不是 queued")
    result.planned += tally.planned
    result.skipped += tally.skipped
    result.resumable += tally.resumable
    result.deferred_budget += tally.deferred_budget
    result.estimated_max_cost_usd += tally.estimated_max_cost_usd
    if submission_id is not None:
        result.submission_ids.append(submission_id)
        log.info(
            "建好一個 Batch",
            batch_submission_id=str(submission_id),
            items=tally.planned,
            estimated_max_cost_usd=str(tally.estimated_max_cost_usd),
        )
    return used, submission_id


def _enqueue_submit(enqueue: Enqueue, submission_id: uuid.UUID, result: LlmBatchPlanResult) -> None:
    """commit 之後排入送出。排不進去只記警告：列是 `queued`，sweep 之後補排。"""
    try:
        enqueue(SUBMIT_LLM_BATCH, {"batch_submission_id": submission_id})
    except Exception as exc:
        result.enqueue_failed += 1
        log.warning(
            "Batch 建好了，排入送出失敗（sweep 之後補排）",
            batch_submission_id=str(submission_id),
            error_type=type(exc).__name__,
        )


# --- 送出 ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Checked:
    """送出前核對的結果：要送的請求（與它們的 run）、對不上的 run 與原因。"""

    requests: list[LlmBatchRequest]
    sent_run_ids: list[uuid.UUID]
    changed: list[tuple[uuid.UUID, ErrorCode]]
    sent_tasks: frozenset[LlmTask]


def submit_llm_batch(
    actor: Viewer,
    database: Database,
    open_provider: Callable[[], LlmProvider],
    setup: LlmBatchSetup,
    batch_submission_id: uuid.UUID,
    *,
    clock: Callable[[], datetime],
    stale_after: timedelta = SUBMITTING_STALE_AFTER,
    lock_timeout_ms: int = SUBMIT_BUDGET_LOCK_TIMEOUT_MS,
) -> LlmBatchSubmitResult:
    """送出一個 `queued` 的批次（見模組說明）。

    `open_provider` 只在確定要送出時呼叫一次（查核與最後確認都過了、條件式 `queued`→`submitting`
    套用之後、同一個交易 commit 之前；建 provider 不連線）：過期的 `submitting` 標
    `needs_attention`、已結束、延後、全部對不上、搶輸這些只碰資料庫的路都不建 provider，所以缺金鑰時
    也照樣做得完。它丟的錯（例如缺金鑰的 `ConfigError`）原樣往上丟、交易退回，列維持 `queued`。

    最後確認的交易等鎖超過 `lock_timeout_ms` 毫秒（規劃的長交易拿著全域預算鎖）時回 `HELD`
    （`batch_budget_lock_busy`），不寫列、不建 provider。預算調低後這批再等也放不下時，同一個交易
    取消（批次與 run 改 `failed`、`batch_budget_cannot_fit`，回 `FAILED`，不丟例外）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        LookupError: 沒有這個 `batch_submission`。
        ValueError: `stale_after` 不是正的時間長度、`lock_timeout_ms` 不是正整數，或時鐘回了沒有時區
            的時間。
        PromptIntegrityError, PromptFormatError: prompt 檔或登記不對（列維持 `queued`）。
        Exception: `open_provider` 丟的錯（例如缺金鑰的 `ConfigError`）原樣丟出（列維持 `queued`）。
        LlmError, NotCloudSafeError: 一定沒有建立（4xx 但 429 除外、沒有狀態碼的設定錯、送出前就
            擋下的文字）：批次與送出的 run 已改 `failed`，原樣丟出。
        BatchSubmitUncertainError: 不知道供應商有沒有建好批次：已標 `needs_attention`（標不上時
            停在 `submitting`，之後照過期規則標）。
        Exception: 寫回時的資料庫錯誤原樣丟出（不重送；列停在 `submitting`）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    if stale_after <= timedelta(0):
        raise ValueError("stale_after 要大於 0")
    if type(lock_timeout_ms) is not int or lock_timeout_ms < 1:
        raise ValueError("lock_timeout_ms 要是正整數")
    viewer = SystemViewer()
    with database.transaction() as connection:
        row = find_batch_submission(viewer, connection, batch_submission_id)
    if row is None:
        raise LookupError("找不到 batch_submission")
    if row.status is BatchSubmissionStatus.SUBMITTING:
        return _expire_submitting(database, batch_submission_id, stale_after)
    if row.status is not BatchSubmissionStatus.QUEUED:
        return LlmBatchSubmitResult(
            SubmitOutcome.NOTHING_TO_DO, row.status, provider_batch_id=row.provider_batch_id
        )
    held = _held_reason(database, setup, clock())
    if held is not None:
        return _held(batch_submission_id, held)

    checked = _check_items(database, setup, batch_submission_id)
    changed_ids = [run_id for run_id, _ in checked.changed]
    if not checked.requests:
        with database.transaction() as connection:
            failed = update_batch_submission_failed(
                connection,
                batch_submission_id,
                ErrorCode.BATCH_INPUT_CHANGED,
                from_status=BatchSubmissionStatus.QUEUED,
            )
            if failed:
                for run_id, reason in checked.changed:
                    update_derivation_run_failed(connection, run_id, reason)
        if not failed:
            # 交易結束、連線還回去之後才重讀（連線池只有一條時不會等自己）。
            return _lost_race(database, batch_submission_id)
        log.warning(
            "Batch 送出前核對全部對不上，批次改 failed",
            batch_submission_id=str(batch_submission_id),
            input_changed=len(changed_ids),
        )
        return LlmBatchSubmitResult(
            SubmitOutcome.FAILED,
            BatchSubmissionStatus.FAILED,
            input_changed=len(changed_ids),
            reason=ErrorCode.BATCH_INPUT_CHANGED,
        )

    # 查核可能很久（最多重建 1,000 筆、可能跨過台北午夜）：真正送出前，在全域 Batch 預算鎖裡再看
    # 一次月初窗口與「帳本＋全部預留」，和 queued→submitting 同一個交易，中間不會有人插隊。
    # 等鎖（或交易裡任何一句等鎖）超過 `lock_timeout_ms` 就整個交易退回、當成延後（L1）。
    cancelled = False
    moved = False
    try:
        with database.transaction_with_lock_timeout(lock_timeout_ms) as connection:
            Database.transaction_lock(connection, LLM_BATCH_BUDGET_LOCK_KEY)
            held = _held_reason_locked(
                connection, setup, clock(), batch_submission_id, checked.sent_tasks
            )
            if held is ErrorCode.BATCH_BUDGET_CANNOT_FIT:
                # 再等也放不下（R2）：同一個交易取消批次、結束 run（釋放預留），不建 provider。
                cancelled = update_batch_submission_failed(
                    connection,
                    batch_submission_id,
                    ErrorCode.BATCH_BUDGET_CANNOT_FIT,
                    from_status=BatchSubmissionStatus.QUEUED,
                )
                if cancelled:
                    for run_id in checked.sent_run_ids:
                        update_derivation_run_failed(
                            connection, run_id, ErrorCode.BATCH_BUDGET_CANNOT_FIT
                        )
                    for run_id, reason in checked.changed:
                        update_derivation_run_failed(connection, run_id, reason)
            elif held is None:
                moved = update_batch_submission_submitting(
                    connection, batch_submission_id, len(checked.requests)
                )
                if moved:
                    # 確定要送才建 provider（不連線）：缺金鑰時在這裡丟、整個交易退回，
                    # 列還是 queued。
                    provider = open_provider()
                    for run_id, reason in checked.changed:
                        update_derivation_run_failed(connection, run_id, reason)
    except LockWaitTimeoutError:
        # 交易已退回：什麼都沒寫（provider 若已建，由呼叫端的 ExitStack 關），列維持 queued。
        return _held(batch_submission_id, ErrorCode.BATCH_BUDGET_LOCK_BUSY)
    if held is ErrorCode.BATCH_BUDGET_CANNOT_FIT:
        if not cancelled:
            # 交易結束、連線還回去之後才重讀（連線池只有一條時不會等自己）。
            return _lost_race(database, batch_submission_id)
        log.warning(
            "Batch 再等也放不下（熔斷線或功能預算調低），取消：批次與 run 改 failed、預留釋放，"
            "下次規劃依新預算重排",
            batch_submission_id=str(batch_submission_id),
            reason=ErrorCode.BATCH_BUDGET_CANNOT_FIT.value,
            released_runs=len(checked.sent_run_ids) + len(changed_ids),
            input_changed=len(changed_ids),
        )
        return LlmBatchSubmitResult(
            SubmitOutcome.FAILED,
            BatchSubmissionStatus.FAILED,
            input_changed=len(changed_ids),
            reason=ErrorCode.BATCH_BUDGET_CANNOT_FIT,
        )
    if held is not None:
        return _held(batch_submission_id, held)
    if not moved:
        # 交易結束、連線還回去之後才重讀（連線池只有一條時不會等自己）。
        return _lost_race(database, batch_submission_id)
    if changed_ids:
        log.warning(
            "Batch 送出前核對有 run 對不上，改 failed、不送",
            batch_submission_id=str(batch_submission_id),
            input_changed=len(changed_ids),
        )

    # 交易外送出；沒有冪等鍵，不重試。
    failure: BaseException | None = None
    try:
        provider_batch_id = send_llm_batch(actor, provider, checked.requests)
    except Exception as exc:
        failure = exc
        provider_batch_id = ""
    if failure is None and not _BATCH_ID.fullmatch(provider_batch_id):
        # adapter 已驗格式；這裡再擋一次，寫不進資料庫的 ID 照「回應看不懂」處理。
        failure = _MalformedBatchIdError()
    if failure is not None:
        return _send_failed(database, batch_submission_id, checked, failure)

    # 先記日誌（含 ID）再寫回：寫回失敗時還查得到供應商那邊的批次。
    log.info(
        "Batch 已送出",
        batch_submission_id=str(batch_submission_id),
        provider_batch_id=provider_batch_id,
        requests=len(checked.requests),
    )
    with database.transaction() as connection:
        written = update_batch_submission_submitted(
            connection, batch_submission_id, provider_batch_id
        )
    if written:
        return LlmBatchSubmitResult(
            SubmitOutcome.SUBMITTED,
            BatchSubmissionStatus.SUBMITTED,
            provider_batch_id=provider_batch_id,
            sent=len(checked.requests),
            input_changed=len(changed_ids),
        )
    # 送出太久，已被當成工作不在、標了 needs_attention：只補記 ID 給人工確認，狀態不變。
    with database.transaction() as connection:
        recorded = update_batch_submission_late_batch_id(
            connection, batch_submission_id, provider_batch_id
        )
    log.error(
        "Batch 已送出，但批次已不是 submitting（可能已標 needs_attention），要人工確認",
        batch_submission_id=str(batch_submission_id),
        provider_batch_id=provider_batch_id,
        recorded=recorded,
    )
    return LlmBatchSubmitResult(
        SubmitOutcome.NEEDS_ATTENTION,
        BatchSubmissionStatus.NEEDS_ATTENTION,
        provider_batch_id=provider_batch_id,
        sent=len(checked.requests),
        input_changed=len(changed_ids),
        reason=ErrorCode.BATCH_SUBMIT_UNCERTAIN,
    )


class _MalformedBatchIdError(Exception):
    """供應商回的 batch ID 格式不對（不放進訊息）。"""

    def __init__(self) -> None:
        super().__init__("供應商回的 batch ID 格式不對")


def _expire_submitting(
    database: Database, batch_submission_id: uuid.UUID, stale_after: timedelta
) -> LlmBatchSubmitResult:
    """停在 `submitting`、沒有 batch ID：過了時限才標 `needs_attention`；都不送。"""
    with database.transaction() as connection:
        marked = update_batch_submission_needs_attention(
            connection,
            batch_submission_id,
            ErrorCode.BATCH_SUBMIT_UNCERTAIN,
            stale_after=stale_after,
        )
    if not marked:
        log.info("Batch 還在送出中（未過時限），不動", batch_submission_id=str(batch_submission_id))
        return LlmBatchSubmitResult(SubmitOutcome.NOT_STALE, BatchSubmissionStatus.SUBMITTING)
    log.error(
        "Batch 停在 submitting 太久、沒有 batch ID，標 needs_attention（不重送，要人工確認）",
        batch_submission_id=str(batch_submission_id),
    )
    return LlmBatchSubmitResult(
        SubmitOutcome.NEEDS_ATTENTION,
        BatchSubmissionStatus.NEEDS_ATTENTION,
        reason=ErrorCode.BATCH_SUBMIT_UNCERTAIN,
    )


def _held(batch_submission_id: uuid.UUID, reason: ErrorCode) -> LlmBatchSubmitResult:
    log.info(
        "Batch 送出延後（列維持 queued，sweep 之後補排）",
        batch_submission_id=str(batch_submission_id),
        reason=reason.value,
    )
    return LlmBatchSubmitResult(SubmitOutcome.HELD, BatchSubmissionStatus.QUEUED, reason=reason)


def _held_reason(database: Database, setup: LlmBatchSetup, now: datetime) -> ErrorCode | None:
    """查核前先看一次月初窗口與帳本熔斷（規劃之後可能變了）；要延後時回原因代碼。

    只看帳本本身有沒有到熔斷線（不算預留：這批自己的預留也在裡面）；含預留的最後確認在查核之後、
    預算鎖裡（`_held_reason_locked`）。
    """
    if in_month_start_hold(now, setup.batches.month_start_hold_hours):
        return ErrorCode.BATCH_MONTH_START_HOLD
    status = get_budget_status(SystemViewer(), database, setup.budget, now=now)
    if status.ledger_breaker_tripped:
        return ErrorCode.BATCH_BUDGET_TRIPPED
    return None


def _held_reason_locked(
    connection: DbConnection,
    setup: LlmBatchSetup,
    now: datetime,
    batch_submission_id: uuid.UUID,
    tasks: frozenset[LlmTask],
) -> ErrorCode | None:
    """（全域 Batch 預算鎖裡）真正送出前的最後確認；要延後或取消時回原因代碼。

    - 月初窗口：查核途中跨過台北午夜也擋得到。
    - 帳本已到熔斷線：`batch_budget_tripped`。
    - 帳本＋全部預留（含這批自己的）**超過**熔斷線或這批工作的功能預算：
      - 再等也放不下（`domain/llm_batches.batch_never_fits`：前面的 queued 批次＋這批的預留就
        超過）：`batch_budget_cannot_fit`（呼叫端在同一個交易取消批次、釋放預留）。
      - 否則（再等可能放得下）：`batch_budget_exceeded`（延後，預留留著）。
      規劃時剛好排滿不算超過；規劃之後互動又花了（在途的呼叫）或預算調低才會超過。
    """
    if in_month_start_hold(now, setup.batches.month_start_hold_hours):
        return ErrorCode.BATCH_MONTH_START_HOLD
    viewer = SystemViewer()
    status = read_budget_status(viewer, connection, setup.budget, now=now)
    if status.ledger_breaker_tripped:
        return ErrorCode.BATCH_BUDGET_TRIPPED
    if not status.over_committed(tasks):
        return None
    own, ahead = list_batch_reservations_ahead_by_task(viewer, connection, batch_submission_id)
    if batch_never_fits(setup.budget, tasks, own, ahead):
        return ErrorCode.BATCH_BUDGET_CANNOT_FIT
    return ErrorCode.BATCH_BUDGET_EXCEEDED


def _current_status(database: Database, batch_submission_id: uuid.UUID) -> BatchSubmissionStatus:
    """條件式更新沒套用時，重讀這筆批次現在的狀態。"""
    with database.transaction() as connection:
        row = find_batch_submission(SystemViewer(), connection, batch_submission_id)
    if row is None:
        raise LookupError("找不到 batch_submission")
    return row.status


def _lost_race(database: Database, batch_submission_id: uuid.UUID) -> LlmBatchSubmitResult:
    status = _current_status(database, batch_submission_id)
    log.info(
        "Batch 已被別的送出者處理",
        batch_submission_id=str(batch_submission_id),
        status=status.value,
    )
    return LlmBatchSubmitResult(SubmitOutcome.LOST_RACE, status)


def _check_items(
    database: Database, setup: LlmBatchSetup, batch_submission_id: uuid.UUID
) -> _Checked:
    """讀批次的 item 與 run（不是 running 的跳過），每筆重讀論文、再問 `plan_for`、重建請求、
    核對 run 的鍵。"""
    viewer = SystemViewer()
    prompts: dict[LlmTask, LoadedPrompt] = {}
    requests: list[LlmBatchRequest] = []
    sent_run_ids: list[uuid.UUID] = []
    changed: list[tuple[uuid.UUID, ErrorCode]] = []
    sent_tasks: set[LlmTask] = set()
    with database.transaction() as connection:
        items = list_batch_items(viewer, connection, batch_submission_id)
        paper_ids = {item.key.paper_id for item in items if item.run_status is RunStatus.RUNNING}
        sources = {
            paper_id: find_paper_llm_source(viewer, connection, paper_id, for_update=False)
            for paper_id in paper_ids
        }
        facts = {
            paper_id: find_paper_processing_facts(viewer, connection, paper_id)
            for paper_id in paper_ids
        }
    for item in items:
        if item.run_status is not RunStatus.RUNNING:
            continue
        source = sources[item.key.paper_id]
        paper_facts = facts[item.key.paper_id]
        task = item.key.task
        # 再問一次 plan_for（ADR-0032 的交接點）：規劃後被排除、或摘要到了改用別種輸入，就不送。
        if (
            source is None
            or paper_facts is None
            or plan_for(paper_facts).for_task(ProcessingTask(task.value)).input_kind
            != item.input_kind
        ):
            changed.append((item.run_id, ErrorCode.BATCH_INPUT_CHANGED))
            continue
        if task not in prompts:
            prompts[task] = load_prompt(setup.gateway.prompts_root, task)
        job = llm_job(setup, item.key.paper_id, task, item.input_kind)
        prepared = prepare_llm_request(setup.gateway, job, source, prompts[task])
        if isinstance(prepared, GatewayResult):
            changed.append((item.run_id, prepared.reason or ErrorCode.BATCH_INPUT_CHANGED))
        elif prepared.key != item.key:
            changed.append((item.run_id, ErrorCode.BATCH_INPUT_CHANGED))
        else:
            requests.append(LlmBatchRequest(custom_id=item.custom_id, request=prepared.request))
            sent_run_ids.append(item.run_id)
            sent_tasks.add(task)
    return _Checked(
        requests=requests,
        sent_run_ids=sent_run_ids,
        changed=changed,
        sent_tasks=frozenset(sent_tasks),
    )


def _definite_reason(exc: BaseException) -> ErrorCode | None:
    """一定沒有建立批次時回原因代碼；不確定時回 None。

    一定沒建立：供應商有回 HTTP 4xx（429 由呼叫端另外處理）；送出任何請求前就丟的
    `NotCloudSafeError`（port 的約定）與沒有狀態碼的 `LlmConfigurationError`（SDK 讀不到憑證這類）。
    其他都不確定（含一般的 TypeError、ValueError：送出後才丟的也可能是這些，不能因此重送）。
    """
    if isinstance(exc, NotCloudSafeError):
        return ErrorCode.CLOUD_TEXT_NOT_REGISTERED
    if not isinstance(exc, LlmError):
        return None
    if exc.status is None:
        return ErrorCode.LLM_CONFIGURATION if isinstance(exc, LlmConfigurationError) else None
    if not 400 <= exc.status < 500:
        return None
    if exc.status == 429:
        return ErrorCode.LLM_RATE_LIMITED
    if isinstance(exc, LlmConfigurationError):
        return ErrorCode.LLM_CONFIGURATION
    return ErrorCode.BATCH_SUBMIT_REJECTED


def _send_failed(
    database: Database,
    batch_submission_id: uuid.UUID,
    checked: _Checked,
    failure: BaseException,
) -> LlmBatchSubmitResult:
    """送出出錯：照「一定沒建立」與「不確定」分類處理（見模組說明）。

    條件式更新沒套用時（別人已改了這筆批次）重讀狀態，記在日誌與回傳值裡。
    """
    error_type = type(failure).__name__
    status = failure.status if isinstance(failure, LlmError) else None
    request_id = failure.request_id if isinstance(failure, LlmError) else None
    reason = _definite_reason(failure)
    if reason is ErrorCode.LLM_RATE_LIMITED:
        with database.transaction() as connection:
            changed = update_batch_submission_requeued(connection, batch_submission_id)
        if not changed:
            actual = _current_status(database, batch_submission_id)
            log.warning(
                "Batch 送出被限流（429），但批次已不是 submitting，沒改",
                batch_submission_id=str(batch_submission_id),
                changed=False,
                status=actual.value,
            )
            return LlmBatchSubmitResult(
                SubmitOutcome.LOST_RACE, actual, reason=ErrorCode.LLM_RATE_LIMITED
            )
        log.warning(
            "Batch 送出被限流（429），退回 queued（sweep 之後補排）",
            batch_submission_id=str(batch_submission_id),
            changed=True,
        )
        return LlmBatchSubmitResult(
            SubmitOutcome.REQUEUED,
            BatchSubmissionStatus.QUEUED,
            reason=ErrorCode.LLM_RATE_LIMITED,
        )
    if reason is not None:
        with database.transaction() as connection:
            changed = update_batch_submission_failed(
                connection,
                batch_submission_id,
                reason,
                from_status=BatchSubmissionStatus.SUBMITTING,
            )
            if changed:
                for run_id in checked.sent_run_ids:
                    update_derivation_run_failed(connection, run_id, reason)
        actual = (
            BatchSubmissionStatus.FAILED
            if changed
            else _current_status(database, batch_submission_id)
        )
        log.error(
            "Batch 一定沒有建立，批次與 run 改 failed"
            if changed
            else "Batch 一定沒有建立，但批次已不是 submitting，沒改",
            batch_submission_id=str(batch_submission_id),
            changed=changed,
            batch_status=actual.value,
            status=status,
            reason=reason.value,
            error_type=error_type,
            request_id=request_id,
        )
        raise failure
    response_id = failure.response_id if isinstance(failure, LlmMalformedResponseError) else None
    provider_batch_id = (
        response_id if response_id is not None and _BATCH_ID.fullmatch(response_id) else None
    )
    marked: bool | None = None
    recorded = False
    try:
        with database.transaction() as connection:
            marked = update_batch_submission_needs_attention(
                connection, batch_submission_id, ErrorCode.BATCH_SUBMIT_UNCERTAIN, stale_after=None
            )
            if provider_batch_id is not None:
                # 回應看不懂但帶了像樣的 batch ID：記下來給人工確認時找供應商那邊的批次。
                recorded = update_batch_submission_late_batch_id(
                    connection, batch_submission_id, provider_batch_id
                )
    except Exception as cleanup:
        # 標不上就停在 submitting，過了時限由 sweep 補排時標。
        log.warning(
            "Batch 送出結果不確定，標 needs_attention 也失敗",
            batch_submission_id=str(batch_submission_id),
            error_type=type(cleanup).__name__,
        )
    actual_text = None
    if marked is False:
        actual_text = _current_status(database, batch_submission_id).value
    log.error(
        "Batch 送出結果不確定，標 needs_attention（不重送，要人工確認）"
        if marked
        else "Batch 送出結果不確定，沒標上 needs_attention（不重送，要人工確認）",
        batch_submission_id=str(batch_submission_id),
        changed=bool(marked),
        batch_status=BatchSubmissionStatus.NEEDS_ATTENTION.value if marked else actual_text,
        error_type=error_type,
        status=status,
        request_id=request_id,
        provider_batch_id=provider_batch_id,
        provider_batch_id_recorded=recorded,
    )
    raise BatchSubmitUncertainError(batch_submission_id, error_type)


# --- 查詢（CLI 用） ------------------------------------------------------------------------


def list_llm_batches(
    actor: Viewer,
    database: Database,
    statuses: Collection[BatchSubmissionStatus],
    ids: Collection[uuid.UUID] | None = None,
) -> list[BatchSubmission]:
    """狀態在 `statuses` 裡的批次（有給 `ids` 時只看這幾筆），依建立順序。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    with database.transaction() as connection:
        return list_batch_submissions(SystemViewer(), connection, statuses, ids)


def count_llm_batch_running_runs(
    actor: Viewer, database: Database, batch_submission_id: uuid.UUID
) -> int:
    """這批還是 `running` 的 run 有幾筆（`radar batches resolve --fail` 確認前印）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    with database.transaction() as connection:
        return count_batch_running_runs(SystemViewer(), connection, batch_submission_id)


# --- 結果匯入 -------------------------------------------------------------------------------


def poll_llm_batches(
    actor: Viewer,
    database: Database,
    open_provider: Callable[[], LlmProvider],
    setup: LlmBatchSetup,
    *,
    clock: Callable[[], datetime],
    ids: Collection[uuid.UUID] | None = None,
) -> LlmBatchPollResult:
    """查 `submitted`／`ended` 的批次（有給 `ids` 時只看這幾筆）、匯入已結束的（見模組說明「結果
    匯入」）。`open_provider` 只在有要查的批次時呼叫一次。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: 時鐘回了沒有時區的時間。
        Exception: `open_provider` 丟的錯（例如缺金鑰的 `ConfigError`）、列批次與最後計數時的資料庫
            錯誤原樣丟出。單一批次裡的非預期錯誤（例如 run 的 prompt 版本讀不出來的
            `PromptIntegrityError`／`PromptFormatError`）不丟：記 log（只有錯誤類別）、這批的列標
            `error`、繼續查下一批。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    viewer = SystemViewer()
    with database.transaction() as connection:
        rows = list_batch_submissions(
            viewer,
            connection,
            (BatchSubmissionStatus.SUBMITTED, BatchSubmissionStatus.ENDED),
            ids,
        )
    result = LlmBatchPollResult()
    if rows:
        provider = open_provider()
        for row in rows:
            try:
                polled = _poll_one(actor, database, provider, setup, row, clock)
            except Exception as exc:  # 一批出錯不擋後面的批次與 ai_done、needs_attention 的計數
                log.error(
                    "Batch 匯入時出了非預期的錯，這批先停在原狀態",
                    batch_submission_id=str(row.id),
                    error_type=type(exc).__name__,
                )
                polled = BatchPollRow(
                    row.id, _current_status(database, row.id), error=type(exc).__name__
                )
            result.rows.append(polled)
    result.ai_done = mark_weekly_runs_ai_done(actor, database, now=_aware(clock()))
    with database.transaction() as connection:
        result.needs_attention = count_batch_submissions(
            viewer, connection, BatchSubmissionStatus.NEEDS_ATTENTION
        )
    log.info(
        "Batch 結果查詢完成",
        batches=len(result.rows),
        errors=result.errors,
        needs_attention=result.needs_attention,
        ai_done=len(result.ai_done),
    )
    if result.needs_attention:
        log.error(
            "有 Batch 卡在 needs_attention，要人工確認", needs_attention=result.needs_attention
        )
    return result


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("時鐘要回帶時區的時間")
    return now


def _poll_one(
    actor: Viewer,
    database: Database,
    provider: LlmProvider,
    setup: LlmBatchSetup,
    row: BatchSubmission,
    clock: Callable[[], datetime],
) -> BatchPollRow:
    """查一批、結束了就匯入。供應商的錯誤記在回傳的列上，不丟。"""
    out = BatchPollRow(row.id, row.status)
    provider_batch_id = row.provider_batch_id
    if provider_batch_id is None:  # submitted 以後一定有（資料庫的 CHECK 守著）
        raise RuntimeError("submitted／ended 的批次沒有供應商 batch ID")
    try:
        status = fetch_llm_batch(actor, provider, provider_batch_id)
    except LlmConfigurationError as exc:
        if exc.status == 404 and _mark_not_found(database, row.id):
            out.status = BatchSubmissionStatus.NEEDS_ATTENTION
            return out
        out.error = _error_text(exc)
        _log_provider_error(row.id, "查狀態", exc)
        return out
    except LlmError as exc:
        out.error = _error_text(exc)
        _log_provider_error(row.id, "查狀態", exc)
        return out
    out.provider_status = status.processing_status
    now = _aware(clock())
    if status.processing_status is not BatchProcessingStatus.ENDED:
        out.overdue = batch_overdue(status, now)
        if out.overdue:
            log.warning(
                "Batch 已過供應商的處理期限還沒結束",
                batch_submission_id=str(row.id),
                provider_status=status.processing_status.value,
            )
        return out

    with database.transaction() as connection:
        update_batch_submission_ended(connection, row.id)
    if results_expired(status, now) or not status.results_available:
        with database.transaction() as connection:
            out.counts.unavailable = update_derivation_runs_failed_for_batch(
                connection, row.id, ErrorCode.BATCH_RESULTS_UNAVAILABLE
            )
        log.warning(
            "Batch 的結果已經拿不到",
            batch_submission_id=str(row.id),
            released_runs=out.counts.unavailable,
        )
    else:
        try:
            _import_results(actor, database, provider, setup, row.id, provider_batch_id, out.counts)
        except LlmError as exc:
            # 下載途中斷線：批次留 ended，下一次接著做（匯入過的不會重記帳）。
            out.error = _error_text(exc)
            _log_provider_error(row.id, "下載結果", exc)
            out.status = _current_status(database, row.id)
            return out
        with database.transaction() as connection:
            out.counts.missing = update_derivation_runs_failed_for_batch(
                connection, row.id, ErrorCode.BATCH_RESULT_MISSING
            )
        if out.counts.missing:
            log.warning(
                "Batch 結果少了幾項",
                batch_submission_id=str(row.id),
                missing=out.counts.missing,
            )
    with database.transaction() as connection:
        update_batch_submission_imported(connection, row.id)
    out.status = _current_status(database, row.id)
    counts = out.counts
    log.info(
        "Batch 結果匯入完成",
        batch_submission_id=str(row.id),
        status=out.status.value,
        **{outcome.value: counts.count(outcome) for outcome in BatchItemOutcome},
        unknown_custom_ids=counts.unknown_custom_ids,
        unreadable_lines=counts.unreadable_lines,
        missing=counts.missing,
        unavailable=counts.unavailable,
    )
    return out


def _import_results(
    actor: Viewer,
    database: Database,
    provider: LlmProvider,
    setup: LlmBatchSetup,
    batch_submission_id: uuid.UUID,
    provider_batch_id: str,
    counts: BatchImportCounts,
) -> None:
    """串流這批的結果，逐項匯入。不認得的 custom_id 與讀不到 custom_id 的行只計數。

    Raises:
        LlmError: 下載前或下載途中供應商的錯誤。
    """
    with database.transaction() as connection:
        items = list_batch_items(SystemViewer(), connection, batch_submission_id)
    by_custom_id: dict[str, BatchItemRun] = {item.custom_id: item for item in items}
    results = iter_llm_batch_results(actor, provider, provider_batch_id)
    try:
        for line in results:
            if line.custom_id is None:
                counts.unreadable_lines += 1
                continue
            item = by_custom_id.get(line.custom_id)
            if item is None:
                counts.unknown_custom_ids += 1
                continue
            job = llm_job(setup, item.key.paper_id, item.key.task, item.input_kind)
            counts.add(import_llm_batch_result(actor, database, setup.gateway, job, item, line))
    finally:
        close = getattr(results, "close", None)
        if callable(close):
            close()
    if counts.unknown_custom_ids or counts.unreadable_lines:
        log.warning(
            "Batch 結果裡有對不上的行",
            batch_submission_id=str(batch_submission_id),
            unknown_custom_ids=counts.unknown_custom_ids,
            unreadable_lines=counts.unreadable_lines,
        )


def _mark_not_found(database: Database, batch_submission_id: uuid.UUID) -> bool:
    """供應商找不到這批：`submitted`／`ended` → `needs_attention`。已經不是這兩種狀態（例如別的
    poll 剛匯入完）時不改、回 False（當成一般錯誤）。"""
    with database.transaction() as connection:
        changed = update_batch_submission_attention_on_not_found(
            connection, batch_submission_id, ErrorCode.BATCH_PROVIDER_NOT_FOUND
        )
    if changed:
        log.error(
            "供應商找不到這個 Batch，標 needs_attention（要人工確認）",
            batch_submission_id=str(batch_submission_id),
        )
    return changed


def _error_text(exc: LlmError) -> str:
    """錯誤只顯示類別與狀態碼（不帶訊息）。"""
    name = type(exc).__name__
    return name if exc.status is None else f"{name}（HTTP {exc.status}）"


def _log_provider_error(batch_submission_id: uuid.UUID, step: str, exc: LlmError) -> None:
    log.error(
        f"Batch {step}時供應商出錯，這批先不動",
        batch_submission_id=str(batch_submission_id),
        error_type=type(exc).__name__,
        status=exc.status,
        request_id=exc.request_id,
    )


# --- 人工確認 -------------------------------------------------------------------------------


def resolve_llm_batch(
    actor: Viewer,
    database: Database,
    batch_submission_id: uuid.UUID,
    *,
    provider_batch_id: str | None = None,
    fail: bool = False,
) -> BatchResolveResult:
    """人工確認一個 `needs_attention` 的批次（見模組說明「人工確認」）。不呼叫供應商。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: `provider_batch_id` 與 `fail` 沒有剛好給一個，或 batch ID 格式不對（不碰
            資料庫）。
        LookupError: 沒有這個 `batch_submission`。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    if (provider_batch_id is None) == (not fail):
        raise ValueError("provider_batch_id 與 fail 要剛好給一個")
    if provider_batch_id is not None and not _BATCH_ID.fullmatch(provider_batch_id):
        raise ValueError("供應商 batch ID 的格式不對")
    status = _current_status(database, batch_submission_id)
    if status is not BatchSubmissionStatus.NEEDS_ATTENTION:
        return BatchResolveResult(ResolveOutcome.NOT_NEEDS_ATTENTION, status)
    if provider_batch_id is not None:
        with database.transaction() as connection:
            resumed = update_batch_submission_resumed(
                connection, batch_submission_id, provider_batch_id
            )
        if resumed:
            log.info(
                "Batch 人工確認：繼續等",
                batch_submission_id=str(batch_submission_id),
                provider_batch_id=provider_batch_id,
            )
            return BatchResolveResult(ResolveOutcome.RESUMED, BatchSubmissionStatus.SUBMITTED)
        status = _current_status(database, batch_submission_id)
        if status is BatchSubmissionStatus.NEEDS_ATTENTION:
            return BatchResolveResult(ResolveOutcome.ID_IN_USE, status)
        return BatchResolveResult(ResolveOutcome.NOT_NEEDS_ATTENTION, status)
    released = 0
    with database.transaction() as connection:
        failed = update_batch_submission_resolved_failed(connection, batch_submission_id)
        if failed:
            released = update_derivation_runs_failed_for_batch(
                connection, batch_submission_id, ErrorCode.BATCH_RESOLVED_FAILED
            )
    if not failed:
        return BatchResolveResult(
            ResolveOutcome.NOT_NEEDS_ATTENTION, _current_status(database, batch_submission_id)
        )
    log.info(
        "Batch 人工確認：標失敗",
        batch_submission_id=str(batch_submission_id),
        released_runs=released,
    )
    return BatchResolveResult(ResolveOutcome.FAILED, BatchSubmissionStatus.FAILED, released)


def _id_text(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


# --- 改版的重跑預估與上限（Story 2.12、ADR-0044） ---------------------------------------------

# 輸入指紋含設定檔版本的工作（分類表、情境值清單改版會讓它們整批重跑）。
REVISION_TASKS: Final = (LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL)


def estimate_revision_reruns(
    actor: Viewer,
    database: Database,
    setup: LlmBatchSetup,
    *,
    tasks: Collection[LlmTask],
    assume_changed: bool,
) -> RevisionEstimate:
    """改分類表（`classify`）或情境值清單（`extract_model`）會重跑幾篇、Batch 價保守上限多少。
    不寫任何列、不建 provider。

    照規劃同樣的候選（`_candidates`：`plan_for`、放行、指紋、大小檢查），每 200 筆開一個只讀交易查
    那幾篇 `active` 的 run 用的設定檔版本（`find_active_config_versions`）：

    - `assume_changed=True`（`radar runs estimate-revision`）：有 `active` 的都算「會重跑」，沒有的
      算「還沒有結果」。
    - `assume_changed=False`（Batch 規劃的上限檢查）：只算 `active` 的版本和現在的設定檔不同的
      （設定檔已經改了、下一次規劃就會重跑的）。第一次全庫沒有任何 `active`，上限不影響規劃。

    金額是每筆 `max_call_cost(..., is_batch=True)` 的合計；上限是 `revision_rerun_max_usd`。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: `tasks` 是空的或有分類、研究模型以外的工作（不碰資料庫）。
        PromptIntegrityError, PromptFormatError: prompt 檔或登記不對。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    wanted = {LlmTask(task) for task in tasks}
    if not wanted or wanted - set(REVISION_TASKS):
        raise ValueError("tasks 要是 classify、extract_model 裡的至少一個")
    ordered = tuple(task for task in REVISION_TASKS if task in wanted)
    estimate = RevisionEstimate(ordered, setup.batches.revision_rerun_max_usd)
    for task in ordered:
        estimate.for_task(task)
    current = {
        LlmTask.CLASSIFY: setup.taxonomy.version_label,
        LlmTask.EXTRACT_MODEL: setup.context_values.version_label,
    }
    prompts = {task: load_prompt(setup.gateway.prompts_root, task) for task in BATCH_TASKS}
    chunk: list[PreparedLlmRequest] = []
    for prepared in _candidates(database, setup, prompts, LlmBatchPlanResult()):
        if prepared.key.task not in wanted:
            continue
        chunk.append(prepared)
        if len(chunk) >= PAGE_SIZE:
            _estimate_revision_chunk(database, setup, chunk, estimate, current, assume_changed)
            chunk = []
    if chunk:
        _estimate_revision_chunk(database, setup, chunk, estimate, current, assume_changed)
    return estimate


def _estimate_revision_chunk(
    database: Database,
    setup: LlmBatchSetup,
    chunk: list[PreparedLlmRequest],
    estimate: RevisionEstimate,
    current: dict[LlmTask, str],
    assume_changed: bool,
) -> None:
    """（只讀交易）這幾筆的論文有沒有 `active`、用哪一版設定檔，計進預估。

    `assume_changed=False`（規劃的上限檢查）時，規劃不會排的不算：新鍵已經有 running、有擋下的
    結果（例如改版後那一次被拒收，舊版本的 active 會一直留著）或可接續的 `llm_result_pending`
    （順序和 `_estimate_chunk` 一樣）。不然累積的拒收會讓每次規劃都卡在上限、沒有出路。
    """
    viewer = SystemViewer()
    versions: dict[LlmTask, dict[uuid.UUID, str | None]] = {}
    will_skip: set[int] = set()
    with database.transaction() as connection:
        for task in {prepared.key.task for prepared in chunk}:
            paper_ids = [prepared.key.paper_id for prepared in chunk if prepared.key.task is task]
            versions[task] = find_active_config_versions(viewer, connection, task, paper_ids)
        if not assume_changed:
            for index, prepared in enumerate(chunk):
                key = prepared.key
                if (
                    find_running_derivation_run_id(viewer, connection, key, None) is not None
                    or find_blocking_ai_result(viewer, connection, key) is not None
                    or find_resumable_derivation_run_id(viewer, connection, key, None) is not None
                ):
                    will_skip.add(index)
    for index, prepared in enumerate(chunk):
        if index in will_skip:
            continue
        key = prepared.key
        found = estimate.for_task(key.task)
        active = versions[key.task]
        if key.paper_id not in active:
            found.no_result += 1
            continue
        if not assume_changed and active[key.paper_id] == current[key.task]:
            continue
        found.rerun += 1
        found.max_cost_usd += max_call_cost(
            setup.gateway.models,
            key.model,
            prepared.estimated_input_tokens,
            prepared.request.max_tokens,
            is_batch=True,
        )
