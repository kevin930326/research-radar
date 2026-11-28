"""混合檢索對真的 PostgreSQL（Story 4.1、FR-26、FR-27、FR-5、FR-7、AD-11、ADR-0005）：
`services/retrieval.retrieve` 的每一條驗收條件。

- 融合：向量腿與全文檢索腿各取前 N、RRF（k）在一條 SQL 裡融合；兩條腿都有的排最前面；向量腿低於
  相似度下限的不算；中文查詢時全文檢索腿找不到東西、整體仍有結果；兩條腿都空時是空結果。
- 分頁：融合後名次的位移，前後頁不重複不漏，`total` 等於翻得到的篇數、最多到 2 × N。
- 模型：同維度的另一個模型的向量不混進來（FR-5 用檢索再驗一次）。
- 範圍：先套可見條件再套範圍；`personal` 換人查不到（換人測試）；`demo` 只給 `PublicViewer`（型別
  與執行時都擋）；被排除的文章在任何範圍、任何路徑都不出現；金絲雀（成員 A 私人論文的標題）不出現在
  成員 B 的結果與日誌裡。
- 篩選：期刊、年份、主題（含 `topic_match`）、方法（含 `method_match`）、`has_abstract`、`this_week`
  和找論文清單（`repo/paper_list.list_paper_list_ids`）是同一套：同樣的條件拿到同一批論文。
- 找相似論文：只用向量腿、用存好的向量、不呼叫 provider（假 provider 一被呼叫就失敗）、不寫
  `api_call`、不含起點；起點沒有向量時是空結果；看不到、不存在、被排除的起點丟同一個錯。
- 查詢字串：不收的字串在呼叫 provider 之前就擋下；日誌不記查詢內容。

向量是測試自己編的 8 維數字（模型 `fake-embedding` 由超級使用者登記，沒有 HNSW 索引，所以這裡都是
精確比對；HNSW＋iterative scan 的查詢計畫在 `test_retrieval_plan.py`）。標題、摘要、email 都是
測試自己編的。
"""

import hashlib
import math
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import pytest
from structlog.testing import capture_logs

from radar.domain.paper_list import MatchMode, PaperListFilters, PaperListScope
from radar.domain.papers import PaperOrigin
from radar.domain.policy import CloudPolicy, CloudSafeText, cloud_text
from radar.domain.retrieval import (
    InvalidSearchQueryError,
    RetrievalAnchorNotFoundError,
    RetrievalConfig,
    RetrievalHit,
    RetrievalPage,
    RetrievalScope,
)
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer
from radar.repo.db import Database
from radar.repo.paper_list import list_paper_list_ids
from radar.services.members import allow_member
from radar.services.retrieval import RetrievalSetup, retrieve
from tests.database import TestDatabase, migrate
from tests.integration.test_effective_views import insert_classification
from tests.integration.test_embeddings import CONFIG, MODEL, NOW, OTHER_MODEL
from tests.integration.test_paper_list_api import (
    ISR,
    MISQ,
    OUTSIDE,
    make_paper,
    make_snapshot,
    put_in_library,
)
from tests.llm_support import execute

DIMENSIONS = 8
CANARY_TITLE = "canary-3e1b7c90-private retrieval title"
RETRIEVAL = RetrievalConfig(
    vector_min_similarity=Decimal("0.2"),
    leg_limit=100,
    rrf_k=60,
    page_size=20,
    qa_paper_count=12,
    query_max_length=50,
)
# 查詢的向量（假 provider 對每一句查詢都回它，除非另外指定）。
QUERY = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
FAR = (0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)  # 相似度 0：低於下限


