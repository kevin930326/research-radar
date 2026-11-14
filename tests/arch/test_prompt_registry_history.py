"""prompt 雜湊登記只增不改（ADR-0030）：已經進 main 的登記不能刪、不能改雜湊。

`test_prompt_registry.py` 只核對「檔和登記對得上」：同時改了 `v1.md` 和它登記的雜湊、版本不變，
兩邊照樣對得上，抓不到。這裡改拿 git 歷史比：`main`（優先 `origin/main`）上每一個改過
`radar/domain/prompt_hashes.py` 的 commit 裡登記過的（工作, 版本），現在的檔裡都要還在、雜湊
一模一樣。還沒合併進 main 的登記（同一個分支上新增又修改）不受限制。

- 拿不到完整歷史（淺層 clone）或找不到 main 時直接失敗並說明，不默默通過；CI 的 checkout 用
  `fetch-depth: 0`（`test_ci_workflow.py` 守著），看得到 `origin/main`。
- 比對函式另外在 tmp 的 git repo 示範：同時改模板與雜湊、版本不變會被抓到；刪掉登記會被抓到；
  新增版本、分支上還沒合併的修改不會被誤抓；淺層 clone、沒有 main 會明確失敗。
"""

import ast
import hashlib
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = "radar/domain/prompt_hashes.py"
BASE_REFS = ("origin/main", "main")
TABLE_NAME = "PROMPT_FILE_HASHES"

type Registrations = dict[tuple[str, int], tuple[str, str | None]]


class RegistryHistoryUnavailableError(RuntimeError):
    """拿不到可以比對的 main 歷史（淺層 clone、沒有 main）。"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
    return result.stdout


def registered_hashes(source: str) -> Registrations:
    """從登記模組的原始碼讀出 `PROMPT_FILE_HASHES`，攤平成（工作, 版本）→ 雜湊。

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
            table = ast.literal_eval(statement.value)
            return {
                (task, version): tuple(hashes)
                for task, versions in table.items()
                for version, hashes in versions.items()
            }
    raise ValueError(f"找不到 {TABLE_NAME}")


def registration_changes(old: Registrations, new: Registrations, *, where: str) -> list[str]:
    """`old` 裡登記過的版本在 `new` 被刪掉或改了雜湊的地方。新增的版本不算。"""
    problems: list[str] = []
    for (task, version), hashes in sorted(old.items()):
        if (task, version) not in new:
            problems.append(f"{task} v{version}：{where} 登記過，現在被刪掉了")
        elif new[(task, version)] != hashes:
            problems.append(
                f"{task} v{version}：雜湊和 {where} 登記的不一樣（已登記的版本不能改，請新增"
                f" v{version + 1}.md 並登記）"
            )
    return problems


def base_commit(repo: Path) -> str:
    """HEAD 與 main（優先 `origin/main`）的分岔點。

    Raises:
        RegistryHistoryUnavailableError: 淺層 clone，或找不到 main。
    """
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() != "false":
        raise RegistryHistoryUnavailableError(
            "淺層 clone 看不到完整歷史，無法核對 prompt 登記只增不改（CI 要 fetch-depth: 0）"
        )
    for ref in BASE_REFS:
        try:
            _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        except subprocess.CalledProcessError:
            continue
        return _git(repo, "merge-base", "HEAD", ref).strip()
    raise RegistryHistoryUnavailableError(
        f"找不到 {' 或 '.join(BASE_REFS)}，無法核對 prompt 登記只增不改"
    )


def registry_history_problems(repo: Path, current_source: str) -> list[str]:
    """main 歷史上每一版登記檔裡的登記，在 `current_source` 被刪掉或改掉的地方。"""
    base = base_commit(repo)
    commits = _git(repo, "log", "--format=%H", "--diff-filter=d", base, "--", REGISTRY_PATH).split()
    current = registered_hashes(current_source)
    problems: set[str] = set()
    for sha in commits:
        old = registered_hashes(_git(repo, "show", f"{sha}:{REGISTRY_PATH}"))
        problems.update(registration_changes(old, current, where=sha[:12]))
    return sorted(problems)


def test_registered_prompt_versions_are_never_changed_or_removed() -> None:
    current = (ROOT / REGISTRY_PATH).read_text(encoding="utf-8")
    try:
        problems = registry_history_problems(ROOT, current)
    except RegistryHistoryUnavailableError as exc:
        pytest.fail(str(exc), pytrace=False)

    assert problems == []


# --- tmp 的 git repo 示範 ----------------------------------------------------------------------

