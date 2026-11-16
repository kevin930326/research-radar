"""跑一篇論文的研究模型擷取（PRD FR-3、FR-17、Story 2.6、ADR-0015、ADR-0034）。

`run_research_model` 先讀論文的處理事實、問 `domain/processing.plan_for` 要不要做：

- 不做（被排除、手填書目 → `not_applicable`；沒有摘要 → `no_input`）：直接回，不呼叫閘門、不寫
  任何列。
- 要做：標題＋摘要（`abstract`）交給 LLM 閘門（`run_llm_job`），語言 `en`（構面英文名與原句是
  英文），domain 驗證是 `ResearchModelValidator(情境值清單)`，預算檢查一律 `BudgetGuard(...)`。
  情境值清單版本算進 `input_sha256`（清單升版＝每篇都要重跑）；同一個清單物件也交給寫入，當「現在的
  情境值清單」，內容只在寫入時從驗證過的輸出得出。

`context_values` 由呼叫端在行程啟動時用 `radar/config.load_context_values` 讀好傳進來（services
不讀設定）。指定論文的試跑（2.10）與 Batch（2.9）之後呼叫這裡或照同樣的方式問 `plan_for`。
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from radar.domain.ai_outputs import GatewayConfig, LlmTask, OutputLang, RunKey
from radar.domain.budget import BudgetConfig
from radar.domain.context_values import ContextValues
from radar.domain.permissions import Action, require
from radar.domain.processing import ProcessingTask, TaskPlan, plan_for
from radar.domain.validators.research_model import ResearchModelValidator
from radar.domain.viewer import SystemViewer, Viewer
from radar.ports import LlmProvider
from radar.repo.db import Database
from radar.repo.papers import find_paper_processing_facts
from radar.services.budget import BudgetGuard
from radar.services.llm_gateway import GatewayResult, LlmJob, run_llm_job


@dataclass(frozen=True)
class ResearchModelRun:
    """一次 `run_research_model` 的結果：`plan` 是 `plan_for` 對研究模型的回答；`result` 只有真的
    交給閘門時才有（不做時是 None）。"""

    plan: TaskPlan
    result: GatewayResult | None


def run_research_model(
    actor: Viewer,
    database: Database,
    provider: LlmProvider,
    config: GatewayConfig,
    budget: BudgetConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
    *,
    clock: Callable[[], datetime],
    confirmed_key: RunKey | None = None,
    call_allowed: bool = True,
) -> ResearchModelRun:
    """跑一篇論文的研究模型擷取（見模組說明）。`clock` 給預算檢查算台北月份用。
    `confirmed_key`：試跑確認過的鍵，原樣交給閘門核對（Story 2.10；見 `run_llm_job`）。
    `call_allowed`：False 時閘門只接續或略過、不呼叫模型（Story 2.11 確認審查 D1；見
    `run_llm_job`）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        LookupError: 論文不存在。
        其他：閘門丟的例外原樣往上丟（見 `run_llm_job`）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    with database.transaction() as connection:
        facts = find_paper_processing_facts(SystemViewer(), connection, paper_id)
    if facts is None:
        raise LookupError("找不到論文")
    plan = plan_for(facts).for_task(ProcessingTask.EXTRACT_MODEL)
    if plan.input_kind is None:
        return ResearchModelRun(plan, None)
    result = run_llm_job(
        actor,
        database,
        provider,
        config,
        LlmJob(
            paper_id=paper_id,
            task=LlmTask.EXTRACT_MODEL,
            input_kind=plan.input_kind,
            lang=OutputLang.EN,
            validate_output=ResearchModelValidator(context_values),
            context_values_version=context_values.version_label,
            context_values=context_values,
        ),
        budget_allows=BudgetGuard(database, budget, clock),
        confirmed_key=confirmed_key,
        call_allowed=call_allowed,
    )
    return ResearchModelRun(plan, result)
