"""只有 radar/config.py 讀環境變數（AD-18）。"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "radar"
ALLOWED = PACKAGE / "config.py"
ENV_NAMES = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}


def _env_accesses(path: Path) -> list[str]:
    """找出 `os.environ`、`os.getenv` 這類存取，以及 `from os import environ`。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ENV_NAMES:
            found.append(f"{path.name}:{node.lineno} .{node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module in {"os", "posix"}:
            found.extend(
                f"{path.name}:{node.lineno} from {node.module} import {alias.name}"
                for alias in node.names
                if alias.name in ENV_NAMES
            )
    return found


def test_only_config_reads_environment() -> None:
    offenders = {
        str(path.relative_to(ROOT)): hits
        for path in sorted(PACKAGE.rglob("*.py"))
        if path != ALLOWED and (hits := _env_accesses(path))
    }

    assert offenders == {}


def test_config_is_the_reader() -> None:
    # config.py 真的有讀環境變數，代表掃描方式對得上實際寫法。
    assert _env_accesses(ALLOWED) != []


def test_scanner_detects_every_form(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import os\n"
        "from os import getenv\n"
        "x = os.environ['A']\n"
        "y = os.getenv('B')\n"
        "z = os.environ.get('C')\n",
        encoding="utf-8",
    )

    assert len(_env_accesses(sample)) == 4
