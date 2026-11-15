"""`radar migrate` 對真的 PostgreSQL（compose 的 db）：第一次、重跑、缺擴充套件、沒設定、連不上，
以及 `alembic check`、downgrade 再 upgrade、`Database.ping()`、遷移狀態。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from radar.domain.schema import SchemaState
from radar.repo.db import Database
from radar.repo.migrations import MigrationScripts, read_schema_state, upgrade_to_head
from tests.database import MIGRATIONS_DIR, ROOT, TestDatabase, migrate

BIN = Path(sys.executable).parent
HEAD = "0020"


def _run(
    command: str, *args: str, env: dict[str, str], cwd: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    # 刻意不繼承目前的環境變數，只給這次需要的。
    return subprocess.run(
        [str(BIN / command), *args],
        env={"PYTHONUNBUFFERED": "1", **env},
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _radar_migrate(url: str) -> subprocess.CompletedProcess[str]:
    return _run("radar", "migrate", env={"RADAR_MIGRATION_DATABASE_URL": url})


def _records(out: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def _privilege_snapshot(database: TestDatabase) -> list[tuple[object, ...]]:
    """遷移留下的東西：版本、預設權限、schema 與 alembic_version 的權限。"""
    with psycopg.connect(database.admin_url, autocommit=True) as connection:
        rows: list[tuple[object, ...]] = []
        rows += connection.execute("SELECT version_num FROM alembic_version").fetchall()
        rows += connection.execute(
            "SELECT defaclobjtype, defaclacl::text FROM pg_default_acl ORDER BY 1"
        ).fetchall()
        rows += connection.execute(
            "SELECT nspacl::text FROM pg_namespace WHERE nspname = 'public'"
        ).fetchall()
        rows += connection.execute(
            "SELECT relacl::text FROM pg_class WHERE oid = 'alembic_version'::regclass"
        ).fetchall()
        rows += connection.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public'"
        ).fetchall()
    return rows


def test_first_migration_succeeds_and_logs_pgvector_version(fresh_database: TestDatabase) -> None:
    result = _radar_migrate(fresh_database.owner_url)

    assert result.returncode == 0, result.stdout + result.stderr
    assert fresh_database.revisions() == {HEAD}
    records = _records(result.stdout)
    checks = [r for r in records if r["event"] == "資料庫功能檢查"]
    assert len(checks) == 1
    assert isinstance(checks[0]["pgvector_version"], str)
    assert checks[0]["pgvector_version"]
    assert "radar_owner@" not in result.stdout + result.stderr


def test_rerun_changes_nothing(fresh_database: TestDatabase) -> None:
    assert _radar_migrate(fresh_database.owner_url).returncode == 0
    before = _privilege_snapshot(fresh_database)

    again = _radar_migrate(fresh_database.owner_url)

    assert again.returncode == 0, again.stdout + again.stderr
    assert _privilege_snapshot(fresh_database) == before
    # 已經是最新版本：第一個遷移沒有再跑。
    assert not any(r["event"] == "資料庫功能檢查" for r in _records(again.stdout))


def test_missing_extensions_fail_and_leave_nothing(bare_database: TestDatabase) -> None:
    result = _radar_migrate(bare_database.owner_url)

    assert result.returncode == 1
    reasons = [line for line in result.stderr.splitlines() if line.startswith("- ")]
    assert len(reasons) == 2, result.stderr
    assert "pgvector" in reasons[0]
    assert "pg_trgm" in reasons[1]
    # 整個交易退回：連 alembic_version 都沒有。
    assert not bare_database.has_table("alembic_version")
    with psycopg.connect(bare_database.admin_url, autocommit=True) as connection:
        assert connection.execute("SELECT count(*) FROM pg_default_acl").fetchone() == (0,)


def test_missing_setting_fails_without_connecting() -> None:
    result = _run("radar", "migrate", env={"DATABASE_URL": "postgresql://radar_app@x:1/radar"})

    assert result.returncode == 1
    assert "RADAR_MIGRATION_DATABASE_URL" in result.stderr
    assert "radar_app@x" not in result.stdout + result.stderr


def test_unreachable_database_fails_without_printing_url() -> None:
    result = _radar_migrate("postgresql://radar_owner:s3cret-PW@127.0.0.1:1/radar")

    assert result.returncode == 1
    assert "OperationalError" in result.stderr
    assert any(r.get("error_type") == "OperationalError" for r in _records(result.stdout))
    output = result.stdout + result.stderr
    assert "127.0.0.1:1" not in output
    assert "s3cret-PW" not in output
    assert "port 1" not in output


def test_alembic_check_finds_models_and_migrations_consistent(
    migrated_database: TestDatabase,
) -> None:
    result = _run(
        "alembic", "check", env={"RADAR_MIGRATION_DATABASE_URL": migrated_database.owner_url}
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No new upgrade operations detected" in result.stdout


def test_downgrade_then_upgrade(fresh_database: TestDatabase) -> None:
    env = {"RADAR_MIGRATION_DATABASE_URL": fresh_database.owner_url}
    assert _radar_migrate(fresh_database.owner_url).returncode == 0

    down = _run("alembic", "downgrade", "base", env=env)

    assert down.returncode == 0, down.stdout + down.stderr
    assert fresh_database.revisions() == set()
    with psycopg.connect(fresh_database.admin_url, autocommit=True) as connection:
        assert connection.execute("SELECT count(*) FROM pg_default_acl").fetchone() == (0,)

    up = _radar_migrate(fresh_database.owner_url)

    assert up.returncode == 0, up.stdout + up.stderr
    assert fresh_database.revisions() == {HEAD}


def test_ping_returns_true_for_real_database(migrated_database: TestDatabase) -> None:
    database = Database(migrated_database.app_url, connect_timeout=3, pool_size=1)

    assert database.ping() is True


def test_schema_state_follows_migration(fresh_database: TestDatabase) -> None:
    scripts = MigrationScripts.load(MIGRATIONS_DIR)
    database = Database(fresh_database.app_url, connect_timeout=3, pool_size=1)

    assert read_schema_state(database, scripts) is SchemaState.NOT_MIGRATED
    # 只讀：查狀態不會建出 alembic_version。
    assert not fresh_database.has_table("alembic_version")

    migrate(fresh_database)

    assert read_schema_state(database, scripts) is SchemaState.CURRENT


def test_unknown_revision_is_ahead(fresh_database: TestDatabase) -> None:
    migrate(fresh_database)
    with fresh_database.connect("radar_owner") as owner:
        owner.execute("UPDATE alembic_version SET version_num = '9999'")
    database = Database(fresh_database.app_url, connect_timeout=3, pool_size=1)

    assert read_schema_state(database, MigrationScripts.load(MIGRATIONS_DIR)) is SchemaState.AHEAD


def test_superuser_cannot_run_migration(fresh_database: TestDatabase) -> None:
    # 用超級使用者跑的話，預設權限會綁到錯的角色；要失敗、什麼都不留。
    result = _radar_migrate(fresh_database.admin_url)

    assert result.returncode == 1
    reasons = [line for line in result.stderr.splitlines() if line.startswith("- ")]
    assert len(reasons) == 1, result.stderr
    assert "radar_owner" in reasons[0]
    assert not fresh_database.has_table("alembic_version")


def test_migrate_without_migrations_dir_explains(tmp_path: Path) -> None:
    result = _run(
        "radar",
        "migrate",
        env={
            "RADAR_MIGRATION_DATABASE_URL": "postgresql://radar_owner@127.0.0.1:1/radar",
            # 工作目錄換掉後領域設定也不在了；指回 repo 的 config/，才測得到「找不到遷移檔」。
            "RADAR_CONFIG_DIR": str(ROOT / "config"),
        },
        cwd=tmp_path,
    )

    assert result.returncode == 1
    assert "找不到遷移檔" in result.stderr


def test_alembic_check_fails_when_database_differs_from_models(
    fresh_database: TestDatabase,
) -> None:
    migrate(fresh_database)
    with fresh_database.connect("radar_owner") as owner:
        owner.execute("CREATE TABLE not_in_models (id uuid PRIMARY KEY DEFAULT uuidv7())")

    result = _run(
        "alembic", "check", env={"RADAR_MIGRATION_DATABASE_URL": fresh_database.owner_url}
    )

    assert result.returncode != 0, result.stdout + result.stderr


# 在子行程裡把一張表加進模型再跑 `alembic check`（走真的 env.py），不污染這個行程的 Base.metadata。
CHECK_WITH_PROBE_TABLE = """
from pathlib import Path
from alembic import command
from sqlalchemy import Column, DateTime, Table, Text, Uuid, text
from radar.repo.migrations import MigrationScripts
from radar.repo.models import Base