GIT_CONFIG = (
    "-c",
    "user.name=registry-test",
    "-c",
    "user.email=registry-test@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
)


def _registry_source(table: dict[str, dict[int, tuple[str, str | None]]]) -> str:
    return (
        '"""測試用的登記。"""\n\n'
        "CURRENT_PROMPT_VERSIONS: dict[str, int] = {}\n\n"
        f"{TABLE_NAME}: dict[str, dict[int, tuple[str, str | None]]] = {table!r}\n"
    )


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, *GIT_CONFIG, "commit", "--quiet", "-m", message)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt_commit(repo: Path, template: str, table: dict[int, str], message: str) -> None:
    _commit(
        repo,
        {
            "prompts/zh_summary/v1.md": template,
            REGISTRY_PATH: _registry_source(
                {"zh_summary": {version: (file_hash, None) for version, file_hash in table.items()}}
            ),
        },
        message,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main 上已經登記了 zh_summary v1，接著切出 feature 分支。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, *GIT_CONFIG, "init", "--quiet", "--initial-branch=main")
    template = "第一版的說明。"
    _prompt_commit(root, template, {1: _sha(template)}, "登記 v1")
    _git(root, "switch", "--quiet", "--create", "feature")
    return root


def _current(repo: Path) -> str:
    return (repo / REGISTRY_PATH).read_text(encoding="utf-8")


def test_template_and_hash_changed_together_without_version_bump_fails(repo: Path) -> None:
    # 模板與登記的雜湊一起改、版本不變：檔和登記對得上，但和 main 上登記的不一樣。
    template = "偷偷改了第一版的說明。"
    _prompt_commit(repo, template, {1: _sha(template)}, "改了 v1 也改了雜湊")

    problems = registry_history_problems(repo, _current(repo))

    assert len(problems) == 1
    assert problems[0].startswith("zh_summary v1：雜湊和 ")
    assert "不能改" in problems[0]


def test_uncommitted_change_is_checked_too(repo: Path) -> None:
    (repo / REGISTRY_PATH).write_text(
        _registry_source({"zh_summary": {1: ("0" * 64, None)}}), encoding="utf-8"
    )

    assert len(registry_history_problems(repo, _current(repo))) == 1


def test_removed_registration_fails(repo: Path) -> None:
    _commit(repo, {REGISTRY_PATH: _registry_source({})}, "刪掉 v1 的登記")

    problems = registry_history_problems(repo, _current(repo))

    assert len(problems) == 1
    assert problems[0].startswith("zh_summary v1：")
    assert problems[0].endswith("登記過，現在被刪掉了")


def test_adding_a_version_and_editing_unmerged_one_pass(repo: Path) -> None:
    template = "第一版的說明。"
    _prompt_commit(repo, template, {1: _sha(template), 2: "a" * 64}, "新增 v2")
    # v2 還沒進 main：分支上再改它不算改已登記的版本。
    _prompt_commit(repo, template, {1: _sha(template), 2: "b" * 64}, "修正 v2")

    assert registry_history_problems(repo, _current(repo)) == []


def test_change_already_merged_into_main_is_checked_against_every_main_commit(
    repo: Path,
) -> None:
    # main 上後來又登記了 v2；現在的分支（從新的 main 切出）改了 v2 的雜湊也要被抓到。
    template = "第一版的說明。"
    _git(repo, "switch", "--quiet", "main")
    _prompt_commit(repo, template, {1: _sha(template), 2: "a" * 64}, "main 登記 v2")
    _git(repo, "switch", "--quiet", "--create", "later")
    _prompt_commit(repo, template, {1: _sha(template), 2: "c" * 64}, "改了 v2")

    problems = registry_history_problems(repo, _current(repo))

    assert [problem.split("：")[0] for problem in problems] == ["zh_summary v2"]


def test_shallow_clone_fails_loudly(repo: Path, tmp_path: Path) -> None:
    template = "第一版的說明。"
    _prompt_commit(repo, template, {1: _sha(template), 2: "a" * 64}, "第二個 commit")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "--quiet", "--depth", "1", f"file://{repo}", str(shallow))

    with pytest.raises(RegistryHistoryUnavailableError, match="淺層"):
        registry_history_problems(shallow, _current(shallow))


def test_missing_main_fails_loudly(repo: Path) -> None:
    _git(repo, "branch", "--quiet", "--move", "main", "trunk")

    with pytest.raises(RegistryHistoryUnavailableError, match="main"):
        registry_history_problems(repo, _current(repo))
