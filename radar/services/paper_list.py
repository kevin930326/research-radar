"""找論文清單（Story 3.13、AD-19、ADR-0011「點數字列出論文」、FR-7）。

一個唯讀、REPEATABLE READ 的交易（`Database.read_only_snapshot_transaction`）裡依序讀：目前已發佈的
快照 → 這一頁的 id（多讀一篇判斷有沒有下一頁）→ 符合條件的總篇數 → 這一頁的論文列
（`repo/paper_rows.find_paper_rows`，照 id 的順序組）。四句讀的是同一份資料狀態：中間每週更新剛好
發佈、或把一篇標成排除並 commit，這次回應也看不到，`items`、`total`、`snapshot` 彼此對得上
（Story 3.13 審查 C1）。

論文列讀不到的跳過，記一筆只有 id 的警告（同一份資料狀態下不該發生，留著當保險）；日誌不帶標題
這類內容。

`list_ranked_papers`：搜尋與找相似論文（`services/retrieval.retrieve` 排好的名次，Story 4.1 PR B）
那一頁的論文列，在一個唯讀、REPEATABLE READ 的交易裡讀。名次是 `retrieve` 另一個交易排的（查詢向量
要先呼叫 Workers AI，外部呼叫不在交易裡），中間被排除或看不到的那一篇讀不到、跳過，記一筆只有 id 的
警告。快照不在這裡重讀：回應用 `retrieve` 排名時讀到的那一個（`RetrievalPage.snapshot`），
`this_week` 的篩選和回應的 `snapshot` 才是同一個（PR B 審查 M3）。
"""

import uuid
from collections.abc import Sequence

import structlog

from radar.domain.paper_list import (
    PAPER_LIST_PAGE_SIZE,
    PaperListCursor,
    PaperListFilters,
    PaperListPage,
    encode_paper_list_cursor,
)
from radar.domain.paper_rows import PaperRowFacts
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
    with database.read_only_snapshot_transaction() as connection:
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


def list_ranked_papers(
    viewer: MemberViewer, database: Database, paper_ids: Sequence[uuid.UUID]
) -> tuple[PaperRowFacts, ...]:
    """`paper_ids`（已照名次排好）裡 `viewer` 看得到的論文列（照同一個順序；讀不到的跳過）。"""
    with database.read_only_snapshot_transaction() as connection:
        rows = find_paper_rows(viewer, connection, paper_ids)
    for paper_id in paper_ids:
        if paper_id not in rows:
            log.warning("搜尋結果的一篇讀不到論文列，跳過", paper_id=str(paper_id))
    return tuple(rows[paper_id] for paper_id in paper_ids if paper_id in rows)
