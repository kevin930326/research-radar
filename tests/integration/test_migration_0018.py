"""遷移 0018（`derivation_run.abstract_source`，Story 2.12、ADR-0044）對真的 PostgreSQL：降版、
升版、回填（表頭與 `batch_item` 記的摘要接得到才補，做兩次結果一樣）、兩個 CHECK。資料都是測試
自己編的。"""

import importlib
import uuid

import psycopg
import pytest

from radar.domain.ai_outputs import (
    CurrentAbstract,
    InputKind,
    LlmTask,
    PaperLlmSource,
    build_llm_input,
)
from radar.domain.embeddings import build_embedding_input
from radar.domain.fingerprints import embedding_input_sha256, input_sha256
from radar.domain.papers import AbstractSource
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.papers import find_paper_llm_source
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


# --- 審查後第 5 點：在途的 run；第 2 點：向量的來源 ---------------------------------------------


def _current_sha(db: TestDatabase, paper_id: uuid.UUID, task: LlmTask, taxonomy: str | None) -> str:
    """這篇論文現在的標題與摘要算出的輸入指紋（和閘門算的一樣）。"""
    database = Database(db.app_url, connect_timeout=5, pool_size=1)
    with database.transaction() as connection:
        source = find_paper_llm_source(SystemViewer(), connection, paper_id, for_update=False)
    assert source is not None
    llm_input = build_llm_input(source, InputKind.ABSTRACT)
    return input_sha256(
        llm_input.blocks, task, taxonomy_version=taxonomy, context_values_version=None
    )


def _in_flight(
    db: TestDatabase,
    paper_id: uuid.UUID,
    task: str,
    sha: str,
    *,
    status: str = "running",
    failure_reason: str | None = None,
    taxonomy: str | None = None,
) -> uuid.UUID:
    """一筆互動的 run（沒有表頭、沒有 batch_item）。"""
    [(run_id,)] = execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
        " input_sha256, input_kind, params, taxonomy_version, code_version, call_mode, status,"
        " failure_reason, finished_at) VALUES (%s, %s, 1, %s, 'claude-haiku-5-5', %s,"
        " 'abstract', '{}'::jsonb, %s, 'test', 'interactive', %s, %s,"
        " CASE WHEN %s = 'running' THEN NULL ELSE now() END) RETURNING id",
        paper_id,
        task,
        "a" * 64,
        sha,
        taxonomy,
        status,
        failure_reason,
        status,
    )
    return uuid.UUID(str(run_id))


def test_backfill_identifies_in_flight_runs_by_their_fingerprint(db: TestDatabase) -> None:
    down = _alembic(db, "downgrade", "0017")
    assert down.returncode == 0, down.stdout + down.stderr
    paper_id, _ = _paper(db, 1)
    zh_sha = _current_sha(db, paper_id, LlmTask.ZH_SUMMARY, None)
    running = _in_flight(db, paper_id, "zh_summary", zh_sha)
    pending = _in_flight(
        db,
        paper_id,
        "classify",
        _current_sha(db, paper_id, LlmTask.CLASSIFY, "v-test"),
        status="failed",
        failure_reason="llm_result_pending",
        taxonomy="v-test",
    )
    changed = _in_flight(db, paper_id, "zh_summary", "c" * 64)  # 輸入已經變了
    done = _in_flight(db, paper_id, "zh_summary", zh_sha, status="succeeded")  # 不是在途
    expired = _in_flight(
        db, paper_id, "zh_summary", zh_sha, status="failed", failure_reason="llm_run_expired"
    )

    migrate(db)

    assert _source(db, running) == "openalex"
    assert _source(db, pending) == "openalex"
    assert _source(db, changed) is None
    assert _source(db, done) is None
    assert _source(db, expired) is None


def _embedding_columns(db: TestDatabase) -> set[str]:
    return {
        str(row[0])
        for row in execute(
            db,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'embedding'",
        )
    }


def _vector(
    db: TestDatabase, paper_id: uuid.UUID, input_kind: str, sha256: str | None = None
) -> None:
    """一筆 qwen3（遷移 0015 登記的 1,024 維）向量與它的向量化 run（`sha256` 沒給時是隨機的指紋，
    對不上任何輸入）。"""
    [(run_id,)] = execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, model, input_sha256, input_kind, params,"
        " code_version, call_mode, status, finished_at) VALUES (%s, 'embed',"
        " '@cf/qwen/qwen3-embedding-0.6b', %s, %s, '{}'::jsonb, 'test', 'interactive',"
        " 'succeeded', now()) RETURNING id",
        paper_id,
        sha256 or uuid.uuid4().hex * 2,
        input_kind,
    )
    execute(
        db,
        "INSERT INTO embedding (item_type, item_id, model_id, dimensions, vector, input_sha256,"
        " run_id) SELECT 'paper', paper_id, model, 1024, CAST(%s AS halfvec), input_sha256, id"
        " FROM derivation_run WHERE id = %s",
        "[" + ",".join(["0.25"] * 1024) + "]",
        run_id,
    )


def _vector_source(db: TestDatabase, paper_id: uuid.UUID) -> object:
    return execute(db, "SELECT abstract_source FROM embedding WHERE item_id = %s", paper_id)[0][0]


