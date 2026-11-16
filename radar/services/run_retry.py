"""管理者確認後重跑被拒收的 AI 結果（`radar runs retry`，Story 2.12、AD-8、ADR-0015、ADR-0044）。

被拒收（`rejected`）是終點狀態：平常流程（互動、試跑、Batch 規劃）同一組鍵遇到它一律 `SKIPPED`。
唯一能讓它重跑的路就是這裡：

- `plan_retry`：每個工作照實跑的前幾步算出這次會送的請求（`plan_for` → `prepare_llm_request`，
  和試跑、Batch 同一套），再在只讀交易裡查目前這組鍵：有 `active`、正在跑、或最新一列平常流程表頭
  不是 `rejected` 都不能重跑（記原因）；可以重跑的記下確認時的 `RunKey`、那一列 rejected 的 id
  與原因、單次上限（互動價，`max_call_cost`）。最壞花費＝單次上限 × adapter 的 `attempts`。
  不寫任何列、不呼叫模型。
- `run_retry`：只跑計畫裡可以重跑的項目：每項經閘門 `run_llm_job` 互動呼叫一次，帶
  `confirmed_key`（確認後輸入、prompt 或模型變了 → `input_changed`，不呼叫）、`retry_of`（閘門鎖內
  再核對「這組鍵最新的平常流程表頭還是那一列 rejected」，兩個重跑同時跑、或同一個確認過的 id 重跑完
  再送，都只會呼叫一次）與 `BudgetGuard`。結果照常寫（`active` 或再一次 `rejected`）。
  `run_llm_job` 的 `retry_of` 只准在這個模組傳（`tests/arch/test_retry_wiring.py`）。

例外：`LlmConfigurationError` → 這一項 `error`（`llm_configuration`＋狀態碼），其餘不跑；論文不見了
→ `not_found`；其他例外 → `error`（只記類別名）。`KeyboardInterrupt` 不攔（閘門已經把 run 收成
`failed`；有存下輸出的，下一次重跑接續、不重付）。

日誌只記 id、工作、狀態、原因代碼與錯誤類別，不記論文內容與模型輸出。
"""

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime

import structlog

from radar.domain.admin_runs import (
    ADMIN_TASKS,
    RetryBlock,
    RetryItem,
    RetryItemStatus,
    RetryPlan,
    RetryPlanItem,
    RetryReport,
)
from radar.domain.ai_outputs import LlmTask, ResultStatus
from radar.domain.errors import ErrorCode
from radar.domain.llm import LlmConfigurationError
from radar.domain.permissions import Action, require
from radar.domain.pricing import max_call_cost
from radar.domain.processing import ProcessingTask, plan_for
from radar.domain.viewer import SystemViewer, Viewer
from radar.ports import LlmProvider
from radar.repo.ai_outputs import find_blocking_ai_result, find_latest_ai_result
from radar.repo.db import Database
from radar.repo.llm_ledger import find_running_derivation_run_id
from radar.repo.papers import find_paper_llm_source, find_paper_processing_facts
from radar.services.budget import BudgetGuard
from radar.services.llm_batches import llm_job
from radar.services.llm_gateway import (
    ConfirmedRequestChangedError,
    GatewayResult,
    PreparedLlmRequest,
    prepare_llm_request,
    run_llm_job,
)
from radar.services.prompt_loader import load_prompt
from radar.services.trial_runs import TrialSetup

log = structlog.get_logger(__name__)


def _check_tasks(tasks: Sequence[LlmTask]) -> tuple[LlmTask, ...]:
    wanted = {LlmTask(task) for task in tasks}
    unknown = wanted - set(ADMIN_TASKS)
    if unknown or not wanted:
        raise ValueError("tasks 要是 zh_summary、classify、extract_model 裡的至少一個")
    return tuple(task for task in ADMIN_TASKS if task in wanted)


