"""遷移 0010（LLM 閘門的來歷、帳本、三張表頭表）對真的 PostgreSQL：升版、降版、再升版，以及權限。

- 降到 0009 後五張表都不在，再升回來都在。
- `radar_app` 對 `llm_call` 只能新增與讀取（帳不能改、不能刪）；對 `derivation_run` 與表頭表
  可以改狀態。
- `llm_call` 存下的輸出：文字與拒收原因最多一個有值，原因代碼要合格式。
"""

import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

from radar.repo.db import Database
from tests.database import APP_ROLE, ROOT, TestDatabase, migrate
from tests.llm_support import create_paper

BIN = Path(sys.executable).parent
NEW_TABLES = ("derivation_run", "llm_call", "zh_summary", "classification", "research_model")
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


def test_downgrade_to_0009_drops_tables_and_upgrade_restores_them(db: TestDatabase) -> None:
    assert all(db.has_table(table) for table in NEW_TABLES)

    down = _alembic(db, "downgrade", "0009")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0009"}
    assert not any(db.has_table(table) for table in NEW_TABLES)
    assert db.has_table("paper")

    migrate(db)

    assert db.revisions() == {"0023"}
    assert all(db.has_table(table) for table in NEW_TABLES)


def _seed_run(db: TestDatabase) -> uuid.UUID:
    """用應用程式角色新增一篇論文與一筆 run，回 run id。"""
    paper_id = create_paper(Database(db.app_url, connect_timeout=5, pool_size=1))
    with db.connect(APP_ROLE) as connection:
        run = connection.execute(
            "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
            " input_sha256, input_kind, params, code_version, call_mode, status)"
            " VALUES (%s, 'zh_summary', 1, %s, 'claude-haiku-5-5', %s, 'abstract', '{}',"
            " 'test', 'interactive', 'running') RETURNING id",
            (paper_id, SHA, SHA),
        ).fetchone()
        assert run is not None
        return uuid.UUID(str(run[0]))


def test_app_role_can_insert_and_select_llm_call_but_not_update_or_delete(
    db: TestDatabase,
) -> None:
    run_id = _seed_run(db)
    with db.connect(APP_ROLE) as connection:
        connection.execute(
            "INSERT INTO llm_call (run_id, input_tokens, output_tokens,"
            " cache_creation_input_tokens, cache_read_input_tokens, cost_usd, price_tier, is_batch)"
            " VALUES (%s, 10, 5, 0, 0, 0.000123, 'standard', false)",
            (run_id,),
        )
        assert connection.execute("SELECT cost_usd::text FROM llm_call").fetchall() == [
            ("0.000123",)
        ]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("UPDATE llm_call SET cost_usd = 0")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("DELETE FROM llm_call")


def test_app_role_can_update_run_and_header_status(db: TestDatabase) -> None:
    run_id = _seed_run(db)
    with db.connect(APP_ROLE) as connection:
        connection.execute(
            "UPDATE derivation_run SET status = 'succeeded', finished_at = now() WHERE id = %s",
            (run_id,),
        )
        for table in ("zh_summary", "classification", "research_model"):
            connection.execute(
                f"INSERT INTO {table} (paper_id, run_id, input_kind, input_sha256, lang, status)"
                " SELECT paper_id, id, 'abstract', input_sha256, 'zh-Hant', 'active'"
                " FROM derivation_run WHERE id = %s",
                (run_id,),
            )
            connection.execute(f"UPDATE {table} SET status = 'superseded'")


def test_check_constraints_reject_bad_rows(db: TestDatabase) -> None:
    run_id = _seed_run(db)
    with db.connect(APP_ROLE) as connection:
        # failed 一定要有原因；費用不能是負的；被拒收一定要有原因代碼。
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "UPDATE derivation_run SET status = 'failed', finished_at = now() WHERE id = %s",
                (run_id,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO llm_call (run_id, input_tokens, output_tokens,"
                " cache_creation_input_tokens, cache_read_input_tokens, cost_usd, price_tier,"
                " is_batch) VALUES (%s, 1, 1, 0, 0, -1, 'standard', false)",
                (run_id,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO zh_summary (paper_id, run_id, input_kind, input_sha256, lang, status)"
                " SELECT paper_id, id, 'abstract', input_sha256, 'zh-Hant', 'rejected'"
                " FROM derivation_run WHERE id = %s",
                (run_id,),
            )


def test_second_running_run_for_same_key_is_refused_but_eval_is_not(db: TestDatabase) -> None:
    run_id = _seed_run(db)
    copy = (
        "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
        " input_sha256, input_kind, params, code_version, call_mode, status, eval_run_id)"
        " SELECT paper_id, task, prompt_version, prompt_sha256, model, input_sha256, input_kind,"
        " params, code_version, call_mode, 'running', %s FROM derivation_run WHERE id = %s"
    )
    with db.connect(APP_ROLE) as connection:
        connection.execute(copy, (uuid.uuid4(), run_id))
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(copy, (None, run_id))


def test_llm_call_saved_output_checks(db: TestDatabase) -> None:
    run_id = _seed_run(db)
    insert = (
        "INSERT INTO llm_call (run_id, input_tokens, output_tokens, cache_creation_input_tokens,"
        " cache_read_input_tokens, cost_usd, price_tier, is_batch, output_text,"
        " output_reject_reason) VALUES (%s, 1, 1, 0, 0, 0, 'standard', false, %s, %s)"
    )
    with db.connect(APP_ROLE) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(insert, (run_id, "{}", "llm_refusal"))
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(insert, (run_id, None, "Not A Code"))
        connection.execute(insert, (run_id, "{}", None))
        assert connection.execute("SELECT output_text FROM llm_call").fetchall() == [("{}",)]
