"""api 的找論文清單路由（Story 3.13）：用假的函式組 `PaperListRoutes`／`AuthRoutes`，不連資料庫。

- 要登入：沒登入 401 `not_authenticated`，不解析條件、不呼叫 services。
- 參數照原樣交給解析函式（多值照順序、`has_abstract` 省略是 None、`this_week` 預設 False、`scope`
  預設 `tracked`），解析結果與解回的 `cursor` 交給 services，身分是 session 的成員。
- 回應：`{items, next_cursor, total, snapshot}`，論文列照 `PaperRow`（期刊代號由 ISSN-L 對回）、
  快照只有編號與截至時間（結尾 `Z`），沒有快照是 null；帶 `no-store`。
- 不合法（解析丟 `InvalidPaperListQueryError`、壞 `cursor`）：422 `request_invalid`，ADR-0047 的
  四個欄位、`no-store`，不呼叫 services；型別錯（`year=abc`、不認得的 `scope`、
  `has_abstract=maybe`）由框架擋下，一樣是 422 `request_invalid`。
- 用正式的解析函式組路由時，超過 50 個 `journal` 是 422。
- 掛上頁面殼之後仍由 API 回應；頁面殼已掛時裝路由丟錯。
資料全是測試自己編的。
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from radar.domain.domain_config import Journal, JournalsConfig
from radar.domain.paper_list import (
    InvalidPaperListQueryError,
    PaperListCursor,
    PaperListFilters,
    PaperListPage,
    PaperListQuery,
    PaperListScope,
    encode_paper_list_cursor,
    parse_paper_list_filters,
)
from radar.domain.paper_rows import PaperRowFacts
from radar.domain.snapshot import Snapshot, SnapshotStatus
from radar.domain.taxonomy import Taxonomy, TaxonomyItem
from radar.domain.viewer import MemberViewer
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import EXPECTED_MEMBER_HEADER, SESSION_COOKIE
from radar.entrypoints.api.paper_list import PaperListRoutes, install_paper_list_routes
from radar.entrypoints.api.web import mount_web
from tests.unit.test_api_auth import GOOD_TOKEN, MEMBER, MEMBER_ID, ORIGIN, SHELL, FakeAuth

PAPER_ID = uuid.UUID("018f0000-0000-7000-8000-0000000000c3")
AS_OF = datetime(2026, 11, 16, 2, 30, 0, 123456, tzinfo=UTC)
FACTS = PaperRowFacts(
    paper_id=PAPER_ID,
    title="A study of team platforms",
    authors=("Chen, A.", "Lin, B.", "Wang, C."),
    author_count=4,
    publication_year=2025,
    journal_issn_l="0276-7783",
    doi="10.1/abc",
    has_abstract=True,
    in_scope=True,
    is_retracted=False,
)
ROW_JSON = {
    "id": str(PAPER_ID),
    "title": "A study of team platforms",
    "authors": ["Chen, A.", "Lin, B.", "Wang, C."],
    "author_count": 4,
    "publication_year": 2025,
    "journal": "misq",
    "journal_issn_l": "0276-7783",
    "doi": "10.1/abc",
    "doi_url": "https://doi.org/10.1/abc",
    "has_abstract": True,
    "in_scope": True,
    "is_retracted": False,
    "in_my_library": False,
}
SNAPSHOT = Snapshot(
    id=uuid.UUID(int=7),
    number=12,
    weekly_run_id=uuid.UUID(int=8),
    status=SnapshotStatus.PUBLISHED,
    as_of=AS_OF,
    created_at=AS_OF,
)
NEXT = encode_paper_list_cursor(PaperListCursor(year=2025, paper_id=PAPER_ID))
PARSED = PaperListFilters(journal_issn_ls=("0276-7783",), scope=PaperListScope.LAB)
PROBLEM_422 = {
    "type": "about:blank",
    "title": "Unprocessable Content",
    "status": 422,
    "code": "request_invalid",
}
JOURNALS = JournalsConfig(
    start_year=2016, journals=(Journal(key="misq", name="MIS Quarterly", issn_l="0276-7783"),)
)
TAXONOMY = Taxonomy(
    version=1,
    topics=(TaxonomyItem(key="erp", name="ERP", definition="Enterprise systems."),),
    methods=(TaxonomyItem(key="survey_sem", name="Survey", definition="Surveys and SEM."),),
)


@dataclass
class FakePaperList:
    """記下每一次解析與讀取；解析遇到 `journal=nope` 丟錯。"""

    snapshot: Snapshot | None = SNAPSHOT
    parsed: list[PaperListQuery] = field(default_factory=list)
    calls: list[tuple[MemberViewer, PaperListFilters, PaperListCursor | None]] = field(
        default_factory=list
    )

    def parse(self, query: PaperListQuery) -> PaperListFilters:
        self.parsed.append(query)
        if "nope" in query.journals:
            raise InvalidPaperListQueryError("有不認得的期刊代號")
        return PARSED

    def list_page(
        self, viewer: MemberViewer, filters: PaperListFilters, after: PaperListCursor | None
    ) -> PaperListPage:
        self.calls.append((viewer, filters, after))
        return PaperListPage(rows=(FACTS,), next_cursor=NEXT, total=45, snapshot=self.snapshot)

    def routes(self) -> PaperListRoutes:
        return PaperListRoutes(
            list_page=self.list_page,
            parse_filters=self.parse,
            journal_key=lambda issn_l: "misq" if issn_l == "0276-7783" else None,
        )


def _client(routes: PaperListRoutes, *, web_dist: Path | None = None) -> TestClient:
    app = create_app(readiness_check=lambda: True)
    install_paper_list_routes(app, routes, FakeAuth().routes())
    if web_dist is not None:
        mount_web(app, web_dist)
    return TestClient(app, base_url=ORIGIN)


def _get(
    client: TestClient,
    params: Sequence[tuple[str, str]] = (),
    *,
    token: str | None = GOOD_TOKEN,
) -> Response:
    headers = {} if token is None else {"Cookie": f"{SESSION_COOKIE}={token}"}
    client.cookies.clear()
    return client.get("/api/v1/papers", params=tuple(params), headers=headers)


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_needs_a_session(token: str | None) -> None:
    fake = FakePaperList()

    response = _get(_client(fake.routes()), [("journal", "nope")], token=token)

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "not_authenticated"
    assert (fake.parsed, fake.calls) == ([], [])


def test_defaults_are_tracked_and_no_filter() -> None:
    fake = FakePaperList()

    response = _get(_client(fake.routes()))

    assert response.status_code == 200
    assert fake.parsed == [PaperListQuery()]
    assert fake.calls == [(MEMBER.viewer, PARSED, None)]


def test_passes_every_parameter_and_returns_the_page() -> None:
    fake = FakePaperList()
    params = [
        ("journal", "misq"),
        ("journal", "isr"),
        ("year", "2021"),
        ("year", "2020"),
        ("topic", "erp"),
        ("topic", "unclassified"),
        ("method", "survey_sem"),
        ("has_abstract", "false"),
        ("this_week", "true"),
        ("scope", "lab"),
        ("cursor", NEXT),
    ]

    response = _get(_client(fake.routes()), params)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "items": [ROW_JSON],
        "next_cursor": NEXT,
        "total": 45,
        "snapshot": {"number": 12, "as_of": "2026-11-16T02:30:00.123456Z"},
    }
    assert fake.parsed == [
        PaperListQuery(
            journals=("misq", "isr"),
            years=(2021, 2020),
            topics=("erp", "unclassified"),
            methods=("survey_sem",),
            has_abstract=False,
            this_week=True,
            scope=PaperListScope.LAB,
        )
    ]
    assert fake.calls == [(MEMBER.viewer, PARSED, PaperListCursor(year=2025, paper_id=PAPER_ID))]


def test_no_snapshot_is_null() -> None:
    fake = FakePaperList(snapshot=None)

    body = _get(_client(fake.routes())).json()

    assert body["snapshot"] is None


def _assert_422(response: Response) -> None:
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == PROBLEM_422


@pytest.mark.parametrize(
    "params",
    [
        [("journal", "nope")],
        [("cursor", "not-a-cursor")],
        [("cursor", "")],
        [("cursor", NEXT + "x")],
    ],
)
def test_invalid_filters_and_cursor_are_422_without_reading(params: list[tuple[str, str]]) -> None:
    fake = FakePaperList()

    response = _get(_client(fake.routes()), params)

    _assert_422(response)
    assert response.headers["cache-control"] == "no-store"
    assert fake.calls == []


@pytest.mark.parametrize(
    "params",
    [
        [("year", "abc")],
        [("year", "2020.5")],
        [("scope", "all")],
        [("has_abstract", "maybe")],
        [("this_week", "sometimes")],
    ],
)
def test_framework_type_errors_are_422_request_invalid(params: list[tuple[str, str]]) -> None:
    fake = FakePaperList()

    response = _get(_client(fake.routes()), params)

    _assert_422(response)
    assert fake.calls == []


def test_real_parser_rejects_more_than_fifty_journals() -> None:
    fake = FakePaperList()
    routes = PaperListRoutes(
        list_page=fake.list_page,
        parse_filters=partial(parse_paper_list_filters, journals=JOURNALS, taxonomy=TAXONOMY),
        journal_key=fake.routes().journal_key,
    )
    client = _client(routes)

    fifty = _get(client, [("journal", "misq")] * 50)
    fifty_one = _get(client, [("journal", "misq")] * 51)

    assert fifty.status_code == 200
    _assert_422(fifty_one)
    assert len(fake.calls) == 1
    assert fake.calls[0][1].journal_issn_ls == ("0276-7783",)


def test_unknown_journal_is_null() -> None:
    fake = FakePaperList()
    routes = PaperListRoutes(
        list_page=fake.list_page, parse_filters=fake.parse, journal_key=lambda _issn_l: None
    )

    body = _get(_client(routes)).json()

    assert (body["items"][0]["journal"], body["items"][0]["journal_issn_l"]) == (None, "0276-7783")


def test_route_still_answers_after_the_web_shell_is_mounted(tmp_path: Path) -> None:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(SHELL, encoding="utf-8")

    response = _get(_client(FakePaperList().routes(), web_dist=dist), token=None)

    assert response.status_code == 401
    assert SHELL not in response.text


def test_installing_after_the_web_shell_is_an_error(tmp_path: Path) -> None:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(SHELL, encoding="utf-8")
    app = create_app(readiness_check=lambda: True)
    mount_web(app, dist)

    with pytest.raises(RuntimeError, match="mount_web"):
        install_paper_list_routes(app, FakePaperList().routes(), FakeAuth().routes())


# --- 這個分頁以為的成員（epic-3h2 PR 1 fix1 H3、ADR-0055）-------------------------------------
# 每一列的 `in_my_library` 是成員專屬的：帶了 `X-Radar-Expected-Member` 要和 session 的成員相同；
# 不同 409、格式不對 400，都不解析不讀；沒帶照常（上面的測試）。

OTHER_MEMBER_ID = uuid.UUID("00000000-0000-7000-8000-00000000000b")


@pytest.mark.parametrize(
    ("expected", "status", "code"),
    [
        (str(MEMBER_ID), 200, None),
        (str(OTHER_MEMBER_ID), 409, "member_changed"),
        ("not-a-member-id", 400, "expected_member_invalid"),
    ],
)
def test_list_compares_the_expected_member(expected: str, status: int, code: str | None) -> None:
    fake = FakePaperList()
    client = _client(fake.routes())
    client.cookies.clear()

    response = client.get(
        "/api/v1/papers",
        headers={"Cookie": f"{SESSION_COOKIE}={GOOD_TOKEN}", EXPECTED_MEMBER_HEADER: expected},
    )

    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    if code is None:
        assert fake.calls == [(MEMBER.viewer, PARSED, None)]
    else:
        assert response.json()["code"] == code
        assert (fake.parsed, fake.calls) == ([], [])
