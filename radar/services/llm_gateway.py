"""LLM 閘門（AD-8、ADR-0009、ADR-0030）：全專案唯一呼叫 `LlmProvider` 的地方。

`run_llm_job` 每次照固定順序走，前一步擋下時後面的步驟都不做、不寫任何列：

1. 從資料列推出要送的文字與來源（`build_llm_input`），每段經 `clear_for_cloud` 放行
   → 沒有輸入 `NO_INPUT`；放行擋下 `BLOCKED`（帶原因代碼）。
2. 預算檢查（注入的 `budget_allows`；真正的 BudgetGuard 在 2.3）→ 不准 `DEFERRED_BUDGET`。
3. 讀 prompt（`prompts/<工作>/v<N>.md`；policy 核對 domain 寫死的雜湊登記才放行）、算
   `input_sha256`、組 `LlmRequest`。
4. 離線保守估計輸入 token；超過 `max_input_tokens` 而且工作沒允許 → `TOO_LARGE`。
5. 取得執行資格的短交易：advisory lock → 再問一次 `should_run`（有 `active`、平常流程的
   `rejected` 或 `running` 就 `SKIPPED`）→ 有 `llm_result_pending` 的 run 就改回 `running` 接續，
   否則新增 `running` 的 `derivation_run`（部分唯一索引兜底），立刻 commit。評估（帶
   `eval_run_id`）不問 `should_run`、不受 running 索引限制；同一個 `eval_run_id` 已經有 `running`
   時 `SKIPPED`，也只接續同一個 `eval_run_id` 的 run。
6. 呼叫模型（不開著交易；接續時跳過 6、7）。暫時性錯誤 → run `failed`、`TRANSIENT_FAILURE`；
   設定錯 → run `failed` 後原樣丟出。
7. 自己的短交易記 `llm_call`（有 usage 才記；費用由 `domain/pricing.py` 算），連同模型回的文字
   或呼叫當下的拒收原因（`SavedLlmOutput`）一起存。
8. Pydantic 驗格式 → domain 驗證（工作的驗證函式）→ 確認輸出存得進資料庫；任何一步不過都是
   拒收（`rejected`＋原因代碼），不重試。
9. `services/ai_results.py` 鎖論文列、核對後寫入（另一個短交易）。

記帳之後（8、9）出非預期的錯（驗證函式丟了 `OutputRejectedError` 以外的例外、寫入的交易失敗）
時，帳照記，run 改成 `failed`（`llm_result_pending`），例外原樣丟出；下一次同一組鍵從存下的輸出
接續 8、9，不再呼叫模型、不再記帳（ADR-0030）。只支援互動呼叫；Batch 由 Batch 的 Story 另做。
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from radar.domain.ai_outputs import (
    HEADER_TABLES,
    CallMode,
    GatewayConfig,
    InputKind,
    LlmInput,
    LlmTask,
    NewDerivationRun,
    NewLlmCall,
    NoLlmInputError,
    OutputLang,
    OutputRejectedError,
    ResultStatus,
    RunKey,
    SavedLlmOutput,
    build_llm_input,
    check_output_storable,
    is_storable_json,
)
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import input_sha256, run_lock_key
from radar.domain.llm import (
    LlmConfigurationError,
    LlmRejectedError,
    LlmRequest,
    LlmTransientError,
    LlmUsage,
    frozen_schema_json,
    system_prompt,
)
from radar.domain.permissions import Action, require
from radar.domain.policy import CloudClearanceError, CloudSafeText, clear_for_cloud, cloud_text
from radar.domain.pricing import estimate_input_tokens, llm_call_cost
from radar.domain.viewer import SystemViewer, Viewer
from radar.ports import LlmProvider
from radar.repo.ai_outputs import find_blocking_ai_result
from radar.repo.db import Database
from radar.repo.llm_ledger import (
    create_derivation_run,
    create_llm_call,
    find_llm_call_output,
    find_running_derivation_run_id,
    update_derivation_run_failed,
    update_derivation_run_failed_or_pending,
    update_derivation_run_resumed,
    update_expired_derivation_runs,
)
from radar.repo.papers import find_paper_llm_source
from radar.services.ai_results import record_llm_result
from radar.services.prompt_loader import LoadedPrompt, load_prompt

# `GatewayConfig` 定義在 domain（bootstrap 不能 import services），從這裡也拿得到。
__all__ = [
    "GatewayConfig",
    "GatewayOutcome",
    "GatewayResult",
    "LlmJob",
    "OutputValidator",
    "expire_stale_runs",
    "run_llm_job",
]

log = structlog.get_logger(__name__)

# 工作的 domain 驗證：收 Pydantic 驗過的輸出（沒有 schema 時是文字）與送出的輸入；
# 不通過丟 `OutputRejectedError(原因代碼)`。
type OutputValidator = Callable[[BaseModel | str, LlmInput], None]


class GatewayOutcome(StrEnum):
    """`run_llm_job` 的結果。前四個寫了表頭；其餘沒有寫表頭。"""

    ACTIVE = "active"
    CANDIDATE = "candidate"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    SKIPPED = "skipped"  # should_run 說不用跑（已有結果、平常流程的 rejected 或 running）
    BLOCKED = "blocked"  # 放行擋下
    NO_INPUT = "no_input"  # 沒有這種 input_kind 要的內容
    DEFERRED_BUDGET = "deferred_budget"  # 預算不准
    TOO_LARGE = "too_large"  # 預估輸入超過上限
    TRANSIENT_FAILURE = "transient_failure"  # 逾時、限流、服務端錯誤、連線錯誤：下一次可以再跑


_RESULT_OUTCOME = {
    ResultStatus.ACTIVE: GatewayOutcome.ACTIVE,
    ResultStatus.CANDIDATE: GatewayOutcome.CANDIDATE,
    ResultStatus.SUPERSEDED: GatewayOutcome.SUPERSEDED,
    ResultStatus.REJECTED: GatewayOutcome.REJECTED,
}


@dataclass(frozen=True)
class LlmJob:
    """一件 LLM 工作：哪篇論文、哪個工作、看標題還是摘要。呼叫端不能給文字或來源。

    - `lang`：產出的語言（寫進表頭）。
    - `validate_output`：這個工作的 domain 驗證（2.4～2.6 各自提供）；None 表示不另外驗。
    - `taxonomy_version`／`context_values_version`：`classify`／`extract_model` 一定要帶，其他不得帶
      （算進 `input_sha256`）。
    """

    paper_id: uuid.UUID
    task: LlmTask
    input_kind: InputKind
    lang: OutputLang
    validate_output: OutputValidator | None = None
    taxonomy_version: str | None = None
    context_values_version: str | None = None


@dataclass(frozen=True)
class GatewayResult:
    """一次 `run_llm_job` 的結果。

    - `run_id`：有寫 `derivation_run` 時才有。
    - `reason`：擋下、拒收或暫時性失敗的原因代碼。
    - `output`：通過驗證的輸出（Pydantic 模型；沒有 schema 時是文字），只有寫成 `active`／
      `candidate`／`superseded` 時有。
    - `cost_usd`：這一次有記 `llm_call` 時的費用（接續時沒有新記帳，是 None）。
    - `resumed`：這一次是從存下的輸出接續（沒有呼叫模型）。
    """

    outcome: GatewayOutcome
    run_id: uuid.UUID | None = None
    reason: ErrorCode | None = None
    output: BaseModel | str | None = None
    cost_usd: Decimal | None = None
    resumed: bool = False


@dataclass(frozen=True)
class _Claim:
    """取得的執行資格：`saved` 有值代表接續（不呼叫模型）。"""

    run_id: uuid.UUID
    saved: SavedLlmOutput | None


def run_llm_job(
    actor: Viewer,
    database: Database,
    provider: LlmProvider,
    config: GatewayConfig,
    job: LlmJob,
    *,
    budget_allows: Callable[[LlmTask], bool],
    eval_run_id: uuid.UUID | None = None,
) -> GatewayResult:
    """照 AD-8 的固定步驟跑一件 LLM 工作（見模組說明）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: 工作沒有表頭表（2.2 只支援 `zh_summary`、`classify`、`extract_model`）、
            分類表／情境值清單版本帶錯，或 `config.call_mode` 不是互動（Batch 還不支援；都不碰
            資料庫）。
        LookupError: 論文不存在。
        PromptIntegrityError, PromptFormatError: prompt 檔或登記不對（還沒寫任何列）。
        LlmConfigurationError: 金鑰、權限或模型設定不對（run 已改成 `failed`）。
        Exception: 記帳後驗證或寫結果失敗（帳照記，run 盡量改成 `failed`／`llm_result_pending`，
            下一次接續），原樣往上丟。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    if config.call_mode is not CallMode.INTERACTIVE:
        # Batch 的計價（五折）與生命週期（不受 sweep 時限、跟著批次結果走）要等真正的 Batch
        # 流程；現在這個入口是同步呼叫，照 Batch 記帳會少記一半（ADR-0030）。
        raise ValueError("閘門目前只支援互動呼叫；Batch 由 Batch 的 Story 另做")
    task = LlmTask(job.task)
    if task not in HEADER_TABLES:
        raise ValueError(f"{task.value} 還沒有表頭表，閘門不支援（2.2 只支援有表頭的工作）")
    viewer = SystemViewer()

    # 1. 從資料列推出來源並放行。
    with database.transaction() as connection:
        source = find_paper_llm_source(viewer, connection, job.paper_id, for_update=False)
    if source is None:
        raise LookupError("找不到論文")
    try:
        llm_input = build_llm_input(source, job.input_kind)
    except NoLlmInputError:
        return GatewayResult(GatewayOutcome.NO_INPUT)
    try:
        content = tuple(
            clear_for_cloud(block.text, block.source, None, config.cloud_policy)
            for block in llm_input.blocks
        )
    except CloudClearanceError as exc:
        log.info("LLM 輸入放行擋下", task=task.value, source=exc.source.value, reason=exc.reason)
        return GatewayResult(GatewayOutcome.BLOCKED, reason=exc.reason)

    # 2. 預算檢查。
    if not budget_allows(task):
        return GatewayResult(GatewayOutcome.DEFERRED_BUDGET)

    # 3. prompt、指紋、請求。
    prompt = load_prompt(config.prompts_root, task)
    task_model = config.models.task_model(task)
    fingerprint = input_sha256(
        llm_input.blocks,
        task,
        taxonomy_version=job.taxonomy_version,
        context_values_version=job.context_values_version,
    )
    key = RunKey(job.paper_id, task, prompt.version, task_model.model, fingerprint)
    request = _build_request(prompt, task_model.model, content)

    # 4. 輸入太大就不送。
    # 送出的 JSON schema 也算進輸入，一起估。
    sent_texts = [system_prompt(request), *(cloud_text(block) for block in request.content)]
    if prompt.schema_class is not None:
        sent_texts.append(frozen_schema_json(prompt.schema_class))
    estimated = estimate_input_tokens(sent_texts, config.models.token_estimate)
    if estimated > config.models.max_input_tokens and not task_model.allow_over_input_limit:
        return GatewayResult(GatewayOutcome.TOO_LARGE, reason=ErrorCode.LLM_INPUT_TOO_LARGE)

    # 5. 取得執行資格（短交易，立刻 commit）。
    claim = _acquire(database, config, job, key, prompt, llm_input, eval_run_id)
    if claim is None:
        return GatewayResult(GatewayOutcome.SKIPPED)
    run_id = claim.run_id
    if claim.saved is not None:
        # 接續：模型已經回應、帳已經記過，只做 8、9。
        log.info("從存下的輸出接續 LLM 工作", task=task.value, run_id=str(run_id))
        return _finish(
            actor, database, config, job, prompt, run_id, llm_input, claim.saved, None, True
        )

    # 6. 呼叫（不開著交易）。
    try:
        result = provider.complete(request)
    except LlmTransientError as exc:
        _fail_run(database, run_id, exc.code)
        return GatewayResult(GatewayOutcome.TRANSIENT_FAILURE, run_id=run_id, reason=exc.code)
    except LlmConfigurationError:
        _fail_run(database, run_id, ErrorCode.LLM_CONFIGURATION)
        raise
    except LlmRejectedError as exc:
        saved = SavedLlmOutput(text=None, reject_reason=exc.code)
        try:
            cost = _record_call(
                database,
                config,
                run_id,
                key.model,
                exc.usage,
                exc.response_id,
                exc.request_id,
                output=saved,
            )
        except BaseException:
            _try_fail_run(database, run_id)
            raise
        return _finish(actor, database, config, job, prompt, run_id, llm_input, saved, cost, False)
    except BaseException:
        _fail_run(database, run_id, ErrorCode.LLM_GATEWAY_ERROR)
        raise

    # 7. 記帳，連同輸出一起存（文字存不進資料庫就直接存拒收原因）。
    saved = (
        SavedLlmOutput(text=result.text, reject_reason=None)
        if is_storable_json(result.text)
        else SavedLlmOutput(text=None, reject_reason=ErrorCode.LLM_OUTPUT_UNSTORABLE)
    )
    try:
        cost = _record_call(
            database,
            config,
            run_id,
            key.model,
            result.usage,
            result.response_id,
            result.request_id,
            stop_reason=result.stop_reason,
            raw=result.raw,
            output=saved,
        )
    except BaseException:
        _try_fail_run(database, run_id)
        raise

    # 8、9.
    return _finish(actor, database, config, job, prompt, run_id, llm_input, saved, cost, False)


