"""給 LLM 的結構化輸出 schema 只能寫死在 `radar/domain/llm_schemas.py`
（ADR-0029、Opus 第三次確認審查）：

- 那個模組只 import `pydantic`、`typing`、`enum`、`__future__`；最上層只有模組 docstring、import、
  類別定義。類別沒有 decorator、沒有方法；類別本體只有 docstring、欄位與 `model_config`。
- `Literal[...]`、`Field(...)`、`ConfigDict(...)`、欄位預設值、列舉成員的值、類別定義的關鍵字參數
  都必須是字面值（`ast.literal_eval` 讀得出來）；不能用 `Enum("X", ...)` 函式寫法；不能指定
  `__doc__`。
- `radar/` 裡（定義 `LlmRequest` 的 `domain/llm.py` 除外：它重建副本時原樣傳回已驗證的類別）
  傳給 `output_schema=` 的值，只能是從 `radar.domain.llm_schemas` import 進來的名字（或 `None`）。
  執行期 `LlmRequest` 另外再查一次類別真的在那個模組的最上層。

掃描函式另外用 tmp 裡的假檔證明抓得到。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
SCHEMA_PATH = RADAR / "domain" / "llm_schemas.py"
LLM_MODULE = RADAR / "domain" / "llm.py"
SCHEMA_MODULE = "radar.domain.llm_schemas"
ALLOWED_IMPORTS = frozenset({"pydantic", "typing", "enum", "__future__"})
LITERAL_CALLS = frozenset({"Field", "ConfigDict"})
ENUM_FACTORIES = frozenset({"Enum", "StrEnum", "IntEnum", "Flag", "IntFlag"})


def _is_literal(node: ast.AST) -> bool:
    try:
        ast.literal_eval(node)
    except ValueError, TypeError, SyntaxError, MemoryError, RecursionError:
        return False
    return True


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _check_expression(node: ast.AST, where: str, found: list[str]) -> None:
    """運算式（預設值、型別標註、類別的關鍵字參數）裡不能有執行期的值。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = _call_name(sub)
            if name in ENUM_FACTORIES:
                found.append(f"{where}:{sub.lineno} 用 {name}(...) 函式寫法")
            elif name not in LITERAL_CALLS:
                found.append(f"{where}:{sub.lineno} 呼叫 {name or '（運算式）'}(...)")
            elif not all(_is_literal(arg) for arg in sub.args) or not all(
                _is_literal(keyword.value) for keyword in sub.keywords
            ):
                found.append(f"{where}:{sub.lineno} {name}(...) 的參數不是字面值")
        elif isinstance(sub, ast.Subscript):
            target = sub.value
            is_literal_type = (isinstance(target, ast.Name) and target.id == "Literal") or (
                isinstance(target, ast.Attribute) and target.attr == "Literal"
            )
            if is_literal_type and not _is_literal(sub.slice):
                found.append(f"{where}:{sub.lineno} Literal[...] 裡不是字面值")
        elif isinstance(sub, ast.Attribute) and sub.attr == "__doc__":
            found.append(f"{where}:{sub.lineno} 用 __doc__")
        elif isinstance(sub, ast.Constant) and sub.value == "__doc__":
            found.append(f"{where}:{sub.lineno} 用字串取 __doc__")


def _check_value(node: ast.expr, where: str, found: list[str]) -> None:
    """欄位預設值、`model_config`、列舉成員的值：字面值，或參數全是字面值的 Field／ConfigDict。"""
    if _is_literal(node):
        return
    if isinstance(node, ast.Call) and _call_name(node) in LITERAL_CALLS:
        _check_expression(node, where, found)
        return
    if isinstance(node, ast.Call) and _call_name(node) in ENUM_FACTORIES:
        found.append(f"{where}:{node.lineno} 用 {_call_name(node)}(...) 函式寫法")
        return
    found.append(f"{where}:{node.lineno} 值不是字面值")


