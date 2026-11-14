"""分類表內容雜湊的登記（`radar/domain/taxonomy_hashes.py`）的架構規則（Story 2.5、ADR-0033）：

- 那個模組只能寫字面值（和 `prompt_hashes.py` 同一套規則），登記不會在執行期由別的資料組出來。
- `radar/` 裡只有 `domain/taxonomy.py`（`taxonomy_registry_problems`）提到 `taxonomy_hashes`；
  沒有程式能改登記表，設定檔也只能經它核對。

- 登記只增不改：比照 prompt 登記（`test_prompt_registry_history.py`，共用它的 git 輔助函式），
  拿 main（優先 `origin/main`）**第一父提交那條線**上改過登記檔的 commit（登記進入 main 時的
  快照）和現在的檔比。已登記的版本雜湊被改（例如設定檔內容與雜湊一起改、版本不變）就失敗；
  HEAD 拿到之後又刪掉也失敗。新增版本、分支上還沒進 main 的修改不算。淺層 clone、找不到 main
  明確失敗，不默默通過。

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
HASHES = RADAR / "domain" / "taxonomy_hashes.py"
TAXONOMY = RADAR / "domain" / "taxonomy.py"
MODULE_NAME = "taxonomy_hashes"


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


def test_taxonomy_hashes_module_is_literals_only() -> None:
    assert literal_module_violations(HASHES, base=ROOT) == []


def test_only_the_taxonomy_module_mentions_the_registry() -> None:
    offenders = [
        hit
        for path in sorted(RADAR.rglob("*.py"))
        if "__pycache__" not in path.parts and path not in (TAXONOMY, HASHES)
        for hit in reference_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_taxonomy_module_really_uses_it() -> None:
    # 真的用到：代表掃描的名稱對得上實際寫法。
    assert reference_violations(TAXONOMY, base=ROOT) != []


def test_literal_scanner_catches_a_computed_registry(tmp_path: Path) -> None:
    sample = tmp_path / "taxonomy_hashes.py"
    sample.write_text(
        '"""說明"""\n'
        "from radar.domain.fingerprints import taxonomy_content_sha256\n"
        "TAXONOMY_CONTENT_HASHES: dict[int, str] = {1: taxonomy_content_sha256(x)}\n",
        encoding="utf-8",
    )

    assert literal_module_violations(sample, base=tmp_path) == [
        "taxonomy_hashes.py:2 不允許的寫法（ImportFrom）",
        "taxonomy_hashes.py:3 TAXONOMY_CONTENT_HASHES 的值不是字面值",
    ]


def test_reference_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "service.py"
    sample.write_text(
        "import radar.domain.taxonomy_hashes\n"
        "from radar.domain import taxonomy_hashes\n"
        "from radar.domain.taxonomy_hashes import TAXONOMY_CONTENT_HASHES\n"
        "a = domain.taxonomy_hashes\n"
        "b = importlib.import_module('radar.domain.taxonomy_hashes')\n"
        "c = 'taxonomy_hashes_dir is fine'\n",
        encoding="utf-8",
    )

    assert sorted(reference_violations(sample, base=tmp_path)) == sorted(
        [
            "service.py:1 import radar.domain.taxonomy_hashes",
            "service.py:2 import taxonomy_hashes",
            "service.py:3 import taxonomy_hashes",
            "service.py:4 用 .taxonomy_hashes",
            "service.py:5 用字串 'radar.domain.taxonomy_hashes'",
        ]
    )


# --- 只增不改：和 main 上登記過的比 -------------------------------------------------------------

REGISTRY_PATH = "radar/domain/taxonomy_hashes.py"
TABLE_NAME = "TAXONOMY_CONTENT_HASHES"


def registered_hashes(source: str) -> dict[int, str]:
    """從登記模組的原始碼讀出 `TAXONOMY_CONTENT_HASHES`（版本 → 內容雜湊）。

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
                problems.append(f"分類表 v{version}：{where} 登記過，現在被刪掉了")
        elif new[version] != content_hash:
            problems.append(
                f"分類表 v{version}：雜湊和 {where} 登記的不一樣（已登記的版本不能改，請把"
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


def taxonomy_history_problems(repo: Path, current_source: str) -> list[str]:
    """main 第一父提交那條線上每一版登記檔裡的登記，在 `current_source` 被改掉（或 HEAD 拿到之後
    又刪掉）的地方。

    Raises:
        RegistryHistoryUnavailableError: 淺層 clone，或找不到 main。
    """
    try:
        ref = main_ref(repo)
    except RegistryHistoryUnavailableError as exc:
        # 共用的函式訊息寫的是 prompt 登記；這裡核對的是分類表登記。
        raise RegistryHistoryUnavailableError(
            str(exc).replace("prompt 登記", "分類表登記")
        ) from exc
    # --first-parent：merge commit 只和它的第一父（合併前的 main）比，分支內部的 commit 不看。
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


def test_registered_taxonomy_versions_are_never_changed_or_removed() -> None:
    current = HASHES.read_text(encoding="utf-8")
    try:
        problems = taxonomy_history_problems(ROOT, current)
    except RegistryHistoryUnavailableError as exc:
        pytest.fail(str(exc), pytrace=False)

    assert problems == []


# --- tmp 的 git repo 示範 ----------------------------------------------------------------------

CONFIG_PATH = "config/taxonomy.yaml"


def _registry_source(table: dict[int, str]) -> str:
    return f'"""測試用的登記。"""\n\n{TABLE_NAME}: dict[int, str] = {table!r}\n'


def _commit(repo: Path, definition: str, table: dict[int, str], message: str) -> None:
    files = {
        CONFIG_PATH: f"version: 1\ntopics:\n  - key: erp\n    definition: {definition}\n",
        REGISTRY_PATH: _registry_source(table),
    }
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, *GIT_CONFIG, "commit", "--quiet", "-m", message)


