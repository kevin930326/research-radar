"""評估的表與唯一的寫入者（Story 4.4、FR-41、Conventions「評估」）：搜尋的評估題組
（`eval_query`、`eval_query_relevant`）與評估的執行紀錄（`eval_run`，最小版本，Epic 8 擴充）。

- `eval_query`：一題。`query_set`（`search`／`similar`）＋`query_key`（Kevin 題目檔的 `query_id`）
  唯一；`query_text` 是驗過的查詢字串（`search`）或正規化後的起點 DOI（`similar`）。
- `eval_query_relevant`：一題的一篇正解（正規化後的 DOI＋選填的備註）。正解存 DOI 不存論文 ID：
  DOI 在每個資料庫都一樣，搬家、還原後題目照樣能用；跑題時才換成論文 ID。題目刪掉時正解跟著刪。
- `eval_run`：一次評估的紀錄。`kind`（現在只有 `search`）、跑了哪幾組題目、相似度下限（可多個）、
  向量模型、排名時讀到的快照、題數、結果（`domain/evaluation.eval_results_json`：每組的命中率、MRR、
  每題的命中名次；不放查詢內容）、開始與結束時間。寫完不改（應用程式角色沒有 UPDATE、DELETE）。
  `derivation_run.eval_run_id`（遷移 0010）還沒有外鍵指到這張表（Story 4.4 不補，列給 Kevin 決定）。

這三張都是共用資料（`domain/backup.SHARED_TABLES`）：沒有成員欄位，是 Kevin 以管理者身分匯入、量測的
系統資料。寫入一律用 `text()` SQL、參數；函式收呼叫端開好的交易連線，不 commit。
"""

import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from radar.domain.evaluation import (
    EvalQuerySet,
    EvalQuestion,
    EvalRelevant,
    EvalRunKind,
)
from radar.domain.viewer import SystemViewer
from radar.repo.models import Base
from radar.repo.visibility import visible_to_system_only

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0024 建表） ---------------------------

