"""讀 AI 表頭表（`zh_summary`、`classification`、`research_model`）與掛在表頭底下的細項表
（`classification_label`、`construct_mention`、`hypothesis`）的查詢只准放在
`radar/repo/ai_outputs.py`（AD-7、ADR-0030、ADR-0033、ADR-0034）：
哪一筆算數（`active`、候選不算）只在一個地方決定；標籤不接上 `active` 表頭就去數，會把候選與
已取代的也算進去。

做法照 `test_keyword_table_reads.py`：掃 `radar/` 每個 .py 檔的字串（f-string 的變數換成佔位
字元、不含 docstring），`FROM`／`JOIN`／`USING` 這四張表只准出現在 `repo/ai_outputs.py`；
`DELETE FROM` 是寫入，不算讀取。表的物件（`zh_summary_table` 等）也不准在別處用。

有效值只由三個 view 決定（AD-10、Story 2.7）：正向檢查 `repo/ai_blocks.py` 讀的是
`effective_zh_summary`、`effective_classification`、`effective_research_model`；反向檢查 `radar/`
裡讀這三個 view 只准在允許清單（`VIEW_READERS`）裡的模組。view 本身不接可見範圍，讀的模組要自己
接 `visible_papers`。
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


EFFECTIVE_VIEWS = ("effective_classification", "effective_research_model", "effective_zh_summary")
_VIEW_READ = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:\"?[A-Za-z_]\w*\"?\.)?\"?"
    rf"({'|'.join(EFFECTIVE_VIEWS)})\"?(?!\w)",
    re.IGNORECASE,
)
# 准讀三個有效值 view 的模組。view 不接可見範圍：之後新增讀取者時加進這裡，並在查詢裡接上
# `repo/visibility.py` 的可見條件（`visible_papers`）。
VIEW_READERS = frozenset({"radar/repo/ai_blocks.py"})


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


def effective_view_reads(source: str) -> set[str]:
    """原始碼字串裡 `FROM`／`JOIN` 到的有效值 view。"""
    return {
        match.group(1).lower()
        for literal in _string_literals(ast.parse(source))
        for match in _VIEW_READ.finditer(literal)
    }


def test_ai_blocks_reads_the_three_effective_views() -> None:
    source = (RADAR / "repo" / "ai_blocks.py").read_text(encoding="utf-8")

    assert effective_view_reads(source) == set(EFFECTIVE_VIEWS)
    assert _reads_in_strings(source) == []


def test_view_scanner_sees_from_and_join_only() -> None:
    source = (
        '"""SELECT 1 FROM effective_research_model"""\n'
        'A = "SELECT 1 FROM effective_zh_summary AS e"\n'
        'B = f"SELECT 1 FROM paper JOIN \\"effective_classification\\" ON {x}"\n'
        'C = "SELECT 1 FROM effective_research_model_old"\n'
    )

    assert effective_view_reads(source) == {"effective_zh_summary", "effective_classification"}


def effective_view_read_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（相對路徑 → 原始碼）裡，在 `VIEW_READERS` 以外讀有效值 view 的地方。"""
    return [
        f"{relative}：讀 {view}，只准在 {sorted(VIEW_READERS)}（要接 visible_papers）"
        for relative, source in sorted(sources.items())
        if relative not in VIEW_READERS
        for view in sorted(effective_view_reads(source))
    ]


def test_only_allowed_modules_read_the_effective_views() -> None:
    assert effective_view_read_problems(_radar_sources()) == []


def test_view_read_scanner_catches_reads_outside_the_allowlist() -> None:
    sources = {
        "radar/repo/stats.py": 'SQL = "SELECT field, keys FROM effective_classification"',
        "radar/services/x.py": 'SQL = "SELECT 1 FROM paper p JOIN public.effective_zh_summary z"',
        # 不算：允許的模組、docstring。
        "radar/repo/ai_blocks.py": 'SQL = "SELECT 1 FROM effective_research_model"',
        "radar/repo/doc.py": '"""SELECT 1 FROM effective_research_model"""',
    }

    problems = effective_view_read_problems(sources)

    assert [problem.split("：")[0] for problem in problems] == [
        "radar/repo/stats.py",
        "radar/services/x.py",
    ], problems