def _merge_into_main(repo: Path, branch: str) -> None:
    """用保留分支提交的 merge commit（`--no-ff`）把 `branch` 合併進 main。"""
    _git(repo, "switch", "--quiet", "main")
    _git(repo, *GIT_CONFIG, "merge", "--no-ff", "--quiet", "-m", f"合併 {branch}", branch)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main 上已經登記了分類表 v1，接著切出 feature 分支。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, *GIT_CONFIG, "init", "--quiet", "--initial-branch=main")
    _commit(root, "企業資源規劃。", {1: "a" * 64}, "登記 v1")
    _git(root, "switch", "--quiet", "--create", "feature")
    return root


def _current(repo: Path) -> str:
    return (repo / REGISTRY_PATH).read_text(encoding="utf-8")


def test_content_and_hash_changed_together_without_version_bump_fails(repo: Path) -> None:
    # 設定檔內容與登記的雜湊一起改、版本不變：讀設定時對得上，但和 main 上登記的不一樣。
    _commit(repo, "企業系統。", {1: "b" * 64}, "改了 v1 的定義也改了雜湊")

    problems = taxonomy_history_problems(repo, _current(repo))

    assert len(problems) == 1
    assert problems[0].startswith("分類表 v1：雜湊和 ")
    assert "不能改" in problems[0]


def test_uncommitted_change_is_checked_too(repo: Path) -> None:
    (repo / REGISTRY_PATH).write_text(_registry_source({1: "0" * 64}), encoding="utf-8")

    assert len(taxonomy_history_problems(repo, _current(repo))) == 1


def test_removed_registration_fails(repo: Path) -> None:
    _commit(repo, "企業資源規劃。", {}, "刪掉 v1 的登記")

    problems = taxonomy_history_problems(repo, _current(repo))

    assert len(problems) == 1
    assert problems[0].startswith("分類表 v1：")
    assert problems[0].endswith("登記過，現在被刪掉了")


def test_adding_a_version_and_editing_unmerged_one_pass(repo: Path) -> None:
    _commit(repo, "企業系統。", {1: "a" * 64, 2: "c" * 64}, "新增 v2")
    # v2 還沒進 main：分支上再改它不算改已登記的版本。
    _commit(repo, "企業系統與 ERP。", {1: "a" * 64, 2: "d" * 64}, "修正 v2")

    assert taxonomy_history_problems(repo, _current(repo)) == []


def test_unmerged_edits_brought_in_by_merge_commit_pass_and_later_edit_fails(repo: Path) -> None:
    _commit(repo, "企業系統。", {1: "a" * 64, 2: "c" * 64}, "分支新增 v2")
    _commit(repo, "企業系統與 ERP。", {1: "a" * 64, 2: "d" * 64}, "分支內改 v2")
    _merge_into_main(repo, "feature")
    _git(repo, "switch", "--quiet", "--create", "next")

    assert taxonomy_history_problems(repo, _current(repo)) == []

    # 進 main 之後再改 v2：要失敗。
    _commit(repo, "偷改。", {1: "a" * 64, 2: "e" * 64}, "進 main 後再改 v2")

    problems = taxonomy_history_problems(repo, _current(repo))

    assert [problem.split("：")[0] for problem in problems] == ["分類表 v2"]


def test_version_added_on_main_after_branching_is_not_reported_missing(repo: Path) -> None:
    _git(repo, "switch", "--quiet", "main")
    _commit(repo, "企業系統。", {1: "a" * 64, 2: "c" * 64}, "main 登記 v2")
    _git(repo, "switch", "--quiet", "feature")

    assert taxonomy_history_problems(repo, _current(repo)) == []


def test_shallow_clone_fails_loudly(repo: Path, tmp_path: Path) -> None:
    _commit(repo, "企業系統。", {1: "a" * 64, 2: "c" * 64}, "第二個 commit")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "--quiet", "--depth", "1", f"file://{repo}", str(shallow))

    with pytest.raises(RegistryHistoryUnavailableError, match=r"淺層.*分類表登記"):
        taxonomy_history_problems(shallow, _current(shallow))


def test_missing_main_fails_loudly(repo: Path) -> None:
    _git(repo, "branch", "--quiet", "--move", "main", "trunk")

    with pytest.raises(RegistryHistoryUnavailableError, match="分類表登記"):
        taxonomy_history_problems(repo, _current(repo))
