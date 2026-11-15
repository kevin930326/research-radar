"""資料層的架構規則（AD-3、AD-4、AD-27、Conventions「交易」「一張表一個寫入者」）：

1. `radar.repo` 的公開函式名稱用登記的前綴；讀取類的第一個參數是沒有預設值的 `viewer`，
   型別是 Viewer 或其中幾種；公開類別只能是基礎設施例外。
2. `lab_visible` 只出現在 `radar/repo/visibility.py` 與 `radar/repo/papers.py`；可見條件只從
   `repo/visibility.py` 來：其他 repo 模組不自己判斷讀者身分，讀取函式把 `viewer` 交給它的函式。
3. 一張表一個寫入者：`radar/repo` 字串裡的寫入 SQL，每張表只對到一個模組；不用 SQLAlchemy 的
   `insert`／`update`／`delete`（用了就掃不到）。
4. `.commit(`／`.rollback(`／`.begin(`／`.begin_nested(` 只在 `repo/db.py` 與 `repo/migrations.py`；
   `.transaction(` 只在 `radar/services`。
5. services 的公開函式只要用到寫入類 repo 函式，第一個參數就叫 `actor`。

每條規則的檢查函式都另外拿故意寫錯的假模組或假原始碼證明抓得到。
"""

import ast
import functools
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
_LOGIN_LOOKUP_REASON = (
    "登入流程用 email 或成員 ID 查允許名單：登入之前沒有 viewer（ADR-0002 的列名例外）"
)
INFRASTRUCTURE_EXCEPTIONS: dict[str, str] = {
    "radar.repo.db.Database": "連線池與交易函式本身，不讀業務資料",
    "radar.repo.db.SessionLock": "持有 advisory lock 的連線與它的交易函式，不讀業務資料",
    "radar.repo.db.AdvisoryLockLostError": "持鎖的連線斷了或鎖不在時丟的例外，不讀業務資料",
    "radar.repo.models.Base": "SQLAlchemy 模型的共同起點（Base.metadata 給 alembic check）",
    "radar.repo.migrations.include_name": _MIGRATIONS_REASON,
    "radar.repo.migrations.create_migration_engine": _MIGRATIONS_REASON,
    "radar.repo.migrations.read_schema_state": _MIGRATIONS_REASON,
    "radar.repo.migrations.read_database_features": _MIGRATIONS_REASON,
    "radar.repo.migrations.upgrade_to_head": _MIGRATIONS_REASON,
    "radar.repo.migrations.MigrationScripts": _MIGRATIONS_REASON,
    "radar.repo.migrations.MigrationsNotFoundError": _MIGRATIONS_REASON,
    # 登入流程的查找（ADR-0002「不帶 viewer 的讀取只有列名的例外」、AD-3）：要先查允許名單或
    # session 才知道是誰，沒有 viewer 可帶。只回成員 ID、顯示名稱、是否管理者，不回 email。
    "radar.repo.members.find_allowed_member_by_email": _LOGIN_LOOKUP_REASON,
    "radar.repo.members.find_allowed_member_by_id": _LOGIN_LOOKUP_REASON,
    "radar.repo.members.list_allowed_members": (
        "開發環境假登入的選人清單：還沒登入、沒有 viewer 可帶（ADR-0002 的列名例外）；"
        "只回成員 ID 與顯示名稱，試用與正式環境不會呼叫"
    ),
    "radar.repo.sessions.find_active_session": (
        "由 cookie 的 token 雜湊認出是誰：查到之前沒有 viewer（ADR-0002 的列名例外）"
    ),
    "radar.repo.sessions.find_session_owner": (
        "登出時由 cookie 的 token 雜湊找 session 的擁有者（過期的也要刪）：查到之前沒有 viewer"
        "（ADR-0002 的列名例外）；只回 session ID、成員 ID、是否管理者"
    ),
}


