"""資料庫出錯時，例外訊息與 api 的例外日誌不帶查詢參數（ADR-0046：日誌不出現 email、token）。

用真的 `Database`（真的 SQLAlchemy engine 與 psycopg 方言），只把驅動的連線換成假的：每一句
SQL 都丟 psycopg 的 `QueryCanceled`（等同超過 statement_timeout）。不連資料庫、不連網。
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest import mock

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from radar.config import load_auth_settings
from radar.domain.auth import FakeLoginRequest, LoginIdentity, VerifiedEmail
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import (
    ACCESS_ASSERTION_HEADER,
    build_auth_routes,
    install_auth_routes,
)
from radar.entrypoints.logging_config import configure_logging
from radar.repo.db import Database
from radar.repo.members import find_allowed_member_by_email
from radar.repo.migrations import create_migration_engine

ADMIN_EMAIL = "admin@example.com"
SITE_ORIGIN = "https://radar.example.com"
DATABASE_URL = "postgresql+psycopg://radar_app@db.invalid:5432/radar"
TRIAL = {
    "RADAR_ENVIRONMENT": "trial",
    "RADAR_FAKE_LOGIN": "true",
    "RADAR_ACCESS_TEAM_DOMAIN": "testteam.cloudflareaccess.com",
    "RADAR_ACCESS_AUDIENCE": "test-audience",
    "RADAR_ADMIN_EMAIL": ADMIN_EMAIL,
    "RADAR_SITE_ORIGIN": SITE_ORIGIN,
}


@pytest.fixture
def failing_database() -> Iterator[Database]:
    """真的 `Database`，驅動連線換成假的：每一句 SQL 都丟 `QueryCanceled`。"""
    database = Database(DATABASE_URL, connect_timeout=3)
    engine = database._engine
    dbapi_connection = mock.MagicMock(name="dbapi_connection")
    dbapi_connection.cursor.return_value.execute.side_effect = psycopg.errors.QueryCanceled(
        "canceling statement due to statement timeout"
    )

    def connect(*_args: object) -> object:
        return dbapi_connection

    event.listen(engine, "do_connect", connect)
    # 第一次連線時方言會查伺服器版本等資訊；假連線回不了，跳過。
    with mock.patch.object(engine.dialect, "initialize", return_value=None):
        yield database
    engine.dispose()


def test_engines_hide_parameters() -> None:
    assert Database(DATABASE_URL, connect_timeout=3)._engine.hide_parameters is True
    assert create_migration_engine(DATABASE_URL, connect_timeout=3).hide_parameters is True


def test_query_error_message_has_no_email(failing_database: Database) -> None:
    with (
        pytest.raises(OperationalError) as caught,
        failing_database.transaction() as connection,
    ):
        find_allowed_member_by_email(connection, ADMIN_EMAIL)

    message = str(caught.value)
    assert "QueryCanceled" in message
    assert "hide_parameters=True" in message  # SQLAlchemy 的「參數已隱藏」字樣
    assert ADMIN_EMAIL not in message


def test_trial_fake_login_database_error_logs_no_email(
    failing_database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    """試用環境假登入時查管理者 email 出錯：api 回 500，例外日誌有錯誤追蹤但沒有 email。

    走真的登入路由、service、repo 與 engine；只有 Access JWT 的驗證換成直接回 email
    （真的驗證要 Cloudflare 的公鑰，在 `test_access_jwt.py` 測）。
    """
    configure_logging()

    def confirm(_request: FakeLoginRequest) -> LoginIdentity:
        return VerifiedEmail(email=ADMIN_EMAIL)

    app = create_app(readiness_check=lambda: True)
    install_auth_routes(
        app,
        build_auth_routes(
            failing_database,
            load_auth_settings(TRIAL),
            confirm,
            lambda: datetime(2026, 11, 15, tzinfo=UTC),
        ),
    )
    client = TestClient(app, base_url=SITE_ORIGIN, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/auth/fake-login",
        json={},
        headers={"Origin": SITE_ORIGIN, ACCESS_ASSERTION_HEADER: "jwt-value"},
    )

    assert response.status_code == 500
    output = capsys.readouterr().out
    assert "請求失敗" in output
    assert "QueryCanceled" in output  # 錯誤追蹤確實記進日誌
    assert ADMIN_EMAIL not in output
    assert ADMIN_EMAIL not in response.text
