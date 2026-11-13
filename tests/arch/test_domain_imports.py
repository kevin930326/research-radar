"""domain 只依賴標準函式庫、Pydantic 與 domain 自己（ADR-0001）。"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = ROOT / "radar" / "domain"
ALLOWED_THIRD_PARTY = {"pydantic", "pydantic_core", "annotated_types", "typing_extensions"}


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def test_domain_imports_only_stdlib_pydantic_and_domain() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(DOMAIN.rglob("*.py")):
        bad = sorted(
            name
            for name in _top_level_imports(path)
            if not (
                name.split(".")[0] in sys.stdlib_module_names
                or name.split(".")[0] in ALLOWED_THIRD_PARTY
                or name == "radar.domain"
                or name.startswith("radar.domain.")
            )
        )
        if bad:
            offenders[str(path.relative_to(ROOT))] = bad

    assert offenders == {}
