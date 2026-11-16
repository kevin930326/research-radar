"""重跑拒收的接線規則（Story 2.12、ADR-0044）：`retry_of=` 只准出現在
`radar/services/run_retry.py` 的 `run_llm_job(...)` 呼叫。

`rejected` 是終點狀態，平常流程（互動、試跑、Batch 規劃）一律跳過；閘門只在帶了 `retry_of` 時放行
（鎖內再核對）。型別檢查擋不住別的模組也傳它，所以用 AST 掃：

- `radar/` 裡任何呼叫寫了 `retry_of=` 關鍵字，都要在 `run_retry.py`，而且呼叫的就是
  `run_llm_job`（直接名稱或 `模組.run_llm_job`）。
- `run_llm_job` 的 `retry_of` 是 keyword-only，不會用位置傳；`**kwargs` 與換名字 import 已經由
  `tests/arch/test_budget_guard_wiring.py` 擋下（看不出傳了什麼）。
- 閘門自己（`radar/services/llm_gateway.py`）把它往下交給 `_acquire`→`_claim_locked`，不算。

掃描函式另外用故意寫錯的原始碼證明抓得到。
"""

import ast
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
GATEWAY = "radar/services/llm_gateway.py"
ALLOWED = "radar/services/run_retry.py"
FUNCTION = "run_llm_job"
KEYWORD = "retry_of"


def _radar_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(RADAR.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def _named(node: ast.AST, name: str) -> bool:
    """`name` 或 `任何東西.name`。"""
    return (isinstance(node, ast.Name) and node.id == name) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


def wiring_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（相對路徑 → 原始碼）裡違反規則的 `retry_of=`。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        if relative == GATEWAY:
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            if not any(keyword.arg == KEYWORD for keyword in node.keywords):
                continue
            if relative != ALLOWED:
                problems.append(f"{relative}:{node.lineno}：{KEYWORD}= 只准在 {ALLOWED} 傳")
            elif not _named(node.func, FUNCTION):
                problems.append(f"{relative}:{node.lineno}：{KEYWORD}= 只能傳給 {FUNCTION}(...)")
    return problems


def test_retry_of_is_only_passed_by_run_retry() -> None:
    sources = _radar_sources()

    assert wiring_problems(sources) == []
    # 掃描真的看得到唯一合法的那一處與閘門的參數。
    allowed = ast.parse(sources[ALLOWED])
    calls = [
        node
        for node in ast.walk(allowed)
        if isinstance(node, ast.Call) and any(k.arg == KEYWORD for k in node.keywords)
    ]
    assert len(calls) == 1
    assert _named(calls[0].func, FUNCTION)
    assert f"{KEYWORD}: uuid.UUID | None = None" in sources[GATEWAY]


def test_scanner_catches_each_mistake() -> None:
    sources = {
        # 平常流程偷傳 retry_of（直接名稱、模組屬性）。
        "radar/services/zh_summary.py": (
            "def f(a, d, p, c, j, g, r):\n"
            "    return run_llm_job(a, d, p, c, j, budget_allows=g, retry_of=r)\n"
        ),
        "radar/services/llm_batches.py": (
            "from radar.services import llm_gateway\n"
            "def f(a, d, p, c, j, g):\n"
            "    return llm_gateway.run_llm_job(a, d, p, c, j, budget_allows=g, retry_of=None)\n"
        ),
        # 入口繞過服務直接傳。
        "radar/entrypoints/cli/__init__.py": "def f(r):\n    return helper(retry_of=r)\n",
        # 不算：閘門自己、只是名字像的關鍵字。
        GATEWAY: "def run_llm_job(*, retry_of=None):\n    return _acquire(retry_of=retry_of)\n",
        "radar/services/other.py": "def f(x):\n    return g(retry_off=x, retry=x)\n",
    }
    # 允許的模組：直接呼叫閘門（兩種寫法）不算；傳給別的函式（包一層再轉交）算。
    allowed_source = {
        ALLOWED: (
            "from radar.services import llm_gateway\n"
            "from radar.services.llm_gateway import run_llm_job\n"
            "def f(a, d, p, c, j, g, r):\n"
            "    run_llm_job(a, d, p, c, j, budget_allows=g, retry_of=r)\n"
            "    llm_gateway.run_llm_job(a, d, p, c, j, budget_allows=g, retry_of=r)\n"
            "    return _wrap(retry_of=r)\n"
        )
    }

    problems = wiring_problems({**sources, **allowed_source})

    assert sorted(problem.split(":")[0] for problem in problems) == [
        "radar/entrypoints/cli/__init__.py",
        "radar/services/llm_batches.py",
        ALLOWED,
        "radar/services/zh_summary.py",
    ], problems
    assert [p for p in problems if p.startswith(ALLOWED)] == [
        f"{ALLOWED}:6：{KEYWORD}= 只能傳給 {FUNCTION}(...)"
    ]
