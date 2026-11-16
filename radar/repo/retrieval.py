"""檢索的 SQL（AD-11、AD-12、ADR-0005）：向量查詢與全文檢索的 SQL 只寫在這個模組。

Story 2.8 先放向量查詢：任何向量查詢都一定要帶 `model_id`（函式簽名強制，沒有預設值），只比同一個
模型的向量；兩個模型的向量不會混在一起比。融合兩條腿、scope、相似度下限等由 Epic 4 的
`services/retrieval.py` 的 `retrieve()` 接上。

查詢的距離運算式寫成 `vector::halfvec(維度) <=> 查詢向量`，和遷移建的 HNSW 部分索引同一個運算式，
再加 `model_id` 條件，才用得到那個模型的索引（integration 測試守著）。ORDER BY 只放這一個距離，
是 HNSW 原生支援的「ORDER BY 距離 LIMIT n」寫法。HNSW 是近似搜尋：
索引一次給 `hnsw.ef_search`（預設 40）個候選，再套看得到、沒被排除的條件，篇數可能比 `limit` 少；
要補足（pgvector 0.8 的 iterative scan）由 Epic 4 的 `retrieve()` 決定。
"""

import math
from collections.abc import Sequence

from sqlalchemy import Connection, text

from radar.domain.embeddings import PAPER_ITEM_TYPE, VectorMatch, halfvec_literal
from radar.domain.viewer import Viewer
from radar.repo.visibility import visible_papers


def search_paper_vectors(
    viewer: Viewer,
    connection: Connection,
    *,
    model_id: str,
    vector: Sequence[float],
    limit: int,
) -> list[VectorMatch]:
    """和 `vector` 最接近的論文（餘弦距離由小到大，最多 `limit` 篇），只看 `model_id` 的向量。

    只回 `viewer` 看得到、沒有被排除的論文（被排除的非研究文章不進任何檢索，AD-30）。

    Raises:
        TypeError: `model_id` 不是非空字串。
        LookupError: `model_id` 沒有登記在 `embedding_model`。
        ValueError: `vector` 的長度和登記的維度不同、有非有限的數字，或 `limit` 不是正整數。
    """
    if type(model_id) is not str or not model_id:
        raise TypeError("向量查詢一定要指定 model_id")
    if type(limit) is not int or limit < 1:
        raise ValueError("limit 要是正整數")
    if any(not math.isfinite(float(value)) for value in vector):
        raise ValueError("查詢向量有非有限的數字")
    dimensions = connection.execute(
        text("SELECT dimensions FROM embedding_model WHERE model_id = :model_id"),
        {"model_id": model_id},
    ).scalar_one_or_none()
    if dimensions is None:
        raise LookupError("這個模型沒有登記")
    size = int(dimensions)
    if len(vector) != size:
        raise ValueError(f"查詢向量的維度不對（要 {size}，收到 {len(vector)}）")
    visible = visible_papers(viewer, alias="paper")
    # 維度是從資料庫讀出來的整數，直接組進 SQL（型別修飾不能用參數）。
    distance = f"e.vector::halfvec({size}) <=> CAST(:query AS halfvec({size}))"
    rows = connection.execute(
        text(
            f"SELECT paper.id, {distance} AS distance"
            " FROM embedding AS e JOIN paper ON paper.id = e.item_id"
            f" WHERE {visible} AND e.item_type = :item_type AND e.model_id = :model_id"
            " AND paper.excluded_reason IS NULL"
            f" ORDER BY {distance} LIMIT :limit"
        ),
        {
            "query": halfvec_literal(vector),
            "item_type": PAPER_ITEM_TYPE,
            "model_id": model_id,
            "limit": limit,
        },
    ).all()
    return [VectorMatch(paper_id=row[0], distance=float(row[1])) for row in rows]
