"""只對指定的論文跑 AI（`radar runs trial`，Story 2.10、AR-62、ADR-0042）。

全庫跑之前、改提示詞之後，先拿少數幾篇看結果：

- `resolve_trial_papers`：指定的論文 ID／DOI → 論文（照參數順序、去重）；有任何一個找不到就丟
  `TrialPapersNotFoundError`，一篇都不跑。
- `sample_trial_papers`：從調整組、三個工作都要做的論文裡，用固定 seed 抽 N 篇（排序規則在
  `domain/trial_runs.sample_tuning_papers`）；seed 與抽到的論文（ID＋DOI）寫進 info 日誌。
- `estimate_trial`：執行前的保守費用上限。每項單次上限是 `max_call_cost(估計輸入, max_tokens,
  互動)`，最壞花費再乘 adapter 每個請求最多送幾次（`attempts`，呼叫端從 adapter 讀，含第一次；
  逾時放棄的請求也可能計費、不進帳本）。已有結果、會被略過的也算。每項記下確認時的 `RunKey`
  （指紋與實跑一樣，`services/llm_batches.llm_job`）。不碰帳本、不寫列。
- `run_trial`：只跑確認過的項目（`estimate` 裡有鍵的）：逐篇逐工作呼叫既有的
  `run_zh_summary`／`run_classification`／`run_research_model`，帶 `confirmed_key`（互動呼叫、
  同一個閘門：照常問 `plan_for` 與 `should_run`、記來歷與帳、受 `BudgetGuard` 限制）。閘門算出的
  鍵和確認的不一樣（例如確認期間補上了摘要）→ `input_changed`，不呼叫模型。確認時不會呼叫的
  項目不交給 service，只在這裡照預估同樣的步驟重算一次狀態（`not_applicable`、`no_input`、
  `blocked`、`too_large`）；現在變成會呼叫的也是 `input_changed`。所以總花費不會超過確認的上限。
  這裡不新增寫入路徑：所有 AI 寫入都經 `run_llm_job` → `services/ai_results.py`。每做完一項呼叫
  `on_item`。

例外怎麼變成項目狀態：論文在跑之前被刪 → `not_found`；`LlmConfigurationError`（金鑰、權限、模型）
→ 這一項 `error`（`llm_configuration`＋狀態碼），停下、其餘 `not_run`；其他例外 → `error`
（只記類別名），其他項目照做。`KeyboardInterrupt` 不攔（閘門已經把 run 收好），原樣往上丟；
重跑同一個指令時做完的會被 `should_run` 略過，不重付。

日誌只記 id、DOI、工作、狀態、原因代碼、耗時與錯誤類別，不記論文內容與模型輸出。
"""

import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog
from pydantic import BaseModel

from radar.domain.ai_outputs import CallMode, GatewayConfig, LlmTask, PaperLlmSource, RunKey
from radar.domain.budget import BudgetConfig
from radar.domain.context_values import ContextValues
from radar.domain.errors import ErrorCode
from radar.domain.llm import LlmConfigurationError, frozen_schema_json, system_prompt
from radar.domain.permissions import Action, require
from radar.domain.policy import cloud_text
from radar.domain.pricing import max_call_cost
from radar.domain.processing import ProcessingFacts, ProcessingTask, TaskPlan, plan_for
from radar.domain.taxonomy import Taxonomy
from radar.domain.trial_runs import (
    TRIAL_TASKS,
    LlmCallDiagnostics,
    TrialCandidate,
    TrialEstimate,
    TrialItem,
    TrialItemStatus,
    TrialPaper,
    TrialPlanItem,
    TrialReport,
    TrialSample,
    TrialSelection,
    TrialTargets,
    check_sample_size,
    sample_tuning_papers,
    trial_paper,
)
from radar.domain.validators.zh_summary import ZhSummaryRules
from radar.domain.viewer import SystemViewer, Viewer
from radar.ports import LlmProvider
from radar.repo.db import Database
from radar.repo.llm_ledger import find_llm_call_diagnostics
from radar.repo.papers import (
    find_paper_ids_by_dois,
    find_paper_llm_source,
    find_paper_processing_facts,
    list_paper_dois,
    list_trial_candidates,
)
from radar.services.classification import run_classification
from radar.services.llm_batches import llm_job
from radar.services.llm_gateway import (
    ConfirmedRequestChangedError,
    GatewayResult,
    PreparedLlmRequest,
    prepare_llm_request,
)
from radar.services.prompt_loader import LoadedPrompt, load_prompt
from radar.services.research_model import run_research_model
from radar.services.zh_summary import run_zh_summary

