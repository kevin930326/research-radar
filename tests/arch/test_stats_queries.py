"""統計查詢只從 view `counted_paper` 出發、只放在統計模組（AD-5、ADR-0011「哪些論文算數」、
ADR-0027）。

統計模組＝`radar/repo/stats.py`，以及之後的 `radar/repo/relations.py`、`radar/repo/keyword_stats.py`
（存在的才掃）。掃 `radar/` 每個 .py 檔的字串（f-string 的變數換成佔位字元、不含 docstring）：

1. 統計模組以外，同一個字串引用 `counted_paper` 又做彙總（`count(`、`sum(`、`avg(`、`GROUP BY`）
   就失敗。只引用、不彙總的可以（ADR-0011「點數字列出論文」：論文清單用同樣條件、從
   `counted_paper` 查；`visibility.py` 的 `PublicViewer` 條件也是）。
2. 統計模組的 SQL 不讀 `paper` 表（`FROM paper`、`JOIN paper`、`FROM x, paper`；`paper_abstract`
   這類 `paper_*` 不算），也不 import `radar.repo.papers`。
3. 統計模組以外的 `radar/repo` 模組，同一句 SQL 讀 `paper` 表又「數篇數」就失敗：有 `count(`、
   `sum(`、`avg(`，沒有 `HAVING`，而且沒有 `GROUP BY`（整張表一個數字）或 `GROUP BY` 裡有
   `publication_year`、`journal_issn_l`（依年份、期刊數）。依別的欄位分組再用 `HAVING` 篩
   （例如找重複標題）不算。
4. 數 `counted_paper` 全部篇數的查詢只准是 `radar/repo/stats.py` 的 `list_cube_counts`，而且剛好
   一個。「全部篇數」＝`FROM` 只有 `counted_paper`（頂多再 `LEFT JOIN paper_abstract`），`WHERE`
   只有變數組進來的條件（可見條件）或只篩 `counted_paper` 自己的期刊、年份欄位，`SELECT` 數整列
   （`count(*)`、`count(1)`、`count(id)`、`count(<別名>.id)`、`count(DISTINCT …id)`）。
   其他計數（`pending_classification`、`topic`、`topic_pair`、`method`）要 `JOIN` 分類的表，或在
   `WHERE` 寫分類的條件，就不是全部篇數。

每條規則都用故意寫錯的假原始碼證明抓得到，也用正當的寫法證明不會誤擋。
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
# 一個子句到哪裡結束：下一個子句的關鍵字、右括號、分號或字串結尾。
_CLAUSE_END = (
    r"(?=\b(?:WHERE|GROUP|ORDER|LIMIT|OFFSET|HAVING|WINDOW|UNION|EXCEPT|INTERSECT|RETURNING"
    r"|FOR)\b|[;)]|\Z)"
)
# `FROM` 後面到下一個子句之間（逗號接法 `FROM a, paper p` 要在這裡面找）。
_FROM_LIST = re.compile(rf"(\bDELETE\s+)?\bFROM\s+(.+?){_CLAUSE_END}", re.IGNORECASE | re.DOTALL)
_PAPER_ITEM = re.compile(rf'\s*({_SCHEMA}"?paper"?)(?![\w.])', re.IGNORECASE)
_AGGREGATE = re.compile(r"\b(?:count|sum|avg)\s*\(|\bGROUP\s+BY\b", re.IGNORECASE)
_COUNTING = re.compile(r"\b(?:count|sum|avg)\s*\(", re.IGNORECASE)
_HAVING = re.compile(r"\bHAVING\b", re.IGNORECASE)
_GROUP_BY = re.compile(
    r"\bGROUP\s+BY\s+(.+?)"
    r"(?=\b(?:HAVING|ORDER|LIMIT|OFFSET|WINDOW|UNION|EXCEPT|INTERSECT|FOR)\b|[;)]|\Z)",
    re.IGNORECASE | re.DOTALL,
)
# 數篇數時分組用的維度欄位（`counted_paper` 自己也只有這兩個維度）。
_DIMENSIONS = frozenset({"publication_year", "journal_issn_l"})
_DIMENSION = re.compile(r'(?<!\w)"?(?:publication_year|journal_issn_l)"?(?!\w)', re.IGNORECASE)
_DIMENSION_ITEM = re.compile(
    r'(?:"?[A-Za-z_]\w*"?\.)?"?(?:publication_year|journal_issn_l)"?', re.IGNORECASE
)
# `FROM counted_paper`；後面的別名、`LEFT JOIN paper_abstract` 另外看。
_COUNTED_FROM = re.compile(rf'\bFROM\s+{_SCHEMA}"?counted_paper"?(?!\w)', re.IGNORECASE)
_FROM_TAIL = re.compile(rf"(.*?){_CLAUSE_END}", re.IGNORECASE | re.DOTALL)
# `counted_paper` 後面只准有別名，頂多再左接 `paper_abstract`（ON 裡不能再 JOIN、不能有逗號）。
_ONLY_ABSTRACT = re.compile(
    rf'\s*(?:(?:AS\s+)?"?(?P<alias>[A-Za-z_]\w*|{_PLACEHOLDER})"?)?'
    rf'(?:\s+LEFT\s+(?:OUTER\s+)?JOIN\s+{_SCHEMA}"?paper_abstract"?'
    r'(?:\s+(?:AS\s+)?"?[A-Za-z_]\w*"?)?\s+ON\s+(?:(?!\bJOIN\b)[^,])*)?\s*',
    re.IGNORECASE | re.DOTALL,
)
_WHERE = re.compile(
    r"\s*WHERE\s+(.*?)"
    r"(?=\b(?:GROUP|ORDER|LIMIT|OFFSET|HAVING|WINDOW|UNION|EXCEPT|INTERSECT|RETURNING|FOR)\b"
    r"|[;)]|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_STRING = re.compile(r"'(?:[^']|'')*'")
_PARAMETER = re.compile(r"(?<!:):[A-Za-z_]\w*")
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_FROM_WORD = re.compile(r"\bFROM\b", re.IGNORECASE)
# 只篩 `counted_paper` 自己的期刊、年份的 WHERE 會用到的字。
_FILTER_WORDS = (
    frozenset({"and", "or", "not", "in", "is", "null", "true", "false", "between", "any"})
    | _DIMENSIONS
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


def _where(relative: str, function: str | None) -> str:
    return f"{relative}.{function}" if function else relative


def _short(literal: str) -> str:
    return " ".join(literal.split())[:80]


def _paper_reads(literal: str) -> list[str]:
    """字串裡讀 `paper` 表的地方：`FROM paper`、`JOIN paper`，以及逗號接法 `FROM x, paper`。"""
    found = [
        " ".join(match.group(0).split())
        for match in _READS_PAPER.finditer(literal)
        if match.group(1) is None
    ]
    for match in _FROM_LIST.finditer(literal):
        if match.group(1) is not None:
            continue
        for item in match.group(2).split(",")[1:]:
            hit = _PAPER_ITEM.match(item)
            if hit:
                found.append(f", {' '.join(hit.group(1).split())}")
    return found


# --- 1. 統計模組以外不彙總 counted_paper ---------------------------------------------------------


def counted_paper_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（相對路徑 → 原始碼）裡，統計模組以外引用 `counted_paper` 又做彙總的字串。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        if relative in STATS_MODULES:
            continue
        for literal, function in _literals(source):
            if _COUNTED_PAPER.search(literal) and _AGGREGATE.search(literal):
                problems.append(
                    f"{_where(relative, function)}：引用 counted_paper 又做彙總"
                    f"（{_short(literal)}），統計查詢只准放在統計模組"
                )
    return problems


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


# --- 3. 其他 repo 模組不數 paper 的篇數 -----------------------------------------------------------


def _counts_papers(literal: str) -> bool:
    """字串是不是「數篇數」的形狀（見模組說明第 3 條）。"""
    if not _COUNTING.search(literal) or _HAVING.search(literal):
        return False
    groups = list(_GROUP_BY.finditer(literal))
    if not groups:
        return True
    for group in groups:
        for item in group.group(1).split(","):
            item = item.strip()
            if _DIMENSION_ITEM.fullmatch(item):
                return True
            # `GROUP BY 1, 2`：前面（SELECT 那一段）有維度欄位就當成依維度分組。
            if item.isdigit() and _DIMENSION.search(literal[: group.start()]):
                return True
    return False


def paper_aggregate_problems(sources: Mapping[str, str]) -> list[str]:
    """統計模組以外的 `radar/repo` 模組，同一句 SQL 讀 `paper` 又數篇數的地方。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        if not relative.startswith("radar/repo/") or relative in STATS_MODULES:
            continue
        for literal, function in _literals(source):
            if _paper_reads(literal) and _counts_papers(literal):
                problems.append(
                    f"{_where(relative, function)}：讀 paper 又數篇數（{_short(literal)}），"
                    "篇數只准由統計模組從 counted_paper 算"
                )
    return problems


