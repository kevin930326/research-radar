"""Batch 送出（`submit_llm_batch`，Story 2.9、ADR-0041）對真的 PostgreSQL：狀態機、送出前核對、
錯誤分類、停在 `submitting` 的過期規則、慢一步的寫回、中斷後重跑不再呼叫 provider。

provider 是 `FakeLlmProvider`（batch ID 是編的，不是真的錄的，形狀照官方文件）；論文、摘要都是
測試自己編的。
"""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from structlog.testing import capture_logs

import radar.services.llm_batches as llm_batches
from radar.adapters.fake import FakeLlmProvider
from radar.domain.ai_outputs import LlmTask
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import LLM_BATCH_BUDGET_LOCK_KEY
from radar.domain.llm import (
    LlmConfigurationError,
    LlmConnectionError,
    LlmInvalidRequestError,
    LlmMalformedResponseError,
    LlmRateLimitedError,
    LlmServerError,
    LlmTimeoutError,
    NotCloudSafeError,
)
from radar.domain.llm_batches import (
    BatchSubmissionStatus,
    BatchSubmitUncertainError,
    LlmBatchSubmitResult,
    SubmitOutcome,
)
from radar.domain.viewer import SystemViewer
from radar.ports import LlmProvider
from radar.repo.db import Database
from radar.services.llm_batches import LlmBatchSetup, plan_llm_batches, submit_llm_batch
from tests.batch_support import (
    BATCH_ID,
    NOW,
    GatedBatchProvider,
    batch_setup,
    batch_status,
    items,
)
from tests.database import TestDatabase, migrate
from tests.llm_support import budget_config, create_paper, execute, insert_ledger, set_abstract

SYSTEM = SystemViewer()


class _Killed(BaseException):
    """模擬行程在送出途中被中斷（不是 `Exception`）。"""


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def setup() -> LlmBatchSetup:
    return batch_setup()


def _paper(database: Database, n: int) -> uuid.UUID:
    return create_paper(
        database,
        doi=f"10.1/submit{n}",
        openalex_id=f"W{n}",
        title=f"Collaboration platforms in remote teams, part {n}",
        abstract=f"We survey {n} remote teams about collaboration platforms.",
    )


def _planned(database: Database, setup: LlmBatchSetup, papers: int = 1) -> uuid.UUID:
    for n in range(1, papers + 1):
        _paper(database, n)
    result = plan_llm_batches(
        SYSTEM,
        database,
        setup,
        weekly_run_id=None,
        clock=lambda: NOW,
        enqueue=lambda *_: True,
    )
    [submission_id] = result.submission_ids
    return submission_id


def _submit(
    database: Database,
    provider: LlmProvider,
    setup: LlmBatchSetup,
    submission_id: uuid.UUID,
    **kwargs: Any,
) -> LlmBatchSubmitResult:
    kwargs.setdefault("clock", lambda: NOW)
    # provider 由 service 在確定要送時才要（Codex 主審查第 4 條改成傳建立函式）。
    return submit_llm_batch(SYSTEM, database, lambda: provider, setup, submission_id, **kwargs)


def _run_states(db: TestDatabase, submission_id: uuid.UUID) -> set[tuple[str, str | None]]:
    return {(row[3], row[5]) for row in items(db, submission_id)}


def test_submits_queued_batch_with_item_ids_as_custom_ids(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup, papers=2)
    provider = FakeLlmProvider(batch_replies=[BATCH_ID])

    with capture_logs() as logs:
        result = _submit(database, provider, setup, submission_id)

    assert result == LlmBatchSubmitResult(
        SubmitOutcome.SUBMITTED, BatchSubmissionStatus.SUBMITTED, provider_batch_id=BATCH_ID, sent=6
    )
    assert batch_status(db, submission_id) == ("submitted", BATCH_ID, None, 6, True)
    [sent] = provider.batch_requests
    assert sorted(request.custom_id for request in sent) == sorted(
        uuid.UUID(str(row[0])).hex for row in items(db, submission_id)
    )
    assert _run_states(db, submission_id) == {("running", None)}
    [logged] = [log for log in logs if log["event"] == "Batch 已送出"]
    assert logged["provider_batch_id"] == BATCH_ID
    assert logged["log_level"] == "info"


