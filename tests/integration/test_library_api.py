"""個人文獻庫的 API 對真的 PostgreSQL（Story 3.16、ADR-0002、AD-4、AD-26）：spec 的 I/O 表每一列。

- 加入：看得到的論文 → 200（`added_via = manual`）＋一筆稽核；再加一次（任何 `added_via`）回原本
  那一筆、不寫稽核。B 加入 A 也有的論文：B 自己的一筆，A 那一筆不變。
- 加看不到的（別人的私人論文）、不存在、壞 ID、被排除的非研究文章：完全相同的 404，沒有寫入。
- 移出：200 回移出前那一筆（`in_my_library` 是假）、`deleted_record` 一筆（整列的 jsonb）、稽核；
  清單不再有它。移出不在的（別人的、沒加過的）404，別人的那一筆不動。
- 復原：200，`id`／`added_via`／`added_at` 同原本，封存刪掉、稽核；別人的封存、沒有封存 404，不寫
  稽核；那篇已經在文獻庫時回現在那一筆、不動任何封存（重送復原不會吃掉另一次移出的封存）。
- 清單：51 篇 → 第一頁 50＋`next_cursor`＋`total` 51，第二頁 1 篇、`next_cursor` null；照
  `added_at DESC, id DESC`（同一時間的照 id，不是照 paper_id）；別人讀是空清單；壞 cursor（含轉成
  UTC 會超出年份範圍的）422。
- 同時：另一條連線拿著（成員, 論文）的鎖時，加入、移出、復原都排隊等它（看 `pg_locks`），放掉後
  才做完。
- 論文頁：A 文獻庫裡的私人論文 A 200；B 和不存在的完全一樣的 404。
- 這篇在不在我的文獻庫（Story 3.17、ADR-0053）：A 加的，A 在找論文清單與論文頁看到真、B 看到假
  （反過來也一樣）；跟著加入、移出、復原變；文獻庫清單每一筆都是真；系統與訪客身分讀一律假（不洩漏
  任何人的文獻庫）；管理者也只看自己的；3 篇與一整頁的 SQL 句數相同（整頁一句，不是每篇一句）。

兩位成員經開發環境的假登入取得真的 session（路由用正式的 `install_api_routes`）。資料全是測試自己
編的（`*@example.com`、假標題）。
"""

import base64
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import event

from radar.adapters.fake.login import pick_member_identity
from radar.domain.auth_settings import AuthSettings, Environment
from radar.domain.library import AddedVia, library_item_lock_key
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer
from radar.entrypoints.api.app import REQUEST_ID_HEADER, create_app
from radar.entrypoints.api.auth import SESSION_COOKIE, build_auth_routes
from radar.entrypoints.api.main import install_api_routes
from radar.repo.db import Database
from radar.repo.library import create_library_item
from radar.repo.paper_rows import find_paper_rows
from radar.repo.papers import find_paper_page_facts
from radar.services.members import allow_member
from tests.backup_support import count_in_snapshot, publish_snapshot
from tests.database import TestDatabase, migrate
from tests.llm_support import (
    create_hidden_paper,
    create_paper,
    execute,
    library_routes,
    paper_list_routes,
    paper_routes,
)
from tests.permissions.framework import CANARY

DEV_ORIGIN = "http://localhost:8000"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
OWNER_EMAIL = "library.owner@example.com"
OTHER_EMAIL = "library.other@example.com"
NOT_FOUND = {"type": "about:blank", "title": "Not Found", "status": 404, "code": "not_found"}


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

    def send(
        self, method: str, path: str, token: str, *, params: dict[str, str] | None = None
    ) -> Response:
        self.client.cookies.clear()
        headers = {"Cookie": f"{SESSION_COOKIE}={token}", "Origin": DEV_ORIGIN}
        return self.client.request(method, path, headers=headers, params=params)

    def add(self, paper_id: object, token: str) -> Response:
        return self.send("PUT", f"/api/v1/library/{paper_id}", token)

    def remove(self, paper_id: object, token: str) -> Response:
        return self.send("DELETE", f"/api/v1/library/{paper_id}", token)

    def restore(self, paper_id: object, token: str) -> Response:
        return self.send("POST", f"/api/v1/library/{paper_id}/restore", token)

    def list(self, token: str, cursor: str | None = None) -> Response:
        params = None if cursor is None else {"cursor": cursor}
        return self.send("GET", "/api/v1/library", token, params=params)


@pytest.fixture
def api(database: Database) -> Api:
    return Api(database)


def _comparable(response: Response) -> tuple[int, bytes, dict[str, str]]:
    headers = {k: v for k, v in response.headers.items() if k.lower() != REQUEST_ID_HEADER.lower()}
    return response.status_code, response.content, headers


def _items(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db,
        "SELECT id, owner_id, paper_id, added_via, added_at FROM library_item ORDER BY id",
    )


def _audits(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db,
        "SELECT actor_member_id, actor_role, action, target_type, target_id, detail"
        " FROM audit_event WHERE target_type = 'library_item' ORDER BY created_at, id",
    )


