"""`radar migrate` 的輸出：沒設定、資料庫功能不足、其他錯誤（不真的連資料庫）。"""

from collections.abc import Callable

import pytest
from typer.testing import CliRunner

import radar.entrypoints.cli as cli
from radar.domain.database_requirements import DatabaseRequirementsError
from radar.domain.settings import MigrationSettings

runner = CliRunner()
OWNER_URL = "postgresql://radar_owner:s3cret-OWNER@db.example:5432/radar"


def _runner_raising(error: Exception) -> Callable[..., Callable[[], frozenset[str]]]:
    def build(_settings: MigrationSettings) -> Callable[[], frozenset[str]]:
        def run() -> frozenset[str]:
            raise error

        return run

    return build


def test_help_lists_migrate() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "migrate" in result.output


def test_missing_setting_names_variable_and_does_not_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RADAR_MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://radar_app:s3cret-APP@db:5432/radar")
    built: list[None] = []
    monkeypatch.setattr(cli, "build_migration_runner", lambda *_a, **_k: built.append(None))

    result = runner.invoke(cli.app, ["migrate"])

    assert result.exit_code == 1
    assert "RADAR_MIGRATION_DATABASE_URL" in result.stderr
    assert "s3cret" not in result.output
    assert built == []


def test_requirement_problems_listed_on_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_MIGRATION_DATABASE_URL", OWNER_URL)
    problems = ["沒有安裝 pgvector", "沒有啟用 pg_trgm"]
    monkeypatch.setattr(
        cli, "build_migration_runner", _runner_raising(DatabaseRequirementsError(problems))
    )

    result = runner.invoke(cli.app, ["migrate"])

    assert result.exit_code == 1
    stderr_lines = result.stderr.splitlines()
    assert "- 沒有安裝 pgvector" in stderr_lines
    assert "- 沒有啟用 pg_trgm" in stderr_lines
    assert "s3cret-OWNER" not in result.output
    assert "db.example" not in result.output


def test_other_error_shows_only_error_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_MIGRATION_DATABASE_URL", OWNER_URL)
    error = ConnectionError(f"could not connect to {OWNER_URL}")
    monkeypatch.setattr(cli, "build_migration_runner", _runner_raising(error))

    result = runner.invoke(cli.app, ["migrate"])

    assert result.exit_code == 1
    assert "ConnectionError" in result.stderr
    assert "s3cret-OWNER" not in result.output
    assert "db.example" not in result.output
    assert '"error_type": "ConnectionError"' in result.stdout


def test_success_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_MIGRATION_DATABASE_URL", OWNER_URL)
    monkeypatch.setattr(
        cli, "build_migration_runner", lambda _settings: lambda: frozenset({"0001"})
    )

    result = runner.invoke(cli.app, ["migrate"])

    assert result.exit_code == 0, result.output
    assert '"revisions": ["0001"]' in result.stdout
    assert "s3cret-OWNER" not in result.output
