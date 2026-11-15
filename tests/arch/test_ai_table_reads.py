"""讀 AI 表頭表（`zh_summary`、`classification`、`research_model`）與掛在表頭底下的細項表
（`classification_label`、`construct_mention`、`hypothesis`）的查詢只准放在
`radar/repo/ai_outputs.py`（AD-7、ADR-0030、ADR-0033、ADR-0034）：
哪一筆算數（`active`、候選不算）只在一個地方決定；標籤不接上 `active` 表頭就去數，會把候選與
已取代的也算進去。

做法照 `test_keyword_table_reads.py`：掃 `radar/` 每個 .py 檔的字串（f-string 的變數換成佔位
字元、不含 docstring），`FROM`／`JOIN`／`USING` 這四張表只准出現在 `repo/ai_outputs.py`；
`DELETE FROM` 是寫入，不算讀取。表的物件（`zh_summary_table` 等）也不准在別處用。
"""

import ast
import re
from collections.abc import Mapping
from pathlib import Path

from radar.domain.ai_outputs import HEADER_TABLES
from tests.arch.test_keyword_table_reads import _string_literals

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
ALLOWED = frozenset({"radar/repo/ai_outputs.py"})
# 表頭表加上細項表（細項的狀態跟著表頭，讀細項也要經同一個模組）。
DETAIL_TABLES = ("classification_label", "construct_mention", "hypothesis")
TABLES = tuple(sorted({*HEADER_TABLES.values(), *DETAIL_TABLES}))
TABLE_OBJECTS = frozenset(f"{table}_table" for table in TABLES)
_READ = re.compile(
    r"(\bDELETE\s+)?\b(?:FROM|JOIN|USING)\s+(?:ONLY\s+)?(?:\"?[A-Za-z_]\w*\"?\.)?\"?"
    rf"({'|'.join(TABLES)})\"?(?!\w)",
    re.IGNORECASE,
)


def _reads_in_strings(source: str) -> list[str]:
    found: list[str] = []
    for literal in _string_literals(ast.parse(source)):
        for match in _READ.finditer(literal):
            if match.group(1) is None:
                found.append(" ".join(match.group(0).split()))
    return found


def _table_object_uses(source: str) -> list[tuple[int, str]]:
    """表物件被用到的（行號, 名稱）；定義它的指派不算。"""
    tree = ast.parse(source)
    defined = {
        id(target)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
    }
    uses: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in TABLE_OBJECTS and id(node) not in defined:
            uses.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in TABLE_OBJECTS:
            uses.append((node.lineno, node.attr))
        elif isinstance(node, ast.alias) and {node.name, node.asname} & TABLE_OBJECTS:
            uses.append((getattr(node, "lineno", 0), node.name))
    return sorted(uses)


def ai_table_read_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（相對路徑 → 原始碼）裡，在 `repo/ai_outputs.py` 以外讀表頭表的地方。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        if relative in ALLOWED:
            continue
        problems += [
            f"{relative}：讀 AI 表頭表（{sql}），只准在 repo/ai_outputs.py"
            for sql in _reads_in_strings(source)
        ]
        problems += [
            f"{relative}:{line}：用到 {name}，讀 AI 表頭表只准在 repo/ai_outputs.py"
            for line, name in _table_object_uses(source)
        ]
    return problems


def _radar_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(RADAR.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def test_only_ai_outputs_repo_reads_header_tables() -> None:
    sources = _radar_sources()

    assert ai_table_read_problems(sources) == []
    # 掃描真的看得到 ai_outputs.py 讀這三張表。
    assert _reads_in_strings(sources["radar/repo/ai_outputs.py"])


def test_scanner_catches_each_mistake() -> None:
    sources = {
        "radar/repo/papers.py": 'SQL = "SELECT 1 FROM zh_summary WHERE paper_id = :id"',
        "radar/repo/stats.py": (
            'def f(c):\n    return f"SELECT 1 FROM paper p JOIN public.classification k ON {c}"'
        ),
        "radar/services/x.py": 'SQL = """SELECT * FROM\n  "research_model" m"""',
        "radar/repo/stats2.py": (
            'SQL = "SELECT key, count(*) FROM classification_label GROUP BY key"'
        ),
        "radar/repo/orm.py": (
            "from radar.repo.ai_outputs import zh_summary_table\n"
            "def f(select):\n    return select(zh_summary_table)"
        ),
        # 不算：寫入、別的表名、docstring、允許的模組。
        "radar/repo/writer.py": 'SQL = "DELETE FROM zh_summary WHERE paper_id = :id"',
        "radar/repo/other.py": 'SQL = "SELECT 1 FROM classification_old JOIN zh_summary_x"',
        "radar/repo/doc.py": '"""SELECT 1 FROM zh_summary"""',
        "radar/repo/ai_outputs.py": 'SQL = "SELECT 1 FROM zh_summary"',
    }

    problems = ai_table_read_problems(sources)

    assert [problem.split("：")[0].split(":")[0] for problem in problems] == [
        "radar/repo/orm.py",
        "radar/repo/orm.py",
        "radar/repo/papers.py",
        "radar/repo/stats.py",
        "radar/repo/stats2.py",
        "radar/services/x.py",
    ], problems
