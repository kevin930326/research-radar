"""快照與 `first_snapshot_id` 只有一個建立者：每週更新的流程（AD-19、ADR-0011「快照」第 2、3 點）。

`create_snapshot`、`update_snapshot_published`、`update_papers_first_snapshot` 只能出現在定義它們的
repo 模組與 `radar/services/weekly_update.py`。用 AST 找名稱（import、呼叫、屬性取用都算），
並用假的原始碼證明各種寫法都抓得到。

另外守著 SQL：字串常數裡對 `first_snapshot_id` 的寫入（`SET … first_snapshot_id =`、
`INSERT INTO paper (… first_snapshot_id …)`）只能出現在 `update_papers_first_snapshot` 的函式本體。
"""

import ast
import re
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
SERVICE = "radar/services/weekly_update.py"
# 函式名稱 → 定義它的模組。
SNAPSHOT_WRITERS: Mapping[str, str] = {
    "create_snapshot": "radar/repo/snapshots.py",
    "update_snapshot_published": "radar/repo/snapshots.py",
    "update_papers_first_snapshot": "radar/repo/papers.py",
}


FIRST_SNAPSHOT_WRITER = ("radar/repo/papers.py", "update_papers_first_snapshot")
# 相鄰的字串字面值在 AST 裡已經合成一個常數，整句 SQL 一起比對。
_FIRST_SNAPSHOT_WRITE = re.compile(
    r"\bSET\b(?:(?!\bWHERE\b).)*?\bfirst_snapshot_id\s*=(?!=)"
    r"|\bINSERT\s+INTO\s+\"?paper\"?\s*\([^)]*\bfirst_snapshot_id\b",
    re.IGNORECASE | re.DOTALL,
)


def _names(source: str) -> set[str]:
    """原始碼裡出現的名稱：變數與呼叫、屬性、`import` 的名稱與別名、函式定義。"""
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


def snapshot_writer_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（相對路徑 → 原始碼）裡，在不該的地方用到快照寫入函式的。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        for name in sorted(_names(source) & set(SNAPSHOT_WRITERS)):
            if relative not in {SNAPSHOT_WRITERS[name], SERVICE}:
                problems.append(f"{relative}：{name} 只能由 {SERVICE} 呼叫")
    return problems


def first_snapshot_sql_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources` 裡，在寫入者本體以外寫 `first_snapshot_id` 的 SQL 字串。"""
    problems: list[str] = []
    for relative, source in sorted(sources.items()):
        tree = ast.parse(source)
        allowed: set[int] = set()
        if relative == FIRST_SNAPSHOT_WRITER[0]:
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == FIRST_SNAPSHOT_WRITER[1]:
                    allowed.update(id(inner) for inner in ast.walk(node))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _FIRST_SNAPSHOT_WRITE.search(node.value)
                and id(node) not in allowed
            ):
                problems.append(f"{relative}:{node.lineno}：寫 first_snapshot_id 的 SQL")
    return problems


def _sources() -> dict[str, str]:
    return {
        str(path.relative_to(ROOT)): path.read_text(encoding="utf-8")
        for path in sorted(RADAR.rglob("*.py"))
    }


def test_snapshot_writers_are_used_only_by_weekly_update() -> None:
    sources = _sources()

    assert snapshot_writer_problems(sources) == []
    # 掃描真的看得到：三個函式都在定義的模組與每週更新的 service 裡。
    for name, module in SNAPSHOT_WRITERS.items():
        assert name in _names(sources[module])
        assert name in _names(sources[SERVICE])


def test_snapshot_writer_check_catches_each_form() -> None:
    sources = {
        "radar/services/reparse.py": (
            "from radar.repo.papers import update_papers_first_snapshot as assign\n"
        ),
        "radar/services/upload.py": (
            "from radar.repo import snapshots\n"
            "def f(connection, run_id):\n"
            "    snapshots.create_snapshot(connection, run_id)\n"
        ),
        "radar/services/rescope.py": (
            "import radar.repo.snapshots\n"
            "def g(connection, snapshot_id):\n"
            "    radar.repo.snapshots.update_snapshot_published(connection, snapshot_id)\n"
        ),
        # 定義的模組、每週更新的 service 都可以。
        "radar/repo/snapshots.py": "def create_snapshot(connection, run_id): ...\n",
        SERVICE: "from radar.repo.snapshots import create_snapshot, update_snapshot_published\n",
        # 定義在別的 repo 模組也不行（一張表一個寫入者另有測試，這裡守的是名稱）。
        "radar/repo/other.py": "def update_papers_first_snapshot(connection, snapshot_id): ...\n",
    }

    assert snapshot_writer_problems(sources) == [
        f"radar/repo/other.py：update_papers_first_snapshot 只能由 {SERVICE} 呼叫",
        f"radar/services/reparse.py：update_papers_first_snapshot 只能由 {SERVICE} 呼叫",
        f"radar/services/rescope.py：update_snapshot_published 只能由 {SERVICE} 呼叫",
        f"radar/services/upload.py：create_snapshot 只能由 {SERVICE} 呼叫",
    ]


def test_first_snapshot_id_sql_is_written_only_in_its_writer() -> None:
    sources = _sources()

    assert first_snapshot_sql_problems(sources) == []
    # 掃描真的看得到：同一份 papers.py 換個路徑，寫入者裡那句 SQL 就會被抓到。
    moved = {"radar/repo/elsewhere.py": sources[FIRST_SNAPSHOT_WRITER[0]]}
    assert len(first_snapshot_sql_problems(moved)) == 1


def test_first_snapshot_sql_check_catches_writes_elsewhere() -> None:
    sources = {
        # 同一個檔案、別的函式（例如 upsert 裡順手寫）不行。
        "radar/repo/papers.py": (
            "def update_papers_first_snapshot(connection):\n"
            "    connection.execute('UPDATE paper SET first_snapshot_id = :id WHERE in_scope')\n"
            "def upsert(connection):\n"
            "    connection.execute(\n"
            "        'INSERT INTO paper (id, title) VALUES (:id, :t)'\n"
            "        ' ON CONFLICT (id) DO UPDATE SET title = :t,'\n"
            "        ' first_snapshot_id = EXCLUDED.first_snapshot_id'\n"
            "    )\n"
        ),
        "radar/repo/other.py": (
            "SQL = 'INSERT INTO paper (id, first_snapshot_id) VALUES (:id, :s)'\n"
            # 只是讀：比較、IS NULL 都不算。
            "READ = 'SELECT id FROM paper WHERE first_snapshot_id = :s'\n"
            "NULLS = 'UPDATE paper SET title = :t WHERE first_snapshot_id IS NULL'\n"
        ),
    }

    assert first_snapshot_sql_problems(sources) == [
        "radar/repo/other.py:1：寫 first_snapshot_id 的 SQL",
        "radar/repo/papers.py:5：寫 first_snapshot_id 的 SQL",
    ]
