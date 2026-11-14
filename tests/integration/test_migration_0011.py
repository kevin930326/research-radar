"""遷移 0011（中文重點的四欄內容）對真的 PostgreSQL：降版、升版，以及兩個 CHECK。

- 降到 0010 後四欄不在（0010 已有的表頭列照樣在），再升回來四欄都在、舊列的內容是 NULL。
- `rejected` 不能有內容；有值時不能只有空白；`active` 可以四欄有值或全是 NULL。
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
CONTENT_COLUMNS = ("research_question", "method", "theory", "findings")


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


def _columns(db: TestDatabase) -> set[str]:
    rows = execute(
        db,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = 'zh_summary'",
    )
    return {row[0] for row in rows}


def _seed_run(db: TestDatabase) -> uuid.UUID:
    """用應用程式角色新增一篇論文與一筆做完的 run，回 run id。"""
    paper_id = create_paper(Database(db.app_url, connect_timeout=5, pool_size=1))
    with db.connect(APP_ROLE) as connection:
        run = connection.execute(
            "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
            " input_sha256, input_kind, params, code_version, call_mode, status, finished_at)"
            " VALUES (%s, 'zh_summary', 1, %s, 'claude-haiku-5-5', %s, 'abstract', '{}',"
            " 'test', 'interactive', 'succeeded', now()) RETURNING id",
            (paper_id, SHA, SHA),
        ).fetchone()
        assert run is not None
        return uuid.UUID(str(run[0]))


def _insert(
    db: TestDatabase,
    run_id: uuid.UUID,
    status: str,
    reject_reason: str | None,
    content: dict[str, str | None],
) -> None:
    values = [content.get(column) for column in CONTENT_COLUMNS]
    with db.connect(APP_ROLE) as connection:
        connection.execute(
            "INSERT INTO zh_summary (paper_id, run_id, input_kind, input_sha256, lang, status,"
            " reject_reason, research_question, method, theory, findings)"
            " SELECT paper_id, id, 'abstract', input_sha256, 'zh-Hant', %s, %s, %s, %s, %s, %s"
            " FROM derivation_run WHERE id = %s",
            (status, reject_reason, *values, run_id),
        )


def test_downgrade_to_0010_drops_the_columns_and_upgrade_restores_them(db: TestDatabase) -> None:
    assert set(CONTENT_COLUMNS) <= _columns(db)
    run_id = _seed_run(db)
    _insert(db, run_id, "active", None, {"method": "問卷調查"})

    down = _alembic(db, "downgrade", "0010")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0010"}
    assert not set(CONTENT_COLUMNS) & _columns(db)
    assert execute(db, "SELECT status FROM zh_summary") == [("active",)]

    migrate(db)

    assert db.revisions() == {"0011"}
    assert set(CONTENT_COLUMNS) <= _columns(db)
    # 降版丟掉的內容不會回來；舊列四欄都是 NULL，兩個 CHECK 都成立。
    assert execute(db, "SELECT research_question, method, theory, findings FROM zh_summary") == [
        (None, None, None, None)
    ]


def test_active_row_can_have_content_or_all_nulls(db: TestDatabase) -> None:
    _insert(
        db,
        _seed_run(db),
        "active",
        None,
        {"research_question": "研究問題", "method": "方法", "theory": "理論", "findings": "發現"},
    )
    _insert(db, _seed_run(db), "superseded", None, {})

    assert execute(db, "SELECT count(*) FROM zh_summary") == [(2,)]


@pytest.mark.parametrize("column", CONTENT_COLUMNS)
def test_rejected_row_cannot_have_content(db: TestDatabase, column: str) -> None:
    run_id = _seed_run(db)

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(db, run_id, "rejected", "zh_summary_field_too_long", {column: "內容"})

    _insert(db, run_id, "rejected", "zh_summary_field_too_long", {})


# 全形空白算不算 [:space:] 依資料庫的 locale 而定，不測；應用層的正規化已經把它縮掉。
@pytest.mark.parametrize("blank", ["", " ", "\t\n"])
@pytest.mark.parametrize("column", CONTENT_COLUMNS)
def test_content_cannot_be_blank(db: TestDatabase, column: str, blank: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(db, _seed_run(db), "active", None, {column: blank})
