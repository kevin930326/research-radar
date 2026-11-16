"""遷移 0012（分類的標籤）對真的 PostgreSQL：降版、升版、CHECK 與唯一約束。

- 降到 0011 後 `classification_label` 與 `dropped_label_count` 都不在（0011 已有的表頭列照樣在），
  再升回來都在、舊列的計數是 NULL。
- `dropped_label_count` 不能是負數；`rejected` 沒有計數。
- 標籤的 `field` 只有 `topic`／`method`、`key` 的寫法、`unclassified` 只能是主題、同一列表頭同一欄
  同一個 key 只有一列、`classification_id` 一定要指到存在的表頭。
- 應用程式角色可以新增、讀取標籤（0001 的預設權限）。
"""

import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

from radar.repo.db import Database
from tests.database import APP_ROLE, ROOT, TestDatabase, migrate
from tests.llm_support import create_paper, execute

BIN = Path(sys.executable).parent
SHA = "a" * 64


def _alembic(database: TestDatabase, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BIN / "alembic"), *args],
        env={"PYTHONUNBUFFERED": "1", "RADAR_MIGRATION_DATABASE_URL": database.owner_url},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _tables(db: TestDatabase) -> set[str]:
    rows = execute(
        db, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    return {row[0] for row in rows}


def _classification_columns(db: TestDatabase) -> set[str]:
    rows = execute(
        db,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = 'classification'",
    )
    return {row[0] for row in rows}


def _seed_run(db: TestDatabase) -> uuid.UUID:
    """用應用程式角色新增一篇論文（每次都是新的一篇）與一筆做完的 classify run，回 run id。"""
    suffix = uuid.uuid4().hex[:8]
    paper_id = create_paper(
        Database(db.app_url, connect_timeout=5, pool_size=1),
        doi=f"10.1/{suffix}",
        openalex_id=f"W{int(suffix, 16)}",
    )
    with db.connect(APP_ROLE) as connection:
        run = connection.execute(
            "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
            " input_sha256, input_kind, params, code_version, call_mode, taxonomy_version,"
            " status, finished_at)"
            " VALUES (%s, 'classify', 1, %s, 'claude-haiku-5-5', %s, 'abstract', '{}',"
            " 'test', 'interactive', '1', 'succeeded', now()) RETURNING id",
            (paper_id, SHA, SHA),
        ).fetchone()
        assert run is not None
        return uuid.UUID(str(run[0]))


def _header(
    db: TestDatabase,
    status: str = "active",
    reject_reason: str | None = None,
    count: int | None = 0,
) -> uuid.UUID:
    """用應用程式角色新增一列分類表頭，回它的 id。"""
    run_id = _seed_run(db)
    with db.connect(APP_ROLE) as connection:
        row = connection.execute(
            "INSERT INTO classification (paper_id, run_id, input_kind, input_sha256, lang, status,"
            " reject_reason, dropped_label_count)"
            " SELECT paper_id, id, 'abstract', input_sha256, 'zxx', %s, %s, %s"
            " FROM derivation_run WHERE id = %s RETURNING id",
            (status, reject_reason, count, run_id),
        ).fetchone()
        assert row is not None
        return uuid.UUID(str(row[0]))


def _label(db: TestDatabase, header_id: uuid.UUID, field: str, key: str) -> None:
    with db.connect(APP_ROLE) as connection:
        connection.execute(
            "INSERT INTO classification_label (classification_id, field, key) VALUES (%s, %s, %s)",
            (header_id, field, key),
        )


def test_downgrade_to_0011_drops_labels_and_upgrade_restores_them(db: TestDatabase) -> None:
    assert "classification_label" in _tables(db)
    assert "dropped_label_count" in _classification_columns(db)
    header_id = _header(db, count=1)
    _label(db, header_id, "topic", "erp")

    down = _alembic(db, "downgrade", "0011")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0011"}
    assert "classification_label" not in _tables(db)
    assert "dropped_label_count" not in _classification_columns(db)
    assert execute(db, "SELECT status FROM classification") == [("active",)]

    migrate(db)

    assert db.revisions() == {"0016"}
    assert "classification_label" in _tables(db)
    # 降版丟掉的標籤與計數不會回來；舊列的計數是 NULL，兩個 CHECK 都成立。
    assert execute(db, "SELECT dropped_label_count FROM classification") == [(None,)]
    assert execute(db, "SELECT count(*) FROM classification_label") == [(0,)]


def test_active_row_can_have_labels_and_a_count(db: TestDatabase) -> None:
    header_id = _header(db, count=2)
    _label(db, header_id, "topic", "erp")
    _label(db, header_id, "topic", "supply_chain")
    _label(db, header_id, "method", "survey_sem")
    # 只有標題、什麼都沒選：主題是 unclassified。
    _label(db, _header(db, status="superseded", count=0), "topic", "unclassified")
    # 2.2 留下的表頭沒有內容：計數是 NULL。
    _header(db, count=None)

    assert execute(db, "SELECT count(*) FROM classification_label") == [(4,)]


def test_count_cannot_be_negative(db: TestDatabase) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _header(db, count=-1)


def test_rejected_row_has_no_count(db: TestDatabase) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _header(db, status="rejected", reject_reason="llm_output_invalid", count=0)

    _header(db, status="rejected", reject_reason="llm_output_invalid", count=None)


@pytest.mark.parametrize(
    ("field", "key"),
    [
        ("theory", "erp"),  # field 只有 topic／method
        ("topic", "ERP"),  # key 的寫法
        ("topic", "supply-chain"),
        ("method", ""),
        ("method", "unclassified"),  # unclassified 只能是主題
    ],
)
def test_label_checks(db: TestDatabase, field: str, key: str) -> None:
    header_id = _header(db)

    with pytest.raises(psycopg.errors.CheckViolation):
        _label(db, header_id, field, key)


def test_same_key_twice_in_the_same_field_is_refused(db: TestDatabase) -> None:
    header_id = _header(db)
    _label(db, header_id, "topic", "erp")

    with pytest.raises(psycopg.errors.UniqueViolation):
        _label(db, header_id, "topic", "erp")

    # 另一欄、另一列表頭可以有同一個 key。
    _label(db, header_id, "method", "erp")
    _label(db, _header(db), "topic", "erp")


def test_label_must_point_to_an_existing_header(db: TestDatabase) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _label(db, uuid.uuid4(), "topic", "erp")