def _check_class(node: ast.ClassDef, where: str, found: list[str]) -> None:
    if node.decorator_list:
        found.append(f"{where}:{node.lineno} 類別 {node.name} 有 decorator")
    for base in node.bases:
        if not isinstance(base, (ast.Name, ast.Attribute)):
            found.append(f"{where}:{base.lineno} 類別 {node.name} 的父類別不是名字")
    for keyword in node.keywords:
        if not _is_literal(keyword.value):
            found.append(f"{where}:{keyword.value.lineno} 類別 {node.name} 的參數不是字面值")
    for index, statement in enumerate(node.body):
        if (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            if statement.target.id == "__doc__":
                found.append(f"{where}:{statement.lineno} 指定 __doc__")
                continue
            _check_expression(statement.annotation, where, found)
            if statement.value is not None:
                _check_value(statement.value, where, found)
            continue
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            if statement.targets[0].id == "__doc__":
                found.append(f"{where}:{statement.lineno} 指定 __doc__")
                continue
            _check_value(statement.value, where, found)
            continue
        found.append(
            f"{where}:{statement.lineno} 類別 {node.name} 裡有不允許的寫法"
            f"（{type(statement).__name__}）"
        )


def schema_module_violations(path: Path, *, base: Path) -> list[str]:
    """schema 模組裡不是寫死字面值的地方（「相對路徑:行號 說明」）。"""
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
        if isinstance(statement, ast.Import):
            found.extend(
                f"{where}:{statement.lineno} import {alias.name}"
                for alias in statement.names
                if alias.name.split(".")[0] not in ALLOWED_IMPORTS
            )
        elif isinstance(statement, ast.ImportFrom):
            module = statement.module or ""
            if statement.level or module.split(".")[0] not in ALLOWED_IMPORTS:
                found.append(f"{where}:{statement.lineno} import {module or '（相對）'}")
        elif isinstance(statement, ast.ClassDef):
            _check_class(statement, where, found)
        else:
            found.append(
                f"{where}:{statement.lineno} 最上層有不允許的寫法（{type(statement).__name__}）"
            )
    return found


def _schema_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """這個檔裡從 schema 模組 import 進來的名字，與指向 schema 模組本身的別名。"""
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == SCHEMA_MODULE:
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == "radar.domain":
            modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "llm_schemas"
            )
        elif isinstance(node, ast.Import):
            modules.update(
                alias.asname for alias in node.names if alias.name == SCHEMA_MODULE and alias.asname
            )
    return names, modules