# --- 4. counted_paper 的全部篇數只有一個查詢 -----------------------------------------------------


def _only_filters_dimensions(where: str, aliases: set[str]) -> bool:
    """WHERE 只有變數組進來的條件，或只篩 `counted_paper` 自己的期刊、年份。"""
    rest = _PARAMETER.sub(" ", _STRING.sub(" ", where.replace(_PLACEHOLDER, " ")))
    allowed = _FILTER_WORDS | {alias.lower() for alias in aliases}
    return all(word.lower() in allowed for word in _IDENTIFIER.findall(rest))


def _row_counts(literal: str) -> list[str]:
    """字串裡數 `counted_paper` 全部篇數的計數（見模組說明第 4 條）。"""
    found: list[str] = []
    for source in _COUNTED_FROM.finditer(literal):
        rest = literal[source.end() :]
        tail_match = _FROM_TAIL.match(rest)
        tail = tail_match.group(1) if tail_match else rest
        shape = _ONLY_ABSTRACT.fullmatch(tail)
        if shape is None:
            continue  # 接了別的表：分類、主題…的計數
        aliases = {"counted_paper"}
        alias = shape.group("alias")
        if alias is not None:
            if alias.lower() in _NOT_ALIASES:
                continue
            aliases.add(alias)
        where = _WHERE.match(rest[len(tail) :])
        if where is not None and not _only_filters_dimensions(where.group(1), aliases):
            continue  # WHERE 有分類之類的條件
        # 這個 FROM 前面、上一個 FROM 之後那一段（SELECT 的欄位）裡數整列的計數。
        before = literal[: source.start()]
        previous = list(_FROM_WORD.finditer(before))
        select = before[previous[-1].end() :] if previous else before
        named = "|".join(re.escape(name) for name in sorted(aliases))
        pattern = re.compile(
            rf'\bcount\s*\(\s*(?:\*|1|(?:DISTINCT\s+)?(?:"?(?:{named})"?\.)?"?id"?)\s*\)',
            re.IGNORECASE,
        )
        found += [" ".join(match.group(0).split()) for match in pattern.finditer(select)]
    return found