def _repo_modules() -> list[types.ModuleType]:
    """`radar.repo` 根套件本身（`__init__.py` 也可能定義函式）與底下每個模組。"""
    return [
        radar.repo,
        *(
            importlib.import_module(info.name)
            for info in pkgutil.walk_packages(radar.repo.__path__, prefix="radar.repo.")
        ),
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


# --- import 的解析（規則 2、5 共用） -------------------------------------------------------


def _resolve_from(module: str, node: ast.ImportFrom, *, is_package: bool) -> str:
    """`from X import ...` 的 X 的完整模組路徑；相對 import 依 `module` 所在的套件解析。"""
    if node.level == 0:
        return node.module or ""
    package = module if is_package else module.rpartition(".")[0]
    parts = package.split(".")
    base = ".".join(parts[: len(parts) - (node.level - 1)])
    return f"{base}.{node.module}" if node.module else base


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(scope: ast.AST) -> list[ast.AST]:
    """`scope` 自己這一層的節點：巢狀的函式、lambda、類別本身算在內，但不往它們裡面走。"""
    own: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        own.append(node)
        if not isinstance(node, _SCOPES):
            pending.extend(ast.iter_child_nodes(node))
    return own


def _scoped_names(
    module: str, tree: ast.Module, *, is_package: bool
) -> dict[int, Mapping[str, str]]:
    """每個節點看得到的 import 名稱表（本地名稱 → 完整路徑：模組，或 `模組.名稱`）。

    模組層的 import 整個模組都看得到；函式（含 lambda、類別）裡的 import 只在那一層和它裡面的
    巢狀函式看得到，不會蓋掉別的函式的解析結果。
    """
    result: dict[int, Mapping[str, str]] = {}

    def visit(scope: ast.AST, inherited: Mapping[str, str]) -> None:
        own = _own_nodes(scope)
        names = dict(inherited)
        for node in own:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        names[alias.asname] = alias.name
                    else:  # `import a.b.c` 綁定的是 `a`
                        root = alias.name.split(".")[0]
                        names[root] = root
            elif isinstance(node, ast.ImportFrom):
                source = _resolve_from(module, node, is_package=is_package)
                for alias in node.names:
                    names[alias.asname or alias.name] = f"{source}.{alias.name}"
        result[id(scope)] = names
        for node in own:
            result[id(node)] = names
        for node in own:
            if isinstance(node, _SCOPES):
                visit(node, names)

    visit(tree, {})
    return result


def _full_name(expression: ast.AST, names: Mapping[str, str]) -> str | None:
    """`a.b.c` 這種名稱鏈換成完整路徑（開頭的名稱依 import 換掉，沒 import 的照原樣）；
    不是名稱鏈時回 None。
    """
    parts: list[str] = []
    while isinstance(expression, ast.Attribute):
        parts.append(expression.attr)
        expression = expression.value
    if not isinstance(expression, ast.Name):
        return None
    parts.append(names.get(expression.id, expression.id))
    return ".".join(reversed(parts))


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


def test_repo_modules_include_root_package() -> None:
    assert _repo_modules()[0] is radar.repo


def test_naming_check_covers_root_package(monkeypatch: pytest.MonkeyPatch) -> None:
    # 直接定義在 `radar/repo/__init__.py` 的函式也要被檢查。
    def fetch_everything(connection: object) -> None: ...

    fetch_everything.__module__ = "radar.repo"
    monkeypatch.setattr(radar.repo, "fetch_everything", fetch_everything, raising=False)

    problems = [
        problem
        for module in _repo_modules()
        for problem in naming_problems(module, INFRASTRUCTURE_EXCEPTIONS)
    ]

    assert problems == ["radar.repo.fetch_everything：名稱沒有用登記的前綴"]


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


# 可見條件只從 visibility.py 來（AD-4）：其他 repo 模組不自己判斷讀者身分（不在型別註記以外的
# 地方用到讀者身分的類別，例如 isinstance、match；不讀 viewer 的屬性、不看 type(viewer)），
# 讀取函式要把自己的 viewer 交給 visibility.py 的函式。
VISIBILITY_MODULE = "radar.repo.visibility"
VIEWER_MODULE = "radar.domain.viewer"
VIEWER_CLASS_NAMES = frozenset({"MemberViewer", "PublicViewer", "SystemViewer", "Viewer"})


def _annotation_nodes(tree: ast.Module) -> set[int]:
    """型別註記與型別別名右側的每個節點（`viewer: SystemViewer` 這種寫法不算判斷身分）。"""
    annotations: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            every = [
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                *(argument for argument in (arguments.vararg, arguments.kwarg) if argument),
            ]
            annotations += [argument.annotation for argument in every if argument.annotation]
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, ast.TypeAlias):  # `type PaperReader = MemberViewer | SystemViewer`
            annotations += [node.value, *node.type_params]
    return {id(inner) for annotation in annotations for inner in ast.walk(annotation)}


