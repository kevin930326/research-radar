"""統計查詢只從 view `counted_paper` 出發、只放在統計模組（AD-5、ADR-0011「哪些論文算數」、
ADR-0027）。

統計模組＝`radar/repo/stats.py`，以及之後的 `radar/repo/relations.py`、`radar/repo/keyword_stats.py`
（存在的才掃）。掃 `radar/` 每個 .py 檔的字串（f-string 的變數換成佔位字元、不含 docstring）：

1. 字串含 `counted_paper` 的只准是統計模組與 `radar/repo/visibility.py`（`PublicViewer` 的
   可見條件）。
2. 統計模組的 SQL 不 `FROM`／`JOIN` `paper` 表（`paper_abstract` 這類 `paper_*` 不算），也不 import
   `radar.repo.papers`。
3. 統計模組以外的 `radar/repo` 模組，同一句 SQL 讀 `paper` 表又做彙總（`count(`、`sum(`、`avg(`、
   `GROUP BY`）就失敗：論文的篇數只由統計模組算。
4. 數 `counted_paper` 整列的計數（`count(*)`、`count(1)`、`count(<counted_paper 的別名>.id)`、
   `count(DISTINCT <別名>.id)`）只准出現在 `radar/repo/stats.py` 的 `list_cube_counts`，而且剛好
   一個：每年全部篇數只有計數方塊這一個查詢，其他模組要分母時呼叫它。

每條規則都用故意寫錯的假原始碼證明抓得到。
"""

import ast
import re
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
STATS_MODULES = frozenset(
    {"radar/repo/stats.py", "radar/repo/relations.py", "radar/repo/keyword_stats.py"}
)
COUNTED_PAPER_ALLOWED = STATS_MODULES | {"radar/repo/visibility.py"}
CUBE_COUNT_HOME = ("radar/repo/stats.py", "list_cube_counts")
PAPERS_MODULE = "radar.repo.papers"

_PLACEHOLDER = "\x00"
_SCHEMA = r'(?:ONLY\s+)?(?:"?[A-Za-z_]\w*"?\.)?'
# 沒加引號的 SQL 名稱不分大小寫，`COUNTED_PAPER` 一樣讀得到 view。
_COUNTED_PAPER = re.compile(r"(?<!\w)counted_paper(?!\w)", re.IGNORECASE)
# 讀 `paper` 表（`DELETE FROM paper` 是寫入，不算讀）；`paper_abstract` 這類不算。
_READS_PAPER = re.compile(
    rf'(\bDELETE\s+)?\b(?:FROM|JOIN)\s+{_SCHEMA}"?paper"?(?!\w)', re.IGNORECASE
)
_AGGREGATE = re.compile(r"\b(?:count|sum|avg)\s*\(|\bGROUP\s+BY\b", re.IGNORECASE)
# `FROM counted_paper [AS] 別名`：別名可省略、可以是 f-string 的變數（佔位字元）。
_COUNTED_SOURCE = re.compile(
    rf'\b(?:FROM|JOIN)\s+{_SCHEMA}"?counted_paper"?'
    rf'(?:\s+(?:AS\s+)?"?([A-Za-z_]\w*|{_PLACEHOLDER})"?)?',
    re.IGNORECASE,
)
_NOT_ALIASES = frozenset(
    {
        "where",
        "join",
        "left",
        "right",
        "inner",
        "full",
        "cross",
        "natural",
        "on",
        "using",
        "group",
        "order",
        "limit",
        "offset",
        "union",
        "except",
        "intersect",
        "having",
        "window",
        "for",
    }
)


# --- 字串的收集 ---------------------------------------------------------------------------


