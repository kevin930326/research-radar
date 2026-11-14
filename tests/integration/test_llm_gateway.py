"""LLM 閘門對真的 PostgreSQL：spec 2.2 的 I/O 表每一列，加上 NFR-5（同一份輸入再跑不重複花錢）。

模型一律用 `FakeLlmProvider`（不連網路）；prompt 寫在 tmp 資料夾（測試自己編的內容）；價格表用
真的 `config/models.yaml`。每個測試一個全新、已遷移的資料庫，應用程式角色連線。
"""

import uuid
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from radar.adapters.fake import FakeLlmProvider, fake_completion
from radar.domain.ai_outputs import (
    CallMode,
    GatewayConfig,
    InputKind,
    LlmInput,
    LlmTask,
    OutputLang,
    OutputRejectedError,
)
from radar.domain.errors import ErrorCode
from radar.domain.llm import (
    LlmConfigurationError,
    LlmConnectionError,
    LlmError,
    LlmRateLimitedError,
    LlmServerError,
    LlmTimeoutError,
    LlmUsage,
)
from radar.domain.llm_schemas import FakeResearchQuestion
from radar.domain.permissions import PermissionDeniedError
from radar.domain.pricing import TaskModel, llm_call_cost
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.services import llm_gateway
from radar.services.llm_gateway import (
    GatewayOutcome,
    GatewayResult,
    LlmJob,
    OutputValidator,
    expire_stale_runs,
    run_llm_job,
)
from tests.database import TestDatabase, migrate
from tests.llm_support import (
    GatedProvider,
    create_paper,
    execute,
    gateway_config,
    make_prompts,
    models_config,
    prompt_text,
    register_prompts,
    set_abstract,
    table_counts,
    write_prompt,
)

MODEL = "claude-haiku-5-5"
GOOD = '{"research_question": "團隊怎麼採用資訊系統？"}'
USAGE = LlmUsage(input_tokens=1200, output_tokens=300)
SYSTEM = SystemViewer()


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=4)


@pytest.fixture
def prompts(tmp_path: Path) -> Path:
    return make_prompts(tmp_path / "prompts", LlmTask.ZH_SUMMARY)


@pytest.fixture
def config(prompts: Path) -> GatewayConfig:
    return gateway_config(prompts)


@pytest.fixture
def paper_id(database: Database) -> uuid.UUID:
    return create_paper(database)


def _job(
    paper_id: uuid.UUID,
    *,
    input_kind: InputKind = InputKind.ABSTRACT,
    validate_output: OutputValidator | None = None,
) -> LlmJob:
    return LlmJob(
        paper_id=paper_id,
        task=LlmTask.ZH_SUMMARY,
        input_kind=input_kind,
        lang=OutputLang.ZH_HANT,
        validate_output=validate_output,
    )


def _provider(*replies: object) -> FakeLlmProvider:
    return FakeLlmProvider(replies or [fake_completion(GOOD, usage=USAGE)])  # type: ignore[arg-type]


def _run(
    database: Database,
    provider: object,
    config: GatewayConfig,
    job: LlmJob,
    *,
    budget: bool = True,
    eval_run_id: uuid.UUID | None = None,
) -> GatewayResult:
    return run_llm_job(
        SYSTEM,
        database,
        provider,  # type: ignore[arg-type]
        config,
        job,
        budget_allows=lambda _task: budget,
        eval_run_id=eval_run_id,
    )


def _runs(db: TestDatabase) -> list[tuple[str, str | None]]:
    return [
        (row[0], row[1])
        for row in execute(
            db, "SELECT status, failure_reason FROM derivation_run ORDER BY created_at, id"
        )
    ]


def _headers(db: TestDatabase, table: str = "zh_summary") -> list[tuple[str, str | None]]:
    return [
        (row[0], row[1])
        for row in execute(db, f"SELECT status, reject_reason FROM {table} ORDER BY created_at, id")
    ]


