"""計數方塊的讀取（Story 3.15、AD-19、ADR-0062）：成員版 `GET /api/v1/cube` 呼叫。只讀。

一個唯讀、REPEATABLE READ 的交易（`Database.read_only_snapshot_transaction`）裡依序讀目前已發佈的
快照與計數，兩句讀的是同一份資料狀態：中間每週更新剛好發佈，這次回應也看不到，計數和快照編號
對得上。計數經 `repo/stats.list_cube_counts` 用成員的 viewer 讀（AD-3）；快照編號是全站共用的，用
系統身分讀（和找論文清單一樣）。`params` 由呼叫的一方從設定組好傳進來（`repo` 不碰設定，AD-1）。
"""

from radar.domain.contracts import CubeResponse, SnapshotRef, TrendParams
from radar.domain.cube import build_cube_rows
from radar.domain.domain_config import JournalsConfig
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.repo.snapshots import find_latest_published_snapshot
from radar.repo.stats import list_cube_counts


def get_cube(
    viewer: MemberViewer,
    database: Database,
    *,
    journals: JournalsConfig,
    params: TrendParams,
) -> CubeResponse:
    """`viewer` 看得到的算數論文的計數方塊（`total`、`with_abstract`）、`params`、目前已發佈的快照。

    Raises:
        UnknownJournalError: 計數裡有設定沒有的期刊（原樣往上丟）。
    """
    with database.read_only_snapshot_transaction() as connection:
        snapshot = find_latest_published_snapshot(SystemViewer(), connection)
        cells = list_cube_counts(viewer, connection)
    return CubeResponse(
        rows=build_cube_rows(cells, journals),
        params=params,
        snapshot=None if snapshot is None else SnapshotRef.from_snapshot(snapshot),
    )