def _is_viewer_class(full: str | None) -> bool:
    if full is None:
        return False
    module, _, name = full.rpartition(".")
    return name in VIEWER_CLASS_NAMES and module in ("", VIEWER_MODULE)


def visibility_problems(module: str, source: str, *, is_package: bool = False) -> list[str]:
    """repo 模組（visibility.py 以外）自己判斷讀者身分的地方，
    以及沒有把 viewer 交給 visibility.py 的讀取函式。
    """
    if module == VISIBILITY_MODULE:
        return []
    tree = ast.parse(source)
    names_of = _scoped_names(module, tree, is_package=is_package)
    annotations = _annotation_nodes(tree)
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    judged = [
        (node.lineno, ast.unparse(node))
        for node in ast.walk(tree)
        if isinstance(node, ast.expr)
        and id(node) not in annotations
        and _judges_viewer(node, names_of[id(node)])
    ]
    problems = [
        f"{module}:{line}：自己判斷讀者身分（{code}），改用 radar.repo.visibility"
        for line, code in sorted(judged)
    ]

    for node in tree.body:
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith(READ_PREFIXES)
        ):
            continue
        arguments = [*node.args.posonlyargs, *node.args.args]
        if not arguments or arguments[0].arg != "viewer":
            continue  # 命名規則另外抓
        # 只看函式自己這一層：只在巢狀函式裡呼叫不算。
        own = _own_nodes(node)
        calls = [call for call in own if _passes_viewer_to_visibility(call, names_of[id(call)])]
        if not calls:
            problems.append(
                f"{module}.{node.name}：讀取函式要把 viewer 交給 radar.repo.visibility 的函式"
            )
        elif not any(_condition_used(call, own, parents, names_of) for call in calls):
            problems.append(
                f"{module}.{node.name}：visibility 回的可見條件要用進查詢"
                "（f-string、text() 或字串相接），不能丟掉或在用之前被覆蓋"
            )
    return problems


_SQL_TEXT = frozenset({"sqlalchemy.text", "sqlalchemy.sql.text", "sqlalchemy.sql.expression.text"})


def _position(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _in_query(
    node: ast.AST, parents: Mapping[int, ast.AST], names_of: Mapping[int, Mapping[str, str]]
) -> bool:
    """`node` 在同一個語句裡被組進查詢：f-string、`text()` 的參數，或和字串相接。"""
    current = node
    while not isinstance(current, ast.stmt):
        parent = parents.get(id(current))
        if parent is None:
            return False
        if isinstance(parent, ast.JoinedStr):
            return True
        if (
            isinstance(parent, ast.Call)
            and current is not parent.func
            and _full_name(parent.func, names_of[id(parent.func)]) in _SQL_TEXT
        ):
            return True
        if (
            isinstance(parent, ast.BinOp)
            and isinstance(parent.op, ast.Add)
            and any(
                isinstance(side, ast.JoinedStr)
                or (isinstance(side, ast.Constant) and isinstance(side.value, str))
                for side in (parent.left, parent.right)
            )
        ):
            return True
        current = parent
    return False


def _condition_used(
    call: ast.AST,
    own: list[ast.AST],
    parents: Mapping[int, ast.AST],
    names_of: Mapping[int, Mapping[str, str]],
) -> bool:
    """visibility 函式的回傳值用進了查詢：直接組進查詢，或指派給一個名稱、
    之後（被重新指派之前）在同一層把這個名稱組進查詢。
    """
    if _in_query(call, parents, names_of):
        return True
    statement = parents.get(id(call))
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
    elif isinstance(statement, ast.AnnAssign):
        target = statement.target
    else:
        return False
    if statement.value is not call or not isinstance(target, ast.Name):
        return False
    after = (statement.end_lineno or 0, statement.end_col_offset or 0)
    same_name = [node for node in own if isinstance(node, ast.Name) and node.id == target.id]
    rebinds = sorted(
        _position(node)
        for node in same_name
        if isinstance(node.ctx, (ast.Store, ast.Del)) and _position(node) > after
    )
    limit = rebinds[0] if rebinds else (float("inf"), 0)
    return any(
        isinstance(node.ctx, ast.Load)
        and after < _position(node) < limit
        and _in_query(node, parents, names_of)
        for node in same_name
    )


def _judges_viewer(node: ast.AST, names: Mapping[str, str]) -> bool:
    """用到讀者身分的類別、讀 `viewer` 的屬性、或 `type(viewer)`。"""
    if isinstance(node, (ast.Name, ast.Attribute)) and _is_viewer_class(_full_name(node, names)):
        return True
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id == "viewer"
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "type"
        and any(isinstance(arg, ast.Name) and arg.id == "viewer" for arg in node.args)
    )