def _private_paper_of(database: Database, owner_id: uuid.UUID) -> uuid.UUID:
    """A 的私人論文（成員看不到），以 `upload` 放進 A 的文獻庫；標題有金絲雀。"""
    paper_id = create_hidden_paper(database, title=f"Private paper {CANARY}")
    with database.transaction() as connection:
        item = create_library_item(
            connection,
            SystemViewer(),
            owner_id=owner_id,
            paper_id=paper_id,
            added_via=AddedVia.UPLOAD,
        )
    assert item is not None
    return paper_id


def _z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _without_in_my_library(body: dict[str, Any]) -> dict[str, Any]:
    """回應拿掉論文列的 `in_my_library` 之後的其餘全部（其他欄位照樣逐一比）。"""
    paper = {key: value for key, value in body["paper"].items() if key != "in_my_library"}
    return {**body, "paper": paper}


# --- 加入 -------------------------------------------------------------------------------------


def test_add_a_visible_paper(db: TestDatabase, database: Database, api: Api) -> None:
    paper_id = create_paper(database)

    response = api.add(paper_id, api.owner_token)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["added_via"] == "manual"
    assert body["paper"] == {
        "id": str(paper_id),
        "title": "A study of information systems",
        "authors": [],
        "author_count": 0,
        "publication_year": 2025,
        "journal": "misq",
        "journal_issn_l": "0276-7783",
        "doi": "10.1/abc",
        "doi_url": "https://doi.org/10.1/abc",
        "has_abstract": True,
        "in_scope": True,
        "is_retracted": False,
        "in_my_library": True,
    }
    ((item_id, owner_id, stored_paper, added_via, added_at),) = _items(db)
    assert (owner_id, stored_paper, added_via) == (api.owner_id, paper_id, "manual")
    assert body["added_at"] == _z(added_at)
    ((actor, role, action, target_type, target_id, detail),) = _audits(db)
    assert (actor, role, action, target_type, target_id) == (
        api.owner_id,
        "member",
        "add_library_item",
        "library_item",
        item_id,
    )
    assert detail == {"paper_id": str(paper_id), "added_via": "manual"}


def test_adding_again_returns_the_original_item(
    db: TestDatabase, database: Database, api: Api
) -> None:
    # 原本是上傳加進來的（私人論文）：再加一次不改 added_via、added_at，不寫稽核。
    private_id = _private_paper_of(database, api.owner_id)
    visible_id = create_paper(database, doi="10.1/second", openalex_id="W2")
    first = api.add(visible_id, api.owner_token).json()
    before = _items(db)

    again_private = api.add(private_id, api.owner_token)
    again_visible = api.add(visible_id, api.owner_token)

    assert again_private.status_code == again_visible.status_code == 200
    assert again_private.json()["added_via"] == "upload"
    assert again_private.json()["paper"]["title"] == f"Private paper {CANARY}"
    assert again_visible.json() == first
    assert _items(db) == before
    assert [row[2] for row in _audits(db)] == ["add_library_item"]


def test_other_member_adds_a_paper_that_is_also_in_my_library(
    db: TestDatabase, database: Database, api: Api
) -> None:
    paper_id = create_paper(database)
    api.add(paper_id, api.owner_token)
    (owner_row,) = _items(db)

    response = api.add(paper_id, api.other_token)

    assert response.status_code == 200, response.text
    rows = {row[1]: row for row in _items(db)}
    assert set(rows) == {api.owner_id, api.other_id}
    assert rows[api.owner_id] == owner_row
    other_id, _owner, _paper, other_via, other_at = rows[api.other_id]
    assert other_id != owner_row[0]
    assert (response.json()["added_via"], other_via) == ("manual", "manual")
    assert response.json()["added_at"] == _z(other_at)
    assert [(row[0], row[4]) for row in _audits(db)] == [
        (api.owner_id, owner_row[0]),
        (api.other_id, other_id),
    ]


def test_hidden_missing_malformed_and_excluded_papers_get_the_same_404(
    db: TestDatabase, database: Database, api: Api
) -> None:
    private_id = _private_paper_of(database, api.owner_id)
    excluded_id = create_paper(database, doi="10.1/editorial", openalex_id="W3")
    execute(db, "UPDATE paper SET excluded_reason = 'editorial' WHERE id = %s", excluded_id)
    before = _items(db)

    hidden = api.add(private_id, api.other_token)
    missing = api.add(uuid.UUID(int=0xDEAD), api.other_token)
    malformed = api.add("not-a-uuid", api.other_token)
    excluded = api.add(excluded_id, api.other_token)

    assert hidden.status_code == 404
    assert hidden.json() == NOT_FOUND
    assert hidden.headers["cache-control"] == "no-store"
    assert _comparable(hidden) == _comparable(missing) == _comparable(malformed)
    assert _comparable(excluded) == _comparable(missing)
    assert CANARY not in hidden.text
    assert _items(db) == before
    assert _audits(db) == []


# --- 移出 -------------------------------------------------------------------------------------


