"""資料層的架構規則（AD-3、AD-4、AD-27、Conventions「交易」「一張表一個寫入者」）：

1. `radar.repo` 的公開函式名稱用登記的前綴；讀取類的第一個參數是沒有預設值的 `viewer`，
   型別是 Viewer 或其中幾種；公開類別只能是基礎設施例外。
2. `lab_visible` 只出現在 `radar/repo/visibility.py` 與 `radar/repo/papers.py`。
3. 一張表一個寫入者：`radar/repo` 字串裡的寫入 SQL，每張表只對到一個模組；不用 SQLAlchemy 的
   `insert`／`update`／`delete`（用了就掃不到）。
4. `.commit(`／`.rollback(`／`.begin(`／`.begin_nested(` 只在 `repo/db.py` 與 `repo/migrations.py`；
   `.transaction(` 只在 `radar/services`。
5. services 的公開函式只要用到寫入類 repo 函式，第一個參數就叫 `actor`。

每條規則的檢查函式都另外拿故意寫錯的假模組或假原始碼證明抓得到。
"""

import ast
import importlib
import inspect
import pkgutil
import re
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import pytest

import radar.repo
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"

READ_PREFIXES = ("get_", "list_", "find_", "count_", "search_", "visible_")
WRITE_PREFIXES = ("upsert_", "create_", "update_", "delete_")
VIEWER_TYPES = frozenset({MemberViewer, PublicViewer, SystemViewer})

# 不讀業務資料的基礎設施：連線、遷移、模型的共同起點。不帶 viewer、名稱不用登記的前綴。
_MIGRATIONS_REASON = (
    "Alembic 遷移的基礎設施（radar migrate、env.py、/readyz），只碰遷移版本與資料庫功能"
)
INFRASTRUCTURE_EXCEPTIONS: dict[str, str] = {
    "radar.repo.db.Database": "連線池與交易函式本身，不讀業務資料",
    "radar.repo.models.Base": "SQLAlchemy 模型的共同起點（Base.metadata 給 alembic check）",
    "radar.repo.migrations.include_name": _MIGRATIONS_REASON,
    "radar.repo.migrations.create_migration_engine": _MIGRATIONS_REASON,
    "radar.repo.migrations.read_schema_state": _MIGRATIONS_REASON,
    "radar.repo.migrations.read_database_features": _MIGRATIONS_REASON,
    "radar.repo.migrations.upgrade_to_head": _MIGRATIONS_REASON,
    "radar.repo.migrations.MigrationScripts": _MIGRATIONS_REASON,
    "radar.repo.migrations.MigrationsNotFoundError": _MIGRATIONS_REASON,
}


def _repo_modules() -> list[types.ModuleType]:
    return [
        importlib.import_module(info.name)
        for info in pkgutil.walk_packages(radar.repo.__path__, prefix="radar.repo.")
    ]


def _sources(directory: Path) -> dict[str, str]:
    """`directory` 底下每個 .py 檔：相對於 repo 根目錄的路徑 → 原始碼。"""
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def _module_name(relative: str) -> str:
    return relative.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


# --- 1. 命名與 viewer ----------------------------------------------------------------------


def _viewer_types(annotation: object) -> set[type] | None:
    """註記是 Viewer 或其中幾種時回那幾種，否則回 None。"""
    if isinstance(annotation, typing.TypeAliasType):
        return _viewer_types(annotation.__value__)
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        found: set[type] = set()
        for argument in typing.get_args(annotation):
            part = _viewer_types(argument)
            if part is None:
                return None
            found |= part
        return found
    if isinstance(annotation, type) and annotation in VIEWER_TYPES:
        return {annotation}
    return None


def naming_problems(module: types.ModuleType, exceptions: Mapping[str, str]) -> list[str]:
    """`module` 自己定義的公開函式與類別違反的命名規則；沒有違規時是空清單。"""
    problems: list[str] = []
    for name, obj in vars(module).items():
        if name.startswith("_") or not (inspect.isfunction(obj) or inspect.isclass(obj)):
            continue
        if getattr(obj, "__module__", None) != module.__name__:
            continue  # import 進來的東西，由定義它的模組負責
        qualified = f"{module.__name__}.{name}"
        if qualified in exceptions:
            continue
        if inspect.isclass(obj):
            problems.append(f"{qualified}：公開類別要列在基礎設施例外清單")
        elif name.startswith(READ_PREFIXES):
            problems += _viewer_problems(qualified, obj)
        elif not name.startswith(WRITE_PREFIXES):
            problems.append(f"{qualified}：名稱沒有用登記的前綴")
    return problems