def cube_count_problems(sources: Mapping[str, str]) -> list[str]:
    """`list_cube_counts` 以外數 `counted_paper` 全部篇數的地方；它裡面不是剛好一個也算。"""
    problems: list[str] = []
    at_home = 0
    for relative, source in sorted(sources.items()):
        for literal, function in _literals(source):
            for found in _row_counts(literal):
                if (relative, function) == CUBE_COUNT_HOME:
                    at_home += 1
                    continue
                problems.append(
                    f"{_where(relative, function)}：數 counted_paper 全部篇數（{found}），"
                    "每年全部篇數只准由 repo/stats.py 的 list_cube_counts 數"
                )
    home = ".".join(CUBE_COUNT_HOME)
    if at_home == 0:
        problems.append(f"{home}：找不到數 counted_paper 全部篇數的計數")
    elif at_home > 1:
        problems.append(f"{home}：數 counted_paper 全部篇數的計數有 {at_home} 個，只准一個")
    return problems


# --- 真的原始碼 ---------------------------------------------------------------------------


def test_counted_paper_aggregates_only_in_stats_modules() -> None:
    sources = _radar_sources()

    assert counted_paper_problems(sources) == []
    # 掃描真的看得到統計模組與 visibility.py 引用它。
    for relative in ("radar/repo/stats.py", "radar/repo/visibility.py"):
        assert any(_COUNTED_PAPER.search(text) for text, _ in _literals(sources[relative]))


