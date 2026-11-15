"""`radar members allow`／`set-admin` 對真的 PostgreSQL（`CliRunner`＋環境變數；Story 3.3）。

email 存小寫、成員還沒有 Google 的 sub、真的改到資料才寫一筆稽核（system）、輸出與日誌只有成員 ID；
重複執行不變更也不寫稽核；格式不對退出碼 1、不寫任何東西。資料全是測試自己編的
（`*@example.com`、假名字）。
"""

import logging
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

import radar.entrypoints.cli as cli
from tests.database import ROOT, TestDatabase, migrate
from tests.llm_support import execute

runner = CliRunner()

EMAIL = "Member.One@Example.com"
STORED_EMAIL = "member.one@example.com"


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def env(db: TestDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", config_dir)
    monkeypatch.setenv("DATABASE_URL", db.app_url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))


def _members(*args: str) -> Result:
    """執行 `radar members ...`；結束後把 root logger 的 handler 換回原本的。"""
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        return runner.invoke(cli.app, ["members", *args])
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)


def _member_rows(db: TestDatabase) -> list[tuple[object, ...]]:
    return execute(
        db,
        "SELECT m.id, m.display_name, m.is_admin, m.google_sub, m.first_login_at, a.email"
        " FROM member m JOIN allowed_email a ON a.member_id = m.id",
    )


def _audit_rows(db: TestDatabase) -> list[tuple[object, ...]]:
    return execute(
        db,
        "SELECT actor_member_id, actor_role, action, target_type, target_id, detail"
        " FROM audit_event ORDER BY created_at, id",
    )


def _counts(db: TestDatabase) -> tuple[int, int, int]:
    (row,) = execute(
        db,
        "SELECT (SELECT count(*) FROM member), (SELECT count(*) FROM allowed_email),"
        " (SELECT count(*) FROM audit_event)",
    )
    return int(row[0]), int(row[1]), int(row[2])


def _assert_no_email(result: Result) -> None:
    assert "@" not in result.output
    assert "example.com" not in result.output.lower()


def test_allow_creates_member_with_lowercase_email_and_one_audit(
    db: TestDatabase, env: None
) -> None:
    result = _members("allow", EMAIL, "--name", "  測試甲  ")

    assert result.exit_code == 0, result.output
    ((member_id, name, is_admin, google_sub, first_login_at, email),) = _member_rows(db)
    assert email == STORED_EMAIL
    assert name == "測試甲"
    assert is_admin is False
    assert google_sub is None
    assert first_login_at is None
    assert f"已加入允許名單：成員 ID {member_id}" in result.output
    _assert_no_email(result)
    assert _audit_rows(db) == [(None, "system", "allow_member", "member", member_id, {})]


def test_allow_twice_changes_nothing(db: TestDatabase, env: None) -> None:
    assert _members("allow", EMAIL, "--name", "測試甲").exit_code == 0

    result = _members("allow", STORED_EMAIL.upper(), "--name", "別的名字")

    assert result.exit_code == 0, result.output
    ((member_id, name, *_rest),) = _member_rows(db)
    assert name == "測試甲"
    assert f"已經在允許名單裡，沒有變更：成員 ID {member_id}" in result.output
    _assert_no_email(result)
    assert len(_audit_rows(db)) == 1


@pytest.mark.parametrize(
    "args",
    [
        ("allow", "not-an-email", "--name", "測試甲"),
        ("allow", "member two@example.com", "--name", "測試乙"),
        ("allow", "member.two@example.com", "--name", "   "),
        ("allow", "member.two@example.com", "--name", "x" * 101),
        ("allow", "member.two@example.com", "--name", "測試\n乙"),
    ],
)
def test_allow_with_bad_input_exits_1_and_writes_nothing(
    db: TestDatabase, env: None, args: tuple[str, ...]
) -> None:
    result = _members(*args)

    assert result.exit_code == 1
    assert _counts(db) == (0, 0, 0)
    _assert_no_email(result)


def test_set_admin_marks_member_and_writes_audit(db: TestDatabase, env: None) -> None:
    assert _members("allow", EMAIL, "--name", "測試甲").exit_code == 0

    result = _members("set-admin", STORED_EMAIL)

    assert result.exit_code == 0, result.output
    ((member_id, _name, is_admin, *_rest),) = _member_rows(db)
    assert is_admin is True
    assert f"已設成管理者：成員 ID {member_id}" in result.output
    _assert_no_email(result)
    assert _audit_rows(db)[-1] == (None, "system", "set_member_admin", "member", member_id, {})
    assert len(_audit_rows(db)) == 2


def test_set_admin_twice_changes_nothing(db: TestDatabase, env: None) -> None:
    assert _members("allow", EMAIL, "--name", "測試甲").exit_code == 0
    assert _members("set-admin", EMAIL).exit_code == 0

    result = _members("set-admin", EMAIL)

    assert result.exit_code == 0, result.output
    assert "本來就是管理者，沒有變更" in result.output
    assert len(_audit_rows(db)) == 2


@pytest.mark.parametrize("email", ["nobody@example.com", "not-an-email"])
def test_set_admin_for_unknown_or_bad_email_exits_1(
    db: TestDatabase, env: None, email: str
) -> None:
    assert _members("allow", EMAIL, "--name", "測試甲").exit_code == 0

    result = _members("set-admin", email)

    assert result.exit_code == 1
    ((_id, _name, is_admin, *_rest),) = _member_rows(db)
    assert is_admin is False
    assert len(_audit_rows(db)) == 1
    _assert_no_email(result)