def _passes_viewer_to_visibility(node: ast.AST, names: Mapping[str, str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    full = _full_name(node.func, names)
    if full is None or full.rpartition(".")[0] != VISIBILITY_MODULE:
        return False
    values = [*node.args, *(keyword.value for keyword in node.keywords)]
    return any(isinstance(value, ast.Name) and value.id == "viewer" for value in values)


def test_read_functions_get_visibility_from_visibility_module() -> None:
    problems = [
        problem
        for relative, source in _sources(RADAR / "repo").items()
        for problem in visibility_problems(
            _module_name(relative), source, is_package=relative.endswith("__init__.py")
        )
    ]
    assert problems == []


def test_visibility_check_catches_condition_rewritten_in_papers() -> None:
    # 拿真的 papers.py，把 get_paper 的條件改成自己判斷：一定要被抓到。
    real = _repo_sources()["radar.repo.papers"]
    rewritten = real.replace(
        'condition = visible_papers(viewer, alias="paper")',
        'condition = "TRUE" if isinstance(viewer, SystemViewer) else \'"paper".lab_visible\'',
    )
    assert rewritten != real

    problems = visibility_problems("radar.repo.papers", rewritten)

    assert len(problems) == 2, problems
    assert "自己判斷讀者身分（SystemViewer）" in problems[0]
    assert problems[1] == (
        "radar.repo.papers.get_paper：讀取函式要把 viewer 交給 radar.repo.visibility 的函式"
    )


FAKE_SELF_JUDGING_REPO = """
import radar.domain.viewer as v
from sqlalchemy import text
from radar.domain.viewer import MemberViewer, SystemViewer, Viewer
from .visibility import visible_papers


def get_by_isinstance(viewer: Viewer, connection: object) -> object:
    return "TRUE" if isinstance(viewer, SystemViewer) else "lab_visible"


def list_by_match(viewer: Viewer, connection: object) -> object:
    match viewer:
        case MemberViewer():
            return visible_papers(viewer)
    return "TRUE"


def count_by_attribute(viewer: Viewer, connection: object) -> object:
    return "x" if viewer.is_admin else visible_papers(viewer)


def find_by_module_alias(viewer: Viewer, connection: object) -> object:
    return "FALSE" if isinstance(viewer, v.PublicViewer) else visible_papers(viewer)


def search_with_someone_else(viewer: Viewer, connection: object) -> object:
    return visible_papers(SystemViewer())


def visible_by_type(viewer: Viewer, connection: object) -> object:
    return type(viewer).__name__ + visible_papers(viewer=viewer)


def get_ok(viewer: v.MemberViewer | SystemViewer, connection: object) -> object:
    reader: Viewer = viewer  # 型別註記裡的身分類別不算判斷
    return text(f"SELECT 1 FROM paper p WHERE {visible_papers(viewer, alias='p')}"), reader
"""


def test_visibility_check_catches_each_mistake() -> None:
    problems = visibility_problems("radar.repo.fake", FAKE_SELF_JUDGING_REPO)

    assert problems == [
        "radar.repo.fake:9：自己判斷讀者身分（SystemViewer），改用 radar.repo.visibility",
        "radar.repo.fake:14：自己判斷讀者身分（MemberViewer），改用 radar.repo.visibility",
        "radar.repo.fake:20：自己判斷讀者身分（viewer.is_admin），改用 radar.repo.visibility",
        "radar.repo.fake:24：自己判斷讀者身分（v.PublicViewer），改用 radar.repo.visibility",
        "radar.repo.fake:28：自己判斷讀者身分（SystemViewer），改用 radar.repo.visibility",
        "radar.repo.fake:32：自己判斷讀者身分（type(viewer)），改用 radar.repo.visibility",
        "radar.repo.fake.get_by_isinstance：讀取函式要把 viewer 交給 radar.repo.visibility 的函式",
        # 下面這幾個把條件直接 return、沒有組進查詢，也違反「條件要用進查詢」。
        f"radar.repo.fake.list_by_match：{UNUSED_CONDITION}",
        f"radar.repo.fake.count_by_attribute：{UNUSED_CONDITION}",
        f"radar.repo.fake.find_by_module_alias：{UNUSED_CONDITION}",
        "radar.repo.fake.search_with_someone_else：讀取函式要把 viewer 交給"
        " radar.repo.visibility 的函式",
        f"radar.repo.fake.visible_by_type：{UNUSED_CONDITION}",
    ]


UNUSED_CONDITION = (
    "visibility 回的可見條件要用進查詢（f-string、text() 或字串相接），不能丟掉或在用之前被覆蓋"
)

FAKE_UNUSED_CONDITION_REPO = """
from sqlalchemy import text
from radar.domain.viewer import MemberViewer, SystemViewer, Viewer
from radar.repo.visibility import visible_papers

type PaperReader = MemberViewer | SystemViewer


def get_discarded(viewer: Viewer, connection: object) -> object:
    visible_papers(viewer)
    return connection.execute(text("SELECT * FROM paper"))


def list_bound_but_unused(viewer: Viewer, connection: object) -> object:
    condition = visible_papers(viewer)
    return connection.execute(text("SELECT * FROM paper"), {"c": condition})


def count_overwritten(viewer: Viewer, connection: object) -> object:
    condition = visible_papers(viewer)
    condition = "TRUE"
    return connection.execute(text(f"SELECT count(*) FROM paper WHERE {condition}"))


def find_used_before_binding(viewer: Viewer, connection: object, condition: str) -> object:
    query = text(f"SELECT * FROM paper WHERE {condition}")
    condition = visible_papers(viewer)
    return connection.execute(query)


def search_nested_only(viewer: Viewer, connection: object) -> object:
    def helper() -> object:
        return text(f"SELECT * FROM paper WHERE {visible_papers(viewer)}")

    return connection.execute(text("SELECT * FROM paper"))


def get_assigned_ok(viewer: PaperReader, connection: object) -> object:
    condition: str = visible_papers(viewer, alias="p")
    return connection.execute(text(f"SELECT * FROM paper p WHERE {condition}"))


def list_concat_ok(viewer: Viewer, connection: object) -> object:
    condition = visible_papers(viewer)
    return connection.execute(text("SELECT * FROM paper WHERE " + condition))


def count_text_argument_ok(viewer: Viewer, connection: object) -> object:
    return connection.execute(text(visible_papers(viewer)))
"""


def test_visibility_check_requires_condition_used_in_query() -> None:
    problems = visibility_problems("radar.repo.fake", FAKE_UNUSED_CONDITION_REPO)

    # 型別別名右側的身分類別（`type PaperReader = ...`）不算判斷身分，不會被報。
    assert problems == [
        f"radar.repo.fake.get_discarded：{UNUSED_CONDITION}",
        f"radar.repo.fake.list_bound_but_unused：{UNUSED_CONDITION}",
        f"radar.repo.fake.count_overwritten：{UNUSED_CONDITION}",
        f"radar.repo.fake.find_used_before_binding：{UNUSED_CONDITION}",
        "radar.repo.fake.search_nested_only：讀取函式要把 viewer 交給 radar.repo.visibility 的函式",
    ]


def test_visibility_check_catches_discarded_condition_in_papers() -> None:
    # 拿真的 papers.py：呼叫 visible_papers 但查詢裡不放條件，一定要被抓到。
    real = _repo_sources()["radar.repo.papers"]
    rewritten = real.replace(" WHERE paper.id = :id AND {condition}", " WHERE paper.id = :id")
    assert rewritten != real

    assert visibility_problems("radar.repo.papers", rewritten) == [
        f"radar.repo.papers.get_paper：{UNUSED_CONDITION}"
    ]


def test_visibility_module_itself_is_exempt() -> None:
    source = (ROOT / "radar" / "repo" / "visibility.py").read_text(encoding="utf-8")

    assert visibility_problems(VISIBILITY_MODULE, source) == []
    # 同一份原始碼放在別的模組就違規：證明豁免只因為它是 visibility.py。
    assert visibility_problems("radar.repo.other", source) != []


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


def _is_sqlalchemy(module: str) -> bool:
    return module == "sqlalchemy" or module.startswith("sqlalchemy.")


@functools.cache
def _is_sqlalchemy_module(full: str) -> bool:
    """`full` 是 sqlalchemy 本身或它的子模組（不是類別、函式等一般物件）。"""
    if not _is_sqlalchemy(full):
        return False
    try:
        importlib.import_module(full)
    except ImportError:
        return False
    return True


def _sqlalchemy_writer_uses(source: str) -> list[str]:
    """SQLAlchemy 的寫入建構函式（`insert`／`update`／`delete`）：直接 import 進來的，
    或經 sqlalchemy 模組、子模組（含別名，依作用範圍解析）取用的。一般物件的 `.update`
    （例如 `PopulateDict.update(cache, {...})`）不算。
    """
    tree = ast.parse(source)
    names_of = _scoped_names("radar.repo", tree, is_package=True)
    uses: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and _is_sqlalchemy(node.module or "")
        ):
            uses += [
                f"from {node.module} import {alias.name}"
                for alias in node.names
                if alias.name in _SQLALCHEMY_WRITERS
            ]
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr in _SQLALCHEMY_WRITERS):
            continue
        owner = _full_name(node.value, names_of[id(node.value)])
        if owner is not None and _is_sqlalchemy_module(owner):
            uses.append(ast.unparse(node))
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
    # 掃描真的看得到寫入：papers 是 paper、external_id、摘要、關鍵字的寫入者；
    # ingest_staging 是暫存表的寫入者。
    assert written_tables(sources["radar.repo.papers"]) == {
        "paper",
        "external_id",
        "paper_abstract",
        "paper_keyword",
    }
    assert written_tables(sources["radar.repo.ingest_staging"]) == {"ingest_staging"}


