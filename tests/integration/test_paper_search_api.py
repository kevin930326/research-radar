"""找論文清單 API 的搜尋（`q`）與找相似論文（`similar_to`）對真的 PostgreSQL（Story 4.1 PR B、
FR-26、FR-27、FR-7、ADR-0005、ADR-0050 的 2026-11 更新）。

- `q`、`similar_to` 互斥（兩個都給 422）；`q` 不收的字 422，且不呼叫 provider、不記帳；`demo` 不從
  這個端點開放（`scope=demo` 422）。
- 搜尋照相關度排（和 `services/retrieval.retrieve` 同一個順序）；名次位移的分頁前後不重複不漏、
  `total` 等於翻得到的篇數；位置字串和清單的 keyset 位置、另一種檢索的位置互相不收（422）。
- 找相似論文：不呼叫 provider（一被呼叫就失敗）、不寫 `api_call`、不含起點；起點不存在、看不到
  （別人的私人論文）、被排除都是同一個 404。
- 換人：`personal` 搜尋看不到別人的文獻庫；別人的私人論文當 `similar_to` 是 404。
- 錯誤：額度用完 503 `search_quota_exhausted`（記 `quota_exhausted`）、連不上 503
  `search_embedding_unavailable`（記 `unreachable`）；查詢向量的時限比背景短，逾時一樣是「連不上」
  並記帳（真的 adapter 接 `httpx2.MockTransport`、假時鐘，不真的等）；api 沒有 Cloudflare 金鑰時
  搜尋回「連不上」、不記帳，找相似論文照常。
- 沒帶新參數時：回應和原本的清單逐欄一樣（同一組資料用 services 直接組的回應比對），也不碰檢索。
- 日誌不記查詢內容。

向量是測試自己編的 8 維數字（模型 `fake-embedding`）；標題、摘要、email 都是測試自己編的。
"""

import base64
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx2
import pytest
import stamina
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from structlog.testing import capture_logs

from radar.adapters.fake.login import pick_member_identity
from radar.adapters.workers_ai import WorkersAiEmbeddingProvider
from radar.domain.auth_settings import AuthSettings, Environment
from radar.domain.embeddings import EmbeddingConfig, EmbeddingTransientError
from radar.domain.paper_list import (
    PaperListCursor,
    PaperListFilters,
    PaperListQuery,
    PaperListScope,
    decode_paper_list_cursor,
    encode_paper_list_cursor,
    parse_paper_list_filters,
)
from radar.domain.paper_page import journal_key_for
from radar.domain.policy import CloudPolicy
from radar.domain.retrieval import (
    RetrievalCursor,
    RetrievalMode,
    RetrievalScope,
    encode_retrieval_cursor,
)
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import SESSION_COOKIE, build_auth_routes
from radar.entrypoints.api.main import install_api_routes
from radar.entrypoints.api.paper_list import PaperSearchSetup, paper_list_response
from radar.entrypoints.logging_config import configure_logging
from radar.repo.db import Database
from radar.services import embeddings as embeddings_service
from radar.services import paper_list as paper_list_service
from radar.services import retrieval as retrieval_service
from radar.services.members import allow_member
from radar.services.retrieval import RetrievalSetup, retrieve
from tests.database import TestDatabase, migrate
from tests.integration.test_embeddings import CONFIG, MODEL, NOW, _config
from tests.integration.test_paper_list_api import (
    ISR,
    make_paper,
    make_snapshot,
    put_in_library,
)
from tests.integration.test_retrieval import (
    FAR,
    RETRIEVAL,
    NeverCalled,
    QueryVectors,
    near,
    put_vector,
)
from tests.llm_support import (
    create_hidden_paper,
    execute,
    library_routes,
    paper_list_routes,
    paper_page_setup,
    paper_routes,
)

DEV_ORIGIN = "http://localhost:8000"
OWNER_EMAIL = "search.owner@example.com"
OTHER_EMAIL = "search.other@example.com"
NOT_FOUND = {"type": "about:blank", "title": "Not Found", "status": 404, "code": "not_found"}
PROBLEM_422 = {
    "type": "about:blank",
    "title": "Unprocessable Content",
    "status": 422,
    "code": "request_invalid",
}
QUOTA_503 = {
    "type": "about:blank",
    "title": "Service Unavailable",
    "status": 503,
    "code": "search_quota_exhausted",
}
UNAVAILABLE_503 = {**QUOTA_503, "code": "search_embedding_unavailable"}
# 每日免費額度 1 neuron（約 930 個估計 token）。
SMALL_QUOTA = {
    "daily_free_neurons": 1,
    "neurons_per_million_input_tokens": 1075,
    "stop_at_ratio": "0.8",
    "verified_on": "2026-11-15",
    "source_url": "https://developers.cloudflare.com/workers-ai/platform/pricing/",
}
# 查詢向量自己的時限：逾時 2 秒、總時限 3 秒、2 次（背景是 30 秒、480 秒、3 次）。
SHORT_QUERY = _config(
    query_request={"timeout_seconds": 2, "total_timeout_seconds": 3, "attempts": 2}
)
FAKE_ACCOUNT = "0123456789abcdef0123456789abcdef"
FAKE_TOKEN = "fake-token-for-tests"

