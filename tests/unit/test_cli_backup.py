"""`radar backup`／`radar restore` 的輸出與退出碼（Story 3.18）。

不連資料庫、不跑外部工具、不碰物件儲存。
"""

import importlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs
from typer.testing import CliRunner

import radar.entrypoints.cli as cli
from radar.domain.backup import (
    BackupCounts,
    BackupManifest,
    BackupMode,
    BackupToolError,
    RestoreCountMismatchError,
    RestoreRefusedError,
    RestoreVerificationError,
)
from radar.services.backup import BackupResult, RestoreResult

# `radar.entrypoints.cli` 把指令函式 `backup` 匯出成同名屬性，`import … as` 會拿到函式；直接取模組。
backup_cli = importlib.import_module("radar.entrypoints.cli.backup")
runner = CliRunner()
APP_URL = "postgresql://radar_app:s3cret-APP@db.example:5432/radar"
OWNER_URL = "postgresql://radar_owner:s3cret-OWNER@db.example:5432/radar"
RECIPIENT = "age1" + "q" * 58
MANIFEST = BackupManifest(
    mode=BackupMode.FULL,
    created_at=datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
    postgres_version="18.1",
    postgres_version_num=180001,
    pgvector_version="0.8.7",
    pg_dump_version="18.7",
    alembic_revisions=("0020",),
    counts=BackupCounts(
        counted_paper=3,
        zh_summary_active=2,
        classification_active=2,
        research_model_active=1,
        embedding=3,
    ),
    member_table_rows={"member": 2},
)


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把組裝換成替身，記下呼叫了哪些（沒有任何一個真的連線）。"""
    calls: list[str] = []

    def stub(name: str) -> Callable[..., object]:
        def build(*_a: object, **_k: object) -> object:
            calls.append(name)
            return (None, None) if name.endswith("_tools") else None

        return build

    for name in (
        "build_object_store",
        "build_database",
        "build_backup_tools",
        "build_restore_database",
        "build_restore_tools",
        "build_known_revisions",
    ):
        monkeypatch.setattr(backup_cli, name, stub(name))
    # 日誌由 capture_logs 接（CLI 自己的設定會蓋掉它）。
    monkeypatch.setattr(backup_cli, "configure_logging", lambda: None)
    return calls


def _backup_env(monkeypatch: pytest.MonkeyPatch, recipient: str | None) -> None:
    monkeypatch.setenv("DATABASE_URL", APP_URL)
    if recipient is None:
        monkeypatch.delenv("RADAR_BACKUP_AGE_RECIPIENT", raising=False)
    else:
        monkeypatch.setenv("RADAR_BACKUP_AGE_RECIPIENT", recipient)


def _run_backup_returning(
    monkeypatch: pytest.MonkeyPatch, outcome: BackupResult | Exception
) -> None:
    def run(*_a: object, **_k: object) -> BackupResult:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(backup_cli, "run_backup", run)


def _run_restore_returning(
    monkeypatch: pytest.MonkeyPatch, outcome: RestoreResult | Exception
) -> None:
    def run(*_a: object, **_k: object) -> RestoreResult:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(backup_cli, "run_restore", run)


def test_help_lists_both_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "backup" in result.output
    assert "restore" in result.output


# --- radar backup -----------------------------------------------------------------------------


def test_backup_without_recipient_fails_and_never_connects(
    monkeypatch: pytest.MonkeyPatch, built: list[str]
) -> None:
    _backup_env(monkeypatch, None)

    result = runner.invoke(cli.app, ["backup"])

    assert result.exit_code == 1
    assert "RADAR_BACKUP_AGE_RECIPIENT" in result.stderr
    assert "不會改成不加密" in result.stderr
    assert built == []


def test_backup_with_a_private_key_as_recipient_fails_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, built: list[str]
) -> None:
    secret = "AGE-SECRET-KEY-1" + "Q" * 58
    _backup_env(monkeypatch, secret)

    result = runner.invoke(cli.app, ["backup"])

    assert result.exit_code == 1
    assert "RADAR_BACKUP_AGE_RECIPIENT" in result.stderr
    assert secret not in result.output
    assert built == []


def test_backup_success_prints_key_and_counts(
    monkeypatch: pytest.MonkeyPatch, built: list[str]
) -> None:
    _backup_env(monkeypatch, RECIPIENT)
    key = "backup/20261118T120000Z-full.radar-backup.age"
    _run_backup_returning(monkeypatch, BackupResult(object_key=key, manifest=MANIFEST))

    result = runner.invoke(cli.app, ["backup"])

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert f"已備份：{key}" in lines
    assert "算數的論文：3" in lines
    assert "成員資料表 member：2 列" in lines
    assert "s3cret" not in result.output


def test_backup_tool_error_exits_one_without_connection_details(
    monkeypatch: pytest.MonkeyPatch, built: list[str]
) -> None:
    _backup_env(monkeypatch, RECIPIENT)
    _run_backup_returning(monkeypatch, BackupToolError("pg_dump", "失敗（退出碼 1）"))

    with capture_logs() as logs:
        result = runner.invoke(cli.app, ["backup"])

    assert result.exit_code == 1
    assert "備份失敗：pg_dump 失敗（退出碼 1）" in result.stderr
    assert "s3cret" not in result.output
    assert "db.example" not in result.output
    # 審查 T9：日誌只記錯誤類別。
    assert logs == [{"event": "備份失敗", "error_type": "BackupToolError", "log_level": "error"}]


def test_backup_unexpected_error_shows_only_its_type(
    monkeypatch: pytest.MonkeyPatch, built: list[str]
) -> None:
    _backup_env(monkeypatch, RECIPIENT)
    _run_backup_returning(monkeypatch, ConnectionError(f"could not connect to {APP_URL}"))

    with capture_logs() as logs:
        result = runner.invoke(cli.app, ["backup"])

    assert result.exit_code == 1
    assert "ConnectionError" in result.stderr
    assert "s3cret" not in result.output
    assert "db.example" not in result.output
    assert "s3cret" not in repr(logs)
    assert "db.example" not in repr(logs)


# --- radar restore ----------------------------------------------------------------------------


@pytest.fixture
def backup_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RADAR_MIGRATION_DATABASE_URL", OWNER_URL)
    path = tmp_path / "decrypted.radar-backup"
    path.write_bytes(b"made-up")
    return path


def test_restore_missing_file_fails_before_connecting(backup_file: Path, built: list[str]) -> None:
    result = runner.invoke(cli.app, ["restore", str(backup_file.with_name("nope"))])

    assert result.exit_code == 1
    assert "找不到備份檔" in result.stderr
    assert built == []


def test_restore_refused_lists_every_reason(
    monkeypatch: pytest.MonkeyPatch, backup_file: Path, built: list[str]
) -> None:
    problems = ["目標資料庫不是空的：public.paper", "目標的 PostgreSQL 是 18.0，比備份時的 18.1 舊"]
    _run_restore_returning(monkeypatch, RestoreRefusedError(problems))

    result = runner.invoke(cli.app, ["restore", str(backup_file)])

    assert result.exit_code == 1
    lines = result.stderr.splitlines()
    assert "沒有還原：" in lines
    assert f"- {problems[0]}" in lines
    assert f"- {problems[1]}" in lines
    assert "s3cret" not in result.output  # 審查 T9：不印 RADAR_MIGRATION_DATABASE_URL


def test_restore_count_mismatch_says_the_data_is_kept(
    monkeypatch: pytest.MonkeyPatch, backup_file: Path, built: list[str]
) -> None:
    _run_restore_returning(
        monkeypatch, RestoreCountMismatchError(["算數的論文：清單 3，還原後 2，差 -1"])
    )

    result = runner.invoke(cli.app, ["restore", str(backup_file)])

    assert result.exit_code == 1
    assert "還原的資料留著，沒有刪除" in result.stderr
    assert "- 算數的論文：清單 3，還原後 2，差 -1" in result.stderr.splitlines()
    assert "s3cret" not in result.output  # 審查 T9


def test_restore_from_stdin_reads_the_pipe(
    monkeypatch: pytest.MonkeyPatch, backup_file: Path, built: list[str]
) -> None:
    seen: list[bytes] = []

    def run(_database: object, source: Any, **_k: object) -> RestoreResult:
        seen.append(source.read())
        return RestoreResult(
            manifest=MANIFEST, counts=MANIFEST.counts, member_table_rows={"member": 2}
        )

    monkeypatch.setattr(backup_cli, "run_restore", run)

    result = runner.invoke(cli.app, ["restore", "-"], input=b"piped-bytes")

    assert result.exit_code == 0
    assert seen == [b"piped-bytes"]
    assert "已還原，筆數和清單一致" in result.stdout.splitlines()
    assert "s3cret" not in result.output


# --- 審查第 1 輪 ------------------------------------------------------------------------------


def test_restore_unexpected_error_shows_only_its_type(
    monkeypatch: pytest.MonkeyPatch, backup_file: Path, built: list[str]
) -> None:
    """審查 T9：例外訊息帶連線字串時，輸出與日誌都只有類別名稱。"""
    _run_restore_returning(monkeypatch, ConnectionError(f"could not connect to {OWNER_URL}"))

    with capture_logs() as logs:
        result = runner.invoke(cli.app, ["restore", str(backup_file)])

    assert result.exit_code == 1
    assert "ConnectionError" in result.stderr
    for secret in ("s3cret", "db.example", "radar_owner"):
        assert secret not in result.output
        assert secret not in repr(logs)
    assert logs == [{"event": "還原失敗", "error_type": "ConnectionError", "log_level": "error"}]


def test_restore_verification_error_says_the_data_was_restored(
    monkeypatch: pytest.MonkeyPatch, backup_file: Path, built: list[str]
) -> None:
    """審查 S6：已經 COMMIT 之後重數失敗，不能印成「還原失敗」。"""
    _run_restore_returning(monkeypatch, RestoreVerificationError("OperationalError"))

    result = runner.invoke(cli.app, ["restore", str(backup_file)])

    assert result.exit_code == 1
    assert result.stderr.startswith("資料已還原，但還原後重數筆數時出錯（OperationalError）")
    assert "還原的資料留著沒有刪除" in result.stderr
    assert "還原失敗" not in result.stderr
    assert "s3cret" not in result.output


@pytest.mark.parametrize(
    ("argument", "expected"),
    [([], None), (["--expect-mode", "shared-only"], BackupMode.SHARED_ONLY)],
)
def test_restore_passes_the_expected_mode(
    monkeypatch: pytest.MonkeyPatch,
    backup_file: Path,
    built: list[str],
    argument: list[str],
    expected: BackupMode | None,
) -> None:
    seen: list[object] = []

    def run(*_a: object, **kwargs: object) -> RestoreResult:
        seen.append(kwargs["expect_mode"])
        return RestoreResult(manifest=MANIFEST, counts=MANIFEST.counts, member_table_rows={})

    monkeypatch.setattr(backup_cli, "run_restore", run)

    result = runner.invoke(cli.app, ["restore", *argument, str(backup_file)])

    assert result.exit_code == 0
    assert seen == [expected]
    assert "build_known_revisions" in built


def test_restore_rejects_an_unknown_expected_mode(backup_file: Path, built: list[str]) -> None:
    result = runner.invoke(cli.app, ["restore", "--expect-mode", "everything", str(backup_file)])

    assert result.exit_code == 2
    assert built == []
