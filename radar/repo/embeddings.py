"""`embedding_model` 與 `embedding` 兩張表：表的定義與唯一的寫入者（PRD FR-5，AD-12，ADR-0005、
ADR-0040，Story 2.8）。

- `embedding_model`：模型登錄（`model_id` 主鍵、維度）。每個模型的 HNSW 部分索引要在遷移裡建，
  所以登錄也只由遷移寫（應用程式角色沒有 INSERT／UPDATE／DELETE，遷移 0015 收回）。
- `embedding`：主鍵（`item_type`, `item_id`, `model_id`），向量型別 `halfvec`；同一個主鍵直接覆寫
  （AD-12：不是 AI 表頭表，只帶 `run_id` 與 `input_sha256`）。（`model_id`, `dimensions`）外鍵指到
  `embedding_model`，CHECK `vector_dims(vector) = dimensions`：維度不對的向量寫不進去。
  同一篇論文不同模型的向量各一列、並存。
- 向量的相似度查詢不在這裡：只在 `repo/retrieval.py`（AD-11）。
- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。讀取函式第一個參數是 `viewer`，
  只給系統工作（`visible_to_system_only`）。
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    literal_column,
    text,
)
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.types import UserDefinedType

from radar.domain.ai_outputs import CurrentAbstract, PaperLlmSource
from radar.domain.embeddings import (
    PAPER_ITEM_TYPE,
    EmbeddingCandidate,
    NewEmbedding,
    StoredEmbedding,
    halfvec_literal,
    parse_halfvec,
)
from radar.domain.papers import AbstractSource, PaperOrigin
from radar.domain.processing import ProcessingFacts
from radar.domain.viewer import SystemViewer
from radar.repo.models import Base
from radar.repo.visibility import visible_papers, visible_to_system_only


class _HalfVec(UserDefinedType[Any]):
    """pgvector 的 `halfvec`（不帶維度：各模型的維度不同，由外鍵與 CHECK 守）。

    只給表定義與 `alembic check` 用；讀寫一律走 `text()` SQL 與文字格式（`halfvec_literal`）。
    """

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "HALFVEC"


# 讓 SQLAlchemy 反射資料庫時認得 `halfvec`（`alembic check` 才比得出型別一樣）。
ischema_names["halfvec"] = _HalfVec

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0015 建表） -----------------------------

embedding_model_table = Table(
    "embedding_model",
    Base.metadata,
    Column("model_id", Text, primary_key=True),
    Column("provider", Text, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # `embedding` 的（model_id, dimensions）外鍵要指到唯一的一組。
    UniqueConstraint("model_id", "dimensions"),
    CheckConstraint("model_id <> ''", name="model_id_not_empty"),
    CheckConstraint("provider ~ '^[a-z][a-z0-9_]*$'", name="provider_format"),
    CheckConstraint("dimensions BETWEEN 1 AND 4000", name="dimensions_range"),
)

embedding_table = Table(
    "embedding",
    Base.metadata,
    Column("item_type", Text, nullable=False),
    Column("item_id", Uuid, nullable=False),
    Column("model_id", Text, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("vector", _HalfVec(), nullable=False),
    Column("input_sha256", Text, nullable=False),
    Column("run_id", Uuid, ForeignKey("derivation_run.id"), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # 向量化當時用的摘要來源（遷移 0018、Story 2.12 審查後）：只看標題的是 NULL。
    # `radar runs remove-by-source` 照它刪向量（之後照指紋規則重算）。
    Column("abstract_source", Text),
    PrimaryKeyConstraint("item_type", "item_id", "model_id"),
    ForeignKeyConstraint(
        ["model_id", "dimensions"],
        ["embedding_model.model_id", "embedding_model.dimensions"],
    ),
    # 外鍵的索引（schema 體檢要求外鍵欄位有索引）。
    Index("ix_embedding_model_id_dimensions", "model_id", "dimensions"),
    CheckConstraint("item_type IN ('paper')", name="item_type"),
    CheckConstraint("input_sha256 ~ '^[0-9a-f]{64}$'", name="input_sha256_format"),
    CheckConstraint("vector_dims(vector) = dimensions", name="vector_dims"),
    CheckConstraint(
        "abstract_source IS NULL"
        " OR abstract_source IN ('openalex', 'semantic_scholar', 'uploaded_pdf')",
        name="abstract_source",
    ),
)

# 每個模型一個 HNSW 部分索引（AD-12）：用運算式 `(vector::halfvec(維度))` 建，查詢要寫一樣的運算式
# 而且帶 `model_id` 條件才用得到（見 `repo/retrieval.py`）。索引在遷移裡建；這裡登記給
# `alembic check`（它對運算式索引只比名稱，不比運算式、opclass 與條件），完整的定義由
# integration 測試逐字核對。
HNSW_INDEXES: dict[str, tuple[str, int]] = {
    "@cf/qwen/qwen3-embedding-0.6b": ("ix_embedding_hnsw_qwen3_embedding_0_6b", 1024),
}
for _model_id, (_index_name, _dimensions) in HNSW_INDEXES.items():
    Index(
        _index_name,
        literal_column(f"((vector)::halfvec({_dimensions}))").label("vector_halfvec"),
        postgresql_using="hnsw",
        postgresql_ops={"vector_halfvec": "halfvec_cosine_ops"},
        postgresql_where=text(f"model_id = '{_model_id}'::text"),
        _table=embedding_table,
    )


# --- 讀取 ---------------------------------------------------------------------------------


def find_embedding_model_dimensions(
    viewer: SystemViewer, connection: Connection, model_id: str
) -> int | None:
    """這個模型登記的維度；沒有登記回 None。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    value = connection.execute(
        text(f"SELECT dimensions FROM embedding_model WHERE {visible} AND model_id = :model_id"),
        {"model_id": model_id},
    ).scalar_one_or_none()
    return None if value is None else int(value)