type Params = Sequence[tuple[str, str]]


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    execute(
        fresh_database,
        "INSERT INTO embedding_model (model_id, provider, dimensions) VALUES (%s, 'fake', 8)",
        MODEL,
    )
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=3)


@pytest.fixture
def published(db: TestDatabase) -> uuid.UUID:
    return make_snapshot(db, 1)


def search_setup(provider: Any, embedding: EmbeddingConfig = CONFIG) -> PaperSearchSetup:
    return PaperSearchSetup(
        embedding=embedding,
        retrieval=RETRIEVAL,
        cloud_policy=CloudPolicy(),
        provider=provider,
        clock=lambda: NOW,
    )


class Api:
    """真的 app（正式的 `install_api_routes`）＋兩位登入的成員；搜尋用 `search` 的 provider
    與設定。"""

    def __init__(self, database: Database, search: PaperSearchSetup) -> None:
        settings = AuthSettings(environment=Environment.DEVELOPMENT, fake_login=True)
        app = create_app(readiness_check=lambda: True)
        install_api_routes(
            app,
            auth_routes=build_auth_routes(database, settings, pick_member_identity, lambda: NOW),
            paper_routes=paper_routes(database, now=NOW),
            library_routes=library_routes(database),
            paper_list_routes=paper_list_routes(database, search=search),
        )
        self.client = TestClient(app, base_url="https://testserver")
        self.owner = self._log_in(database, OWNER_EMAIL, "測試甲")
        self.other = self._log_in(database, OTHER_EMAIL, "測試乙")

    def _log_in(self, database: Database, email: str, name: str) -> tuple[MemberViewer, str]:
        member_id = allow_member(SystemViewer(), database, email=email, display_name=name).member_id
        response = self.client.post(
            "/api/v1/auth/fake-login",
            json={"member_id": str(member_id)},
            headers={"Origin": DEV_ORIGIN},
        )
        assert response.status_code == 200, response.text
        self.client.cookies.clear()
        name_value, _, rest = response.headers["set-cookie"].partition("=")
        assert name_value == SESSION_COOKIE
        return MemberViewer(member_id=member_id, is_admin=False), rest.split(";", 1)[0]

    def get(self, params: Params = (), *, as_other: bool = False) -> Response:
        _viewer, token = self.other if as_other else self.owner
        self.client.cookies.clear()
        return self.client.get(
            "/api/v1/papers", params=tuple(params), headers={"Cookie": f"{SESSION_COOKIE}={token}"}
        )

    def ok(self, params: Params = (), *, as_other: bool = False) -> dict[str, Any]:
        response = self.get(params, as_other=as_other)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body: dict[str, Any] = response.json()
        return body

    def walk(self, params: Params = ()) -> list[dict[str, Any]]:
        pages = [self.ok(params)]
        while pages[-1]["next_cursor"] is not None:
            pages.append(self.ok([*params, ("cursor", pages[-1]["next_cursor"])]))
        return pages


def problem(response: Response, expected: dict[str, object], *, by_framework: bool = False) -> None:
    """錯誤回應剛好是 `expected`。這個端點自己丟的錯誤帶 `no-store`；框架擋下的型別錯誤（不認得的
    `scope`、不是 UUID 的 `similar_to`）不帶（ADR-0050「已知限制」，共用處理器的行為）。"""
    assert response.status_code == expected["status"], response.text
    assert response.headers["content-type"] == "application/problem+json"
    if not by_framework:
        assert response.headers["cache-control"] == "no-store"
    assert response.json() == expected


def ids(body: dict[str, Any]) -> list[uuid.UUID]:
    return [uuid.UUID(item["id"]) for item in body["items"]]


def api_calls(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db,
        "SELECT operation, outcome, http_status FROM api_call ORDER BY created_at, id",
    )


