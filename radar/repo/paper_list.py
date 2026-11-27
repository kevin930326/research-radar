"""找論文清單的查詢（Story 3.13、AD-19、ADR-0011「點數字列出論文」）。只讀，不寫任何表。

- 範圍：`tracked` 從 view `counted_paper` 出發（和計數方塊同一群論文）；`lab`、`personal` 從
  `paper` 出發，加上「沒有被排除」（`excluded_reason IS NULL`），`lab` 用
  `visibility.visible_lab_papers`、`personal` 要在自己的文獻庫
  （`visibility.visible_library_items`）。三種都再接 `visibility.visible_papers(viewer)`。
- 條件：期刊、年份多值是「或」（`= ANY`）；主題、方法多值預設是「或」、`MatchMode.ALL` 時是「且」
  （Story 3.20、ADR-0011 的 2026-11-27 更新）：每個欄位只接一次
  `repo/paper_filters.CLASSIFICATION_KEYS`（這些條件由 `paper_filters._field_conditions` 組，和檢索
  共用，Story 4.1）（和計數方塊的 `topic`、`method` 格同一個片段），
  `key = ANY(:keys)`。「或」取有任一個 key 的論文（`DISTINCT`）；「且」依論文分組、
  `HAVING count(*) = 給的值的個數`（片段對同一篇同一個 key 只有
  一列，所以數到的個數就是符合的 key 數）。兩種結果都包成 `ARRAY(...)`，論文 `id = ANY(那個陣列)`：
  子查詢不接外面的論文，PostgreSQL 只算一次（InitPlan），再用主鍵找論文。原本每個值各一個
  `EXISTS`，每多一個值整個有效分類就多算一次（3 個主題＋2 個方法、一萬篇，一個請求約 1.2 秒；
  改寫後約 0.17 秒；審查 S1，ADR-0050）；有摘要＝有 `is_current` 的摘要（和計數方塊的
  `with_abstract` 同一個定義）；
  本週新增＝`first_snapshot_id` 是目前已發佈的快照（沒有快照時沒有結果）。
- 排序 `publication_year DESC NULLS LAST, id DESC`，keyset 分頁；出版年是 NULL 的那一段排在最後。
- 一頁的 id 與總數是兩個函式（同一個交易、兩句查詢），條件由同一個私有函式組。
- `count_paper_list` 是清單的總數，`tests/arch/test_stats_queries.py` 登記的唯一例外（清單要給
  `total`，數的是清單本身；和計數方塊對得上由「點得回去」的整合測試守著）。
"""

import uuid
from typing import Any, Final

from sqlalchemy import Connection, text

from radar.domain.paper_list import PaperListCursor, PaperListFilters, PaperListScope
from radar.domain.viewer import Viewer
from radar.repo.paper_filters import _field_conditions
from radar.repo.visibility import visible_lab_papers, visible_library_items, visible_papers

# 查詢裡論文（`counted_paper` 或 `paper`）的別名；兩種來源用同一個，條件才能共用。
_ALIAS: Final = "listed"
_ORDER: Final = f" ORDER BY {_ALIAS}.publication_year DESC NULLS LAST, {_ALIAS}.id DESC"


def _conditions(
    viewer: Viewer, filters: PaperListFilters, current_snapshot_id: uuid.UUID | None
) -> tuple[str, dict[str, Any]]:
    """範圍與篩選的 SQL 條件（接在可見條件後面，以 AND 開頭）與參數。篩選條件和檢索共用
    （`repo/paper_filters._field_conditions`，Story 4.1）。"""
    parts: list[str] = []
    match filters.scope:
        case PaperListScope.TRACKED:
            pass  # `counted_paper` 本身就排除了被排除的、範圍外的、快照沒發佈的
        case PaperListScope.LAB:
            parts += [
                f"{_ALIAS}.excluded_reason IS NULL",
                visible_lab_papers(viewer, alias=_ALIAS),
            ]
        case PaperListScope.PERSONAL:
            mine = visible_library_items(viewer, alias="listed_item")
            parts += [
                f"{_ALIAS}.excluded_reason IS NULL",
                "EXISTS (SELECT 1 FROM library_item AS listed_item"
                f" WHERE listed_item.paper_id = {_ALIAS}.id AND {mine})",
            ]
    fields, params = _field_conditions(filters, current_snapshot_id, alias=_ALIAS)
    parts += fields
    return "".join(f" AND {part}" for part in parts), params


def list_paper_list_ids(
    viewer: Viewer,
    connection: Connection,
    filters: PaperListFilters,
    *,
    current_snapshot_id: uuid.UUID | None,
    after: PaperListCursor | None,
    limit: int,
) -> list[tuple[uuid.UUID, int | None]]:
    """符合條件、`viewer` 看得到的論文的（id, 出版年），照 `publication_year DESC NULLS LAST,
    id DESC`，從 `after` 之後（None 是第一頁）最多 `limit` 篇。`current_snapshot_id` 是目前已發佈的
    快照（「本週新增」用；沒有就是 None）。

    Raises:
        ValueError: `limit` 不是正整數。
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("limit 要是正整數")
    visible = visible_papers(viewer, alias=_ALIAS)
    where, params = _conditions(viewer, filters, current_snapshot_id)
    keyset = ""
    if after is not None:
        params["after_id"] = after.paper_id
        if after.year is None:
            keyset = f" AND {_ALIAS}.publication_year IS NULL AND {_ALIAS}.id < :after_id"
        else:
            # 出版年是 NULL 的排在最後，所以永遠在有年份的位置之後。
            keyset = (
                f" AND ({_ALIAS}.publication_year < :after_year"
                f" OR ({_ALIAS}.publication_year = :after_year AND {_ALIAS}.id < :after_id)"
                f" OR {_ALIAS}.publication_year IS NULL)"
            )
            params["after_year"] = after.year
    params["limit"] = limit
    if filters.scope is PaperListScope.TRACKED:
        sql = (
            f"SELECT {_ALIAS}.id, {_ALIAS}.publication_year FROM counted_paper AS {_ALIAS}"
            f" WHERE {visible}{where}{keyset}{_ORDER} LIMIT :limit"
        )
    else:
        sql = (
            f"SELECT {_ALIAS}.id, {_ALIAS}.publication_year FROM paper AS {_ALIAS}"
            f" WHERE {visible}{where}{keyset}{_ORDER} LIMIT :limit"
        )
    rows = connection.execute(text(sql), params).all()
    return [(uuid.UUID(str(paper_id)), year) for paper_id, year in rows]


def count_paper_list(
    viewer: Viewer,
    connection: Connection,
    filters: PaperListFilters,
    *,
    current_snapshot_id: uuid.UUID | None,
) -> int:
    """符合條件、`viewer` 看得到的論文篇數（清單的 `total`；條件和 `list_paper_list_ids` 相同）。"""
    visible = visible_papers(viewer, alias=_ALIAS)
    where, params = _conditions(viewer, filters, current_snapshot_id)
    if filters.scope is PaperListScope.TRACKED:
        sql = f"SELECT count(*) FROM counted_paper AS {_ALIAS} WHERE {visible}{where}"
    else:
        sql = f"SELECT count(*) FROM paper AS {_ALIAS} WHERE {visible}{where}"
    return int(connection.execute(text(sql), params).scalar_one())
