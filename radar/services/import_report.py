"""匯入報表的讀取：`radar weekly-run report` 呼叫（epics Story 1.9、ADR-0026）。

一個短交易讀 `weekly_run`、每本期刊的進度與快照，交給 `domain/import_report.py` 組報表。
不查 `paper` 等論文表：論文的篇數只由之後的 `repo/stats.py` 算（AD-5，Story 1.10）。
"""

import uuid

from radar.domain.domain_config import JournalsConfig
from radar.domain.import_report import ImportBaseline, ImportReport, build_import_report
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.snapshots import get_snapshot_of_weekly_run
from radar.repo.weekly_runs import find_latest_weekly_run, get_weekly_run, list_weekly_run_journals


def get_import_report(
    viewer: SystemViewer,
    database: Database,
    weekly_run_id: uuid.UUID | None,
    *,
    journals: JournalsConfig,
    baseline: ImportBaseline,
) -> ImportReport | None:
    """這一次每週更新（`weekly_run_id` 是 None＝最新的一筆）的匯入報表；找不到就回 None。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`（repo 的讀取函式在碰連線之前丟）。
    """
    with database.transaction() as connection:
        if weekly_run_id is None:
            run = find_latest_weekly_run(viewer, connection)
        else:
            run = get_weekly_run(viewer, connection, weekly_run_id)
        if run is None:
            return None
        progress = list_weekly_run_journals(viewer, connection, run.id)
        snapshot = get_snapshot_of_weekly_run(viewer, connection, run.id)
    return build_import_report(run, progress, snapshot, journals, baseline)
