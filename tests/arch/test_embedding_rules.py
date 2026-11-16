"""向量的「只能經過」規則（AD-12、ADR-0040，Story 2.8）：

1. 寫 `embedding` 的函式（`upsert_embeddings`）只出現在定義它的 `radar/repo/embeddings.py` 與
   `radar/services/embeddings.py`；SQL 字串裡寫 `embedding` 表（`INSERT INTO`／`UPDATE`／
   `DELETE FROM`）只能在 `radar/repo/embeddings.py`。
2. 向量距離運算子（`<=>`、`<->`、`<#>`）只能出現在 `radar/repo/retrieval.py`：向量查詢都要經過
   那裡、都要帶 `model_id`。
3. `radar/repo/retrieval.py` 每個公開函式都有必填（沒有預設值）的關鍵字參數 `model_id`。

用 AST 找名稱與字串常數，並用假的原始碼證明各種寫法都抓得到。
"""

import ast
import inspect
import re
from collections.abc import Mapping
from pathlib import Path

import pytest

import radar.repo.retrieval as retrieval

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
REPO = "radar/repo/embeddings.py"
SERVICE = "radar/services/embeddings.py"
RETRIEVAL = "radar/repo/retrieval.py"
WRITERS = frozenset({"upsert_embeddings"})
_EMBEDDING_WRITE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\"?embedding\"?(?![\w.])", re.IGNORECASE
)
_DISTANCE = re.compile(r"<=>|<->|<#>")


def _names(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.rsplit(".", 1)[-1])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.add(node.name)
    return found


def _strings(source: str) -> list[str]:
    return [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def embedding_rule_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（相對路徑 → 原始碼）裡違反上面 1、2 條的地方。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        if relative not in {REPO, SERVICE}:
            for name in sorted(_names(source) & WRITERS):
                problems.append(f"{relative}：{name} 只能由 {SERVICE} 呼叫")
        for value in _strings(source):
            if relative != REPO and _EMBEDDING_WRITE.search(value):
                problems.append(f"{relative}：寫 embedding 表的 SQL 只能在 {REPO}")
            if relative != RETRIEVAL and _DISTANCE.search(value):
                problems.append(f"{relative}：向量距離運算只能在 {RETRIEVAL}")
    return problems


def _radar_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(RADAR.rglob("*.py"))
    }


def test_embedding_writes_and_vector_distance_only_go_through_their_modules() -> None:
    assert embedding_rule_problems(_radar_sources()) == []


@pytest.mark.parametrize(
    ("relative", "source", "expected"),
    [
        (
            "radar/services/other.py",
            "from radar.repo.embeddings import upsert_embeddings",
            "upsert_embeddings 只能由",
        ),
        ("radar/services/other.py", "import x\nx.upsert_embeddings(c, [])", "upsert_embeddings"),
        (
            "radar/repo/other.py",
            'SQL = "INSERT INTO embedding (item_id) VALUES (1)"',
            "寫 embedding 表的 SQL",
        ),
        ("radar/repo/other.py", "SQL = 'update \"embedding\" set x = 1'", "寫 embedding 表"),
        ("radar/repo/other.py", 'SQL = "DELETE FROM embedding WHERE true"', "寫 embedding 表"),
        ("radar/repo/other.py", 'SQL = "ORDER BY vector <=> :q"', "向量距離運算"),
        ("radar/services/other.py", "SQL = 'a <-> b'", "向量距離運算"),
        ("radar/repo/embeddings.py", "SQL = 'a <#> b'", "向量距離運算"),
    ],
)
def test_checker_catches_each_form(relative: str, source: str, expected: str) -> None:
    problems = embedding_rule_problems({relative: source})
    assert any(expected in problem for problem in problems), problems


@pytest.mark.parametrize(
    "source",
    [
        'SQL = "INSERT INTO embedding_model (model_id) VALUES (1)"',
        'SQL = "SELECT * FROM embedding WHERE model_id = :m"',
        'SQL = "UPDATE embedding_other SET x = 1"',
    ],
)
def test_checker_ignores_reads_and_other_tables(source: str) -> None:
    assert embedding_rule_problems({"radar/repo/other.py": source}) == []


def _public_functions() -> list[tuple[str, inspect.Signature]]:
    return [
        (name, inspect.signature(function))
        for name, function in inspect.getmembers(retrieval, inspect.isfunction)
        if function.__module__ == retrieval.__name__ and not name.startswith("_")
    ]


def test_every_vector_query_requires_model_id_as_a_keyword_without_default() -> None:
    functions = _public_functions()
    assert [name for name, _ in functions] == ["search_paper_vectors"]
    for name, signature in functions:
        parameter = signature.parameters.get("model_id")
        assert parameter is not None, f"{name} 沒有 model_id"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameter.default is inspect.Parameter.empty, name