def test_stats_modules_do_not_query_paper() -> None:
    sources = _radar_sources()

    assert "radar/repo/stats.py" in sources
    assert stats_module_problems(sources) == []


def test_other_repo_modules_do_not_count_paper() -> None:
    sources = _radar_sources()

    assert paper_aggregate_problems(sources) == []
    # 掃描真的看得到 papers.py 讀 paper 表（只是沒有數篇數）。
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
        "radar/repo/papers.py": (
            "def count_list(viewer, c):\n"
            '    return "SELECT count(*) FROM paper p JOIN counted_paper c ON c.id = p.id"\n'
        ),
        "radar/services/trends.py": (
            'def f(c):\n    return f"SELECT 1 FROM {c} JOIN counted_paper GROUP BY 1"'
        ),
        "radar/repo/upper.py": 'SQL = "select SUM(x) from public.COUNTED_PAPER"',  # 大寫也算
        # 不算：統計模組、只引用不彙總、docstring、別的名稱。
        "radar/repo/stats.py": 'SQL = "SELECT count(*) FROM counted_paper"',
        "radar/repo/relations.py": 'SQL = "SELECT count(*) FROM counted_paper"',
        "radar/repo/keyword_stats.py": 'SQL = "SELECT avg(x) FROM counted_paper"',
        "radar/repo/visibility.py": 'SQL = "EXISTS (SELECT 1 FROM counted_paper)"',
        "radar/repo/doc.py": '"""SELECT count(*) FROM counted_paper"""',
        "radar/repo/other.py": 'SQL = "SELECT count(*) FROM counted_papers_old, not_counted_paper"',
    }

    problems = counted_paper_problems(sources)

    assert [problem.split("：")[0] for problem in problems] == [
        "radar/repo/papers.py.count_list",
        "radar/repo/upper.py",
        "radar/services/trends.py.f",
    ], problems


def test_counted_paper_check_allows_paper_list_join() -> None:
    """ADR-0011：論文清單用同樣條件、從 counted_paper 查，只要不彙總就不擋。"""
    sources = {
        "radar/repo/papers.py": (
            "def list_papers(viewer, condition):\n"
            '    return f"""SELECT p.id, p.title, p.publication_year\n'
            "      FROM paper AS p JOIN counted_paper AS c ON c.id = p.id\n"
            "      WHERE {condition} AND c.journal_issn_l = ANY(:issns)\n"
            '      ORDER BY p.publication_year DESC, p.id LIMIT :limit"""\n'
        ),
        "radar/repo/visibility.py": (
            'SQL = "EXISTS (SELECT 1 FROM counted_paper AS visible_counted'
            ' WHERE visible_counted.id = x.id)"'
        ),
    }

    assert counted_paper_problems(sources) == []
    assert paper_aggregate_problems(sources) == []


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


def test_stats_module_check_catches_comma_join() -> None:
    sources = {
        "radar/repo/stats.py": (
            'A = "SELECT count(*) FROM counted_paper c, paper p WHERE p.id = c.id"\n'
            'B = "SELECT 1 FROM counted_paper c JOIN x ON x.id = c.id, public.paper"\n'
            # 不算：paper_* 的表、欄位名稱、函式裡的逗號。
            'C = "SELECT c.id, paper_id FROM counted_paper c, paper_abstract a"\n'
            'D = "SELECT coalesce(a.x, b.y) FROM counted_paper c"\n'
        ),
    }

    assert stats_module_problems(sources) == [
        "radar/repo/stats.py：直接查 paper 表（, paper），統計只從 counted_paper 出發",
        "radar/repo/stats.py：直接查 paper 表（, public.paper），統計只從 counted_paper 出發",
    ]


