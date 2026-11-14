"""放行登記處（ADR-0029、Codex 審查 PR #54 第 1 點）的架構規則：

- 放行登記處的私有狀態（登記表、號碼產生器、物件上的號碼欄位）只能在 `radar/domain/policy.py`
  裡碰：`radar/` 其他地方不能用名稱、屬性、`from ... import` 或字串（`getattr`／`object.__setattr__`
  的第二個參數）引用它們。
- `radar/` 裡 `domain/` 以外（adapters、services、repo、entrypoints、ports、config）不能呼叫
  `object.__setattr__`、`object.__new__`、`object.__delattr__`（含 `getattr(object, "__setattr__")`
  這種寫法），也不能 import `pickle`、`copyreg`、`marshal`、`shelve`：這些是硬改或另外做出
  `CloudSafeText` 的工具。`domain/` 的 frozen dataclass 在 `__post_init__` 正規化欄位要用
  `object.__setattr__`，所以這條只管 `domain/` 以外；`domain/` 裡要硬改 `CloudSafeText` 也得碰到
  上一條守著的私有狀態才有用。

掃描函式另外用 tmp 裡的假檔證明抓得到。這是新增的規則，沒有改 `test_cloud_clearance.py` 的既有掃描。
"""

import ast
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
POLICY = RADAR / "domain" / "policy.py"
DOMAIN = RADAR / "domain"
# 拆開寫：這個檔不在掃描範圍，但寫法和 test_cloud_clearance.py 一致，避免日後擴大範圍時誤判。
PRIVATE_NAMES = frozenset({"_REGIS" + "TRY", "_TOK" + "ENS", "_tok" + "en"})
RAW_OBJECT_METHODS = frozenset({"__setattr__", "__new__", "__delattr__"})
SERIALIZER_MODULES = frozenset({"pickle", "copyreg", "marshal", "shelve"})


def private_state_violations(path: Path, *, base: Path) -> list[str]:
    """這個檔裡引用放行登記處私有狀態的地方（「相對路徑:行號 說明」）。"""
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
        elif isinstance(node, ast.Constant) and node.value in PRIVATE_NAMES:
            found.append(f"{name}:{node.lineno} 用字串取私有狀態 {node.value}")
    return found


def _is_object(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "object"


def raw_object_violations(path: Path, *, base: Path) -> list[str]:
    """這個檔裡繞過 `__setattr__`／建構子的寫法，與 import 序列化工具的地方。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    name = path.relative_to(base).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and _is_object(node.value)
            and node.attr in RAW_OBJECT_METHODS
        ):
            found.append(f"{name}:{node.lineno} 用 object.{node.attr}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and _is_object(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in RAW_OBJECT_METHODS
        ):
            found.append(f"{name}:{node.lineno} 用 getattr 取 object.{node.args[1].value}")
        elif isinstance(node, ast.Import):
            found.extend(
                f"{name}:{node.lineno} import {alias.name}"
                for alias in node.names
                if alias.name.split(".")[0] in SERIALIZER_MODULES
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.split(".")[0] in SERIALIZER_MODULES
        ):
            found.append(f"{name}:{node.lineno} import {node.module}")
    return found


def _python_files(roots: Sequence[Path]) -> list[Path]:
    return sorted(path for root in roots for path in root.rglob("*.py"))


def test_only_policy_touches_the_registry() -> None:
    offenders = [
        hit
        for path in _python_files([RADAR])
        if path != POLICY
        for hit in private_state_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_policy_is_where_the_registry_lives() -> None:
    # policy.py 真的用到這三個名稱：代表掃描的名稱對得上實際寫法。
    hits = private_state_violations(POLICY, base=ROOT)

    for private in PRIVATE_NAMES:
        assert any(private in hit for hit in hits), private


def test_outside_domain_nothing_bypasses_setattr_or_serializes() -> None:
    offenders = [
        hit
        for path in _python_files([RADAR])
        if not path.is_relative_to(DOMAIN)
        for hit in raw_object_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_private_state_scanner_catches_each_form(tmp_path: Path) -> None:
    registry, tokens, token = "_REGIS" + "TRY", "_TOK" + "ENS", "_tok" + "en"
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from radar.domain import policy\n"
        f"from radar.domain.policy import {registry}\n"
        f"a = policy.{tokens}\n"
        f"b = safe.{token}\n"
        f"object.__setattr__(safe, '{token}', 1)\n"
        f"c = getattr(policy, '{registry}')\n",
        encoding="utf-8",
    )

    hits = private_state_violations(sample, base=tmp_path)

    assert sorted(hits) == [
        f"sample.py:2 import 私有狀態 {registry}",
        f"sample.py:3 引用私有狀態 .{tokens}",
        f"sample.py:4 引用私有狀態 .{token}",
        f"sample.py:5 用字串取私有狀態 {token}",
        f"sample.py:6 用字串取私有狀態 {registry}",
    ]


def test_private_state_scanner_ignores_similar_names(tmp_path: Path) -> None:
    sample = tmp_path / "ok.py"
    sample.write_text(
        "from radar.domain.sweep import SWEEP_REGISTRY\n"
        "access_token = 1\n"
        "x = SWEEP_REGISTRY\n"
        "y = safe.text\n",
        encoding="utf-8",
    )

    assert private_state_violations(sample, base=tmp_path) == []


def test_raw_object_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import pickle\n"
        "import copyreg as c\n"
        "from marshal import dumps\n"
        "object.__setattr__(safe, 'x', 1)\n"
        "shell = object.__new__(cls)\n"
        "object.__delattr__(safe, 'x')\n"
        "setter = getattr(object, '__setattr__')\n"
        "f = object.__setattr__\n",
        encoding="utf-8",
    )

    hits = raw_object_violations(sample, base=tmp_path)

    assert sorted(hits) == [
        "sample.py:1 import pickle",
        "sample.py:2 import copyreg",
        "sample.py:3 import marshal",
        "sample.py:4 用 object.__setattr__",
        "sample.py:5 用 object.__new__",
        "sample.py:6 用 object.__delattr__",
        "sample.py:7 用 getattr 取 object.__setattr__",
        "sample.py:8 用 object.__setattr__",
    ]


def test_raw_object_scanner_allows_ordinary_code(tmp_path: Path) -> None:
    sample = tmp_path / "ok.py"
    sample.write_text(
        "import copy\n"
        "import json\n"
        "class A:\n"
        "    def __setattr__(self, name, value):\n"
        "        super().__setattr__(name, value)\n"
        "x = isinstance(1, object)\n",
        encoding="utf-8",
    )

    assert raw_object_violations(sample, base=tmp_path) == []