Table(
    "default_probe",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("status", Text, nullable=False, server_default="new"),
    Column("created_at", DateTime(timezone=True), server_default=text("now()")),
)
command.check(MigrationScripts.load(Path("migrations")).alembic_config())
"""

PROBE_TABLE_SQL = (
    "CREATE TABLE default_probe ("
    " id uuid PRIMARY KEY DEFAULT uuidv7(),"
    " status text NOT NULL DEFAULT 'new',"
    " created_at timestamptz DEFAULT now())"
)


def test_alembic_check_compares_server_defaults(fresh_database: TestDatabase) -> None:
    migrate(fresh_database)
    env = {"RADAR_MIGRATION_DATABASE_URL": fresh_database.owner_url}
    with fresh_database.connect("radar_owner") as owner:
        owner.execute(PROBE_TABLE_SQL)

    # 預設值都一樣：uuidv7()、now() 這類函式預設值不能誤報。
    same = _run("python", "-c", CHECK_WITH_PROBE_TABLE, env=env)
    assert same.returncode == 0, same.stdout + same.stderr

    with fresh_database.connect("radar_owner") as owner:
        owner.execute("ALTER TABLE default_probe ALTER COLUMN status SET DEFAULT 'old'")

    # 只有一般欄位的預設值不同：要被抓到。
    differs = _run("python", "-c", CHECK_WITH_PROBE_TABLE, env=env)
    assert differs.returncode != 0, differs.stdout + differs.stderr
    assert "modify_default" in differs.stdout + differs.stderr


# 測試專用、接在目前 head 後面的下一版：0001 已經授權之後才失敗。
# 先回報當下的授權狀態，證明失敗點真的在授權之後。
NEXT = f"{int(HEAD) + 1:04d}"
FAILING_NEXT = f"""
from alembic import op
from sqlalchemy import text

