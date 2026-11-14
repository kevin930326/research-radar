"""讀 `paper_keyword` 的查詢只准放在 `radar/repo/keyword_stats.py`（AD-31、ADR-0016 第 3 點）。

掃 `radar/` 每個 .py 檔的字串（f-string 的變數換成佔位字元、不含 docstring）：
`FROM paper_keyword`、`JOIN paper_keyword` 或 `USING paper_keyword` 只准出現在
`radar/repo/keyword_stats.py`（目前不存在，所以全部都不准）。
`DELETE FROM paper_keyword` 是寫入（`repo/papers.py` 整批覆寫），不算讀取。另外，表的物件
`paper_keyword_table` 除了定義它的那一行，也不准在別處用（避免改用 SQLAlchemy 的 select 讀）。
"""

import ast
import re
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
ALLOWED = frozenset({"radar/repo/keyword_stats.py"})
TABLE_OBJECT = "paper_keyword_table"
_PLACEHOLDER = "\x00"
_READ = re.compile(
    r'(\bDELETE\s+)?\b(?:FROM|JOIN|USING)\s+(?:ONLY\s+)?(?:"?[A-Za-z_]\w*"?\.)?"?paper_keyword"?(?!\w)',
    re.IGNORECASE,
)


def _string_literals(tree: ast.Module) -> list[str]:
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    literals: list[str] = []
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                inside.add(id(value))
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                else:
                    parts.append(_PLACEHOLDER)
            literals.append("".join(parts))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and id(node) not in inside
        ):
            literals.append(node.value)
    return literals


def _reads_in_strings(source: str) -> list[str]:
    found: list[str] = []
    for literal in _string_literals(ast.parse(source)):
        for match in _READ.finditer(literal):
            if match.group(1) is None:
                found.append(" ".join(match.group(0).split()))
    return found


def _table_object_uses(source: str) -> list[int]:
    """`paper_keyword_table` 被用到的行號（定義它的指派不算）。"""
    tree = ast.parse(source)
    defined = {
        id(target)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
    }
    lines: list[int] = []
    for node in ast.walk(tree):
        uses = (
            (isinstance(node, ast.Name) and node.id == TABLE_OBJECT and id(node) not in defined)
            or (isinstance(node, ast.Attribute) and node.attr == TABLE_OBJECT)
            or (isinstance(node, ast.alias) and TABLE_OBJECT in (node.name, node.asname))
        )
        if uses:
            lines.append(getattr(node, "lineno", 0))
    return sorted(lines)


def keyword_read_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（相對路徑 → 原始碼）裡，在 `keyword_stats.py` 以外讀 `paper_keyword` 的地方。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        if relative in ALLOWED:
            continue
        problems += [
            f"{relative}：讀 paper_keyword（{sql}），只准在 repo/keyword_stats.py"
            for sql in _reads_in_strings(source)
        ]
        problems += [
            f"{relative}:{line}：用到 {TABLE_OBJECT}，讀關鍵字只准在 repo/keyword_stats.py"
            for line in _table_object_uses(source)
        ]
    return problems


def _radar_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(RADAR.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def test_no_module_reads_paper_keyword() -> None:
    sources = _radar_sources()

    assert keyword_read_problems(sources) == []
    # 掃描真的看得到 papers.py 寫這張表（寫入不算讀取）。
    assert "DELETE FROM paper_keyword" in sources["radar/repo/papers.py"]
    assert "INSERT INTO paper_keyword" in sources["radar/repo/papers.py"]


def test_scanner_catches_each_mistake() -> None:
    sources = {
        "radar/repo/stats.py": 'SQL = "SELECT key FROM paper_keyword WHERE paper_id = :id"',
        "radar/repo/relations.py": (
            'def f(c):\n    return f"SELECT 1 FROM paper p JOIN public.paper_keyword k ON {c}"'
        ),
        "radar/repo/quoted.py": 'SQL = """SELECT * FROM\n  "paper_keyword" k"""',
        "radar/repo/lower.py": 'SQL = "select 1 from paper_keyword"',
        "radar/repo/orm.py": (
            "from radar.repo.papers import paper_keyword_table\n"
            "def f(select):\n    return select(paper_keyword_table)"
        ),
        "radar/services/x.py": "import radar.repo.papers as p\nT = p.paper_keyword_table",
        "radar/repo/using.py": (
            'SQL = "DELETE FROM paper USING paper_keyword k WHERE k.paper_id = paper.id"'
        ),
        # 不算：寫入、別的表名、docstring、允許的模組。
        "radar/repo/writer.py": 'SQL = "DELETE FROM paper_keyword WHERE paper_id = :id"',
        "radar/repo/other.py": 'SQL = "SELECT 1 FROM paper_keywords_old JOIN paper_keyword_x"',
        "radar/repo/doc.py": '"""SELECT key FROM paper_keyword"""',
        "radar/repo/keyword_stats.py": 'SQL = "SELECT key FROM paper_keyword"',
    }

    problems = keyword_read_problems(sources)

    assert [problem.split("：")[0].split(":")[0] for problem in problems] == [
        "radar/repo/lower.py",
        "radar/repo/orm.py",
        "radar/repo/orm.py",
        "radar/repo/quoted.py",
        "radar/repo/relations.py",
        "radar/repo/stats.py",
        "radar/repo/using.py",
        "radar/services/x.py",
    ], problems
