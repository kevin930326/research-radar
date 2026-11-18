"""統計查詢只從 view `counted_paper` 出發、只放在統計模組（AD-5、ADR-0011「哪些論文算數」、
ADR-0027）。

統計模組＝`radar/repo/stats.py`，以及之後的 `radar/repo/relations.py`、`radar/repo/keyword_stats.py`
（存在的才掃）。掃 `radar/` 每個 .py 檔的字串（f-string 的變數換成佔位字元、不含 docstring、
SQL 註解 `--`、`/* */` 先去掉）：

1. 統計模組以外，同一個字串引用 `counted_paper` 又做彙總（`count(`、`sum(`、`avg(`、`GROUP BY`）
   就失敗。只引用、不彙總的可以（ADR-0011「點數字列出論文」：論文清單用同樣條件、從
   `counted_paper` 查；`visibility.py` 的 `PublicViewer` 條件也是）。
2. 統計模組的 SQL 不讀 `paper` 表（`FROM paper`、`JOIN paper`、`FROM x, paper`；`paper_abstract`
   這類 `paper_*` 不算），也不 import `radar.repo.papers`。
3. 統計模組以外的 `radar/repo` 模組，同一句 SQL 讀 `paper` 表又「數篇數」就失敗：有 `count(`、
   `sum(`、`avg(`，而且沒有 `GROUP BY`（整張表一個數字），或 `GROUP BY` 提到 `publication_year`、
   `journal_issn_l`（依年份、期刊數；`GROUP BY 1` 這種寫位置的，SELECT 那一段有這兩個欄位也算）。
   有沒有 `HAVING` 都一樣。只依別的欄位分組（例如 `GROUP BY title_norm HAVING count(*) > 1` 找
   重複標題）不算。
4. 數 `counted_paper` 全部篇數的查詢只准是 `radar/repo/stats.py` 的 `list_cube_counts`，而且剛好
   一個。「全部篇數」＝一個 `SELECT` 的 `FROM` 有 `counted_paper`，`SELECT` 數整列（`count(*)`、
   `count(1)`、`count(id)`、`count(<別名>.id)`、`count(<別名>.*)`、`count(DISTINCT …id)`），而且
   `FROM` 到 `WHERE` 結束（含裡面的子查詢）沒有出現分類：名稱含 `classification`、`topic`、
   `method` 的表或欄位（字串、註解裡的不算）。其他計數（`pending_classification`、`topic`、
   `topic_pair`、`method`）要 `JOIN` 分類的表，或在 `WHERE` 寫分類的條件（例如
   `NOT EXISTS (SELECT 1 FROM <分類表> …)`），才不是全部篇數；接別的表、加別的條件都還是。

**登記的例外只有一個**：`radar/repo/paper_list.py` 的 `count_paper_list`（`LIST_COUNT_HOME`，Story
3.13）。規則 1、3、4 對這一個（模組, 函式）放行，同一個模組別的函式照樣檢查；它也不算進規則 4 的
「`list_cube_counts` 剛好一個」。

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
# 找論文清單的總數（Story 3.13）：清單要給 `total`（Conventions「API」），而且 `tracked` 要從
# `counted_paper` 查、`lab`／`personal` 要讀 `paper`（ADR-0011「點數字列出論文」）。它數的是清單
# 本身，不是計數方塊；和計數方塊的每一格對得上由 `tests/integration/test_paper_list_click_back.py`
# 守著。
# 只放行這一個（模組, 函式），不拆字串、不在 Python 數筆數繞過。
LIST_COUNT_HOME = ("radar/repo/paper_list.py", "count_paper_list")
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
_GROUP_BY = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)
# 數篇數時分組用的維度欄位（`counted_paper` 自己也只有這兩個維度）。
_DIMENSION = re.compile(r'(?<!\w)"?(?:publication_year|journal_issn_l)"?(?!\w)', re.IGNORECASE)
_POSITION = re.compile(r"(?<![\w.])\d+(?![\w.])")
# 子句在同一層括號裡到哪裡結束。
_GROUP_BY_END = re.compile(
    r"\b(?:HAVING|ORDER|LIMIT|OFFSET|WINDOW|UNION|EXCEPT|INTERSECT|FOR)\b|;", re.IGNORECASE
)
_SET_OPERATOR = re.compile(r"\b(?:UNION|EXCEPT|INTERSECT)\b", re.IGNORECASE)
_FROM_WORD = re.compile(r"\bFROM\b", re.IGNORECASE)
_FROM_END = re.compile(
    r"\b(?:WHERE|GROUP|HAVING|ORDER|LIMIT|OFFSET|WINDOW|FOR|RETURNING)\b|;", re.IGNORECASE
)
_WHERE_END = re.compile(
    r"\b(?:GROUP|HAVING|ORDER|LIMIT|OFFSET|WINDOW|FOR|RETURNING)\b|;", re.IGNORECASE
)
_STRING = re.compile(r"'(?:[^']|'')*'")
# 分類的表或欄位：名稱含 classification、topic、method（AD-19 另外四種計數都要碰到其中一種）。
_CLASSIFICATION = re.compile(
    r"(?<![\w$])[A-Za-z_]*(?:classification|topic|method)\w*", re.IGNORECASE
)
# `counted_paper` 後面的別名（`AS c`、`c`、f-string 變數）。
_ALIAS_AFTER = re.compile(rf'"?\s+(?:AS\s+)?"?([A-Za-z_]\w*|{_PLACEHOLDER})', re.IGNORECASE)
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


def _strip_comments(sql: str) -> str:
    """去掉 SQL 註解（`--` 到行尾、`/* */`，可以巢狀），各換成一個空白。

    單引號字串、雙引號名稱裡的 `--`、`/*` 不是註解，原樣留著。
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        char = sql[i]
        if char in "'\"":
            end = i + 1
            while end < n:
                if sql[end] == char:
                    if sql.startswith(char * 2, end):
                        end += 2
                        continue
                    break
                end += 1
            out.append(sql[i : end + 1])
            i = end + 1
        elif sql.startswith("--", i):
            newline = sql.find("\n", i)
            i = n if newline < 0 else newline
            out.append(" ")
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            out.append(" ")
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _blank_strings(sql: str) -> str:
    """單引號字串的內容換成空白（長度不變），裡面的括號、名稱不算數。"""
    return _STRING.sub(lambda match: "'" + " " * (len(match.group(0)) - 2) + "'", sql)