@pytest.mark.parametrize("status", ["submitted", "failed", "needs_attention"])
def test_done_or_stopped_batches_are_left_alone(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, status: str
) -> None:
    submission_id = _planned(database, setup)
    provider = FakeLlmProvider(batch_replies=[BATCH_ID])
    if status == "submitted":
        _submit(database, provider, setup, submission_id)
    else:
        execute(
            db,
            "UPDATE batch_submission SET status = %s, failure_reason = 'batch_submit_uncertain'"
            " WHERE id = %s",
            status,
            submission_id,
        )

    result = _submit(database, FakeLlmProvider(), setup, submission_id)

    assert result.outcome is SubmitOutcome.NOTHING_TO_DO
    assert result.status.value == status


def test_input_changed_runs_fail_and_the_rest_are_sent(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup, papers=2)
    [changed] = execute(db, "SELECT id FROM paper WHERE title LIKE '%%part 1'")[0]
    set_abstract(db, changed, "A different abstract that arrived after planning.")
    provider = FakeLlmProvider(batch_replies=[BATCH_ID])

    result = _submit(database, provider, setup, submission_id)

    assert (result.outcome, result.sent, result.input_changed) == (SubmitOutcome.SUBMITTED, 3, 3)
    assert batch_status(db, submission_id) == ("submitted", BATCH_ID, None, 3, True)
    states = execute(
        db,
        "SELECT r.paper_id, r.status, r.failure_reason FROM batch_item AS i"
        " JOIN derivation_run AS r ON r.id = i.run_id",
    )
    assert {(status, reason) for paper, status, reason in states if paper == changed} == {
        ("failed", "batch_input_changed")
    }
    assert {(status, reason) for paper, status, reason in states if paper != changed} == {
        ("running", None)
    }
    assert len(provider.batch_requests[0]) == 3


def test_all_inputs_changed_fails_the_batch_without_calling_the_provider(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)
    execute(db, "UPDATE paper_abstract SET content = 'Changed after planning.'")

    result = _submit(database, FakeLlmProvider(), setup, submission_id)

    assert result.outcome is SubmitOutcome.FAILED
    assert result.reason is ErrorCode.BATCH_INPUT_CHANGED
    assert batch_status(db, submission_id) == ("failed", None, "batch_input_changed", 3, False)
    assert _run_states(db, submission_id) == {("failed", "batch_input_changed")}


def test_runs_no_longer_running_are_skipped(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)
    [first] = items(db, submission_id)[:1]
    execute(
        db,
        "UPDATE derivation_run SET status = 'failed', failure_reason = 'llm_timeout',"
        " finished_at = now() WHERE id = %s",
        first[1],
    )
    provider = FakeLlmProvider(batch_replies=[BATCH_ID])

    result = _submit(database, provider, setup, submission_id)

    assert (result.sent, result.input_changed) == (2, 0)
    assert batch_status(db, submission_id)[3] == 2


def test_429_puts_the_batch_back_to_queued_without_raising(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)
    provider = FakeLlmProvider(
        batch_replies=[LlmRateLimitedError("HTTP 429", status=429), BATCH_ID]
    )

    first = _submit(database, provider, setup, submission_id)

    assert (first.outcome, first.status) == (SubmitOutcome.REQUEUED, BatchSubmissionStatus.QUEUED)
    assert batch_status(db, submission_id)[:3] == ("queued", None, None)
    assert _run_states(db, submission_id) == {("running", None)}

    second = _submit(database, provider, setup, submission_id)

    assert second.outcome is SubmitOutcome.SUBMITTED
    assert len(provider.batch_requests) == 2


