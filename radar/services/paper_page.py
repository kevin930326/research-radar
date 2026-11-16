"""論文頁的資料（Story 3.6、ADR-0048）。只讀，不改任何資料。

`get_paper_page` 的順序：

1. 讀預算狀態（`services/budget.get_budget_status`，一個短交易、系統身分；預算是全站的事實），組好
   三種產出「現在」的設定（`services/ai_blocks.current_ai_block_settings`）。
2. 一個交易裡依序讀：論文欄位（`repo/papers.find_paper_page_facts`，一句帶可見條件的查詢；不存在與
   看不到走同一條路，回 None）→ 三個 AI 區塊（`services/ai_blocks.find_ai_blocks`，狀態只由
   `repo/ai_blocks.find_ai_block` 判定）→ 目前已發佈的快照（`repo/snapshots`，全站的事實，用系統
   身分讀）。

`setup` 由 api 行程啟動時讀好設定檔組成（services 不讀設定）。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from radar.domain.ai_blocks import AiProduct
from radar.domain.budget import BudgetConfig
from radar.domain.context_values import ContextValues
from radar.domain.domain_config import JournalsConfig
from radar.domain.paper_page import PaperPage, journal_key_for
from radar.domain.pricing import ModelsConfig
from radar.domain.taxonomy import Taxonomy
from radar.domain.viewer import SystemViewer, Viewer
from radar.repo.db import Database
from radar.repo.papers import find_paper_page_facts
from radar.repo.snapshots import find_latest_published_snapshot
from radar.services.ai_blocks import current_ai_block_settings, find_ai_blocks
from radar.services.budget import get_budget_status


@dataclass(frozen=True)
class PaperPageSetup:
    """論文頁要的設定（api 啟動時讀檔組好）：模型（判定「目前這組鍵」）、預算、分類表、情境值清單、
    追蹤的期刊（ISSN-L → 期刊代號）。"""

    models: ModelsConfig
    budget: BudgetConfig
    taxonomy: Taxonomy
    context_values: ContextValues
    journals: JournalsConfig


def get_paper_page(
    viewer: Viewer,
    database: Database,
    setup: PaperPageSetup,
    paper_id: uuid.UUID,
    *,
    now: datetime,
) -> PaperPage | None:
    """`viewer` 看得到的那篇論文的論文頁資料；不存在或看不到都回 None（不透露資料存在）。

    `now` 給預算算台北月份用。

    Raises:
        ValueError: `now` 沒有時區。
        PromptIntegrityError: 有工作沒有登記目前的 prompt 版本。
    """
    status = get_budget_status(SystemViewer(), database, setup.budget, now=now)
    settings = current_ai_block_settings(setup.models, status, setup.taxonomy, setup.context_values)
    with database.transaction() as connection:
        facts = find_paper_page_facts(viewer, connection, paper_id)
        if facts is None:
            return None
        blocks = find_ai_blocks(viewer, connection, paper_id, settings)
        if blocks is None:
            # 同一個交易裡兩句之間論文變成看不到（例如被刪掉）：照看不到回報。
            return None
        snapshot = find_latest_published_snapshot(SystemViewer(), connection)
    return PaperPage(
        facts=facts,
        journal_key=journal_key_for(setup.journals, facts.journal_issn_l),
        zh_summary=blocks[AiProduct.ZH_SUMMARY],
        classification=blocks[AiProduct.CLASSIFICATION],
        research_model=blocks[AiProduct.RESEARCH_MODEL],
        snapshot=snapshot,
    )