def near(step: int) -> tuple[float, ...]:
    """和 `QUERY` 很像的向量：`step` 越大越不像（相似度仍遠高於下限）。"""
    raw = [1.0, 0.05 * step, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    norm = math.sqrt(sum(value * value for value in raw))
    return tuple(value / norm for value in raw)


class QueryVectors:
    """假的 `EmbeddingProvider`：`embed_query` 回指定的向量（沒指定回 `QUERY`），記下每次呼叫；
    `embed_documents` 不該被呼叫。"""

    def __init__(
        self, vectors: Mapping[str, Sequence[float]] | None = None, model_id: str = MODEL
    ) -> None:
        self._vectors = dict(vectors or {})
        self._model_id = model_id
        self.queries: list[str] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        raise AssertionError("檢索不該呼叫 embed_documents")

    def embed_query(self, text: CloudSafeText) -> list[float]:
        value = cloud_text(text)
        self.queries.append(value)
        return list(self._vectors.get(value, QUERY))


class NeverCalled:
    """一被呼叫就失敗的 provider：找相似論文不呼叫 Workers AI。"""

    model_id = MODEL

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        raise AssertionError("找相似論文不該呼叫 provider")

    def embed_query(self, text: CloudSafeText) -> list[float]:
        raise AssertionError("找相似論文不該呼叫 provider")


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    for model_id in (MODEL, OTHER_MODEL):
        execute(
            fresh_database,
            "INSERT INTO embedding_model (model_id, provider, dimensions) VALUES (%s, 'fake', %s)",
            model_id,
            DIMENSIONS,
        )
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=3)


@pytest.fixture
def published(db: TestDatabase) -> uuid.UUID:
    return make_snapshot(db, 1)


def member(database: Database, email: str) -> MemberViewer:
    change = allow_member(SystemViewer(), database, email=email, display_name="測試")
    return MemberViewer(member_id=change.member_id, is_admin=False)


@pytest.fixture
def owner(database: Database) -> MemberViewer:
    return member(database, "retrieval.owner@example.com")


@pytest.fixture
def other(database: Database) -> MemberViewer:
    return member(database, "retrieval.other@example.com")


def put_vector(
    db: TestDatabase, paper_id: uuid.UUID, vector: Sequence[float], *, model: str = MODEL
) -> None:
    """用超級使用者寫一筆向量（含它的 `derivation_run`）；數字是測試自己編的。"""
    sha = hashlib.sha256(f"{paper_id}:{model}".encode()).hexdigest()
    ((run_id,),) = execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, model, input_sha256, input_kind, params,"
        " code_version, status, call_mode, finished_at) VALUES (%s, 'embed', %s, %s, 'abstract',"
        " '{}', 'test', 'succeeded', 'interactive', now()) RETURNING id",
        paper_id,
        model,
        sha,
    )
    execute(
        db,
        "INSERT INTO embedding (item_type, item_id, model_id, dimensions, vector, input_sha256,"
        " run_id) VALUES ('paper', %s, %s, %s, %s::halfvec, %s, %s)",
        paper_id,
        model,
        len(vector),
        "[" + ",".join(repr(float(value)) for value in vector) + "]",
        sha,
        run_id,
    )


def setup_with(provider: Any = None, retrieval: RetrievalConfig = RETRIEVAL) -> RetrievalSetup:
    return RetrievalSetup(
        embedding=CONFIG,
        retrieval=retrieval,
        cloud_policy=CloudPolicy(),
        provider=provider if provider is not None else QueryVectors(),
        clock=lambda: NOW,
    )


def search(
    viewer: Any,
    database: Database,
    query: str,
    *,
    scope: RetrievalScope = RetrievalScope.TRACKED,
    filters: PaperListFilters | None = None,
    k: int = 200,
    offset: int = 0,
    setup: RetrievalSetup | None = None,
) -> RetrievalPage:
    chosen = setup or setup_with()
    if scope is RetrievalScope.DEMO:
        return retrieve(
            viewer,
            database,
            chosen,
            query=query,
            scope=scope,
            filters=filters or PaperListFilters(),
            k=k,
            offset=offset,
        )
    return retrieve(
        viewer,
        database,
        chosen,
        query=query,
        scope=scope,
        filters=filters or PaperListFilters(scope=PaperListScope(scope)),
        k=k,
        offset=offset,
    )


def ids(page: RetrievalPage) -> list[uuid.UUID]:
    return [hit.paper_id for hit in page.hits]


