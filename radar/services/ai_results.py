"""AI 結果的唯一寫入處（AD-7、AD-8、ADR-0009 第 8 步、ADR-0015）。

`record_llm_result` 在一個短交易裡：

1. 鎖住論文那一列（`FOR UPDATE OF paper`），再鎖住那一筆 `derivation_run`。
2. 依序決定這一列表頭的狀態：
   - 被拒收（帶原因代碼）→ `rejected`；
   - 評估（`eval_run_id` 有值）→ `candidate`（不啟用、不擋平常的流程）；
   - `derivation_run` 已經不是 `running`（例如 sweep 判定逾時改成 `failed`）→ `superseded`；
   - 重算目前輸入的 `input_sha256`、目前的 prompt 版本、目前該用的模型，有一個對不上
     → `superseded`（晚到的結果不蓋掉新的）；
   - 都對 → 先把舊的 `active` 標成 `superseded`，再新增 `active`。
3. 內容：不是拒收、工作是 `zh_summary`、輸出是 `ZhSummaryOutput` 時，用
   `normalize_zh_summary` 從（閘門已經驗證過的）輸出得出四欄內容一起寫；呼叫端不能直接給內容。
   拒收一律不寫內容（資料庫的 CHECK 也擋）。
4. `derivation_run` 還是 `running` 時，同一個交易改成 `succeeded`（被 sweep 改成 `failed` 的維持
   `failed`；原因是 `llm_result_pending` 的改成 `llm_run_expired`：已經寫了表頭，不再接續）。

核對與切換在同一個交易、先鎖論文：兩筆結果同時要啟用時一定一筆等另一筆做完，最後有效的是
目前輸入算出來的那一筆。`radar runs promote`（候選升正式，Epic 8）之後走同一個函式的另一個入口。
"""

import uuid

from pydantic import BaseModel

from radar.domain.ai_outputs import (
    HEADER_TABLES,
    DerivationRun,
    GatewayConfig,
    LlmTask,
    NewAiResult,
    NoLlmInputError,
    OutputLang,
    PaperLlmSource,
    ResultStatus,
    RunStatus,
    ZhSummaryContent,
    build_llm_input,
)
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import input_sha256
from radar.domain.llm_schemas import ZhSummaryOutput
from radar.domain.permissions import Action, require
from radar.domain.validators.zh_summary import normalize_zh_summary
from radar.domain.viewer import SystemViewer, Viewer
from radar.repo.ai_outputs import create_ai_result, update_ai_results_superseded
from radar.repo.db import Database
from radar.repo.llm_ledger import (
    find_derivation_run,
    update_derivation_run_pending_settled,
    update_derivation_run_succeeded,
)
from radar.repo.papers import find_paper_llm_source
from radar.services.prompt_loader import current_prompt_version


class ResultTargetNotFoundError(LookupError):
    """要寫結果的論文或 `derivation_run` 不存在（或兩者對不起來）。"""


def record_llm_result(
    actor: Viewer,
    database: Database,
    config: GatewayConfig,
    *,
    paper_id: uuid.UUID,
    run_id: uuid.UUID,
    lang: OutputLang,
    abstract_id: uuid.UUID | None,
    reject_reason: ErrorCode | None,
    taxonomy_version: str | None = None,
    context_values_version: str | None = None,
    output: BaseModel | str | None = None,
) -> ResultStatus:
    """把一次模型呼叫的結果寫成一列表頭，回寫成的狀態（見模組說明的順序）。

    `reject_reason` 有值代表被拒收；`abstract_id` 是送出時用的那一筆摘要（只看標題時是 None）。
    `output` 是閘門驗證過的輸出（Pydantic 模型；沒有 schema 時是文字），內容欄位從它得出。
    `taxonomy_version`／`context_values_version` 是**現在**的分類表與情境值清單版本（重算目前輸入
    的指紋用；2.5／2.6 由呼叫端在寫入時讀設定傳進來）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ResultTargetNotFoundError: 論文或 `derivation_run` 不存在，或 run 不是這篇論文的。
        ValueError: 這個工作沒有表頭表。
        PromptIntegrityError, PromptFormatError: 讀不出目前的 prompt 版本。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    viewer = SystemViewer()
    with database.transaction() as connection:
        source = find_paper_llm_source(viewer, connection, paper_id, for_update=True)
        run = find_derivation_run(viewer, connection, run_id, for_update=True)
        if source is None or run is None or run.key.paper_id != paper_id:
            raise ResultTargetNotFoundError("找不到要寫結果的論文或 derivation_run")
        task = run.key.task
        if task not in HEADER_TABLES:
            raise ValueError(f"{task.value} 沒有表頭表")

        if reject_reason is not None:
            status = ResultStatus.REJECTED
        elif run.eval_run_id is not None:
            status = ResultStatus.CANDIDATE
        elif run.status is not RunStatus.RUNNING or not _still_current(
            config, run, source, taxonomy_version, context_values_version
        ):
            # sweep 已經判定失敗，或輸入／prompt／模型已經不是現在該用的：只留作歷史。
            status = ResultStatus.SUPERSEDED
        else:
            update_ai_results_superseded(connection, task, paper_id, lang)
            status = ResultStatus.ACTIVE

        create_ai_result(
            connection,
            task,
            NewAiResult(
                paper_id=paper_id,
                run_id=run.id,
                input_kind=run.input_kind,
                input_sha256=run.key.input_sha256,
                abstract_id=abstract_id,
                lang=lang,
                status=status,
                reject_reason=reject_reason,
                zh_summary=_zh_summary_content(task, reject_reason, output),
            ),
        )
        if run.status is RunStatus.RUNNING:
            update_derivation_run_succeeded(connection, run.id)
        elif run.failure_reason == ErrorCode.LLM_RESULT_PENDING:
            update_derivation_run_pending_settled(connection, run.id)
    return status


def _zh_summary_content(
    task: LlmTask, reject_reason: ErrorCode | None, output: BaseModel | str | None
) -> ZhSummaryContent | None:
    """要和表頭一起寫的中文重點內容：只有不是拒收、`zh_summary`、輸出是 `ZhSummaryOutput` 時有。"""
    if reject_reason is not None or task is not LlmTask.ZH_SUMMARY:
        return None
    if not isinstance(output, ZhSummaryOutput):
        return None
    return normalize_zh_summary(output)


def _still_current(
    config: GatewayConfig,
    run: DerivationRun,
    source: PaperLlmSource,
    taxonomy_version: str | None,
    context_values_version: str | None,
) -> bool:
    """這筆 run 的輸入指紋、prompt 版本、模型是不是這篇論文**現在**該用的。"""
    task = run.key.task
    try:
        current_input = build_llm_input(source, run.input_kind)
    except NoLlmInputError:
        return False
    current_sha = input_sha256(
        current_input.blocks,
        task,
        taxonomy_version=taxonomy_version,
        context_values_version=context_values_version,
    )
    return (
        current_sha == run.key.input_sha256
        and current_prompt_version(task) == run.key.prompt_version
        and config.models.task_model(task).model == run.key.model
    )
