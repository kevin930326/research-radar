"""收錄過濾：抓到的論文哪些放進暫存表（PRD FR-8、ADR-0021 決定第 2 點、ADR-0025）。

抓取只篩期刊＋年份；進暫存表前才用這裡過濾：只收 article／review、有 DOI、主要出處是這本
期刊、出版年 ≥ 起始年的。出版年比今年晚照收（OpenAlex 先登記的下一年度論文）。
`paratext`（例如 `Editorial Board`）因此不進暫存表，不另外加排除規則（Kevin 2026-11-14，D10）。

純函式：不連網路、不碰資料庫、不讀設定。被排除規則標記的（`domain/exclusion.py`）照樣進暫存表，
那是另一件事。
"""

from enum import StrEnum

from radar.domain.papers import ParsedPaper

# 收錄的文章類型（OpenAlex 的 `type`）。
COLLECTED_WORK_TYPES = frozenset({"article", "review"})


class CollectionSkip(StrEnum):
    """沒有收錄的原因（依檢查順序）。"""

    WORK_TYPE = "work_type"  # 不是 article／review
    NO_DOI = "no_doi"
    OTHER_JOURNAL = "other_journal"  # 主要出處不是這本期刊
    NO_PUBLICATION_YEAR = "no_publication_year"
    BEFORE_START_YEAR = "before_start_year"


def collection_skip_reason(
    paper: ParsedPaper, *, journal_issn_l: str, start_year: int
) -> CollectionSkip | None:
    """`paper` 不收錄的原因；要收錄回 None。依序檢查：類型、DOI、期刊、出版年、起始年。

    `journal_issn_l` 是正在抓的那本期刊的 ISSN-L（已正規化）。
    """
    if paper.work_type not in COLLECTED_WORK_TYPES:
        return CollectionSkip.WORK_TYPE
    if paper.doi is None:
        return CollectionSkip.NO_DOI
    if paper.journal_issn_l != journal_issn_l:
        return CollectionSkip.OTHER_JOURNAL
    if paper.publication_year is None:
        return CollectionSkip.NO_PUBLICATION_YEAR
    if paper.publication_year < start_year:
        return CollectionSkip.BEFORE_START_YEAR
    return None