def plan_retry(
    actor: Viewer,
    database: Database,
    setup: TrialSetup,
    paper_id: uuid.UUID,
    tasks: Sequence[LlmTask],
    *,
    attempts: int,
) -> RetryPlan:
    """這篇論文這幾個工作能不能重跑、會送什麼、最壞花多少（見模組說明）。不寫任何列。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: `tasks` 是空的或有不是三個 AI 工作的，或 `attempts` 不是至少 1 的整數（不碰
            資料庫）。
        PromptIntegrityError, PromptFormatError: prompt 檔或登記不對。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    wanted = _check_tasks(tasks)
    if type(attempts) is not int or attempts < 1:
        raise ValueError("attempts 要是至少 1 的整數")
    viewer = SystemViewer()
    with database.transaction() as connection:
        facts = find_paper_processing_facts(viewer, connection, paper_id)
        source = find_paper_llm_source(viewer, connection, paper_id, for_update=False)
    if facts is None or source is None:
        items = tuple(RetryPlanItem(task, block=RetryBlock.NOT_FOUND) for task in wanted)
        return RetryPlan(paper_id, items, attempts)
    plan = plan_for(facts)
    items_list: list[RetryPlanItem] = []
    for task in wanted:
        task_plan = plan.for_task(ProcessingTask(task.value))
        if task_plan.input_kind is None:
            # SkipState 的兩個值（not_applicable、no_input）和 RetryBlock 的同名。
            skip = task_plan.skip
            block = RetryBlock.NOT_APPLICABLE if skip is None else RetryBlock(skip.value)
            items_list.append(RetryPlanItem(task, block=block))
            continue
        job = llm_job(setup, paper_id, task, task_plan.input_kind)
        prompt = load_prompt(setup.gateway.prompts_root, task)
        prepared = prepare_llm_request(setup.gateway, job, source, prompt)
        if isinstance(prepared, GatewayResult):
            items_list.append(_blocked_item(task, prepared))
            continue
        items_list.append(_check_key(database, setup, prepared))
    return RetryPlan(paper_id, tuple(items_list), attempts)


def _blocked_item(task: LlmTask, blocked: GatewayResult) -> RetryPlanItem:
    block = {
        "no_input": RetryBlock.NO_INPUT,
        "blocked": RetryBlock.BLOCKED,
        "too_large": RetryBlock.TOO_LARGE,
    }[blocked.outcome.value]
    return RetryPlanItem(
        task, block=block, detail=None if blocked.reason is None else blocked.reason.value
    )


def _check_key(
    database: Database, setup: TrialSetup, prepared: PreparedLlmRequest
) -> RetryPlanItem:
    """（只讀交易）目前這組鍵能不能重跑：沒有 running、沒有 active、最新一列平常流程表頭是
    rejected。"""
    viewer = SystemViewer()
    key = prepared.key
    with database.transaction() as connection:
        running = find_running_derivation_run_id(viewer, connection, key, None)
        blocking = find_blocking_ai_result(viewer, connection, key)
        latest = find_latest_ai_result(viewer, connection, key)
    task = key.task
    if running is not None:
        return RetryPlanItem(task, block=RetryBlock.RUNNING)
    if blocking is ResultStatus.ACTIVE:
        return RetryPlanItem(task, block=RetryBlock.HAS_ACTIVE)
    if latest is None or latest.status is not ResultStatus.REJECTED:
        detail = None if latest is None else latest.status.value
        return RetryPlanItem(task, block=RetryBlock.NO_REJECTED, detail=detail)
    return RetryPlanItem(
        task,
        key=key,
        input_kind=prepared.llm_input.input_kind,
        rejected_id=latest.id,
        reject_reason=latest.reject_reason,
        max_cost_usd=max_call_cost(
            setup.gateway.models,
            key.model,
            prepared.estimated_input_tokens,
            prepared.request.max_tokens,
            is_batch=False,
        ),
    )


def run_retry(
    actor: Viewer,
    database: Database,
    provider: LlmProvider,
    setup: TrialSetup,
    plan: RetryPlan,
    *,
    clock: Callable[[], datetime],
) -> RetryReport:
    """只跑 `plan` 裡可以重跑的項目（見模組說明），回每項的結果。`clock` 給預算檢查算台北月份。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        KeyboardInterrupt: 原樣往上丟（閘門已經把 run 收好）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    items: list[RetryItem] = []
    stopped = False
    for planned in plan.retryable:
        if stopped:
            break
        item = _retry_one(actor, database, provider, setup, plan.paper_id, planned, clock)
        stopped = item.reason == ErrorCode.LLM_CONFIGURATION.value
        items.append(item)
        log.info(
            "重跑被拒收的 AI 結果",
            paper_id=str(plan.paper_id),
            task=item.task.value,
            status=item.status.value,
            reason=item.reason,
            http_status=item.http_status,
            run_id=None if item.run_id is None else str(item.run_id),
        )
    return RetryReport(plan.paper_id, tuple(items))


def _retry_one(
    actor: Viewer,
    database: Database,
    provider: LlmProvider,
    setup: TrialSetup,
    paper_id: uuid.UUID,
    planned: RetryPlanItem,
    clock: Callable[[], datetime],
) -> RetryItem:
    task = planned.task
    # retryable 保證有鍵與輸入種類。
    assert planned.key is not None
    assert planned.input_kind is not None
    try:
        result = run_llm_job(
            actor,
            database,
            provider,
            setup.gateway,
            llm_job(setup, paper_id, task, planned.input_kind),
            budget_allows=BudgetGuard(database, setup.budget, clock),
            confirmed_key=planned.key,
            retry_of=planned.rejected_id,
        )
    except ConfirmedRequestChangedError:
        log.warning("確認後輸入變了，這一項沒有重跑", paper_id=str(paper_id), task=task.value)
        return RetryItem(task, RetryItemStatus.INPUT_CHANGED)
    except LlmConfigurationError as exc:
        log.error("金鑰或設定錯誤，重跑停下", task=task.value, http_status=exc.status)
        return RetryItem(
            task,
            RetryItemStatus.ERROR,
            reason=ErrorCode.LLM_CONFIGURATION.value,
            http_status=exc.status,
        )
    except Exception as exc:
        if type(exc) is LookupError:  # 閘門找不到論文時丟的就是 LookupError 本身
            return RetryItem(task, RetryItemStatus.NOT_FOUND)
        log.error("重跑的項目出錯", task=task.value, error_type=type(exc).__name__)
        return RetryItem(task, RetryItemStatus.ERROR, reason=type(exc).__name__)
    return RetryItem(
        task,
        RetryItemStatus(result.outcome.value),
        reason=None if result.reason is None else result.reason.value,
        run_id=result.run_id,
        cost_usd=result.cost_usd,
    )
