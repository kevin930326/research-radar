"""檢索的「只能經過」規則（Story 4.1 驗收條件 1、AD-11）：

1. 全文檢索的 SQL（`to_tsvector`、`websearch_to_tsquery` 等函式、`@@` 比對運算子、從
   `paper_search_document` 表讀寫）只能出現在 `radar/repo/retrieval.py`。觸發器與回填在遷移
   0023，不在 `radar/` 底下。
2. `radar.repo.retrieval` 只能由 `radar/services/retrieval.py` 匯入：搜尋與找相似論文都只經過
   `services/retrieval.retrieve` 這一個入口。

向量距離運算子的同類規則在 `test_embedding_rules.py`。檢查函式另外拿故意寫錯的假原始碼證明
抓得到。
"""

import ast
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
REPO = "radar/repo/retrieval.py"
SERVICE = "radar/services/retrieval.py"
MODULE = "radar.repo.retrieval"
_FULL_TEXT = re.compile(
    r"\b(?:to_tsvector|to_tsquery|plainto_tsquery|phraseto_tsquery|websearch_to_tsquery"
    r"|ts_rank|ts_rank_cd|setweight)\s*\(|@@"
    r"|\b(?:FROM|JOIN|INTO|UPDATE)\s+\"?paper_search_document\b",
    re.IGNORECASE,
)


def _imports_retrieval_repo(tree: ast.AST) -> Iterator[int]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == MODULE for alias in node.names):
                yield node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            from_package = node.module == "radar.repo" and any(
                alias.name == "retrieval" for alias in node.names
            )
            if node.module == MODULE or from_package:
                yield node.lineno


def retrieval_rule_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（相對路徑 → 原始碼）裡違反上面兩條的地方。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        tree = ast.parse(source)
        if relative != REPO:
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and _FULL_TEXT.search(node.value)
                ):
                    problems.append(f"{relative}:{node.lineno} 全文檢索的 SQL 只能在 {REPO}")
        if relative not in {REPO, SERVICE}:
            for line in _imports_retrieval_repo(tree):
                problems.append(f"{relative}:{line} {MODULE} 只能由 {SERVICE} 匯入")
    return problems


def _radar_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(RADAR.rglob("*.py"))
    }


def test_full_text_sql_and_the_retrieval_repo_only_go_through_their_modules() -> None:
    assert retrieval_rule_problems(_radar_sources()) == []


def test_the_rule_sees_the_real_modules() -> None:
    sources = _radar_sources()
    assert _FULL_TEXT.search(sources[REPO]) is not None
    assert list(_imports_retrieval_repo(ast.parse(sources[SERVICE])))


@pytest.mark.parametrize(
    ("relative", "source", "expected"),
    [
        (
            "radar/repo/paper_list.py",
            "SQL = \"SELECT 1 FROM paper WHERE to_tsvector('english', title) @@ q\"",
            "全文檢索的 SQL 只能在",
        ),
        (
            "radar/repo/stats.py",
            "SQL = \"SELECT websearch_to_tsquery('english', :q)\"",
            "全文檢索的 SQL 只能在",
        ),
        (
            "radar/repo/stats.py",
            'SQL = "SELECT paper_id FROM paper_search_document"',
            "全文檢索的 SQL 只能在",
        ),
        (
            "radar/services/retrieval.py",
            'SQL = "SELECT ts_rank_cd(document, q) FROM x"',
            "全文檢索的 SQL 只能在",
        ),
        ("radar/services/qa.py", "import radar.repo.retrieval", "只能由"),
        ("radar/services/qa.py", "from radar.repo.retrieval import search_papers", "只能由"),
        ("radar/entrypoints/api/x.py", "from radar.repo import retrieval", "只能由"),
    ],
)
def test_the_rule_catches_each_way_of_breaking_it(
    relative: str, source: str, expected: str
) -> None:
    problems = retrieval_rule_problems({relative: source})
    assert problems
    assert all(expected in problem for problem in problems)


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        ("radar/repo/retrieval.py", "SQL = \"SELECT websearch_to_tsquery('english', :q)\""),
        ("radar/services/retrieval.py", "from radar.repo.retrieval import search_papers"),
        ("radar/domain/backup.py", 'TABLES = {"paper_search_document": "全文檢索資料"}'),
        ("radar/services/other.py", "from radar.services.retrieval import retrieve"),
    ],
)
def test_the_rule_allows_the_owning_modules_and_mere_table_names(
    relative: str, source: str
) -> None:
    assert retrieval_rule_problems({relative: source}) == []