eval_query_table = Table(
    "eval_query",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("query_set", Text, nullable=False),
    Column("query_key", Text, nullable=False),
    Column("query_text", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("query_set", "query_key"),
    CheckConstraint("query_set IN ('search', 'similar')", name="query_set"),
    CheckConstraint("char_length(query_key) BETWEEN 1 AND 64", name="query_key_length"),
    CheckConstraint("char_length(query_text) BETWEEN 1 AND 2000", name="query_text_length"),
)

eval_query_relevant_table = Table(
    "eval_query_relevant",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("eval_query_id", Uuid, ForeignKey("eval_query.id", ondelete="CASCADE"), nullable=False),
    Column("relevant_doi", Text, nullable=False),
    Column("note", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # 同一題的正解不重複（也當 `eval_query_id` 外鍵的索引）。
    UniqueConstraint("eval_query_id", "relevant_doi"),
    CheckConstraint("char_length(relevant_doi) BETWEEN 1 AND 2000", name="relevant_doi_length"),
    CheckConstraint("note IS NULL OR char_length(note) BETWEEN 1 AND 500", name="note_length"),
)

eval_run_table = Table(
    "eval_run",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("kind", Text, nullable=False),
    Column("query_sets", ARRAY(Text), nullable=False),
    Column("min_similarities", ARRAY(Numeric), nullable=False),
    # 當時用的向量模型（紀錄用的字串，不設外鍵：模型登錄換掉時紀錄照留）。
    Column("embedding_model_id", Text, nullable=False),
    Column("snapshot_id", Uuid, ForeignKey("snapshot.id"), index=True),
    Column("query_count", Integer, nullable=False),
    Column("results", JSONB, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("kind IN ('search')", name="kind"),
    CheckConstraint(
        "cardinality(query_sets) >= 1 AND query_sets <@ ARRAY['search', 'similar']::text[]",
        name="query_sets",
    ),
    CheckConstraint(
        "cardinality(min_similarities) >= 1 AND array_position(min_similarities, NULL) IS NULL"
        " AND 0 <= ALL (min_similarities) AND 1 > ALL (min_similarities)",
        name="min_similarities",
    ),
    CheckConstraint("query_count >= 0", name="query_count_not_negative"),
    CheckConstraint("finished_at >= started_at", name="finished_after_started"),
)


# --- 讀取 -------------------------------------------------------------------------------------


def list_eval_queries(viewer: SystemViewer, connection: Connection) -> list[EvalQuestion]:
    """全部的評估題目與正解（先 `search` 再 `similar`，同一組照題目編號、正解照 DOI 排）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    rows = connection.execute(
        text(
            "SELECT q.query_set, q.query_key, q.query_text, r.relevant_doi, r.note"
            " FROM eval_query AS q JOIN eval_query_relevant AS r ON r.eval_query_id = q.id"
            f" WHERE {visible}"
            " ORDER BY CASE q.query_set WHEN 'search' THEN 0 ELSE 1 END, q.query_key,"
            " r.relevant_doi"
        )
    ).all()
    grouped: dict[tuple[str, str], tuple[str, list[EvalRelevant]]] = {}
    for query_set, query_key, query_text, relevant_doi, note in rows:
        _query, relevant = grouped.setdefault((query_set, query_key), (query_text, []))
        relevant.append(EvalRelevant(doi=relevant_doi, note=note))
    return [
        EvalQuestion(EvalQuerySet(query_set), query_key, query_text, tuple(relevant))
        for (query_set, query_key), (query_text, relevant) in grouped.items()
    ]


# --- 寫入 -------------------------------------------------------------------------------------


def delete_eval_queries(connection: Connection) -> int:
    """刪掉全部的評估題目（正解跟著刪），回刪了幾題。匯入新的題目檔前呼叫（同一個交易）。"""
    result = connection.execute(text("DELETE FROM eval_query"))
    return int(result.rowcount)


def create_eval_queries(connection: Connection, questions: Sequence[EvalQuestion]) -> None:
    """新增題目與它們的正解（`(query_set, query_key)` 已經有了會違反唯一約束）。"""
    for question in questions:
        ((query_id,),) = connection.execute(
            text(
                "INSERT INTO eval_query (query_set, query_key, query_text)"
                " VALUES (:query_set, :query_key, :query_text) RETURNING id"
            ),
            {
                "query_set": question.query_set.value,
                "query_key": question.query_id,
                "query_text": question.query,
            },
        ).all()
        connection.execute(
            text(
                "INSERT INTO eval_query_relevant (eval_query_id, relevant_doi, note)"
                " VALUES (:eval_query_id, :relevant_doi, :note)"
            ),
            [
                {"eval_query_id": query_id, "relevant_doi": item.doi, "note": item.note}
                for item in question.relevant
            ],
        )


def create_eval_run(
    connection: Connection,
    *,
    kind: EvalRunKind,
    query_sets: Sequence[EvalQuerySet],
    min_similarities: Sequence[Decimal],
    embedding_model_id: str,
    snapshot_id: uuid.UUID | None,
    query_count: int,
    results: dict[str, object],
    started_at: datetime,
    finished_at: datetime,
) -> uuid.UUID:
    """新增一筆評估紀錄，回它的 ID。"""
    ((run_id,),) = connection.execute(
        text(
            "INSERT INTO eval_run (kind, query_sets, min_similarities, embedding_model_id,"
            " snapshot_id, query_count, results, started_at, finished_at)"
            " VALUES (:kind, CAST(:query_sets AS text[]), CAST(:min_similarities AS numeric[]),"
            " :embedding_model_id, :snapshot_id, :query_count, CAST(:results AS jsonb),"
            " :started_at, :finished_at) RETURNING id"
        ),
        {
            "kind": EvalRunKind(kind).value,
            "query_sets": [EvalQuerySet(item).value for item in query_sets],
            "min_similarities": list(min_similarities),
            "embedding_model_id": embedding_model_id,
            "snapshot_id": snapshot_id,
            "query_count": query_count,
            "results": json.dumps(results, ensure_ascii=False),
            "started_at": started_at,
            "finished_at": finished_at,
        },
    ).all()
    return uuid.UUID(str(run_id))
