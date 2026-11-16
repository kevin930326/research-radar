"""`radar runs report` 的拒收報表（Story 2.11、ADR-0043）：範圍內三個 Batch 工作的平常流程 run，
每篇每工作取最新的一筆（`repo/ai_outputs.list_run_outcomes`）。只讀，不寫任何列。"""

from radar.domain.permissions import Action, require
from radar.domain.run_reports import RunOutcomeRow, RunScope
from radar.domain.viewer import SystemViewer, Viewer
from radar.repo.ai_outputs import list_run_outcomes
from radar.repo.db import Database


def get_run_report(actor: Viewer, database: Database, scope: RunScope) -> list[RunOutcomeRow]:
    """範圍內每篇論文每個工作最新那一筆 run 的結果（照論文 id、工作排序）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    with database.transaction() as connection:
        return list_run_outcomes(SystemViewer(), connection, scope)
