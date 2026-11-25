"""`radar export-demo`（Story 3.7）：`RADAR_DEMO_PUBLISH` 開著時拒絕、不連資料庫、什麼都不寫。

真的匯出在 `tests/integration/test_demo_export.py` 直接呼叫 service（對測試用的資料庫）。
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

import radar.entrypoints.cli.demo_export as cli_module
from radar.entrypoints.cli import app

ROOT = Path(__file__).resolve().parents[2]


def test_publish_switch_is_refused_before_touching_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_database(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("不該連資料庫")

    monkeypatch.setattr(cli_module, "build_database", no_database)
    output = tmp_path / "out"

    result = CliRunner().invoke(
        app,
        ["export-demo", "--output-dir", str(output)],
        env={
            # 不會真的連（上面換掉了）；埠故意寫一個沒有服務的。
            "DATABASE_URL": "postgresql://nobody@127.0.0.1:9/none",
            "RADAR_CONFIG_DIR": str(ROOT / "config"),
            "RADAR_DEMO_PUBLISH": "true",
        },
    )

    assert result.exit_code == 1
    assert "RADAR_DEMO_PUBLISH 開著" in result.output
    assert not output.exists()


def test_publish_switch_is_off_by_default() -> None:
    from radar.domain.settings import Settings

    assert Settings.model_fields["demo_publish"].default is False