def _viewer_problems(qualified: str, function: Callable[..., object]) -> list[str]:
    parameters = list(inspect.signature(function, eval_str=True).parameters.values())
    if not parameters or parameters[0].name != "viewer":
        return [f"{qualified}：讀取類的第一個參數要叫 viewer"]
    viewer = parameters[0]
    if viewer.default is not inspect.Parameter.empty:
        return [f"{qualified}：viewer 不能有預設值"]
    if viewer.kind not in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        return [f"{qualified}：viewer 要是位置參數"]
    if not _viewer_types(viewer.annotation):
        return [f"{qualified}：viewer 的型別要是 Viewer 或其中幾種"]
    return []


def missing_exception_problems(
    modules: Iterable[types.ModuleType], exceptions: Mapping[str, str]
) -> list[str]:
    """例外清單裡不存在的名稱、沒寫理由的例外。"""
    defined = {
        f"{module.__name__}.{name}"
        for module in modules
        for name, obj in vars(module).items()
        if getattr(obj, "__module__", None) == module.__name__
    }
    problems = [f"{name}：在例外清單裡，但不存在" for name in exceptions if name not in defined]
    problems += [f"{name}：例外沒有寫理由" for name, why in exceptions.items() if not why.strip()]
    return problems


def test_repo_public_names_follow_registered_prefixes() -> None:
    problems = [
        problem
        for module in _repo_modules()
        for problem in naming_problems(module, INFRASTRUCTURE_EXCEPTIONS)
    ]
    assert problems == []


def test_every_infrastructure_exception_exists_and_has_reason() -> None:
    assert missing_exception_problems(_repo_modules(), INFRASTRUCTURE_EXCEPTIONS) == []


def test_missing_exception_is_reported() -> None:
    exceptions = {**INFRASTRUCTURE_EXCEPTIONS, "radar.repo.db.NoSuchThing": "測試用"}

    assert missing_exception_problems(_repo_modules(), exceptions) == [
        "radar.repo.db.NoSuchThing：在例外清單裡，但不存在"
    ]


FAKE_REPO_MODULE = """
from radar.domain.viewer import MemberViewer, SystemViewer, Viewer


def fetch_papers(viewer: Viewer) -> None: ...
def get_paper(connection: object, viewer: Viewer) -> None: ...
def list_papers(viewer: Viewer = SystemViewer()) -> None: ...
def count_papers(viewer: int) -> None: ...
def search_papers(*, viewer: Viewer) -> None: ...
def find_ok(viewer: MemberViewer | SystemViewer, limit: int) -> None: ...
def visible_ok(viewer: "Viewer") -> None: ...
def upsert_thing(connection: object) -> None: ...
def _private_helper() -> None: ...


class Helper:
    pass
"""


def test_naming_check_catches_each_mistake() -> None:
    module = types.ModuleType("radar.repo.fake_probe")
    exec(compile(FAKE_REPO_MODULE, "fake_probe.py", "exec"), module.__dict__)

    problems = naming_problems(module, {})

    by_name = {problem.split("：")[0].rsplit(".", 1)[1] for problem in problems}
    assert by_name == {
        "fetch_papers",
        "get_paper",
        "list_papers",
        "count_papers",
        "search_papers",
        "Helper",
    }, problems
    assert len(problems) == len(by_name), problems


# --- 2. lab_visible 只在兩個檔 ----------------------------------------------------------------

LAB_VISIBLE_FILES = frozenset({"radar/repo/visibility.py", "radar/repo/papers.py"})


def lab_visible_offenders(root: Path, base: Path, allowed: frozenset[str]) -> list[str]:
    """`root` 底下（不含 `__pycache__`）出現 `lab_visible`、但不在 `allowed` 的檔。"""
    return [
        relative
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and (relative := path.relative_to(base).as_posix()) not in allowed
        and b"lab_visible" in path.read_bytes()
    ]