log = structlog.get_logger(__name__)

# 抽樣時依論文 id 分頁，一頁讀幾篇（每頁讀完只留 id、DOI 與判斷結果，不留摘要）。
PAGE_SIZE: Final = 500


@dataclass(frozen=True)
class TrialSetup:
    """試跑要的設定（CLI 啟動時讀檔組好；不含 provider）。

    Raises:
        ValueError: `gateway.call_mode` 不是互動（試跑一律互動呼叫）。
    """

    gateway: GatewayConfig
    budget: BudgetConfig
    zh_summary_rules: ZhSummaryRules
    taxonomy: Taxonomy
    context_values: ContextValues

    def __post_init__(self) -> None:
        if self.gateway.call_mode is not CallMode.INTERACTIVE:
            raise ValueError("試跑一律互動呼叫；閘門設定要用 call_mode=interactive")


class TrialPapersNotFoundError(LookupError):
    """指定的論文 ID 或 DOI 有找不到的（一篇都不跑）。`missing` 照參數順序。"""

    def __init__(self, missing: tuple[str, ...]) -> None:
        super().__init__(f"找不到 {len(missing)} 個指定的論文")
        self.missing = missing


# --- 選論文 -------------------------------------------------------------------------------


def resolve_trial_papers(
    actor: Viewer, database: Database, targets: TrialTargets
) -> TrialSelection:
    """把指定的論文 ID 與 DOI 解析成論文（照參數順序；ID 與 DOI 指到同一篇時只留第一次）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        TrialPapersNotFoundError: 有任何一個 ID 或 DOI 找不到。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    viewer = SystemViewer()
    with database.transaction() as connection:
        by_doi = find_paper_ids_by_dois(viewer, connection, targets.dois)
        dois_of = list_paper_dois(viewer, connection, [*targets.paper_ids, *by_doi.values()])
    ordered: list[uuid.UUID] = []
    missing: list[str] = []
    for target in targets.targets:
        paper_id = target if isinstance(target, uuid.UUID) else by_doi.get(target)
        if paper_id is None or paper_id not in dois_of:
            missing.append(str(target))
        elif paper_id not in ordered:
            ordered.append(paper_id)
    if missing:
        raise TrialPapersNotFoundError(tuple(missing))
    return TrialSelection(tuple(trial_paper(paper_id, dois_of[paper_id]) for paper_id in ordered))


def _all_candidates(database: Database) -> Iterator[TrialCandidate]:
    viewer = SystemViewer()
    after: uuid.UUID | None = None
    while True:
        with database.transaction() as connection:
            page = list_trial_candidates(viewer, connection, after_id=after, limit=PAGE_SIZE)
        if not page:
            return
        after = page[-1].paper_id
        yield from page


def sample_trial_papers(actor: Viewer, database: Database, size: int, seed: int) -> TrialSelection:
    """從調整組、三個工作都要做的論文裡用 `seed` 抽 `size` 篇（不足時全部）；seed、要幾篇、符合
    條件的篇數與抽到的論文（ID＋DOI）寫進 info 日誌。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        TrialArgumentError: `size` 不在 1～`MAX_TRIAL_PAPERS`（不碰資料庫）。
        ValueError: `seed` 不是整數（不碰資料庫）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    check_sample_size(size)
    if type(seed) is not int:
        raise ValueError("seed 要是整數")
    result = sample_tuning_papers(_all_candidates(database), size, seed)
    papers = tuple(trial_paper(candidate.paper_id, candidate.dois) for candidate in result.selected)
    log.info(
        "試跑抽樣",
        seed=seed,
        requested=size,
        eligible=result.eligible,
        papers=[{"paper_id": str(paper.paper_id), "dois": list(paper.dois)} for paper in papers],
    )
    return TrialSelection(papers, TrialSample(seed=seed, requested=size, eligible=result.eligible))


# --- 預估 ---------------------------------------------------------------------------------


