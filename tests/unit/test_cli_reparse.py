"""`radar reparse` 的輸出與退出碼（替身取代 `reparse_papers` 與 bootstrap，不連資料庫）。"""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import radar.entrypoints.cli as cli
from radar.adapters.fake import FakeObjectStore
from radar.domain.domain_config import ExclusionRulesConfig
from radar.domain.scope import ScopeSettings
from radar.domain.viewer import SystemViewer
from radar.services.reparse import AdvisoryLockLostError, ReparseResult

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[2]
SECRET_URL = "postgresql://radar_app:s3cret-APP@db.example:5432/radar"
OBJECT_STORE_ENV = {
    "RADAR_OBJECT_STORE_ENDPOINT_URL": "http://127.0.0.1:9000",
    "RADAR_OBJECT_STORE_REGION": "us-east-1",
    "RADAR_OBJECT_STORE_BUCKET": "radar",
    "RADAR_OBJECT_STORE_ACCESS_KEY_ID": "test-access",
    "RADAR_OBJECT_STORE_SECRET_ACCESS_KEY": "test-secret-KEY",
}
RESULT = ReparseResult(
    raw_keys=11,
    not_in_database=2,
    missing_object=1,
    hash_mismatch=3,
    parse_error=4,
    id_mismatch=5,
    staged=6,
    updated=7,
    stale=8,
    conflicts=9,
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)
    monkeypatch.delenv("RADAR_CLI_DATABASE_POOL_SIZE", raising=False)
    for name, value in OBJECT_STORE_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    recorded: dict[str, Any] = {}
    store = FakeObjectStore()
    database = object()

    def build_database(_settings: object, **kwargs: Any) -> object:
        recorded["build_database"] = kwargs
        return database

    def build_object_store(_settings: object) -> FakeObjectStore:
        return store

    def reparse_papers(actor: object, db: object, st: object, **kwargs: Any) -> ReparseResult:
        recorded["reparse"] = {"actor": actor, "database": db, "store": st, **kwargs}
        return RESULT

    monkeypatch.setattr(cli, "build_database", build_database)
    monkeypatch.setattr(cli, "build_object_store", build_object_store)
    monkeypatch.setattr(cli, "reparse_papers", reparse_papers)
    recorded["database"] = database
    recorded["store"] = store
    return recorded


def test_help_lists_reparse() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "reparse" in result.output


