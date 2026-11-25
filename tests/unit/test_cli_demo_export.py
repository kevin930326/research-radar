"""`radar export-demo`（Story 3.7）：開關、輸出資料夾、失敗時的退出碼與印出的原因。

資料庫與 service 一律換成替身（不連任何資料庫）；真的匯出在 `tests/integration/test_demo_export.py`
直接呼叫 service（對測試用的資料庫）。
"""

import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

import radar.entrypoints.cli.demo_export as cli_module
from radar.domain.demo_export import DemoExportBusyError, DemoExportFailure, DemoExportRun
from radar.domain.demo_export import DemoExportStatus as Status
from radar.entrypoints.cli import app
from radar.services.demo_export import (
    DemoExportCheckError,
    DemoExportError,
    DemoExportResult,
    NoPublishedSnapshotError,
)

ROOT = Path(__file__).resolve().parents[2]
# 不會真的連（`build_database` 換掉了）；埠故意寫一個沒有服務的。
FAKE_DATABASE_URL = "postgresql://nobody@127.0.0.1:9/none"
SECRET_IN_MESSAGE = "postgresql://kevin:hunter2@db.example.org/radar"


def _invoke(output: Path | None, **extra: str) -> Result:
    env = {"DATABASE_URL": FAKE_DATABASE_URL, "RADAR_CONFIG_DIR": str(ROOT / "config"), **extra}
    if output is not None:
        env["RADAR_DEMO_EXPORT_DIR"] = str(output)
    else:
        env["RADAR_DEMO_EXPORT_DIR"] = ""
    return CliRunner().invoke(app, ["export-demo"], env=env)


def _no_database(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("不該連資料庫")


def test_publish_switch_is_refused_before_touching_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "build_database", _no_database)
    output = tmp_path / "out"

    result = _invoke(output, RADAR_DEMO_PUBLISH="true")

    assert result.exit_code == 1
    assert "RADAR_DEMO_PUBLISH 開著" in result.output
    assert not output.exists()


def test_publish_switch_is_off_by_default() -> None:
    from radar.domain.settings import Settings

    assert Settings.model_fields["demo_publish"].default is False


def test_missing_output_dir_is_refused_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 輸出資料夾只看 RADAR_DEMO_EXPORT_DIR（和 worker 同一個設定，F11）；沒設就不匯出。
    monkeypatch.setattr(cli_module, "build_database", _no_database)

    result = _invoke(None)

    assert result.exit_code == 1
    assert "沒有設定 RADAR_DEMO_EXPORT_DIR" in result.output


def _fake_service(
    monkeypatch: pytest.MonkeyPatch, *, queue_error: Exception | None, run_error: Exception | None
) -> list[Path | None]:
    """把資料庫、排入與匯出換成替身；回傳匯出收到的輸出資料夾。"""
    seen: list[Path | None] = []
    run = DemoExportRun(
        id=uuid.UUID(int=7),
        snapshot_id=uuid.UUID(int=8),
        snapshot_number=2,
        sequence=1,
        status=Status.QUEUED,
    )

    def queue(*_args: object) -> DemoExportRun:
        if queue_error is not None:
            raise queue_error
        return run

    def export(*args: object, **_kwargs: object) -> DemoExportResult:
        output = args[4]
        assert output is None or isinstance(output, Path)
        seen.append(output)
        if run_error is not None:
            raise run_error
        assert isinstance(output, Path)
        return DemoExportResult("2-1", 60, 34, output / "preview" / "manifest.json")

    monkeypatch.setattr(cli_module, "build_database", lambda *_a, **_k: object())
    monkeypatch.setattr(cli_module, "queue_demo_export", queue)
    monkeypatch.setattr(cli_module, "run_demo_export", export)
    return seen


def test_success_uses_the_configured_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_service(monkeypatch, queue_error=None, run_error=None)

    result = _invoke(tmp_path)

    assert result.exit_code == 0, result.output
    assert seen == [tmp_path]
    assert "示範站匯出完成：2-1" in result.output
    assert "論文：60 篇，其中有完整內容 34 篇" in result.output


def test_self_check_failure_prints_every_reason_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    problems = [
        "2-1/titles.json：出現金絲雀字串",
        "2-1/papers/000.json：不在 demo_full_paper 的論文 x 帶了完整內容",
    ]
    _fake_service(monkeypatch, queue_error=None, run_error=DemoExportCheckError(problems))

    result = _invoke(tmp_path)

    assert result.exit_code == 1
    assert "自我檢查沒過，沒有寫任何檔、manifest.json 沒換：" in result.output
    for problem in problems:
        assert f"- {problem}" in result.output
    assert not (tmp_path / "preview").exists()


@pytest.mark.parametrize(
    ("queue_error", "run_error", "expected"),
    [
        (NoPublishedSnapshotError("x"), None, "還沒有已發佈的快照"),
        (DemoExportBusyError("x"), None, "已經有一筆排隊中或執行中的匯出"),
        (None, DemoExportError(DemoExportFailure.SUPERSEDED, "較新"), "匯出失敗（superseded）"),
    ],
    ids=["no-snapshot", "busy", "known-failure"],
)
def test_known_failures_exit_1_with_a_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    queue_error: Exception | None,
    run_error: Exception | None,
    expected: str,
) -> None:
    _fake_service(monkeypatch, queue_error=queue_error, run_error=run_error)

    result = _invoke(tmp_path)

    assert result.exit_code == 1
    assert expected in result.output


def test_unknown_error_prints_only_the_class_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 例外訊息可能帶主機、帳號：只印類別名稱（和 worker 一樣，F17）。
    _fake_service(monkeypatch, queue_error=None, run_error=RuntimeError(SECRET_IN_MESSAGE))

    result = _invoke(tmp_path)

    assert result.exit_code == 1
    assert "匯出失敗（RuntimeError）" in result.output
    assert "hunter2" not in result.output
    assert "hunter2" not in capsys.readouterr().out