def _expected_cost(usage: LlmUsage = USAGE, *, is_batch: bool = False) -> Decimal:
    return llm_call_cost(models_config(), MODEL, usage, is_batch=is_batch).cost_usd


# --- 正常、再跑一次（NFR-5） ---------------------------------------------------------------------


def test_normal_run_writes_run_call_and_active_header(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    provider = _provider()
    result = _run(database, provider, config, _job(paper_id))

    assert result.outcome is GatewayOutcome.ACTIVE
    assert isinstance(result.output, FakeResearchQuestion)
    assert result.cost_usd == _expected_cost()
    assert _runs(db) == [("succeeded", None)]
    assert _headers(db) == [("active", None)]
    call = execute(
        db,
        "SELECT run_id, input_tokens, output_tokens, cost_usd, price_tier, is_batch, response_id,"
        " request_id, stop_reason FROM llm_call",
    )
    assert call == [
        (
            result.run_id,
            1200,
            300,
            _expected_cost(),
            "standard",
            False,
            "msg_fake_0001",
            "req_fake_0001",
            "end_turn",
        )
    ]
    # 來歷：鍵、prompt 雜湊、參數、版本都記下來。
    run = execute(
        db,
        "SELECT task, prompt_version, model, input_kind, call_mode, code_version, eval_run_id,"
        " params ->> 'output_schema', finished_at IS NOT NULL FROM derivation_run",
    )
    assert run == [
        ("zh_summary", 1, MODEL, "abstract", "interactive", "test-0.0.0", None,
         "FakeResearchQuestion", True)
    ]  # fmt: skip
    header = execute(
        db,
        "SELECT h.run_id, h.input_kind, h.input_sha256 = r.input_sha256, h.lang,"
        " h.abstract_id = a.id FROM zh_summary AS h JOIN derivation_run AS r ON r.id = h.run_id"
        " JOIN paper_abstract AS a ON a.paper_id = h.paper_id AND a.is_current",
    )
    assert header == [(result.run_id, "abstract", True, "zh-Hant", True)]
    # 送出去的是標題＋摘要兩個文件區塊，模型是設定檔指定的。
    (request,) = provider.requests
    assert request.model == MODEL
    assert len(request.content) == 2


def test_rerun_with_same_input_skips_and_keeps_ledger_unchanged(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    assert _run(database, _provider(), config, _job(paper_id)).outcome is GatewayOutcome.ACTIVE
    before = table_counts(db)

    provider = FakeLlmProvider()  # 沒有準備回覆：真的呼叫就會失敗
    result = _run(database, provider, config, _job(paper_id))

    assert result == GatewayResult(GatewayOutcome.SKIPPED)
    assert provider.requests == []
    assert table_counts(db) == before


def test_title_only_input_has_no_abstract_id(
    db: TestDatabase, database: Database, config: GatewayConfig
) -> None:
    paper_id = create_paper(database, abstract=None)
    provider = _provider()

    result = _run(database, provider, config, _job(paper_id, input_kind=InputKind.TITLE))

    assert result.outcome is GatewayOutcome.ACTIVE
    assert execute(db, "SELECT input_kind, abstract_id FROM zh_summary") == [("title", None)]
    assert len(provider.requests[0].content) == 1


# --- should_run：已拒收、進行中 -------------------------------------------------------------------


def test_existing_rejected_result_skips(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    first = _run(database, _provider(fake_completion("{}", usage=USAGE)), config, _job(paper_id))
    assert first.outcome is GatewayOutcome.REJECTED
    before = table_counts(db)

    provider = FakeLlmProvider()
    assert _run(database, provider, config, _job(paper_id)).outcome is GatewayOutcome.SKIPPED
    assert provider.requests == []
    assert table_counts(db) == before


def test_running_run_with_same_key_skips(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    nested: list[GatewayResult] = []
    nested_provider = FakeLlmProvider()

    def run_again() -> None:
        # 第一次呼叫還在等模型時（running 已經 commit），同一組鍵再跑一次。
        nested.append(_run(database, nested_provider, config, _job(paper_id)))

    provider = GatedProvider(_provider(), gated=False, before_reply=run_again)
    result = _run(database, provider, config, _job(paper_id))

    assert nested == [GatewayResult(GatewayOutcome.SKIPPED)]
    assert nested_provider.requests == []
    assert result.outcome is GatewayOutcome.ACTIVE
    assert _runs(db) == [("succeeded", None)]


# --- 輸入或 prompt 版本變了 -----------------------------------------------------------------------


def test_changed_abstract_runs_again_and_supersedes_old(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    first = _run(database, _provider(), config, _job(paper_id))
    set_abstract(db, paper_id, "A completely different abstract about platforms.")

    second = _run(database, _provider(), config, _job(paper_id))

    assert second.outcome is GatewayOutcome.ACTIVE
    rows = execute(db, "SELECT run_id, status FROM zh_summary ORDER BY created_at, id")
    assert rows == [(first.run_id, "superseded"), (second.run_id, "active")]
    assert table_counts(db)["llm_call"] == 2


def test_prompt_version_bump_runs_again_and_supersedes_old(
    db: TestDatabase, database: Database, prompts: Path, paper_id: uuid.UUID
) -> None:
    first = _run(database, _provider(), gateway_config(prompts), _job(paper_id))
    write_prompt(prompts, LlmTask.ZH_SUMMARY, 2, prompt_text("第二版：請找出研究問題，用 JSON。"))
    register_prompts(prompts)

    second = _run(database, _provider(), gateway_config(prompts), _job(paper_id))

    assert second.outcome is GatewayOutcome.ACTIVE
    rows = execute(
        db,
        "SELECT h.run_id, h.status, r.prompt_version FROM zh_summary AS h"
        " JOIN derivation_run AS r ON r.id = h.run_id ORDER BY h.created_at, h.id",
    )
    assert rows == [(first.run_id, "superseded", 1), (second.run_id, "active", 2)]


# --- 呼叫之前擋下：不寫任何列 --------------------------------------------------------------------


def test_unlicensed_abstract_source_is_blocked(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    execute(
        db, "UPDATE paper_abstract SET source = 'semantic_scholar' WHERE paper_id = %s", paper_id
    )
    provider = FakeLlmProvider()

    result = _run(database, provider, config, _job(paper_id))

    assert result == GatewayResult(GatewayOutcome.BLOCKED, reason=ErrorCode.CLOUD_BLOCKED_SOURCE)
    assert provider.requests == []
    assert set(table_counts(db).values()) == {0}


def test_missing_abstract_is_no_input(
    db: TestDatabase, database: Database, config: GatewayConfig
) -> None:
    paper_id = create_paper(database, abstract=None)
    provider = FakeLlmProvider()

    assert _run(database, provider, config, _job(paper_id)) == GatewayResult(
        GatewayOutcome.NO_INPUT
    )
    assert provider.requests == []
    assert set(table_counts(db).values()) == {0}


def test_budget_denied_stops_before_reading_prompt(
    db: TestDatabase, database: Database, tmp_path: Path, paper_id: uuid.UUID
) -> None:
    # prompt 資料夾不存在：只要讀了 prompt 就會丟錯，證明預算擋下後沒有往下走。
    config = gateway_config(tmp_path / "no-prompts")
    provider = FakeLlmProvider()

    result = _run(database, provider, config, _job(paper_id), budget=False)

    assert result == GatewayResult(GatewayOutcome.DEFERRED_BUDGET)
    assert provider.requests == []
    assert set(table_counts(db).values()) == {0}


def _small_limit(config: GatewayConfig, *, allow: bool = False) -> GatewayConfig:
    """上限改成 50 tokens（一定超過）；`allow` 是 zh_summary 的 `allow_over_input_limit`。"""
    tasks = dict(config.models.tasks)
    tasks[LlmTask.ZH_SUMMARY] = TaskModel(model=MODEL, allow_over_input_limit=allow)
    return replace(
        config, models=config.models.model_copy(update={"max_input_tokens": 50, "tasks": tasks})
    )


def test_input_over_limit_is_too_large(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    provider = FakeLlmProvider()

    result = _run(database, provider, _small_limit(config), _job(paper_id))

    assert result == GatewayResult(GatewayOutcome.TOO_LARGE, reason=ErrorCode.LLM_INPUT_TOO_LARGE)
    assert provider.requests == []
    assert set(table_counts(db).values()) == {0}


def test_task_allowed_over_limit_is_sent(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    allowed = _small_limit(config, allow=True)

    assert _run(database, _provider(), allowed, _job(paper_id)).outcome is GatewayOutcome.ACTIVE


# --- 拒收：帳照記、不重試 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("completion_kwargs", "text", "code"),
    [
        ({"stop_reason": "refusal"}, GOOD, ErrorCode.LLM_REFUSAL),
        ({"stop_reason": "max_tokens"}, '{"research_question": "團隊', ErrorCode.LLM_TRUNCATED),
        ({}, "這不是 JSON", ErrorCode.LLM_MALFORMED_RESPONSE),
    ],
    ids=["refusal", "truncated", "malformed"],
)
def test_refusal_truncation_and_malformed_are_rejected_with_call_recorded(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    paper_id: uuid.UUID,
    completion_kwargs: dict[str, str],
    text: str,
    code: ErrorCode,
) -> None:
    provider = _provider(fake_completion(text, usage=USAGE, **completion_kwargs))  # type: ignore[arg-type]

    result = _run(database, provider, config, _job(paper_id))

    assert result.outcome is GatewayOutcome.REJECTED
    assert result.reason is code
    assert result.output is None
    assert result.cost_usd == _expected_cost()
    assert len(provider.requests) == 1  # 不重試
    assert _runs(db) == [("succeeded", None)]
    assert _headers(db) == [("rejected", code.value)]
    assert execute(db, "SELECT cost_usd FROM llm_call") == [(_expected_cost(),)]


@pytest.mark.parametrize(
    "text",
    [
        '{"research_question": "問題", "extra": 1}',
        "{}",
        '{"research_question": 42}',
    ],
    ids=["extra-field", "missing-field", "wrong-type"],
)
def test_pydantic_failure_is_rejected_as_output_invalid(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID, text: str
) -> None:
    result = _run(database, _provider(fake_completion(text, usage=USAGE)), config, _job(paper_id))

    assert (result.outcome, result.reason) == (
        GatewayOutcome.REJECTED,
        ErrorCode.LLM_OUTPUT_INVALID,
    )
    assert _headers(db) == [("rejected", "llm_output_invalid")]
    assert table_counts(db)["llm_call"] == 1


def test_domain_validation_failure_is_rejected_with_its_code(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    seen: list[tuple[object, LlmInput]] = []

    def reject(output: object, llm_input: LlmInput) -> None:
        seen.append((output, llm_input))
        raise OutputRejectedError(ErrorCode.LLM_INVALID_REQUEST, "測試：輸出夾指令")

    result = _run(database, _provider(), config, _job(paper_id, validate_output=reject))

    assert (result.outcome, result.reason) == (
        GatewayOutcome.REJECTED,
        ErrorCode.LLM_INVALID_REQUEST,
    )
    assert _headers(db) == [("rejected", "llm_invalid_request")]
    ((output, llm_input),) = seen
    assert isinstance(output, FakeResearchQuestion)
    assert llm_input.input_kind is InputKind.ABSTRACT


def test_unstorable_output_is_rejected(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    text = '{"research_question": "a\\u0000b"}'

    result = _run(database, _provider(fake_completion(text, usage=USAGE)), config, _job(paper_id))

    assert result.reason is ErrorCode.LLM_OUTPUT_UNSTORABLE
    assert _headers(db) == [("rejected", "llm_output_unstorable")]


def test_unstorable_raw_response_is_kept_as_null(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    completion = fake_completion(GOOD, usage=USAGE, raw={"content": "a\x00b"})

    assert _run(database, _provider(completion), config, _job(paper_id)).outcome is (
        GatewayOutcome.ACTIVE
    )
    assert execute(db, "SELECT raw_response FROM llm_call") == [(None,)]


def test_storable_raw_response_is_kept(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    completion = fake_completion(GOOD, usage=USAGE, raw={"id": "msg_fake_0001"})

    _run(database, _provider(completion), config, _job(paper_id))

    assert execute(db, "SELECT raw_response FROM llm_call") == [({"id": "msg_fake_0001"},)]


# --- 暫時性錯誤、設定錯 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        LlmTimeoutError("測試：逾時"),
        LlmRateLimitedError("測試：限流", status=429),
        LlmServerError("測試：服務端錯誤", status=529),
        LlmConnectionError("測試：連線錯誤"),
    ],
    ids=["timeout", "rate-limited", "server", "connection"],
)
def test_transient_error_fails_run_and_allows_next_run(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    paper_id: uuid.UUID,
    error: LlmError,
) -> None:
    result = _run(database, _provider(error), config, _job(paper_id))

    assert result.outcome is GatewayOutcome.TRANSIENT_FAILURE
    assert result.reason is error.code
    assert _runs(db) == [("failed", error.code.value)]
    counts = table_counts(db)
    assert (counts["llm_call"], counts["zh_summary"]) == (0, 0)

    # 下一次可以再跑。
    assert _run(database, _provider(), config, _job(paper_id)).outcome is GatewayOutcome.ACTIVE


def test_configuration_error_fails_run_and_raises(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    with pytest.raises(LlmConfigurationError):
        _run(
            database,
            _provider(LlmConfigurationError("測試：金鑰錯", status=401)),
            config,
            _job(paper_id),
        )

    assert _runs(db) == [("failed", "llm_configuration")]
    assert table_counts(db)["llm_call"] == 0


def test_unexpected_provider_error_fails_run_and_raises(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    class Broken:
        def complete(self, request: object) -> None:
            raise RuntimeError("測試：替身壞了")

    with pytest.raises(RuntimeError):
        _run(database, Broken(), config, _job(paper_id))

    assert _runs(db) == [("failed", "llm_gateway_error")]


# --- 晚到、sweep 判定失敗後才回來 -----------------------------------------------------------------


def test_late_result_after_abstract_changed_is_superseded(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    first = _run(database, _provider(), config, _job(paper_id))
    set_abstract(db, paper_id, "Second abstract text.")

    provider = GatedProvider(
        _provider(),
        gated=False,
        before_reply=lambda: set_abstract(db, paper_id, "Third abstract text."),
    )
    late = _run(database, provider, config, _job(paper_id))

    assert late.outcome is GatewayOutcome.SUPERSEDED
    rows = execute(db, "SELECT run_id, status FROM zh_summary ORDER BY created_at, id")
    assert rows == [(first.run_id, "active"), (late.run_id, "superseded")]
    assert _runs(db) == [("succeeded", None), ("succeeded", None)]


def test_result_after_sweep_expired_run_is_superseded(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    def sweep_meanwhile() -> None:
        execute(db, "UPDATE derivation_run SET created_at = now() - interval '1 hour'")
        assert expire_stale_runs(SYSTEM, database, timedelta(minutes=15)) == 1

    provider = GatedProvider(_provider(), gated=False, before_reply=sweep_meanwhile)
    result = _run(database, provider, config, _job(paper_id))

    assert result.outcome is GatewayOutcome.SUPERSEDED
    assert _runs(db) == [("failed", "llm_run_expired")]
    assert _headers(db) == [("superseded", None)]
    assert execute(db, "SELECT cost_usd FROM llm_call") == [(_expected_cost(),)]


# --- 評估 ----------------------------------------------------------------------------------------


def test_eval_run_ignores_should_run_and_writes_candidate(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    normal = _run(database, _provider(), config, _job(paper_id))
    eval_run_id = uuid.uuid4()

    evaluated = _run(database, _provider(), config, _job(paper_id), eval_run_id=eval_run_id)

    assert evaluated.outcome is GatewayOutcome.CANDIDATE
    rows = execute(db, "SELECT run_id, status FROM zh_summary ORDER BY created_at, id")
    assert rows == [(normal.run_id, "active"), (evaluated.run_id, "candidate")]
    assert execute(
        db, "SELECT eval_run_id FROM derivation_run WHERE id = %s", evaluated.run_id
    ) == [(eval_run_id,)]


def test_eval_results_do_not_block_normal_run(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    eval_run_id = uuid.uuid4()
    assert (
        _run(database, _provider(), config, _job(paper_id), eval_run_id=eval_run_id).outcome
        is GatewayOutcome.CANDIDATE
    )
    rejected_eval = _run(
        database,
        _provider(fake_completion("{}", usage=USAGE)),
        config,
        _job(paper_id),
        eval_run_id=eval_run_id,
    )
    assert rejected_eval.outcome is GatewayOutcome.REJECTED

    assert _run(database, _provider(), config, _job(paper_id)).outcome is GatewayOutcome.ACTIVE


# --- 結果寫入失敗 --------------------------------------------------------------------------------


def test_result_write_failure_keeps_ledger_and_fails_run(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    paper_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("測試：寫入失敗")

    monkeypatch.setattr(llm_gateway, "record_llm_result", broken)

    with pytest.raises(RuntimeError, match="寫入失敗"):
        _run(database, _provider(), config, _job(paper_id))

    assert _runs(db) == [("failed", "llm_gateway_error")]
    assert execute(db, "SELECT cost_usd FROM llm_call") == [(_expected_cost(),)]
    assert table_counts(db)["zh_summary"] == 0


# --- 其他：Batch 計價、權限、不支援的工作 ---------------------------------------------------------


def test_batch_call_mode_records_discounted_cost(
    db: TestDatabase, database: Database, prompts: Path, paper_id: uuid.UUID
) -> None:
    config = gateway_config(prompts, call_mode=CallMode.BATCH)

    result = _run(database, _provider(), config, _job(paper_id))

    assert result.cost_usd == _expected_cost(is_batch=True)
    assert execute(db, "SELECT is_batch, cost_usd FROM llm_call") == [
        (True, _expected_cost(is_batch=True))
    ]
    assert execute(db, "SELECT call_mode FROM derivation_run") == [("batch",)]


def test_member_cannot_run_llm_jobs(
    db: TestDatabase, database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    member = MemberViewer(member_id=uuid.uuid4(), is_admin=True)
    with pytest.raises(PermissionDeniedError):
        run_llm_job(
            member,
            database,
            FakeLlmProvider(),
            config,
            _job(paper_id),
            budget_allows=lambda _task: True,
        )
    with pytest.raises(PermissionDeniedError):
        expire_stale_runs(member, database, timedelta(minutes=15))
    assert set(table_counts(db).values()) == {0}


def test_task_without_header_table_is_refused(
    database: Database, config: GatewayConfig, paper_id: uuid.UUID
) -> None:
    job = LlmJob(paper_id, LlmTask.QA_ANSWER, InputKind.ABSTRACT, OutputLang.ZH_HANT)
    with pytest.raises(ValueError, match="表頭"):
        _run(database, FakeLlmProvider(), config, job)


def test_missing_paper_raises_lookup_error(database: Database, config: GatewayConfig) -> None:
    with pytest.raises(LookupError):
        _run(database, FakeLlmProvider(), config, _job(uuid.uuid4()))
