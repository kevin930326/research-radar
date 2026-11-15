"""Batch 的規劃與送出（AD-8、AD-14、ADR-0004 決定第 2、4 點、ADR-0031 決定 15、ADR-0041、
Story 2.9）。`batch_submission` 的狀態只在這裡改。

規劃（`plan_llm_batches`，每週更新發佈快照後排入）：

0. 設定 `weekly_planning_enabled` 是 false：不排、不寫任何列（Story 2.11 的手動試跑以前）。
1. 台北時間每月 1 日 00:00～`month_start_hold_hours` 點以前：整次延後，不寫任何列、不算失敗；
   每一批開始前再看一次，途中跨進窗口就停（已 commit 的批次留著）。
2. 已熔斷：整次不排。
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
  一個交易裡先拿全域 Batch 預算鎖，再看一次月初窗口（查核可能跨過午夜）與「帳本＋全部預留」
  （超過熔斷線或這批工作的功能預算就 `HELD`／`batch_budget_exceeded`，不寫任何列），都過了才
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

日誌與錯誤只帶 id、計數、原因代碼與錯誤類別，不帶金鑰與文件內容。
"""

import re
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

import structlog

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
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import LLM_BATCH_BUDGET_LOCK_KEY
from radar.domain.jobs import SUBMIT_LLM_BATCH, Enqueue
from radar.domain.llm import (
    LlmBatchRequest,
    LlmConfigurationError,
    LlmError,
    LlmMalformedResponseError,
    NotCloudSafeError,
)
from radar.domain.llm_batches import (
    BATCH_TASKS,
    PROVIDER_BATCH_ID_PATTERN,
    SUBMITTING_STALE_AFTER,
    BatchSubmissionStatus,
    BatchSubmitUncertainError,
    LlmBatchConfig,
    LlmBatchPlanResult,
    LlmBatchSubmitResult,
    PlanStop,
    SubmitOutcome,
    in_month_start_hold,
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
from radar.repo.db import Database, DbConnection
from radar.repo.llm_batches import (
    create_batch_item,
    create_batch_submission,
    find_batch_submission,
    list_batch_items,
    update_batch_submission_failed,
    update_batch_submission_late_batch_id,
    update_batch_submission_needs_attention,
    update_batch_submission_requeued,
    update_batch_submission_submitted,
    update_batch_submission_submitting,
    update_batch_submission_totals,
)
from radar.repo.llm_ledger import update_derivation_run_failed
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
    prepare_llm_request,
    send_llm_batch,
)
from radar.services.prompt_loader import LoadedPrompt, load_prompt

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


def llm_job(
    setup: LlmBatchSetup, paper_id: uuid.UUID, task: LlmTask, input_kind: InputKind
) -> LlmJob:
    """三個 Batch 工作的 `LlmJob`（和 2.4～2.6 的 service 一樣的語言、驗證與設定版本）。

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
) -> LlmBatchPlanResult:
    """把 `plan_for` 要做、`should_run` 說要跑的 AI 工作排成 Batch（見模組說明）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: 時鐘回了沒有時區的時間。
        PromptIntegrityError, PromptFormatError: prompt 檔或登記不對（還沒寫任何列）。
        Exception: 資料庫錯誤原樣丟出（進行中那一批的交易退回；已 commit 的批次留著）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    result = LlmBatchPlanResult()
    if not setup.batches.weekly_planning_enabled:
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
) -> LlmBatchSubmitResult:
    """送出一個 `queued` 的批次（見模組說明）。

    `open_provider` 只在確定要送出時呼叫一次（查核與最後確認都過了、條件式 `queued`→`submitting`
    套用之後、同一個交易 commit 之前；建 provider 不連線）：過期的 `submitting` 標
    `needs_attention`、已結束、延後、全部對不上、搶輸這些只碰資料庫的路都不建 provider，所以缺金鑰時
    也照樣做得完。它丟的錯（例如缺金鑰的 `ConfigError`）原樣往上丟、交易退回，列維持 `queued`。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        LookupError: 沒有這個 `batch_submission`。
        ValueError: `stale_after` 不是正的時間長度，或時鐘回了沒有時區的時間。
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
    with database.transaction() as connection:
        Database.transaction_lock(connection, LLM_BATCH_BUDGET_LOCK_KEY)
        held = _held_reason_locked(connection, setup, clock(), checked.sent_tasks)
        moved = False
        if held is None:
            moved = update_batch_submission_submitting(
                connection, batch_submission_id, len(checked.requests)
            )
            if moved:
                # 確定要送才建 provider（不連線）：缺金鑰時在這裡丟、整個交易退回，列還是 queued。
                provider = open_provider()
                for run_id, reason in checked.changed:
                    update_derivation_run_failed(connection, run_id, reason)
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
    connection: DbConnection, setup: LlmBatchSetup, now: datetime, tasks: frozenset[LlmTask]
) -> ErrorCode | None:
    """（全域 Batch 預算鎖裡）真正送出前的最後確認；要延後時回原因代碼。

    - 月初窗口：查核途中跨過台北午夜也擋得到。
    - 帳本已到熔斷線：`batch_budget_tripped`。
    - 帳本＋全部預留（含這批自己的）**超過**熔斷線或這批工作的功能預算：`batch_budget_exceeded`。
      規劃時剛好排滿不算超過；規劃之後互動又花了（在途的呼叫）或預算調低才會超過。
    """
    if in_month_start_hold(now, setup.batches.month_start_hold_hours):
        return ErrorCode.BATCH_MONTH_START_HOLD
    status = read_budget_status(SystemViewer(), connection, setup.budget, now=now)
    if status.ledger_breaker_tripped:
        return ErrorCode.BATCH_BUDGET_TRIPPED
    if status.over_committed(tasks):
        return ErrorCode.BATCH_BUDGET_EXCEEDED
    return None


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


def _id_text(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)