revision = "{NEXT}"
down_revision = "{HEAD}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    granted = op.get_bind().execute(
        text(
            "SELECT (SELECT count(*) FROM pg_default_acl) > 0"
            " AND (SELECT nspacl::text FROM pg_namespace WHERE nspname = 'public')"
            " LIKE '%radar_app=U%'"
        )
    ).scalar_one()
    raise RuntimeError(f"故意失敗 granted={{granted}}")


def downgrade() -> None:
    pass
"""


def _grant_state(database: TestDatabase) -> tuple[object, ...]:
    """授權相關的狀態：有沒有 alembic_version、預設權限、public schema 的權限。"""
    with psycopg.connect(database.admin_url, autocommit=True) as connection:
        has_version = connection.execute(
            "SELECT to_regclass('alembic_version') IS NOT NULL"
        ).fetchone()
        default_acl = connection.execute(
            "SELECT defaclobjtype, defaclacl::text FROM pg_default_acl ORDER BY 1"
        ).fetchall()
        schema_acl = connection.execute(
            "SELECT nspacl::text FROM pg_namespace WHERE nspname = 'public'"
        ).fetchone()
    return (has_version, default_acl, schema_acl)


def test_failure_after_grants_rolls_everything_back(
    fresh_database: TestDatabase, tmp_path: Path
) -> None:
    # 複製正式的遷移檔到暫存資料夾，再接一個故意失敗的下一版；不動正式的 migrations/。
    scripts_dir = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, scripts_dir, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts_dir / "versions" / f"{NEXT}_fail_after_grants.py").write_text(
        FAILING_NEXT, encoding="utf-8"
    )
    before = _grant_state(fresh_database)

    with pytest.raises(RuntimeError, match="granted=True"):
        upgrade_to_head(
            fresh_database.owner_url, MigrationScripts.load(scripts_dir), connect_timeout=5
        )

    after = _grant_state(fresh_database)
    assert after == before
    assert after[0] == (False,)
    assert after[1] == []
    assert "radar_app" not in str(after[2])
