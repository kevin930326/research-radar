"""`radar export-schema`（Story 3.4、AD-21、ADR-0047）：用子行程產生 openapi.json 再寫檔。"""

import importlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from radar.entrypoints.api.openapi import render_openapi
from radar.entrypoints.cli import app

# 套件 __init__ 把同名的函式 import 進來了，用模組全名拿模組本身。
export_schema_module = importlib.import_module("radar.entrypoints.cli.export_schema")

runner = CliRunner()


def test_export_schema_writes_the_openapi_document(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"

    result = runner.invoke(app, ["export-schema", "--output", str(output)])

    assert result.exit_code == 0, result.output
    assert output.read_text(encoding="utf-8") == render_openapi()


def test_export_schema_fails_without_writing_when_generation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "openapi.json"
    monkeypatch.setattr(export_schema_module, "OPENAPI_MODULE", "radar.no_such_module")

    result = runner.invoke(app, ["export-schema", "--output", str(output)])

    assert result.exit_code == 1
    assert not output.exists()


def test_export_schema_refuses_output_that_is_not_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "openapi.json"
    # 標準函式庫的 `this` 模組：退出碼 0，但印的是一段文字，不是 JSON。
    monkeypatch.setattr(export_schema_module, "OPENAPI_MODULE", "this")

    result = runner.invoke(app, ["export-schema", "--output", str(output)])

    assert result.exit_code == 1
    assert not output.exists()
