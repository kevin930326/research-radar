"""遷移 0015（向量、模型登錄、向量化的來歷與用量）對真的 PostgreSQL：降版、升版、權限、CHECK、
HNSW 部分索引的完整定義，以及查詢真的用得到那個索引。

`alembic check` 對運算式索引只比名稱（不比運算式、opclass 與 WHERE），所以索引定義在這裡逐字核對。
向量都是測試自己產生的數字。
"""

import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import text

from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.retrieval import search_paper_vectors
from tests.database import APP_ROLE, ROOT, TestDatabase, connect_admin, migrate
from tests.llm_support import create_paper, execute

BIN = Path(sys.executable).parent
QWEN = "@cf/qwen/qwen3-embedding-0.6b"
HNSW = "ix_embedding_hnsw_qwen3_embedding_0_6b"
SHA = "a" * 64
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")


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


@pytest.fixture
def paper_id(db: TestDatabase) -> uuid.UUID:
    return create_paper(Database(db.app_url, connect_timeout=5, pool_size=1))


def _app(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description is not None else []


def _embed_run(db: TestDatabase, paper_id: uuid.UUID, model: str = QWEN) -> uuid.UUID:
    (row,) = _app(
        db,
        "INSERT INTO derivation_run (paper_id, task, model, input_sha256, input_kind, params,"
        " code_version, status, call_mode, finished_at) VALUES (%s, 'embed', %s, %s, 'abstract',"
        " '{}', 'test', 'succeeded', 'interactive', now()) RETURNING id",
        paper_id,
        model,
        SHA,
    )
    return uuid.UUID(str(row[0]))


def _vector(size: int, value: float = 0.01) -> str:
    return "[" + ",".join(str(value * (index % 7 + 1)) for index in range(size)) + "]"


def _insert_embedding(
    db: TestDatabase,
    paper_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    model: str = QWEN,
    dimensions: int = 1024,
    size: int = 1024,
) -> None:
    _app(
        db,
        "INSERT INTO embedding (item_type, item_id, model_id, dimensions, vector, input_sha256,"
        " run_id) VALUES ('paper', %s, %s, %s, %s::halfvec, %s, %s)",
        paper_id,
        model,
        dimensions,
        _vector(size),
        SHA,
        run_id,
    )


def _relations(db: TestDatabase) -> set[str]:
    with connect_admin(db.name) as admin:
        rows = admin.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    return {row[0] for row in rows}


def _constraint_defs(db: TestDatabase, table: str) -> set[str]:
    rows = execute(
        db,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass"
        " AND contype = 'c'",
        table,
    )
    return {row[0] for row in rows}


# --- 降版、升版 ---------------------------------------------------------------------------------


def test_downgrade_to_0014_and_upgrade_again(db: TestDatabase, paper_id: uuid.UUID) -> None:
    assert _relations(db) >= {"embedding", "embedding_model", "embedding_quota_hold"}
    run_id = _embed_run(db, paper_id)
    _insert_embedding(db, paper_id, run_id)
    execute(
        db,
        "INSERT INTO api_call (source, operation, http_status, cost_usd, estimated_input_tokens,"
        " called_at) VALUES ('workers_ai', 'embed_documents', 200, 0, 10, now())",
    )

    down = _alembic(db, "downgrade", "0014")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0014"}
    assert not {"embedding", "embedding_model", "embedding_quota_hold"} & _relations(db)
    assert execute(db, "SELECT count(*) FROM derivation_run WHERE task = 'embed'") == [(0,)]
    assert execute(db, "SELECT count(*) FROM api_call WHERE source = 'workers_ai'") == [(0,)]
    columns = {
        row[0]: row[1]
        for row in execute(
            db,
            "SELECT column_name, is_nullable FROM information_schema.columns"
            " WHERE table_name = 'derivation_run'"
            " AND column_name IN ('prompt_version', 'prompt_sha256')",
        )
    }
    assert columns == {"prompt_version": "NO", "prompt_sha256": "NO"}
    assert not [
        row
        for row in execute(
            db,
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'api_call' AND column_name = 'estimated_input_tokens'",
        )
    ]

    migrate(db)

    assert db.revisions() == {"0018"}
    assert "embedding_quota_hold" in _relations(db)
    assert execute(db, "SELECT model_id, provider, dimensions FROM embedding_model") == [
        (QWEN, "workers_ai", 1024)
    ]


# --- 模型登錄與權限 -----------------------------------------------------------------------------


def _granted(db: TestDatabase, relation: str) -> set[str]:
    with connect_admin(db.name) as admin:
        return {
            privilege
            for privilege in TABLE_PRIVILEGES
            if admin.execute(
                "SELECT has_table_privilege(%s, %s, %s)", (APP_ROLE, relation, privilege)
            ).fetchone()
            == (True,)
        }


def test_model_registry_is_seeded_and_read_only_for_the_app(db: TestDatabase) -> None:
    assert _app(db, "SELECT model_id, dimensions FROM embedding_model") == [(QWEN, 1024)]
    assert _granted(db, "embedding_model") == {"SELECT"}
    assert _granted(db, "embedding") == set(TABLE_PRIVILEGES)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(
            db, "INSERT INTO embedding_model (model_id, provider, dimensions) VALUES ('x', 'y', 8)"
        )


# --- 索引 ---------------------------------------------------------------------------------------


def test_hnsw_partial_index_definition_is_exact(db: TestDatabase) -> None:
    [(definition,)] = execute(db, "SELECT pg_get_indexdef(%s::regclass)", HNSW)
    assert definition == (
        f"CREATE INDEX {HNSW} ON public.embedding USING hnsw"
        " (((vector)::halfvec(1024)) halfvec_cosine_ops)"
        f" WHERE (model_id = '{QWEN}'::text)"
    )
    indexes = {
        row[0]
        for row in execute(db, "SELECT indexname FROM pg_indexes WHERE tablename = 'embedding'")
    }
    assert indexes == {
        "pk_embedding",
        "ix_embedding_run_id",
        "ix_embedding_model_id_dimensions",
        HNSW,
    }


def test_vector_query_uses_the_model_hnsw_index(db: TestDatabase, paper_id: uuid.UUID) -> None:
    _insert_embedding(db, paper_id, _embed_run(db, paper_id))
    database = Database(db.app_url, connect_timeout=5, pool_size=1)
    query = [0.01 * (index % 7 + 1) for index in range(1024)]

    with database.transaction() as connection:
        # 表幾乎是空的，規劃器會寧可排序；關掉循序掃描與排序，證明這個查詢的寫法「用得到」索引
        # （少了 model_id 條件或距離運算式和索引寫得不一樣，就只能退回別的掃描，掃描數是 0）。
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        connection.execute(text("SET LOCAL enable_sort = off"))
        matches = search_paper_vectors(
            SystemViewer(), connection, model_id=QWEN, vector=query, limit=5
        )
        scans = connection.execute(
            text("SELECT pg_stat_get_xact_numscans(CAST(:index AS regclass))"), {"index": HNSW}
        ).scalar_one()

    assert [match.paper_id for match in matches] == [paper_id]
    assert scans >= 1


# --- CHECK 與外鍵 -------------------------------------------------------------------------------


def test_wrong_dimensions_cannot_be_stored(db: TestDatabase, paper_id: uuid.UUID) -> None:
    run_id = _embed_run(db, paper_id)
    # 向量長度和 dimensions 不同：CHECK 擋下（HNSW 運算式的轉型也會擋）。
    with pytest.raises(psycopg.errors.DatabaseError):
        _insert_embedding(db, paper_id, run_id, size=1023)
    # dimensions 和模型登記的不同：外鍵擋下（這個模型的 HNSW 運算式先轉型失敗也算擋下）。
    with pytest.raises((psycopg.errors.ForeignKeyViolation, psycopg.DataError)):
        _insert_embedding(db, paper_id, run_id, dimensions=8, size=8)
    # 沒登記的模型：外鍵擋下。
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert_embedding(db, paper_id, run_id, model="unregistered")
    assert _app(db, "SELECT count(*) FROM embedding") == [(0,)]


def test_halfvec_rejects_values_out_of_range(db: TestDatabase, paper_id: uuid.UUID) -> None:
    run_id = _embed_run(db, paper_id)
    with pytest.raises(psycopg.DataError, match="halfvec"):
        _app(
            db,
            "INSERT INTO embedding (item_type, item_id, model_id, dimensions, vector, input_sha256,"
            " run_id) VALUES ('paper', %s, %s, 1024, %s::halfvec, %s, %s)",
            paper_id,
            QWEN,
            _vector(1024, value=100000.0),
            SHA,
            run_id,
        )


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        # 約束名稱經命名慣例加了前綴、太長的被截短加雜湊（和既有遷移一樣），只比開頭。
        (
            {"task": "'embed'", "prompt_version": "1", "prompt_sha256": f"'{SHA}'"},
            "prompt_version_iff",
        ),
        ({"task": "'zh_summary'"}, "prompt_version_iff"),
        ({"task": "'zh_summary'", "prompt_version": "1"}, "prompt_sha256_iff"),
        ({"task": "'embed'", "prompt_sha256": f"'{SHA}'"}, "prompt_sha256_iff"),
        (
            {"task": "'embedding'", "prompt_version": "1", "prompt_sha256": f"'{SHA}'"},
            "derivation_run_task",
        ),
    ],
)
def test_derivation_run_prompt_columns_follow_the_task(
    db: TestDatabase, paper_id: uuid.UUID, changes: dict[str, str], constraint: str
) -> None:
    values = {
        "paper_id": f"'{paper_id}'",
        "model": "'m'",
        "input_sha256": f"'{SHA}'",
        "input_kind": "'abstract'",
        "params": "'{}'",
        "code_version": "'test'",
        "status": "'running'",
        "call_mode": "'interactive'",
        **changes,
    }
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        _app(
            db,
            f"INSERT INTO derivation_run ({', '.join(values)})"
            f" VALUES ({', '.join(values.values())})",
        )
    assert constraint in str(caught.value)


