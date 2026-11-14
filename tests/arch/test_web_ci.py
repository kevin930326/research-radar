"""CI 的前端 job（Story 3.1、ADR-0029）：步驟齊全、三種引擎、和 checks 平行、action 釘 SHA。"""

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
PACKAGE_JSON = ROOT / "web" / "package.json"
# 和本機 web/ 的 npm scripts 同一組，順序固定。
WEB_STEPS = [
    "web-npm-ci",
    "web-lint",
    "web-typecheck",
    "web-test",
    "web-build-member",
    "web-build-demo",
    "web-playwright-install",
    "web-e2e",
]
PINNED_USES = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _web_job() -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    job: dict[str, Any] = workflow["jobs"]["web"]
    return job


def _runs() -> dict[str, str]:
    return {step["id"]: step["run"] for step in _web_job()["steps"] if "id" in step}


def test_web_job_runs_in_parallel_with_checks() -> None:
    job = _web_job()

    assert job["runs-on"] == "ubuntu-24.04"
    assert "needs" not in job
    assert "if" not in job
    assert "permissions" not in job  # 沿用 workflow 預設的 contents: read
    assert job["defaults"]["run"]["working-directory"] == "web"


def test_web_job_runs_every_frontend_check_in_order() -> None:
    ids = [step["id"] for step in _web_job()["steps"] if "id" in step]

    assert ids == WEB_STEPS


def test_web_job_commands_match_npm_scripts() -> None:
    runs = _runs()
    scripts = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"]

    assert runs["web-npm-ci"].startswith("npm ci")
    assert runs["web-lint"] == "npm run lint"
    assert runs["web-typecheck"] == "npm run typecheck"
    assert runs["web-test"] == "npm test"
    assert runs["web-build-member"] == "npm run build:member"
    assert runs["web-build-demo"] == "npm run build:demo"
    assert runs["web-e2e"] == "npm run e2e"
    for name in ("lint", "typecheck", "test", "build:member", "build:demo", "e2e"):
        assert name in scripts


def test_web_job_installs_all_three_browser_engines() -> None:
    install = _runs()["web-playwright-install"]

    assert "playwright install --with-deps" in install
    for engine in ("chromium", "firefox", "webkit"):
        assert engine in install.split()


def test_web_job_steps_cannot_hide_a_failure() -> None:
    for step in _web_job()["steps"]:
        assert "if" not in step, step.get("name")
        assert step.get("continue-on-error", False) is False, step.get("name")


def test_web_job_actions_are_pinned_and_node_follows_nvmrc() -> None:
    uses = [step["uses"] for step in _web_job()["steps"] if "uses" in step]

    assert any(use.startswith("actions/setup-node@") for use in uses)
    assert all(PINNED_USES.match(use) for use in uses), uses
    setup_node = next(
        step
        for step in _web_job()["steps"]
        if step.get("uses", "").startswith("actions/setup-node@")
    )
    assert setup_node["with"]["node-version-file"] == "web/.nvmrc"
    assert (ROOT / "web" / ".nvmrc").read_text(encoding="utf-8").strip() == "26"


def test_dockerfile_node_major_matches_nvmrc() -> None:
    # Node 的大版本在 web/.nvmrc 與 Dockerfile 的前端建置段各寫一次，兩邊要一致。
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    majors = re.findall(
        r"^FROM\s+(?:--platform=\S+\s+)?node:(\d+)[-.:\s]", dockerfile, re.MULTILINE
    )

    assert majors, "Dockerfile 沒有 node 的建置段"
    assert set(majors) == {(ROOT / "web" / ".nvmrc").read_text(encoding="utf-8").strip()}
