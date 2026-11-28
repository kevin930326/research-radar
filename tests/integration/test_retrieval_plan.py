"""檢索的查詢計畫對真的 PostgreSQL（Story 4.1、ADR-0005 第 3、6 點）：做法同
`test_migration_0015.test_vector_query_uses_the_model_hnsw_index`。

- `lab`、`tracked`（以及 `demo`）的向量腿走那個模型的 HNSW 部分索引，交易內打開了 iterative scan
  （`hnsw.iterative_scan = relaxed_order`）、`hnsw.ef_search` 是每條腿的名額。
- `personal` 先過濾再精確比距離：不走 HNSW 索引。
- 過濾掉大部分候選時（範圍只剩少數幾篇、散在很遠的位置），iterative scan 仍把它們找回來。

向量是測試自己產生的 1,024 維數字（用真的模型 `@cf/qwen/qwen3-embedding-0.6b` 的索引，遷移 0015
建的）。
"""

import math
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import Connection, text

from radar.domain.paper_list import PaperListFilters, PaperListScope
from radar.domain.retrieval import FusedMatch, RetrievalScope
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.repo.retrieval import search_papers
from radar.services.members import allow_member
from tests.database import TestDatabase, migrate
from tests.integration.test_paper_list_api import make_paper, make_snapshot, put_in_library
from tests.integration.test_retrieval import put_vector

QWEN = "@cf/qwen/qwen3-embedding-0.6b"
HNSW = "ix_embedding_hnsw_qwen3_embedding_0_6b"
SIZE = 1024


def vector(seed: int) -> list[float]:
    """1,024 維、長度 1 的向量；`seed` 不同方向不同（測試自己編的數字）。"""
    raw = [
        math.sin(seed * 0.37 + index * 0.011) + (1.0 if index == seed % SIZE else 0.0)
        for index in range(SIZE)
    ]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


def _search(
    connection: Connection, viewer: MemberViewer, scope: RetrievalScope, query: list[float]
) -> list[FusedMatch]:
    return search_papers(
        viewer,
        connection,
        model_id=QWEN,
        vector=query,
        text_query=None,
        scope=scope,
        filters=PaperListFilters(scope=PaperListScope(scope.value)),
        current_snapshot_id=None,
        leg_limit=100,
        rrf_k=60,
        max_distance=Decimal(2),
    )


def _scans(connection: Connection) -> int:
    return int(
        connection.execute(
            text("SELECT pg_stat_get_xact_numscans(CAST(:index AS regclass))"), {"index": HNSW}
        ).scalar_one()
    )


@pytest.mark.parametrize("scope", [RetrievalScope.LAB, RetrievalScope.TRACKED])
def test_shared_scopes_use_the_model_hnsw_index_with_iterative_scan(
    db: TestDatabase, database: Database, scope: RetrievalScope
) -> None:
    published = make_snapshot(db, 1)
    papers = [make_paper(database, db, number, snapshot_id=published) for number in range(3)]
    for number, paper_id in enumerate(papers):
        put_vector(db, paper_id, vector(number), model=QWEN)
    viewer = MemberViewer(member_id=uuid.uuid4(), is_admin=False)

    with database.transaction() as connection:
        # 表幾乎是空的，規劃器會寧可排序；關掉循序掃描與排序，證明這個寫法「用得到」索引。
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        connection.execute(text("SET LOCAL enable_sort = off"))
        matches = _search(connection, viewer, scope, vector(0))
        scans = _scans(connection)
        settings = connection.execute(
            text("SELECT current_setting('hnsw.iterative_scan'), current_setting('hnsw.ef_search')")
        ).one()

    assert matches[0].paper_id == papers[0]
    assert {match.paper_id for match in matches} == set(papers)
    assert scans >= 1
    assert tuple(settings) == ("relaxed_order", "100")


def test_personal_scope_filters_first_and_compares_exactly_without_the_index(
    db: TestDatabase, database: Database
) -> None:
    published = make_snapshot(db, 1)
    owner_id = allow_member(
        SystemViewer(), database, email="plan.owner@example.com", display_name="測試"
    ).member_id
    papers = [make_paper(database, db, number, snapshot_id=published) for number in range(3)]
    for number, paper_id in enumerate(papers):
        put_vector(db, paper_id, vector(number), model=QWEN)
    put_in_library(database, owner_id, papers[1])
    viewer = MemberViewer(member_id=owner_id, is_admin=False)

    with database.transaction() as connection:
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        connection.execute(text("SET LOCAL enable_sort = off"))
        matches = _search(connection, viewer, RetrievalScope.PERSONAL, vector(0))
        scans = _scans(connection)

    assert [match.paper_id for match in matches] == [papers[1]]
    assert scans == 0


def test_iterative_scan_finds_the_few_papers_left_after_filtering(
    db: TestDatabase, database: Database
) -> None:
    # 120 篇裡只有 3 篇在 tracked（其他的快照還沒發佈），而且離查詢最遠：沒有 iterative scan 時，
    # HNSW 第一輪給的候選全被範圍條件篩掉，結果會是空的。
    published = make_snapshot(db, 1)
    building = make_snapshot(db, 2, status="building")
    query = vector(0)

    def similarity(number: int) -> float:
        return sum(a * b for a, b in zip(vector(number), query, strict=True))

    farthest = set(sorted(range(1, 121), key=similarity)[:3])
    counted: list[uuid.UUID] = []
    for number in range(1, 121):
        in_scope = number in farthest
        paper_id = make_paper(database, db, number, snapshot_id=published if in_scope else building)
        put_vector(db, paper_id, vector(number), model=QWEN)
        if in_scope:
            counted.append(paper_id)
    viewer = MemberViewer(member_id=uuid.uuid4(), is_admin=False)

    with database.transaction() as connection:
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        connection.execute(text("SET LOCAL enable_sort = off"))
        matches = search_papers(
            viewer,
            connection,
            model_id=QWEN,
            vector=query,
            text_query=None,
            scope=RetrievalScope.TRACKED,
            filters=PaperListFilters(),
            current_snapshot_id=None,
            leg_limit=3,
            rrf_k=60,
            max_distance=Decimal(2),
        )
        scans = _scans(connection)

    assert scans >= 1
    assert {match.paper_id for match in matches} == set(counted)
