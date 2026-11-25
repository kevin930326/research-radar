"""`demo-openapi.json`（Story 3.7、ADR-0010 第 4 點、ADR-0061）：repo 裡那份和程式產生的一致；
兩邊都有的形狀和成員版 `openapi.json` 同名同形；每個物件都是「多一個欄位就報錯」。

重新產生：`uv run --no-sync radar export-demo-schema`。
"""

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from radar.domain.demo_export import DEMO_FILE_MODELS, build_demo_openapi, render_demo_openapi
from radar.entrypoints.cli import app

ROOT = Path(__file__).resolve().parents[2]
DEMO_OPENAPI = ROOT / "demo-openapi.json"
MEMBER_OPENAPI = ROOT / "openapi.json"


def test_repo_copy_matches_the_code() -> None:
    assert DEMO_OPENAPI.read_text(encoding="utf-8") == render_demo_openapi(), (
        "demo-openapi.json 和程式不一致：跑 `uv run --no-sync radar export-demo-schema` 重新產生"
    )


def test_every_data_file_is_a_read_only_endpoint() -> None:
    document = build_demo_openapi()
    refs = {
        path: operations["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        for path, operations in document["paths"].items()
    }

    assert list(document["paths"]) == [
        "/manifest.json",
        "/{export_id}/taxonomy.json",
        "/{export_id}/papers-index.json",
        "/{export_id}/titles.json",
        "/{export_id}/cube.json",
        "/{export_id}/papers/{shard}.json",
    ]
    assert all(set(operations) == {"get"} for operations in document["paths"].values())
    assert sorted(refs.values()) == sorted(
        f"#/components/schemas/{model.__name__}" for _, model in DEMO_FILE_MODELS
    )


def test_shared_shapes_are_the_same_as_the_member_api() -> None:
    demo = build_demo_openapi()["components"]["schemas"]
    member = json.loads(MEMBER_OPENAPI.read_text(encoding="utf-8"))["components"]["schemas"]

    shared = sorted(name for name in demo if not name.startswith("Demo"))
    assert shared  # 快照、來歷、顯示名稱、原文摘要、全文連結、構面與假設
    for name in shared:
        assert name in member, name
        assert demo[name] == member[name], name


def _objects(schema: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        if schema.get("type") == "object" and "properties" in schema:
            found.append(schema)
        for value in schema.values():
            found.extend(_objects(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_objects(value))
    return found


def test_every_object_forbids_extra_fields() -> None:
    schemas = build_demo_openapi()["components"]["schemas"]
    objects = _objects(schemas)

    assert objects
    assert all(item.get("additionalProperties") is False for item in objects)


def test_cli_writes_the_same_document(tmp_path: Path) -> None:
    output = tmp_path / "demo-openapi.json"

    result = CliRunner().invoke(app, ["export-demo-schema", "--output", str(output)])

    assert result.exit_code == 0, result.output
    assert output.read_text(encoding="utf-8") == render_demo_openapi()
