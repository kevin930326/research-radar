"""換人測試與金絲雀測試，對真的 PostgreSQL（Story 3.4、FR-7、AD-3、ADR-0047）。

兩位成員都經開發環境的假登入取得真的 session：成員 A 的 email 是金絲雀字串（`CANARY`），成員 B 打
`tests/permissions/cases.py` 登記的每一個端點。

- 金絲雀：B 的回應（本文與標頭）與這段期間的日誌，都不能出現金絲雀字串，也不能出現 A 的
  session token。
- 換人測試：沒有豁免的端點，B 打 A 的資料要得到 404 或空清單。目前的端點都豁免（理由寫在登記表），
  所以這一組現在沒有用例；第一個會回成員資料的端點（Story 3.6 起）加進登記表後就會跑。
資料全是測試自己編的（`*@example.com`、假名字）。
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from radar.adapters.fake.login import pick_member_identity
from radar.domain.auth_settings import AuthSettings, Environment
from radar.domain.viewer import SystemViewer
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import SESSION_COOKIE, build_auth_routes
from radar.entrypoints.api.main import install_api_routes
from radar.entrypoints.logging_config import configure_logging
from radar.repo.db import Database
from radar.services.members import allow_member
from tests.database import TestDatabase, migrate
from tests.llm_support import execute
from tests.permissions.cases import REGISTRY
from tests.permissions.framework import (
    CANARY,
    CANARY_EMAIL,
    EndpointCase,
    Member,
    PermissionContext,
    assert_hidden_from_other_member,
    response_texts,
    secrets_found,
)

DEV_ORIGIN = "http://localhost:8000"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
OTHER_EMAIL = "other.member@example.com"

CASES = sorted(REGISTRY.cases.values(), key=lambda case: case.endpoint)
CROSS_MEMBER_CASES = [case for case in CASES if case.exempt_reason is None]


def _case_id(case: EndpointCase) -> str:
    return str(case.endpoint)


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _log_in(client: TestClient, database: Database, email: str, name: str) -> Member:
    member_id = allow_member(SystemViewer(), database, email=email, display_name=name).member_id
    response = client.post(
        "/api/v1/auth/fake-login",
        json={"member_id": str(member_id)},
        headers={"Origin": DEV_ORIGIN},
    )
    assert response.status_code == 200, response.text
    client.cookies.clear()
    name_value, _, rest = response.headers["set-cookie"].partition("=")
    assert name_value == SESSION_COOKIE
    return Member(member_id=str(member_id), token=rest.split(";", 1)[0], email=email)


@pytest.fixture
def context(db: TestDatabase, capsys: pytest.CaptureFixture[str]) -> PermissionContext:
    # 日誌接到 capsys：測試讀得到從這裡開始的每一筆（含兩位成員登入）。
    configure_logging()
    database = Database(db.app_url, connect_timeout=5, pool_size=2)
    settings = AuthSettings(environment=Environment.DEVELOPMENT, fake_login=True)
    app = create_app(readiness_check=lambda: True)
    # 和正式的 main() 用同一個函式裝路由。
    install_api_routes(
        app, auth_routes=build_auth_routes(database, settings, pick_member_identity, lambda: NOW)
    )
    client = TestClient(app, base_url="https://testserver")
    owner = _log_in(client, database, CANARY_EMAIL, "測試甲")
    other = _log_in(client, database, OTHER_EMAIL, "測試乙")
    return PermissionContext(
        client=client,
        owner=owner,
        other=other,
        origin=DEV_ORIGIN,
        extras={"database": database, "test_database": db},
    )


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_other_member_never_sees_the_canary(
    case: EndpointCase,
    context: PermissionContext,
    db: TestDatabase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 前提：金絲雀確實在成員 A 的私人資料裡（不然這個測試永遠會過）。
    emails = {row[0] for row in execute(db, "SELECT email FROM allowed_email")}
    assert CANARY_EMAIL in emails

    response = case.call(context)
    logs = capsys.readouterr().out

    # B 的 session 是有效的：這個請求真的是以成員 B 的身分送的。
    assert response.status_code != 401, response.text
    # 日誌真的有接到（不然「日誌裡沒有」沒有意義）。
    assert "請求完成" in logs
    assert secrets_found([*response_texts(response), logs], [CANARY, context.owner.token]) == []


@pytest.mark.parametrize("case", CROSS_MEMBER_CASES, ids=_case_id)
def test_other_member_gets_404_or_an_empty_list(
    case: EndpointCase, context: PermissionContext
) -> None:
    assert_hidden_from_other_member(case.call(context))


def test_other_member_session_is_really_the_other_member(context: PermissionContext) -> None:
    # 對照：as_other 帶的是 B 的 session，認出來的是 B，不是 A。
    me = context.as_other("GET", "/api/v1/me")

    assert me.status_code == 200
    assert uuid.UUID(me.json()["member_id"]) == uuid.UUID(context.other.member_id)
    assert context.other.member_id != context.owner.member_id
