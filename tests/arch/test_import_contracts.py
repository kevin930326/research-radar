"""AD-1 的 import 方向與套件位置都寫成 import-linter 契約，而且全部通過。"""

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _importlinter() -> dict[str, Any]:
    with (ROOT / "pyproject.toml").open("rb") as file:
        config: dict[str, Any] = tomllib.load(file)["tool"]["importlinter"]
    return config


def _contracts() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in _importlinter()["contracts"]}


def _owners(contract: dict[str, Any]) -> set[str]:
    """forbidden 契約的 ignore_imports 裡，被允許直接 import 的模組。"""
    return {
        rule.split(" -> ")[0].removesuffix(".**") for rule in contract.get("ignore_imports", [])
    }


def test_external_packages_are_included() -> None:
    config = _importlinter()

    assert config["root_packages"] == ["radar"]
    assert config["include_external_packages"] is True


def test_package_location_contracts() -> None:
    contracts = _contracts()
    expected = {
        "pkg-sqlalchemy": ({"sqlalchemy", "pgvector", "psycopg"}, {"radar.repo"}),
        "pkg-adapters": (
            {"httpx2", "anthropic", "boto3", "pypdf", "joserfc"},
            {"radar.adapters"},
        ),
        "pkg-procrastinate": (
            {"procrastinate"},
            {"radar.adapters.jobs", "radar.entrypoints.worker"},
        ),
        "pkg-fastapi": ({"fastapi", "starlette", "uvicorn"}, {"radar.entrypoints.api"}),
        "pkg-typer": ({"typer"}, {"radar.entrypoints.cli"}),
    }
    for contract_id, (packages, owners) in expected.items():
        contract = contracts[contract_id]
        assert contract["type"] == "forbidden"
        assert contract["source_modules"] == ["radar"]
        assert set(contract["forbidden_modules"]) == packages
        assert _owners(contract) == owners, contract_id


def test_layer_direction_contracts_exist() -> None:
    contracts = _contracts()

    domain = contracts["domain-pure"]
    assert domain["source_modules"] == ["radar.domain"]
    assert not domain.get("allow_indirect_imports", False)
    assert {
        "radar.services",
        "radar.repo",
        "radar.adapters",
        "radar.entrypoints",
        "radar.config",
        "sqlalchemy",
        "httpx2",
        "anthropic",
        "boto3",
        "pypdf",
        "fastapi",
    } <= set(domain["forbidden_modules"])

    assert {"radar.adapters", "radar.entrypoints", "radar.config"} <= set(
        contracts["services-boundary"]["forbidden_modules"]
    )
    assert set(contracts["entrypoints-independent"]["modules"]) == {
        "radar.entrypoints.api",
        "radar.entrypoints.cli",
        "radar.entrypoints.worker",
    }
    assert set(contracts["entrypoints-no-repo-adapters"]["forbidden_modules"]) == {
        "radar.repo",
        "radar.adapters",
    }
    assert contracts["config-importers"]["allowed_importers"] == ["radar.entrypoints"]
    assert set(contracts["repo-importers"]["allowed_importers"]) == {
        "radar.services",
        "radar.entrypoints.bootstrap",
    }
    for contract_id in ("repo-boundary", "adapters-boundary", "ports-boundary", "config-boundary"):
        assert contract_id in contracts


def test_all_contracts_kept() -> None:
    executable = shutil.which("lint-imports", path=str(Path(sys.executable).parent))
    assert executable is not None
    result = subprocess.run(
        [executable, "--no-cache"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 broken" in result.stdout
