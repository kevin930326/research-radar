"""檢索的 SQL（AD-11、AD-12、ADR-0005）：向量查詢與全文檢索的 SQL 只寫在這個模組。

任何向量查詢都一定要帶 `model_id`（函式簽名強制，沒有預設值），只比同一個模型的向量；兩個模型的向量
不會混在一起比（`tests/arch/test_embedding_rules.py` 守著）。

距離運算式寫成 `vector::halfvec(維度) <=> 查詢向量`，和遷移建的 HNSW 部分索引同一個運算式，再加
`model_id` 條件，才用得到那個模型的索引（integration 測試守著）。ORDER BY 只放這一個距離，是 HNSW
原生支援的「ORDER BY 距離 LIMIT n」寫法。

- `search_paper_vectors`（Story 2.8）：只有向量、沒有範圍與篩選，HNSW 一次給 `hnsw.ef_search`
  （預設 40）個候選，篇數可能比 `limit` 少。檢索的流程不用它（`services/retrieval.retrieve` 用
  `search_papers`）。
- `search_papers`（Story 4.1）：混合檢索。一條 SQL 裡：
  1. 條件（兩條腿相同）：先 `visible_papers(viewer)`、沒有被排除，再套範圍（`RetrievalScope`），
     再接期刊、年份、主題、方法、`has_abstract`、`this_week`（`repo/paper_filters._field_conditions`，
     和找論文清單同一套）。
  2. 向量腿：`personal` 先把自己文獻庫裡的向量撈出來（MATERIALIZED，不走索引）再精確比距離；其他
     範圍走 HNSW，交易內打開 iterative scan（`hnsw.iterative_scan = relaxed_order`，過濾後不夠時
     自動往下找）、`hnsw.ef_search` 設成每條腿的名額。取前 N 名（MATERIALIZED，relaxed_order 的
     順序在外層重排），距離超過下限換算的上限（`1 － 相似度下限`）的不算。
  3. 全文檢索腿（有查詢文字時）：`paper_search_document.document @@ websearch_to_tsquery('english',
     查詢)`，`ts_rank_cd` 由高到低取前 N 名。中文查詢在這條腿通常找不到東西（預期的）。
  4. RRF：兩條腿 FULL OUTER JOIN，分數＝Σ 1 ÷ (k ＋ 名次)（numeric，精確比大小）；同分時名次比較前
     面的優先，再依論文 id。
  回融合後全部的結果（最多 2 × N 篇），分頁與 `total` 由 service 切（同一句查詢，兩者一定對得上）。
  找相似論文：沒有查詢文字（只有向量腿），`exclude_paper_id` 排除起點自己。
- 查詢文字、向量、篩選值一律用參數；組進 SQL 的只有從資料庫讀出來的維度（整數）、驗證過的識別字與
  `visibility.py` 回的條件。

`paper_search_document`（遷移 0023）：一篇論文一列的 english `tsvector`（標題 A＋目前的摘要 B），GIN
索引。由資料庫的觸發器維護（論文新增、改標題，摘要新增、修改、刪除之後重算那一篇），應用程式只讀。
被排除的論文也有一列，查詢自己擋。
"""

