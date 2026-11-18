"""找論文清單（Story 3.13、AD-19、ADR-0011「點數字列出論文」、FR-7）。

一個交易裡依序讀：目前已發佈的快照 → 這一頁的 id（多讀一篇判斷有沒有下一頁）→ 符合條件的總篇數 →
這一頁的論文列（`repo/paper_rows.find_paper_rows`，照 id 的順序組）。總數和這一頁是兩句查詢，中間
剛好有每週更新發佈時可能差一點（和文獻庫清單一樣）。

論文列讀不到的（兩句之間變成看不到）跳過，記一筆只有 id 的警告；日誌不帶標題這類內容。
"""

import structlog

from radar.domain.paper_list import (
    PAPER_LIST_PAGE_SIZE,
    PaperListCursor,
    PaperListFilters,
    PaperListPage,
    encode_paper_list_cursor,
)
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.repo.paper_list import count_paper_list, list_paper_list_ids
from radar.repo.paper_rows import find_paper_rows
from radar.repo.snapshots import find_latest_published_snapshot

log = structlog.get_logger(__name__)


def list_papers(
    viewer: MemberViewer,
    database: Database,
    filters: PaperListFilters,
    *,
    after: PaperListCursor | None,
) -> PaperListPage:
    """符合 `filters`、`viewer` 看得到的論文的一頁（從 `after` 之後；None 是第一頁），加上總篇數與
    目前已發佈的快照。"""
    with database.transaction() as connection:
        # 快照編號與截至時間是全站共用的，用系統身分讀（和論文頁一樣）。
        snapshot = find_latest_published_snapshot(SystemViewer(), connection)
        snapshot_id = None if snapshot is None else snapshot.id
        found = list_paper_list_ids(
            viewer,
            connection,
            filters,
            current_snapshot_id=snapshot_id,
            after=after,
            limit=PAPER_LIST_PAGE_SIZE + 1,
        )
        page, more = found[:PAPER_LIST_PAGE_SIZE], len(found) > PAPER_LIST_PAGE_SIZE
        total = count_paper_list(viewer, connection, filters, current_snapshot_id=snapshot_id)
        rows = find_paper_rows(viewer, connection, [paper_id for paper_id, _year in page])
    for paper_id, _year in page:
        if paper_id not in rows:
            log.warning("找論文清單的一篇讀不到論文列，跳過", paper_id=str(paper_id))
    next_cursor = None
    if more:
        last_id, last_year = page[-1]
        next_cursor = encode_paper_list_cursor(PaperListCursor(year=last_year, paper_id=last_id))
    return PaperListPage(
        rows=tuple(rows[paper_id] for paper_id, _year in page if paper_id in rows),
        next_cursor=next_cursor,
        total=total,
        snapshot=snapshot,
    )
