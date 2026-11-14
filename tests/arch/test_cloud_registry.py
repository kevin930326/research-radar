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

- `radar/` 裡 `policy.py` 以外不能用 `isinstance`／`issubclass` 判斷 `CloudSafeText`（Codex 複查
  PR #54）：`isinstance` 相信物件自己的 `__class__`，偽裝的物件騙得過；一律用 policy 的
  `is_cloud_safe_text`（真實型別）或直接 `cloud_text`／`cloud_source`。

- `radar/` 裡 `domain/policy.py`、`domain/llm.py` 以外不能用 `isinstance`／`issubclass` 判斷
  `LlmRequest`（Opus 確認審查 77caba7）：一律經 `check_llm_request`／`snapshot_llm_request`。
- `radar/` 裡不用 `create_model`、`json_schema_extra`：`output_schema` 只能是寫在程式裡的模型類別，
  不能用執行期的文字組出 schema（ADR-0029）。
- `radar/` 裡不 import `gc`、不用 `__globals__`：這兩樣是不寫出名字也拿得到放行登記處的手法。
  `weakref.finalize._registry`、組字串的 `getattr` 這類更刻意的手法不在範圍內（ADR-0029 取捨）。

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


# --- 不用 isinstance 判斷 CloudSafeText -------------------------------------------------------

CLASS_NAME = "CloudSafeText"


def _mentions_class(node: ast.AST) -> bool:
    return any(
        (isinstance(sub, ast.Name) and sub.id == CLASS_NAME)
        or (isinstance(sub, ast.Attribute) and sub.attr == CLASS_NAME)
        for sub in ast.walk(node)
    )


def isinstance_violations(path: Path, *, base: Path) -> list[str]:
    """這個檔裡用 `isinstance`／`issubclass` 判斷 `CloudSafeText` 的地方（含放在 tuple 裡）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    name = path.relative_to(base).as_posix()
    return [
        f"{name}:{node.lineno} 用 {node.func.id} 判斷 CloudSafeText"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"isinstance", "issubclass"}
        and len(node.args) >= 2
        and _mentions_class(node.args[1])
    ]


def test_outside_policy_nothing_uses_isinstance_on_cloud_safe_text() -> None:
    offenders = [
        hit
        for path in _python_files([RADAR])
        if path != POLICY
        for hit in isinstance_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_isinstance_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from radar.domain import policy\n"
        "from radar.domain.policy import CloudSafeText\n"
        "a = isinstance(x, CloudSafeText)\n"
        "b = isinstance(x, policy.CloudSafeText)\n"
        "c = isinstance(x, (str, CloudSafeText))\n"
        "d = issubclass(type(x), CloudSafeText)\n",
        encoding="utf-8",
    )

    hits = isinstance_violations(sample, base=tmp_path)

    assert sorted(hits) == [
        "sample.py:3 用 isinstance 判斷 CloudSafeText",
        "sample.py:4 用 isinstance 判斷 CloudSafeText",
        "sample.py:5 用 isinstance 判斷 CloudSafeText",
        "sample.py:6 用 issubclass 判斷 CloudSafeText",
    ]


def test_isinstance_scanner_allows_real_type_checks(tmp_path: Path) -> None:
    sample = tmp_path / "ok.py"
    sample.write_text(
        "from radar.domain.policy import CloudSafeText, is_cloud_safe_text\n"
        "def f(x: object) -> bool:\n"
        "    return is_cloud_safe_text(x) or type(x) is CloudSafeText or isinstance(x, str)\n",
        encoding="utf-8",
    )

    assert isinstance_violations(sample, base=tmp_path) == []


# --- 不用 isinstance 判斷 LlmRequest、schema 不能動態組、不碰 gc／__globals__ ---------------------

LLM_MODULE = RADAR / "domain" / "llm.py"
DYNAMIC_SCHEMA_NAMES = frozenset({"create_model", "json_schema_extra"})


def request_isinstance_violations(path: Path, *, base: Path) -> list[str]:
    """這個檔裡用 `isinstance`／`issubclass` 判斷 `LlmRequest` 的地方（含放在 tuple 裡）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    name = path.relative_to(base).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"isinstance", "issubclass"}
            and len(node.args) >= 2
            and any(
                (isinstance(sub, ast.Name) and sub.id == "LlmRequest")
                or (isinstance(sub, ast.Attribute) and sub.attr == "LlmRequest")
                for sub in ast.walk(node.args[1])
            )
        ):
            found.append(f"{name}:{node.lineno} 用 {node.func.id} 判斷 LlmRequest")
    return found


