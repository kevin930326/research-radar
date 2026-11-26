"""計數方塊的 API 對真的 PostgreSQL（Story 3.15、AD-19、ADR-0062）。

- 成員版 `GET /api/v1/cube` 和同一份資料匯出的示範站 `cube.json` 的計數與 `params` 一樣（兩邊同一個
  查詢、同一個形狀），快照編號也一樣；60 篇的範例資料合計 60 篇、其中 51 篇有摘要。
- 還沒有已發佈的快照：`snapshot` 是 null、`rows` 是空的。
- 沒登入、停用（移出允許名單）的成員、不是成員（沒有這個 session）都是 401 `not_authenticated`、
  `no-store`，而且沒有讀計數。

成員經開發環境的假登入取得真的 session（路由用正式的 `install_api_routes`）。論文是
`tests/demo_export_support.preview_specs` 的假論文（`Fake Paper …`）。
"""

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from radar.adapters.fake.login import pick_member_identity
from radar.domain.auth_settings import AuthSettings, Environment
from radar.domain.contracts import CubeResponse
from radar.domain.demo_export import CUBE_FILE, PREVIEW_DIR, data_file_path
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import SESSION_COOKIE, build_auth_routes
from radar.entrypoints.api.main import install_api_routes
from radar.repo.db import Database
from radar.services.demo_export import queue_demo_export, run_demo_export
from radar.services.members import allow_member
from tests.database import TestDatabase, migrate
from tests.demo_export_support import (
    EXPORTED_AT,
    demo_setup,
    preview_specs,
    publish_snapshots,
    seed_papers,
)
from tests.llm_support import execute, library_routes, paper_list_routes, paper_routes

DEV_ORIGIN = "http://localhost:8000"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
SYSTEM = SystemViewer()


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


def _client(database: Database, reads: list[MemberViewer] | None = None) -> tuple[TestClient, str]:
    """真的路由與真的計數服務；給 `reads` 時，每次讀計數記下身分（證明被擋下的請求沒有讀）。"""
    settings = AuthSettings(environment=Environment.DEVELOPMENT, fake_login=True)
    app = create_app(readiness_check=lambda: True)
    routes = paper_list_routes(database)
    if reads is not None:
        load = routes.load_cube

        def recorded(viewer: MemberViewer) -> CubeResponse:
            reads.append(viewer)
            return load(viewer)

        routes = replace(routes, load_cube=recorded)
    install_api_routes(
        app,
        auth_routes=build_auth_routes(database, settings, pick_member_identity, lambda: NOW),
        paper_routes=paper_routes(database, now=NOW),
        library_routes=library_routes(database),
        paper_list_routes=routes,
    )
    client = TestClient(app, base_url="https://testserver")
    member_id = allow_member(
        SYSTEM, database, email="cube.member@example.org", display_name="測試甲"
    ).member_id
    response = client.post(
        "/api/v1/auth/fake-login",
        json={"member_id": str(member_id)},
        headers={"Origin": DEV_ORIGIN},
    )
    assert response.status_code == 200, response.text
    client.cookies.clear()
    _, _, rest = response.headers["set-cookie"].partition("=")
    return client, rest.split(";", 1)[0]


def _cube(client: TestClient, token: str | None) -> Any:
    client.cookies.clear()
    headers = {} if token is None else {"Cookie": f"{SESSION_COOKIE}={token}"}
    return client.get("/api/v1/cube", headers=headers)


def test_member_cube_matches_the_demo_cube(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    snapshots = publish_snapshots(db)
    seed_papers(db, database, preview_specs(), snapshots=snapshots)
    run = queue_demo_export(SYSTEM, database)
    result = run_demo_export(
        SYSTEM, database, demo_setup(), run.id, tmp_path, clock=lambda: EXPORTED_AT
    )
    assert result is not None
    demo = json.loads(
        (tmp_path / PREVIEW_DIR / data_file_path(result.export_id, CUBE_FILE)).read_bytes()
    )
    client, token = _client(database)

    response = _cube(client, token)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    member = response.json()
    assert member["rows"] == demo["rows"]
    assert member["params"] == demo["params"]
    assert member["snapshot"] == demo["snapshot"] == {"number": 2, "as_of": "2026-11-16T03:00:00Z"}
    assert sum(row["n"] for row in member["rows"] if row["dim"] == "total") == 60
    assert sum(row["n"] for row in member["rows"] if row["dim"] == "with_abstract") == 51


def test_no_published_snapshot_yet(database: Database) -> None:
    client, token = _client(database)

    response = _cube(client, token)

    assert response.status_code == 200
    assert response.json()["rows"] == []
    assert response.json()["snapshot"] is None


def test_needs_a_session(database: Database) -> None:
    client, _token = _client(database)

    response = _cube(client, None)

    assert response.status_code == 401


def _assert_rejected_without_reading(response: Any, reads: list[MemberViewer]) -> None:
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"
    assert response.headers["cache-control"] == "no-store"
    assert reads == []


def test_member_removed_from_the_allowlist_is_rejected(
    db: TestDatabase, database: Database
) -> None:
    # 停用：移出允許名單之後，原本的 session 馬上失效，計數一次都沒讀。
    reads: list[MemberViewer] = []
    client, token = _client(database, reads)
    execute(db, "DELETE FROM allowed_email WHERE email = %s", "cube.member@example.org")

    _assert_rejected_without_reading(_cube(client, token), reads)


def test_someone_who_is_not_a_member_is_rejected(database: Database) -> None:
    # 不是成員：格式正確、但沒有這個 session 的 token（不在允許名單的人拿不到 session）。
    reads: list[MemberViewer] = []
    client, token = _client(database, reads)
    stranger = ("A" if token[0] != "A" else "B") + token[1:]

    _assert_rejected_without_reading(_cube(client, stranger), reads)


def test_the_member_read_is_recorded_as_that_member(db: TestDatabase, database: Database) -> None:
    # 對照：同一個組法下，有效的 session 真的會讀一次計數（上面兩條的「沒讀」才算數），而且讀的
    # 身分就是登入的那位成員（不是別人、也不是管理者）。
    reads: list[MemberViewer] = []
    client, token = _client(database, reads)
    rows = execute(
        db, "SELECT member_id FROM allowed_email WHERE email = %s", "cube.member@example.org"
    )

    assert _cube(client, token).status_code == 200
    assert reads == [MemberViewer(member_id=rows[0][0], is_admin=False)]


def test_someone_outside_the_allowlist_gets_no_session_and_no_counts(
    db: TestDatabase, database: Database
) -> None:
    # 不是成員的真實身分：不在允許名單的人走同一個登入端點（照 test_sessions 測名單外的寫法），
    # 拿不到 session；帶著登入回應留下的任何 cookie 去讀計數，也是 401、一次都沒讀。
    reads: list[MemberViewer] = []
    client, _token = _client(database, reads)
    sessions_before = execute(db, "SELECT count(*) FROM session")
    client.cookies.clear()

    login = client.post(
        "/api/v1/auth/fake-login",
        json={"member_id": str(uuid.uuid4())},
        headers={"Origin": DEV_ORIGIN},
    )

    assert login.status_code == 403
    assert login.json()["code"] == "login_rejected"
    assert "set-cookie" not in login.headers
    assert execute(db, "SELECT count(*) FROM session") == sessions_before
    _assert_rejected_without_reading(client.get("/api/v1/cube"), reads)