def test_remove_copies_to_deleted_record_then_deletes(
    db: TestDatabase, database: Database, api: Api
) -> None:
    paper_id = create_paper(database)
    added = api.add(paper_id, api.owner_token).json()
    ((item_id, *_rest),) = _items(db)

    response = api.remove(paper_id, api.owner_token)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    # 移出的回應＝加入時回的那一筆，只有 `in_my_library` 是假（ADR-0053 更新 2026-11-21）。
    assert added["paper"]["in_my_library"] is True
    assert response.json()["paper"]["in_my_library"] is False
    assert _without_in_my_library(response.json()) == _without_in_my_library(added)
    assert _items(db) == []
    ((record_id, source, source_id, content, owner_id, deleted_by),) = execute(
        db,
        "SELECT id, source_table, source_id, content, owner_id, deleted_by_member_id"
        " FROM deleted_record",
    )
    assert (source, source_id, owner_id, deleted_by) == (
        "library_item",
        item_id,
        api.owner_id,
        api.owner_id,
    )
    assert set(content) == {"id", "owner_id", "paper_id", "added_via", "added_at"}
    assert (content["id"], content["paper_id"], content["added_via"]) == (
        str(item_id),
        str(paper_id),
        "manual",
    )
    actions = [(row[0], row[1], row[2], row[4], row[5]) for row in _audits(db)]
    assert actions[-1] == (
        api.owner_id,
        "member",
        "remove_library_item",
        item_id,
        {"paper_id": str(paper_id), "added_via": "manual", "deleted_record_id": str(record_id)},
    )
    listed = api.list(api.owner_token).json()
    assert listed == {"items": [], "next_cursor": None, "total": 0}


def test_removing_what_is_not_in_my_library_is_404(
    db: TestDatabase, database: Database, api: Api
) -> None:
    private_id = _private_paper_of(database, api.owner_id)
    visible_id = create_paper(database)
    api.add(visible_id, api.owner_token)
    before = _items(db)

    others = api.remove(private_id, api.other_token)
    shared = api.remove(visible_id, api.other_token)
    never = api.remove(create_paper(database, doi="10.1/never", openalex_id="W4"), api.owner_token)
    missing = api.remove(uuid.UUID(int=0xDEAD), api.other_token)
    malformed = api.remove("not-a-uuid", api.other_token)

    assert others.status_code == 404
    assert others.json() == NOT_FOUND
    for response in (shared, never, malformed):
        assert _comparable(response) == _comparable(missing)
    assert _comparable(others) == _comparable(missing)
    assert _items(db) == before
    assert execute(db, "SELECT count(*) FROM deleted_record") == [(0,)]
    assert [row[2] for row in _audits(db)] == ["add_library_item"]


# --- 復原 -------------------------------------------------------------------------------------


def test_restore_brings_back_the_same_item(db: TestDatabase, database: Database, api: Api) -> None:
    private_id = _private_paper_of(database, api.owner_id)
    original = _items(db)
    listed_before = api.list(api.owner_token).json()
    removed = api.remove(private_id, api.owner_token)
    # 移出之後私人論文就看不到了（論文頁和不存在一樣）。
    assert api.send("GET", f"/api/v1/papers/{private_id}", api.owner_token).status_code == 404
    ((record_id,),) = execute(db, "SELECT id FROM deleted_record")

    response = api.restore(private_id, api.owner_token)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    # 復原的回應＝移出的回應，只有 `in_my_library` 是真（ADR-0053 更新 2026-11-21）。
    assert removed.json()["paper"]["in_my_library"] is False
    assert response.json()["paper"]["in_my_library"] is True
    assert _without_in_my_library(response.json()) == _without_in_my_library(removed.json())
    assert response.json()["added_via"] == "upload"
    assert _items(db) == original
    assert execute(db, "SELECT count(*) FROM deleted_record") == [(0,)]
    assert api.list(api.owner_token).json() == listed_before
    ((actor, role, action, _type, target_id, detail),) = [
        row for row in _audits(db) if row[2] == "restore_library_item"
    ]
    assert (actor, role, target_id) == (api.owner_id, "member", original[0][0])
    assert detail == {
        "paper_id": str(private_id),
        "added_via": "upload",
        "deleted_record_id": str(record_id),
        "restored": True,
    }
    assert action == "restore_library_item"


def test_restore_needs_my_own_record(db: TestDatabase, database: Database, api: Api) -> None:
    private_id = _private_paper_of(database, api.owner_id)
    api.remove(private_id, api.owner_token)
    visible_id = create_paper(database)

    others = api.restore(private_id, api.other_token)
    none = api.restore(visible_id, api.owner_token)
    missing = api.restore(uuid.UUID(int=0xDEAD), api.other_token)
    malformed = api.restore("not-a-uuid", api.other_token)

    assert others.status_code == 404
    assert others.json() == NOT_FOUND
    for response in (others, none, malformed):
        assert _comparable(response) == _comparable(missing)
    assert CANARY not in others.text
    assert _items(db) == []
    assert execute(db, "SELECT count(*) FROM deleted_record") == [(1,)]
    assert [row[2] for row in _audits(db)] == ["remove_library_item"]