def _depths(sql: str) -> list[int]:
    """每個字元在第幾層括號裡（括號本身算外層）。"""
    found: list[int] = []
    depth = 0
    for char in sql:
        if char == ")":
            depth = max(depth - 1, 0)
        found.append(depth)
        if char == "(":
            depth += 1
    return found


def _flat(sql: str, depths: list[int], level: int) -> str:
    """只留第 `level` 層的字元，更裡層的換成空白（長度不變，位置對得上）。"""
    return "".join(char if depth == level else " " for char, depth in zip(sql, depths, strict=True))


def _scope(depths: list[int], at: int) -> tuple[int, int]:
    """`at` 所在那一層括號的範圍（不含括號本身）；最外層是整個字串。"""
    level = depths[at]
    start = at
    while start > 0 and depths[start - 1] >= level:
        start -= 1
    end = at
    while end < len(depths) and depths[end] >= level:
        end += 1
    return start, end  # 左右括號本身是外層，不在範圍裡


def _first(pattern: re.Pattern[str], flat: str, start: int, end: int) -> int:
    """`flat[start:end]` 裡 `pattern` 第一次出現的位置；沒有就是 `end`。"""
    match = pattern.search(flat, start, end)
    return match.start() if match else end


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

    f-string 的變數換成佔位字元；docstring 不算；SQL 註解去掉。
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
            found.append((_strip_comments("".join(parts)), function))
            for value in node.values:  # 變數裡的運算式也可能有字串
                if isinstance(value, ast.FormattedValue):
                    visit(value.value, function)
            return
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.append((_strip_comments(node.value), function))
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
            if (relative, function) == LIST_COUNT_HOME:
                continue
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
    sql = _blank_strings(literal)
    if not _COUNTING.search(sql):
        return False
    groups = list(_GROUP_BY.finditer(sql))
    if not groups:
        return True
    depths = _depths(sql)
    for group in groups:
        start, end = _scope(depths, group.start())
        flat = _flat(sql, depths, depths[group.start()])
        clause = sql[group.end() : _first(_GROUP_BY_END, flat, group.end(), end)]
        if _DIMENSION.search(clause):
            return True
        # `GROUP BY 1, 2`：SELECT 那一段有維度欄位就當成依維度分組。
        if _POSITION.search(clause) and _DIMENSION.search(sql[start : group.start()]):
            return True
    return False