def output_schema_source_violations(path: Path, *, base: Path) -> list[str]:
    """`output_schema=` 的值不是從 schema 模組來的地方。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = path.relative_to(base).as_posix()
    names, modules = _schema_names(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.keyword) and node.arg == "output_schema"):
            continue
        value = node.value
        ok = (
            (isinstance(value, ast.Constant) and value.value is None)
            or (isinstance(value, ast.Name) and value.id in names)
            or (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id in modules
            )
        )
        if not ok:
            found.append(f"{where}:{value.lineno} output_schema 不是 llm_schemas 的類別")
    return found


def test_schema_module_only_has_literal_schemas() -> None:
    assert schema_module_violations(SCHEMA_PATH, base=ROOT) == []


def test_output_schema_comes_from_the_schema_module() -> None:
    offenders = [
        hit
        for path in sorted(RADAR.rglob("*.py"))
        if path != LLM_MODULE
        for hit in output_schema_source_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_schema_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "llm_schemas.py"
    sample.write_text(
        '"""說明。"""\n'
        "import json\n"
        "from enum import Enum, StrEnum\n"
        "from typing import Literal\n"
        "from pydantic import BaseModel, ConfigDict, Field\n"
        "from radar.domain.policy import CloudPolicy\n"
        "NAMES = ('a', 'b')\n"
        "Category = Enum('Category', {'A': 'x'})\n"
        "class A(BaseModel):\n"
        "    model_config = ConfigDict(title=NAMES[0])\n"
        "    kind: Literal[NAMES]\n"
        "    concept: str = Field(description=NAMES[1])\n"
        "    other: str = NAMES[0]\n"
        "    def method(self) -> None: ...\n"
        "@dataclass\n"
        "class B(BaseModel):\n"
        "    __doc__ = 'x'\n"
        "class C(StrEnum):\n"
        "    X = Enum('Y', 'Z')\n"
        "A.__doc__ = 'x'\n"
        "def build(): ...\n",
        encoding="utf-8",
    )

    hits = schema_module_violations(sample, base=tmp_path)

    assert sorted(hits) == sorted(
        [
            "llm_schemas.py:2 import json",
            "llm_schemas.py:6 import radar.domain.policy",
            "llm_schemas.py:7 最上層有不允許的寫法（Assign）",
            "llm_schemas.py:8 最上層有不允許的寫法（Assign）",
            "llm_schemas.py:10 ConfigDict(...) 的參數不是字面值",
            "llm_schemas.py:11 Literal[...] 裡不是字面值",
            "llm_schemas.py:12 Field(...) 的參數不是字面值",
            "llm_schemas.py:13 值不是字面值",
            "llm_schemas.py:14 類別 A 裡有不允許的寫法（FunctionDef）",
            "llm_schemas.py:16 類別 B 有 decorator",
            "llm_schemas.py:17 指定 __doc__",
            "llm_schemas.py:19 用 Enum(...) 函式寫法",
            "llm_schemas.py:20 最上層有不允許的寫法（Assign）",
            "llm_schemas.py:21 最上層有不允許的寫法（FunctionDef）",
        ]
    )


def test_schema_scanner_catches_calls_inside_annotations(tmp_path: Path) -> None:
    sample = tmp_path / "llm_schemas.py"
    sample.write_text(
        "from typing import Annotated, Literal\n"
        "from pydantic import BaseModel, Field\n"
        "class A(BaseModel):\n"
        "    x: Annotated[str, Field(description=TEXT)]\n"
        "    y: Literal[tuple(NAMES)]\n"
        "    z: Annotated[str, make()]\n",
        encoding="utf-8",
    )

    hits = schema_module_violations(sample, base=tmp_path)

    assert sorted(hits) == [
        "llm_schemas.py:4 Field(...) 的參數不是字面值",
        "llm_schemas.py:5 Literal[...] 裡不是字面值",
        "llm_schemas.py:5 呼叫 tuple(...)",
        "llm_schemas.py:6 呼叫 make(...)",
    ]


def test_schema_scanner_allows_literal_schemas(tmp_path: Path) -> None:
    sample = tmp_path / "llm_schemas.py"
    sample.write_text(
        '"""說明。"""\n'
        "from enum import StrEnum\n"
        "from typing import Annotated, Literal\n"
        "from pydantic import BaseModel, ConfigDict, Field\n"
        "class Kind(StrEnum):\n"
        '    """種類。"""\n'
        "    A = 'a'\n"
        "class A(BaseModel, frozen=True):\n"
        '    """說明。"""\n'
        "    model_config = ConfigDict(extra='forbid')\n"
        "    kind: Literal['a', 'b']\n"
        "    concept: str = Field(description='寫死的說明', max_length=200)\n"
        "    tags: list[str] = []\n"
        "    note: Annotated[str, Field(description='x')] = ''\n"
        "    other: Kind\n",
        encoding="utf-8",
    )

    assert schema_module_violations(sample, base=tmp_path) == []


def test_output_schema_scanner(tmp_path: Path) -> None:
    sample = tmp_path / "service.py"
    sample.write_text(
        "from radar.domain import llm_schemas\n"
        "from radar.domain import llm_schemas as s\n"
        "from radar.domain.llm_schemas import FakeAnyObject, FakeResearchQuestion as Q\n"
        "a = LlmRequest(output_schema=FakeAnyObject)\n"
        "b = LlmRequest(output_schema=Q)\n"
        "c = LlmRequest(output_schema=llm_schemas.FakeAnyObject)\n"
        "d = LlmRequest(output_schema=s.FakeAnyObject)\n"
        "e = LlmRequest(output_schema=None)\n"
        "f = LlmRequest(output_schema=Local)\n"
        "g = LlmRequest(output_schema=build(names))\n",
        encoding="utf-8",
    )

    hits = output_schema_source_violations(sample, base=tmp_path)

    assert hits == [
        "service.py:9 output_schema 不是 llm_schemas 的類別",
        "service.py:10 output_schema 不是 llm_schemas 的類別",
    ]