def test_restore_when_already_back_returns_the_current_item(
    db: TestDatabase, database: Database, api: Api
) -> None:
    paper_id = create_paper(database)
    api.add(paper_id, api.owner_token)
    api.remove(paper_id, api.owner_token)
    records = execute(db, "SELECT id FROM deleted_record")
    current = api.add(paper_id, api.owner_token).json()
    current_rows = _items(db)

    first = api.restore(paper_id, api.owner_token)
    again = api.restore(paper_id, api.owner_token)

    # 冪等：重送幾次都是 200＋現在那一筆，封存一筆都不動。
    assert first.status_code == again.status_code == 200, first.text
    assert first.json() == again.json() == current
    assert _items(db) == current_rows
    assert execute(db, "SELECT id FROM deleted_record") == records
    restores = [row for row in _audits(db) if row[2] == "restore_library_item"]
    expected = (
        api.owner_id,
        "member",
        "restore_library_item",
        "library_item",
        current_rows[0][0],
        {"paper_id": str(paper_id), "added_via": "manual", "restored": False},
    )
    assert restores == [expected, expected]


def test_repeated_restore_keeps_the_older_removal(
    db: TestDatabase, database: Database, api: Api
) -> None:
    # PUT → DELETE → PUT → DELETE → restore → restore：第二次復原時那篇已經回來了，不能吃掉第一次
    # 移出留下的封存。
    paper_id = create_paper(database)
    api.add(paper_id, api.owner_token)
    (first_item,) = _items(db)
    api.remove(paper_id, api.owner_token)
    ((first_record,),) = execute(db, "SELECT id FROM deleted_record")
    api.add(paper_id, api.owner_token)
    (second_item,) = _items(db)
    api.remove(paper_id, api.owner_token)

    restored = api.restore(paper_id, api.owner_token)
    again = api.restore(paper_id, api.owner_token)

    assert restored.status_code == again.status_code == 200
    assert again.json() == restored.json()
    assert _items(db) == [second_item]
    assert execute(db, "SELECT id, source_id FROM deleted_record") == [
        (first_record, first_item[0])
    ]
    assert [row[5]["restored"] for row in _audits(db) if row[2] == "restore_library_item"] == [
        True,
        False,
    ]


def test_restore_uses_the_latest_record(db: TestDatabase, database: Database, api: Api) -> None:
    paper_id = create_paper(database)
    api.add(paper_id, api.owner_token)
    api.remove(paper_id, api.owner_token)
    api.add(paper_id, api.owner_token)
    latest = _items(db)
    api.remove(paper_id, api.owner_token)

    api.restore(paper_id, api.owner_token)

    assert _items(db) == latest
    assert execute(db, "SELECT count(*) FROM deleted_record") == [(1,)]


# --- 清單 -------------------------------------------------------------------------------------


def test_list_pages_by_added_at_then_id(db: TestDatabase, database: Database, api: Api) -> None:
    paper_ids = [
        create_paper(database, doi=f"10.1/p{n}", openalex_id=f"W{100 + n}", title=f"Paper {n}")
        for n in range(51)
    ]
    base = datetime(2026, 11, 1, tzinfo=UTC)
    rows = []
    for n, paper_id in enumerate(paper_ids):
        # 每三篇同一個時間。文獻庫的 id 和論文建立的順序相反：同一時間「照 id」和「照 paper_id」
        # 排出來的順序不同。
        item_id = uuid.UUID(int=1000 - n)
        added_at = base + timedelta(minutes=n // 3)
        rows.append((added_at, item_id, paper_id))
        execute(
            db,
            "INSERT INTO library_item (id, owner_id, paper_id, added_via, added_at)"
            " VALUES (%s, %s, %s, 'manual', %s)",
            item_id,
            api.owner_id,
            paper_id,
            added_at,
        )
    expected = [str(row[2]) for row in sorted(rows, key=lambda row: (row[0], row[1]), reverse=True)]
    by_paper_id = [
        str(row[2]) for row in sorted(rows, key=lambda row: (row[0], row[2]), reverse=True)
    ]
    assert expected != by_paper_id

    first = api.list(api.owner_token)
    body = first.json()
    second = api.list(api.owner_token, body["next_cursor"]).json()

    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert (len(body["items"]), body["total"]) == (50, 51)
    assert body["next_cursor"] is not None
    assert (len(second["items"]), second["total"], second["next_cursor"]) == (1, 51, None)
    listed = [item["paper"]["id"] for item in body["items"] + second["items"]]
    assert listed == expected
    assert len(set(listed)) == 51


def test_other_member_reads_an_empty_list(database: Database, api: Api) -> None:
    _private_paper_of(database, api.owner_id)
    api.add(create_paper(database), api.owner_token)

    response = api.list(api.other_token)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None, "total": 0}
    assert CANARY not in response.text
    assert api.list(api.owner_token).json()["total"] == 2


def test_malformed_cursor_is_422(api: Api) -> None:
    response = api.list(api.owner_token, "not-a-cursor")

    assert response.status_code == 422
    assert response.json()["code"] == "request_invalid"


@pytest.mark.parametrize(
    "added_at",
    ["0001-01-01T00:00:00.000000+14:00", "9999-12-31T23:59:59.999999-14:00"],
)
def test_cursor_out_of_range_in_utc_is_422(api: Api, added_at: str) -> None:
    # 時間與 ID 都解得出來，轉成 UTC 才超出年份範圍：ADR-0047 的 422，不是 500。
    plain = f"{added_at},{uuid.UUID(int=0)}"
    cursor = base64.urlsafe_b64encode(plain.encode()).decode().rstrip("=")

    response = api.list(api.owner_token, cursor)

    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "about:blank",
        "title": "Unprocessable Content",
        "status": 422,
        "code": "request_invalid",
    }