def paper_aggregate_problems(sources: Mapping[str, str]) -> list[str]:
    """統計模組以外的 `radar/repo` 模組，同一句 SQL 讀 `paper` 又數篇數的地方。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        if not relative.startswith("radar/repo/") or relative in STATS_MODULES:
            continue
        for literal, function in _literals(source):
            if (relative, function) == LIST_COUNT_HOME:
                continue
            if _paper_reads(literal) and _counts_papers(literal):
                problems.append(
                    f"{_where(relative, function)}：讀 paper 又數篇數（{_short(literal)}），"
                    "篇數只准由統計模組從 counted_paper 算"
                )
    return problems


# --- 4. counted_paper 的全部篇數只有一個查詢 -----------------------------------------------------


def _row_count_pattern(aliases: set[str]) -> re.Pattern[str]:
    named = "|".join(re.escape(name) for name in sorted(aliases))
    return re.compile(
        rf'\bcount\s*\(\s*(?:\*|1|(?:DISTINCT\s+)?(?:"?(?:{named})"?\.)?(?:\*|"?id"?))\s*\)',
        re.IGNORECASE,
    )


def _row_counts(literal: str) -> list[str]:
    """字串裡數 `counted_paper` 全部篇數的計數（見模組說明第 4 條）。

    一個一個 `SELECT` 看（括號裡的子查詢、`UNION` 的每一段分開）：`FROM` 有 `counted_paper`、
    SELECT 欄位數整列，`FROM` 到 `WHERE` 結束沒有分類的表或欄位，就是全部篇數。
    """
    sql = _blank_strings(literal)
    depths = _depths(sql)
    seen: set[tuple[int, int]] = set()
    found: list[str] = []
    for mention in _COUNTED_PAPER.finditer(sql):
        level = depths[mention.start()]
        start, end = _scope(depths, mention.start())
        flat = _flat(sql, depths, level)
        # 同一層用 UNION／EXCEPT／INTERSECT 隔開的每一段是不同的 SELECT。
        for operator in _SET_OPERATOR.finditer(flat, start, end):
            if operator.end() <= mention.start():
                start = operator.end()
            else:
                end = operator.start()
                break
        if (start, end) in seen:
            continue
        from_word = _FROM_WORD.search(flat, start, end)
        if from_word is None or from_word.start() > mention.start():
            continue
        from_end = _first(_FROM_END, flat, from_word.end(), end)
        if mention.start() >= from_end:
            continue  # 在 WHERE 之類的地方提到，不是這個 SELECT 的來源
        seen.add((start, end))
        sources = flat[from_word.end() : from_end]
        conditions = sql[from_word.end() : _first(_WHERE_END, flat, from_word.end(), end)]
        if _CLASSIFICATION.search(conditions):
            continue  # FROM 到 WHERE 結束碰到分類的表或欄位：分類的計數
        aliases = {"counted_paper"}
        for source in _COUNTED_PAPER.finditer(sources):
            alias = _ALIAS_AFTER.match(sources, source.end())
            if alias is not None and alias.group(1).lower() not in _NOT_ALIASES:
                aliases.add(alias.group(1))
        select = sql[start : from_word.start()]
        found += [
            " ".join(match.group(0).split())
            for match in _row_count_pattern(aliases).finditer(select)
            if depths[start + match.start()] == level  # 子查詢裡的不算這一段的
        ]
    return found


def cube_count_problems(sources: Mapping[str, str]) -> list[str]:
    """`list_cube_counts` 以外數 `counted_paper` 全部篇數的地方；它裡面不是剛好一個也算。"""
    problems: list[str] = []
    at_home = 0
    for relative, source in sorted(sources.items()):
        for literal, function in _literals(source):
            if (relative, function) == LIST_COUNT_HOME:
                continue
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


def test_list_count_exception_is_really_used() -> None:
    # 例外不是空放著：`count_paper_list` 真的數 `counted_paper`（tracked）與 `paper`
    # （lab、personal）。
    relative, function = LIST_COUNT_HOME
    literals = [text for text, owner in _literals(_radar_sources()[relative]) if owner == function]

    assert any(_row_counts(text) for text in literals)
    assert any(_paper_reads(text) and _counts_papers(text) for text in literals)


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
        # 依年份、期刊分組的計數，加 HAVING、多分一個欄位、括號、註解都還是數篇數。
        "radar/repo/having.py": (
            "def by_year(viewer, c):\n"
            '    return """SELECT publication_year, count(*) FROM paper\n'
            '      GROUP BY publication_year HAVING count(*) > 0"""\n'
            "def by_position(viewer, c):\n"
            '    return "SELECT publication_year, count(*) FROM paper GROUP BY 1 HAVING true"\n'
            "def dupes_in_journal(viewer, c):\n"
            '    return """SELECT journal_issn_l, title_norm FROM paper\n'
            '      GROUP BY journal_issn_l, title_norm HAVING count(*) > 1"""\n'
            "def by_year_flag(viewer, c):\n"
            '    return "SELECT count(*) FROM paper GROUP BY (p.publication_year), is_oa"\n'
            "def by_year_commented(viewer, c):\n"
            '    return """SELECT count(*) FROM paper -- 依標題 title_norm\n'
            '      GROUP BY /* title_norm */ publication_year"""\n'
            "def total_having(viewer, c):\n"
            '    return "SELECT count(*) FROM paper HAVING count(*) > 0"\n'
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
        "radar/repo/having.py.by_year",
        "radar/repo/having.py.by_position",
        "radar/repo/having.py.dupes_in_journal",
        "radar/repo/having.py.by_year_flag",
        "radar/repo/having.py.by_year_commented",
        "radar/repo/having.py.total_having",
        "radar/repo/papers.py.count_papers",
        "radar/repo/papers.py.list_years",
        "radar/repo/papers.py.by_journal",
        "radar/repo/papers.py.by_comma",
        "radar/repo/trends.py",
        "radar/repo/trends.py",
    ], problems


