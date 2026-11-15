"""`services/ai_results.record_llm_result` 對真的 PostgreSQL：狀態怎麼決定、舊的 active 怎麼切換。

`derivation_run` 直接用 repo 新增（不經閘門、不呼叫模型），再呼叫 `record_llm_result` 看寫成什麼。
每個測試一個全新、已遷移的資料庫；prompt 寫在 tmp 資料夾（測試自己編的內容）。
"""

import uuid
from pathlib import Path

import psycopg
import pytest

from radar.domain.ai_outputs import (
    CallMode,
    GatewayConfig,
    InputKind,
    LlmTask,
    NewDerivationRun,
    OutputLang,
    ResultStatus,
    RunKey,
    build_llm_input,
)
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import input_sha256
from radar.domain.permissions import PermissionDeniedError
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.repo.llm_ledger import create_derivation_run, update_derivation_run_failed
from radar.repo.papers import find_paper_llm_source
from radar.services.ai_results import ResultTargetNotFoundError, record_llm_result
from tests.database import TestDatabase, migrate
from tests.llm_support import (
    create_paper,
    execute,
    gateway_config,
    make_prompts,
    prompt_text,
    register_prompts,
    write_prompt,
)

MODEL = "claude-haiku-5-5"
SYSTEM = SystemViewer()


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def prompts(tmp_path: Path) -> Path:
    return make_prompts(tmp_path / "prompts", LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY)


@pytest.fixture
def config(prompts: Path) -> GatewayConfig:
    return gateway_config(prompts)


@pytest.fixture
def paper_id(database: Database) -> uuid.UUID:
    return create_paper(database)