import math
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from sqlalchemy import (
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    Table,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR

from radar.domain.embeddings import PAPER_ITEM_TYPE, VectorMatch, halfvec_literal
from radar.domain.paper_list import PaperListFilters
from radar.domain.retrieval import FusedMatch, RetrievalScope
from radar.domain.viewer import Viewer
from radar.repo.models import Base
from radar.repo.paper_filters import _field_conditions
from radar.repo.visibility import (
    visible_demo_papers,
    visible_lab_papers,
    visible_library_items,
    visible_papers,
)

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0023 建表與觸發器） ------------------------

paper_search_document_table = Table(
    "paper_search_document",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("paper_id", Uuid, ForeignKey("paper.id", ondelete="CASCADE"), nullable=False),
    Column("document", TSVECTOR, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("paper_id"),
    Index("ix_paper_search_document_document", "document", postgresql_using="gin"),
)

# 查詢裡論文（`paper`）的別名：篩選條件（`paper_filters._field_conditions`）接在這個別名上。
_ALIAS: Final = "listed"
# 全文檢索用的設定（ADR-0005 第 4 點；和遷移 0023 建資料時同一個）。
_TEXT_CONFIG: Final = "english"


def _check_model(connection: Connection, model_id: str, vector: Sequence[float]) -> int:
    """`model_id` 登記的維度；`vector` 要剛好這麼長、每個數字有限。

    Raises:
        TypeError: `model_id` 不是非空字串。
        LookupError: `model_id` 沒有登記在 `embedding_model`。
        ValueError: `vector` 的長度和登記的維度不同，或有非有限的數字。
    """
    if type(model_id) is not str or not model_id:
        raise TypeError("向量查詢一定要指定 model_id")
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
    return size


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


def _scope_condition(viewer: Viewer, scope: RetrievalScope) -> str:
    """範圍的 SQL 條件（接在可見條件之後；論文的別名是 `_ALIAS`）。

    Raises:
        TypeError: `scope` 是 `demo` 但 `viewer` 不是 `PublicViewer`
            （`visibility.visible_demo_papers` 擋）。
    """
    match RetrievalScope(scope):
        case RetrievalScope.TRACKED:
            return f"EXISTS (SELECT 1 FROM counted_paper AS scoped WHERE scoped.id = {_ALIAS}.id)"
        case RetrievalScope.LAB:
            return visible_lab_papers(viewer, alias=_ALIAS)
        case RetrievalScope.PERSONAL:
            mine = visible_library_items(viewer, alias="scoped_item")
            return (
                "EXISTS (SELECT 1 FROM library_item AS scoped_item"
                f" WHERE scoped_item.paper_id = {_ALIAS}.id AND {mine})"
            )
        case RetrievalScope.DEMO:
            return visible_demo_papers(viewer, alias=_ALIAS)


def search_papers(
    viewer: Viewer,
    connection: Connection,
    *,
    model_id: str,
    vector: Sequence[float],
    text_query: str | None,
    scope: RetrievalScope,
    filters: PaperListFilters,
    current_snapshot_id: uuid.UUID | None,
    leg_limit: int,
    rrf_k: int,
    max_distance: Decimal,
    exclude_paper_id: uuid.UUID | None = None,
) -> list[FusedMatch]:
    """混合檢索（見模組說明）：融合後的全部結果，照融合後的順序（最多 2 × `leg_limit` 篇）。

    - `vector`：查詢向量（找相似論文時是起點存好的向量），只比 `model_id` 的向量。
    - `text_query`：全文檢索腿的查詢文字（已驗證、去掉頭尾空白）；None＝只用向量腿（找相似論文）。
    - `scope`、`filters`：先套 `viewer` 的可見條件，再套範圍，再套篩選（`filters.scope` 不看）。
    - `current_snapshot_id`：目前已發佈的快照（`this_week` 用；沒有是 None）。
    - `max_distance`：向量腿的餘弦距離上限（1 － 相似度下限），超過的不算結果。
    - `exclude_paper_id`：不列這一篇（找相似論文的起點）。

    要在呼叫端開好的交易裡呼叫：iterative scan 與 `ef_search` 用 `set_config(…, true)`（只在這個
    交易裡有效）。

    Raises:
        TypeError: `model_id` 不是非空字串，或 `scope` 是 `demo` 但 `viewer` 不是
            `PublicViewer`。
        LookupError: `model_id` 沒有登記在 `embedding_model`。
        ValueError: `vector` 的維度不對或有非有限的數字、`leg_limit`／`rrf_k` 不是正整數、
            `max_distance` 不在 0 到 2 之間，或 `text_query` 是空字串。
    """
    size = _check_model(connection, model_id, vector)
    for name, value in (("leg_limit", leg_limit), ("rrf_k", rrf_k)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} 要是正整數")
    if not isinstance(max_distance, Decimal) or not Decimal(0) <= max_distance <= Decimal(2):
        raise ValueError("max_distance 要是 0 到 2 之間的 Decimal")
    if text_query is not None and (not isinstance(text_query, str) or not text_query.strip()):
        raise ValueError("text_query 要是 None 或有字的字串")

    visible = visible_papers(viewer, alias=_ALIAS)
    scoped = _scope_condition(viewer, scope)
    fields, params = _field_conditions(filters, current_snapshot_id, alias=_ALIAS)
    where = f"{visible} AND {_ALIAS}.excluded_reason IS NULL AND {scoped}" + "".join(
        f" AND {part}" for part in fields
    )
    if exclude_paper_id is not None:
        where += f" AND {_ALIAS}.id <> :exclude_paper_id"
        params["exclude_paper_id"] = exclude_paper_id
    params |= {
        "query_vector": halfvec_literal(vector),
        "item_type": PAPER_ITEM_TYPE,
        "model_id": model_id,
        "leg_limit": leg_limit,
        "rrf_k": rrf_k,
        "max_distance": max_distance,
    }

    # 維度是從資料庫讀出來的整數，直接組進 SQL（型別修飾不能用參數）。
    query_vector = f"CAST(:query_vector AS halfvec({size}))"
    distance = f"e.vector::halfvec({size}) <=> {query_vector}"
    candidates = (
        f" FROM embedding AS e JOIN paper AS {_ALIAS} ON {_ALIAS}.id = e.item_id"
        f" WHERE e.item_type = :item_type AND e.model_id = :model_id AND {where}"
    )
    if RetrievalScope(scope) is RetrievalScope.PERSONAL:
        # 先過濾（自己的文獻庫通常只有幾十到幾百篇）、再精確比距離：MATERIALIZED 讓規劃器不走索引。
        vector_leg = (
            f"vector_pool AS MATERIALIZED (SELECT e.item_id AS paper_id,"
            f" e.vector::halfvec({size}) AS vector{candidates}),"
            f" vector_leg AS MATERIALIZED (SELECT paper_id, vector <=> {query_vector} AS distance"
            " FROM vector_pool ORDER BY distance LIMIT :leg_limit)"
        )
    else:
        connection.execute(
            text(
                "SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true),"
                " set_config('hnsw.ef_search', CAST(:ef_search AS text), true)"
            ),
            {"ef_search": max(leg_limit, 40)},
        )
        vector_leg = (
            f"vector_leg AS MATERIALIZED (SELECT e.item_id AS paper_id, {distance} AS distance"
            f"{candidates} ORDER BY {distance} LIMIT :leg_limit)"
        )
    ranked = [
        vector_leg,
        "vector_ranked AS (SELECT paper_id,"
        " row_number() OVER (ORDER BY distance, paper_id) AS rank"
        " FROM vector_leg WHERE distance <= :max_distance)",
    ]
    if text_query is None:
        fused = (
            "SELECT v.paper_id, v.rank AS vector_rank, NULL::bigint AS text_rank"
            " FROM vector_ranked AS v ORDER BY v.rank"
        )
    else:
        params["text_query"] = text_query
        ranked += [
            f"text_query AS (SELECT websearch_to_tsquery('{_TEXT_CONFIG}', :text_query) AS q)",
            "text_leg AS MATERIALIZED (SELECT d.paper_id,"
            " ts_rank_cd(d.document, text_query.q) AS score"
            f" FROM paper_search_document AS d JOIN paper AS {_ALIAS} ON {_ALIAS}.id = d.paper_id"
            f" CROSS JOIN text_query WHERE d.document @@ text_query.q AND {where}"
            " ORDER BY score DESC, d.paper_id LIMIT :leg_limit)",
            "text_ranked AS (SELECT paper_id,"
            " row_number() OVER (ORDER BY score DESC, paper_id) AS rank FROM text_leg)",
        ]
        fused = (
            "SELECT COALESCE(v.paper_id, t.paper_id) AS paper_id, v.rank AS vector_rank,"
            " t.rank AS text_rank"
            " FROM vector_ranked AS v FULL OUTER JOIN text_ranked AS t ON t.paper_id = v.paper_id"
            " ORDER BY COALESCE(1.0 / (:rrf_k + v.rank), 0) + COALESCE(1.0 / (:rrf_k + t.rank), 0)"
            " DESC, LEAST(v.rank, t.rank), COALESCE(v.paper_id, t.paper_id)"
        )
    rows = connection.execute(text(f"WITH {', '.join(ranked)} {fused}"), params).all()
    return [
        FusedMatch(
            paper_id=uuid.UUID(str(paper_id)),
            vector_rank=None if vector_rank is None else int(vector_rank),
            text_rank=None if text_rank is None else int(text_rank),
        )
        for paper_id, vector_rank, text_rank in rows
    ]