def api_calls(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(db, "SELECT operation, outcome FROM api_call ORDER BY called_at, id")


# --- 融合、下限、中文、兩條腿都空 -----------------------------------------------------------------


def test_rrf_puts_papers_found_by_both_legs_first_and_drops_vectors_below_the_floor(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    both = make_paper(database, db, 1, snapshot_id=published, title="Trust in shared ledgers")
    vector_only = make_paper(database, db, 2, snapshot_id=published, title="Ledger adoption")
    text_only = make_paper(database, db, 3, snapshot_id=published, title="Trust and governance")
    far_text = make_paper(database, db, 4, snapshot_id=published, title="Platform trust signals")
    far = make_paper(database, db, 5, snapshot_id=published, title="Unrelated topic")
    put_vector(db, both, near(2))
    put_vector(db, vector_only, near(0))
    put_vector(db, far_text, FAR)
    put_vector(db, far, FAR)

    page = search(owner, database, "trust")

    by_id = {hit.paper_id: hit for hit in page.hits}
    # 向量腿：vector_only（第 1）、both（第 2）；far、far_text 低於下限不算。
    assert by_id[vector_only].vector_rank == 1
    assert by_id[both].vector_rank == 2
    assert by_id[far_text].vector_rank is None
    # 全文檢索腿只有標題有 trust 的三篇；都在兩條腿的排最前面。
    assert {by_id[paper].text_rank for paper in (both, text_only, far_text)} == {1, 2, 3}
    assert by_id[vector_only].text_rank is None
    assert ids(page)[0] == both
    assert far not in by_id
    assert set(by_id) == {both, vector_only, text_only, far_text}
    assert page.total == 4
    assert [hit.rank for hit in page.hits] == [1, 2, 3, 4]
    # RRF：分數＝Σ 1 ÷ (k＋名次)，由高到低。
    k = RETRIEVAL.rrf_k

    def score(hit: RetrievalHit) -> float:
        return sum(1 / (k + rank) for rank in (hit.vector_rank, hit.text_rank) if rank is not None)

    scores = [score(hit) for hit in page.hits]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize(
    ("rrf_k", "leader"), [(1, "p"), (60, "q")], ids=["k1-best-rank-wins", "k60-rank-sum-wins"]
)
def test_rrf_k_decides_between_two_papers_found_by_both_legs(
    db: TestDatabase,
    database: Database,
    published: uuid.UUID,
    owner: MemberViewer,
    rrf_k: int,
    leader: str,
) -> None:
    # 審查 M8：p 的名次是（向量 1、全文 5），q 是（向量 2、全文 3）。RRF 分數 Σ 1 ÷ (k＋名次)：
    # k＝1 時 p 0.667 > q 0.583；k＝60 時 q 0.03200 > p 0.03178。
    # 其他三篇只有全文腿（名次 1、2、4）。
    # 全文腿的名次由標題裡 trust 出現的次數決定（越多越前面）。
    def titled(number: int, times: int) -> uuid.UUID:
        return make_paper(
            database, db, number, snapshot_id=published, title=" ".join(["trust"] * times)
        )

    t1, t2, q, t4, p = titled(1, 6), titled(2, 5), titled(3, 4), titled(4, 3), titled(5, 2)
    put_vector(db, p, near(0))
    put_vector(db, q, near(1))
    setup = setup_with(retrieval=RETRIEVAL.model_copy(update={"rrf_k": rrf_k}))

    page = search(owner, database, "trust", setup=setup)

    ranks = {hit.paper_id: (hit.vector_rank, hit.text_rank) for hit in page.hits}
    assert ranks == {
        t1: (None, 1),
        t2: (None, 2),
        q: (2, 3),
        t4: (None, 4),
        p: (1, 5),
    }
    # 兩個 k 下，t1（1 ÷ (k＋1)）都排在 p、q 之後：k＝1 是 0.5，k＝60 是 0.01639。
    first_two = [p, q] if leader == "p" else [q, p]
    assert ids(page) == [*first_two, t1, t2, t4]


def test_personal_vector_leg_breaks_distance_ties_by_id_before_the_cut(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    # 審查 M3：每條腿只取 2 名，20 篇的向量一模一樣（同距離）：入榜的一定是 id 最小的兩篇。論文、
    # 文獻庫、向量都照 id 由大到小重寫或寫入（表裡的實體順序跟著倒過來），讓「先掃到的先入榜」選錯。
    papers = [make_paper(database, db, number, snapshot_id=published) for number in range(20)]
    for paper in sorted(papers, reverse=True):
        execute(db, "UPDATE paper SET publication_year = 2021 WHERE id = %s", paper)
        put_in_library(database, owner.member_id, paper)
        put_vector(db, paper, near(1))
    # 有統計資料時規劃器走循序掃描（實體順序）；沒有時走主鍵索引，剛好是 id 順序，測不出來。
    execute(db, "ANALYZE paper, library_item, embedding")
    setup = setup_with(retrieval=RETRIEVAL.model_copy(update={"leg_limit": 2}))

    page = search(owner, database, "測試", scope=RetrievalScope.PERSONAL, setup=setup, k=4)

    assert ids(page) == sorted(papers)[:2]


def test_chinese_query_finds_english_papers_only_through_the_vector_leg(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    trust = make_paper(database, db, 1, snapshot_id=published, title="Trust in shared ledgers")
    other = make_paper(database, db, 2, snapshot_id=published, title="Ledger adoption")
    put_vector(db, trust, near(0))
    put_vector(db, other, near(3))

    page = search(owner, database, "區塊鏈的信任")

    assert ids(page) == [trust, other]
    assert all(hit.text_rank is None for hit in page.hits)
    assert page.total == 2


def test_both_legs_empty_gives_an_empty_page(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    paper = make_paper(database, db, 1, snapshot_id=published, title="Ledger adoption")
    put_vector(db, paper, FAR)

    page = search(owner, database, "信任")

    assert page.hits == ()
    assert page.total == 0


def test_text_leg_alone_still_finds_papers_without_vectors(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    # 還沒向量化的論文：全文檢索腿照樣找得到（標題＋目前的摘要）。
    by_abstract = make_paper(database, db, 1, snapshot_id=published, title="Ledger adoption")
    execute(
        db,
        "UPDATE paper_abstract SET content = 'Interorganizational trust matters.'"
        " WHERE paper_id = %s",
        by_abstract,
    )

    page = search(owner, database, "trust")

    assert ids(page) == [by_abstract]
    assert page.hits[0].vector_rank is None
    assert page.hits[0].text_rank == 1


# --- 分頁與 total ------------------------------------------------------------------------------


def test_pages_have_no_gaps_or_repeats_and_total_matches_what_can_be_paged(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    papers = []
    for number in range(25):
        title = f"Trust study {number}" if number % 2 else f"Ledger study {number}"
        paper = make_paper(database, db, number, snapshot_id=published, title=title)
        put_vector(db, paper, near(number))
        papers.append(paper)

    whole = search(owner, database, "trust")
    pages = [search(owner, database, "trust", k=10, offset=offset) for offset in (0, 10, 20)]

    assert whole.total == 25
    assert [page.total for page in pages] == [25, 25, 25]
    assert [len(page.hits) for page in pages] == [10, 10, 5]
    paged = [hit for page in pages for hit in page.hits]
    assert [hit.paper_id for hit in paged] == ids(whole)
    assert [hit.rank for hit in paged] == list(range(1, 26))
    assert len(set(ids(whole))) == 25
    assert set(ids(whole)) == set(papers)
    past_the_end = search(owner, database, "trust", k=10, offset=30)
    assert past_the_end.hits == ()
    assert past_the_end.total == 25


def test_total_is_capped_by_the_leg_limit(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    small = RETRIEVAL.model_copy(update={"leg_limit": 3})
    for number in range(10):
        paper = make_paper(database, db, number, snapshot_id=published, title=f"Trust {number}")
        put_vector(db, paper, near(number))

    page = search(owner, database, "trust", k=6, setup=setup_with(retrieval=small))

    # 兩條腿各取前 3：最多 6 篇；這裡兩條腿各自的前 3 不一定一樣，所以 3 到 6 篇。
    assert 3 <= page.total <= 6
    assert page.total == len(page.hits)
    assert all(hit.vector_rank is None or hit.vector_rank <= 3 for hit in page.hits)
    assert all(hit.text_rank is None or hit.text_rank <= 3 for hit in page.hits)


@pytest.mark.parametrize(("k", "offset"), [(0, 0), (201, 0), (10, -1)])
def test_k_and_offset_are_checked(
    database: Database, owner: MemberViewer, k: int, offset: int
) -> None:
    with pytest.raises(ValueError, match=r"k 要是|offset"):
        search(owner, database, "trust", k=k, offset=offset)


# --- 模型不混 ----------------------------------------------------------------------------------


def test_vectors_of_another_model_with_the_same_dimensions_are_never_compared(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    ours = make_paper(database, db, 1, snapshot_id=published, title="Ledger adoption")
    theirs = make_paper(database, db, 2, snapshot_id=published, title="Platform signals")
    put_vector(db, ours, near(5))
    put_vector(db, ours, FAR, model=OTHER_MODEL)
    # 另一個模型的向量和查詢一模一樣，但不能被拿來比。
    put_vector(db, theirs, QUERY, model=OTHER_MODEL)

    page = search(owner, database, "信任")

    assert ids(page) == [ours]
    similar = retrieve(
        owner,
        database,
        setup_with(NeverCalled()),
        paper_id=ours,
        scope=RetrievalScope.TRACKED,
        filters=PaperListFilters(),
        k=20,
    )
    assert ids(similar) == []  # `theirs` 只有另一個模型的向量


# --- 範圍、換人、金絲雀、被排除 ---------------------------------------------------------------


def test_personal_scope_is_private_and_lab_hides_others_private_papers(
    db: TestDatabase,
    database: Database,
    published: uuid.UUID,
    owner: MemberViewer,
    other: MemberViewer,
) -> None:
    private = make_paper(
        database,
        db,
        1,
        snapshot_id=None,
        issn_l=OUTSIDE,
        origin=PaperOrigin.IMPORT,
        title=CANARY_TITLE,
    )
    counted = make_paper(database, db, 2, snapshot_id=published, title="Ledger adoption")
    put_in_library(database, owner.member_id, private)
    put_in_library(database, owner.member_id, counted)
    put_vector(db, private, near(0))
    put_vector(db, counted, near(1))

    assert ids(search(owner, database, "canary", scope=RetrievalScope.PERSONAL)) == [
        private,
        counted,
    ]
    with capture_logs() as logs:
        for scope in RetrievalScope.PERSONAL, RetrievalScope.LAB, RetrievalScope.TRACKED:
            found = search(other, database, "canary private", scope=scope)
            assert private not in ids(found)
            assert CANARY_TITLE not in repr(found)
        b_personal = search(other, database, "canary", scope=RetrievalScope.PERSONAL)
    assert b_personal.hits == ()
    assert b_personal.total == 0
    assert "canary-3e1b7c90" not in repr(logs)
    # 對照：A 自己在 lab 看得到自己的私人論文嗎？lab 只有全實驗室看得到的，所以也看不到。
    assert private not in ids(search(owner, database, "canary", scope=RetrievalScope.LAB))
    # 範圍外、全實驗室看得到的論文在 lab、不在 tracked。
    outside = make_paper(
        database,
        db,
        3,
        snapshot_id=None,
        issn_l=OUTSIDE,
        origin=PaperOrigin.EXPANSION,
        title="Ledger x",
    )
    put_vector(db, outside, near(2))
    assert outside in ids(search(other, database, "x", scope=RetrievalScope.LAB))
    assert outside not in ids(search(other, database, "x", scope=RetrievalScope.TRACKED))


def test_similar_papers_cannot_start_from_a_paper_the_viewer_cannot_see(
    db: TestDatabase,
    database: Database,
    published: uuid.UUID,
    owner: MemberViewer,
    other: MemberViewer,
) -> None:
    private = make_paper(
        database, db, 1, snapshot_id=None, issn_l=OUTSIDE, origin=PaperOrigin.IMPORT
    )
    excluded = make_paper(database, db, 2, snapshot_id=published, excluded_reason="erratum")
    counted = make_paper(database, db, 3, snapshot_id=published)
    put_in_library(database, owner.member_id, private)
    for paper in (private, excluded, counted):
        put_vector(db, paper, near(0))
    setup = setup_with(NeverCalled())

    def similar(viewer: MemberViewer, paper_id: uuid.UUID) -> RetrievalPage:
        page: RetrievalPage = retrieve(
            viewer,
            database,
            setup,
            paper_id=paper_id,
            scope=RetrievalScope.LAB,
            filters=PaperListFilters(scope=PaperListScope.LAB),
            k=20,
        )
        return page

    # 對照：A 拿自己的私人論文當起點可以（結果不含它自己）。
    assert ids(similar(owner, private)) == [counted]
    # B 看不到 A 的私人論文、被排除的文章、不存在的 id：三種都是同一個錯。
    for paper_id in (private, excluded, uuid.uuid4()):
        with pytest.raises(RetrievalAnchorNotFoundError) as caught:
            similar(other, paper_id)
        assert str(caught.value) == "找不到這篇論文"


def test_excluded_papers_never_appear_in_any_scope_or_path(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    kept = make_paper(database, db, 1, snapshot_id=published, title="Trust in ledgers")
    excluded = make_paper(
        database, db, 2, snapshot_id=published, title="Trust erratum", excluded_reason="erratum"
    )
    later = make_paper(database, db, 3, snapshot_id=published, title="Trust correction")
    for paper in (kept, excluded, later):
        put_vector(db, paper, near(0))
    # 被排除的文章加不進文獻庫；`later` 是加進去之後才被排除的。
    for paper in (kept, later):
        put_in_library(database, owner.member_id, paper)
    # 加進文獻庫、向量化之後才被排除的：全文檢索資料還在，查詢自己擋。
    execute(db, "UPDATE paper SET excluded_reason = 'erratum' WHERE id = %s", later)

    for scope in RetrievalScope.PERSONAL, RetrievalScope.LAB, RetrievalScope.TRACKED:
        assert ids(search(owner, database, "trust", scope=scope)) == [kept]
        similar = retrieve(
            owner,
            database,
            setup_with(NeverCalled()),
            paper_id=kept,
            scope=scope,
            filters=PaperListFilters(scope=PaperListScope(scope)),
            k=20,
        )
        assert similar.hits == ()

    # 取消排除之後重新出現在兩條腿（全文檢索資料一直都在，審查 M7）。
    execute(db, "UPDATE paper SET excluded_reason = NULL WHERE id = %s", later)
    for scope in RetrievalScope.PERSONAL, RetrievalScope.LAB, RetrievalScope.TRACKED:
        page = search(owner, database, "trust", scope=scope)
        assert set(ids(page)) == {kept, later}
        (back,) = [hit for hit in page.hits if hit.paper_id == later]
        assert back.vector_rank is not None
        assert back.text_rank is not None


def test_demo_scope_only_takes_public_viewer(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    demo = make_paper(database, db, 1, snapshot_id=published, title="Ledger adoption")
    counted = make_paper(database, db, 2, snapshot_id=published, title="Ledger trust")
    # 示範站選過、之後才被排除的：demo 範圍也不出現（審查 M7）。
    excluded = make_paper(database, db, 3, snapshot_id=published, title="Ledger erratum")
    execute(db, "UPDATE paper SET demo_eligible = true WHERE id IN (%s, %s)", demo, excluded)
    execute(db, "UPDATE paper SET excluded_reason = 'erratum' WHERE id = %s", excluded)
    for paper in (demo, counted, excluded):
        put_vector(db, paper, near(0))

    public = PublicViewer()
    assert ids(search(public, database, "ledger", scope=RetrievalScope.DEMO)) == [demo]
    for viewer in (owner, SystemViewer()):
        with pytest.raises(TypeError, match="PublicViewer"):
            retrieve(  # type: ignore[call-overload]
                viewer,
                database,
                setup_with(),
                query="ledger",
                scope=RetrievalScope.DEMO,
                filters=PaperListFilters(),
                k=20,
            )
    # 訪客用 tracked 看得到兩篇（`counted_paper`）；demo 只有示範站有完整內容的那一篇。
    assert set(ids(search(public, database, "ledger"))) == {demo, counted}


def test_demo_scope_is_refused_before_sending_the_query_or_reading_the_anchor(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    # 審查 H1：非 PublicViewer 的 demo 在分流前就擋，不送查詢向量（不占額度）、不讀起點。起點故意
    # 是「看得到但沒有向量」的論文：讀了起點就會回空結果而不是丟錯。
    anchor = make_paper(database, db, 1, snapshot_id=published, title="Ledger adoption")
    setup = setup_with(NeverCalled())
    for viewer in (owner, SystemViewer()):
        for target in ({"query": "ledger"}, {"paper_id": anchor}):
            with pytest.raises(TypeError, match="PublicViewer"):
                retrieve(  # type: ignore[call-overload]
                    viewer,
                    database,
                    setup,
                    scope=RetrievalScope.DEMO,
                    filters=PaperListFilters(),
                    k=20,
                    **target,
                )
    assert api_calls(db) == []
    assert execute(db, "SELECT count(*) FROM embedding_quota_hold") == [(0,)]


def test_filters_scope_must_match_the_retrieval_scope(
    database: Database, owner: MemberViewer
) -> None:
    with pytest.raises(ValueError, match="對不上"):
        search(
            owner,
            database,
            "trust",
            scope=RetrievalScope.TRACKED,
            filters=PaperListFilters(scope=PaperListScope.LAB),
        )
    with pytest.raises(ValueError, match="剛好給一個"):
        retrieve(
            owner,
            database,
            setup_with(),
            query="trust",
            paper_id=uuid.uuid4(),
            scope=RetrievalScope.TRACKED,
            filters=PaperListFilters(),
            k=20,
        )


# --- 篩選和找論文清單同一套 ---------------------------------------------------------------------


FILTER_CASES: list[PaperListFilters] = [
    PaperListFilters(),
    PaperListFilters(journal_issn_ls=(ISR,)),
    PaperListFilters(years=(2019, 2021)),
    PaperListFilters(has_abstract=True),
    PaperListFilters(has_abstract=False),
    PaperListFilters(this_week=True),
    PaperListFilters(topics=("erp",)),
    PaperListFilters(topics=("erp", "supply_chain")),
    PaperListFilters(topics=("erp", "supply_chain"), topic_match=MatchMode.ALL),
    PaperListFilters(topics=("unclassified",)),
    PaperListFilters(methods=("interview_case_study", "survey_sem")),
    PaperListFilters(methods=("interview_case_study", "survey_sem"), method_match=MatchMode.ALL),
    PaperListFilters(scope=PaperListScope.LAB, topics=("erp",)),
    PaperListFilters(scope=PaperListScope.PERSONAL, years=(2020,)),
]


@pytest.mark.parametrize("filters", FILTER_CASES, ids=lambda filters: repr(filters))
def test_filters_select_the_same_papers_as_the_paper_list(
    db: TestDatabase,
    database: Database,
    published: uuid.UUID,
    owner: MemberViewer,
    filters: PaperListFilters,
) -> None:
    # `published`（1）是上一次的快照；目前已發佈的是 2（`this_week` 看它）。
    older, latest = published, make_snapshot(db, 2)
    rows: list[tuple[str, int, bool, uuid.UUID, list[str], list[str]]] = [
        (MISQ, 2019, True, latest, ["erp"], ["survey_sem"]),
        (ISR, 2020, True, older, ["erp", "supply_chain"], ["survey_sem", "interview_case_study"]),
        (ISR, 2021, False, latest, ["supply_chain"], []),
        (MISQ, 2020, False, older, ["unclassified"], ["interview_case_study"]),
        (MISQ, 2021, True, latest, [], []),
    ]
    papers = []
    for number, (issn, year, abstract, snapshot, topics, methods) in enumerate(rows):
        paper = make_paper(
            database, db, number, snapshot_id=snapshot, issn_l=issn, year=year, abstract=abstract
        )
        if topics or methods:
            insert_classification(db, paper, topics, methods)
        put_vector(db, paper, near(number))
        put_in_library(database, owner.member_id, paper)
        papers.append(paper)
    outside = make_paper(database, db, 9, snapshot_id=None, issn_l=OUTSIDE, year=2020)
    insert_classification(db, outside, ["erp"])
    put_vector(db, outside, near(9))

    with database.transaction() as connection:
        expected = {
            paper_id
            for paper_id, _year in list_paper_list_ids(
                owner,
                connection,
                filters,
                current_snapshot_id=latest,
                after=None,
                limit=1000,
            )
        }
    assert expected  # 每一組條件都至少選得到一篇（空對空證明不了什麼）
    # 中文查詢：全文檢索腿找不到，結果全部來自向量腿（每一篇都很像），所以只差在條件。
    found = search(owner, database, "測試", scope=RetrievalScope(filters.scope), filters=filters)

    assert set(ids(found)) == expected
    assert found.total == len(expected)
    # 每一篇的標題都有 study：兩條腿都有命中，各自也只拿到同一批
    # （審查 M7：全文腿漏套條件會被抓到）。
    both = search(owner, database, "study", scope=RetrievalScope(filters.scope), filters=filters)
    assert {hit.paper_id for hit in both.hits if hit.vector_rank is not None} == expected
    assert {hit.paper_id for hit in both.hits if hit.text_rank is not None} == expected
    assert both.total == len(expected)


# --- 找相似論文 ---------------------------------------------------------------------------------


def test_similar_papers_use_the_stored_vector_exclude_the_paper_and_never_call_the_provider(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    anchor = make_paper(database, db, 1, snapshot_id=published)
    close = make_paper(database, db, 2, snapshot_id=published)
    farther = make_paper(database, db, 3, snapshot_id=published)
    unlike = make_paper(database, db, 4, snapshot_id=published)
    put_vector(db, anchor, near(0))
    put_vector(db, close, near(1))
    put_vector(db, farther, near(6))
    put_vector(db, unlike, FAR)

    page = retrieve(
        owner,
        database,
        setup_with(NeverCalled()),
        paper_id=anchor,
        scope=RetrievalScope.TRACKED,
        filters=PaperListFilters(),
        k=20,
    )

    assert ids(page) == [close, farther]  # 依相似度排、不含自己、低於下限的不算
    assert [hit.text_rank for hit in page.hits] == [None, None]
    assert [hit.vector_rank for hit in page.hits] == [1, 2]
    assert api_calls(db) == []  # 不佔每日額度


def test_similar_papers_of_a_paper_without_a_vector_is_an_empty_result(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    anchor = make_paper(database, db, 1, snapshot_id=published)
    other = make_paper(database, db, 2, snapshot_id=published)
    put_vector(db, other, near(0))

    page = retrieve(
        owner,
        database,
        setup_with(NeverCalled()),
        paper_id=anchor,
        scope=RetrievalScope.TRACKED,
        filters=PaperListFilters(),
        k=20,
    )

    assert page.hits == ()
    assert page.total == 0


# --- 查詢字串 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["", "   ", "‍", "trust\x00", "a‮b", "line break", "x" * 51, "﻿trust"],
    ids=["empty", "spaces", "zwj-only", "nul", "bidi", "line-separator", "too-long", "bom"],
)
def test_bad_queries_are_rejected_before_the_provider_is_called(
    db: TestDatabase, database: Database, owner: MemberViewer, query: str
) -> None:
    provider = QueryVectors()

    with pytest.raises(InvalidSearchQueryError):
        search(owner, database, query, setup=setup_with(provider))

    assert provider.queries == []
    assert api_calls(db) == []


def test_query_is_trimmed_and_never_logged(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    paper = make_paper(database, db, 1, snapshot_id=published, title="Trust in ledgers")
    put_vector(db, paper, near(0))
    provider = QueryVectors()
    secret = "  distinctive-query-words 信任  "

    with capture_logs() as logs:
        search(owner, database, secret, setup=setup_with(provider))

    assert provider.queries == ["distinctive-query-words 信任"]
    assert "distinctive-query-words" not in repr(logs)
    facts = [entry for entry in logs if entry.get("event") == "檢索：搜尋"]
    assert len(facts) == 1
    assert {key: facts[0][key] for key in ("scope", "query_length", "query_has_cjk")} == {
        "scope": "tracked",
        "query_length": 26,
        "query_has_cjk": True,
    }
    assert api_calls(db) == [("embed_query", "succeeded")]


def test_full_text_document_follows_title_and_current_abstract(
    db: TestDatabase, database: Database, published: uuid.UUID, owner: MemberViewer
) -> None:
    paper = make_paper(database, db, 1, snapshot_id=published, title="Ledger adoption")

    def text_hits(query: str) -> list[uuid.UUID]:
        page = search(owner, database, query, setup=setup_with())
        return [hit.paper_id for hit in page.hits if hit.text_rank is not None]

    assert text_hits("ledger") == [paper]
    execute(db, "UPDATE paper SET title = 'Blockchain governance' WHERE id = %s", paper)
    assert text_hits("ledger") == []
    assert text_hits("governance") == [paper]
    execute(
        db, "UPDATE paper_abstract SET content = 'Escrow contracts.' WHERE paper_id = %s", paper
    )
    assert text_hits("escrow") == [paper]
    execute(db, "DELETE FROM paper_abstract WHERE paper_id = %s", paper)
    assert text_hits("escrow") == []
