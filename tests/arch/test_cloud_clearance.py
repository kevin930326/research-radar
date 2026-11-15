"""送雲端的放行（AD-9、ADR-0029）的架構規則：

- 建立 `CloudSafeText`（`CloudSafeText(...)`、`X.__new__(CloudSafeText, ...)`）與引用放行用的私有
  記號 `_CLEARANCE`，只能出現在 `radar/domain/policy.py`（掃 `radar/` 與 `tests/`）。
- `radar/adapters/anthropic/` 沒有任何呼叫帶 `tools`、`tool_choice`、`mcp_servers`（與能夾帶它們的
  `extra_body`）參數，也沒有用這些名稱當字典的鍵。
- 兩個 port 的文字參數與 `LlmRequest` 的 `system`／`content` 都標成 `CloudSafeText`。

掃描函式另外用 tmp 裡的假檔證明抓得到。
"""

import ast
from collections.abc import Sequence
from pathlib import Path
from typing import get_type_hints

from radar.domain.llm import LlmRequest
from radar.domain.policy import CloudSafeText
from radar.ports import EmbeddingProvider, LlmProvider

ROOT = Path(__file__).resolve().parents[2]
SCANNED = (ROOT / "radar", ROOT / "tests")
POLICY = ROOT / "radar" / "domain" / "policy.py"
ANTHROPIC_ADAPTER = ROOT / "radar" / "adapters" / "anthropic"
CLASS_NAME = "CloudSafeText"
# 拆開寫：這個檔自己也會被掃，整串寫出來會被當成「用字串取私有記號」。
SENTINEL = "_CLEAR" + "ANCE"
FORBIDDEN_KEYWORDS = frozenset({"tools", "tool_choice", "mcp_servers", "extra_body"})


def _is_class_ref(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == CLASS_NAME) or (
        isinstance(node, ast.Attribute) and node.attr == CLASS_NAME
    )


def clearance_violations(path: Path, *, base: Path) -> list[str]:
    """這個檔裡建立 `CloudSafeText` 或引用私有記號的地方（「相對路徑:行號 說明」）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    name = path.relative_to(base).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _is_class_ref(node.func):
                found.append(f"{name}:{node.lineno} 直接呼叫 CloudSafeText(...)")
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "__new__"
                and any(_is_class_ref(arg) for arg in node.args)
            ):
                found.append(f"{name}:{node.lineno} 用 __new__ 建立 CloudSafeText")
        if (isinstance(node, ast.Name) and node.id == SENTINEL) or (
            isinstance(node, ast.Attribute) and node.attr == SENTINEL
        ):
            found.append(f"{name}:{node.lineno} 引用私有記號 {SENTINEL}")
        elif isinstance(node, ast.ImportFrom) and any(a.name == SENTINEL for a in node.names):
            found.append(f"{name}:{node.lineno} import 私有記號 {SENTINEL}")
        elif isinstance(node, ast.Constant) and node.value == SENTINEL:
            found.append(f"{name}:{node.lineno} 用字串取私有記號 {SENTINEL}")
    return found


def _python_files(roots: Sequence[Path]) -> list[Path]:
    return sorted(path for root in roots for path in root.rglob("*.py"))


def test_only_policy_creates_cloud_safe_text() -> None:
    offenders = [
        hit
        for path in _python_files(SCANNED)
        if path != POLICY
        for hit in clearance_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_policy_is_where_cloud_safe_text_is_created() -> None:
    # policy.py 真的有建立與引用：代表掃描方式對得上實際寫法。
    hits = clearance_violations(POLICY, base=ROOT)

    assert any("直接呼叫 CloudSafeText" in hit for hit in hits)
    assert any(SENTINEL in hit for hit in hits)


def test_clearance_scanner_catches_each_form(tmp_path: Path) -> None:
    sentinel = SENTINEL
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from radar.domain import policy\n"
        f"from radar.domain.policy import CloudSafeText, {sentinel}\n"
        "a = CloudSafeText('x', 'title', clearance=object())\n"
        "b = policy.CloudSafeText('x', 'title', clearance=object())\n"
        "c = object.__new__(CloudSafeText)\n"
        f"d = policy.{sentinel}\n"
        f"e = getattr(policy, '{sentinel}')\n",
        encoding="utf-8",
    )

    hits = clearance_violations(sample, base=tmp_path)

    assert sorted(hits) == [
        f"sample.py:2 import 私有記號 {sentinel}",
        "sample.py:3 直接呼叫 CloudSafeText(...)",
        "sample.py:4 直接呼叫 CloudSafeText(...)",
        "sample.py:5 用 __new__ 建立 CloudSafeText",
        f"sample.py:6 引用私有記號 {sentinel}",
        f"sample.py:7 用字串取私有記號 {sentinel}",
    ]


def test_clearance_scanner_allows_type_annotations_and_isinstance(tmp_path: Path) -> None:
    sample = tmp_path / "ok.py"
    sample.write_text(
        "from radar.domain.policy import CloudSafeText\n"
        "def f(text: CloudSafeText) -> bool:\n"
        "    return isinstance(text, CloudSafeText)\n",
        encoding="utf-8",
    )

    assert clearance_violations(sample, base=tmp_path) == []


# --- Anthropic adapter 不給工具 ----------------------------------------------------------------


def tool_arguments(path: Path, *, base: Path) -> list[str]:
    """這個檔裡帶工具相關參數的呼叫，或用這些名稱當鍵的字典（「相對路徑:行號 說明」）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    name = path.relative_to(base).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            found.extend(
                f"{name}:{node.lineno} 呼叫帶 {keyword.arg}="
                for keyword in node.keywords
                if keyword.arg in FORBIDDEN_KEYWORDS
            )
        elif isinstance(node, ast.Dict):
            found.extend(
                f"{name}:{node.lineno} 字典的鍵 {key.value!r}"
                for key in node.keys
                if isinstance(key, ast.Constant) and key.value in FORBIDDEN_KEYWORDS
            )
    return found


def test_anthropic_adapter_never_passes_tools() -> None:
    files = _python_files([ANTHROPIC_ADAPTER])

    assert files, "找不到 radar/adapters/anthropic/"
    assert [hit for path in files for hit in tool_arguments(path, base=ROOT)] == []


def test_tool_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "client.create(model='m', tools=[])\n"
        "client.create(tool_choice={'type': 'auto'})\n"
        "client.create(mcp_servers=[])\n"
        "client.create(extra_body={'x': 1})\n"
        "body = {'tools': []}\n",
        encoding="utf-8",
    )

    assert sorted(tool_arguments(sample, base=tmp_path)) == [
        "sample.py:1 呼叫帶 tools=",
        "sample.py:2 呼叫帶 tool_choice=",
        "sample.py:3 呼叫帶 mcp_servers=",
        "sample.py:4 呼叫帶 extra_body=",
        "sample.py:5 字典的鍵 'tools'",
    ]


# --- port 與 LlmRequest 的型別 -----------------------------------------------------------------


def test_ports_and_request_take_cloud_safe_text() -> None:
    assert get_type_hints(LlmProvider.complete)["request"] is LlmRequest
    request_hints = get_type_hints(LlmRequest)
    assert request_hints["system"] is CloudSafeText
    assert request_hints["content"] == tuple[CloudSafeText, ...]
    assert get_type_hints(EmbeddingProvider.embed_documents)["texts"] == Sequence[CloudSafeText]
    assert get_type_hints(EmbeddingProvider.embed_query)["text"] is CloudSafeText
