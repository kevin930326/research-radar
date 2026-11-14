"""統計查詢（AD-5、ADR-0011「哪些論文算數」「計數方塊」、ADR-0027）：只讀，不寫任何表。

- 所有統計只從 view `counted_paper` 出發（`in_scope`、沒有 `excluded_reason`、`first_snapshot_id`
  指向已發佈的快照），不直接查 `paper`、不 import `radar.repo.papers`。統計查詢只准放在這裡
  （及之後的 `relations.py`、`keyword_stats.py`），架構測試守著
  （`tests/arch/test_stats_queries.py`）。
- **每年全部篇數只有一個查詢**：`list_cube_counts`。之後 `relations.py`、`keyword_stats.py` 要分母時
  呼叫它，不另外數。
- 「有摘要」只看 `paper_abstract` 裡 `is_current` 的那一筆（AD-7；同一篇最多一筆，部分唯一索引
  保證），所以左接之後每篇論文仍只有一列。
- 數字每次讀取時當場算（ADR-0011「乙」），不存。
"""

from sqlalchemy import Connection, text

from radar.domain.cube import CountedCell
from radar.domain.papers import AbstractSource
from radar.domain.viewer import Viewer
from radar.repo.visibility import visible_papers


def list_cube_counts(viewer: Viewer, connection: Connection) -> list[CountedCell]:
    """`viewer` 看得到的算數論文，依（期刊 ISSN-L, 出版年, 目前的摘要來源）數篇數。

    沒有目前的摘要的論文，來源是 None。排序：ISSN-L、年份、來源（None 在最後）。
    """
    condition = visible_papers(viewer, alias="c")
    rows = connection.execute(
        text(
            "SELECT c.journal_issn_l, c.publication_year, a.source, count(*)"
            " FROM counted_paper AS c"
            " LEFT JOIN paper_abstract AS a ON a.paper_id = c.id AND a.is_current"
            f" WHERE {condition}"
            " GROUP BY 1, 2, 3"
            " ORDER BY 1, 2, 3"
        )
    ).all()
    return [
        CountedCell(
            journal_issn_l=str(issn_l),
            year=int(year),
            abstract_source=None if source is None else AbstractSource(source),
            n=int(n),
        )
        for issn_l, year, source, n in rows
    ]
