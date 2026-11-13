"""`MigrationScripts.load`：heads 只有最新版本，known 是全部版本（停在舊版的資料庫才判成落後）。"""

from pathlib import Path

from radar.domain.schema import SchemaState, classify_schema
from radar.repo.migrations import MigrationScripts


def _write_revision(versions: Path, revision: str, down_revision: str | None) -> None:
    (versions / f"{revision}_step.py").write_text(
        f"revision = {revision!r}\ndown_revision = {down_revision!r}\n"
        "branch_labels = None\ndepends_on = None\n",
        encoding="utf-8",
    )


def test_known_includes_older_revisions(tmp_path: Path) -> None:
    (tmp_path / "env.py").write_text("", encoding="utf-8")
    versions = tmp_path / "versions"
    versions.mkdir()
    _write_revision(versions, "0001", None)
    _write_revision(versions, "0002", "0001")

    scripts = MigrationScripts.load(tmp_path)

    assert scripts.heads == {"0002"}
    assert scripts.known == {"0001", "0002"}
    state = classify_schema(frozenset({"0001"}), heads=scripts.heads, known=scripts.known)
    assert state is SchemaState.BEHIND
