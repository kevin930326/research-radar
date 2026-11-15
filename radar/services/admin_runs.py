"""管理者查 AI 結果與依摘要來源移除衍生產出（`radar runs show`／`remove-by-source`，Story 2.12、
FR-11、AD-26、ADR-0044）。重跑在 `services/run_retry.py`；改版預估在 `services/llm_batches.py`
（放這裡會和它互相 import）。

- `get_paper_ai_report`（只讀）：論文 ID 或 DOI → 三個 AI 區塊（`services/ai_blocks.get_ai_blocks`，
  不自己判斷狀態）＋這篇三個工作的歷次 run（`repo/ai_outputs.list_paper_ai_history`）。
- `preview_source_removal`（只讀，一個交易）：送出時用了這個來源的摘要的表頭（各表各狀態）與細項、
  還在跑的 run、目前摘要仍是這個來源的論文、來源不明的表頭。
- `remove_source_ai_outputs`：一個交易：先拿固定名稱的交易級 advisory lock（`source_removal`，兩個
  同時執行時一個等另一個做完）→ 鎖內重查擋下條件（有這個來源的 running run，或有論文的目前摘要仍是
  這個來源：刪了也會再長出來）→ 有就什麼都不刪、不寫稽核 → 沒有就先細項後表頭直接刪（不複製被刪的
  內容）→ 有刪到才寫一筆 `audit_event`（系統身分，`detail` 只有來源與各表計數）。中途丟例外整個
  交易退回。刪完再跑一次刪 0 列、不寫稽核。

`derivation_run`、`llm_call`、`batch_item`、構面、向量、`paper_abstract`、人工判斷事件與稽核都不動；
標題輸入與其他來源的表頭也不動。日誌只記來源與計數。
"""

import uuid
from datetime import datetime

import structlog

from radar.domain.admin_runs import (
    PaperAiReport,
    SourceRemovalPreview,
    SourceRemovalResult,
    removal_blocks,
    source_removal_audit,
)
from radar.domain.fingerprints import named_lock_key
from radar.domain.papers import AbstractSource
from radar.domain.permissions import Action, require
from radar.domain.trial_runs import TrialTargets
from radar.domain.viewer import SystemViewer, Viewer
from radar.repo.ai_outputs import (
    count_ai_results_by_abstract_source,
    delete_ai_results_by_abstract_source,
    list_paper_ai_history,
)
from radar.repo.audit import create_audit_event
from radar.repo.db import Database
from radar.repo.llm_ledger import count_running_runs_by_abstract_source
from radar.repo.papers import count_papers_with_current_abstract_source
from radar.services.ai_blocks import get_ai_blocks
from radar.services.trial_runs import TrialPapersNotFoundError, TrialSetup, resolve_trial_papers

log = structlog.get_logger(__name__)

# 依來源移除的全域鎖（交易級，交易結束自動放掉）。
SOURCE_REMOVAL_LOCK_KEY = named_lock_key("source_removal")


def get_paper_ai_report(
    actor: Viewer,
    database: Database,
    setup: TrialSetup,
    target: uuid.UUID | str,
    *,
    now: datetime,
) -> PaperAiReport | None:
    """一篇論文（ID 或正規化後的 DOI）的三個 AI 區塊與歷次 run；找不到回 None。只讀。`setup` 只用
    閘門設定（模型）、預算、分類表與情境值清單（判定「目前這組鍵」）；`now` 給預算算台北月份。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: `now` 沒有時區。
        PromptIntegrityError: 有工作沒有登記目前的 prompt 版本。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    try:
        selection = resolve_trial_papers(actor, database, TrialTargets((target,)))
    except TrialPapersNotFoundError:
        return None
    paper = selection.papers[0]
    blocks = get_ai_blocks(
        SystemViewer(),
        database,
        setup.gateway,
        setup.budget,
        setup.taxonomy,
        setup.context_values,
        paper.paper_id,
        now=now,
    )
    if blocks is None:  # 查到之後被刪了
        return None
    with database.transaction() as connection:
        history = list_paper_ai_history(SystemViewer(), connection, paper.paper_id)
    return PaperAiReport(paper.paper_id, paper.dois, blocks, tuple(history))


def preview_source_removal(
    actor: Viewer, database: Database, source: AbstractSource
) -> SourceRemovalPreview:
    """`source` 衍生的 AI 產出有多少、刪得刪不了（一個只讀交易）。不寫任何列。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: `source` 不是摘要來源（不碰資料庫）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    source = AbstractSource(source)
    viewer = SystemViewer()
    with database.transaction() as connection:
        counts = count_ai_results_by_abstract_source(viewer, connection, source)
        running = count_running_runs_by_abstract_source(viewer, connection, source)
        current = count_papers_with_current_abstract_source(viewer, connection, source)
    return SourceRemovalPreview(source, counts, running, current)


def remove_source_ai_outputs(
    actor: Viewer, database: Database, source: AbstractSource
) -> SourceRemovalResult:
    """在一個交易裡刪掉 `source` 衍生的 AI 表頭（四種狀態）與細項，有刪到才寫一筆稽核（見模組
    說明）。被擋下時什麼都不刪、不寫稽核，結果的 `blocks` 不是空的。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: `source` 不是摘要來源（不碰資料庫）。
        Exception: 資料庫錯誤原樣丟出（整個交易退回，一列都沒少）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    source = AbstractSource(source)
    viewer = SystemViewer()
    with database.transaction() as connection:
        Database.transaction_lock(connection, SOURCE_REMOVAL_LOCK_KEY)
        running = count_running_runs_by_abstract_source(viewer, connection, source)
        current = count_papers_with_current_abstract_source(viewer, connection, source)
        blocks = removal_blocks(running, current)
        if blocks:
            log.warning(
                "依來源移除被擋下，沒有刪任何一列",
                source=source.value,
                running_runs=running,
                current_papers=current,
            )
            return SourceRemovalResult(source, blocks, running, current)
        deleted = delete_ai_results_by_abstract_source(connection, source)
        audit_id = None
        if any(deleted.values()):
            audit_id = create_audit_event(connection, source_removal_audit(source, deleted))
    log.info("依來源移除 AI 產出", source=source.value, deleted=deleted)
    return SourceRemovalResult(source, (), running, current, deleted, audit_id)