def trust_papers(
    database: Database, db: TestDatabase, published: uuid.UUID, count: int
) -> list[uuid.UUID]:
    """`count` 篇標題有 trust、向量和查詢很像的論文（兩條腿都找得到）。"""
    found = []
    for number in range(count):
        paper_id = make_paper(
            database, db, number, snapshot_id=published, title=f"Trust study {number}"
        )
        put_vector(db, paper_id, near(number % 10))
        found.append(paper_id)
    return found


# --- 互斥、驗證、範圍 --------------------------------------------------------------------------


def test_q_and_similar_to_together_is_422(database: Database, published: uuid.UUID) -> None:
    provider = NeverCalled()
    api = Api(database, search_setup(provider))

    problem(api.get([("q", "trust"), ("similar_to", str(uuid.uuid4()))]), PROBLEM_422)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "‍",  # 只有零寬連接也算空的
        "a" * 51,  # 這裡的長度上限是 50
        "tr\x00ust",
        "trust\nsignals",
        "tr​ust",  # 看不見的格式字元
        "‮trust",  # 雙向控制字元
    ],
)
def test_bad_q_is_422_without_calling_the_provider(
    db: TestDatabase, database: Database, published: uuid.UUID, value: str
) -> None:
    api = Api(database, search_setup(NeverCalled()))

    problem(api.get([("q", value)]), PROBLEM_422)
    assert api_calls(db) == []


@pytest.mark.parametrize("extra", [[("q", "trust")], [("similar_to", str(uuid.uuid4()))]])
def test_demo_scope_is_not_open_on_this_endpoint(
    db: TestDatabase, database: Database, extra: list[tuple[str, str]]
) -> None:
    api = Api(database, search_setup(NeverCalled()))

    problem(api.get([*extra, ("scope", "demo")]), PROBLEM_422, by_framework=True)
    assert api_calls(db) == []


def test_bad_similar_to_is_422(database: Database) -> None:
    api = Api(database, search_setup(NeverCalled()))

    problem(api.get([("similar_to", "not-a-uuid")]), PROBLEM_422, by_framework=True)


def test_bad_filters_are_422_with_q_too(database: Database) -> None:
    provider = QueryVectors()
    api = Api(database, search_setup(provider))

    problem(api.get([("q", "trust"), ("journal", "nope")]), PROBLEM_422)
    assert provider.queries == []


# --- 搜尋：排序、分頁、total -------------------------------------------------------------------


