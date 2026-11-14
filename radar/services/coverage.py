"""覆蓋率的讀取：`radar coverage` 呼叫（epics Story 1.10、ADR-0027）。只讀，不改任何資料。

一個交易裡讀「最新已發佈的快照 → 計數 → 再讀一次最新已發佈的快照」。交易是 READ COMMITTED，
每一句看到的資料可能不同；計數那一句夾在兩次讀快照之間，兩次是同一個快照，就表示計數時沒有新的
快照發佈，快照編號和數字是同一個時間點的。不同（中間剛好發佈了新快照）就重讀一輪。
"""

from radar.domain.coverage import CoverageReport, build_coverage_report
from radar.domain.cube import CountedCell
from radar.domain.domain_config import JournalsConfig
from radar.domain.snapshot import Snapshot
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.snapshots import find_latest_published_snapshot
from radar.repo.stats import list_cube_counts

# 快照前後各讀一次、對不上時最多讀幾輪。
MAX_ATTEMPTS = 3


def get_coverage(
    viewer: SystemViewer, database: Database, *, journals: JournalsConfig
) -> CoverageReport | None:
    """最新已發佈快照的覆蓋率報表；還沒有任何已發佈的快照就回 None。

    Raises:
        RuntimeError: 連續三輪，計數前後讀到的快照都不同。
        UnknownJournalError: 計數裡有設定沒有的期刊（原樣往上丟）。
        TypeError: `viewer` 不是 `SystemViewer`（repo 的讀取函式在碰連線之前丟）。
    """
    found: tuple[Snapshot, list[CountedCell]] | None = None
    with database.transaction() as connection:
        for _attempt in range(MAX_ATTEMPTS):
            before = find_latest_published_snapshot(viewer, connection)
            if before is None:
                return None
            cells = list_cube_counts(viewer, connection)
            after = find_latest_published_snapshot(viewer, connection)
            if after is not None and after.id == before.id:
                found = (before, cells)
                break
    if found is None:
        raise RuntimeError(f"連續 {MAX_ATTEMPTS} 輪，計數前後讀到的快照都不同；沒有產生覆蓋率")
    snapshot, cells = found
    return build_coverage_report(snapshot, cells, journals)