# --- 論文頁 -----------------------------------------------------------------------------------


def test_paper_page_of_a_private_paper_in_my_library(database: Database, api: Api) -> None:
    private_id = _private_paper_of(database, api.owner_id)

    mine = api.send("GET", f"/api/v1/papers/{private_id}", api.owner_token)
    others = api.send("GET", f"/api/v1/papers/{private_id}", api.other_token)
    missing = api.send("GET", f"/api/v1/papers/{uuid.UUID(int=0xDEAD)}", api.other_token)

    assert mine.status_code == 200, mine.text
    assert mine.json()["paper"]["title"] == f"Private paper {CANARY}"
    assert others.json() == NOT_FOUND
    assert _comparable(others) == _comparable(missing)
    assert CANARY not in others.text


# --- 論文列走真的 SQL ------------------------------------------------------------------------


def test_paper_rows_through_the_real_query(db: TestDatabase, database: Database, api: Api) -> None:
    # 五位作者：只列前三位（照原本順序），author_count 是 5。後來才記上的第二個 DOI（字母順序
    # 在前）：仍用最早記上的那一個。
    many_id = create_paper(database, doi="10.1/zzz-first", openalex_id="W11")
    execute(
        db,
        "UPDATE paper SET authors = ARRAY['Eve, E.', 'Bob, B.', 'Cat, C.', 'Ann, A.', 'Dan, D.']"
        " WHERE id = %s",
        many_id,
    )
    execute(
        db,
        "INSERT INTO external_id (paper_id, scheme, value_norm, created_at)"
        " VALUES (%s, 'doi', '10.1/aaa-later', now() + interval '1 hour')",
        many_id,
    )
    # 沒有 DOI、只有不是目前的摘要；不是追蹤的期刊。
    bare_id = create_paper(database, doi=None, openalex_id="W12", abstract=None)
    execute(
        db,
        "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
        " VALUES (%s, 'semantic_scholar', 'An older abstract.', false)",
        bare_id,
    )
    execute(db, "UPDATE paper SET journal_issn_l = '9999-9994' WHERE id = %s", bare_id)

    many = api.add(many_id, api.owner_token).json()["paper"]
    bare = api.add(bare_id, api.owner_token).json()["paper"]

    assert (many["authors"], many["author_count"]) == (["Eve, E.", "Bob, B.", "Cat, C."], 5)
    assert (many["doi"], many["doi_url"]) == ("10.1/zzz-first", "https://doi.org/10.1/zzz-first")
    assert many["has_abstract"] is True
    assert (bare["doi"], bare["doi_url"], bare["has_abstract"]) == (None, None, False)
    assert (bare["journal"], bare["journal_issn_l"]) == (None, "9999-9994")
    assert [item["paper"] for item in api.list(api.owner_token).json()["items"]] == [bare, many]


def test_item_whose_paper_was_excluded_later_is_still_listed_and_removable(
    db: TestDatabase, database: Database, api: Api
) -> None:
    paper_id = create_paper(database)
    added = api.add(paper_id, api.owner_token).json()
    execute(db, "UPDATE paper SET excluded_reason = 'editorial' WHERE id = %s", paper_id)

    listed = api.list(api.owner_token).json()
    removed = api.remove(paper_id, api.owner_token)

    assert (listed["items"], listed["total"]) == ([added], 1)
    assert removed.status_code == 200, removed.text
    # 移出的回應＝加入的回應，只有 `in_my_library` 是假（ADR-0053 更新 2026-11-21）。
    assert added["paper"]["in_my_library"] is True
    assert removed.json()["paper"]["in_my_library"] is False
    assert _without_in_my_library(removed.json()) == _without_in_my_library(added)
    assert _items(db) == []


# --- 同時被另一個請求改掉：整個交易退回 ----------------------------------------------------------


