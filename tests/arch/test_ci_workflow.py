"""CI workflow 與 pytest 網路封鎖的規則（ADR-0017、AD-29）。"""

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
PUSH_ONLY = "github.event_name == 'push'"
# AC 規定的順序；ruff 分成「風格」與「排版」兩步。
CHECK_ORDER = [
    "ruff",
    "ruff-format",
    "mypy",
    "import-linter",
    "pytest-unit",
    "pytest-integration",
    "pytest-arch",
    "gitleaks",
    "pip-audit",
]
PINNED_USES = re.compile(r"^\s*(?:-\s+)?uses:\s+[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+#\s+v\d+\S*\s*$")


def _text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _workflow() -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(_text())
    return data


def _jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = _workflow()["jobs"]
    return jobs


def _steps(job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _jobs()[job]["steps"]
    return steps


def test_triggers_on_every_branch_push_and_every_pull_request() -> None:
    workflow = _workflow()
    # YAML 1.1 會把沒加引號的 on 讀成 True。
    triggers: dict[str, Any] = workflow["on"] if "on" in workflow else workflow[True]
    assert set(triggers) == {"push", "pull_request"}
    assert triggers["push"] == {"branches": ["**"]}
    assert not triggers["pull_request"]  # 不限 branches、paths


def test_workflow_uses_no_repo_secrets() -> None:
    assert "secrets." not in _text()


def test_third_party_actions_are_pinned_to_commit_sha() -> None:
    uses_lines = [line for line in _text().splitlines() if re.match(r"^\s*(-\s+)?uses:", line)]
    assert uses_lines
    unpinned = [line.strip() for line in uses_lines if not PINNED_USES.match(line)]
    assert unpinned == []


def test_permissions_are_minimal() -> None:
    assert _workflow()["permissions"] == {"contents": "read"}
    jobs = _jobs()
    assert "permissions" not in jobs["checks"]
    for name in ("image", "manifest"):
        assert jobs[name]["permissions"] == {"contents": "read", "packages": "write"}


def test_checks_run_in_required_order() -> None:
    ids = [step["id"] for step in _steps("checks") if "id" in step]
    assert ids == CHECK_ORDER


def test_checks_commands() -> None:
    runs = {step["id"]: step["run"] for step in _steps("checks") if "id" in step}
    for marker in ("unit", "integration", "arch"):
        assert f"pytest -m {marker}" in runs[f"pytest-{marker}"]
    assert "git --config .gitleaks.toml --redact" in runs["gitleaks"]
    assert "sha256sum --check" in runs["gitleaks"]
    assert "--all-groups" in runs["pip-audit"]
    assert "pip-audit" in runs["pip-audit"]
    checkout = _steps("checks")[0]
    assert checkout["with"]["fetch-depth"] == 0  # gitleaks 掃整段歷史


def test_gitleaks_binary_is_pinned_with_sha256() -> None:
    gitleaks = next(step for step in _steps("checks") if step.get("id") == "gitleaks")
    assert re.fullmatch(r"\d+\.\d+\.\d+", gitleaks["env"]["GITLEAKS_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", gitleaks["env"]["GITLEAKS_SHA256"])


def test_image_builds_natively_on_both_architectures_after_checks() -> None:
    image = _jobs()["image"]
    assert image["needs"] == "checks"
    runners = {entry["arch"]: entry["runner"] for entry in image["strategy"]["matrix"]["include"]}
    assert runners == {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
    assert image["runs-on"] == "${{ matrix.runner }}"
    assert "qemu" not in _text().lower()


def test_image_smoke_runs_before_push_and_push_only_on_push_event() -> None:
    steps = _steps("image")
    runs = [step.get("run", "") for step in steps]
    smoke = next(i for i, run in enumerate(runs) if ".github/scripts/smoke.sh" in run)
    for i, step in enumerate(steps):
        if "docker login" in runs[i] or "docker push" in runs[i]:
            assert i > smoke, step["name"]
            assert step["if"] == PUSH_ONLY, step["name"]
    assert any("docker push" in run for run in runs)


def test_manifest_only_on_push_and_after_both_images() -> None:
    manifest = _jobs()["manifest"]
    assert manifest["needs"] == "image"
    assert manifest["if"] == PUSH_ONLY
    runs = "\n".join(step.get("run", "") for step in manifest["steps"])
    assert "docker buildx imagetools create" in runs
    for platform in ("linux/amd64", "linux/arm64"):
        assert platform in runs


def test_pytest_blocks_network_by_default() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "--disable-socket" in pyproject["tool"]["pytest"]["ini_options"]["addopts"]