def _embedding_sha(db: TestDatabase, paper_id: uuid.UUID) -> str:
    """這篇論文現在的標題＋目前摘要送去向量化的指紋（和向量化服務算的一樣）。"""
    [(title, abstract_id, source, content)] = execute(
        db,
        "SELECT p.title, a.id, a.source, a.content FROM paper AS p JOIN paper_abstract AS a"
        " ON a.paper_id = p.id AND a.is_current WHERE p.id = %s",
        paper_id,
    )
    paper = PaperLlmSource(
        paper_id=paper_id,
        title=title,
        abstract=CurrentAbstract(
            id=uuid.UUID(str(abstract_id)), source=AbstractSource(source), content=content
        ),
    )
    embedding_input = build_embedding_input(paper, InputKind.ABSTRACT)
    return embedding_input_sha256(embedding_input.text, InputKind.ABSTRACT.value)


def test_embedding_source_is_backfilled_only_when_proven(db: TestDatabase) -> None:
    # 審查後第二輪 R2：目前摘要從向量化之前就沒改過、指紋也對得上才補；其他一律 NULL。
    down = _alembic(db, "downgrade", "0017")
    assert down.returncode == 0, down.stdout + down.stderr
    assert "abstract_source" not in _embedding_columns(db)
    proven, _ = _paper(db, 1)
    titled, _ = _paper(db, 2)
    gone, _ = _paper(db, 3)
    changed_text, _ = _paper(db, 4)
    _vector(db, proven, "abstract", _embedding_sha(db, proven))
    _vector(db, titled, "title")
    _vector(db, gone, "abstract", _embedding_sha(db, gone))
    execute(db, "DELETE FROM paper_abstract WHERE paper_id = %s", gone)
    # 文字不同：向量是用舊的摘要算的，之後摘要改了（這裡刻意不動 updated_at，只靠指紋擋）。
    _vector(db, changed_text, "abstract", _embedding_sha(db, changed_text))
    execute(
        db,
        "UPDATE paper_abstract SET content = 'A different abstract.' WHERE paper_id = %s",
        changed_text,
    )
    # 標題改了（摘要列沒動，時間條件成立）：指紋對不上。
    retitled, _ = _paper(db, 5)
    _vector(db, retitled, "abstract", _embedding_sha(db, retitled))
    execute(db, "UPDATE paper SET title = 'Another title' WHERE id = %s", retitled)

    migrate(db)

    assert "abstract_source" in _embedding_columns(db)
    assert _vector_source(db, proven) == "openalex"
    assert _vector_source(db, titled) is None
    assert _vector_source(db, gone) is None  # 已經沒有目前的摘要：證明不了
    assert _vector_source(db, changed_text) is None  # 文字不同：證明不了（照指紋規則會重算）
    assert _vector_source(db, retitled) is None
    with pytest.raises(psycopg.errors.CheckViolation):
        execute(db, "UPDATE embedding SET abstract_source = 'crossref'")
    down_again = _alembic(db, "downgrade", "0017")
    assert down_again.returncode == 0, down_again.stdout + down_again.stderr
    assert "abstract_source" not in _embedding_columns(db)


def test_embedding_source_is_not_guessed_when_the_source_switched_with_the_same_text(
    db: TestDatabase,
) -> None:
    # 向量是用 OpenAlex 的摘要算的；之後目前摘要換成文字完全相同的 Semantic Scholar。指紋對得上
    # 目前的輸入，但目前摘要那一列在向量之後才改過：不能拿目前來源當歷史來源，留 NULL。
    down = _alembic(db, "downgrade", "0017")
    assert down.returncode == 0, down.stdout + down.stderr
    switched, _ = _paper(db, 1)
    sha = _embedding_sha(db, switched)
    _vector(db, switched, "abstract", sha)
    execute(
        db,
        "UPDATE paper_abstract SET is_current = false, updated_at = clock_timestamp()"
        " WHERE paper_id = %s",
        switched,
    )
    execute(
        db,
        "INSERT INTO paper_abstract (paper_id, source, content, is_current, created_at,"
        " updated_at) SELECT paper_id, 'semantic_scholar', content, true, clock_timestamp(),"
        " clock_timestamp() FROM paper_abstract WHERE paper_id = %s AND source = 'openalex'",
        switched,
    )
    assert _embedding_sha(db, switched) == sha  # 文字一樣，指紋規則不會挑到它

    migrate(db)

    assert _vector_source(db, switched) is None


def test_embedding_source_is_not_guessed_when_the_abstract_changed_back(
    db: TestDatabase,
) -> None:
    # 向量之後目前摘要改過（又改回同樣的文字）：向量化當時讀到的是不是這一列證明不了，留 NULL。
    down = _alembic(db, "downgrade", "0017")
    assert down.returncode == 0, down.stdout + down.stderr
    touched, _ = _paper(db, 1)
    _vector(db, touched, "abstract", _embedding_sha(db, touched))
    execute(
        db,
        "UPDATE paper_abstract SET updated_at = clock_timestamp() + interval '1 second'"
        " WHERE paper_id = %s",
        touched,
    )

    migrate(db)

    assert _vector_source(db, touched) is None