def test_lab_visible_only_in_visibility_and_papers() -> None:
    assert lab_visible_offenders(RADAR, ROOT, LAB_VISIBLE_FILES) == []
    for relative in LAB_VISIBLE_FILES:
        assert "lab_visible" in (ROOT / relative).read_text(encoding="utf-8"), relative


def test_lab_visible_check_catches_other_file(tmp_path: Path) -> None:
    services = tmp_path / "radar" / "services"
    services.mkdir(parents=True)
    (services / "leak.py").write_text("SQL = 'SELECT * FROM paper WHERE lab_visible'\n")
    (tmp_path / "radar" / "repo").mkdir()
    (tmp_path / "radar" / "repo" / "papers.py").write_text("# lab_visible\n")

    assert lab_visible_offenders(tmp_path / "radar", tmp_path, LAB_VISIBLE_FILES) == [
        "radar/services/leak.py"
    ]


# --- 3. 一張表一個寫入者 ----------------------------------------------------------------------

# f-string 裡的變數換成這個字元；寫入 SQL 的表名是變數時掃不到，直接算違規。
_PLACEHOLDER = "\x00"
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
# 表名前面可以有 `ONLY` 與 `<schema>.`（都可以加引號）；只取表名。
_TABLE = rf'(?:ONLY\s+)?(?:"?{_IDENTIFIER}"?\.)?"?({_IDENTIFIER}|{_PLACEHOLDER})"?'
_WRITE_SQL = re.compile(
    rf"\b(?:INSERT\s+INTO|DELETE\s+FROM|MERGE\s+INTO)\s+{_TABLE}"
    rf"|\bUPDATE\s+{_TABLE}(?:\s+(?:AS\s+)?{_IDENTIFIER})?\s+SET\b"
    rf"|\bTRUNCATE\s+(?:TABLE\s+)?{_TABLE}",
    re.IGNORECASE,
)
_SQLALCHEMY_WRITERS = frozenset({"insert", "update", "delete"})


def _string_literals(tree: ast.Module) -> list[str]:
    """模組裡的字串（f-string 的變數換成佔位字元），不含 docstring。"""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    literals: list[str] = []
    inside_fstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                inside_fstrings.add(id(value))
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
            and id(node) not in inside_fstrings
        ):
            literals.append(node.value)
    return literals


def written_tables(source: str) -> set[str]:
    """原始碼字串裡的寫入 SQL 寫到的表（表名是變數時記成佔位字元）。"""
    tables: set[str] = set()
    for literal in _string_literals(ast.parse(source)):
        for match in _WRITE_SQL.finditer(literal):
            tables.add(next(group for group in match.groups() if group).lower())
    return tables


def _sqlalchemy_writer_uses(source: str) -> list[str]:
    tree = ast.parse(source)
    module_aliases = {"sqlalchemy"}
    uses: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_aliases |= {
                alias.asname or alias.name for alias in node.names if alias.name == "sqlalchemy"
            }
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy"):
            uses += [
                f"from {node.module} import {alias.name}"
                for alias in node.names
                if alias.name in _SQLALCHEMY_WRITERS
            ]
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr in _SQLALCHEMY_WRITERS):
            continue
        if isinstance(node.value, ast.Name) and node.value.id in module_aliases:
            uses.append(f"{node.value.id}.{node.attr}")
    for node in ast.walk(tree):
        # `paper_table.insert()` 這類 Table 的寫入方法（不帶參數的呼叫）。
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SQLALCHEMY_WRITERS
            and not node.args
            and not node.keywords
        ):
            uses.append(f".{node.func.attr}()")
    return uses


