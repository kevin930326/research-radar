"""分類表內容雜湊的登記（`radar/domain/taxonomy_hashes.py`）的架構規則（Story 2.5、ADR-0033）：

- 那個模組只能寫字面值（和 `prompt_hashes.py` 同一套規則），登記不會在執行期由別的資料組出來。
- `radar/` 裡只有 `domain/taxonomy.py`（`taxonomy_registry_problems`）提到 `taxonomy_hashes`；
  沒有程式能改登記表，設定檔也只能經它核對。

掃描函式另外用 tmp 裡的假檔證明抓得到。
"""

import ast
from pathlib import Path

from tests.arch.test_prompt_hashes import literal_module_violations

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
HASHES = RADAR / "domain" / "taxonomy_hashes.py"
TAXONOMY = RADAR / "domain" / "taxonomy.py"
MODULE_NAME = "taxonomy_hashes"


def reference_violations(path: Path, *, base: Path) -> list[str]:
    """這個檔裡提到登記模組的地方（import、屬性、名字、字串）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = path.relative_to(base).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                f"{where}:{node.lineno} import {alias.name}"
                for alias in node.names
                if MODULE_NAME in alias.name.split(".")
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if MODULE_NAME in module.split(".") or any(a.name == MODULE_NAME for a in node.names):
                found.append(f"{where}:{node.lineno} import {MODULE_NAME}")
        elif isinstance(node, ast.Name) and node.id == MODULE_NAME:
            found.append(f"{where}:{node.lineno} 用 {MODULE_NAME}")
        elif isinstance(node, ast.Attribute) and node.attr == MODULE_NAME:
            found.append(f"{where}:{node.lineno} 用 .{MODULE_NAME}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and MODULE_NAME in node.value.split(".")
        ):
            found.append(f"{where}:{node.lineno} 用字串 {node.value!r}")
    return found


def test_taxonomy_hashes_module_is_literals_only() -> None:
    assert literal_module_violations(HASHES, base=ROOT) == []


def test_only_the_taxonomy_module_mentions_the_registry() -> None:
    offenders = [
        hit
        for path in sorted(RADAR.rglob("*.py"))
        if "__pycache__" not in path.parts and path not in (TAXONOMY, HASHES)
        for hit in reference_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_taxonomy_module_really_uses_it() -> None:
    # 真的用到：代表掃描的名稱對得上實際寫法。
    assert reference_violations(TAXONOMY, base=ROOT) != []


def test_literal_scanner_catches_a_computed_registry(tmp_path: Path) -> None:
    sample = tmp_path / "taxonomy_hashes.py"
    sample.write_text(
        '"""說明"""\n'
        "from radar.domain.fingerprints import taxonomy_content_sha256\n"
        "TAXONOMY_CONTENT_HASHES: dict[int, str] = {1: taxonomy_content_sha256(x)}\n",
        encoding="utf-8",
    )

    assert literal_module_violations(sample, base=tmp_path) == [
        "taxonomy_hashes.py:2 不允許的寫法（ImportFrom）",
        "taxonomy_hashes.py:3 TAXONOMY_CONTENT_HASHES 的值不是字面值",
    ]


def test_reference_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "service.py"
    sample.write_text(
        "import radar.domain.taxonomy_hashes\n"
        "from radar.domain import taxonomy_hashes\n"
        "from radar.domain.taxonomy_hashes import TAXONOMY_CONTENT_HASHES\n"
        "a = domain.taxonomy_hashes\n"
        "b = importlib.import_module('radar.domain.taxonomy_hashes')\n"
        "c = 'taxonomy_hashes_dir is fine'\n",
        encoding="utf-8",
    )

    assert sorted(reference_violations(sample, base=tmp_path)) == sorted(
        [
            "service.py:1 import radar.domain.taxonomy_hashes",
            "service.py:2 import taxonomy_hashes",
            "service.py:3 import taxonomy_hashes",
            "service.py:4 用 .taxonomy_hashes",
            "service.py:5 用字串 'radar.domain.taxonomy_hashes'",
        ]
    )
