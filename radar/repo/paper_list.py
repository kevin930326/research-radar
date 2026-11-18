"""找論文清單的查詢（Story 3.13、AD-19、ADR-0011「點數字列出論文」）。只讀，不寫任何表。

- 範圍：`tracked` 從 view `counted_paper` 出發（和計數方塊同一群論文）；`lab`、`personal` 從
  `paper` 出發，加上「沒有被排除」（`excluded_reason IS NULL`），`lab` 用
  `visibility.visible_lab_papers`、`personal` 要在自己的文獻庫
  （`visibility.visible_library_items`）。三種都再接 `visibility.visible_papers(viewer)`。
- 條件：期刊、年份多值是「或」（`= ANY`）；每個主題、每個方法各一個 `EXISTS`（多值是「且」），
  「論文有這個 key」用 `repo/paper_filters.CLASSIFICATION_KEYS`（和計數方塊的 `topic`、`method`
  格同一個片段）；有摘要＝有 `is_current` 的摘要（和計數方塊的 `with_abstract` 同一個定義）；
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
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import Viewer
from radar.repo.paper_filters import CLASSIFICATION_KEYS
from radar.repo.visibility import visible_lab_papers, visible_library_items, visible_papers

# 查詢裡論文（`counted_paper` 或 `paper`）的別名；兩種來源用同一個，條件才能共用。
_ALIAS: Final = "listed"
_ORDER: Final = f" ORDER BY {_ALIAS}.publication_year DESC NULLS LAST, {_ALIAS}.id DESC"


def _key_condition(field: TaxonomyField, param: str) -> str:
    """論文的有效分類在 `field` 有 `:param` 這個 key。"""
    return (
        f"EXISTS (SELECT 1 FROM ({CLASSIFICATION_KEYS}) AS classification_key"
        f" WHERE classification_key.paper_id = {_ALIAS}.id"
        f" AND classification_key.field = '{TaxonomyField(field).value}'"
        f" AND classification_key.key = :{param})"
    )


def _conditions(
    viewer: Viewer, filters: PaperListFilters, current_snapshot_id: uuid.UUID | None
) -> tuple[str, dict[str, Any]]:
    """範圍與篩選的 SQL 條件（接在可見條件後面，以 AND 開頭）與參數。"""
    parts: list[str] = []
    params: dict[str, Any] = {}
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
    if filters.journal_issn_ls:
        parts.append(f"{_ALIAS}.journal_issn_l = ANY(:journals)")
        params["journals"] = list(filters.journal_issn_ls)
    if filters.years:
        parts.append(f"{_ALIAS}.publication_year = ANY(:years)")
        params["years"] = list(filters.years)
    for index, key in enumerate(filters.topics):
        parts.append(_key_condition(TaxonomyField.TOPIC, f"topic_{index}"))
        params[f"topic_{index}"] = key
    for index, key in enumerate(filters.methods):
        parts.append(_key_condition(TaxonomyField.METHOD, f"method_{index}"))
        params[f"method_{index}"] = key
    if filters.has_abstract is not None:
        current = (
            "EXISTS (SELECT 1 FROM paper_abstract AS listed_abstract"
            f" WHERE listed_abstract.paper_id = {_ALIAS}.id AND listed_abstract.is_current)"
        )
        parts.append(current if filters.has_abstract else f"NOT {current}")
    if filters.this_week:
        if current_snapshot_id is None:
            parts.append("FALSE")  # 還沒有已發佈的快照：沒有「本週新增」
        else:
            parts.append(f"{_ALIAS}.first_snapshot_id = :snapshot_id")
            params["snapshot_id"] = current_snapshot_id
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
