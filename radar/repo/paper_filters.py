"""計數方塊、找論文清單與檢索共用的 SQL 片段（Story 3.13、4.1、AD-19「點得回去」、ADR-0011「點數字
列出論文」、ADR-0005）。

`repo/stats.list_cube_key_counts`（計數方塊的 `topic`、`method` 格）與 `repo/paper_list.py`（清單的
主題、方法條件）都從這裡組，所以「論文有這個主題／方法」只有一個定義：格子裡的篇數和用同樣條件查出來
的清單篇數一定一樣（`tests/integration/test_paper_list_click_back.py` 守著）。

清單與檢索（`repo/retrieval.py`）的期刊、年份、主題、方法、`has_abstract`、`this_week` 條件也只組
一次（`_field_conditions`，Story 4.1 從 `repo/paper_list.py` 搬過來，寫法一字不改）：兩邊同一套條件
語意。它只組 SQL 片段與參數、不讀資料，repo 的公開名稱又要用登記的前綴（讀取類帶 `viewer`），
所以用底線開頭，只給這兩個 repo 模組用。

- `CLASSIFICATION_KEYS`：論文的有效分類 key，一列一個（`paper_id`, `field`, `key`）。讀 view
  `effective_classification`（人修正過的有效值，AD-10），把 `keys` 陣列展開。`classification_label`
  對（表頭, field, key）唯一、view 的人工值已去重，所以同一篇同一個 key 只有一列，接上去數不會重複。
  主題的 `unclassified` 是一般的 key；沒有方法的論文沒有 `method` 的列（方法沒有 `unclassified`）。
- view 不接可見範圍：用的查詢要自己接 `repo/visibility.py` 的條件。
- 用法：放進括號當子查詢，例如 `JOIN ({CLASSIFICATION_KEYS}) AS classification_key ON …`。
"""

import re
import uuid
from typing import Any, Final

from radar.domain.paper_list import MatchMode, PaperListFilters
from radar.domain.taxonomy import TaxonomyField

CLASSIFICATION_KEYS: Final = (
    "SELECT ec.paper_id, ec.field, k.key"
    " FROM effective_classification AS ec CROSS JOIN LATERAL unnest(ec.keys) AS k(key)"
)

# 別名直接組進 SQL，所以只收小寫識別字（同 `repo/visibility.py`）。
_ALIAS = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def _all_keys_condition(alias: str, field: TaxonomyField, param: str) -> str:
    """論文的有效分類在 `field` 同時有 `:param`（key 的陣列，不重複）裡的每一個 key（「且」）；
    `:{param}_count` 是陣列的長度。

    寫成 `id = ANY(ARRAY(子查詢))`、不寫 `id IN (子查詢)`：`IN` 的寫法規劃器把 `HAVING` 估成 1 列，
    選了巢狀迴圈、一再重算整個有效分類（一萬篇要 20 秒以上，量過）；`ARRAY(...)` 一定只算一次。
    """
    return (
        f"{alias}.id = ANY(ARRAY(SELECT classification_key.paper_id"
        f" FROM ({CLASSIFICATION_KEYS}) AS classification_key"
        f" WHERE classification_key.field = '{TaxonomyField(field).value}'"
        f" AND classification_key.key = ANY(:{param})"
        f" GROUP BY classification_key.paper_id HAVING count(*) = :{param}_count))"
    )


def _any_key_condition(alias: str, field: TaxonomyField, param: str) -> str:
    """論文的有效分類在 `field` 有 `:param`（key 的陣列）裡的任一個 key（「或」）。寫法和
    `_all_keys_condition` 一樣包成 `ARRAY(...)`（只算一次），差在不分組、不數個數。"""
    return (
        f"{alias}.id = ANY(ARRAY(SELECT DISTINCT classification_key.paper_id"
        f" FROM ({CLASSIFICATION_KEYS}) AS classification_key"
        f" WHERE classification_key.field = '{TaxonomyField(field).value}'"
        f" AND classification_key.key = ANY(:{param})))"
    )


def _field_conditions(
    filters: PaperListFilters, current_snapshot_id: uuid.UUID | None, *, alias: str
) -> tuple[list[str], dict[str, Any]]:
    """期刊、年份、主題、方法、`has_abstract`、`this_week` 的 SQL 條件（每一項一個片段，呼叫的一方
    以 AND 接）與參數。`alias` 是查詢裡論文（`paper` 或 `counted_paper`）的別名；`filters.scope`
    不看（範圍由呼叫的一方自己組）。參數名稱固定：`journals`、`years`、`topics`、`topics_count`、
    `methods`、`methods_count`、`snapshot_id`。

    Raises:
        ValueError: `alias` 不是小寫識別字。
    """
    if not _ALIAS.fullmatch(alias):
        raise ValueError(f"別名只能是小寫識別字：{alias!r}")
    parts: list[str] = []
    params: dict[str, Any] = {}
    if filters.journal_issn_ls:
        parts.append(f"{alias}.journal_issn_l = ANY(:journals)")
        params["journals"] = list(filters.journal_issn_ls)
    if filters.years:
        parts.append(f"{alias}.publication_year = ANY(:years)")
        params["years"] = list(filters.years)
    for field, param, keys, mode in (
        (TaxonomyField.TOPIC, "topics", filters.topics, filters.topic_match),
        (TaxonomyField.METHOD, "methods", filters.methods, filters.method_match),
    ):
        if not keys:
            continue
        params[param] = list(keys)
        if mode is MatchMode.ALL:  # 已去重（`PaperListFilters`），個數就是要同時符合的 key 數
            parts.append(_all_keys_condition(alias, field, param))
            params[f"{param}_count"] = len(keys)
        else:
            parts.append(_any_key_condition(alias, field, param))
    if filters.has_abstract is not None:
        current = (
            "EXISTS (SELECT 1 FROM paper_abstract AS listed_abstract"
            f" WHERE listed_abstract.paper_id = {alias}.id AND listed_abstract.is_current)"
        )
        parts.append(current if filters.has_abstract else f"NOT {current}")
    if filters.this_week:
        if current_snapshot_id is None:
            parts.append("FALSE")  # 還沒有已發佈的快照：沒有「本週新增」
        else:
            parts.append(f"{alias}.first_snapshot_id = :snapshot_id")
            params["snapshot_id"] = current_snapshot_id
    return parts, params