def test_only_one_running_embed_run_per_input(db: TestDatabase, paper_id: uuid.UUID) -> None:
    insert = (
        "INSERT INTO derivation_run (paper_id, task, model, input_sha256, input_kind, params,"
        " code_version, status, call_mode, finished_at) VALUES (%s, 'embed', 'm', %s, 'abstract',"
        " '{}', 'test', %s, 'interactive', %s)"
    )
    _app(db, insert, paper_id, SHA, "running", None)
    _app(db, insert, paper_id, SHA, "succeeded", "2026-11-15T00:00:00Z")  # 已經結束的不算
    with pytest.raises(psycopg.errors.UniqueViolation):
        _app(db, insert, paper_id, SHA, "running", None)


@pytest.mark.parametrize(
    ("source", "tokens"),
    [("workers_ai", None), ("openalex", 10), ("workers_ai", -1), ("anthropic", 10)],
)
def test_api_call_estimated_tokens_only_for_workers_ai(
    db: TestDatabase, source: str, tokens: int | None
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _app(
            db,
            "INSERT INTO api_call (source, operation, http_status, cost_usd,"
            " estimated_input_tokens, called_at) VALUES (%s, 'embed_documents', 200, 0, %s, now())",
            source,
            tokens,
        )


def test_constraints_match_the_repo_definitions(db: TestDatabase) -> None:
    # CHECK 約束 autogenerate 不比：遷移與 repo 的表定義要寫同一份條件。
    assert "CHECK ((vector_dims(vector) = dimensions))" in _constraint_defs(db, "embedding")
    assert "CHECK (((task = 'embed'::text) = (prompt_version IS NULL)))" in _constraint_defs(
        db, "derivation_run"
    )
    assert (
        "CHECK (((source = 'workers_ai'::text) = (estimated_input_tokens IS NOT NULL)))"
        in _constraint_defs(db, "api_call")
    )
    assert _constraint_defs(db, "embedding_quota_hold") == {
        "CHECK ((estimated_input_tokens >= 0))",
        "CHECK ((expires_at > reserved_at))",
    }
