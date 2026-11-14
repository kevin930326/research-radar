"""預算檢查的接線規則（NFR-3、ADR-0031）：`radar/` 裡每個 `run_llm_job(...)` 呼叫都要寫
`budget_allows=BudgetGuard(...)`。

閘門的 `budget_allows` 參數收任何 `Callable[[LlmTask], bool]`（測試傳 lambda），所以型別檢查擋不住
正式程式傳 `lambda _: True`；這裡用 AST 掃：

- 呼叫 `run_llm_job`（直接名稱或 `模組.run_llm_job`）一定要有 `budget_allows=` 關鍵字，值是
  `BudgetGuard(...)`（或 `模組.BudgetGuard(...)`）這個呼叫本身；變數、lambda、`**kwargs` 都不算。
- `run_llm_job` 不能被拿去別處用（例如 `functools.partial(run_llm_job, ...)`、指派給別的名字），
  也不能 `import ... as` 換名字：那樣就掃不到。
- 閘門自己（`radar/services/llm_gateway.py`）定義它，不算。

掃描函式另外用故意寫錯的原始碼證明抓得到。
"""

import ast
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
GATEWAY = "radar/services/llm_gateway.py"
FUNCTION = "run_llm_job"
GUARD = "BudgetGuard"


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


def _call_problem(call: ast.Call) -> str | None:
    if any(keyword.arg is None for keyword in call.keywords):
        return "用 **kwargs 傳參數，看不出 budget_allows 是什麼"
    values = [keyword.value for keyword in call.keywords if keyword.arg == "budget_allows"]
    if not values:
        return "沒有寫 budget_allows=BudgetGuard(...)"
    value = values[0]
    if not (isinstance(value, ast.Call) and _named(value.func, GUARD)):
        return "budget_allows 要直接寫 BudgetGuard(...)"
    return None


def wiring_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（相對路徑 → 原始碼）裡違反接線規則的地方。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        if relative == GATEWAY:
            continue
        tree = ast.parse(source)
        called = {
            id(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _named(node.func, FUNCTION)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _named(node.func, FUNCTION):
                problem = _call_problem(node)
                if problem is not None:
                    problems.append(f"{relative}:{node.lineno}：{FUNCTION}(...) {problem}")
            elif _named(node, FUNCTION) and id(node) not in called:
                line = getattr(node, "lineno", 0)
                problems.append(f"{relative}:{line}：{FUNCTION} 只能直接呼叫，不能拿去別處用")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == FUNCTION and alias.asname not in (None, FUNCTION):
                        problems.append(
                            f"{relative}:{node.lineno}：{FUNCTION} 不能 import 成別的名字"
                        )
    return problems


def test_every_gateway_call_passes_budget_guard() -> None:
    sources = _radar_sources()

    assert wiring_problems(sources) == []
    # 掃描真的看得到閘門的定義與 BudgetGuard。
    assert f"def {FUNCTION}(" in sources[GATEWAY]
    assert f"class {GUARD}:" in sources["radar/services/budget.py"]


def test_scanner_catches_each_mistake() -> None:
    sources = {
        "radar/services/missing.py": (
            "from radar.services.llm_gateway import run_llm_job\n"
            "def f(a, d, p, c, j):\n"
            "    return run_llm_job(a, d, p, c, j)\n"
        ),
        "radar/services/lambda_.py": (
            "def f(a, d, p, c, j):\n"
            "    return run_llm_job(a, d, p, c, j, budget_allows=lambda _task: True)\n"
        ),
        "radar/services/variable.py": (
            "def f(a, d, p, c, j, guard):\n"
            "    return run_llm_job(a, d, p, c, j, budget_allows=guard)\n"
        ),
        "radar/services/module_attr.py": (
            "from radar.services import llm_gateway\n"
            "def f(a, d, p, c, j):\n"
            "    return llm_gateway.run_llm_job(a, d, p, c, j, budget_allows=always())\n"
        ),
        "radar/services/kwargs.py": (
            "def f(a, d, p, c, j, **kwargs):\n"
            "    return run_llm_job(a, d, p, c, j, budget_allows=BudgetGuard(d, c), **kwargs)\n"
        ),
        "radar/services/partial.py": (
            "import functools\n"
            "def f():\n"
            "    return functools.partial(run_llm_job, budget_allows=lambda _t: True)\n"
        ),
        "radar/services/assigned.py": "job = run_llm_job\n",
        "radar/entrypoints/worker/alias.py": (
            "from radar.services.llm_gateway import run_llm_job as run\n"
        ),
        # 不算：正確的寫法（直接名稱、模組屬性、BudgetGuard 經模組取）、閘門自己、名字只是像。
        "radar/services/good.py": (
            "from radar.services.budget import BudgetGuard\n"
            "from radar.services import budget, llm_gateway\n"
            "from radar.services.llm_gateway import run_llm_job\n"
            "def f(a, d, p, c, j, b):\n"
            "    run_llm_job(a, d, p, c, j, budget_allows=BudgetGuard(d, b))\n"
            "    llm_gateway.run_llm_job(a, d, p, c, j, budget_allows=budget.BudgetGuard(d, b))\n"
        ),
        GATEWAY: "def run_llm_job(*, budget_allows):\n    return run_llm_job(budget_allows=1)\n",
        "radar/services/other.py": "def run_llm_jobs(x):\n    return run_llm_jobs(x)\n",
    }

    problems = wiring_problems(sources)

    assert sorted(problem.split(":")[0] for problem in problems) == [
        "radar/entrypoints/worker/alias.py",
        "radar/services/assigned.py",
        "radar/services/kwargs.py",
        "radar/services/lambda_.py",
        "radar/services/missing.py",
        "radar/services/module_attr.py",
        "radar/services/partial.py",
        "radar/services/variable.py",
    ], problems
