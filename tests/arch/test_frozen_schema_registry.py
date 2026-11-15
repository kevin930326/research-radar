"""凍結的 schema 登記表（ADR-0029、2.1 第五次確認審查第 2 點）的架構規則：

- `radar/domain/llm.py` 的 `_FROZEN_SCHEMAS`（登記表）、`_freeze_schemas`（產生登記表）、
  `_render_schema`（產生凍結字串）只能在 `llm.py` 裡碰：`radar/` 其他地方不能用名稱、屬性、
  `from ... import` 或字串（`getattr` 的第二個參數）引用它們。要凍結字串或它的雜湊，一律經公開的
  `frozen_schema_json`。
- 登記表在執行期是唯讀的 `MappingProxyType`（不能增刪項目）。

寫法照 `tests/arch/test_cloud_registry.py` 的放行登記處規則。掃描函式另外用 tmp 裡的假檔證明抓得到。
"""

import ast
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from radar.domain import llm

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
LLM_MODULE = RADAR / "domain" / "llm.py"
# 拆開寫：這個檔不在掃描範圍，但寫法和 test_cloud_registry.py 一致，避免日後擴大範圍時誤判。
PRIVATE_NAMES = frozenset({"_FROZEN" + "_SCHEMAS", "_freeze" + "_schemas", "_render" + "_schema"})


def private_schema_violations(path: Path, *, base: Path) -> list[str]:
    """這個檔裡引用凍結 schema 登記表私有狀態的地方（「相對路徑:行號 說明」）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    name = path.relative_to(base).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in PRIVATE_NAMES:
            found.append(f"{name}:{node.lineno} 引用私有狀態 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in PRIVATE_NAMES:
            found.append(f"{name}:{node.lineno} 引用私有狀態 .{node.attr}")
        elif isinstance(node, ast.ImportFrom):
            found.extend(
                f"{name}:{node.lineno} import 私有狀態 {alias.name}"
                for alias in node.names
                if alias.name in PRIVATE_NAMES
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
            PRIVATE_NAMES
        ):
            found.append(f"{name}:{node.lineno} 定義私有狀態 {node.name}")
        elif isinstance(node, ast.Constant) and node.value in PRIVATE_NAMES:
            found.append(f"{name}:{node.lineno} 用字串取私有狀態 {node.value}")
    return found


def test_only_llm_module_touches_the_frozen_schema_registry() -> None:
    offenders = [
        hit
        for path in sorted(RADAR.rglob("*.py"))
        if path != LLM_MODULE
        for hit in private_schema_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_llm_module_is_where_the_registry_lives() -> None:
    # 真的用到：代表掃描的名稱對得上實際寫法。
    hits = private_schema_violations(LLM_MODULE, base=ROOT)

    assert any("_FROZEN_SCHEMAS" in hit for hit in hits)
    assert any("_freeze_schemas" in hit for hit in hits)
    assert any("_render_schema" in hit for hit in hits)


def test_registry_is_read_only_at_runtime() -> None:
    registry: Any = getattr(llm, "_FROZEN" + "_SCHEMAS")

    with pytest.raises(TypeError):
        registry[0] = (object, "{}")
    assert type(registry) is MappingProxyType


def test_private_schema_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "gateway.py"
    sample.write_text(
        "from radar.domain import llm\n"
        "from radar.domain.llm import _FROZEN_SCHEMAS, _render_schema\n"
        "a = llm._FROZEN_SCHEMAS\n"
        "b = getattr(llm, '_freeze_schemas')\n"
        "c = llm._render_schema(Model)\n"
        "def _freeze_schemas(module):\n    return {}\n",
        encoding="utf-8",
    )

    assert sorted(private_schema_violations(sample, base=tmp_path)) == sorted(
        [
            "gateway.py:2 import 私有狀態 _FROZEN_SCHEMAS",
            "gateway.py:2 import 私有狀態 _render_schema",
            "gateway.py:3 引用私有狀態 ._FROZEN_SCHEMAS",
            "gateway.py:4 用字串取私有狀態 _freeze_schemas",
            "gateway.py:5 引用私有狀態 ._render_schema",
            "gateway.py:6 定義私有狀態 _freeze_schemas",
        ]
    )


def test_private_schema_scanner_ignores_similar_names(tmp_path: Path) -> None:
    sample = tmp_path / "ok.py"
    sample.write_text(
        "from radar.domain.llm import frozen_schema_json\n"
        "a = frozen_schema_json(Model)\n"
        "b = '_FROZEN_SCHEMAS_v2'\n"
        "_render_schemas = 1\n",
        encoding="utf-8",
    )

    assert private_schema_violations(sample, base=tmp_path) == []