_CANDIDATE_SELECT = (
    "SELECT paper.id, paper.origin, paper.excluded_reason, paper.title,"
    " a.id AS abstract_id, a.source AS abstract_source, a.content AS abstract_content,"
    " e.input_sha256 AS current_sha256"
    " FROM paper"
    " LEFT JOIN paper_abstract AS a ON a.paper_id = paper.id AND a.is_current"
    " LEFT JOIN embedding AS e ON e.item_type = :item_type AND e.item_id = paper.id"
    " AND e.model_id = :model_id"
)


def _candidate(row: Any) -> EmbeddingCandidate:
    abstract = None
    if row["abstract_id"] is not None:
        abstract = CurrentAbstract(
            id=row["abstract_id"],
            source=AbstractSource(row["abstract_source"]),
            content=row["abstract_content"],
        )
    return EmbeddingCandidate(
        facts=ProcessingFacts(
            origin=PaperOrigin(row["origin"]),
            excluded_reason=row["excluded_reason"],
            abstract=row["abstract_content"],
        ),
        source=PaperLlmSource(paper_id=row["id"], title=row["title"], abstract=abstract),
        current_sha256=row["current_sha256"],
    )


def list_embedding_candidates(
    viewer: SystemViewer,
    connection: Connection,
    model_id: str,
    *,
    after_id: uuid.UUID | None,
    limit: int,
) -> list[EmbeddingCandidate]:
    """依論文 id 順序讀一頁論文（`after_id` 之後、最多 `limit` 篇）：處理事實、標題、目前的摘要、
    這個模型現有向量的 `input_sha256`。全部論文都讀（要不要做由 `plan_for` 判斷，這裡不篩）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: `limit` 不是正整數。
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("limit 要是正整數")
    visible_to_system_only(viewer)
    visible = visible_papers(viewer, alias="paper")
    after = "" if after_id is None else " AND paper.id > :after_id"
    rows = (
        connection.execute(
            text(f"{_CANDIDATE_SELECT} WHERE {visible}{after} ORDER BY paper.id LIMIT :limit"),
            {
                "item_type": PAPER_ITEM_TYPE,
                "model_id": model_id,
                "after_id": after_id,
                "limit": limit,
            },
        )
        .mappings()
        .all()
    )
    return [_candidate(row) for row in rows]


def list_embedding_candidates_by_ids(
    viewer: SystemViewer, connection: Connection, model_id: str, paper_ids: Sequence[uuid.UUID]
) -> list[EmbeddingCandidate]:
    """指定論文的同一份資料（寫入前核對輸入有沒有變用）；不存在的論文不在結果裡。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible_to_system_only(viewer)
    visible = visible_papers(viewer, alias="paper")
    if not paper_ids:
        return []
    rows = (
        connection.execute(
            text(
                f"{_CANDIDATE_SELECT} WHERE {visible} AND paper.id = ANY(CAST(:ids AS uuid[]))"
                " ORDER BY paper.id"
            ),
            {
                "item_type": PAPER_ITEM_TYPE,
                "model_id": model_id,
                "ids": [str(paper_id) for paper_id in paper_ids],
            },
        )
        .mappings()
        .all()
    )
    return [_candidate(row) for row in rows]