def _plan_item(setup: TrialSetup, prepared: PreparedLlmRequest, attempts: int) -> TrialPlanItem:
    request = prepared.request
    schema_class = prepared.prompt.schema_class
    return TrialPlanItem(
        paper_id=prepared.key.paper_id,
        task=prepared.key.task,
        max_cost_usd=max_call_cost(
            setup.gateway.models,
            prepared.key.model,
            prepared.estimated_input_tokens,
            request.max_tokens,
            is_batch=False,
        ),
        estimated_input_tokens=prepared.estimated_input_tokens,
        max_tokens=request.max_tokens,
        system_chars=len(system_prompt(request)),
        content_chars=sum(len(cloud_text(block)) for block in request.content),
        schema_chars=0 if schema_class is None else len(frozen_schema_json(schema_class)),
        attempts=attempts,
        key=prepared.key,
    )


def _trial_prompts(setup: TrialSetup) -> dict[LlmTask, LoadedPrompt]:
    return {task: load_prompt(setup.gateway.prompts_root, task) for task in TRIAL_TASKS}


def _read_paper(
    database: Database, paper_id: uuid.UUID
) -> tuple[ProcessingFacts, PaperLlmSource] | None:
    """讀一篇論文的處理事實與送模型的來源（同一個交易）；論文不在回 None。"""
    viewer = SystemViewer()
    with database.transaction() as connection:
        facts = find_paper_processing_facts(viewer, connection, paper_id)
        source = find_paper_llm_source(viewer, connection, paper_id, for_update=False)
    if facts is None or source is None:
        return None
    return facts, source


def _prepare(
    setup: TrialSetup,
    prompts: dict[LlmTask, LoadedPrompt],
    paper_id: uuid.UUID,
    task: LlmTask,
    facts: ProcessingFacts,
    source: PaperLlmSource,
) -> TaskPlan | GatewayResult | PreparedLlmRequest:
    """和實跑一樣的前幾步（不碰資料庫、不看預算）：`plan_for` 說不做回 `TaskPlan`；放行或大小
    擋下回 `GatewayResult`；會呼叫模型回 `PreparedLlmRequest`。"""
    plan = plan_for(facts).for_task(ProcessingTask(task.value))
    if plan.input_kind is None:
        return plan
    job = llm_job(setup, paper_id, task, plan.input_kind)
    return prepare_llm_request(setup.gateway, job, source, prompts[task])


def estimate_trial(
    actor: Viewer,
    database: Database,
    setup: TrialSetup,
    selection: TrialSelection,
    *,
    attempts: int,
) -> TrialEstimate:
    """執行前的預估：每篇照 `plan_for` 要做的工作組請求（和實跑同一個 `prepare_llm_request`），
    放行或大小擋下的不算；已經有結果、會被略過的照算（保守）。`attempts` 是 adapter 每個請求最多
    送幾次（含第一次；CLI 從 `AnthropicLlmProvider.attempts` 讀），最壞花費＝單次上限 × attempts。
    每項記下確認時的 `RunKey`，`run_trial` 只跑這些。不看帳本、不寫列。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: `attempts` 不是至少 1 的整數（不碰資料庫）。
        PromptIntegrityError, PromptFormatError: prompt 檔或登記不對。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    if type(attempts) is not int or attempts < 1:
        raise ValueError("attempts 要是至少 1 的整數")
    prompts = _trial_prompts(setup)
    items: list[TrialPlanItem] = []
    for paper in selection.papers:
        found = _read_paper(database, paper.paper_id)
        if found is None:
            continue  # 剛被刪：執行時會是 not_found
        facts, source = found
        for task in TRIAL_TASKS:
            prepared = _prepare(setup, prompts, paper.paper_id, task, facts, source)
            if isinstance(prepared, PreparedLlmRequest):
                items.append(_plan_item(setup, prepared, attempts))
            # 其餘（plan_for 不做、放行或大小擋下）不會呼叫模型，不算。
    return TrialEstimate(tuple(items), attempts=attempts)


# --- 執行 ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _RunOptions:
    database: Database
    provider: LlmProvider
    setup: TrialSetup
    clock: Callable[[], datetime]
    diagnostics: bool
    show_output: bool
    prompts: dict[LlmTask, LoadedPrompt]


def run_trial(
    actor: Viewer,
    database: Database,
    provider: LlmProvider,
    setup: TrialSetup,
    selection: TrialSelection,
    *,
    clock: Callable[[], datetime],
    estimate: TrialEstimate,
    diagnostics: bool = False,
    show_output: bool = False,
    on_item: Callable[[TrialItem], None] | None = None,
) -> TrialReport:
    """逐篇逐工作跑（見模組說明），回整次的結果。只呼叫 `estimate`（確認過的預估）裡有鍵的
    項目，而且閘門算出的鍵要一樣。`diagnostics` 為真時有 run 的項目讀 `llm_call` 的診斷欄位；
    `show_output` 為真時帶驗證過的輸出（或被拒收時的模型原文）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        PromptIntegrityError, PromptFormatError: prompt 檔或登記不對（還沒跑任何項目）。
        KeyboardInterrupt: 原樣往上丟（已完成的項目留著，重跑不重付）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    options = _RunOptions(
        database, provider, setup, clock, diagnostics, show_output, _trial_prompts(setup)
    )
    items: list[TrialItem] = []
    stopped = False
    for paper in selection.papers:
        for task in TRIAL_TASKS:
            plan_item = estimate.for_item(paper.paper_id, task)
            if stopped:
                item = TrialItem(paper.paper_id, task, TrialItemStatus.NOT_RUN, plan=plan_item)
            else:
                item = _run_one(actor, options, paper, task, plan_item)
                stopped = item.reason == ErrorCode.LLM_CONFIGURATION.value
            items.append(item)
            log.info(
                "試跑一項",
                paper_id=str(item.paper_id),
                task=task.value,
                status=item.status.value,
                reason=item.reason,
                http_status=item.http_status,
                run_id=None if item.run_id is None else str(item.run_id),
                elapsed_seconds=(
                    None if item.elapsed_seconds is None else round(item.elapsed_seconds, 3)
                ),
            )
            if on_item is not None:
                on_item(item)
    return TrialReport(
        papers=selection.papers, items=tuple(items), stopped_by_configuration=stopped
    )


