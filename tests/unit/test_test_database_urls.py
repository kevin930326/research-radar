"""測試資料庫的連線字串：換成擁有者／應用程式角色時，不能帶著管理者的密碼。"""

import pytest

from tests.database import ADMIN_URL_ENV, APP_ROLE, OWNER_ROLE, TestDatabase

ADMIN_WITH_PASSWORD = "postgresql://postgres:s3cret-ADMIN@db.example:5432/postgres"


def test_role_urls_drop_admin_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ADMIN_URL_ENV, ADMIN_WITH_PASSWORD)
    database = TestDatabase("radar_test_x")

    assert database.owner_url == f"postgresql://{OWNER_ROLE}@db.example:5432/radar_test_x"
    assert database.app_url == f"postgresql://{APP_ROLE}@db.example:5432/radar_test_x"
    assert "s3cret-ADMIN" not in database.owner_url + database.app_url


def test_admin_url_keeps_admin_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ADMIN_URL_ENV, ADMIN_WITH_PASSWORD)

    assert TestDatabase("radar_test_x").admin_url == (
        "postgresql://postgres:s3cret-ADMIN@db.example:5432/radar_test_x"
    )