def dynamic_schema_and_introspection_violations(path: Path, *, base: Path) -> list[str]:
    """這個檔裡動態組 Pydantic schema，或用 `gc`／`__globals__` 的地方。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    name = path.relative_to(base).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in DYNAMIC_SCHEMA_NAMES:
            found.append(f"{name}:{node.lineno} 用 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in DYNAMIC_SCHEMA_NAMES:
            found.append(f"{name}:{node.lineno} 用 .{node.attr}")
        elif isinstance(node, ast.keyword) and node.arg in DYNAMIC_SCHEMA_NAMES:
            found.append(f"{name}:{node.value.lineno} 用 {node.arg}=")
        elif isinstance(node, ast.ImportFrom) and any(
            alias.name in DYNAMIC_SCHEMA_NAMES for alias in node.names
        ):
            found.append(f"{name}:{node.lineno} import 動態 schema 工具")
        elif isinstance(node, ast.Attribute) and node.attr == "__globals__":
            found.append(f"{name}:{node.lineno} 用 __globals__")
        elif isinstance(node, ast.Constant) and node.value == "__globals__":
            found.append(f"{name}:{node.lineno} 用字串取 __globals__")
        elif (
            isinstance(node, ast.Import)
            and any(alias.name.split(".")[0] == "gc" for alias in node.names)
        ) or (isinstance(node, ast.ImportFrom) and node.module == "gc"):
            found.append(f"{name}:{node.lineno} import gc")
    return found


def test_outside_policy_and_llm_nothing_uses_isinstance_on_llm_request() -> None:
    offenders = [
        hit
        for path in _python_files([RADAR])
        if path not in (POLICY, LLM_MODULE)
        for hit in request_isinstance_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_radar_has_no_dynamic_schema_gc_or_globals() -> None:
    offenders = [
        hit
        for path in _python_files([RADAR])
        for hit in dynamic_schema_and_introspection_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_request_isinstance_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from radar.domain import llm\n"
        "from radar.domain.llm import LlmRequest\n"
        "a = isinstance(x, LlmRequest)\n"
        "b = isinstance(x, llm.LlmRequest)\n"
        "c = isinstance(x, (str, LlmRequest))\n"
        "d = issubclass(type(x), LlmRequest)\n"
        "e = type(x) is LlmRequest\n",
        encoding="utf-8",
    )

    hits = request_isinstance_violations(sample, base=tmp_path)

    assert sorted(hits) == [
        "sample.py:3 用 isinstance 判斷 LlmRequest",
        "sample.py:4 用 isinstance 判斷 LlmRequest",
        "sample.py:5 用 isinstance 判斷 LlmRequest",
        "sample.py:6 用 issubclass 判斷 LlmRequest",
    ]


def test_dynamic_schema_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import gc\n"
        "from gc import get_referrers\n"
        "from pydantic import create_model\n"
        "import pydantic\n"
        "M = pydantic.create_model('M')\n"
        "class A(pydantic.BaseModel):\n"
        "    x: str = pydantic.Field(json_schema_extra={'a': 1})\n"
        "g = f.__globals__\n"
        "h = getattr(f, '__globals__')\n",
        encoding="utf-8",
    )

    hits = dynamic_schema_and_introspection_violations(sample, base=tmp_path)

    assert sorted(hits) == [
        "sample.py:1 import gc",
        "sample.py:2 import gc",
        "sample.py:3 import 動態 schema 工具",
        "sample.py:5 用 .create_model",
        "sample.py:7 用 json_schema_extra=",
        "sample.py:8 用 __globals__",
        "sample.py:9 用字串取 __globals__",
    ]


def test_dynamic_schema_scanner_allows_static_models(tmp_path: Path) -> None:
    sample = tmp_path / "ok.py"
    sample.write_text(
        "from pydantic import BaseModel, ConfigDict, Field\n"
        "class A(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    x: str = Field(description='寫死的說明')\n",
        encoding="utf-8",
    )

    assert dynamic_schema_and_introspection_violations(sample, base=tmp_path) == []