def test_search_pages_by_rank_offset_without_gaps_or_repeats(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    papers = trust_papers(database, db, published, 45)
    provider = QueryVectors()
    api = Api(database, search_setup(provider))
    owner, _token = api.owner

    pages = api.walk([("q", "trust")])

    # 每翻一頁都重新算一次查詢向量（每頁一筆 api_call）。
    assert api_calls(db) == [("embed_query", "succeeded", 200)] * 3
    assert provider.queries == ["trust"] * 3
    expected = retrieve(
        owner,
        database,
        RetrievalSetup(
            embedding=CONFIG,
            retrieval=RETRIEVAL,
            cloud_policy=CloudPolicy(),
            provider=QueryVectors(),
            clock=lambda: NOW,
        ),
        query="trust",
        scope=RetrievalScope.TRACKED,
        filters=PaperListFilters(),
        k=200,
    )
    assert [len(page["items"]) for page in pages] == [20, 20, 5]
    seen = [paper_id for page in pages for paper_id in ids(page)]
    assert seen == [hit.paper_id for hit in expected.hits]
    assert sorted(seen) == sorted(papers)
    assert all(page["total"] == 45 == expected.total for page in pages)
    assert all(page["snapshot"] == {"number": 1, "as_of": "2026-11-16T02:30:00Z"} for page in pages)
    # 第二頁的位置就是名次位移 20。
    assert pages[0]["next_cursor"] == encode_retrieval_cursor(
        RetrievalCursor(mode=RetrievalMode.SEARCH, offset=20)
    )


def test_exactly_one_full_page_has_no_next_cursor(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    trust_papers(database, db, published, 20)
    api = Api(database, search_setup(QueryVectors()))

    body = api.ok([("q", "trust")])

    assert (len(body["items"]), body["total"], body["next_cursor"]) == (20, 20, None)


def test_search_respects_filters_and_scope(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    misq = make_paper(database, db, 1, snapshot_id=published, title="Trust in MISQ")
    isr = make_paper(database, db, 2, snapshot_id=published, issn_l=ISR, title="Trust in ISR")
    api = Api(database, search_setup(QueryVectors()))

    assert ids(api.ok([("q", "trust"), ("journal", "misq")])) == [misq]
    assert set(ids(api.ok([("q", "trust")]))) == {misq, isr}
    assert ids(api.ok([("q", "trust"), ("year", "2019")])) == []


def test_search_with_no_match_is_an_empty_page(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    make_paper(database, db, 1, snapshot_id=published, title="Governance")
    api = Api(database, search_setup(QueryVectors()))

    body = api.ok([("q", "blockchain")])

    assert body == {
        "items": [],
        "next_cursor": None,
        "total": 0,
        "snapshot": {"number": 1, "as_of": "2026-11-16T02:30:00Z"},
    }


def test_offset_past_the_end_is_an_empty_page(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    trust_papers(database, db, published, 3)
    api = Api(database, search_setup(QueryVectors()))
    cursor = encode_retrieval_cursor(RetrievalCursor(mode=RetrievalMode.SEARCH, offset=100))

    body = api.ok([("q", "trust"), ("cursor", cursor)])

    assert (body["items"], body["total"], body["next_cursor"]) == ([], 3, None)


# --- 位置字串互相不收 ---------------------------------------------------------------------------


def test_cursors_are_not_interchangeable(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    anchor = make_paper(database, db, 1, snapshot_id=published, title="Trust anchor")
    put_vector(db, anchor, near(0))
    provider = QueryVectors()
    api = Api(database, search_setup(provider))
    listed = encode_paper_list_cursor(PaperListCursor(year=2020, paper_id=anchor))
    search = encode_retrieval_cursor(RetrievalCursor(mode=RetrievalMode.SEARCH, offset=20))
    similar = encode_retrieval_cursor(RetrievalCursor(mode=RetrievalMode.SIMILAR, offset=20))

    # 清單的位置拿去搜尋、找相似論文；檢索的位置拿去翻清單；兩種檢索互換。
    problem(api.get([("q", "trust"), ("cursor", listed)]), PROBLEM_422)
    problem(api.get([("similar_to", str(anchor)), ("cursor", listed)]), PROBLEM_422)
    problem(api.get([("cursor", search)]), PROBLEM_422)
    problem(api.get([("cursor", similar)]), PROBLEM_422)
    problem(api.get([("q", "trust"), ("cursor", similar)]), PROBLEM_422)
    problem(api.get([("similar_to", str(anchor)), ("cursor", search)]), PROBLEM_422)
    raw = base64.urlsafe_b64encode(b"search:abc").decode().rstrip("=")
    problem(api.get([("q", "trust"), ("cursor", raw)]), PROBLEM_422)
    # 位置不對時不送查詢、不記帳。
    assert provider.queries == []
    assert api_calls(db) == []


# --- 找相似論文 -------------------------------------------------------------------------------


def test_similar_to_excludes_the_anchor_and_never_calls_the_provider(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    anchor = make_paper(database, db, 0, snapshot_id=published, title="Anchor")
    put_vector(db, anchor, near(0))
    others = []
    for number in range(1, 24):
        paper_id = make_paper(database, db, number, snapshot_id=published)
        put_vector(db, paper_id, near(number))
        others.append(paper_id)
    far = make_paper(database, db, 99, snapshot_id=published)
    put_vector(db, far, FAR)
    api = Api(database, search_setup(NeverCalled()))

    pages = api.walk([("similar_to", str(anchor))])

    seen = [paper_id for page in pages for paper_id in ids(page)]
    assert [len(page["items"]) for page in pages] == [20, 3]
    # 照相似度排（step 越小越像），不含起點、不含低於下限的那一篇。
    assert seen == others
    assert all(page["total"] == 23 for page in pages)
    assert pages[0]["next_cursor"] == encode_retrieval_cursor(
        RetrievalCursor(mode=RetrievalMode.SIMILAR, offset=20)
    )
    assert api_calls(db) == []


def test_anchor_without_a_vector_is_an_empty_page(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    anchor = make_paper(database, db, 0, snapshot_id=published)
    other = make_paper(database, db, 1, snapshot_id=published)
    put_vector(db, other, near(0))
    api = Api(database, search_setup(NeverCalled()))

    body = api.ok([("similar_to", str(anchor))])

    assert (body["items"], body["total"], body["next_cursor"]) == ([], 0, None)


def test_missing_hidden_and_excluded_anchors_are_the_same_404(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    api = Api(database, search_setup(NeverCalled()))
    owner, _token = api.owner
    hidden = create_hidden_paper(database, doi="10.9/search-hidden", openalex_id="W77")
    put_vector(db, hidden, near(0))
    put_in_library(database, owner.member_id, hidden)
    excluded = make_paper(
        database, db, 2, snapshot_id=published, excluded_reason="erratum", title="Excluded"
    )
    put_vector(db, excluded, near(0))
    visible = make_paper(database, db, 3, snapshot_id=published)
    put_vector(db, visible, near(1))

    for scope in ("tracked", "lab", "personal"):
        for anchor in (uuid.uuid4(), excluded):
            problem(api.get([("similar_to", str(anchor)), ("scope", scope)]), NOT_FOUND)
        # 別人的私人論文（在 A 的文獻庫）：B 看不到，和不存在一樣。
        problem(api.get([("similar_to", str(hidden)), ("scope", scope)], as_other=True), NOT_FOUND)
    # 對照：A 自己拿它當起點找得到別的論文。
    assert ids(api.ok([("similar_to", str(hidden))])) == [visible]
    assert api_calls(db) == []


# --- 換人 -------------------------------------------------------------------------------------


def test_personal_search_never_sees_another_members_library(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    api = Api(database, search_setup(QueryVectors()))
    owner, _token = api.owner
    mine = create_hidden_paper(
        database, doi="10.9/search-mine", openalex_id="W78", title="Trust kept privately"
    )
    put_vector(db, mine, near(0))
    put_in_library(database, owner.member_id, mine)

    other = api.ok([("q", "trust"), ("scope", "personal")], as_other=True)
    assert (other["items"], other["total"], other["next_cursor"]) == ([], 0, None)
    # lab、tracked 也看不到別人的私人論文。
    for scope in ("lab", "tracked"):
        assert mine not in ids(api.ok([("q", "trust"), ("scope", scope)], as_other=True))
    # 對照：A 自己的 personal 找得到。
    assert ids(api.ok([("q", "trust"), ("scope", "personal")])) == [mine]


def test_lab_search_finds_uncounted_lab_papers_and_tracked_does_not(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    # 審查 M8：一篇全實驗室看得到、但還沒算進任何快照（不在 `counted_paper`）的論文：`lab` 找得到，
    # `tracked` 找不到（兩個範圍誤用同一套條件就會失敗）。
    counted = make_paper(database, db, 1, snapshot_id=published, title="Trust counted")
    uncounted = make_paper(database, db, 2, snapshot_id=None, title="Trust not yet counted")
    for paper_id in (counted, uncounted):
        put_vector(db, paper_id, near(0))
    api = Api(database, search_setup(QueryVectors()))

    assert set(ids(api.ok([("q", "trust"), ("scope", "lab")]))) == {counted, uncounted}
    assert ids(api.ok([("q", "trust"), ("scope", "tracked")])) == [counted]
    # 找相似論文同一套範圍。
    anchor = make_paper(database, db, 3, snapshot_id=published, title="Anchor")
    put_vector(db, anchor, near(1))
    assert set(ids(api.ok([("similar_to", str(anchor)), ("scope", "lab")]))) == {
        counted,
        uncounted,
    }
    assert ids(api.ok([("similar_to", str(anchor)), ("scope", "tracked")])) == [counted]


# --- 錯誤：額度用完、連不上、逾時、沒有金鑰 ------------------------------------------------------


def test_quota_exhausted_is_503_and_recorded(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    trust_papers(database, db, published, 2)
    execute(
        db,
        "INSERT INTO api_call (source, operation, http_status, cost_usd, estimated_input_tokens,"
        " called_at) VALUES ('workers_ai', 'embed_documents', 200, 0, 1000, %s)",
        NOW,
    )
    provider = QueryVectors()
    api = Api(database, search_setup(provider, _config(quota=SMALL_QUOTA)))

    problem(api.get([("q", "trust")]), QUOTA_503)

    assert provider.queries == []  # 沒有送出
    assert api_calls(db)[1:] == [("embed_query", "quota_exhausted", None)]
    # 找相似論文不占額度，照常。
    anchor, *_ = execute(db, "SELECT id FROM paper ORDER BY id LIMIT 1")[0]
    assert api.get([("similar_to", str(anchor))]).status_code == 200


class _Unreachable:
    """embed_query 丟連不上的替身（像 adapter 重試用完）。"""

    model_id = MODEL

    def embed_documents(self, texts: Any) -> list[list[float]]:
        raise AssertionError("不該呼叫")

    def embed_query(self, text: Any) -> list[float]:
        raise EmbeddingTransientError("重試用完仍失敗（ConnectError）")


def test_unreachable_is_503_and_recorded(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    trust_papers(database, db, published, 2)
    api = Api(database, search_setup(_Unreachable()))

    problem(api.get([("q", "trust")]), UNAVAILABLE_503)

    assert api_calls(db) == [("embed_query", "unreachable", None)]


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def _no_retry_wait() -> Iterator[None]:
    with stamina.set_testing(True, attempts=100, cap=True):
        yield


def _workers_ai(
    handler: Callable[[httpx2.Request], httpx2.Response],
    clock: _FakeClock,
    config: EmbeddingConfig,
) -> WorkersAiEmbeddingProvider:
    return WorkersAiEmbeddingProvider(
        SecretStr(FAKE_ACCOUNT),
        SecretStr(FAKE_TOKEN),
        config.api,
        model_id=MODEL,
        dimensions=8,
        query_api=config.query_api,
        transport=httpx2.MockTransport(handler),
        monotonic=clock,
    )


@pytest.mark.usefixtures("_no_retry_wait")
def test_query_timeout_uses_the_short_limit_and_is_unavailable_and_recorded(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    # 真的 adapter：每次請求花 2 秒（撥假時鐘）、回 503。查詢的總時限 3 秒：第 1 次結束在 2 秒，再等
    # 就超過，不再試（背景的數字是 480 秒、3 次，照它會再試兩次）。
    trust_papers(database, db, published, 2)
    clock = _FakeClock()
    read_timeouts: list[float] = []

    def slow_503(request: httpx2.Request) -> httpx2.Response:
        read_timeouts.append(request.extensions["timeout"]["read"])
        clock.now += 2
        return httpx2.Response(503)

    api = Api(database, search_setup(_workers_ai(slow_503, clock, SHORT_QUERY), SHORT_QUERY))

    problem(api.get([("q", "trust")]), UNAVAILABLE_503)

    assert read_timeouts == [2]
    assert clock.now == 2
    assert api_calls(db) == [("embed_query", "unreachable", 503)]
    assert execute(db, "SELECT count(*) FROM embedding_quota_hold") == [(0,)]


@pytest.mark.usefixtures("_no_retry_wait")
def test_query_deadline_before_sending_is_unavailable_and_recorded_as_not_sent(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    # 拿到額度之後時間就過了總時限（例如等鎖）：一個請求都不送，記一列 not_sent。
    trust_papers(database, db, published, 1)
    readings = iter([0.0])

    def never(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("不該送出")

    provider = WorkersAiEmbeddingProvider(
        SecretStr(FAKE_ACCOUNT),
        SecretStr(FAKE_TOKEN),
        SHORT_QUERY.api,
        model_id=MODEL,
        dimensions=8,
        query_api=SHORT_QUERY.query_api,
        transport=httpx2.MockTransport(never),
        monotonic=lambda: next(readings, 4.0),
    )
    api = Api(database, search_setup(provider, SHORT_QUERY))

    problem(api.get([("q", "trust")]), UNAVAILABLE_503)

    assert api_calls(db) == [("embed_query", "not_sent", None)]


@pytest.mark.usefixtures("_no_retry_wait")
def test_query_read_timeout_is_unavailable_and_every_attempt_is_recorded(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    # 審查 M2：真的讀取逾時（httpx 的 ReadTimeout，不是 503）。查詢最多 2 次、每次的讀取逾時是查詢
    # 自己的 2 秒；兩次都記 unreachable（沒有狀態碼），占用清乾淨。
    trust_papers(database, db, published, 1)
    clock = _FakeClock()
    read_timeouts: list[float] = []

    def read_timeout(request: httpx2.Request) -> httpx2.Response:
        read_timeouts.append(request.extensions["timeout"]["read"])
        raise httpx2.ReadTimeout("slow")

    api = Api(database, search_setup(_workers_ai(read_timeout, clock, SHORT_QUERY), SHORT_QUERY))

    problem(api.get([("q", "trust")]), UNAVAILABLE_503)

    assert read_timeouts == [2, 2]
    assert api_calls(db) == [("embed_query", "unreachable", None)] * 2
    assert execute(db, "SELECT count(*) FROM embedding_quota_hold") == [(0,)]


@pytest.mark.usefixtures("_no_retry_wait")
def test_query_deadline_after_the_quota_hold_is_not_sent_and_released(
    db: TestDatabase, database: Database, published: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 審查 M2：嘗試前的額度確認（`before`）做完、占用還在時，時間就過了總時限（例如等額度鎖
    # 等很久）：不送，記一列 not_sent，占用清乾淨。
    trust_papers(database, db, published, 1)
    clock = _FakeClock()
    real_before = embeddings_service._QueryLedger._before
    held: list[int] = []

    def slow_before(self: Any) -> None:
        real_before(self)
        ((count,),) = execute(db, "SELECT count(*) FROM embedding_quota_hold")
        held.append(count)
        clock.now += 4  # 查詢的總時限是 3 秒

    monkeypatch.setattr(embeddings_service._QueryLedger, "_before", slow_before)

    def never(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("不該送出")

    api = Api(database, search_setup(_workers_ai(never, clock, SHORT_QUERY), SHORT_QUERY))

    problem(api.get([("q", "trust")]), UNAVAILABLE_503)

    assert held == [1]  # 確實是占住額度之後才到期
    assert api_calls(db) == [("embed_query", "not_sent", None)]
    assert execute(db, "SELECT count(*) FROM embedding_quota_hold") == [(0,)]


@pytest.mark.usefixtures("_no_retry_wait")
def test_query_response_that_ends_after_the_deadline_is_unavailable_and_recorded(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    # 審查 M1：完整的 JSON 在時限內送到，回應卻拖到 3 秒的總時限之後才結束：不當成功，503
    # `search_embedding_unavailable`，記 unreachable（沒有狀態碼），占用清乾淨。
    trust_papers(database, db, published, 1)
    clock = _FakeClock()
    body = (
        b'{"success": true, "errors": [], "result": {"shape": [1, 8], "data": [['
        + b", ".join([b"1.0"] + [b"0.0"] * 7)
        + b"]]}}"
    )

    def stream() -> Iterator[bytes]:
        yield body
        clock.now += 5

    def late_end(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=stream())

    api = Api(database, search_setup(_workers_ai(late_end, clock, SHORT_QUERY), SHORT_QUERY))

    problem(api.get([("q", "trust")]), UNAVAILABLE_503)

    assert api_calls(db) == [("embed_query", "unreachable", None)]
    assert execute(db, "SELECT count(*) FROM embedding_quota_hold") == [(0,)]


def test_without_cloudflare_keys_search_is_unavailable_and_similar_still_works(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    anchor = make_paper(database, db, 0, snapshot_id=published, title="Trust anchor")
    put_vector(db, anchor, near(0))
    other = make_paper(database, db, 1, snapshot_id=published)
    put_vector(db, other, near(1))
    api = Api(database, search_setup(None))

    problem(api.get([("q", "trust")]), UNAVAILABLE_503)
    # 不收的字照樣是 422（先驗再回「連不上」）。
    problem(api.get([("q", "")]), PROBLEM_422)
    assert api_calls(db) == []
    assert ids(api.ok([("similar_to", str(anchor))])) == [other]


# --- 沒帶新參數：和原本的清單逐欄一樣 ------------------------------------------------------------


def test_without_q_or_similar_to_the_response_is_the_list_field_by_field(
    db: TestDatabase, database: Database, published: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 同一組資料：API 的回應和直接用 services 的清單組出來的回應逐欄相同；不碰檢索、不記帳。
    for number in range(25):
        paper_id = make_paper(
            database, db, number, snapshot_id=published, year=2016 + number % 9, title="Trust"
        )
        put_vector(db, paper_id, near(number % 10))
    api = Api(database, search_setup(NeverCalled()))
    owner, _token = api.owner
    put_in_library(database, owner.member_id, paper_id)

    def no_retrieval(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("沒帶 q、similar_to 時不該呼叫檢索")

    monkeypatch.setattr(retrieval_service, "retrieve", no_retrieval)
    setup = paper_page_setup()

    def journal_key(issn_l: str | None) -> str | None:
        return journal_key_for(setup.journals, issn_l)

    cases: list[tuple[list[tuple[str, str]], PaperListQuery]] = [
        ([], PaperListQuery()),
        ([("scope", "lab")], PaperListQuery(scope=PaperListScope.LAB)),
        ([("scope", "personal")], PaperListQuery(scope=PaperListScope.PERSONAL)),
        (
            [("journal", "misq"), ("year", "2020"), ("year", "2021")],
            PaperListQuery(journals=("misq",), years=(2020, 2021)),
        ),
    ]
    for params, query in cases:
        filters = parse_paper_list_filters(query, setup.journals, setup.taxonomy)
        pages = api.walk(params)
        after = None
        for page in pages:
            direct = paper_list_service.list_papers(owner, database, filters, after=after)
            assert page == paper_list_response(direct, journal_key).model_dump(mode="json")
            if direct.next_cursor is not None:
                after = decode_paper_list_cursor(direct.next_cursor)
        assert sum(len(page["items"]) for page in pages) == pages[0]["total"]
    assert len(api.walk()) == 2  # 25 篇翻兩頁：真的比到了 next_cursor
    assert api_calls(db) == []


# --- 日誌 -------------------------------------------------------------------------------------


def test_query_text_never_reaches_the_logs(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    query_text = "zebra-quokka-7f1d trust"
    trust_papers(database, db, published, 2)
    api = Api(database, search_setup(QueryVectors()))

    with capture_logs() as logs:
        api.ok([("q", query_text)])
        problem(api.get([("q", query_text + "\x00")]), PROBLEM_422)

    assert logs
    assert not any("zebra" in str(record) for record in logs)
    searched = [record for record in logs if record.get("event") == "檢索：搜尋"]
    assert searched == [
        {
            "event": "檢索：搜尋",
            "log_level": "info",
            "scope": "tracked",
            "query_length": len(query_text),
            "query_has_cjk": False,
        }
    ]


# --- 同一頁的快照（審查 M3）-----------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["q", "similar_to"])
def test_this_week_and_the_snapshot_in_the_response_are_the_same_snapshot(
    db: TestDatabase,
    database: Database,
    published: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    # 排名之後、讀論文列之前剛好發佈了下一個快照：回應的 `snapshot` 要是篩 `this_week` 用的那一個
    # （第 1 號），不能是剛發佈的第 2 號（舊一週的 items 標成新一週）。
    anchor = make_paper(database, db, 0, snapshot_id=published, title="Trust anchor")
    put_vector(db, anchor, near(0))
    fresh = make_paper(database, db, 1, snapshot_id=published, title="Trust this week")
    put_vector(db, fresh, near(1))
    real_rows = paper_list_service.list_ranked_papers
    published_next: list[uuid.UUID] = []

    def rows_after_a_new_snapshot(*args: Any) -> Any:
        published_next.append(make_snapshot(db, 2))
        return real_rows(*args)

    monkeypatch.setattr(paper_list_service, "list_ranked_papers", rows_after_a_new_snapshot)
    api = Api(database, search_setup(QueryVectors()))
    value = "trust" if mode == "q" else str(anchor)

    body = api.ok([(mode, value), ("this_week", "true")])

    assert len(published_next) == 1
    assert fresh in ids(body)
    assert body["snapshot"] == {"number": 1, "as_of": "2026-11-16T02:30:00Z"}


def test_anchor_without_a_vector_still_reports_the_snapshot(
    db: TestDatabase, database: Database, published: uuid.UUID
) -> None:
    anchor = make_paper(database, db, 0, snapshot_id=published)
    api = Api(database, search_setup(NeverCalled()))

    body = api.ok([("similar_to", str(anchor))])

    assert body["snapshot"] == {"number": 1, "as_of": "2026-11-16T02:30:00Z"}


# --- 正式的日誌設定（審查 M7）---------------------------------------------------------------------


class _ThenUnreachable:
    """第一次照 `QueryVectors` 回向量，之後都連不上（同一個 app 走成功與 503 兩條路）。"""

    model_id = MODEL

    def __init__(self) -> None:
        self._vectors = QueryVectors()
        self.calls = 0

    def embed_documents(self, texts: Any) -> list[list[float]]:
        raise AssertionError("不該呼叫")

    def embed_query(self, text: Any) -> list[float]:
        self.calls += 1
        if self.calls == 1:
            return self._vectors.embed_query(text)
        raise EmbeddingTransientError("重試用完仍失敗（ConnectError）")


def test_query_text_never_reaches_the_formatted_logs(
    db: TestDatabase,
    database: Database,
    published: uuid.UUID,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 用正式的日誌設定（`configure_logging`：structlog 與標準函式庫的 logging 都輸出成 JSON，含
    # uvicorn、starlette 的紀錄器），送 200、422、503 三種帶測試標記的查詢：輸出裡一次都不能出現。
    # 日誌設定要在測試本體裡呼叫（handler 才綁到 capsys 這時的輸出）。
    marker = "zebra-quokka-7f1d"
    trust_papers(database, db, published, 2)
    api = Api(database, search_setup(_ThenUnreachable()))
    configure_logging()
    capsys.readouterr()

    api.ok([("q", f"{marker} trust")])
    problem(api.get([("q", f"{marker}\x00")]), PROBLEM_422)
    problem(api.get([("q", f"{marker} trust")]), UNAVAILABLE_503)
    problem(api.get([("q", marker), ("similar_to", str(uuid.uuid4()))]), PROBLEM_422)

    out = capsys.readouterr()
    logs = out.out + out.err
    finished = [line for line in logs.splitlines() if "請求完成" in line]
    assert len(finished) == 4  # 真的接到了這四個請求的日誌
    assert all('"path": "/api/v1/papers"' in line for line in finished)
    assert marker not in logs
    assert "zebra" not in logs