def _run_one(
    actor: Viewer,
    options: _RunOptions,
    paper: TrialPaper,
    task: LlmTask,
    plan_item: TrialPlanItem | None,
) -> TrialItem:
    if plan_item is None or plan_item.key is None:
        # 確認時不會呼叫模型的項目：不交給 service，只重算狀態。
        try:
            return _unconfirmed_item(options, paper, task)
        except Exception as exc:
            log.error(
                "試跑的項目出錯",
                paper_id=str(paper.paper_id),
                task=task.value,
                error_type=type(exc).__name__,
            )
            return TrialItem(paper.paper_id, task, TrialItemStatus.ERROR, reason=type(exc).__name__)
    started = time.monotonic()
    try:
        run = _call_service(actor, options, paper.paper_id, task, plan_item.key)
    except Exception as exc:
        elapsed = time.monotonic() - started
        if isinstance(exc, ConfirmedRequestChangedError):
            log.warning(
                "確認後輸入變了，這一項沒有呼叫模型",
                paper_id=str(paper.paper_id),
                task=task.value,
                prompt_version_changed=exc.confirmed.prompt_version != exc.current.prompt_version,
                model_changed=exc.confirmed.model != exc.current.model,
                input_changed=exc.confirmed.input_sha256 != exc.current.input_sha256,
            )
            return TrialItem(
                paper.paper_id,
                task,
                TrialItemStatus.INPUT_CHANGED,
                elapsed_seconds=elapsed,
                plan=plan_item,
            )
        if type(exc) is LookupError:
            # run_* 找不到論文時丟的就是 LookupError 本身（KeyError 之類的子類別算非預期錯誤）。
            return TrialItem(
                paper.paper_id, task, TrialItemStatus.NOT_FOUND, elapsed_seconds=elapsed
            )
        if isinstance(exc, LlmConfigurationError):
            log.error(
                "金鑰或設定錯誤，試跑停下",
                paper_id=str(paper.paper_id),
                task=task.value,
                http_status=exc.status,
            )
            return TrialItem(
                paper.paper_id,
                task,
                TrialItemStatus.ERROR,
                reason=ErrorCode.LLM_CONFIGURATION.value,
                http_status=exc.status,
                elapsed_seconds=elapsed,
                plan=plan_item,
            )
        log.error(
            "試跑的項目出錯",
            paper_id=str(paper.paper_id),
            task=task.value,
            error_type=type(exc).__name__,
        )
        return TrialItem(
            paper.paper_id,
            task,
            TrialItemStatus.ERROR,
            reason=type(exc).__name__,
            elapsed_seconds=elapsed,
            plan=plan_item,
        )
    elapsed = time.monotonic() - started
    plan, result = run
    if result is None:
        if plan.skip is None:  # plan_for 保證不做時一定有 skip
            raise RuntimeError("plan_for 說不做卻沒有狀態")
        return TrialItem(paper.paper_id, task, TrialItemStatus(plan.skip.value), plan=plan_item)
    status = TrialItemStatus(result.outcome.value)
    found = None
    if result.run_id is not None and (options.diagnostics or options.show_output):
        found = _diagnostics(options.database, result.run_id)
    output: object | None = None
    output_is_raw = False
    if options.show_output:
        if result.output is not None:
            output = _json_value(result.output)
        elif status is TrialItemStatus.REJECTED and found is not None:
            output = found.output_text
            output_is_raw = output is not None
    return TrialItem(
        paper.paper_id,
        task,
        status,
        reason=None if result.reason is None else result.reason.value,
        cost_usd=result.cost_usd,
        run_id=result.run_id,
        resumed=result.resumed,
        elapsed_seconds=elapsed,
        plan=plan_item,
        diagnostics=found if options.diagnostics else None,
        output=output,
        output_is_raw=output_is_raw,
    )