def find_embedding_candidate_for_update(
    viewer: SystemViewer, connection: Connection, model_id: str, paper_id: uuid.UUID
) -> EmbeddingCandidate | None:
    """先鎖住論文那一列（`FOR UPDATE`），再讀它的候選資料（標題、目前的摘要、這個模型現有向量的
    `input_sha256`）；論文不存在回 None。

    向量化取得執行資格與寫入前都先呼叫：鎖住同一篇論文的每週更新寫入、另一個向量化工作的核對與
    寫入都要排隊，所以「核對指紋 → 寫入」之間不會被插隊（舊輸入算的向量蓋不掉新的，兩個工作也不會
    重複做同一份輸入）。鎖和讀分兩句：寫在同一句時，等鎖結束後 JOIN 進來的摘要與向量仍是等鎖前的
    快照（同 `repo/papers.find_paper_llm_source`）。在呼叫端開好的交易裡做，不 commit。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible_to_system_only(viewer)
    visible = visible_papers(viewer, alias="paper")
    locked = connection.execute(
        text(f"SELECT paper.id FROM paper WHERE {visible} AND paper.id = :id FOR UPDATE"),
        {"id": paper_id},
    ).one_or_none()
    if locked is None:
        return None
    candidates = list_embedding_candidates_by_ids(viewer, connection, model_id, [paper_id])
    return candidates[0] if candidates else None


def find_paper_embedding(
    viewer: SystemViewer, connection: Connection, paper_id: uuid.UUID, *, model_id: str
) -> StoredEmbedding | None:
    """這篇論文這個模型的向量；沒有回 None。一定要指定 `model_id`（AD-12）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    row = (
        connection.execute(
            text(
                "SELECT item_id, model_id, vector::text AS vector, input_sha256, run_id"
                f" FROM embedding WHERE {visible} AND item_type = :item_type"
                " AND item_id = :item_id AND model_id = :model_id"
            ),
            {"item_type": PAPER_ITEM_TYPE, "item_id": paper_id, "model_id": model_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return StoredEmbedding(
        item_id=row["item_id"],
        model_id=row["model_id"],
        vector=parse_halfvec(row["vector"]),
        input_sha256=row["input_sha256"],
        run_id=row["run_id"],
    )


# --- 寫入 ---------------------------------------------------------------------------------


def count_embeddings_by_abstract_source(
    viewer: SystemViewer, connection: Connection, source: AbstractSource
) -> int:
    """用 `source` 的摘要算的向量有幾列（`embedding.abstract_source`；`radar runs remove-by-source`
    的預覽，Story 2.12 審查後）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    value = connection.execute(
        text(f"SELECT count(*) FROM embedding WHERE {visible} AND abstract_source = :source"),
        {"source": AbstractSource(source).value},
    ).scalar_one()
    return int(value)


def delete_embeddings_by_abstract_source(connection: Connection, source: AbstractSource) -> int:
    """刪掉用 `source` 的摘要算的向量，回刪了幾列（`radar runs remove-by-source --execute`，
    Story 2.12 審查後、ADR-0044）。之後的向量化看不到現有向量，照指紋規則用現在的輸入重算。
    在呼叫端開好的交易裡做，不 commit。"""
    result = connection.execute(
        text("DELETE FROM embedding WHERE abstract_source = :source"),
        {"source": AbstractSource(source).value},
    )
    return int(result.rowcount)


def upsert_embeddings(connection: Connection, rows: Sequence[NewEmbedding]) -> int:
    """寫入論文的向量：同一個主鍵（論文, 模型）已經有就整列覆寫（AD-12），回寫了幾列。

    維度不對（和 `embedding_model` 登記的不同）時資料庫的外鍵與 CHECK 擋下、整個交易退回。
    在呼叫端開好的交易裡做，不 commit。
    """
    if not rows:
        return 0
    connection.execute(
        text(
            "INSERT INTO embedding"
            " (item_type, item_id, model_id, dimensions, vector, input_sha256, run_id,"
            " abstract_source)"
            " VALUES (:item_type, :item_id, :model_id, :dimensions, CAST(:vector AS halfvec),"
            " :input_sha256, :run_id, :abstract_source)"
            " ON CONFLICT (item_type, item_id, model_id) DO UPDATE SET"
            " dimensions = EXCLUDED.dimensions, vector = EXCLUDED.vector,"
            " input_sha256 = EXCLUDED.input_sha256, run_id = EXCLUDED.run_id,"
            " abstract_source = EXCLUDED.abstract_source, updated_at = clock_timestamp()"
        ),
        [
            {
                "item_type": PAPER_ITEM_TYPE,
                "item_id": row.item_id,
                "model_id": row.model_id,
                "dimensions": row.dimensions,
                "vector": halfvec_literal(row.vector),
                "input_sha256": row.input_sha256,
                "run_id": row.run_id,
                "abstract_source": (
                    None
                    if row.abstract_source is None
                    else AbstractSource(row.abstract_source).value
                ),
            }
            for row in rows
        ],
    )
    return len(rows)