def _docstring_ids(tree: ast.Module) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _literals(source: str) -> list[tuple[str, str | None]]:
    """模組裡的字串與它所在的函式（最外層的函式名稱；模組層是 None）。

    f-string 的變數換成佔位字元；docstring 不算。
    """
    tree = ast.parse(source)
    docstrings = _docstring_ids(tree)
    found: list[tuple[str, str | None]] = []

    def visit(node: ast.AST, function: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and function is None:
            function = node.name
        if isinstance(node, ast.JoinedStr):
            parts = [
                value.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
                else _PLACEHOLDER
                for value in node.values
            ]
            found.append(("".join(parts), function))
            for value in node.values:  # 變數裡的運算式也可能有字串
                if isinstance(value, ast.FormattedValue):
                    visit(value.value, function)
            return
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.append((node.value, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, None)
    return found


def _radar_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(RADAR.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def _module_name(relative: str) -> str:
    return relative.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def _paper_reads(literal: str) -> list[str]:
    return [
        " ".join(match.group(0).split())
        for match in _READS_PAPER.finditer(literal)
        if match.group(1) is None
    ]


# --- 1. counted_paper 只在統計模組與 visibility.py --------------------------------------------


def counted_paper_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（相對路徑 → 原始碼）裡，不該出現 `counted_paper` 字串的模組。"""
    return [
        f"{relative}：字串含 counted_paper，統計查詢只准放在統計模組"
        for relative, source in sorted(sources.items())
        if relative not in COUNTED_PAPER_ALLOWED
        and any(_COUNTED_PAPER.search(literal) for literal, _function in _literals(source))
    ]


# --- 2. 統計模組不直接查 paper、不 import repo.papers -------------------------------------------


def _imports_papers_module(relative: str, source: str) -> list[int]:
    module = _module_name(relative)
    package = module if relative.endswith("__init__.py") else module.rpartition(".")[0]
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(
                alias.name == PAPERS_MODULE or alias.name.startswith(f"{PAPERS_MODULE}.")
                for alias in node.names
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                parts = package.split(".")
                root = ".".join(parts[: len(parts) - (node.level - 1)])
                base = f"{root}.{node.module}" if node.module else root
            targets = {base, *(f"{base}.{alias.name}" for alias in node.names)}
            if any(
                target == PAPERS_MODULE or target.startswith(f"{PAPERS_MODULE}.")
                for target in targets
            ):
                lines.append(node.lineno)
    return sorted(lines)


def stats_module_problems(sources: Mapping[str, str]) -> list[str]:
    """統計模組裡直接查 `paper` 表、或 import `radar.repo.papers` 的地方。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        if relative not in STATS_MODULES:
            continue
        for literal, _function in _literals(source):
            problems += [
                f"{relative}：直接查 paper 表（{sql}），統計只從 counted_paper 出發"
                for sql in _paper_reads(literal)
            ]
        problems += [
            f"{relative}:{line}：import {PAPERS_MODULE}，統計模組不用論文表的函式"
            for line in _imports_papers_module(relative, source)
        ]
    return problems


# --- 3. 其他 repo 模組不彙總 paper -------------------------------------------------------------


def paper_aggregate_problems(sources: Mapping[str, str]) -> list[str]:
    """統計模組以外的 `radar/repo` 模組，同一句 SQL 讀 `paper` 又做彙總的地方。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        if not relative.startswith("radar/repo/") or relative in STATS_MODULES:
            continue
        for literal, function in _literals(source):
            if _paper_reads(literal) and _AGGREGATE.search(literal):
                where = f"{relative}.{function}" if function else relative
                problems.append(
                    f"{where}：讀 paper 又做彙總（{' '.join(literal.split())[:80]}），"
                    "篇數只准由統計模組從 counted_paper 算"
                )
    return problems


# --- 4. counted_paper 整列的計數只有一個 --------------------------------------------------------


def _row_counts(literal: str) -> list[str]:
    """字串裡數 `counted_paper` 整列的計數（字串沒有讀 `counted_paper` 時是空的）。"""
    sources = list(_COUNTED_SOURCE.finditer(literal))
    if not sources:
        return []
    aliases = {"counted_paper"}
    for match in sources:
        alias = match.group(1)
        if alias is not None and alias.lower() not in _NOT_ALIASES:
            aliases.add(alias)
    named = "|".join(re.escape(alias) for alias in sorted(aliases))
    pattern = re.compile(
        rf'\bcount\s*\(\s*(?:\*|1|(?:DISTINCT\s+)?"?(?:{named})"?\.id)\s*\)', re.IGNORECASE
    )
    return [" ".join(match.group(0).split()) for match in pattern.finditer(literal)]


def cube_count_problems(sources: Mapping[str, str]) -> list[str]:
    """`list_cube_counts` 以外數 `counted_paper` 整列的地方；`list_cube_counts` 裡不是剛好一個。"""
    problems: list[str] = []
    at_home = 0
    for relative, source in sorted(sources.items()):
        for literal, function in _literals(source):
            for found in _row_counts(literal):
                if (relative, function) == CUBE_COUNT_HOME:
                    at_home += 1
                    continue
                where = f"{relative}.{function}" if function else relative
                problems.append(
                    f"{where}：數 counted_paper 整列（{found}），每年全部篇數只准由"
                    " repo/stats.py 的 list_cube_counts 數"
                )
    home = ".".join(CUBE_COUNT_HOME)
    if at_home == 0:
        problems.append(f"{home}：找不到數 counted_paper 整列的計數")
    elif at_home > 1:
        problems.append(f"{home}：數 counted_paper 整列的計數有 {at_home} 個，只准一個")
    return problems


# --- 真的原始碼 ---------------------------------------------------------------------------


def test_counted_paper_only_in_stats_modules_and_visibility() -> None:
    sources = _radar_sources()

    assert counted_paper_problems(sources) == []
    # 掃描真的看得到兩個允許的模組在用它。
    for relative in ("radar/repo/stats.py", "radar/repo/visibility.py"):
        assert any(_COUNTED_PAPER.search(text) for text, _ in _literals(sources[relative]))


def test_stats_modules_do_not_query_paper() -> None:
    sources = _radar_sources()

    assert "radar/repo/stats.py" in sources
    assert stats_module_problems(sources) == []


def test_other_repo_modules_do_not_aggregate_paper() -> None:
    sources = _radar_sources()

    assert paper_aggregate_problems(sources) == []
    # 掃描真的看得到 papers.py 讀 paper 表（只是沒有彙總）。
    assert any(_paper_reads(text) for text, _ in _literals(sources["radar/repo/papers.py"]))


def test_cube_total_is_counted_exactly_once() -> None:
    sources = _radar_sources()

    assert cube_count_problems(sources) == []
    found = [
        count
        for text, function in _literals(sources["radar/repo/stats.py"])
        if function == "list_cube_counts"
        for count in _row_counts(text)
    ]
    assert found == ["count(*)"]


# --- 假原始碼證明抓得到 -----------------------------------------------------------------------


def test_counted_paper_check_catches_other_modules() -> None:
    sources = {
        "radar/repo/papers.py": 'SQL = "SELECT id FROM counted_paper"',
        "radar/services/trends.py": 'def f(c):\n    return f"SELECT 1 FROM {c} JOIN counted_paper"',
        "radar/repo/upper.py": 'SQL = "select 1 from public.COUNTED_PAPER"',  # 大寫也算
        # 不算：允許的模組、docstring、別的名稱。
        "radar/repo/stats.py": 'SQL = "SELECT 1 FROM counted_paper"',
        "radar/repo/relations.py": 'SQL = "SELECT 1 FROM counted_paper"',
        "radar/repo/keyword_stats.py": 'SQL = "SELECT 1 FROM counted_paper"',
        "radar/repo/visibility.py": 'SQL = "EXISTS (SELECT 1 FROM counted_paper)"',
        "radar/repo/doc.py": '"""讀 counted_paper 的說明。"""',
        "radar/repo/other.py": 'SQL = "SELECT 1 FROM counted_papers_old, not_counted_paper"',
    }

    assert counted_paper_problems(sources) == [
        "radar/repo/papers.py：字串含 counted_paper，統計查詢只准放在統計模組",
        "radar/repo/upper.py：字串含 counted_paper，統計查詢只准放在統計模組",
        "radar/services/trends.py：字串含 counted_paper，統計查詢只准放在統計模組",
    ]


def test_stats_module_check_catches_each_mistake() -> None:
    sources = {
        "radar/repo/stats.py": (
            '"""SELECT * FROM paper"""\n'
            "from radar.repo.papers import get_paper\n"
            'SQL = "SELECT count(*) FROM paper AS p"\n'
            "def f(c):\n"
            '    return f"SELECT 1 FROM counted_paper c JOIN public.\\"paper\\" p ON {c}"'
        ),
        "radar/repo/relations.py": "from radar.repo import papers\nimport radar.repo.papers as p",
        "radar/repo/keyword_stats.py": (
            "from . import papers\nfrom .papers import paper_table\n"
            'SQL = "select 1 from ONLY paper"'
        ),
        # 不算：paper_* 的表、統計模組以外的模組、docstring。
        "radar/repo/weekly_runs.py": 'SQL = "SELECT count(*) FROM paper"',
    }
    sources["radar/repo/stats.py"] += (
        '\nOK = "SELECT 1 FROM counted_paper c JOIN paper_abstract a ON a.paper_id = c.id"'
    )

    assert stats_module_problems(sources) == [
        "radar/repo/keyword_stats.py：直接查 paper 表（from ONLY paper），"
        "統計只從 counted_paper 出發",
        "radar/repo/keyword_stats.py:1：import radar.repo.papers，統計模組不用論文表的函式",
        "radar/repo/keyword_stats.py:2：import radar.repo.papers，統計模組不用論文表的函式",
        "radar/repo/relations.py:1：import radar.repo.papers，統計模組不用論文表的函式",
        "radar/repo/relations.py:2：import radar.repo.papers，統計模組不用論文表的函式",
        "radar/repo/stats.py：直接查 paper 表（FROM paper），統計只從 counted_paper 出發",
        'radar/repo/stats.py：直接查 paper 表（JOIN public."paper"），統計只從 counted_paper 出發',
        "radar/repo/stats.py:2：import radar.repo.papers，統計模組不用論文表的函式",
    ]


def test_paper_aggregate_check_catches_each_mistake() -> None:
    sources = {
        "radar/repo/papers.py": (
            "def count_papers(viewer, c):\n"
            '    return "SELECT count(*) FROM paper WHERE in_scope"\n'
            "def list_years(viewer, c):\n"
            '    return f"SELECT publication_year FROM paper p WHERE {c} GROUP BY 1"\n'
        ),
        "radar/repo/trends.py": (
            'SQL = """SELECT sum(cited_by_count)\n  FROM public.paper"""\n'
            'AVG = "SELECT AVG (cited_by_count) FROM x JOIN paper ON true"'
        ),
        # 不算：paper_* 的表、沒有彙總、寫入、統計模組、repo 以外、docstring。
        "radar/repo/abstracts.py": 'SQL = "SELECT count(*) FROM paper_abstract GROUP BY source"',
        "radar/repo/reader.py": 'SQL = "SELECT id FROM paper WHERE id = :id"',
        "radar/repo/writer.py": 'SQL = "DELETE FROM paper WHERE id IN (SELECT max(x) FROM y)"',
        "radar/repo/stats.py": 'SQL = "SELECT count(*) FROM paper"',
        "radar/services/x.py": 'SQL = "SELECT count(*) FROM paper"',
        "radar/repo/doc.py": '"""SELECT count(*) FROM paper"""',
    }

    problems = paper_aggregate_problems(sources)

    assert [problem.split("：")[0] for problem in problems] == [
        "radar/repo/papers.py.count_papers",
        "radar/repo/papers.py.list_years",
        "radar/repo/trends.py",
        "radar/repo/trends.py",
    ], problems


def test_cube_count_check_catches_each_mistake() -> None:
    sources = {
        "radar/repo/stats.py": (
            "def list_cube_counts(viewer, c):\n"
            '    return "SELECT a.source, count(*) FROM counted_paper AS c LEFT JOIN x"\n'
            "def count_more(viewer, c):\n"
            '    return "SELECT count(1) FROM counted_paper"\n'
        ),
        "radar/repo/relations.py": (
            "def list_relations(viewer, c):\n"
            '    return f"SELECT count(DISTINCT cp.id) FROM counted_paper cp WHERE {c}"\n'
            'TOTAL = "SELECT COUNT( counted_paper.id ) FROM counted_paper"\n'
        ),
        "radar/repo/keyword_stats.py": (
            "def list_keywords(viewer, alias):\n"
            '    return f"SELECT count({alias}.id) FROM counted_paper AS {alias}"\n'
        ),
        # 不算：數別的表的列、沒有讀 counted_paper 的字串。
        "radar/repo/other.py": (
            'A = "SELECT count(a.id) FROM counted_paper c JOIN paper_abstract a ON true"\n'
            'B = "SELECT count(*) FROM weekly_run"\n'
            'C = "SELECT count(c.id) FROM counted_paper WHERE x"\n'
        ),
    }

    problems = cube_count_problems(sources)

    assert [problem.split("：")[0] for problem in problems] == [
        "radar/repo/keyword_stats.py.list_keywords",
        "radar/repo/relations.py.list_relations",
        "radar/repo/relations.py",
        "radar/repo/stats.py.count_more",
    ], problems


def test_cube_count_check_requires_exactly_one_at_home() -> None:
    none = {"radar/repo/stats.py": 'def list_cube_counts(viewer, c):\n    return "SELECT 1"\n'}
    two = {
        "radar/repo/stats.py": (
            "def list_cube_counts(viewer, c):\n"
            '    a = "SELECT count(*) FROM counted_paper"\n'
            '    return a, "SELECT count(*) FROM counted_paper AS c"\n'
        )
    }

    assert cube_count_problems(none) == [
        "radar/repo/stats.py.list_cube_counts：找不到數 counted_paper 整列的計數"
    ]
    assert cube_count_problems(two) == [
        "radar/repo/stats.py.list_cube_counts：數 counted_paper 整列的計數有 2 個，只准一個"
    ]
