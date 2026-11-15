"""跑一篇論文的中文重點（PRD FR-14、Story 2.4、ADR-0015、ADR-0032）。

`run_zh_summary` 先讀論文的處理事實、問 `domain/processing.plan_for` 要不要做：

- 不做（被排除 → `not_applicable`；手填書目 → `not_applicable`；沒有摘要 → `no_input`）：
  直接回，不呼叫閘門、不寫任何列。
- 要做：照計畫的 `input_kind` 交給 LLM 閘門（`run_llm_job`），語言 `zh-Hant`，domain 驗證是
  `ZhSummaryValidator(門檻)`，預算檢查一律 `BudgetGuard(...)`。寫不寫、寫成什麼狀態都由閘門與
  `services/ai_results.py` 決定；內容只從驗證過的輸出得出。

指定論文的試跑（2.10）與 Batch（2.9）之後呼叫這裡或照同樣的方式問 `plan_for`。
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from radar.domain.ai_outputs import GatewayConfig, LlmTask, OutputLang, RunKey
from radar.domain.budget import BudgetConfig
from radar.domain.permissions import Action, require
from radar.domain.processing import ProcessingTask, TaskPlan, plan_for
from radar.domain.validators.zh_summary import ZhSummaryRules, ZhSummaryValidator
from radar.domain.viewer import SystemViewer, Viewer
from radar.ports import LlmProvider
from radar.repo.db import Database
from radar.repo.papers import find_paper_processing_facts
from radar.services.budget import BudgetGuard
from radar.services.llm_gateway import GatewayResult, LlmJob, run_llm_job


@dataclass(frozen=True)
class ZhSummaryRun:
    """一次 `run_zh_summary` 的結果：`plan` 是 `plan_for` 對中文重點的回答；`result` 只有真的交給
    閘門時才有（不做時是 None）。"""

    plan: TaskPlan
    result: GatewayResult | None


def run_zh_summary(
    actor: Viewer,
    database: Database,
    provider: LlmProvider,
    config: GatewayConfig,
    budget: BudgetConfig,
    rules: ZhSummaryRules,
    paper_id: uuid.UUID,
    *,
    clock: Callable[[], datetime],
    confirmed_key: RunKey | None = None,
) -> ZhSummaryRun:
    """跑一篇論文的中文重點（見模組說明）。`clock` 給預算檢查算台北月份用。
    `confirmed_key`：試跑確認過的鍵，原樣交給閘門核對（Story 2.10；見 `run_llm_job`）。

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
    plan = plan_for(facts).for_task(ProcessingTask.ZH_SUMMARY)
    if plan.input_kind is None:
        return ZhSummaryRun(plan, None)
    result = run_llm_job(
        actor,
        database,
        provider,
        config,
        LlmJob(
            paper_id=paper_id,
            task=LlmTask.ZH_SUMMARY,
            input_kind=plan.input_kind,
            lang=OutputLang.ZH_HANT,
            validate_output=ZhSummaryValidator(rules),
        ),
        budget_allows=BudgetGuard(database, budget, clock),
        confirmed_key=confirmed_key,
    )
    return ZhSummaryRun(plan, result)