def test_429_after_the_row_moved_reports_the_real_status(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)

    def sweep_marks_it() -> None:
        execute(
            db,
            "UPDATE batch_submission SET status = 'needs_attention',"
            " failure_reason = 'batch_submit_uncertain' WHERE id = %s",
            submission_id,
        )

    provider = GatedBatchProvider(
        [LlmRateLimitedError("HTTP 429", status=429)], during_call=sweep_marks_it
    )

    with capture_logs() as logs:
        result = _submit(database, provider, setup, submission_id)

    assert result == LlmBatchSubmitResult(
        SubmitOutcome.LOST_RACE,
        BatchSubmissionStatus.NEEDS_ATTENTION,
        reason=ErrorCode.LLM_RATE_LIMITED,
    )
    assert batch_status(db, submission_id)[0] == "needs_attention"
    [logged] = [log for log in logs if "429" in log["event"]]
    assert (logged["changed"], logged["status"]) == (False, "needs_attention")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (LlmInvalidRequestError("HTTP 400", status=400), "batch_submit_rejected"),
        (LlmInvalidRequestError("HTTP 413", status=413), "batch_submit_rejected"),
        (LlmConfigurationError("HTTP 401", status=401), "llm_configuration"),
        (LlmConfigurationError("HTTP 404", status=404), "llm_configuration"),
        (LlmTimeoutError("HTTP 408", status=408), "batch_submit_rejected"),
        (LlmServerError("HTTP 409", status=409), "batch_submit_rejected"),
        # 送出任何請求前就丟的：一定沒有建立（port 的約定）。
        (LlmConfigurationError("讀不到憑證"), "llm_configuration"),
        (NotCloudSafeError("LlmRequest.content 不是 CloudSafeText"), "cloud_text_not_registered"),
    ],
)
def test_definite_4xx_fails_batch_and_runs_and_reraises(
    db: TestDatabase,
    database: Database,
    setup: LlmBatchSetup,
    error: Exception,
    reason: str,
) -> None:
    submission_id = _planned(database, setup)
    provider = FakeLlmProvider(batch_replies=[error])

    with pytest.raises(type(error)) as caught:
        _submit(database, provider, setup, submission_id)

    assert caught.value is error
    assert batch_status(db, submission_id) == ("failed", None, reason, 3, False)
    assert _run_states(db, submission_id) == {("failed", reason)}


@pytest.mark.parametrize(
    "error",
    [
        LlmTimeoutError("逾時"),
        LlmConnectionError("連線錯誤"),
        LlmServerError("HTTP 500", status=500),
        LlmServerError("HTTP 529", status=529),
        LlmMalformedResponseError("回應格式不對"),
        RuntimeError("unexpected"),
        # 一般的 TypeError／ValueError 可能是送出後才丟的：不確定，不能因此重送。
        TypeError("unexpected"),
        ValueError("unexpected"),
    ],
)
def test_uncertain_errors_mark_needs_attention_and_never_resend(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, error: Exception
) -> None:
    submission_id = _planned(database, setup)
    provider = FakeLlmProvider(batch_replies=[error])

    with capture_logs() as logs, pytest.raises(BatchSubmitUncertainError) as caught:
        _submit(database, provider, setup, submission_id)

    assert caught.value.error_type == type(error).__name__
    assert caught.value.__cause__ is None
    assert batch_status(db, submission_id) == (
        "needs_attention",
        None,
        "batch_submit_uncertain",
        3,
        False,
    )
    # run 留著（2.11 處理），預留也留著。
    assert _run_states(db, submission_id) == {("running", None)}
    assert any(log["log_level"] == "error" for log in logs)

    again = _submit(database, provider, setup, submission_id)

    assert again.outcome is SubmitOutcome.NOTHING_TO_DO
    assert len(provider.batch_requests) == 1


def test_malformed_batch_id_is_treated_as_uncertain(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)

    with pytest.raises(BatchSubmitUncertainError):
        _submit(database, FakeLlmProvider(batch_replies=["bad id"]), setup, submission_id)

    assert batch_status(db, submission_id)[:3] == (
        "needs_attention",
        None,
        "batch_submit_uncertain",
    )