def _unconfirmed_item(options: _RunOptions, paper: TrialPaper, task: LlmTask) -> TrialItem:
    """確認時不會呼叫模型的項目：照預估同樣的步驟重算狀態，不呼叫模型、不寫列。現在變成會呼叫
    （例如確認期間補上了摘要）→ `input_changed`。"""
    found = _read_paper(options.database, paper.paper_id)
    if found is None:
        return TrialItem(paper.paper_id, task, TrialItemStatus.NOT_FOUND)
    facts, source = found
    prepared = _prepare(options.setup, options.prompts, paper.paper_id, task, facts, source)
    if isinstance(prepared, TaskPlan):
        if prepared.skip is None:  # plan_for 保證不做時一定有 skip
            raise RuntimeError("plan_for 說不做卻沒有狀態")
        return TrialItem(paper.paper_id, task, TrialItemStatus(prepared.skip.value))
    if isinstance(prepared, GatewayResult):
        return TrialItem(
            paper.paper_id,
            task,
            TrialItemStatus(prepared.outcome.value),
            reason=None if prepared.reason is None else prepared.reason.value,
        )
    log.warning(
        "確認時不會呼叫、現在會呼叫，這一項沒有呼叫模型",
        paper_id=str(paper.paper_id),
        task=task.value,
    )
    return TrialItem(paper.paper_id, task, TrialItemStatus.INPUT_CHANGED)


def _call_service(
    actor: Viewer,
    options: _RunOptions,
    paper_id: uuid.UUID,
    task: LlmTask,
    confirmed_key: RunKey,
) -> tuple[TaskPlan, GatewayResult | None]:
    setup = options.setup
    if task is LlmTask.ZH_SUMMARY:
        zh = run_zh_summary(
            actor,
            options.database,
            options.provider,
            setup.gateway,
            setup.budget,
            setup.zh_summary_rules,
            paper_id,
            clock=options.clock,
            confirmed_key=confirmed_key,
        )
        return zh.plan, zh.result
    if task is LlmTask.CLASSIFY:
        classified = run_classification(
            actor,
            options.database,
            options.provider,
            setup.gateway,
            setup.budget,
            setup.taxonomy,
            paper_id,
            clock=options.clock,
            confirmed_key=confirmed_key,
        )
        return classified.plan, classified.result
    if task is LlmTask.EXTRACT_MODEL:
        model = run_research_model(
            actor,
            options.database,
            options.provider,
            setup.gateway,
            setup.budget,
            setup.context_values,
            paper_id,
            clock=options.clock,
            confirmed_key=confirmed_key,
        )
        return model.plan, model.result
    raise ValueError(f"{task.value} 不是試跑的工作")


def _diagnostics(database: Database, run_id: uuid.UUID) -> LlmCallDiagnostics | None:
    """讀 `llm_call` 的診斷欄位；讀失敗只記警告（結果已經寫好了，不影響這一項的狀態）。"""
    try:
        with database.transaction() as connection:
            return find_llm_call_diagnostics(SystemViewer(), connection, run_id)
    except Exception as exc:
        log.warning("讀 llm_call 診斷欄位失敗", run_id=str(run_id), error_type=type(exc).__name__)
        return None


def _json_value(output: BaseModel | str) -> object:
    return output.model_dump(mode="json") if isinstance(output, BaseModel) else output
