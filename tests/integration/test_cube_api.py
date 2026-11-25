"""計數方塊的 API 對真的 PostgreSQL（Story 3.15、AD-19、ADR-0062）。

- 成員版 `GET /api/v1/cube` 和同一份資料匯出的示範站 `cube.json` 的計數與 `params` 一樣（兩邊同一個
  查詢、同一個形狀），快照編號也一樣；60 篇的範例資料合計 60 篇、其中 51 篇有摘要。
- 還沒有已發佈的快照：`snapshot` 是 null、`rows` 是空的。
- 沒登入 401。

成員經開發環境的假登入取得真的 session（路由用正式的 `install_api_routes`）。論文是
`tests/demo_export_support.preview_specs` 的假論文（`Fake Paper …`）。
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from radar.adapters.fake.login import pick_member_identity
from radar.domain.auth_settings import AuthSettings, Environment
from radar.domain.demo_export import CUBE_FILE, PREVIEW_DIR, data_file_path
from radar.domain.viewer import SystemViewer
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
from tests.llm_support import library_routes, paper_list_routes, paper_routes

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


def _client(database: Database) -> tuple[TestClient, str]:
    settings = AuthSettings(environment=Environment.DEVELOPMENT, fake_login=True)
    app = create_app(readiness_check=lambda: True)
    install_api_routes(
        app,
        auth_routes=build_auth_routes(database, settings, pick_member_identity, lambda: NOW),
        paper_routes=paper_routes(database, now=NOW),
        library_routes=library_routes(database),
        paper_list_routes=paper_list_routes(database),
    )
    client = TestClient(app, base_url="https://testserver")
    member_id = allow_member(
        SYSTEM, database, email="cube.member@example.com", display_name="測試甲"
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