def _new_run(
    database: Database,
    paper_id: uuid.UUID,
    *,
    task: LlmTask = LlmTask.ZH_SUMMARY,
    model: str = MODEL,
    prompt_version: int = 1,
    taxonomy_version: str | None = None,
    eval_run_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """依論文現在的輸入新增一筆 `running` 的 run，回（run id, 用的摘要 id）。"""
    with database.transaction() as connection:
        source = find_paper_llm_source(SYSTEM, connection, paper_id, for_update=False)
        assert source is not None
        llm_input = build_llm_input(source, InputKind.ABSTRACT)
        fingerprint = input_sha256(
            llm_input.blocks, task, taxonomy_version=taxonomy_version, context_values_version=None
        )
        run_id = create_derivation_run(
            connection,
            NewDerivationRun(
                key=RunKey(paper_id, task, prompt_version, model, fingerprint),
                prompt_sha256="0" * 64,
                input_kind=InputKind.ABSTRACT,
                params={},
                taxonomy_version=taxonomy_version,
                context_values_version=None,
                code_version="test-0.0.0",
                call_mode=CallMode.INTERACTIVE,
                eval_run_id=eval_run_id,
            ),
        )
    assert run_id is not None
    return run_id, llm_input.abstract_id


def _record(
    database: Database,
    config: GatewayConfig,
    paper_id: uuid.UUID,
    run_id: uuid.UUID,
    abstract_id: uuid.UUID | None,
    *,
    lang: OutputLang = OutputLang.ZH_HANT,
    reject_reason: ErrorCode | None = None,
    taxonomy_version: str | None = None,
) -> ResultStatus:
    return record_llm_result(
        SYSTEM,
        database,
        config,
        paper_id=paper_id,
        run_id=run_id,
        lang=lang,
        abstract_id=abstract_id,
        reject_reason=reject_reason,
        taxonomy_version=taxonomy_version,
    )


def _run_status(db: TestDatabase, run_id: uuid.UUID) -> tuple[str, str | None]:
    ((status, reason),) = execute(
        db, "SELECT status, failure_reason FROM derivation_run WHERE id = %s", run_id
    )
    return status, reason


def _header(db: TestDatabase, run_id: uuid.UUID, table: str = "zh_summary") -> tuple[object, ...]:
    (row,) = execute(
        db,
        f"SELECT status, reject_reason, lang, abstract_id FROM {table} WHERE run_id = %s",
        run_id,
    )
    return tuple(row)


def test_current_result_becomes_active_and_run_succeeds(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    run_id, abstract_id = _new_run(database, paper_id)

    assert _record(database, config, paper_id, run_id, abstract_id) is ResultStatus.ACTIVE

    assert _header(db, run_id) == ("active", None, "zh-Hant", abstract_id)
    assert _run_status(db, run_id) == ("succeeded", None)


def test_new_active_supersedes_old_active_of_same_language_only(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    first, abstract_id = _new_run(database, paper_id)
    _record(database, config, paper_id, first, abstract_id)
    english, _ = _new_run(database, paper_id)
    _record(database, config, paper_id, english, abstract_id, lang=OutputLang.EN)

    second, _ = _new_run(database, paper_id)
    assert _record(database, config, paper_id, second, abstract_id) is ResultStatus.ACTIVE

    assert _header(db, first)[0] == "superseded"
    assert _header(db, english)[0] == "active"
    assert _header(db, second)[0] == "active"


def test_rejected_result_is_rejected_and_run_succeeds(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    run_id, abstract_id = _new_run(database, paper_id)

    status = _record(
        database, config, paper_id, run_id, abstract_id, reject_reason=ErrorCode.LLM_REFUSAL
    )

    assert status is ResultStatus.REJECTED
    assert _header(db, run_id)[:2] == ("rejected", "llm_refusal")
    assert _run_status(db, run_id) == ("succeeded", None)


def test_rejection_does_not_touch_existing_active(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    first, abstract_id = _new_run(database, paper_id)
    _record(database, config, paper_id, first, abstract_id)
    second, _ = _new_run(database, paper_id)

    _record(database, config, paper_id, second, abstract_id, reject_reason=ErrorCode.LLM_TRUNCATED)

    assert _header(db, first)[0] == "active"


def test_eval_result_is_candidate_and_does_not_touch_active(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    normal, abstract_id = _new_run(database, paper_id)
    _record(database, config, paper_id, normal, abstract_id)
    evaluated, _ = _new_run(database, paper_id, eval_run_id=uuid.uuid4())

    assert _record(database, config, paper_id, evaluated, abstract_id) is ResultStatus.CANDIDATE

    assert _header(db, normal)[0] == "active"
    assert _header(db, evaluated)[0] == "candidate"
    assert _run_status(db, evaluated) == ("succeeded", None)


def test_run_already_failed_by_sweep_is_superseded_and_stays_failed(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    run_id, abstract_id = _new_run(database, paper_id)
    with database.transaction() as connection:
        update_derivation_run_failed(connection, run_id, ErrorCode.LLM_RUN_EXPIRED)

    assert _record(database, config, paper_id, run_id, abstract_id) is ResultStatus.SUPERSEDED

    assert _run_status(db, run_id) == ("failed", "llm_run_expired")


def test_changed_abstract_is_superseded(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    run_id, abstract_id = _new_run(database, paper_id)
    execute(db, "UPDATE paper_abstract SET content = 'Changed.' WHERE paper_id = %s", paper_id)

    assert _record(database, config, paper_id, run_id, abstract_id) is ResultStatus.SUPERSEDED
    assert _run_status(db, run_id) == ("succeeded", None)


def test_abstract_gone_is_superseded(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    run_id, abstract_id = _new_run(database, paper_id)
    execute(db, "DELETE FROM paper_abstract WHERE paper_id = %s", paper_id)

    # abstract_id 沒有外鍵：摘要被刪了，表頭照樣寫得進去。
    assert _record(database, config, paper_id, run_id, abstract_id) is ResultStatus.SUPERSEDED
    assert _header(db, run_id)[3] == abstract_id


def test_prompt_moved_to_new_version_is_superseded(
    db: TestDatabase, database: Database, prompts: Path, paper_id: uuid.UUID
) -> None:
    run_id, abstract_id = _new_run(database, paper_id)
    write_prompt(prompts, LlmTask.ZH_SUMMARY, 2, prompt_text("第二版的說明。"))
    register_prompts(prompts)

    status = _record(database, gateway_config(prompts), paper_id, run_id, abstract_id)

    assert status is ResultStatus.SUPERSEDED


def test_model_no_longer_configured_is_superseded(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    run_id, abstract_id = _new_run(database, paper_id, model="claude-old-model")

    assert _record(database, config, paper_id, run_id, abstract_id) is ResultStatus.SUPERSEDED


@pytest.mark.parametrize(
    ("current_taxonomy", "expected"),
    [("tax-v1", ResultStatus.ACTIVE), ("tax-v2", ResultStatus.SUPERSEDED)],
)
def test_taxonomy_version_is_part_of_current_input(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    paper_id: uuid.UUID,
    current_taxonomy: str,
    expected: ResultStatus,
) -> None:
    run_id, abstract_id = _new_run(
        database, paper_id, task=LlmTask.CLASSIFY, taxonomy_version="tax-v1"
    )

    status = _record(
        database,
        config,
        paper_id,
        run_id,
        abstract_id,
        lang=OutputLang.ZXX,
        taxonomy_version=current_taxonomy,
    )

    assert status is expected
    assert _header(db, run_id, "classification")[0] == expected.value


def test_missing_paper_or_run_or_mismatch_raises(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    run_id, abstract_id = _new_run(database, paper_id)
    other_paper = create_paper(database, doi="10.1/other", openalex_id="W2")

    with pytest.raises(ResultTargetNotFoundError):
        _record(database, config, paper_id, uuid.uuid4(), abstract_id)
    with pytest.raises(ResultTargetNotFoundError):
        _record(database, config, uuid.uuid4(), run_id, abstract_id)
    with pytest.raises(ResultTargetNotFoundError):
        _record(database, config, other_paper, run_id, abstract_id)
    assert execute(db, "SELECT count(*) FROM zh_summary") == [(0,)]
    assert _run_status(db, run_id) == ("running", None)


def test_member_cannot_record_results(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    run_id, abstract_id = _new_run(database, paper_id)

    with pytest.raises(PermissionDeniedError):
        record_llm_result(
            MemberViewer(member_id=uuid.uuid4(), is_admin=True),
            database,
            config,
            paper_id=paper_id,
            run_id=run_id,
            lang=OutputLang.ZH_HANT,
            abstract_id=abstract_id,
            reject_reason=None,
        )
    assert execute(db, "SELECT count(*) FROM zh_summary") == [(0,)]


def test_two_active_rows_for_same_language_are_refused_by_database(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    run_id, abstract_id = _new_run(database, paper_id)
    _record(database, config, paper_id, run_id, abstract_id)
    other, _ = _new_run(database, paper_id)

    # 部分唯一索引兜底：繞過 ai_results 直接塞第二筆 active 會被擋下。
    with pytest.raises(psycopg.errors.UniqueViolation):
        execute(
            db,
            "INSERT INTO zh_summary (paper_id, run_id, input_kind, input_sha256, lang, status)"
            " SELECT paper_id, %s, input_kind, input_sha256, lang, 'active' FROM zh_summary",
            other,
        )
