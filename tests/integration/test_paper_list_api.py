"""找論文清單的 API 對真的 PostgreSQL（Story 3.13、AD-19、ADR-0011、ADR-0002、ADR-0047）：spec 的
I/O 表每一列（「點得回去」在 `test_paper_list_click_back.py`）。

- 預設：`tracked`，算數的論文依出版年新到舊，一頁 20 篇＋`next_cursor`、`total`、`snapshot`。
- 翻頁：45 篇、同一年多篇，照 `next_cursor` 翻到底是 20／20／5，合起來剛好 45 個不同 id、順序和一次
  全查一樣；`lab` 裡出版年是 NULL 的那一段也翻得過去。
- 期刊、年份多值是「或」；主題、方法多值是「且」；人修正過的主題以修正後的為準；`unclassified`
  查得到；`has_abstract` 只看目前的摘要；`this_week` 只看目前已發佈的快照（沒有已發佈的快照時沒有
  結果）。
- 範圍：`tracked` 不含被排除、範圍外、快照建立中、還沒有快照的；`lab` 含範圍外但全實驗室看得到的與
  快照還沒發佈的、不含被排除的與別人的私人論文；`personal` 只有自己的文獻庫（不含被排除的），B 的是
  空清單（`total` 0、`snapshot` 照常）。
- 不合法的值：422 `request_invalid`（ADR-0047 的四個欄位）；沒登入 401。

兩位成員經開發環境的假登入取得真的 session（路由用正式的 `install_api_routes`）。論文經正式的
`upsert_by_external_id` 寫入；快照、`first_snapshot_id`、不是目前的摘要用超級使用者直接寫（只在
測試裡）。資料全是測試自己編的（`*@example.com`、假標題）。
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import text

from radar.adapters.fake.login import pick_member_identity
from radar.domain.auth_settings import AuthSettings, Environment
from radar.domain.library import AddedVia
from radar.domain.papers import PaperOrigin, ParsedPaper
from radar.domain.scope import ScopeSettings
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import SystemViewer
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import SESSION_COOKIE, build_auth_routes
from radar.entrypoints.api.main import install_api_routes
from radar.repo import paper_list as paper_list_repo
from radar.repo.db import Database
from radar.repo.library import create_library_item
from radar.repo.papers import upsert_by_external_id
from radar.services import paper_list as paper_list_service
from radar.services.members import allow_member
from tests.database import TestDatabase, migrate
from tests.integration.test_effective_views import fix, insert_classification
from tests.llm_support import execute, library_routes, paper_list_routes, paper_routes

DEV_ORIGIN = "http://localhost:8000"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
FETCHED_AT = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
AS_OF = datetime(2026, 11, 16, 2, 30, tzinfo=UTC)
AS_OF_JSON = "2026-11-16T02:30:00Z"
OWNER_EMAIL = "list.owner@example.com"
OTHER_EMAIL = "list.other@example.com"
MISQ = "0276-7783"  # 設定檔的 misq
ISR = "1047-7047"  # isr
EJIS = "0960-085X"  # ejis
OUTSIDE = "9999-9994"  # 不是追蹤的期刊
SCOPE = ScopeSettings(frozenset({MISQ, ISR, EJIS}), start_year=2016)
PROBLEM_422 = {
    "type": "about:blank",
    "title": "Unprocessable Content",
    "status": 422,
    "code": "request_invalid",
}

type Params = Sequence[tuple[str, str]]


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


class Api:
    """真的 app（正式的 `install_api_routes`）＋兩位登入的成員（A 是 owner、B 是 other）。"""

    def __init__(self, database: Database) -> None:
        settings = AuthSettings(environment=Environment.DEVELOPMENT, fake_login=True)
        app = create_app(readiness_check=lambda: True)
        install_api_routes(
            app,
            auth_routes=build_auth_routes(database, settings, pick_member_identity, lambda: NOW),
            paper_routes=paper_routes(database, now=NOW),
            library_routes=library_routes(database),
            paper_list_routes=paper_list_routes(database),
        )
        self.client = TestClient(app, base_url="https://testserver")
        self.owner_id, self.owner_token = self._log_in(database, OWNER_EMAIL, "測試甲")
        self.other_id, self.other_token = self._log_in(database, OTHER_EMAIL, "測試乙")

    def _log_in(self, database: Database, email: str, name: str) -> tuple[uuid.UUID, str]:
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
        return member_id, rest.split(";", 1)[0]

    def papers(self, params: Params = (), *, token: str | None) -> Response:
        self.client.cookies.clear()
        headers = {} if token is None else {"Cookie": f"{SESSION_COOKIE}={token}"}
        return self.client.get("/api/v1/papers", params=tuple(params), headers=headers)

    def ok(self, params: Params = (), *, token: str | None = None) -> dict[str, Any]:
        """A（或 `token`）讀一頁，要 200 而且帶 `no-store`。"""
        response = self.papers(params, token=token or self.owner_token)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body: dict[str, Any] = response.json()
        return body

    def ids(self, params: Params = (), *, token: str | None = None) -> set[uuid.UUID]:
        """一頁之內的 id（篇數不超過一頁時就是全部）；`total` 要和篇數一樣。"""
        body = self.ok(params, token=token)
        assert body["next_cursor"] is None
        assert body["total"] == len(body["items"])
        return {uuid.UUID(item["id"]) for item in body["items"]}

    def walk(self, params: Params = (), *, token: str | None = None) -> list[dict[str, Any]]:
        """照 `next_cursor` 翻到底，回每一頁的本文。"""
        pages = [self.ok(params, token=token)]
        while pages[-1]["next_cursor"] is not None:
            pages.append(self.ok([*params, ("cursor", pages[-1]["next_cursor"])], token=token))
        return pages


@pytest.fixture
def api(database: Database) -> Api:
    return Api(database)


# --- 造資料（`test_paper_list_click_back.py` 也用）----------------------------------------------


def make_snapshot(db: TestDatabase, number: int, status: str = "published") -> uuid.UUID:
    """一個快照（`published` 的截至時間是 `AS_OF`），回它的 id。"""
    ((run_id,),) = execute(db, "INSERT INTO weekly_run (status) VALUES ('failed') RETURNING id")
    ((snapshot_id,),) = execute(
        db,
        "INSERT INTO snapshot (number, weekly_run_id, status, as_of) VALUES (%s, %s, %s, %s)"
        " RETURNING id",
        number,
        run_id,
        status,
        AS_OF if status == "published" else None,
    )
    return uuid.UUID(str(snapshot_id))


def make_paper(
    database: Database,
    db: TestDatabase,
    number: int,
    *,
    snapshot_id: uuid.UUID | None,
    issn_l: str = MISQ,
    year: int | None = 2020,
    abstract: bool = True,
    origin: PaperOrigin = PaperOrigin.TRACKED,
    excluded_reason: str | None = None,
    title: str | None = None,
) -> uuid.UUID:
    """經 `upsert_by_external_id` 新增一篇（追蹤範圍照 `SCOPE` 判定），指定 `first_snapshot_id`。"""
    fields: dict[str, object] = {
        "source": "openalex",
        "fetched_at": FETCHED_AT,
        "doi": f"10.9999/list.{number}",
        "openalex_id": f"W8{number:08d}",
        "title": title or f"Made-up list study {number}",
        "journal_issn_l": issn_l,
        "publication_year": year,
    }
    if abstract:
        fields |= {"abstract": f"Made-up abstract {number}.", "abstract_source": "openalex"}
    with database.transaction() as connection:
        result = upsert_by_external_id(
            connection,
            ParsedPaper.model_validate(fields),
            origin=origin,
            excluded_reason=excluded_reason,
            scope=SCOPE,
        )
    if snapshot_id is not None:
        execute(
            db,
            "UPDATE paper SET first_snapshot_id = %s WHERE id = %s",
            snapshot_id,
            result.paper_id,
        )
    return result.paper_id


def put_in_library(database: Database, owner_id: uuid.UUID, paper_id: uuid.UUID) -> None:
    """用系統身分把一篇放進成員的文獻庫（私人論文成員自己加不了）。"""
    with database.transaction() as connection:
        item = create_library_item(
            connection,
            SystemViewer(),
            owner_id=owner_id,
            paper_id=paper_id,
            added_via=AddedVia.UPLOAD,
        )
    assert item is not None


def expected_order(rows: Iterable[tuple[uuid.UUID, int | None]]) -> list[uuid.UUID]:
    """`publication_year DESC NULLS LAST, id DESC`（uuid 的比較和 PostgreSQL 一樣是位元組順序）。"""
    return [
        paper_id
        for paper_id, _year in sorted(
            rows, key=lambda row: (row[1] is None, -(row[1] or 0), -row[0].int)
        )
    ]


def _ids(page: dict[str, Any]) -> list[uuid.UUID]:
    return [uuid.UUID(item["id"]) for item in page["items"]]


# --- 預設與翻頁 ------------------------------------------------------------------------------


def test_default_is_tracked_newest_first_with_cursor_total_and_snapshot(
    db: TestDatabase, database: Database, api: Api
) -> None:
    published = make_snapshot(db, 1)
    made = [
        (make_paper(database, db, n, snapshot_id=published, year=2016 + n % 9), 2016 + n % 9)
        for n in range(23)
    ]

    body = api.ok()

    assert len(body["items"]) == 20
    assert body["next_cursor"] is not None
    assert body["total"] == 23
    assert body["snapshot"] == {"number": 1, "as_of": AS_OF_JSON}
    assert _ids(body) == expected_order(made)[:20]
    first = body["items"][0]
    assert (first["journal"], first["journal_issn_l"], first["in_scope"]) == ("misq", MISQ, True)
    assert first["has_abstract"] is True


def test_paging_45_papers_gives_20_20_5_without_gaps_or_repeats(
    db: TestDatabase, database: Database, api: Api
) -> None:
    published = make_snapshot(db, 1)
    # 同一年很多篇：30 篇 2020、15 篇 2019（頁界落在同一年裡）。
    made = [
        (make_paper(database, db, n, snapshot_id=published, year=year), year)
        for n, year in enumerate([2020] * 30 + [2019] * 15)
    ]

    pages = api.walk()

    assert [len(page["items"]) for page in pages] == [20, 20, 5]
    assert [page["total"] for page in pages] == [45, 45, 45]
    assert pages[-1]["next_cursor"] is None
    listed = [paper_id for page in pages for paper_id in _ids(page)]
    assert len(set(listed)) == 45
    full = execute(
        db, "SELECT id FROM counted_paper ORDER BY publication_year DESC NULLS LAST, id DESC"
    )
    assert listed == [uuid.UUID(str(row[0])) for row in full] == expected_order(made)


def test_paging_exactly_40_papers_gives_20_20_without_an_empty_page(
    db: TestDatabase, database: Database, api: Api
) -> None:
    # 總數剛好是一頁的倍數（Story 3.13 審查 T3）：第二頁滿 20 篇、沒有下一頁，不會多一頁空的。
    published = make_snapshot(db, 1)
    made = [
        (make_paper(database, db, n, snapshot_id=published, year=2020 - n % 3), 2020 - n % 3)
        for n in range(40)
    ]

    pages = api.walk()

    assert [len(page["items"]) for page in pages] == [20, 20]
    assert [page["total"] for page in pages] == [40, 40]
    assert pages[0]["next_cursor"] is not None
    assert pages[-1]["next_cursor"] is None
    assert [paper_id for page in pages for paper_id in _ids(page)] == expected_order(made)


def test_lab_paging_crosses_into_papers_without_a_year(
    db: TestDatabase, database: Database, api: Api
) -> None:
    # 範圍外但全實驗室看得到的論文（引用擴展），19 篇有出版年、3 篇沒有：第一頁的最後一篇沒有年份，
    # 第二頁的位置年份是 null。
    made = [
        (
            make_paper(
                database,
                db,
                n,
                snapshot_id=None,
                issn_l=OUTSIDE,
                year=year,
                origin=PaperOrigin.EXPANSION,
            ),
            year,
        )
        for n, year in enumerate([2010 + n % 4 for n in range(19)] + [None] * 3)
    ]

    pages = api.walk([("scope", "lab")])

    assert [len(page["items"]) for page in pages] == [20, 2]
    listed = [paper_id for page in pages for paper_id in _ids(page)]
    assert listed == expected_order(made)
    assert [item["publication_year"] for item in pages[1]["items"]] == [None, None]


def test_lab_paging_from_a_dated_cursor_reaches_papers_without_a_year(
    db: TestDatabase, database: Database, api: Api
) -> None:
    # 20 篇有出版年、2 篇沒有：第一頁剛好在有年份的最後一篇結束，第二頁的位置年份不是 null，
    # 沒有年份的那兩篇要靠 `OR publication_year IS NULL` 接上。
    made = [
        (
            make_paper(
                database,
                db,
                n,
                snapshot_id=None,
                issn_l=OUTSIDE,
                year=year,
                origin=PaperOrigin.EXPANSION,
            ),
            year,
        )
        for n, year in enumerate([2010 + n % 4 for n in range(20)] + [None] * 2)
    ]

    pages = api.walk([("scope", "lab")])

    assert [len(page["items"]) for page in pages] == [20, 2]
    assert [page["total"] for page in pages] == [22, 22]
    assert pages[0]["items"][-1]["publication_year"] is not None
    listed = [paper_id for page in pages for paper_id in _ids(page)]
    assert listed == expected_order(made)


# --- 篩選 ------------------------------------------------------------------------------------


def test_journals_and_years_are_or_within_and_across(
    db: TestDatabase, database: Database, api: Api
) -> None:
    published = make_snapshot(db, 1)
    misq_2020 = make_paper(database, db, 1, snapshot_id=published, issn_l=MISQ, year=2020)
    misq_2021 = make_paper(database, db, 2, snapshot_id=published, issn_l=MISQ, year=2021)
    make_paper(database, db, 3, snapshot_id=published, issn_l=MISQ, year=2022)
    isr_2020 = make_paper(database, db, 4, snapshot_id=published, issn_l=ISR, year=2020)
    make_paper(database, db, 5, snapshot_id=published, issn_l=EJIS, year=2020)

    found = api.ids([("journal", "misq"), ("journal", "isr"), ("year", "2020"), ("year", "2021")])

    assert found == {misq_2020, misq_2021, isr_2020}
    # 重複的值去重，不算錯。
    assert api.ids([("journal", "isr"), ("journal", "isr")]) == {isr_2020}


def test_topics_and_methods_all_is_and(db: TestDatabase, database: Database, api: Api) -> None:
    published = make_snapshot(db, 1)
    both = make_paper(database, db, 1, snapshot_id=published)
    only_erp = make_paper(database, db, 2, snapshot_id=published)
    none = make_paper(database, db, 3, snapshot_id=published)
    insert_classification(db, both, ["erp", "health_it"], ["experiment", "survey_sem"])
    insert_classification(db, only_erp, ["erp"], ["survey_sem"])

    # Story 3.20：多個值的「且」要明確帶 `*_match=all`（預設改成「或」，見下一個測試）。
    assert api.ids([("topic", "erp"), ("topic", "health_it"), ("topic_match", "all")]) == {both}
    assert api.ids([("topic", "erp")]) == {both, only_erp}
    assert api.ids(
        [("method", "survey_sem"), ("method", "experiment"), ("method_match", "all")]
    ) == {both}
    assert api.ids([("method", "survey_sem")]) == {both, only_erp}
    assert api.ids([("topic", "erp"), ("method", "experiment")]) == {both}
    # 沒有分類的那一篇不在上面任何一個結果裡，但不篩選時照列。
    assert api.ids() == {both, only_erp, none}


def test_topics_and_methods_default_to_any(db: TestDatabase, database: Database, api: Api) -> None:
    # Story 3.20、ADR-0011 的 2026-11-27 更新：沒帶 `*_match` 時多個值是「或」。
    published = make_snapshot(db, 1)
    both = make_paper(database, db, 1, snapshot_id=published)
    only_erp = make_paper(database, db, 2, snapshot_id=published)
    only_health = make_paper(database, db, 3, snapshot_id=published)
    none = make_paper(database, db, 4, snapshot_id=published)
    insert_classification(db, both, ["erp", "health_it"], ["experiment", "survey_sem"])
    insert_classification(db, only_erp, ["erp"], ["survey_sem"])
    insert_classification(db, only_health, ["health_it"], ["experiment"])

    two_topics = [("topic", "erp"), ("topic", "health_it")]
    two_methods = [("method", "survey_sem"), ("method", "experiment")]
    assert api.ids(two_topics) == {both, only_erp, only_health}
    assert api.ids([*two_topics, ("topic_match", "any")]) == {both, only_erp, only_health}
    assert api.ids([*two_topics, ("topic_match", "all")]) == {both}
    assert api.ids(two_methods) == {both, only_erp, only_health}
    assert api.ids([*two_methods, ("method_match", "any")]) == {both, only_erp, only_health}
    assert api.ids([*two_methods, ("method_match", "all")]) == {both}
    # `total` 和列出來的一樣（同一個條件組）。
    assert api.ok(two_topics)["total"] == 3
    assert api.ok([*two_topics, ("topic_match", "all")])["total"] == 1
    # 重複的 key 去重之後才數個數：`all` 和只給一個時一樣（審查 A-5）。
    assert api.ids([("topic", "erp"), ("topic", "erp"), ("topic_match", "all")]) == {
        both,
        only_erp,
    }
    assert none in api.ids()  # 沒分類的那篇只在不篩選時出現


@pytest.mark.parametrize("match", ["any", "all"])
def test_one_key_is_the_same_either_way(
    db: TestDatabase, database: Database, api: Api, match: str
) -> None:
    published = make_snapshot(db, 1)
    both = make_paper(database, db, 1, snapshot_id=published)
    only_erp = make_paper(database, db, 2, snapshot_id=published)
    make_paper(database, db, 3, snapshot_id=published)
    insert_classification(db, both, ["erp", "health_it"], ["experiment", "survey_sem"])
    insert_classification(db, only_erp, ["erp"], ["survey_sem"])

    assert api.ids([("topic", "erp"), ("topic_match", match)]) == {both, only_erp}
    assert api.ids([("method", "survey_sem"), ("method_match", match)]) == {both, only_erp}
    # 沒有給 key 時 `*_match` 不影響結果。
    assert len(api.ids([("topic_match", match), ("method_match", match)])) == 3


def test_topic_and_method_match_are_independent(
    db: TestDatabase, database: Database, api: Api
) -> None:
    published = make_snapshot(db, 1)
    both = make_paper(database, db, 1, snapshot_id=published)
    erp_survey = make_paper(database, db, 2, snapshot_id=published)
    health_experiment = make_paper(database, db, 3, snapshot_id=published)
    erp_both_methods = make_paper(database, db, 4, snapshot_id=published)
    insert_classification(db, both, ["erp", "health_it"], ["experiment", "survey_sem"])
    insert_classification(db, erp_survey, ["erp"], ["survey_sem"])
    insert_classification(db, health_experiment, ["health_it"], ["experiment"])
    insert_classification(db, erp_both_methods, ["erp"], ["experiment", "survey_sem"])
    params = [
        ("topic", "erp"),
        ("topic", "health_it"),
        ("method", "experiment"),
        ("method", "survey_sem"),
    ]

    # 主題任一個、方法同時符合：方法兩個都有的才算，主題有一個就好。
    assert api.ids([*params, ("method_match", "all")]) == {both, erp_both_methods}
    # 主題同時符合、方法任一個。
    assert api.ids([*params, ("topic_match", "all")]) == {both}
    # 兩個都同時符合、兩個都任一個。
    assert api.ids([*params, ("topic_match", "all"), ("method_match", "all")]) == {both}
    assert api.ids(params) == {both, erp_survey, health_experiment, erp_both_methods}


def test_any_match_respects_scope_visibility_and_pages(
    db: TestDatabase, database: Database, api: Api
) -> None:
    # 審查 A-4：「或」的新查法和三種範圍、換人、翻頁一起跑。
    published = make_snapshot(db, 1)
    counted = make_paper(database, db, 1, snapshot_id=published)
    expansion = make_paper(
        database, db, 2, snapshot_id=published, issn_l=OUTSIDE, origin=PaperOrigin.EXPANSION
    )
    private = make_paper(
        database,
        db,
        3,
        snapshot_id=None,
        issn_l=OUTSIDE,
        origin=PaperOrigin.IMPORT,
        title="A made-up private study",
    )
    excluded = make_paper(
        database, db, 4, snapshot_id=published, excluded_reason="editors_comments"
    )
    insert_classification(db, counted, ["erp"])
    insert_classification(db, expansion, ["health_it"])
    insert_classification(db, private, ["erp", "health_it"])
    insert_classification(db, excluded, ["erp"])
    put_in_library(database, api.owner_id, private)
    put_in_library(database, api.owner_id, counted)
    two_topics = [("topic", "erp"), ("topic", "health_it")]

    for token in (api.owner_token, api.other_token):
        assert api.ids(two_topics, token=token) == {counted}
        assert api.ids([*two_topics, ("scope", "lab")], token=token) == {counted, expansion}
    # A 的私人論文只在 A 的文獻庫；B 用「或」也帶不出來。
    assert api.ids([*two_topics, ("scope", "personal")]) == {private, counted}
    assert api.ids([*two_topics, ("scope", "personal")], token=api.other_token) == set()
    both = [*two_topics, ("scope", "personal"), ("topic_match", "all")]
    assert api.ids(both) == {private}

    # 超過一頁：照 `next_cursor` 翻到底，每篇只出現一次、合起來等於 `total`。
    many = {counted}
    for number in range(10, 35):
        paper_id = make_paper(database, db, number, snapshot_id=published, year=2016 + number % 5)
        insert_classification(db, paper_id, ["erp"] if number % 2 else ["health_it"])
        many.add(paper_id)
    pages = api.walk(two_topics)
    seen = [uuid.UUID(item["id"]) for page in pages for item in page["items"]]
    assert len(pages) == 2
    assert len(seen) == len(set(seen)) == pages[0]["total"] == len(many)
    assert set(seen) == many


def test_unclassified_with_another_topic(db: TestDatabase, database: Database, api: Api) -> None:
    # Kevin 2026-11-27 決定：「未分類」和別的一起給、又是 `all` 時不特別處理（照樣是「且」）。
    published = make_snapshot(db, 1)
    erp = make_paper(database, db, 1, snapshot_id=published)
    unclassified = make_paper(database, db, 2, snapshot_id=published)
    insert_classification(db, erp, ["erp"])
    insert_classification(db, unclassified, ["unclassified"])
    params = [("topic", "erp"), ("topic", "unclassified")]

    assert api.ids(params) == {erp, unclassified}
    assert api.ids([*params, ("topic_match", "all")]) == set()
    assert api.ok([*params, ("topic_match", "all")])["total"] == 0


def test_human_fix_wins_and_unclassified_is_a_topic(
    db: TestDatabase, database: Database, api: Api
) -> None:
    published = make_snapshot(db, 1)
    fixed = make_paper(database, db, 1, snapshot_id=published)
    unclassified = make_paper(database, db, 2, snapshot_id=published)
    insert_classification(db, fixed, ["erp"])
    fix(database, fixed, TaxonomyField.TOPIC, ["health_it"])
    insert_classification(db, unclassified, ["unclassified"])

    assert api.ids([("topic", "health_it")]) == {fixed}
    assert api.ids([("topic", "erp")]) == set()
    assert api.ids([("topic", "unclassified")]) == {unclassified}


def test_has_abstract_only_counts_the_current_abstract(
    db: TestDatabase, database: Database, api: Api
) -> None:
    published = make_snapshot(db, 1)
    with_abstract = make_paper(database, db, 1, snapshot_id=published)
    without = make_paper(database, db, 2, snapshot_id=published, abstract=False)
    only_old = make_paper(database, db, 3, snapshot_id=published, abstract=False)
    execute(
        db,
        "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
        " VALUES (%s, 'semantic_scholar', 'An older made-up abstract.', false)",
        only_old,
    )

    assert api.ids([("has_abstract", "false")]) == {without, only_old}
    assert api.ids([("has_abstract", "true")]) == {with_abstract}
    assert api.ids() == {with_abstract, without, only_old}


def test_this_week_is_the_current_published_snapshot(
    db: TestDatabase, database: Database, api: Api
) -> None:
    older = make_snapshot(db, 1)
    current = make_snapshot(db, 2)
    make_snapshot(db, 3, "building")
    make_paper(database, db, 1, snapshot_id=older)
    new = make_paper(database, db, 2, snapshot_id=current)

    body = api.ok([("this_week", "true")])

    assert [uuid.UUID(item["id"]) for item in body["items"]] == [new]
    assert body["snapshot"] == {"number": 2, "as_of": AS_OF_JSON}


def test_this_week_also_filters_lab_and_personal(
    db: TestDatabase, database: Database, api: Api
) -> None:
    # 本週新增在 `lab`、`personal` 一樣只看目前已發佈的快照（Story 3.13 審查 T5）。
    older = make_snapshot(db, 1)
    current = make_snapshot(db, 2)
    old_tracked = make_paper(database, db, 1, snapshot_id=older)
    new_tracked = make_paper(database, db, 2, snapshot_id=current)
    old_outside = make_paper(
        database, db, 3, snapshot_id=older, issn_l=OUTSIDE, origin=PaperOrigin.EXPANSION
    )
    new_outside = make_paper(
        database, db, 4, snapshot_id=current, issn_l=OUTSIDE, origin=PaperOrigin.EXPANSION
    )
    put_in_library(database, api.owner_id, old_tracked)
    put_in_library(database, api.owner_id, new_outside)

    assert api.ids([("scope", "lab")]) == {old_tracked, new_tracked, old_outside, new_outside}
    assert api.ids([("scope", "lab"), ("this_week", "true")]) == {new_tracked, new_outside}
    assert api.ids([("scope", "personal")]) == {old_tracked, new_outside}
    assert api.ids([("scope", "personal"), ("this_week", "true")]) == {new_outside}
    assert api.ids([("scope", "personal"), ("this_week", "true")], token=api.other_token) == set()


def test_this_week_without_a_published_snapshot_is_empty(
    db: TestDatabase, database: Database, api: Api
) -> None:
    building = make_snapshot(db, 1, "building")
    # lab 範圍不需要快照也列得出來；本週新增仍然沒有結果（不用現在時間算）。
    paper_id = make_paper(database, db, 1, snapshot_id=building)

    assert api.ids([("scope", "lab")]) == {paper_id}
    body = api.ok([("scope", "lab"), ("this_week", "true")])
    assert (body["items"], body["total"], body["snapshot"]) == ([], 0, None)


# --- 範圍 ------------------------------------------------------------------------------------


def test_scopes(db: TestDatabase, database: Database, api: Api) -> None:
    published = make_snapshot(db, 1)
    building = make_snapshot(db, 2, "building")
    counted = make_paper(database, db, 1, snapshot_id=published)
    excluded = make_paper(
        database, db, 2, snapshot_id=published, excluded_reason="editors_comments"
    )
    expansion = make_paper(
        database,
        db,
        3,
        snapshot_id=published,
        issn_l=OUTSIDE,
        origin=PaperOrigin.EXPANSION,
    )
    on_building = make_paper(database, db, 4, snapshot_id=building)
    unassigned = make_paper(database, db, 5, snapshot_id=None)
    private = make_paper(
        database,
        db,
        6,
        snapshot_id=None,
        issn_l=OUTSIDE,
        origin=PaperOrigin.IMPORT,
        title="A made-up private study",
    )
    later_excluded = make_paper(database, db, 7, snapshot_id=published)
    put_in_library(database, api.owner_id, private)
    put_in_library(database, api.owner_id, counted)
    # 加進文獻庫之後才被排除的：三種範圍都不列（和論文頁 404 一致）。
    put_in_library(database, api.owner_id, later_excluded)
    execute(db, "UPDATE paper SET excluded_reason = 'erratum' WHERE id = %s", later_excluded)
    # 被排除的兩篇、A 的私人論文都不在 lab；範圍外但全實驗室看得到的、快照建立中的、還沒有快照的在。
    lab = {counted, expansion, on_building, unassigned}

    for token in (api.owner_token, api.other_token):
        assert api.ids(token=token) == {counted}
        assert api.ids([("scope", "tracked")], token=token) == {counted}
        assert api.ids([("scope", "lab")], token=token) == lab
    assert {excluded, later_excluded, private}.isdisjoint(lab)
    assert api.ids([("scope", "personal")]) == {private, counted}
    other = api.ok([("scope", "personal")], token=api.other_token)
    assert other == {
        "items": [],
        "next_cursor": None,
        "total": 0,
        "snapshot": {"number": 1, "as_of": AS_OF_JSON},
    }


# --- 不合法與沒登入 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        [("journal", "nope")],
        [("topic", "nope")],
        [("method", "nope")],
        [("method", "unclassified")],
        [("year", "2015")],
        [("year", "10000")],
        [("cursor", "not-a-cursor")],
        [("year", "abc")],
        [("scope", "all")],
        [("journal", "misq")] * 51,
        [("year", "2020")] * 51,
        # Story 3.13 審查 T6：主題、方法 51 個值，空字串，壞的布林。
        [("topic", "erp")] * 51,
        [("method", "survey_sem")] * 51,
        [("journal", "")],
        [("year", "")],
        [("scope", "")],
        [("has_abstract", "maybe")],
        # Story 3.20：`*_match` 只收 `any`、`all`。
        [("topic_match", "or")],
        [("topic_match", "")],
        [("topic_match", "ALL")],
        [("method_match", "and")],
        [("method_match", "")],
    ],
)
def test_invalid_values_are_422(api: Api, params: list[tuple[str, str]]) -> None:
    response = api.papers(params, token=api.owner_token)

    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == PROBLEM_422


@pytest.mark.parametrize(
    "params",
    [
        [("journal", "nope")],
        [("journal", "")],
        [("topic", "nope")],
        [("method", "nope")],
        [("method", "unclassified")],
        [("year", "2015")],
        [("year", "10000")],
        [("cursor", "not-a-cursor")],
        [("journal", "misq")] * 51,
        [("year", "2020")] * 51,
        [("topic", "erp")] * 51,
        [("method", "survey_sem")] * 51,
    ],
)
def test_invalid_filter_values_are_422_with_no_store(
    api: Api, params: list[tuple[str, str]]
) -> None:
    # 條件與 cursor 的驗證是這個端點自己做的（`ApiProblemError`），回應帶 `no-store`（Story 3.13
    # 審查 T6）。型別錯誤（`year=abc`、`scope=`、`has_abstract=maybe`）由框架擋，不在這裡。
    response = api.papers(params, token=api.owner_token)

    assert response.status_code == 422, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == PROBLEM_422


# --- 同一份資料狀態（Story 3.13 審查 C1）------------------------------------------------------


def test_one_response_reads_one_state_even_if_a_paper_is_excluded_midway(
    db: TestDatabase, database: Database, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 讀完這一頁的 id 之後，另一個交易把那一篇標成排除並 commit；這次回應仍是讀 id 時的那份資料
    # 狀態：items 與 total 對得上（不會 items 有它、total 卻是 0）。讀取的交易是唯讀的
    # REPEATABLE READ。下一次請求才看得到排除。
    published = make_snapshot(db, 1)
    paper_id = make_paper(database, db, 1, snapshot_id=published)
    real_list_ids = paper_list_repo.list_paper_list_ids
    real_count = paper_list_repo.count_paper_list
    seen: list[tuple[str, str]] = []

    def list_ids_then_exclude(*args: Any, **kwargs: Any) -> list[tuple[uuid.UUID, int | None]]:
        found = real_list_ids(*args, **kwargs)
        # 另一條連線（超級使用者、自動 commit），不是這次讀取的交易。
        execute(db, "UPDATE paper SET excluded_reason = 'erratum' WHERE id = %s", paper_id)
        return found

    def count_and_record(viewer: Any, connection: Any, *args: Any, **kwargs: Any) -> int:
        seen.append(
            (
                connection.execute(text("SHOW transaction_isolation")).scalar_one(),
                connection.execute(text("SHOW transaction_read_only")).scalar_one(),
            )
        )
        return real_count(viewer, connection, *args, **kwargs)

    monkeypatch.setattr(paper_list_service, "list_paper_list_ids", list_ids_then_exclude)
    monkeypatch.setattr(paper_list_service, "count_paper_list", count_and_record)

    body = api.ok()

    assert seen == [("repeatable read", "on")]
    assert [uuid.UUID(item["id"]) for item in body["items"]] == [paper_id]
    assert body["total"] == 1
    monkeypatch.undo()
    after = api.ok()
    assert (after["items"], after["total"]) == ([], 0)


def test_without_a_session_is_401(api: Api) -> None:
    response = api.papers(token=None)

    assert response.status_code == 401
    assert response.json() == {
        "type": "about:blank",
        "title": "Unauthorized",
        "status": 401,
        "code": "not_authenticated",
    }