def test_writer_check_catches_each_mistake() -> None:
    sources = {
        "radar.repo.a": 'SQL = "INSERT INTO paper (title) VALUES (:t)"',
        "radar.repo.b": 'SQL = "UPDATE paper p SET title = :t"',
        "radar.repo.c": 'def f(t):\n    return f"DELETE FROM {t} WHERE id = :id"',
        "radar.repo.d": "from sqlalchemy import insert",
        "radar.repo.e": "import sqlalchemy as sa\nX = sa.delete",
        "radar.repo.f": "def f(table):\n    return table.update()",
        # 子模組的別名、經子模組的完整路徑、子模組直接 import 成別名。
        "radar.repo.h": (
            "from sqlalchemy.sql import expression as sql\n"
            "def f(paper_table):\n    return sql.update(paper_table)"
        ),
        "radar.repo.i": "import sqlalchemy.sql\nX = sqlalchemy.sql.expression.insert",
        "radar.repo.j": "import sqlalchemy.sql.dml as dml\nX = dml.delete",
        "radar.repo.k": "from sqlalchemy import sql\nX = sql.expression.update",
        # 一般物件的 `.update`（不是寫入建構函式）不算；函式裡的局部 import 也看得到。
        "radar.repo.l": (
            "from sqlalchemy.util import PopulateDict\n"
            "from sqlalchemy import util\n"
            "def f(cache):\n"
            "    PopulateDict.update(cache, {'a': 1})\n"
            "    util.PopulateDict.update(cache, {'b': 2})"
        ),
        "radar.repo.m": "def f(t):\n    import sqlalchemy as sa\n    return sa.update(t)",
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
        "radar.repo.h：不要用 SQLAlchemy 的寫入（sql.update），改用 text() SQL",
        "radar.repo.i：不要用 SQLAlchemy 的寫入（sqlalchemy.sql.expression.insert），"
        "改用 text() SQL",
        "radar.repo.j：不要用 SQLAlchemy 的寫入（dml.delete），改用 text() SQL",
        "radar.repo.k：不要用 SQLAlchemy 的寫入（sql.expression.update），改用 text() SQL",
        "radar.repo.m：不要用 SQLAlchemy 的寫入（sa.update），改用 text() SQL",
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


def actor_problems(module: str, source: str, *, is_package: bool = False) -> list[str]:
    """services 模組裡，用到（直接或經私有函式）寫入類 repo 函式、
    第一個參數卻不是 `actor` 的公開函式。相對 import 先解析成完整模組路徑。
    """
    tree = ast.parse(source)
    names_of = _scoped_names(module, tree, is_package=is_package)

    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def is_repo_writer(node: ast.AST) -> bool:
        full = _full_name(node, names_of[id(node)])
        if full is None:
            return False
        owner, _, name = full.rpartition(".")
        return _is_repo_module(owner) and name.startswith(WRITE_PREFIXES)

    def writes_directly(function: ast.AST) -> bool:
        return any(
            isinstance(node, (ast.Name, ast.Attribute)) and is_repo_writer(node)
            for node in ast.walk(function)
        )

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
        for problem in actor_problems(
            _module_name(relative), source, is_package=relative.endswith("__init__.py")
        )
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


FAKE_SERVICES_RELATIVE = """
from .. import repo
from ..repo import papers as repo_papers
from ..repo.papers import upsert_by_external_id
from ..repo.papers import get_paper
from . import helpers
from radar import repo as absolute_repo


def via_relative_function(database, paper):
    upsert_by_external_id(database, paper)


def via_relative_module(database):
    repo_papers.create_manual(database)


def via_relative_package(database):
    repo.papers.delete_paper(database)


def via_absolute_package(database):
    absolute_repo.papers.update_paper(database)


def not_repo(database):
    helpers.update_cache(database)


def relative_ok(actor, database, paper):
    upsert_by_external_id(database, paper)


def relative_read_only(viewer, connection, paper_id):
    return get_paper(viewer, connection, paper_id)
"""


def test_actor_check_resolves_relative_imports() -> None:
    problems = actor_problems("radar.services.fake", FAKE_SERVICES_RELATIVE)

    assert problems == [
        "radar.services.fake.via_relative_function：用到寫入類 repo 函式，第一個參數要叫 actor",
        "radar.services.fake.via_relative_module：用到寫入類 repo 函式，第一個參數要叫 actor",
        "radar.services.fake.via_relative_package：用到寫入類 repo 函式，第一個參數要叫 actor",
        "radar.services.fake.via_absolute_package：用到寫入類 repo 函式，第一個參數要叫 actor",
    ]


def test_relative_imports_resolve_from_package_init() -> None:
    # `__init__.py` 的相對 import 以套件本身為起點：`radar/services/__init__.py` 裡的
    # `from ..repo.papers` 是 `radar.repo.papers`；`radar/services/sub/__init__.py` 裡的同一句
    # 是 `radar.services.repo.papers`（不是 repo）。
    source = "from ..repo.papers import upsert_by_external_id\n" + (
        "def f(database):\n    upsert_by_external_id(database)\n"
    )

    assert actor_problems("radar.services.sub", source, is_package=True) == []
    assert actor_problems("radar.services", source, is_package=True) == [
        "radar.services.f：用到寫入類 repo 函式，第一個參數要叫 actor"
    ]


FAKE_SERVICES_LOCAL_IMPORTS = """
from radar.repo.papers import upsert_by_external_id as write


def ingest(database, paper):
    write(database, paper)


def other():
    from pathlib import Path as write

    return write(".")


def local_repo_import(database):
    from radar.repo.papers import create_manual

    create_manual(database)


def local_name_elsewhere(database):
    create_manual = print
    create_manual(database)
"""


def test_actor_check_resolves_imports_per_scope() -> None:
    # 另一個函式裡的 `from pathlib import Path as write` 不能蓋掉模組層的 `write`（repo 寫入）；
    # 函式裡局部 import 的寫入函式只算在那個函式。
    problems = actor_problems("radar.services.fake", FAKE_SERVICES_LOCAL_IMPORTS)

    assert problems == [
        "radar.services.fake.ingest：用到寫入類 repo 函式，第一個參數要叫 actor",
        "radar.services.fake.local_repo_import：用到寫入類 repo 函式，第一個參數要叫 actor",
    ]