def test_paper_aggregate_check_catches_each_mistake() -> None:
    sources = {
        "radar/repo/papers.py": (
            "def count_papers(viewer, c):\n"
            '    return "SELECT count(*) FROM paper WHERE in_scope"\n'
            "def list_years(viewer, c):\n"
            '    return f"SELECT publication_year, count(*) FROM paper p WHERE {c} GROUP BY 1"\n'
            "def by_journal(viewer, c):\n"
            '    return "SELECT count(id) FROM paper AS p GROUP BY p.journal_issn_l"\n'
            "def by_comma(viewer, c):\n"
            '    return "SELECT count(*) FROM journal j, paper p WHERE p.x = j.x"\n'
        ),
        "radar/repo/trends.py": (
            'SQL = """SELECT sum(cited_by_count)\n  FROM public.paper"""\n'
            'AVG = "SELECT AVG (cited_by_count) FROM x JOIN paper ON true"'
        ),
        # 不算：paper_* 的表、沒有數篇數、寫入、統計模組、repo 以外、docstring。
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
        "radar/repo/papers.py.by_journal",
        "radar/repo/papers.py.by_comma",
        "radar/repo/trends.py",
        "radar/repo/trends.py",
    ], problems


def test_paper_aggregate_check_allows_duplicate_titles() -> None:
    """依標題分組再用 HAVING 找重複、只列出年份（不數），都不是數篇數。"""
    sources = {
        "radar/repo/papers.py": (
            "def find_duplicates(viewer, c):\n"
            '    return """SELECT title_norm, count(*) FROM paper\n'
            '      GROUP BY title_norm HAVING count(*) > 1"""\n'
            "def find_duplicates_in_journal(viewer, c):\n"
            '    return """SELECT journal_issn_l, title_norm FROM paper\n'
            '      GROUP BY journal_issn_l, title_norm HAVING count(*) > 1"""\n'
            "def list_years(viewer, c):\n"
            '    return "SELECT publication_year FROM paper GROUP BY 1"\n'
            "def list_by_title(viewer, c):\n"
            '    return "SELECT title_norm, count(*) FROM paper GROUP BY title_norm"\n'
        ),
    }

    assert paper_aggregate_problems(sources) == []


def test_cube_count_check_catches_each_mistake() -> None:
    sources = {
        "radar/repo/stats.py": (
            "def list_cube_counts(viewer, c):\n"
            '    return f"""SELECT c.journal_issn_l, c.publication_year, a.source, count(*)\n'
            "      FROM counted_paper AS c\n"
            "      LEFT JOIN paper_abstract AS a ON a.paper_id = c.id AND a.is_current\n"
            '      WHERE {c} GROUP BY 1, 2, 3"""\n'
            "def count_more(viewer, c):\n"
            '    return "SELECT count(1) FROM counted_paper"\n'
            "def count_with_abstract(viewer, c):\n"
            '    return f"""SELECT count(*) FROM counted_paper c\n'
            '      LEFT OUTER JOIN paper_abstract a ON a.paper_id = c.id WHERE {c}"""\n'
        ),
        "radar/repo/relations.py": (
            "def list_relations(viewer, c):\n"
            '    return f"SELECT count(DISTINCT cp.id) FROM counted_paper cp WHERE {c}"\n'
            'TOTAL = "SELECT COUNT( counted_paper.id ) FROM counted_paper"\n'
            'BARE = "SELECT count(id) FROM counted_paper"\n'
            "def year_total(viewer, c):\n"
            '    return f"""SELECT count(*) FROM counted_paper c\n'
            '      WHERE c.publication_year = :year AND {c}"""\n'
        ),
        "radar/repo/keyword_stats.py": (
            "def list_keywords(viewer, alias):\n"
            '    return f"SELECT count({alias}.id) FROM counted_paper AS {alias}"\n'
        ),
    }

    problems = cube_count_problems(sources)

    assert [problem.split("：")[0] for problem in problems] == [
        "radar/repo/keyword_stats.py.list_keywords",
        "radar/repo/relations.py.list_relations",
        "radar/repo/relations.py",
        "radar/repo/relations.py",
        "radar/repo/relations.py.year_total",
        "radar/repo/stats.py.count_more",
        "radar/repo/stats.py.count_with_abstract",
    ], problems


