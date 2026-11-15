"""情境值清單內容雜湊的登記（`radar/domain/context_values_hashes.py`）的架構規則
（Story 2.6、ADR-0034）：

- 那個模組只能寫字面值（和 `prompt_hashes.py` 同一套規則），登記不會在執行期由別的資料組出來。
- `radar/` 裡只有 `domain/context_values.py`（`context_values_registry_problems`）提到
  `context_values_hashes`；沒有程式能改登記表，設定檔也只能經它核對。

- 登記只增不改：比照分類表登記（`test_taxonomy_hashes.py`）與 prompt 登記
  （`test_prompt_registry_history.py`，共用它的 git 輔助函式），拿 main（優先 `origin/main`）
  **第一父提交那條線**上改過登記檔的 commit 和現在的檔比。已登記的版本雜湊被改（例如設定檔內容
  與雜湊一起改、版本不變）就失敗；HEAD 拿到之後又刪掉也失敗。新增版本、分支上還沒進 main 的修改
  不算。淺層 clone、找不到 main 明確失敗，不默默通過。

掃描函式另外用 tmp 裡的假檔、比對函式用 tmp 裡的 git repo 證明抓得到、也不誤抓。
"""

import ast
import subprocess
from pathlib import Path

import pytest

from tests.arch.test_prompt_hashes import literal_module_violations
from tests.arch.test_prompt_registry_history import (
    GIT_CONFIG,
    RegistryHistoryUnavailableError,
    _git,
    _is_ancestor_of_head,
    main_ref,
)

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
HASHES = RADAR / "domain" / "context_values_hashes.py"
CONTEXT_VALUES = RADAR / "domain" / "context_values.py"
MODULE_NAME = "context_values_hashes"


