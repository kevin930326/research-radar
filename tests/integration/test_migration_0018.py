"""遷移 0018（`derivation_run.abstract_source`，Story 2.12、ADR-0044）對真的 PostgreSQL：降版、
升版、回填（表頭與 `batch_item` 記的摘要接得到才補，做兩次結果一樣）、兩個 CHECK。資料都是測試
自己編的。"""

import importlib
import uuid

import psycopg
import pytest

from radar.repo.db import Database
from tests.database import TestDatabase, migrate
from tests.integration.test_migration_0016 import _alembic, _submission
from tests.llm_support import create_paper, execute


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _columns(db: TestDatabase) -> set[str]:
    return {
        str(row[0])
        for row in execute(
            db,
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'derivation_run'",
        )
    }


def _paper(db: TestDatabase, n: int) -> tuple[uuid.UUID, uuid.UUID]:
    """一篇有 OpenAlex 摘要的論文：（論文 id, 摘要列 id）。"""
    database = Database(db.app_url, connect_timeout=5, pool_size=1)
    paper_id = create_paper(database, doi=f"10.1/m18-{n}", openalex_id=f"W18{n}")
    [(abstract_id,)] = execute(db, "SELECT id FROM paper_abstract WHERE paper_id = %s", paper_id)
    return paper_id, uuid.UUID(str(abstract_id))


def _run(
    db: TestDatabase,
    paper_id: uuid.UUID,
    *,
    task: str = "zh_summary",
    input_kind: str = "abstract",
    abstract_source: str | None = None,
) -> uuid.UUID:
    with_source = abstract_source is not None
    columns = ", abstract_source" if with_source else ""
    values = ", %s" if with_source else ""
    params: tuple[object, ...] = (abstract_source,) if with_source else ()
    prompt = (None, None) if task == "embed" else (1, "a" * 64)
    [(run_id,)] = execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
        f" input_sha256, input_kind, params, code_version, call_mode, status{columns})"
        f" VALUES (%s, %s, %s, %s, 'claude-haiku-5-5', %s, %s, '{{}}'::jsonb, 'test', 'batch',"
        f" 'running'{values}) RETURNING id",
        paper_id,
        task,
        *prompt,
        uuid.uuid4().hex * 2,
        input_kind,
        *params,
    )
    return uuid.UUID(str(run_id))


def _rejected_header(
    db: TestDatabase, table: str, run_id: uuid.UUID, abstract_id: uuid.UUID | None
) -> None:
    lang = "zh-Hant" if table == "zh_summary" else "zxx"
    execute(
        db,
        f"INSERT INTO {table} (paper_id, run_id, input_kind, input_sha256, abstract_id,"
        " status, reject_reason, lang)"
        f" SELECT paper_id, id, input_kind, input_sha256, %s, 'rejected', 'llm_output_invalid'"
        ", %s FROM derivation_run WHERE id = %s",
        abstract_id,
        lang,
        run_id,
    )


def _source(db: TestDatabase, run_id: uuid.UUID) -> object:
    return execute(db, "SELECT abstract_source FROM derivation_run WHERE id = %s", run_id)[0][0]


def test_downgrade_to_0017_and_upgrade_again(db: TestDatabase) -> None:
    paper_id, _ = _paper(db, 1)
    _run(db, paper_id, abstract_source="openalex")

    down = _alembic(db, "downgrade", "0017")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0017"}
    assert "abstract_source" not in _columns(db)

    migrate(db)

    assert db.revisions() == {"0018"}
    assert "abstract_source" in _columns(db)
    check = _alembic(db, "check")
    assert check.returncode == 0, check.stdout + check.stderr


def test_backfill_fills_runs_whose_abstract_is_still_there(db: TestDatabase) -> None:
    down = _alembic(db, "downgrade", "0017")
    assert down.returncode == 0, down.stdout + down.stderr
    first, first_abstract = _paper(db, 1)
    second, _ = _paper(db, 2)
    execute(db, "UPDATE paper_abstract SET source = 'semantic_scholar' WHERE paper_id = %s", second)
    [(second_abstract,)] = execute(db, "SELECT id FROM paper_abstract WHERE paper_id = %s", second)
    zh = _run(db, first)
    _rejected_header(db, "zh_summary", zh, first_abstract)
    classify = _run(db, second, task="classify")
    _rejected_header(db, "classification", classify, uuid.UUID(str(second_abstract)))
    batch = _run(db, first, task="extract_model")
    execute(
        db,
        "INSERT INTO batch_item (batch_submission_id, run_id, abstract_id, estimated_max_cost_usd)"
        " VALUES (%s, %s, %s, 0.001)",
        _submission(db),
        batch,
        first_abstract,
    )
    gone = _run(db, first, task="classify")  # 摘要列已經不在（abstract_id 接不到）
    _rejected_header(db, "classification", gone, uuid.uuid4())
    title = _run(db, first, task="zh_summary", input_kind="title")
    _rejected_header(db, "zh_summary", title, first_abstract)
    nothing = _run(db, first, task="extract_model")  # 沒有表頭也沒有 batch_item

    migrate(db)

    assert _source(db, zh) == "openalex"
    assert _source(db, classify) == "semantic_scholar"
    assert _source(db, batch) == "openalex"
    assert _source(db, gone) is None
    assert _source(db, title) is None
    assert _source(db, nothing) is None


def test_backfill_twice_gives_the_same_result(db: TestDatabase) -> None:
    down = _alembic(db, "downgrade", "0017")
    assert down.returncode == 0, down.stdout + down.stderr
    paper_id, abstract_id = _paper(db, 1)
    zh = _run(db, paper_id)
    _rejected_header(db, "zh_summary", zh, abstract_id)
    migrate(db)
    # 已經有值的不覆寫（只補 NULL）：改成別的來源後再跑一次回填。
    execute(db, "UPDATE derivation_run SET abstract_source = 'uploaded_pdf' WHERE id = %s", zh)
    snapshot = execute(db, "SELECT id, abstract_source FROM derivation_run ORDER BY id")
    module = importlib.import_module("migrations.versions.0018_run_abstract_source")

    execute(db, module.BACKFILL)
    execute(db, module.BACKFILL)

    assert execute(db, "SELECT id, abstract_source FROM derivation_run ORDER BY id") == snapshot
    assert _source(db, zh) == "uploaded_pdf"


@pytest.mark.parametrize(
    ("task", "input_kind", "source"),
    [
        ("zh_summary", "abstract", "crossref"),  # 不是摘要來源
        ("zh_summary", "abstract", ""),
        ("embed", "abstract", "openalex"),  # 向量化先不記
    ],
)
def test_checks_reject_bad_sources(
    db: TestDatabase, task: str, input_kind: str, source: str
) -> None:
    paper_id, _ = _paper(db, 1)
    with pytest.raises(psycopg.errors.CheckViolation):
        _run(db, paper_id, task=task, input_kind=input_kind, abstract_source=source)


def test_checks_accept_every_abstract_source_and_null(db: TestDatabase) -> None:
    paper_id, _ = _paper(db, 1)
    for source in ("openalex", "semantic_scholar", "uploaded_pdf"):
        _run(db, paper_id, task="classify", abstract_source=source)
    _run(db, paper_id, task="embed")
    _run(db, paper_id, input_kind="title")