def writer_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（模組名稱 → 原始碼）裡，一張表有兩個以上寫入者、表名是變數、用 SQLAlchemy 寫入。"""
    writers: dict[str, set[str]] = {}
    problems: list[str] = []
    for module, source in sources.items():
        for table in written_tables(source):
            if table == _PLACEHOLDER:
                problems.append(f"{module}：寫入 SQL 的表名不能是變數")
            else:
                writers.setdefault(table, set()).add(module)
        problems += [
            f"{module}：不要用 SQLAlchemy 的寫入（{use}），改用 text() SQL"
            for use in _sqlalchemy_writer_uses(source)
        ]
    problems += [
        f"{table}：有兩個以上的寫入者（{'、'.join(sorted(modules))}）"
        for table, modules in sorted(writers.items())
        if len(modules) > 1
    ]
    return problems


def _repo_sources() -> dict[str, str]:
    return {_module_name(relative): source for relative, source in _sources(RADAR / "repo").items()}


def test_each_table_has_one_writer_module() -> None:
    sources = _repo_sources()

    assert writer_problems(sources) == []
    # 掃描真的看得到寫入：papers 是 paper 與 external_id 的寫入者。
    assert written_tables(sources["radar.repo.papers"]) == {"paper", "external_id"}


def test_writer_check_catches_each_mistake() -> None:
    sources = {
        "radar.repo.a": 'SQL = "INSERT INTO paper (title) VALUES (:t)"',
        "radar.repo.b": 'SQL = "UPDATE paper p SET title = :t"',
        "radar.repo.c": 'def f(t):\n    return f"DELETE FROM {t} WHERE id = :id"',
        "radar.repo.d": "from sqlalchemy import insert",
        "radar.repo.e": "import sqlalchemy as sa\nX = sa.delete",
        "radar.repo.f": "def f(table):\n    return table.update()",
        # docstring 與 ON CONFLICT ... DO UPDATE SET 不算。
        "radar.repo.g": (
            '"""這裡會 INSERT INTO paper。"""\n'
            'SQL = "INSERT INTO note (a) VALUES (1) ON CONFLICT (a) DO UPDATE SET a = 2"'
        ),
    }

    problems = writer_problems(sources)

    assert problems == [
        "radar.repo.c：寫入 SQL 的表名不能是變數",
        "radar.repo.d：不要用 SQLAlchemy 的寫入（from sqlalchemy import insert），改用 text() SQL",
        "radar.repo.e：不要用 SQLAlchemy 的寫入（sa.delete），改用 text() SQL",
        "radar.repo.f：不要用 SQLAlchemy 的寫入（.update()），改用 text() SQL",
        "paper：有兩個以上的寫入者（radar.repo.a、radar.repo.b）",
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE ONLY paper SET title = :t",
        "INSERT INTO public.paper (title) VALUES (:t)",
        'INSERT INTO "public"."paper" (title) VALUES (:t)',
        "DELETE FROM ONLY public.paper WHERE id = :id",
        "TRUNCATE paper",
        "TRUNCATE TABLE ONLY paper",
        "MERGE INTO paper USING x ON true WHEN MATCHED THEN DO NOTHING",
    ],
)
def test_written_tables_sees_qualified_and_truncate_forms(sql: str) -> None:
    assert written_tables(f"SQL = {sql!r}") == {"paper"}
    # 另一個模組也寫 paper 時要被抓到。
    problems = writer_problems(
        {"radar.repo.a": f"SQL = {sql!r}", "radar.repo.b": 'SQL = "DELETE FROM paper"'}
    )
    assert problems == ["paper：有兩個以上的寫入者（radar.repo.a、radar.repo.b）"]


# --- 4. 交易只在該在的地方開 ------------------------------------------------------------------

TRANSACTION_METHODS = frozenset({"commit", "rollback", "begin", "begin_nested"})
TRANSACTION_FILES = frozenset({"radar/repo/db.py", "radar/repo/migrations.py"})
TRANSACTION_OPENERS = "radar/services/"


def transaction_problems(sources: Mapping[str, str]) -> list[str]:
    """`sources`（相對路徑 → 原始碼）裡，在不該的地方 commit／rollback／begin 或開交易。"""
    problems: list[str] = []
    for relative, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            method = node.func.attr
            if method in TRANSACTION_METHODS and relative not in TRANSACTION_FILES:
                problems.append(f"{relative}:{node.lineno}：.{method}() 只能在 repo/db.py 與遷移")
            elif method == "transaction" and not relative.startswith(TRANSACTION_OPENERS):
                problems.append(f"{relative}:{node.lineno}：.transaction() 只能在 radar/services")
    return problems


def test_transactions_are_opened_only_by_services() -> None:
    assert transaction_problems(_sources(RADAR)) == []


def test_transaction_check_catches_each_mistake() -> None:
    sources = {
        "radar/repo/papers.py": "def f(connection):\n    connection.commit()",
        "radar/services/ingest.py": "def g(connection):\n    connection.rollback()",
        "radar/entrypoints/cli/x.py": "def h(database):\n    database.transaction()",
        "radar/repo/db.py": "def ok(engine):\n    engine.begin()",
        "radar/services/ok.py": "def ok(database):\n    database.transaction()",
    }

    assert transaction_problems(sources) == [
        "radar/repo/papers.py:2：.commit() 只能在 repo/db.py 與遷移",
        "radar/services/ingest.py:2：.rollback() 只能在 repo/db.py 與遷移",
        "radar/entrypoints/cli/x.py:2：.transaction() 只能在 radar/services",
    ]


# --- 5. 寫入的 services 函式第一個參數叫 actor ------------------------------------------------


def _is_repo_module(name: str) -> bool:
    return name == "radar.repo" or name.startswith("radar.repo.")


def actor_problems(module: str, source: str) -> list[str]:
    """services 模組裡，用到（直接或經私有函式）寫入類 repo 函式、
    第一個參數卻不是 `actor` 的公開函式。
    """
    tree = ast.parse(source)
    write_names: set[str] = set()  # 直接 import 進來的寫入類 repo 函式（本地名稱）
    repo_aliases: set[str] = set()  # 代表 repo 模組的本地名稱
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _is_repo_module(node.module or ""):
            for alias in node.names:
                local = alias.asname or alias.name
                if node.module == "radar.repo" or not alias.name.startswith(WRITE_PREFIXES):
                    repo_aliases.add(local)  # `from radar.repo import papers`（或讀取函式，無害）
                else:
                    write_names.add(local)
        elif isinstance(node, ast.Import):
            repo_aliases |= {
                alias.asname for alias in node.names if alias.asname and _is_repo_module(alias.name)
            }

    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def writes_directly(function: ast.AST) -> bool:
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and node.id in write_names:
                return True
            if isinstance(node, ast.Attribute) and node.attr.startswith(WRITE_PREFIXES):
                owner = ast.unparse(node.value)
                if owner in repo_aliases or _is_repo_module(owner):
                    return True
        return False

    def writes(name: str, seen: set[str]) -> bool:
        seen.add(name)
        function = functions[name]
        if writes_directly(function):
            return True
        called = {
            node.id
            for node in ast.walk(function)
            if isinstance(node, ast.Name) and node.id in functions and node.id.startswith("_")
        }
        return any(writes(helper, seen) for helper in called - seen)

    problems: list[str] = []
    for name, function in functions.items():
        if name.startswith("_") or not writes(name, set()):
            continue
        arguments = [*function.args.posonlyargs, *function.args.args]
        if not arguments or arguments[0].arg != "actor":
            problems.append(f"{module}.{name}：用到寫入類 repo 函式，第一個參數要叫 actor")
    return problems


def test_services_that_write_take_actor_first() -> None:
    problems = [
        problem
        for relative, source in _sources(RADAR / "services").items()
        for problem in actor_problems(_module_name(relative), source)
    ]
    assert problems == []


FAKE_SERVICES = """
from radar.repo import papers
from radar.repo.papers import get_paper, upsert_by_external_id as upsert
import radar.repo.papers as papers_module


def ingest(database, paper):
    upsert(database, paper)


def ingest_ok(actor, database, paper):
    upsert(database, paper)


def via_helper(database):
    _write(database)


def _write(database):
    papers.upsert_by_external_id(database)


def via_module_alias(database):
    papers_module.create_manual(database)


def via_full_path(database):
    radar.repo.papers.delete_paper(database)


def read_only(viewer, connection, paper_id):
    return get_paper(viewer, connection, paper_id)
"""


def test_actor_check_catches_each_mistake() -> None:
    problems = actor_problems("radar.services.fake", FAKE_SERVICES)

    assert problems == [
        "radar.services.fake.ingest：用到寫入類 repo 函式，第一個參數要叫 actor",
        "radar.services.fake.via_helper：用到寫入類 repo 函式，第一個參數要叫 actor",
        "radar.services.fake.via_module_alias：用到寫入類 repo 函式，第一個參數要叫 actor",
        "radar.services.fake.via_full_path：用到寫入類 repo 函式，第一個參數要叫 actor",
    ]
