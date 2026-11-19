"""api 的文獻庫路由（Story 3.16）：用假的函式組 `LibraryRoutes`／`AuthRoutes`，不連資料庫。

- 四個端點都要登入：沒登入 401 `not_authenticated`，不呼叫 services。
- 會改資料的三個（PUT、DELETE、POST）要本站的 `Origin`：缺少或不對 403 `origin_rejected`，不呼叫
  services。
- 看不到（假函式回 None）與不是 UUID 的 ID：同一個 404，狀態碼、本文、標頭（除 `X-Request-ID`）
  相同；ID 不是 UUID 時不呼叫 services。
- 200 的回應：一筆是 `{paper, added_via, added_at}`（論文列照 `PaperRow`、期刊代號由 ISSN-L
  對回、時間結尾 `Z`），清單是 `{items, next_cursor, total}`；都帶 `no-store`；讀寫的身分是
  session 的成員。
- `cursor`：沒給是第一頁；上一頁給的照原樣解回位置交給 services；格式不對 422 `request_invalid`。
- 掛上頁面殼之後仍由 API 回應；頁面殼已掛時裝路由丟錯。
資料全是測試自己編的。
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from radar.domain.library import (
    AddedVia,
    LibraryCursor,
    LibraryEntry,
    LibraryItem,
    LibraryPage,
    encode_library_cursor,
)
from radar.domain.paper_rows import PaperRowFacts
from radar.domain.viewer import MemberViewer
from radar.entrypoints.api.app import REQUEST_ID_HEADER, create_app
from radar.entrypoints.api.auth import SESSION_COOKIE
from radar.entrypoints.api.library import LibraryRoutes, install_library_routes
from radar.entrypoints.api.web import mount_web
from tests.unit.test_api_auth import GOOD_TOKEN, MEMBER, MEMBER_ID, ORIGIN, SHELL, FakeAuth

PAPER_ID = uuid.UUID("018f0000-0000-7000-8000-0000000000c3")
ITEM_ID = uuid.UUID("018f0000-0000-7000-8000-0000000000d4")
ADDED_AT = datetime(2026, 11, 18, 9, 30, 15, 123456, tzinfo=UTC)
FACTS = PaperRowFacts(
    paper_id=PAPER_ID,
    title="A study of team platforms",
    authors=("Chen, A.", "Lin, B.", "Wang, C."),
    author_count=4,
    publication_year=2025,
    journal_issn_l="0276-7783",
    doi="10.1/abc",
    has_abstract=False,
    in_scope=True,
    is_retracted=False,
)
ENTRY = LibraryEntry(
    item=LibraryItem(
        id=ITEM_ID,
        owner_id=MEMBER_ID,
        paper_id=PAPER_ID,
        added_via=AddedVia.UPLOAD,
        added_at=ADDED_AT,
    ),
    paper=FACTS,
)
ENTRY_JSON = {
    "paper": {
        "id": str(PAPER_ID),
        "title": "A study of team platforms",
        "authors": ["Chen, A.", "Lin, B.", "Wang, C."],
        "author_count": 4,
        "publication_year": 2025,
        "journal": "misq",
        "journal_issn_l": "0276-7783",
        "doi": "10.1/abc",
        "doi_url": "https://doi.org/10.1/abc",
        "has_abstract": False,
        "in_scope": True,
        "is_retracted": False,
    },
    "added_via": "upload",
    "added_at": "2026-11-18T09:30:15.123456Z",
}
NEXT = encode_library_cursor(LibraryCursor(added_at=ADDED_AT, item_id=ITEM_ID))
CHANGES = (
    ("PUT", "/api/v1/library/{id}", "add"),
    ("DELETE", "/api/v1/library/{id}", "remove"),
    ("POST", "/api/v1/library/{id}/restore", "restore"),
)
ENDPOINTS = (("GET", "/api/v1/library"), *((method, path) for method, path, _ in CHANGES))


@dataclass
class FakeLibrary:
    """記下每一次呼叫（哪一個、身分、參數）；只有 `PAPER_ID` 在文獻庫。"""

    calls: list[tuple[str, MemberViewer, object]] = field(default_factory=list)

    def list_page(self, viewer: MemberViewer, after: LibraryCursor | None) -> LibraryPage:
        self.calls.append(("list", viewer, after))
        return LibraryPage(entries=(ENTRY,), next_cursor=NEXT, total=51)

    def _change(self, name: str, viewer: MemberViewer, paper_id: uuid.UUID) -> LibraryEntry | None:
        self.calls.append((name, viewer, paper_id))
        return ENTRY if paper_id == PAPER_ID else None

    def routes(self) -> LibraryRoutes:
        return LibraryRoutes(
            list_page=self.list_page,
            add=lambda viewer, paper_id: self._change("add", viewer, paper_id),
            remove=lambda viewer, paper_id: self._change("remove", viewer, paper_id),
            restore=lambda viewer, paper_id: self._change("restore", viewer, paper_id),
            journal_key=lambda issn_l: "misq" if issn_l == "0276-7783" else None,
        )


def _client(library: FakeLibrary, *, web_dist: Path | None = None) -> TestClient:
    app = create_app(readiness_check=lambda: True)
    install_library_routes(app, library.routes(), FakeAuth().routes())
    if web_dist is not None:
        mount_web(app, web_dist)
    return TestClient(app, base_url=ORIGIN)


def _send(
    client: TestClient,
    method: str,
    path: str,
    *,
    token: str | None = GOOD_TOKEN,
    origin: str | None = ORIGIN,
    params: dict[str, str] | None = None,
) -> Response:
    headers = {} if token is None else {"Cookie": f"{SESSION_COOKIE}={token}"}
    if origin is not None:
        headers["Origin"] = origin
    client.cookies.clear()
    return client.request(method, path, headers=headers, params=params)


def _comparable(response: Response) -> tuple[int, bytes, dict[str, str]]:
    headers = {k: v for k, v in response.headers.items() if k.lower() != REQUEST_ID_HEADER.lower()}
    return response.status_code, response.content, headers


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_every_endpoint_needs_a_session(method: str, path: str, token: str | None) -> None:
    library = FakeLibrary()

    response = _send(_client(library), method, path.format(id=PAPER_ID), token=token)

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "not_authenticated"
    assert library.calls == []


@pytest.mark.parametrize(("method", "path", "_name"), CHANGES)
@pytest.mark.parametrize("origin", [None, "https://evil.example"])
def test_changes_need_this_site_as_origin(
    method: str, path: str, _name: str, origin: str | None
) -> None:
    library = FakeLibrary()

    response = _send(_client(library), method, path.format(id=PAPER_ID), origin=origin)

    assert response.status_code == 403
    assert response.json()["code"] == "origin_rejected"
    assert library.calls == []


def test_list_reads_the_session_members_library() -> None:
    library = FakeLibrary()

    response = _send(_client(library), "GET", "/api/v1/library", origin=None)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"items": [ENTRY_JSON], "next_cursor": NEXT, "total": 51}
    assert library.calls == [("list", MEMBER.viewer, None)]


def test_list_passes_the_decoded_cursor() -> None:
    library = FakeLibrary()

    response = _send(_client(library), "GET", "/api/v1/library", params={"cursor": NEXT})

    assert response.status_code == 200
    assert library.calls == [
        ("list", MEMBER.viewer, LibraryCursor(added_at=ADDED_AT, item_id=ITEM_ID))
    ]


@pytest.mark.parametrize("cursor", ["", "not-a-cursor", NEXT + "x", "A" * 300])
def test_malformed_cursor_is_422(cursor: str) -> None:
    library = FakeLibrary()

    response = _send(_client(library), "GET", "/api/v1/library", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "about:blank",
        "title": "Unprocessable Content",
        "status": 422,
        "code": "request_invalid",
    }
    assert library.calls == []


@pytest.mark.parametrize(("method", "path", "name"), CHANGES)
def test_changes_act_as_the_session_member_and_return_the_item(
    method: str, path: str, name: str
) -> None:
    library = FakeLibrary()

    response = _send(_client(library), method, path.format(id=PAPER_ID))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == ENTRY_JSON
    assert library.calls == [(name, MEMBER.viewer, PAPER_ID)]


@pytest.mark.parametrize(("method", "path", "name"), CHANGES)
def test_hidden_and_malformed_ids_get_the_same_404(method: str, path: str, name: str) -> None:
    library = FakeLibrary()
    client = _client(library)
    hidden_id = uuid.UUID(int=0xDEAD)

    hidden = _send(client, method, path.format(id=hidden_id))
    malformed = _send(client, method, path.format(id="not-a-uuid"))

    assert hidden.status_code == 404
    assert hidden.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "code": "not_found",
    }
    assert hidden.headers["cache-control"] == "no-store"
    assert _comparable(hidden) == _comparable(malformed)
    # ID 不是 UUID 時不呼叫 services。
    assert library.calls == [(name, MEMBER.viewer, hidden_id)]


def test_unknown_journal_is_null() -> None:
    library = FakeLibrary()
    routes = library.routes()
    app = create_app(readiness_check=lambda: True)
    install_library_routes(
        app,
        LibraryRoutes(
            list_page=routes.list_page,
            add=routes.add,
            remove=routes.remove,
            restore=routes.restore,
            journal_key=lambda _issn_l: None,
        ),
        FakeAuth().routes(),
    )

    body = _send(TestClient(app, base_url=ORIGIN), "PUT", f"/api/v1/library/{PAPER_ID}").json()

    assert (body["paper"]["journal"], body["paper"]["journal_issn_l"]) == (None, "0276-7783")


def test_routes_still_answer_after_the_web_shell_is_mounted(tmp_path: Path) -> None:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(SHELL, encoding="utf-8")
    client = _client(FakeLibrary(), web_dist=dist)

    for method, path in ENDPOINTS:
        response = _send(client, method, path.format(id=PAPER_ID), token=None)
        assert response.status_code == 401, (method, path)
        assert SHELL not in response.text


def test_installing_after_the_web_shell_is_an_error(tmp_path: Path) -> None:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(SHELL, encoding="utf-8")
    app = create_app(readiness_check=lambda: True)
    mount_web(app, dist)

    with pytest.raises(RuntimeError, match="mount_web"):
        install_library_routes(app, FakeLibrary().routes(), FakeAuth().routes())
