"""計數方塊與找論文清單共用的 SQL 片段（Story 3.13、AD-19「點得回去」、ADR-0011「點數字列出
論文」）。

只放常數，不放函式與類別。`repo/stats.list_cube_key_counts`（計數方塊的 `topic`、`method` 格）與
`repo/paper_list.py`（清單的主題、方法條件）都從這裡組，所以「論文有這個主題／方法」只有一個定義：
格子裡的篇數和用同樣條件查出來的清單篇數一定一樣（`tests/integration/test_paper_list_click_back.py`
守著）。

- `CLASSIFICATION_KEYS`：論文的有效分類 key，一列一個（`paper_id`, `field`, `key`）。讀 view
  `effective_classification`（人修正過的有效值，AD-10），把 `keys` 陣列展開。`classification_label`
  對（表頭, field, key）唯一、view 的人工值已去重，所以同一篇同一個 key 只有一列，接上去數不會重複。
  主題的 `unclassified` 是一般的 key；沒有方法的論文沒有 `method` 的列（方法沒有 `unclassified`）。
- view 不接可見範圍：用的查詢要自己接 `repo/visibility.py` 的條件。
- 用法：放進括號當子查詢，例如 `JOIN ({CLASSIFICATION_KEYS}) AS classification_key ON …`。
"""

from typing import Final

CLASSIFICATION_KEYS: Final = (
    "SELECT ec.paper_id, ec.field, k.key"
    " FROM effective_classification AS ec CROSS JOIN LATERAL unnest(ec.keys) AS k(key)"
)
