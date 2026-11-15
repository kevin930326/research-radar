"""一篇論文三個 AI 區塊的狀態（ADR-0015、ADR-0035、Story 2.7）。只讀，不改任何資料。

`get_ai_blocks` 組「現在」的設定交給 `repo/ai_blocks.find_ai_block`：

- prompt 版本：`services/prompt_loader.current_prompt_version`（repo 不讀 prompt 登記）。
- 模型：`GatewayConfig.models.task_model(工作).model`。
- 設定檔版本：分類帶分類表版本、研究模型帶情境值清單版本（和閘門算 `input_sha256` 的方式一樣）。
- 預算：`services/budget.get_budget_status`（一個短交易讀本月帳本）的 `allows(工作)`。

`taxonomy`、`context_values` 由呼叫端在行程啟動時讀好傳進來（services 不讀設定）。之後的論文頁 API
（Epic 3）與示範站匯出呼叫這裡，不自己判斷狀態。
"""

import uuid
from datetime import datetime

from radar.domain.ai_blocks import AiBlock, AiBlockSettings, AiProduct
from radar.domain.ai_outputs import GatewayConfig
from radar.domain.budget import BudgetConfig, BudgetStatus
from radar.domain.context_values import ContextValues
from radar.domain.taxonomy import Taxonomy
from radar.domain.viewer import SystemViewer, Viewer
from radar.repo.ai_blocks import find_ai_block
from radar.repo.db import Database
from radar.services.budget import get_budget_status
from radar.services.prompt_loader import current_prompt_version


def _settings(
    product: AiProduct,
    config: GatewayConfig,
    status: BudgetStatus,
    taxonomy: Taxonomy,
    context_values: ContextValues,
) -> AiBlockSettings:
    task = product.llm_task
    return AiBlockSettings(
        prompt_version=current_prompt_version(task),
        model=config.models.task_model(task).model,
        taxonomy_version=taxonomy.version_label if product is AiProduct.CLASSIFICATION else None,
        context_values_version=(
            context_values.version_label if product is AiProduct.RESEARCH_MODEL else None
        ),
        budget_allows=status.allows(task),
    )


def get_ai_blocks(
    viewer: Viewer,
    database: Database,
    config: GatewayConfig,
    budget: BudgetConfig,
    taxonomy: Taxonomy,
    context_values: ContextValues,
    paper_id: uuid.UUID,
    *,
    now: datetime,
) -> dict[AiProduct, AiBlock] | None:
    """`paper_id` 三個 AI 區塊（中文重點、分類、研究模型）現在的狀態；論文不存在或 `viewer` 看不到
    回 None。`now` 給預算算台北月份用。

    預算狀態是全站的事實（不分讀者），用系統身分讀帳本；三個區塊在同一個交易裡判定。

    Raises:
        ValueError: `now` 沒有時區。
        PromptIntegrityError: 有工作沒有登記目前的 prompt 版本。
    """
    status = get_budget_status(SystemViewer(), database, budget, now=now)
    settings = {
        product: _settings(product, config, status, taxonomy, context_values)
        for product in AiProduct
    }
    blocks: dict[AiProduct, AiBlock] = {}
    with database.transaction() as connection:
        for product in AiProduct:
            block = find_ai_block(viewer, connection, paper_id, product, settings[product])
            if block is None:
                return None
            blocks[product] = block
    return blocks
