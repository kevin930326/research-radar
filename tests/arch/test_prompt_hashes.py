"""prompt 雜湊登記（`radar/domain/prompt_hashes.py`）的架構規則（ADR-0030「依此更正」）：

- 那個模組只能寫字面值：最上層只有模組 docstring 與「名字: 型別 = 字面值」的指定，不 import、
  不呼叫函式（`ast.literal_eval` 讀得出值），所以登記不會在執行期由別的資料組出來。
- `radar/` 裡只有 `domain/policy.py`（放行時核對）與 `services/prompt_loader.py`（目前版本、
  schema 雜湊）提到 `prompt_hashes`；沒有程式能改登記表。
- 模板的放行入口 `clear_prompt_file` 只在 `domain/policy.py` 定義、只由 `services/prompt_loader.py`
  呼叫。

掃描函式另外用 tmp 裡的假檔證明抓得到。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
HASHES = RADAR / "domain" / "prompt_hashes.py"
POLICY = RADAR / "domain" / "policy.py"
PROMPT_LOADER = RADAR / "services" / "prompt_loader.py"
MODULE_NAME = "prompt_hashes"
ENTRY = "clear_prompt_file"


def _is_literal(node: ast.AST) -> bool:
    try:
        ast.literal_eval(node)
    except ValueError, TypeError, SyntaxError, MemoryError, RecursionError:
        return False
    return True


def literal_module_violations(path: Path, *, base: Path) -> list[str]:
    """登記模組裡不是「字面值指定」的地方（「相對路徑:行號 說明」）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = path.relative_to(base).as_posix()
    found: list[str] = []
    for index, statement in enumerate(tree.body):
        if (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
        ):
            if not _is_literal(statement.value):
                found.append(f"{where}:{statement.lineno} {statement.target.id} 的值不是字面值")
            continue
        found.append(f"{where}:{statement.lineno} 不允許的寫法（{type(statement).__name__}）")
    return found


def hashes_reference_violations(path: Path, *, base: Path) -> list[str]:
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


def entry_call_violations(path: Path, *, base: Path) -> list[str]:
    """這個檔裡呼叫、定義或 import 模板放行入口的地方。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = path.relative_to(base).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == ENTRY:
            found.append(f"{where}:{node.lineno} 用 {ENTRY}")
        elif isinstance(node, ast.Attribute) and node.attr == ENTRY:
            found.append(f"{where}:{node.lineno} 用 .{ENTRY}")
        elif isinstance(node, ast.ImportFrom) and any(a.name == ENTRY for a in node.names):
            found.append(f"{where}:{node.lineno} import {ENTRY}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == ENTRY:
            found.append(f"{where}:{node.lineno} 定義 {ENTRY}")
        elif isinstance(node, ast.Constant) and node.value == ENTRY:
            found.append(f"{where}:{node.lineno} 用字串 {ENTRY!r}")
    return found


def test_prompt_hashes_module_is_literals_only() -> None:
    assert literal_module_violations(HASHES, base=ROOT) == []


def test_only_policy_and_loader_mention_prompt_hashes() -> None:
    offenders = [
        hit
        for path in sorted(RADAR.rglob("*.py"))
        if path not in (POLICY, PROMPT_LOADER, HASHES)
        for hit in hashes_reference_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_only_the_loader_uses_the_template_entry() -> None:
    offenders = [
        hit
        for path in sorted(RADAR.rglob("*.py"))
        if path not in (POLICY, PROMPT_LOADER)
        for hit in entry_call_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_policy_and_loader_really_use_them() -> None:
    # 真的用到：代表掃描的名稱對得上實際寫法。
    assert hashes_reference_violations(POLICY, base=ROOT) != []
    assert hashes_reference_violations(PROMPT_LOADER, base=ROOT) != []
    assert entry_call_violations(PROMPT_LOADER, base=ROOT) != []


def test_literal_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "prompt_hashes.py"
    sample.write_text(
        '"""說明"""\n'
        "import json\n"
        "A: dict[str, int] = {}\n"
        "B: dict[str, int] = dict(x=1)\n"
        "C = {}\n"
        "D: dict[str, str] = {'x': json.dumps(1)}\n"
        "def f():\n    pass\n",
        encoding="utf-8",
    )

    assert literal_module_violations(sample, base=tmp_path) == [
        "prompt_hashes.py:2 不允許的寫法（Import）",
        "prompt_hashes.py:4 B 的值不是字面值",
        "prompt_hashes.py:5 不允許的寫法（Assign）",
        "prompt_hashes.py:6 D 的值不是字面值",
        "prompt_hashes.py:7 不允許的寫法（FunctionDef）",
    ]


def test_reference_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "service.py"
    sample.write_text(
        "import radar.domain.prompt_hashes\n"
        "from radar.domain import prompt_hashes\n"
        "from radar.domain.prompt_hashes import PROMPT_FILE_HASHES\n"
        "a = domain.prompt_hashes\n"
        "b = importlib.import_module('radar.domain.prompt_hashes')\n"
        "c = 'prompt_hashes_dir is fine'\n",
        encoding="utf-8",
    )

    assert sorted(hashes_reference_violations(sample, base=tmp_path)) == sorted(
        [
            "service.py:1 import radar.domain.prompt_hashes",
            "service.py:2 import prompt_hashes",
            "service.py:3 import prompt_hashes",
            "service.py:4 用 .prompt_hashes",
            "service.py:5 用字串 'radar.domain.prompt_hashes'",
        ]
    )


def test_entry_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "gateway.py"
    sample.write_text(
        "from radar.domain.policy import clear_prompt_file\n"
        "a = policy.clear_prompt_file('t', 1, b'')\n"
        "b = getattr(policy, 'clear_prompt_file')\n"
        "def clear_prompt_file():\n    pass\n"
        "c = clear_for_cloud(text, source, None, p)\n",
        encoding="utf-8",
    )

    assert sorted(entry_call_violations(sample, base=tmp_path)) == sorted(
        [
            "gateway.py:1 import clear_prompt_file",
            "gateway.py:2 用 .clear_prompt_file",
            "gateway.py:3 用字串 'clear_prompt_file'",
            "gateway.py:4 定義 clear_prompt_file",
        ]
    )
