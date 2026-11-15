"""LLM 閘門的架構規則（AD-8、ADR-0009、ADR-0030）：

- 模型只經閘門呼叫：`radar/` 裡 `.complete(...)` 的呼叫只准在 `radar/services/llm_gateway.py`。
- 工作代碼固定六個（寫進 `derivation_run.task` 之後不改名），資料庫的 CHECK 列的是同一組。
- `hashlib` 只准在算指紋與原始資料雜湊的模組 import（新的雜湊要集中到 `domain/fingerprints.py`）。
- `input_sha256`、`prompt_sha256`、`schema_sha256`、`run_lock_key` 只在
  `domain/fingerprints.py` 定義。

掃描函式另外用假的原始碼證明抓得到。
"""

import ast
import re
from collections.abc import Mapping
from pathlib import Path

from radar.domain.ai_outputs import LlmTask
from tests.database import OWNER_ROLE, TestDatabase

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
GATEWAY = "radar/services/llm_gateway.py"
FINGERPRINTS = "radar/domain/fingerprints.py"
TASK_CODES = frozenset(
    {"zh_summary", "classify", "extract_model", "merge_judge", "qa_answer", "eval_judge"}
)
# 現有用到 hashlib 的模組（各自的理由寫在模組說明）；新的雜湊放 fingerprints.py。
HASHLIB_ALLOWED = frozenset(
    {
        FINGERPRINTS,
        "radar/domain/raw_records.py",  # 原始回應的內容雜湊（Story 1.4）
        "radar/services/reparse.py",  # 重新解析時核對原始檔（Story 1.5）
        "radar/adapters/fake/embedding.py",  # 假向量由文字雜湊決定（測試替身）
    }
)
FINGERPRINT_FUNCTIONS = frozenset(
    {"input_sha256", "prompt_sha256", "schema_sha256", "run_lock_key"}
)


def _radar_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(RADAR.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def gateway_rule_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（相對路徑 → 原始碼）裡違反上面三條掃描規則的地方。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "complete"
                and relative != GATEWAY
            ):
                problems.append(f"{relative}:{node.lineno}：呼叫 .complete()，模型只准經閘門呼叫")
            if relative not in HASHLIB_ALLOWED:
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                if any(name == "hashlib" or name.startswith("hashlib.") for name in names):
                    line = getattr(node, "lineno", 0)
                    problems.append(f"{relative}:{line}：import hashlib，雜湊集中到 fingerprints")
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in FINGERPRINT_FUNCTIONS
                and relative != FINGERPRINTS
            ):
                problems.append(
                    f"{relative}:{node.lineno}：定義 {node.name}，只准在 fingerprints.py"
                )
    return problems


def test_radar_follows_gateway_rules() -> None:
    sources = _radar_sources()

    assert gateway_rule_problems(sources) == []
    # 掃描真的看得到閘門的呼叫與指紋函式的定義。
    assert "provider.complete(request)" in sources[GATEWAY]
    assert "def input_sha256(" in sources[FINGERPRINTS]


def test_scanner_catches_each_mistake() -> None:
    sources = {
        "radar/services/qa.py": "def f(provider, r):\n    return provider.complete(r)",
        "radar/services/hash.py": "import hashlib\n",
        "radar/repo/hash2.py": "from hashlib import sha256\n",
        "radar/services/fp.py": "def input_sha256(blocks):\n    return ''\n",
        "radar/domain/fp2.py": "def run_lock_key(key):\n    return 0\n",
        # 不算：閘門自己、允許的模組、名字只是像。
        GATEWAY: "def f(provider, r):\n    return provider.complete(r)",
        FINGERPRINTS: "import hashlib\ndef input_sha256():\n    pass\n",
        "radar/services/other.py": "def completed(x):\n    return x.completed()\n",
    }

    problems = gateway_rule_problems(sources)

    assert sorted(problem.split(":")[0] for problem in problems) == [
        "radar/domain/fp2.py",
        "radar/repo/hash2.py",
        "radar/services/fp.py",
        "radar/services/hash.py",
        "radar/services/qa.py",
    ], problems


def test_task_codes_are_fixed() -> None:
    assert {task.value for task in LlmTask} == TASK_CODES


def test_database_task_check_lists_same_codes(migrated_database: TestDatabase) -> None:
    # 約束名稱經命名慣例加了前綴（和既有遷移一樣），所以用定義內容找。
    with migrated_database.connect(OWNER_ROLE) as connection:
        definitions = [
            row[0]
            for row in connection.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conrelid = 'derivation_run'::regclass AND contype = 'c'"
            ).fetchall()
            if row[0].startswith("CHECK ((task = ANY")
        ]
    assert len(definitions) == 1, definitions
    assert set(re.findall(r"'([a-z_]+)'", definitions[0])) == TASK_CODES