def test_malformed_response_with_batch_id_records_it_for_manual_check(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)
    error = LlmMalformedResponseError("回應格式不對", request_id="req_fake01", response_id=BATCH_ID)

    with capture_logs() as logs, pytest.raises(BatchSubmitUncertainError):
        _submit(database, FakeLlmProvider(batch_replies=[error]), setup, submission_id)

    assert batch_status(db, submission_id)[:3] == (
        "needs_attention",
        BATCH_ID,
        "batch_submit_uncertain",
    )
    [logged] = [log for log in logs if log["log_level"] == "error"]
    assert (logged["provider_batch_id"], logged["request_id"]) == (BATCH_ID, "req_fake01")
    assert logged["changed"] is True


def test_malformed_response_with_bad_batch_id_records_nothing(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)
    error = LlmMalformedResponseError("回應格式不對", response_id="bad id")

    with pytest.raises(BatchSubmitUncertainError):
        _submit(database, FakeLlmProvider(batch_replies=[error]), setup, submission_id)

    assert batch_status(db, submission_id)[:2] == ("needs_attention", None)


def test_month_start_hold_leaves_the_batch_queued_without_calling_the_provider(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)
    provider = FakeLlmProvider()
    # 台北 2026-12-01 07:59（窗口 8 小時，還在裡面）。
    held_at = datetime(2026, 11, 30, 23, 59, tzinfo=UTC)

    result = _submit(database, provider, setup, submission_id, clock=lambda: held_at)

    assert result == LlmBatchSubmitResult(
        SubmitOutcome.HELD,
        BatchSubmissionStatus.QUEUED,
        reason=ErrorCode.BATCH_MONTH_START_HOLD,
    )
    assert batch_status(db, submission_id)[:3] == ("queued", None, None)
    assert _run_states(db, submission_id) == {("running", None)}
    assert provider.batch_requests == []


def test_tripped_breaker_leaves_the_batch_queued_without_calling_the_provider(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)
    [paper_id] = execute(db, "SELECT id FROM paper")[0]
    insert_ledger(db, paper_id, LlmTask.QA_ANSWER, "50", NOW)
    provider = FakeLlmProvider()

    result = _submit(database, provider, setup, submission_id)

    assert result == LlmBatchSubmitResult(
        SubmitOutcome.HELD,
        BatchSubmissionStatus.QUEUED,
        reason=ErrorCode.BATCH_BUDGET_TRIPPED,
    )
    assert batch_status(db, submission_id)[:3] == ("queued", None, None)
    assert _run_states(db, submission_id) == {("running", None)}
    assert provider.batch_requests == []


def test_paper_excluded_after_planning_is_not_sent(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup, papers=2)
    [excluded] = execute(db, "SELECT id FROM paper WHERE title LIKE '%%part 1'")[0]
    execute(db, "UPDATE paper SET excluded_reason = 'editors_comments' WHERE id = %s", excluded)
    provider = FakeLlmProvider(batch_replies=[BATCH_ID])

    result = _submit(database, provider, setup, submission_id)

    assert (result.outcome, result.sent, result.input_changed) == (SubmitOutcome.SUBMITTED, 3, 3)
    states = execute(
        db,
        "SELECT r.paper_id, r.status, r.failure_reason FROM batch_item AS i"
        " JOIN derivation_run AS r ON r.id = i.run_id",
    )
    assert {(status, reason) for paper, status, reason in states if paper == excluded} == {
        ("failed", "batch_input_changed")
    }
    assert len(provider.batch_requests[0]) == 3


def test_submitting_without_id_is_left_alone_until_stale_then_needs_attention(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)
    execute(db, "UPDATE batch_submission SET status = 'submitting' WHERE id = %s", submission_id)
    provider = FakeLlmProvider()

    fresh = _submit(database, provider, setup, submission_id)

    assert fresh.outcome is SubmitOutcome.NOT_STALE
    assert batch_status(db, submission_id)[0] == "submitting"

    execute(
        db,
        "UPDATE batch_submission SET updated_at = now() - interval '31 minutes' WHERE id = %s",
        submission_id,
    )
    stale = _submit(database, provider, setup, submission_id)

    assert stale.outcome is SubmitOutcome.NEEDS_ATTENTION
    assert batch_status(db, submission_id)[:3] == (
        "needs_attention",
        None,
        "batch_submit_uncertain",
    )
    assert provider.batch_requests == []