def expire_stale_runs(actor: Viewer, database: Database, older_than: timedelta) -> int:
    """把超過 `older_than` 沒有進展還是 `running` 的互動 `derivation_run` 改成 `failed`，回改了
    幾筆：已經存下輸出的記 `llm_result_pending`（下一次接續），否則記 `llm_run_expired`。sweep 每
    15 分鐘呼叫一次。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: `older_than` 不是正的時間長度。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    if older_than <= timedelta(0):
        raise ValueError("older_than 要大於 0")
    with database.transaction() as connection:
        expired = update_expired_derivation_runs(connection, older_than)
    if expired:
        log.warning("互動 LLM 呼叫逾時，derivation_run 改成 failed", runs=expired)
    return expired


# --- 各步驟 ---------------------------------------------------------------------------------


def _build_request(
    prompt: LoadedPrompt, model: str, content: tuple[CloudSafeText, ...]
) -> LlmRequest:
    settings = prompt.settings
    # schema 類別由 loader 從 `OUTPUT_SCHEMAS` 取出；`LlmRequest` 再核對它在凍結的登記表裡。
    return LlmRequest(
        model=model,
        system=prompt.template,
        content=content,
        max_tokens=settings.max_tokens,
        effort=settings.effort,
        disable_thinking=settings.disable_thinking,
        output_schema=prompt.schema_class,
    )


def _acquire(
    database: Database,
    config: GatewayConfig,
    job: LlmJob,
    key: RunKey,
    prompt: LoadedPrompt,
    llm_input: LlmInput,
    eval_run_id: uuid.UUID | None,
) -> _Claim | None:
    """取得執行資格：同一個短交易裡上鎖、重問 `should_run`、接續 `llm_result_pending` 的 run 或
    新增 `running`。不用跑回 None。"""
    viewer = SystemViewer()
    settings = prompt.settings
    params: dict[str, object] = {
        "effort": settings.effort,
        "max_tokens": settings.max_tokens,
        "disable_thinking": settings.disable_thinking,
        "output_schema": settings.output_schema,
        # 送出的凍結 schema 字串的雜湊：`Field(description=...)` 這類改動也看得出來（來歷）。
        "output_schema_sha256": prompt.schema_sha256,
    }
    with database.transaction() as connection:
        # 阻塞的交易級鎖：交易結束（commit 或退回）時自動放掉。
        Database.transaction_lock(connection, run_lock_key(key))
        # 先查 running 再查表頭：READ COMMITTED 下兩句各自取快照，別人的表頭與 run 改 succeeded
        # 在同一個交易 commit。先查表頭再查 running 的話，對方剛好在兩句之間 commit 時兩句都查不到，
        # 就會再付一次錢；這個順序下一定看得到其中一個。
        # 評估也查 running（只看同一個 eval_run_id）：別人剛把 pending 改回 running、離開鎖去
        # 驗證時，這裡找不到 pending，不擋就會新增 run、再付一次錢。評估不查表頭（`should_run`）。
        if find_running_derivation_run_id(viewer, connection, key, eval_run_id) is not None:
            return None
        if eval_run_id is None and find_blocking_ai_result(viewer, connection, key) is not None:
            return None
        # 已經付過錢、只差寫結果的 run：改回 running，從存下的輸出接續（不再呼叫模型）。
        resumed = update_derivation_run_resumed(connection, key, eval_run_id)
        if resumed is not None:
            saved = find_llm_call_output(viewer, connection, resumed)
            if saved is None:  # 改回 running 的條件已經要求有存下的輸出
                raise RuntimeError("接續的 derivation_run 沒有存下的輸出")
            return _Claim(resumed, saved)
        run_id = create_derivation_run(
            connection,
            NewDerivationRun(
                key=key,
                prompt_sha256=prompt.sha256,
                input_kind=llm_input.input_kind,
                params=params,
                taxonomy_version=job.taxonomy_version,
                context_values_version=job.context_values_version,
                code_version=config.code_version,
                call_mode=config.call_mode,
                eval_run_id=eval_run_id,
            ),
        )
    return None if run_id is None else _Claim(run_id, None)


def _fail_run(database: Database, run_id: uuid.UUID, reason: ErrorCode) -> None:
    """自己的短交易把 run 改成 `failed`（已經不是 `running` 就不改）。"""
    with database.transaction() as connection:
        update_derivation_run_failed(connection, run_id, reason)


def _try_fail_run(database: Database, run_id: uuid.UUID) -> None:
    """出錯後的收尾：盡量把 run 改成 `failed`。已經有存下輸出的 `llm_call` 時記
    `llm_result_pending`（下一次接續、不再付錢），否則記 `llm_gateway_error`。這一步也失敗只記
    日誌（run 停在 running，sweep 之後照同一個規則收），讓原本的例外照樣往上丟。"""
    try:
        with database.transaction() as connection:
            update_derivation_run_failed_or_pending(connection, run_id, ErrorCode.LLM_GATEWAY_ERROR)
    except Exception as cleanup:
        log.warning("出錯後把 derivation_run 改成 failed 也失敗", error_type=type(cleanup).__name__)


def _record_call(
    database: Database,
    config: GatewayConfig,
    run_id: uuid.UUID,
    model: str,
    usage: LlmUsage | None,
    response_id: str | None,
    request_id: str | None,
    *,
    stop_reason: str | None = None,
    raw: Mapping[str, Any] | None = None,
    output: SavedLlmOutput,
) -> Decimal | None:
    """自己的短交易記一筆 `llm_call`（連同存下的輸出），回這次的費用（依送出的模型計價）。沒有
    usage 時不記、回 None（這時沒有東西可接續，之後出錯就是 `llm_gateway_error`）。

    原始回應存不進 jsonb（例如含 NUL、NaN）時改存 NULL，帳照記；接續只用 `output`，不看原始回應。
    """
    if usage is None:
        return None
    cost = llm_call_cost(config.models, model, usage, is_batch=config.call_mode is CallMode.BATCH)
    with database.transaction() as connection:
        create_llm_call(
            connection,
            NewLlmCall(
                run_id=run_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                cache_read_input_tokens=usage.cache_read_input_tokens,
                cost_usd=cost.cost_usd,
                price_tier=cost.price_tier.value,
                is_batch=config.call_mode is CallMode.BATCH,
                response_id=response_id,
                request_id=request_id,
                stop_reason=stop_reason,
                raw_response=raw if raw is not None and is_storable_json(dict(raw)) else None,
                output=output,
            ),
        )
    return cost.cost_usd


def _finish(
    actor: Viewer,
    database: Database,
    config: GatewayConfig,
    job: LlmJob,
    prompt: LoadedPrompt,
    run_id: uuid.UUID,
    llm_input: LlmInput,
    saved: SavedLlmOutput,
    cost: Decimal | None,
    resumed: bool,
) -> GatewayResult:
    """8、9：驗證存下的輸出、寫結果。剛呼叫完與接續走同一段。

    驗證丟出 `OutputRejectedError` 以外的例外時盡量把 run 改成 `failed`（有帳就是
    `llm_result_pending`），例外原樣丟出。
    """
    reject_reason = saved.reject_reason
    output: BaseModel | str | None = None
    if saved.text is not None:
        try:
            output = _validated_output(prompt, saved.text, job, llm_input)
        except OutputRejectedError as exc:
            reject_reason = exc.code
        except BaseException:
            _try_fail_run(database, run_id)
            raise
    return _write_result(
        actor, database, config, job, run_id, llm_input, reject_reason, output, cost, resumed
    )


def _validated_output(
    prompt: LoadedPrompt, text: str, job: LlmJob, llm_input: LlmInput
) -> BaseModel | str:
    """Pydantic → domain 驗證 → 存得進資料庫。不過丟 `OutputRejectedError`。"""
    output: BaseModel | str
    if prompt.schema_class is not None:
        try:
            output = prompt.schema_class.model_validate_json(text, strict=True)
        except ValidationError:
            raise OutputRejectedError(ErrorCode.LLM_OUTPUT_INVALID) from None
        storable: object = output.model_dump(mode="json")
    else:
        output = text
        storable = output
    if job.validate_output is not None:
        job.validate_output(output, llm_input)
    check_output_storable(storable)
    return output


def _write_result(
    actor: Viewer,
    database: Database,
    config: GatewayConfig,
    job: LlmJob,
    run_id: uuid.UUID,
    llm_input: LlmInput,
    reject_reason: ErrorCode | None,
    output: BaseModel | str | None,
    cost: Decimal | None,
    resumed: bool,
) -> GatewayResult:
    """經 `ai_results` 寫表頭。失敗時盡量把 run 改成 `failed`（有帳就是 `llm_result_pending`，
    下一次接續），例外原樣丟出。"""
    try:
        status = record_llm_result(
            actor,
            database,
            config,
            paper_id=job.paper_id,
            run_id=run_id,
            lang=job.lang,
            abstract_id=llm_input.abstract_id,
            reject_reason=reject_reason,
            taxonomy_version=job.taxonomy_version,
            context_values_version=job.context_values_version,
        )
    except BaseException as exc:
        _try_fail_run(database, run_id)
        log.error("寫 AI 結果失敗", task=job.task, error_type=type(exc).__name__)
        raise
    return GatewayResult(
        _RESULT_OUTCOME[status],
        run_id=run_id,
        reason=reject_reason,
        output=None if status is ResultStatus.REJECTED else output,
        cost_usd=cost,
        resumed=resumed,
    )