def reference_violations(path: Path, *, base: Path) -> list[str]:
    """這個檔裡提到登記模組的地方（import、屬性、名字、字串）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = path.relative_to(base).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                f"{where}:{node.lineno} import {alias.name}"
                for alias in node.names
                if MODULE_NAME in alias.name.split(".")
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if MODULE_NAME in module.split(".") or any(a.name == MODULE_NAME for a in node.names):
                found.append(f"{where}:{node.lineno} import {MODULE_NAME}")
        elif isinstance(node, ast.Name) and node.id == MODULE_NAME:
            found.append(f"{where}:{node.lineno} 用 {MODULE_NAME}")
        elif isinstance(node, ast.Attribute) and node.attr == MODULE_NAME:
            found.append(f"{where}:{node.lineno} 用 .{MODULE_NAME}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and MODULE_NAME in node.value.split(".")
        ):
            found.append(f"{where}:{node.lineno} 用字串 {node.value!r}")
    return found


def test_context_values_hashes_module_is_literals_only() -> None:
    assert literal_module_violations(HASHES, base=ROOT) == []


def test_only_the_context_values_module_mentions_the_registry() -> None:
    offenders = [
        hit
        for path in sorted(RADAR.rglob("*.py"))
        if "__pycache__" not in path.parts and path not in (CONTEXT_VALUES, HASHES)
        for hit in reference_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_context_values_module_really_uses_it() -> None:
    # 真的用到：代表掃描的名稱對得上實際寫法。
    assert reference_violations(CONTEXT_VALUES, base=ROOT) != []


def test_literal_scanner_catches_a_computed_registry(tmp_path: Path) -> None:
    sample = tmp_path / "context_values_hashes.py"
    sample.write_text(
        '"""說明"""\n'
        "from radar.domain.fingerprints import context_values_content_sha256\n"
        "CONTEXT_VALUES_CONTENT_HASHES: dict[int, str] = {1: context_values_content_sha256(x)}\n",
        encoding="utf-8",
    )

    assert literal_module_violations(sample, base=tmp_path) == [
        "context_values_hashes.py:2 不允許的寫法（ImportFrom）",
        "context_values_hashes.py:3 CONTEXT_VALUES_CONTENT_HASHES 的值不是字面值",
    ]


def test_reference_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "service.py"
    sample.write_text(
        "import radar.domain.context_values_hashes\n"
        "from radar.domain import context_values_hashes\n"
        "from radar.domain.context_values_hashes import CONTEXT_VALUES_CONTENT_HASHES\n"
        "a = domain.context_values_hashes\n"
        "b = importlib.import_module('radar.domain.context_values_hashes')\n"
        "c = 'context_values_hashes_dir is fine'\n",
        encoding="utf-8",
    )

    assert sorted(reference_violations(sample, base=tmp_path)) == sorted(
        [
            "service.py:1 import radar.domain.context_values_hashes",
            "service.py:2 import context_values_hashes",
            "service.py:3 import context_values_hashes",
            "service.py:4 用 .context_values_hashes",
            "service.py:5 用字串 'radar.domain.context_values_hashes'",
        ]
    )


# --- 只增不改：和 main 上登記過的比 -------------------------------------------------------------

REGISTRY_PATH = "radar/domain/context_values_hashes.py"
TABLE_NAME = "CONTEXT_VALUES_CONTENT_HASHES"


def registered_hashes(source: str) -> dict[int, str]:
    """從登記模組的原始碼讀出 `CONTEXT_VALUES_CONTENT_HASHES`（版本 → 內容雜湊）。

    Raises:
        ValueError: 找不到這張表，或它不是字面值。
    """
    for statement in ast.parse(source).body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == TABLE_NAME
            and statement.value is not None
        ):
            return dict(ast.literal_eval(statement.value))
    raise ValueError(f"找不到 {TABLE_NAME}")


def registration_changes(
    old: dict[int, str], new: dict[int, str], *, where: str, check_removed: bool = True
) -> list[str]:
    """`old` 登記過的版本在 `new` 被改了雜湊（`check_removed` 時也看被刪掉）的地方；新增的不算。"""
    problems: list[str] = []
    for version, content_hash in sorted(old.items()):
        if version not in new:
            if check_removed:
                problems.append(f"情境值清單 v{version}：{where} 登記過，現在被刪掉了")
        elif new[version] != content_hash:
            problems.append(
                f"情境值清單 v{version}：雜湊和 {where} 登記的不一樣（已登記的版本不能改，請把"
                f" version 加一並登記新版本）"
            )
    return problems


def _has_registry(repo: Path, sha: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}:{REGISTRY_PATH}"],
        cwd=repo,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def context_values_history_problems(repo: Path, current_source: str) -> list[str]:
    """main 第一父提交那條線上每一版登記檔裡的登記，在 `current_source` 被改掉（或 HEAD 拿到之後
    又刪掉）的地方。

    Raises:
        RegistryHistoryUnavailableError: 淺層 clone，或找不到 main。
    """
    try:
        ref = main_ref(repo)
    except RegistryHistoryUnavailableError as exc:
        # 共用的函式訊息寫的是 prompt 登記；這裡核對的是情境值清單登記。
        raise RegistryHistoryUnavailableError(
            str(exc).replace("prompt 登記", "情境值清單登記")
        ) from exc
    commits = _git(repo, "rev-list", "--first-parent", ref, "--", REGISTRY_PATH).split()
    current = registered_hashes(current_source)
    problems: set[str] = set()
    for sha in commits:
        if not _has_registry(repo, sha):  # 這個 commit 刪了登記檔
            continue
        old = registered_hashes(_git(repo, "show", f"{sha}:{REGISTRY_PATH}"))
        problems.update(
            registration_changes(
                old, current, where=sha[:12], check_removed=_is_ancestor_of_head(repo, sha)
            )
        )
    return sorted(problems)


def test_registered_context_values_versions_are_never_changed_or_removed() -> None:
    current = HASHES.read_text(encoding="utf-8")
    try:
        problems = context_values_history_problems(ROOT, current)
    except RegistryHistoryUnavailableError as exc:
        pytest.fail(str(exc), pytrace=False)

    assert problems == []


# --- tmp 的 git repo 示範 ----------------------------------------------------------------------

CONFIG_PATH = "config/context_values.yaml"


def _registry_source(table: dict[int, str]) -> str:
    return f'"""測試用的登記。"""\n\n{TABLE_NAME}: dict[int, str] = {table!r}\n'


def _commit(repo: Path, definition: str, table: dict[int, str], message: str) -> None:
    files = {
        CONFIG_PATH: f"version: 1\ncountries:\n  - key: tw\n    definition: {definition}\n",
        REGISTRY_PATH: _registry_source(table),
    }
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, *GIT_CONFIG, "commit", "--quiet", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main 上已經登記了情境值清單 v1，接著切出 feature 分支。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, *GIT_CONFIG, "init", "--quiet", "--initial-branch=main")
    _commit(root, "Taiwan", {1: "a" * 64}, "登記 v1")
    _git(root, "switch", "--quiet", "--create", "feature")
    return root


def _current(repo: Path) -> str:
    return (repo / REGISTRY_PATH).read_text(encoding="utf-8")


def test_content_and_hash_changed_together_without_version_bump_fails(repo: Path) -> None:
    _commit(repo, "Taiwan (ROC)", {1: "b" * 64}, "改了 v1 的定義也改了雜湊")

    problems = context_values_history_problems(repo, _current(repo))

    assert len(problems) == 1
    assert problems[0].startswith("情境值清單 v1：雜湊和 ")


def test_removed_registration_fails(repo: Path) -> None:
    _commit(repo, "Taiwan", {}, "刪掉 v1 的登記")

    problems = context_values_history_problems(repo, _current(repo))

    assert len(problems) == 1
    assert problems[0].endswith("登記過，現在被刪掉了")


def test_adding_a_version_and_editing_unmerged_one_pass(repo: Path) -> None:
    _commit(repo, "Taiwan (ROC)", {1: "a" * 64, 2: "c" * 64}, "新增 v2")
    _commit(repo, "Taiwan, ROC", {1: "a" * 64, 2: "d" * 64}, "修正 v2")

    assert context_values_history_problems(repo, _current(repo)) == []


def test_shallow_clone_fails_loudly(repo: Path, tmp_path: Path) -> None:
    _commit(repo, "Taiwan (ROC)", {1: "a" * 64, 2: "c" * 64}, "第二個 commit")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "--quiet", "--depth", "1", f"file://{repo}", str(shallow))

    with pytest.raises(RegistryHistoryUnavailableError, match=r"淺層.*情境值清單登記"):
        context_values_history_problems(shallow, _current(shallow))


def test_missing_main_fails_loudly(repo: Path) -> None:
    _git(repo, "branch", "--quiet", "--move", "main", "trunk")

    with pytest.raises(RegistryHistoryUnavailableError, match="情境值清單登記"):
        context_values_history_problems(repo, _current(repo))
