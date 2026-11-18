"""論文列要的欄位（Story 3.16；3.13 的論文清單也用這裡）。只讀，不寫任何表。

- 只有 `find_paper_rows`：一句帶可見條件的查詢，一次讀好幾篇；回 dict（論文 id → 欄位），呼叫端照
  自己的順序排。看不到、不存在的論文不在結果裡（不先查存在，ADR-0002）。
- 不濾被排除的文章：要不要列由呼叫端的條件決定（文獻庫裡的一篇照列）。
- DOI 取最早記上的那一個（`external_id.created_at, id`），和論文頁的 `find_paper_page_facts` 一樣；
  `has_abstract` 看有沒有 `is_current` 的摘要；作者只取前 `PAPER_ROW_AUTHOR_LIMIT` 位＋總人數。
"""

import uuid
from collections.abc import Iterable
from typing import Final

from sqlalchemy import Connection, text

from radar.domain.identifiers import IdentifierScheme
from radar.domain.paper_rows import PAPER_ROW_AUTHOR_LIMIT, PaperRowFacts
from radar.domain.viewer import Viewer
from radar.repo.visibility import visible_papers

# 一次最多讀幾篇（清單一頁的量級；呼叫端分頁）。
_MAX_IDS: Final = 500


def find_paper_rows(
    viewer: Viewer, connection: Connection, paper_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, PaperRowFacts]:
    """`viewer` 看得到的這幾篇論文的論文列欄位（論文 id → 欄位）；看不到、不存在的不在結果裡。

    Raises:
        ValueError: `paper_ids` 有不是 UUID 的值，或超過一次能讀的篇數。
    """
    ids = list(dict.fromkeys(paper_ids))
    if not all(isinstance(paper_id, uuid.UUID) for paper_id in ids):
        raise ValueError("paper_ids 要是 UUID")
    if len(ids) > _MAX_IDS:
        raise ValueError(f"一次最多讀 {_MAX_IDS} 篇")
    visible = visible_papers(viewer, alias="paper")
    if not ids:
        return {}
    rows = (
        connection.execute(
            text(
                # `[1:n]` 的冒號前面是數字，text() 不會把它當成參數。
                "SELECT paper.id, paper.title,"
                f" paper.authors[1:{PAPER_ROW_AUTHOR_LIMIT}] AS authors,"
                " cardinality(paper.authors) AS author_count,"
                " paper.publication_year, paper.journal_issn_l, paper.in_scope,"
                " paper.is_retracted,"
                " (SELECT d.value_norm FROM external_id AS d"
                "  WHERE d.paper_id = paper.id AND d.scheme = :doi"
                "  ORDER BY d.created_at, d.id LIMIT 1) AS doi,"
                " EXISTS (SELECT 1 FROM paper_abstract AS a"
                "  WHERE a.paper_id = paper.id AND a.is_current) AS has_abstract"
                f" FROM paper WHERE {visible} AND paper.id = ANY(:ids)"
            ),
            {"ids": ids, "doi": IdentifierScheme.DOI.value},
        )
        .mappings()
        .all()
    )
    return {
        row["id"]: PaperRowFacts(
            paper_id=row["id"],
            title=row["title"],
            authors=tuple(row["authors"]),
            author_count=row["author_count"],
            publication_year=row["publication_year"],
            journal_issn_l=row["journal_issn_l"],
            doi=row["doi"],
            has_abstract=row["has_abstract"],
            in_scope=row["in_scope"],
            is_retracted=row["is_retracted"],
        )
        for row in rows
    }