def test_paper_aggregate_check_allows_duplicate_titles() -> None:
    """依標題分組再用 HAVING 找重複、只列出年份（不數），都不是數篇數。

    同一本期刊裡的重複標題要依期刊分組就會被擋（依期刊分組的計數一律算數篇數），改用 EXISTS。
    """
    sources = {
        "radar/repo/papers.py": (
            "def find_duplicates(viewer, c):\n"
            '    return """SELECT title_norm, count(*) FROM paper\n'
            '      GROUP BY title_norm HAVING count(*) > 1"""\n'
            "def find_duplicates_in_journal(viewer, c):\n"
            '    return """SELECT p.id, p.title_norm FROM paper AS p WHERE EXISTS (\n'
            "      SELECT 1 FROM paper AS q WHERE q.title_norm = p.title_norm\n"
            '        AND q.journal_issn_l = p.journal_issn_l AND q.id <> p.id)"""\n'
            "def find_duplicates_commented(viewer, c):\n"
            '    return """SELECT title_norm, count(*) FROM paper\n'
            "      GROUP BY title_norm -- 不依 publication_year\n"
            '      HAVING count(*) > 1"""\n'
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


def test_cube_count_check_catches_disguised_totals() -> None:
    """加註解、型別轉換、括號、接別的表、子查詢不碰分類，都還是全部篇數。"""
    sources = {
        "radar/repo/stats.py": (
            "def list_cube_counts(viewer, c):\n"
            '    return f"SELECT count(*) FROM counted_paper AS c WHERE {c}"\n'
            "def block_comment(viewer, c):\n"
            '    return """SELECT publication_year, count(*) FROM counted_paper\n'
            '      WHERE publication_year >= :year /* total */ GROUP BY publication_year"""\n'
            "def line_comment(viewer, c):\n"
            '    return """SELECT count(*) FROM counted_paper c -- paper_topic\n'
            '      WHERE c.publication_year >= :year"""\n'
            "def cast_call(viewer, c):\n"
            '    return """SELECT count(*) FROM counted_paper\n'
            '      WHERE publication_year >= CAST(:year AS integer)"""\n'
            "def cast_colons(viewer, c):\n"
            '    return "SELECT count(*) FROM counted_paper WHERE publication_year >= :year::int"\n'
            "def string_mentions(viewer, c):\n"
            "    return \"SELECT count(*) FROM counted_paper c WHERE 'paper_topic' <> ''\"\n"
            "def not_exists_other(viewer, c):\n"
            '    return f"""SELECT count(*) FROM counted_paper AS c\n'
            "      WHERE (c.publication_year >= :year) AND NOT EXISTS (\n"
            '        SELECT 1 FROM journal AS j WHERE j.issn_l = c.journal_issn_l) AND {c}"""\n'
            "def join_journal(viewer, c):\n"
            '    return """SELECT j.name, count(c.*) FROM counted_paper c\n'
            '      JOIN journal j ON j.issn_l = c.journal_issn_l GROUP BY 1"""\n'
            "def comma_journal(viewer, c):\n"
            '    return "SELECT count(*) FROM journal j, public.counted_paper c WHERE true"\n'
            "def scalar_subquery(viewer, c):\n"
            '    return "SELECT (SELECT count(*) FROM counted_paper) AS n, 1 AS topic"\n'
            "def union_branch(viewer, c):\n"
            '    return """SELECT count(*) FROM paper_topic\n'
            '      UNION ALL SELECT count(*) FROM counted_paper"""\n'
        ),
    }

    problems = cube_count_problems(sources)

    assert [problem.split("：")[0] for problem in problems] == [
        "radar/repo/stats.py.block_comment",
        "radar/repo/stats.py.line_comment",
        "radar/repo/stats.py.cast_call",
        "radar/repo/stats.py.cast_colons",
        "radar/repo/stats.py.string_mentions",
        "radar/repo/stats.py.not_exists_other",
        "radar/repo/stats.py.join_journal",
        "radar/repo/stats.py.comma_journal",
        "radar/repo/stats.py.scalar_subquery",
        "radar/repo/stats.py.union_branch",
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
            "def list_pending_parenthesized(viewer, c):\n"
            '    return f"""SELECT count(*) FROM counted_paper AS c\n'
            "      WHERE (c.publication_year >= :year) AND NOT EXISTS (\n"
            "        SELECT 1 FROM paper_classification AS x /* 有效分類 */\n"
            '        WHERE (x.paper_id = c.id)) AND {c}"""\n'
            "def list_pending_by_column(viewer, c):\n"
            '    return f"""SELECT count(*) FROM counted_paper AS c\n'
            '      WHERE c.classification_status = :pending AND {c}"""\n'
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


def test_strip_comments_keeps_strings_and_nested_blocks() -> None:
    sql = "SELECT '--x', \"a/*b\" /* 1 /* 2 */ 3 */ FROM t -- z\nWHERE '/*' = '*/'"

    assert _strip_comments(sql) == "SELECT '--x', \"a/*b\"   FROM t  \nWHERE '/*' = '*/'"


def test_list_count_exception_covers_only_count_paper_list() -> None:
    """只有 `paper_list.count_paper_list` 放行；同一個模組別的函式數篇數，規則 1、3、4 照樣抓；
    `list_cube_counts` 仍是唯一數 `counted_paper` 全部篇數的地方。"""
    sources = {
        "radar/repo/stats.py": (
            "def list_cube_counts(viewer, c):\n"
            '    return f"SELECT c.publication_year, count(*) FROM counted_paper AS c WHERE {c}"\n'
        ),
        "radar/repo/paper_list.py": (
            "def count_paper_list(viewer, c):\n"
            '    a = f"SELECT count(*) FROM counted_paper AS listed WHERE {c}"\n'
            '    return a, f"SELECT count(*) FROM paper AS listed WHERE {c}"\n'
            "def list_paper_list_ids(viewer, c):\n"
            '    return f"SELECT count(*) FROM counted_paper AS listed WHERE {c}"\n'
            "def count_more(viewer, c):\n"
            '    return f"SELECT count(*) FROM paper AS listed WHERE {c}"\n'
        ),
    }

    assert [p.split("：")[0] for p in counted_paper_problems(sources)] == [
        "radar/repo/paper_list.py.list_paper_list_ids"
    ]
    assert [p.split("：")[0] for p in paper_aggregate_problems(sources)] == [
        "radar/repo/paper_list.py.count_more"
    ]
    assert [p.split("：")[0] for p in cube_count_problems(sources)] == [
        "radar/repo/paper_list.py.list_paper_list_ids"
    ]


def test_list_count_exception_does_not_count_as_the_cube_total() -> None:
    # 只有清單的總數、沒有 `list_cube_counts` 的計數：仍報「找不到」，例外不能頂替它。
    sources = {
        "radar/repo/stats.py": 'def list_cube_counts(viewer, c):\n    return "SELECT 1"\n',
        "radar/repo/paper_list.py": (
            "def count_paper_list(viewer, c):\n"
            '    return f"SELECT count(*) FROM counted_paper AS listed WHERE {c}"\n'
        ),
    }

    assert cube_count_problems(sources) == [
        "radar/repo/stats.py.list_cube_counts：找不到數 counted_paper 全部篇數的計數"
    ]
