"""`radar enqueue <工作>` 的輸出與退出碼（不真的連資料庫）。"""

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest
from typer.testing import CliRunner

import radar.entrypoints.cli as cli
from radar.adapters.fake import FakeJobQueue
from radar.domain.jobs import Enqueue, JobSpec

runner = CliRunner()
APP_URL = "postgresql://radar_app:s3cret-APP@db.example:5432/radar"


def _fake_enqueuer(queue: FakeJobQueue, calls: list[dict[str, Any]]) -> Any:
    @contextmanager
    def build(_settings: object, **kwargs: Any) -> Iterator[Enqueue]:
        calls.append(kwargs)
        yield queue.enqueue

    return build


def test_help_lists_enqueue() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "enqueue" in result.output


def test_unknown_job_exits_2_lists_jobs_and_does_not_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", APP_URL)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "build_job_enqueuer", _fake_enqueuer(FakeJobQueue(), calls))

    result = runner.invoke(cli.app, ["enqueue", "nope"])

    assert result.exit_code == 2
    assert "nope" in result.stderr
    assert "可用的工作：sweep" in result.stderr
    assert calls == []


def test_enqueue_sweep_uses_cli_pool_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", APP_URL)
    monkeypatch.delenv("RADAR_CLI_DATABASE_POOL_SIZE", raising=False)
    queue = FakeJobQueue()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "build_job_enqueuer", _fake_enqueuer(queue, calls))

    result = runner.invoke(cli.app, ["enqueue", "sweep"])

    assert result.exit_code == 0, result.output
    assert "已排入 sweep" in result.stdout
    assert calls == [{"max_connections": 2}]
    assert [item.job.name for item in queue.queued] == ["sweep"]


def test_already_queued_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", APP_URL)
    queue = FakeJobQueue()
    monkeypatch.setattr(cli, "build_job_enqueuer", _fake_enqueuer(queue, []))

    runner.invoke(cli.app, ["enqueue", "sweep"])
    result = runner.invoke(cli.app, ["enqueue", "sweep"])

    assert result.exit_code == 0, result.output
    assert "已經在排隊" in result.stdout
    assert len(queue.queued) == 1


def test_connection_error_shows_only_error_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", APP_URL)

    def broken(_job: JobSpec, _ids: Mapping[str, uuid.UUID]) -> bool:
        raise ConnectionError(f"could not connect to {APP_URL}")

    @contextmanager
    def build(_settings: object, **_kwargs: Any) -> Iterator[Enqueue]:
        yield broken

    monkeypatch.setattr(cli, "build_job_enqueuer", build)

    result = runner.invoke(cli.app, ["enqueue", "sweep"])

    assert result.exit_code == 1
    assert "ConnectionError" in result.stderr
    assert '"error_type": "ConnectionError"' in result.stdout
    assert "s3cret-APP" not in result.output
    assert "db.example" not in result.output


def test_missing_database_url_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "build_job_enqueuer", _fake_enqueuer(FakeJobQueue(), calls))

    result = runner.invoke(cli.app, ["enqueue", "sweep"])

    assert result.exit_code == 1
    assert "DATABASE_URL" in result.stderr
    assert calls == []