def test_cube_count_check_allows_classification_counts() -> None:
    """AD-19 另外四種計數：counted_paper 加 JOIN 或分類的 WHERE 再 count，不是全部篇數。"""
    sources = {
        "radar/repo/stats.py": (
            "def list_cube_counts(viewer, c):\n"
            '    return f"""SELECT c.journal_issn_l, c.publication_year, a.source, count(*)\n'
            "      FROM counted_paper AS c\n"
            "      LEFT JOIN paper_abstract AS a ON a.paper_id = c.id AND a.is_current\n"
            '      WHERE {c} GROUP BY 1, 2, 3"""\n'
            "def list_topic_counts(viewer, c):\n"
            '    return f"""SELECT c.journal_issn_l, c.publication_year, t.topic, count(*)\n'
            "      FROM counted_paper AS c JOIN paper_topic AS t ON t.paper_id = c.id\n"
            '      WHERE {c} AND t.is_valid GROUP BY 1, 2, 3"""\n'
            "def list_topic_pair_counts(viewer, c):\n"
            '    return f"""SELECT a.topic, b.topic, count(DISTINCT c.id)\n'
            "      FROM counted_paper c\n"
            "      JOIN paper_topic a ON a.paper_id = c.id\n"
            "      JOIN paper_topic b ON b.paper_id = c.id AND a.topic < b.topic\n"
            '      WHERE {c} GROUP BY 1, 2"""\n'
            "def list_method_counts(viewer, c):\n"
            '    return f"""SELECT m.method, count(*) FROM counted_paper c, paper_method m\n'
            '      WHERE m.paper_id = c.id AND {c} GROUP BY 1"""\n'
            "def list_pending_counts(viewer, c):\n"
            '    return f"""SELECT c.journal_issn_l, c.publication_year, count(*)\n'
            "      FROM counted_paper AS c\n"
            "      WHERE {c} AND NOT EXISTS (\n"
            "        SELECT 1 FROM paper_classification AS x WHERE x.paper_id = c.id)\n"
            '      GROUP BY 1, 2"""\n'
            "def list_pending_left_join(viewer, c):\n"
            '    return f"""SELECT count(*) FROM counted_paper c\n'
            "      LEFT JOIN paper_classification x ON x.paper_id = c.id\n"
            '      WHERE x.id IS NULL AND {c}"""\n'
            "def list_abstract_counts(viewer, c):\n"
            '    return f"""SELECT count(a.id) FROM counted_paper c\n'
            '      LEFT JOIN paper_abstract a ON a.paper_id = c.id WHERE {c}"""\n'
        ),
        "radar/repo/relations.py": (
            "def count_topics(viewer, c):\n"
            '    return """SELECT count(*) FROM paper_topic t\n'
            '      WHERE t.paper_id IN (SELECT id FROM counted_paper)"""\n'
        ),
    }

    assert cube_count_problems(sources) == []
    assert counted_paper_problems(sources) == []


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
        "radar/repo/stats.py.list_cube_counts：找不到數 counted_paper 全部篇數的計數"
    ]
    assert cube_count_problems(two) == [
        "radar/repo/stats.py.list_cube_counts：數 counted_paper 全部篇數的計數有 2 個，只准一個"
    ]
