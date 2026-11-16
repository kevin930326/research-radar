"""一篇論文三個 AI 區塊的狀態（ADR-0015、ADR-0035、Story 2.7）。只讀，不改任何資料。

`current_ai_block_settings` 組「現在」的設定交給 `repo/ai_blocks.find_ai_block`：

- prompt 版本：`services/prompt_loader.current_prompt_version`（repo 不讀 prompt 登記）。
- 模型：`ModelsConfig.task_model(工作).model`。
- 設定檔版本：分類帶分類表版本、研究模型帶情境值清單版本（和閘門算 `input_sha256` 的方式一樣）。
- 預算：`services/budget.get_budget_status`（一個短交易讀本月帳本）的 `allows(工作)`。

`find_ai_blocks` 在呼叫端開好的交易裡判三塊（論文頁 `services/paper_page.py` 和論文欄位、快照
同一個交易）；`get_ai_blocks` 自己讀預算、自己開交易（`radar runs show` 用）。

`taxonomy`、`context_values` 由呼叫端在行程啟動時讀好傳進來（services 不讀設定）。論文頁 API
與之後的示範站匯出都呼叫這裡，不自己判斷狀態。
"""

import uuid
from collections.abc import Mapping
from datetime import datetime

from radar.domain.ai_blocks import AiBlock, AiBlockSettings, AiProduct
from radar.domain.ai_outputs import GatewayConfig
from radar.domain.budget import BudgetConfig, BudgetStatus
from radar.domain.context_values import ContextValues
from radar.domain.pricing import ModelsConfig
from radar.domain.taxonomy import Taxonomy
from radar.domain.viewer import SystemViewer, Viewer
from radar.repo.ai_blocks import find_ai_block
from radar.repo.db import Database, DbConnection
from radar.services.budget import get_budget_status
from radar.services.prompt_loader import current_prompt_version


def current_ai_block_settings(
    models: ModelsConfig,
    status: BudgetStatus,
    taxonomy: Taxonomy,
    context_values: ContextValues,
) -> dict[AiProduct, AiBlockSettings]:
    """三種產出各自「現在」的設定（prompt 版本、模型、設定檔版本、預算准不准）。

    Raises:
        PromptIntegrityError: 有工作沒有登記目前的 prompt 版本。
    """
    settings: dict[AiProduct, AiBlockSettings] = {}
    for product in AiProduct:
        task = product.llm_task
        settings[product] = AiBlockSettings(
            prompt_version=current_prompt_version(task),
            model=models.task_model(task).model,
            taxonomy_version=(
                taxonomy.version_label if product is AiProduct.CLASSIFICATION else None
            ),
            context_values_version=(
                context_values.version_label if product is AiProduct.RESEARCH_MODEL else None
            ),
            budget_allows=status.allows(task),
        )
    return settings


def find_ai_blocks(
    viewer: Viewer,
    connection: DbConnection,
    paper_id: uuid.UUID,
    settings: Mapping[AiProduct, AiBlockSettings],
) -> dict[AiProduct, AiBlock] | None:
    """在呼叫端開好的交易裡判定 `paper_id` 的三個 AI 區塊；論文不存在或 `viewer` 看不到回 None。

    Raises:
        KeyError: `settings` 缺了某一種產出。
        ValueError: `settings` 的設定檔版本和產出種類對不上。
    """
    blocks: dict[AiProduct, AiBlock] = {}
    for product in AiProduct:
        block = find_ai_block(viewer, connection, paper_id, product, settings[product])
        if block is None:
            return None
        blocks[product] = block
    return blocks


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
    settings = current_ai_block_settings(config.models, status, taxonomy, context_values)
    with database.transaction() as connection:
        return find_ai_blocks(viewer, connection, paper_id, settings)