def test_late_writer_only_records_the_batch_id_on_needs_attention(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)

    def sweep_marks_it() -> None:
        # 送出太久，sweep 補排的送出工作已經把它標成 needs_attention。
        execute(
            db,
            "UPDATE batch_submission SET status = 'needs_attention',"
            " failure_reason = 'batch_submit_uncertain' WHERE id = %s",
            submission_id,
        )

    provider = GatedBatchProvider(during_call=sweep_marks_it)

    with capture_logs() as logs:
        result = _submit(database, provider, setup, submission_id)

    assert result.outcome is SubmitOutcome.NEEDS_ATTENTION
    assert result.provider_batch_id == BATCH_ID
    assert batch_status(db, submission_id) == (
        "needs_attention",
        BATCH_ID,
        "batch_submit_uncertain",
        3,
        False,
    )
    events = [log["event"] for log in logs]
    assert events.index("Batch 已送出") < next(
        i for i, log in enumerate(logs) if log["log_level"] == "error"
    )


def test_interrupted_after_id_before_write_back_never_resends(
    db: TestDatabase,
    database: Database,
    setup: LlmBatchSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submission_id = _planned(database, setup)
    provider = FakeLlmProvider(batch_replies=[BATCH_ID])

    def broken_write_back(*_args: object) -> bool:
        raise RuntimeError("database went away")

    monkeypatch.setattr(llm_batches, "update_batch_submission_submitted", broken_write_back)
    with capture_logs() as logs, pytest.raises(RuntimeError, match="database went away"):
        _submit(database, provider, setup, submission_id)
    monkeypatch.undo()

    # ID 已經先記在日誌裡（人工可以照著查），列停在 submitting。
    [logged] = [log for log in logs if log["event"] == "Batch 已送出"]
    assert logged["provider_batch_id"] == BATCH_ID
    assert batch_status(db, submission_id)[:2] == ("submitting", None)

    again = _submit(database, provider, setup, submission_id)
    assert again.outcome is SubmitOutcome.NOT_STALE

    execute(
        db,
        "UPDATE batch_submission SET updated_at = now() - interval '31 minutes' WHERE id = %s",
        submission_id,
    )
    stale = _submit(database, provider, setup, submission_id)

    assert stale.outcome is SubmitOutcome.NEEDS_ATTENTION
    assert len(provider.batch_requests) == 1


def test_interrupted_after_submitting_before_id_never_resends(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)
    provider = FakeLlmProvider(batch_replies=[_Killed()])

    with pytest.raises(_Killed):
        _submit(database, provider, setup, submission_id)

    assert batch_status(db, submission_id)[:2] == ("submitting", None)
    again = _submit(database, provider, setup, submission_id, stale_after=timedelta(hours=1))
    # 替身沒有準備第二個回覆：再呼叫就會丟 FakeRepliesExhaustedError。
    assert again.outcome is SubmitOutcome.NOT_STALE
    assert len(provider.batch_requests) == 1


def test_unknown_batch_raises_lookup_error(database: Database, setup: LlmBatchSetup) -> None:
    with pytest.raises(LookupError):
        _submit(database, FakeLlmProvider(), setup, uuid.uuid4())


def test_errors_and_logs_carry_no_document_text_or_key(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    create_paper(
        database,
        doi="10.1/secret",
        openalex_id="W99",
        title="Secret title",
        abstract="SECRET-ABSTRACT-MARKER about collaboration.",
    )
    result = plan_llm_batches(
        SYSTEM, database, setup, weekly_run_id=None, clock=lambda: NOW, enqueue=lambda *_: True
    )
    [submission_id] = result.submission_ids

    with capture_logs() as logs, pytest.raises(BatchSubmitUncertainError) as caught:
        _submit(
            database, FakeLlmProvider(batch_replies=[LlmTimeoutError("逾時")]), setup, submission_id
        )

    assert "SECRET-ABSTRACT-MARKER" not in str(logs) + str(caught.value)


# --- Codex 主審查的修正 -----------------------------------------------------------------------

TAIPEI = ZoneInfo("Asia/Taipei")


def _no_provider() -> LlmProvider:
    raise AssertionError("這條路不該建 provider")


def _reserved(db: TestDatabase) -> Decimal:
    value = execute(db, "SELECT sum(estimated_max_cost_usd) FROM batch_item")[0][0]
    assert isinstance(value, Decimal)
    return value


# 第 1 條：送出前在預算鎖裡確認「帳本＋全部預留」。


def test_spending_after_planning_that_overcommits_the_breaker_holds_the_batch(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)
    [paper_id] = execute(db, "SELECT id FROM paper")[0]
    # 規劃之後互動又花了：帳本還沒到熔斷線，但帳本＋這批的預留超過 50。
    insert_ledger(db, paper_id, LlmTask.QA_ANSWER, str(50 - _reserved(db) + Decimal("1e-6")), NOW)

    with capture_logs() as logs:
        result = submit_llm_batch(
            SYSTEM, database, _no_provider, setup, submission_id, clock=lambda: NOW
        )

    assert result == LlmBatchSubmitResult(
        SubmitOutcome.HELD,
        BatchSubmissionStatus.QUEUED,
        reason=ErrorCode.BATCH_BUDGET_EXCEEDED,
    )
    assert batch_status(db, submission_id)[:3] == ("queued", None, None)
    assert _run_states(db, submission_id) == {("running", None)}
    assert [log["reason"] for log in logs if log["event"].startswith("Batch 送出延後")] == [
        "batch_budget_exceeded"
    ]


def test_batch_that_exactly_fills_the_breaker_is_still_sent(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)
    [paper_id] = execute(db, "SELECT id FROM paper")[0]
    # 帳本＋預留剛好等於熔斷線：規劃時就是這樣排的，不能擋住自己。
    insert_ledger(db, paper_id, LlmTask.QA_ANSWER, str(50 - _reserved(db)), NOW)
    provider = FakeLlmProvider(batch_replies=[BATCH_ID])

    result = _submit(database, provider, setup, submission_id)

    assert result.outcome is SubmitOutcome.SUBMITTED
    assert len(provider.batch_requests) == 1


def test_spending_after_planning_that_overcommits_a_task_budget_holds_the_batch(
    db: TestDatabase, database: Database
) -> None:
    setup = batch_setup(budget=budget_config(zh_summary="1"))
    submission_id = _planned(database, setup)
    [paper_id] = execute(db, "SELECT id FROM paper")[0]
    [(zh_reserved,)] = execute(
        db,
        "SELECT i.estimated_max_cost_usd FROM batch_item AS i"
        " JOIN derivation_run AS r ON r.id = i.run_id WHERE r.task = 'zh_summary'",
    )
    insert_ledger(db, paper_id, LlmTask.ZH_SUMMARY, str(1 - zh_reserved + Decimal("1e-6")), NOW)

    result = submit_llm_batch(
        SYSTEM, database, _no_provider, setup, submission_id, clock=lambda: NOW
    )

    assert result.reason is ErrorCode.BATCH_BUDGET_EXCEEDED
    assert batch_status(db, submission_id)[0] == "queued"


def test_overcommit_check_runs_under_the_batch_budget_lock(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = _planned(database, setup)
    taken: list[int] = []
    original = Database.transaction_lock

    def record(connection: Any, key: int) -> None:
        taken.append(key)
        original(connection, key)

    monkeypatch.setattr(Database, "transaction_lock", staticmethod(record))

    result = _submit(database, FakeLlmProvider(batch_replies=[BATCH_ID]), setup, submission_id)

    assert result.outcome is SubmitOutcome.SUBMITTED
    assert taken == [LLM_BATCH_BUDGET_LOCK_KEY]


# 第 2 條：查核途中跨過台北午夜。


def test_check_that_crosses_midnight_into_the_hold_window_does_not_send(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = _planned(database, setup)
    now = [datetime(2026, 11, 30, 23, 59, tzinfo=TAIPEI)]
    original = llm_batches._check_items

    def slow_check(*args: Any) -> Any:
        checked = original(*args)
        now[0] = datetime(2026, 12, 1, 0, 30, tzinfo=TAIPEI)  # 重建請求花了 31 分鐘
        return checked

    monkeypatch.setattr(llm_batches, "_check_items", slow_check)

    result = submit_llm_batch(
        SYSTEM, database, _no_provider, setup, submission_id, clock=lambda: now[0]
    )

    assert result == LlmBatchSubmitResult(
        SubmitOutcome.HELD,
        BatchSubmissionStatus.QUEUED,
        reason=ErrorCode.BATCH_MONTH_START_HOLD,
    )
    assert batch_status(db, submission_id)[:3] == ("queued", None, None)
    assert _run_states(db, submission_id) == {("running", None)}


# 第 3 條：條件式更新沒套用時，連線池只有一條也重讀得到狀態。


@pytest.mark.parametrize("all_changed", [False, True])
def test_lost_race_with_a_single_connection_pool_reports_the_real_status(
    db: TestDatabase,
    database: Database,
    setup: LlmBatchSetup,
    monkeypatch: pytest.MonkeyPatch,
    all_changed: bool,
) -> None:
    submission_id = _planned(database, setup)
    single = Database(db.app_url, connect_timeout=2, pool_size=1)
    original = llm_batches._check_items

    def check_then_lose(*args: Any) -> Any:
        checked = original(*args)
        # 查核途中別的送出者已經把這批改走（用另一條連線）。
        execute(
            db,
            "UPDATE batch_submission SET status = 'failed',"
            " failure_reason = 'batch_submit_rejected' WHERE id = %s",
            submission_id,
        )
        if not all_changed:
            return checked
        return dataclasses.replace(
            checked,
            requests=[],
            sent_run_ids=[],
            sent_tasks=frozenset(),
            changed=[(run_id, ErrorCode.BATCH_INPUT_CHANGED) for run_id in checked.sent_run_ids],
        )

    monkeypatch.setattr(llm_batches, "_check_items", check_then_lose)

    result = submit_llm_batch(SYSTEM, single, _no_provider, setup, submission_id, clock=lambda: NOW)

    assert result == LlmBatchSubmitResult(SubmitOutcome.LOST_RACE, BatchSubmissionStatus.FAILED)
    # 搶輸的一方不改 run。
    assert _run_states(db, submission_id) == {("running", None)}


# 第 4 條：只碰資料庫的路不建 provider。


def test_db_only_paths_never_build_the_provider(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    held = _planned(database, setup)

    def bare(status: str, extra: str = "") -> uuid.UUID:
        [(submission_id,)] = execute(
            db,
            "INSERT INTO batch_submission (status, item_count, estimated_max_cost_usd,"
            f" failure_reason, updated_at) VALUES (%s, 0, 0, %s, now(){extra}) RETURNING id",
            status,
            "batch_submit_rejected" if status == "failed" else None,
        )
        return uuid.UUID(str(submission_id))

    stale = bare("submitting", " - interval '31 minutes'")
    done = bare("failed")
    hold_at = datetime(2026, 12, 1, 0, 30, tzinfo=TAIPEI)

    def submit(submission_id: uuid.UUID, at: datetime = NOW) -> SubmitOutcome:
        return submit_llm_batch(
            SYSTEM, database, _no_provider, setup, submission_id, clock=lambda: at
        ).outcome

    assert submit(held, hold_at) is SubmitOutcome.HELD
    assert submit(stale) is SubmitOutcome.NEEDS_ATTENTION
    assert submit(done) is SubmitOutcome.NOTHING_TO_DO
    assert batch_status(db, stale)[:3] == ("needs_attention", None, "batch_submit_uncertain")


def test_provider_setup_error_keeps_the_batch_queued(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _planned(database, setup)

    def missing_key() -> LlmProvider:
        raise RuntimeError("缺金鑰")

    with pytest.raises(RuntimeError, match="缺金鑰"):
        submit_llm_batch(SYSTEM, database, missing_key, setup, submission_id, clock=lambda: NOW)

    assert batch_status(db, submission_id)[:3] == ("queued", None, None)
    assert _run_states(db, submission_id) == {("running", None)}