@pytest.mark.usefixtures("env")
def test_success_prints_every_number(calls: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["reparse"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "重新解析完成"
    assert [line.rsplit("：", 1)[1] for line in lines[1:]] == [
        "11",
        "2",
        "1",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
    ]
    assert calls["build_database"] == {"pool_size": 2}
    reparse = calls["reparse"]
    assert isinstance(reparse["actor"], SystemViewer)
    assert reparse["database"] is calls["database"]
    assert reparse["store"] is calls["store"]
    assert isinstance(reparse["scope"], ScopeSettings)
    assert "1471-7727" in reparse["scope"].journals
    assert isinstance(reparse["exclusion_rules"], ExclusionRulesConfig)
    assert "parse" not in reparse
    assert "source" not in reparse


@pytest.mark.usefixtures("calls")
def test_missing_database_url_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    result = runner.invoke(cli.app, ["reparse"])

    assert result.exit_code == 1
    assert "DATABASE_URL" in result.stderr
    assert "重新解析完成" not in result.stdout


@pytest.mark.usefixtures("env")
def test_missing_object_store_settings_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RADAR_OBJECT_STORE_BUCKET")
    monkeypatch.setattr(cli, "reparse_papers", _explode)

    result = runner.invoke(cli.app, ["reparse"])

    assert result.exit_code == 1
    assert "RADAR_OBJECT_STORE_BUCKET" in result.stderr
    assert "test-secret-KEY" not in result.output


def _explode(*_args: Any, **_kwargs: Any) -> ReparseResult:
    raise RuntimeError(f"connection to {SECRET_URL} failed")


@pytest.mark.usefixtures("env", "calls")
def test_failure_prints_only_error_class(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "reparse_papers", _explode)

    result = runner.invoke(cli.app, ["reparse"])

    assert result.exit_code == 1
    assert "RuntimeError" in result.stderr
    assert "s3cret-APP" not in result.output
    assert "db.example" not in result.output
    assert "s3cret-APP" not in capsys.readouterr().out


FAKE_PASSWORD = "FAKE-PASSWORD-not-real-0000"
FAKE_ENDPOINT = f"http://fake-user:{FAKE_PASSWORD}@bad-host.invalid:9000"


def _store_init_fails(_settings: object) -> FakeObjectStore:
    # botocore 建 client 時端點不合法，就是丟這種帶整個網址的 ValueError。
    raise ValueError(f"Invalid endpoint: {FAKE_ENDPOINT}")


@pytest.mark.usefixtures("env", "calls")
def test_object_store_init_failure_prints_only_error_class(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "build_object_store", _store_init_fails)

    # 不讓 CliRunner 吞掉例外：沒接住的話測試直接失敗（實際執行時會印出整段例外訊息）。
    result = runner.invoke(cli.app, ["reparse"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "ValueError" in result.stderr
    assert '"error_type": "ValueError"' in result.stdout
    captured = capsys.readouterr()
    for text in (result.stdout, result.stderr, captured.out, captured.err):
        assert FAKE_PASSWORD not in text
        assert "fake-user" not in text
        assert "bad-host.invalid" not in text
        assert "test-secret-KEY" not in text
    assert "重新解析完成" not in result.stdout


@pytest.mark.usefixtures("env")
def test_invalid_object_store_endpoint_prints_only_error_class(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 真的 build_object_store：主機名稱不合法但通過設定驗證，botocore 建 client 時才失敗。
    monkeypatch.setenv("RADAR_OBJECT_STORE_ENDPOINT_URL", "http://bad host!.invalid:9000")
    monkeypatch.setenv("RADAR_OBJECT_STORE_ACCESS_KEY_ID", "FAKE-ACCESS-not-real")
    monkeypatch.setenv("RADAR_OBJECT_STORE_SECRET_ACCESS_KEY", FAKE_PASSWORD)
    monkeypatch.setattr(cli, "reparse_papers", _botocore_should_have_failed)

    result = runner.invoke(cli.app, ["reparse"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "ValueError" in result.stderr
    # 日誌（JSON）寫在 stdout：只有錯誤類別，沒有主機與假帳密。
    assert '"error_type": "ValueError"' in result.stdout
    captured = capsys.readouterr()
    for text in (result.stdout, result.stderr, captured.out, captured.err):
        assert "bad host!" not in text
        assert "bad%20host" not in text
        assert FAKE_PASSWORD not in text
        assert "FAKE-ACCESS-not-real" not in text
        assert "s3cret-APP" not in text
    assert "重新解析完成" not in result.stdout


def _botocore_should_have_failed(*_args: Any, **_kwargs: Any) -> ReparseResult:
    pytest.fail("botocore 沒有擋下不合法的端點")


@pytest.mark.usefixtures("env")
def test_endpoint_with_credentials_is_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], calls: dict[str, Any]
) -> None:
    monkeypatch.setenv("RADAR_OBJECT_STORE_ENDPOINT_URL", FAKE_ENDPOINT)

    result = runner.invoke(cli.app, ["reparse"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "RADAR_OBJECT_STORE_ENDPOINT_URL" in result.stderr
    captured = capsys.readouterr()
    for text in (result.stdout, result.stderr, captured.out, captured.err):
        assert FAKE_PASSWORD not in text
        assert "fake-user" not in text
    assert "reparse" not in calls


def _lock_lost(*_args: Any, **_kwargs: Any) -> ReparseResult:
    # 這個替身證明 CLI 不印例外鏈（連線字串掛在鏈上）。CLI 會原樣印出例外訊息本身；訊息不含
    # 連線字串，是由 radar/repo/db.py 組訊息的方式保證（原因只帶固定字串或錯誤類別）。
    try:
        raise RuntimeError(f"connection to {SECRET_URL} lost")
    except RuntimeError as cause:
        raise AdvisoryLockLostError("持有鎖的連線斷了（OperationalError）") from cause


@pytest.mark.usefixtures("env", "calls")
def test_lost_write_lock_prints_reason_and_rerun_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "reparse_papers", _lock_lost)

    result = runner.invoke(cli.app, ["reparse"], catch_exceptions=False)

    assert result.exit_code == 1
    # 印出例外裡的安全訊息（原因、已 commit 的不受影響）與「直接重跑」，不是只有類別名稱。
    [headline] = result.stderr.splitlines()
    assert headline == (
        "寫入鎖已經不在了（持有鎖的連線斷了（OperationalError）），這次作業中止；"
        "已經 commit 的交易不受影響；直接重跑 radar reparse 即可"
    )
    assert "細節見日誌" not in result.stderr
    # 日誌（JSON，寫在 stdout）記原因。
    assert '"reason": "持有鎖的連線斷了（OperationalError）"' in result.stdout
    captured = capsys.readouterr()
    for text in (result.stdout, result.stderr, captured.out, captured.err):
        assert "s3cret-APP" not in text
        assert "db.example" not in text
    assert "重新解析完成" not in result.stdout
