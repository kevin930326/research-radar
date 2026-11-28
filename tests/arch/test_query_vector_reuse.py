"""重用查詢向量只限評估（Story 4.4、PR C 審查 H1）：

1. 呼叫時帶 `query_vector=` 關鍵字的地方只有 `radar/services/evaluation.py`（`radar eval search`
   每題的查詢向量只算一次、多個下限共用）。API、問答、其他任何地方都不傳：搜尋一定經
   `retrieve` 自己驗字、外送檢查、記帳後再轉向量。
2. `SearchQueryVector(` 只在 `radar/services/retrieval.py` 建立（`embed_retrieval_query` 帶封條；
   別處自己建的物件 `retrieve` 也會拒收，見 `tests/integration/test_search_eval.py`）。

檢查函式另外拿故意寫錯的假原始碼證明抓得到。
"""

import ast
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
REUSERS = frozenset({"radar/services/evaluation.py"})
BUILDERS = frozenset({"radar/services/retrieval.py"})


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _offences(relative: str, source: str) -> Iterator[str]:
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if relative not in REUSERS and any(kw.arg == "query_vector" for kw in node.keywords):
            yield f"{relative}:{node.lineno} 傳了 query_vector"
        if relative not in BUILDERS and _call_name(node) == "SearchQueryVector":
            yield f"{relative}:{node.lineno} 自己建 SearchQueryVector"


def test_only_the_evaluation_service_reuses_query_vectors() -> None:
    offences = [
        offence
        for path in sorted(RADAR.rglob("*.py"))
        for offence in _offences(path.relative_to(ROOT).as_posix(), path.read_text("utf-8"))
    ]

    assert offences == []


def test_the_check_catches_api_code_passing_a_query_vector() -> None:
    bad = (
        "def handler(viewer, database, setup, vector):\n"
        "    retrieval_service.retrieve(viewer, database, setup, query='q', query_vector=vector)\n"
        "    return SearchQueryVector(text='q', model_id='m', vector=(), seal=b'')\n"
    )

    assert list(_offences("radar/entrypoints/api/paper_list.py", bad)) == [
        "radar/entrypoints/api/paper_list.py:2 傳了 query_vector",
        "radar/entrypoints/api/paper_list.py:3 自己建 SearchQueryVector",
    ]
    assert list(_offences("radar/services/evaluation.py", bad)) == [
        "radar/services/evaluation.py:3 自己建 SearchQueryVector",
    ]
