"""遷移檔的規則（AD-18）：一條直線、只有一個 head、版本號是四位數流水號且和檔名一致。"""

import configparser
import re
import tomllib
from pathlib import Path
from typing import Any

from alembic.script import Script, ScriptDirectory

from radar.repo.migrations import MigrationScripts
from tests.database import MIGRATIONS_DIR, ROOT

VERSIONS_DIR = MIGRATIONS_DIR / "versions"
FILE_NAME = re.compile(r"(?P<rev>\d{4})_[a-z0-9_]+\.py")


def _revisions() -> list[Script]:
    """從最舊到最新。"""
    config = MigrationScripts.load(MIGRATIONS_DIR).alembic_config()
    return list(reversed(list(ScriptDirectory.from_config(config).walk_revisions())))


def test_exactly_one_head() -> None:
    assert len(MigrationScripts.load(MIGRATIONS_DIR).heads) == 1


def test_revisions_are_a_numbered_straight_line() -> None:
    revisions = _revisions()
    assert revisions, "至少要有 0001"
    for number, revision in enumerate(revisions, start=1):
        assert revision.revision == f"{number:04d}", revision.path
        expected_down = None if number == 1 else f"{number - 1:04d}"
        assert revision.down_revision == expected_down, revision.path
        assert not revision.branch_labels, revision.path
        assert not revision.dependencies, revision.path


def test_file_names_match_revision_ids() -> None:
    files = sorted(VERSIONS_DIR.glob("*.py"))
    by_path = {Path(revision.path).name: revision.revision for revision in _revisions()}
    assert sorted(by_path) == [path.name for path in files]
    for path in files:
        match = FILE_NAME.fullmatch(path.name)
        assert match, f"{path.name} 要是 <四位數版本號>_<說明>.py"
        assert by_path[path.name] == match["rev"]


def test_alembic_ini_points_at_migrations() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "alembic.ini", encoding="utf-8")

    assert parser["alembic"]["script_location"] == "%(here)s/migrations"
    assert parser["alembic"]["file_template"] == "%%(rev)s_%%(slug)s"
    # 連線字串不寫在 alembic.ini（env.py 經 radar.config 讀 RADAR_MIGRATION_DATABASE_URL）。
    assert "sqlalchemy.url" not in parser["alembic"]


def _contracts() -> dict[str, dict[str, Any]]:
    with (ROOT / "pyproject.toml").open("rb") as file:
        config: dict[str, Any] = tomllib.load(file)["tool"]["importlinter"]
    return {contract["id"]: contract for contract in config["contracts"]}


def test_alembic_only_in_repo_contract() -> None:
    contracts = _contracts()
    contract = contracts["pkg-alembic"]

    assert contract["type"] == "forbidden"
    assert contract["source_modules"] == ["radar"]
    assert contract["forbidden_modules"] == ["alembic"]
    owners = {rule.split(" -> ")[0].removesuffix(".**") for rule in contract["ignore_imports"]}
    assert owners == {"radar.repo"}
    assert "alembic" in contracts["domain-pure"]["forbidden_modules"]