def test_remove_rolls_back_when_the_item_was_removed_meanwhile(
    db: TestDatabase, database: Database, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper_id = create_paper(database)
    api.add(paper_id, api.owner_token)
    before = _items(db)
    monkeypatch.setattr("radar.services.library.delete_library_item", lambda *_args: None)

    response = api.remove(paper_id, api.owner_token)

    assert response.json() == NOT_FOUND
    assert _items(db) == before
    assert execute(db, "SELECT count(*) FROM deleted_record") == [(0,)]
    assert [row[2] for row in _audits(db)] == ["add_library_item"]


def test_restore_rolls_back_when_the_record_was_used_meanwhile(
    db: TestDatabase, database: Database, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper_id = create_paper(database)
    api.add(paper_id, api.owner_token)
    api.remove(paper_id, api.owner_token)
    records = execute(db, "SELECT id FROM deleted_record")
    monkeypatch.setattr("radar.services.library.delete_deleted_record", lambda *_args: False)

    response = api.restore(paper_id, api.owner_token)

    assert response.json() == NOT_FOUND
    assert _items(db) == []
    assert execute(db, "SELECT id FROM deleted_record") == records
    assert "restore_library_item" not in [row[2] for row in _audits(db)]


# --- 同一位成員同一篇的寫入排隊（兩條真的資料庫連線） ---------------------------------------


def _waiting_for(db: TestDatabase, key: int) -> bool:
    """這個測試資料庫裡有沒有連線正在等 `key` 這把 advisory lock（`pg_locks` 沒拿到的列）。"""
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    ((waiting,),) = execute(
        db,
        "SELECT count(*) FROM pg_locks"
        " WHERE locktype = 'advisory' AND NOT granted AND objsubid = 1"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        " AND classid = %s::bigint::oid AND objid = %s::bigint::oid",
        unsigned >> 32,
        unsigned & 0xFFFF_FFFF,
    )
    return bool(waiting)


@pytest.mark.parametrize("action", ["add", "remove", "restore"])
def test_writes_for_the_same_paper_wait_for_the_lock(
    db: TestDatabase, database: Database, api: Api, action: str
) -> None:
    paper_id = create_paper(database)
    if action in ("remove", "restore"):
        api.add(paper_id, api.owner_token)
    if action == "restore":
        api.remove(paper_id, api.owner_token)
    before = _items(db)
    send: Callable[[object, str], Response] = getattr(api, action)
    key = library_item_lock_key(api.owner_id, paper_id)
    results: list[Response] = []
    holder = Database(db.app_url, connect_timeout=5)
    with holder.transaction() as connection:
        Database.transaction_lock(connection, key)
        worker = threading.Thread(target=lambda: results.append(send(paper_id, api.owner_token)))
        worker.start()
        # 等到那個請求真的卡在這把鎖上（看 pg_locks，不猜時間）；它的 statement_timeout 是 5 秒。
        deadline = time.monotonic() + 4
        while not _waiting_for(db, key):
            assert time.monotonic() < deadline, "請求沒有在等這把鎖"
            time.sleep(0.02)
        assert worker.is_alive()
        assert results == []
        assert _items(db) == before
    worker.join(timeout=10)

    assert not worker.is_alive()
    (response,) = results
    assert response.status_code == 200, response.text
    assert not _waiting_for(db, key)
    in_library = [row for row in _items(db) if row[2] == paper_id]
    assert len(in_library) == (0 if action == "remove" else 1)


# --- 這篇在不在我的文獻庫（Story 3.17、ADR-0053）--------------------------------------------------


class CountingDatabase(Database):
    """記下執行過的每一句 SQL（證明「在不在我的文獻庫」不是每篇一句）。"""

    def __init__(self, url: str) -> None:
        super().__init__(url, connect_timeout=5, pool_size=2)
        self.statements: list[str] = []
        event.listen(self._engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        self.statements.append(statement)


def _flags(rows: list[dict[str, Any]]) -> dict[str, bool]:
    return {row["id"]: row["in_my_library"] for row in rows}


def test_in_my_library_is_only_the_viewers_own(database: Database, api: Api) -> None:
    # 甲加入第一篇；乙加入第二篇。清單與論文頁都只看讀的人自己的文獻庫。
    mine = create_paper(database, doi="10.1/mine", openalex_id="W31")
    theirs = create_paper(database, doi="10.1/theirs", openalex_id="W32")
    nobody = create_paper(database, doi="10.1/nobody", openalex_id="W33")
    assert api.add(mine, api.owner_token).status_code == 200
    assert api.add(theirs, api.other_token).status_code == 200
    lab = {"scope": "lab"}

    owner_list = api.send("GET", "/api/v1/papers", api.owner_token, params=lab)
    other_list = api.send("GET", "/api/v1/papers", api.other_token, params=lab)

    assert owner_list.status_code == other_list.status_code == 200
    assert _flags(owner_list.json()["items"]) == {
        str(mine): True,
        str(theirs): False,
        str(nobody): False,
    }
    assert _flags(other_list.json()["items"]) == {
        str(mine): False,
        str(theirs): True,
        str(nobody): False,
    }
    for paper_id, owner_sees, other_sees in (
        (mine, True, False),
        (theirs, False, True),
        (nobody, False, False),
    ):
        owner_page = api.send("GET", f"/api/v1/papers/{paper_id}", api.owner_token)
        other_page = api.send("GET", f"/api/v1/papers/{paper_id}", api.other_token)
        assert owner_page.json()["in_my_library"] is owner_sees
        assert other_page.json()["in_my_library"] is other_sees


def test_in_my_library_follows_add_remove_and_restore(database: Database, api: Api) -> None:
    paper_id = create_paper(database)

    def page_flag() -> bool:
        flag = api.send("GET", f"/api/v1/papers/{paper_id}", api.owner_token).json()
        return bool(flag["in_my_library"])

    assert page_flag() is False
    assert api.add(paper_id, api.owner_token).json()["paper"]["in_my_library"] is True
    assert page_flag() is True
    # 移出回的那一筆已經說不在（F19，ADR-0053 更新），之後讀論文頁也不在。
    assert api.remove(paper_id, api.owner_token).status_code == 200
    assert page_flag() is False
    assert api.restore(paper_id, api.owner_token).json()["paper"]["in_my_library"] is True
    assert page_flag() is True


def test_remove_says_not_in_my_library_add_and_restore_say_in_it(
    database: Database, api: Api
) -> None:
    # F19（ADR-0053 更新 2026-11-21）：移出的回應是假，加入、復原的回應是真；私人論文（移出之後
    # 就看不到）也一樣。
    visible_id = create_paper(database)
    private_id = _private_paper_of(database, api.owner_id)

    added = api.add(visible_id, api.owner_token)
    removed_visible = api.remove(visible_id, api.owner_token)
    removed_private = api.remove(private_id, api.owner_token)
    restored_visible = api.restore(visible_id, api.owner_token)
    restored_private = api.restore(private_id, api.owner_token)

    for response in (added, removed_visible, removed_private, restored_visible, restored_private):
        assert response.status_code == 200, response.text
    assert added.json()["paper"]["in_my_library"] is True
    assert removed_visible.json()["paper"]["in_my_library"] is False
    assert removed_private.json()["paper"]["in_my_library"] is False
    assert restored_visible.json()["paper"]["in_my_library"] is True
    assert restored_private.json()["paper"]["in_my_library"] is True


def test_library_list_rows_are_all_in_my_library(database: Database, api: Api) -> None:
    private_id = _private_paper_of(database, api.owner_id)
    added = create_paper(database, doi="10.1/added", openalex_id="W34")
    other_only = create_paper(database, doi="10.1/other", openalex_id="W35")
    api.add(added, api.owner_token)
    api.add(other_only, api.other_token)

    owner_items = api.list(api.owner_token).json()["items"]
    other_items = api.list(api.other_token).json()["items"]

    assert {item["paper"]["id"] for item in owner_items} == {str(private_id), str(added)}
    assert all(item["paper"]["in_my_library"] is True for item in owner_items)
    assert [(item["paper"]["id"], item["paper"]["in_my_library"]) for item in other_items] == [
        (str(other_only), True)
    ]


def test_system_and_public_reads_never_say_in_my_library(database: Database, api: Api) -> None:
    # 示範站匯出等系統身分、訪客身分的讀取：就算有人加過，也一律是假（不洩漏任何人的文獻庫）。
    paper_id = create_paper(database)
    api.add(paper_id, api.owner_token)
    api.add(paper_id, api.other_token)

    with database.transaction() as connection:
        system_rows = find_paper_rows(SystemViewer(), connection, [paper_id])
        system_page = find_paper_page_facts(SystemViewer(), connection, paper_id)
        public_rows = find_paper_rows(PublicViewer(), connection, [paper_id])
        owner_rows = find_paper_rows(
            MemberViewer(member_id=api.owner_id, is_admin=True), connection, [paper_id]
        )

    assert system_rows[paper_id].in_my_library is False
    assert system_page is not None
    assert system_page.in_my_library is False
    # 訪客只看得到算進已發佈快照的論文；這篇沒有快照，訪客讀不到，更不會帶出文獻庫狀態。
    assert public_rows == {}
    # 管理者也只看自己的文獻庫（這裡甲自己有加）。
    assert owner_rows[paper_id].in_my_library is True


def test_admin_does_not_see_other_members_library(database: Database, api: Api) -> None:
    paper_id = create_paper(database)
    # 前提：乙真的加入了（審查 F8：沒加成功時這條什麼都會過）。
    added = api.add(paper_id, api.other_token)
    assert added.status_code == 200, added.text
    assert added.json()["paper"]["in_my_library"] is True

    with database.transaction() as connection:
        rows = find_paper_rows(
            MemberViewer(member_id=api.owner_id, is_admin=True), connection, [paper_id]
        )
        page = find_paper_page_facts(
            MemberViewer(member_id=api.owner_id, is_admin=True), connection, paper_id
        )

    assert rows[paper_id].in_my_library is False
    assert page is not None
    assert page.in_my_library is False


def test_in_my_library_is_one_query_for_the_whole_page(db: TestDatabase) -> None:
    # 同一份請求：3 篇與 20 篇（一整頁）執行的 SQL 句數相同（不是每篇一句）；整頁的旗標只在論文列
    # 那一句裡算，文獻庫清單也一樣。
    counting = CountingDatabase(db.app_url)
    api = Api(counting)

    def add_papers(start: int, stop: int) -> list[uuid.UUID]:
        ids = [
            create_paper(counting, doi=f"10.1/n{index}", openalex_id=f"W4{index:02d}")
            for index in range(start, stop)
        ]
        for paper_id in ids[::2]:
            assert api.add(paper_id, api.owner_token).status_code == 200
        return ids

    def measure(path: str, params: dict[str, str] | None) -> tuple[list[dict[str, Any]], list[str]]:
        counting.statements.clear()
        response = api.send("GET", path, api.owner_token, params=params)
        assert response.status_code == 200, response.text
        return response.json()["items"], list(counting.statements)

    few = add_papers(0, 3)
    few_items, few_sql = measure("/api/v1/papers", {"scope": "lab"})
    few_library, few_library_sql = measure("/api/v1/library", None)
    many = few + add_papers(3, 30)
    many_items, many_sql = measure("/api/v1/papers", {"scope": "lab"})
    many_library, many_library_sql = measure("/api/v1/library", None)

    assert (len(few_items), len(many_items)) == (3, 20)
    assert (len(few_library), len(many_library)) == (2, 16)
    added = {str(paper_id) for paper_id in few[::2]} | {str(paper_id) for paper_id in many[3:][::2]}
    for item in many_items:
        assert item["in_my_library"] is (item["id"] in added)
    assert all(item["paper"]["in_my_library"] is True for item in many_library)
    assert len(few_sql) == len(many_sql)
    assert len(few_library_sql) == len(many_library_sql)
    for statements in (few_sql, many_sql, few_library_sql, many_library_sql):
        assert len([sql for sql in statements if "AS in_my_library" in sql]) == 1


def test_public_reads_of_a_visible_paper_never_say_in_my_library(
    db: TestDatabase, database: Database, api: Api
) -> None:
    # 審查 F8：訪客看得到的論文（算進已發佈的快照），乙確定加入了；訪客讀列表與論文頁都讀得到
    # 這一篇，而且 in_my_library 是假（不洩漏任何人的文獻庫）。
    paper_id = create_paper(database)
    count_in_snapshot(db, paper_id, publish_snapshot(db))
    added = api.add(paper_id, api.other_token)
    assert added.status_code == 200, added.text
    assert added.json()["paper"]["in_my_library"] is True

    with database.transaction() as connection:
        public_rows = find_paper_rows(PublicViewer(), connection, [paper_id])
        public_page = find_paper_page_facts(PublicViewer(), connection, paper_id)
        other_rows = find_paper_rows(
            MemberViewer(member_id=api.other_id, is_admin=False), connection, [paper_id]
        )

    assert set(public_rows) == {paper_id}
    assert public_rows[paper_id].in_my_library is False
    assert public_page is not None
    assert public_page.paper_id == paper_id
    assert public_page.in_my_library is False
    # 同一篇在乙自己讀的時候是真（確定上面的假不是因為根本沒加進去）。
    assert other_rows[paper_id].in_my_library is True


# --- 這個分頁以為的成員（epic-3h PR 3、ADR-0055）---------------------------------------------
# 情境：同一個瀏覽器，cookie 已經換成乙，分頁還以為是甲。三種寫入 × 標頭相符（照常）、不相符（409、
# 甲乙的文獻庫、封存、稽核都沒變）、格式不對（400、沒變）、沒帶（照常，和現在一樣）。

EXPECTED_MEMBER = "X-Radar-Expected-Member"


def _write_expecting(
    api: Api, method: str, path: str, token: str, expected: str | None
) -> Response:
    api.client.cookies.clear()
    headers = {"Cookie": f"{SESSION_COOKIE}={token}", "Origin": DEV_ORIGIN}
    if expected is not None:
        headers[EXPECTED_MEMBER] = expected
    return api.client.request(method, path, headers=headers)


def _library_state(db: TestDatabase) -> tuple[list[tuple[Any, ...]], ...]:
    records = execute(
        db,
        "SELECT id, source_table, source_id, owner_id, deleted_by_member_id FROM deleted_record"
        " ORDER BY id",
    )
    return _items(db), records, _audits(db)


@pytest.mark.parametrize("action", ["add", "remove", "restore"])
@pytest.mark.parametrize("header", ["match", "mismatch", "malformed", "absent"])
def test_writes_compare_the_expected_member_with_the_session(
    db: TestDatabase, database: Database, api: Api, action: str, header: str
) -> None:
    # 甲、乙都有 shared；乙移出過 removed（有封存，可以復原）；fresh 誰都沒有。
    shared = create_paper(database, doi="10.1/shared", openalex_id="W91")
    removed = create_paper(database, doi="10.1/removed", openalex_id="W92")
    fresh = create_paper(database, doi="10.1/fresh", openalex_id="W93")
    for token in (api.owner_token, api.other_token):
        assert api.add(shared, token).status_code == 200
    assert api.add(removed, api.other_token).status_code == 200
    assert api.remove(removed, api.other_token).status_code == 200
    method, path, paper_id = {
        "add": ("PUT", f"/api/v1/library/{fresh}", fresh),
        "remove": ("DELETE", f"/api/v1/library/{shared}", shared),
        "restore": ("POST", f"/api/v1/library/{removed}/restore", removed),
    }[action]
    expected = {
        "match": str(api.other_id),
        "mismatch": str(api.owner_id),
        "malformed": "not-a-member-id",
        "absent": None,
    }[header]
    before = _library_state(db)

    response = _write_expecting(api, method, path, api.other_token, expected)

    if header in ("mismatch", "malformed"):
        status, code = (
            (409, "member_changed") if header == "mismatch" else (400, "expected_member_invalid")
        )
        assert response.status_code == status, response.text
        assert response.json()["code"] == code
        assert response.headers["cache-control"] == "no-store"
        assert _library_state(db) == before
        return
    assert response.status_code == 200, response.text
    assert response.json()["paper"]["id"] == str(paper_id)
    other_papers = {row[2] for row in _items(db) if row[1] == api.other_id}
    owner_papers = {row[2] for row in _items(db) if row[1] == api.owner_id}
    assert (
        other_papers
        == {
            "add": {shared, fresh},
            "remove": set(),
            "restore": {shared, removed},
        }[action]
    )
    # 甲的文獻庫一直只有 shared（乙的寫入不會動到甲）。
    assert owner_papers == {shared}
