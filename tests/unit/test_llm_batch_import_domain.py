"""Batch 結果匯入的 domain 與替身（Story 2.11、ADR-0043）：`interpret_message`、批次狀態與結果行的
形狀、保留期與逾期、計數與要印的字、`poll_interval_minutes`、拒收報表的範圍與表格、
`run_checked_scheduled_job`、`FakeLlmProvider` 的查詢與下載。不碰資料庫、不連網路。

假的狀態與結果行不是真的錄的，形狀照官方文件。
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from radar.adapters.fake import FakeLlmProvider, fake_batch_status, fake_completion
from radar.adapters.fake.llm import FakeRepliesExhaustedError
from radar.config import load_llm_batch_config
from radar.domain.ai_outputs import LlmTask, ResultStatus, RunStatus
from radar.domain.budget import BUDGET_TIMEZONE
from radar.domain.llm import (
    BatchProcessingStatus,
    BatchRequestCounts,
    LlmBatchResult,
    LlmBatchResultKind,
    LlmBatchStatus,
    LlmConfigurationError,
    LlmConnectionError,
    LlmMalformedResponseError,
    LlmRefusalError,
    LlmServerError,
    LlmTruncatedError,
    interpret_message,
)
from radar.domain.llm_batches import (
    AI_DONE_AFTER,
    PROVIDER_PROCESSING_WINDOW,
    PROVIDER_RESULTS_RETENTION,
    BatchImportCounts,
    BatchItemOutcome,
    BatchPollRow,
    BatchSubmission,
    BatchSubmissionStatus,
    LlmBatchConfig,
    LlmBatchEstimate,
    LlmBatchPollResult,
    batch_overdue,
    is_finished,
    render_poll_row,
    render_poll_summary,
    render_queued_batches,
    render_wait_progress,
    results_expired,
    results_incomplete,
)
from radar.domain.run_reports import (
    RunOutcome,
    RunOutcomeRow,
    RunScope,
    parse_ids_file,
    parse_since,
    render_run_report,
    run_outcome,
)
from radar.services.scheduled import run_checked_scheduled_job

BATCH_ID = "msgbatch_01FAKEbatchIdForTests"
CREATED = datetime(2026, 11, 15, 4, 0, tzinfo=UTC)

# --- interpret_message ------------------------------------------------------------------------


def test_interpret_message_without_json_returns_text() -> None:
    result = interpret_message(fake_completion("純文字"), expects_json=False)

    assert result.text == "純文字"
    assert result.parsed is None


def test_interpret_message_with_json_parses_object() -> None:
    result = interpret_message(fake_completion('{"a": 1}', thinking=True), expects_json=True)

    assert result.parsed == {"a": 1}


@pytest.mark.parametrize(
    ("completion", "error"),
    [
        (fake_completion("x", stop_reason="refusal"), LlmRefusalError),
        (fake_completion("x", stop_reason="max_tokens"), LlmTruncatedError),
        (fake_completion("x", stop_reason=None), LlmMalformedResponseError),
        (fake_completion(None), LlmMalformedResponseError),
        (fake_completion("not json"), LlmMalformedResponseError),
        (fake_completion("[1]"), LlmMalformedResponseError),
    ],
)
def test_interpret_message_rejections(completion: object, error: type[Exception]) -> None:
    with pytest.raises(error) as caught:
        interpret_message(completion, expects_json=True)  # type: ignore[arg-type]

    assert caught.value.usage is not None  # type: ignore[attr-defined]


# --- 狀態與結果行 -----------------------------------------------------------------------------


def test_batch_status_needs_aware_times() -> None:
    with pytest.raises(ValueError, match="時區"):
        LlmBatchStatus(
            BATCH_ID,
            BatchProcessingStatus.ENDED,
            created_at=datetime(2026, 11, 15),
            expires_at=CREATED,
            ended_at=None,
            results_available=True,
        )


def test_only_succeeded_has_a_completion() -> None:
    with pytest.raises(ValueError, match="completion"):
        LlmBatchResult("a" * 32, LlmBatchResultKind.SUCCEEDED)
    with pytest.raises(ValueError, match="completion"):
        LlmBatchResult("a" * 32, LlmBatchResultKind.CANCELED, completion=fake_completion("x"))


def test_retention_and_processing_window_constants() -> None:
    assert timedelta(days=29) == PROVIDER_RESULTS_RETENTION
    assert timedelta(hours=24) == PROVIDER_PROCESSING_WINDOW
    assert timedelta(hours=24) == AI_DONE_AFTER


def test_results_expire_after_29_days() -> None:
    status = fake_batch_status(BATCH_ID, created_at=CREATED)

    assert not results_expired(status, CREATED + timedelta(days=29))
    assert results_expired(status, CREATED + timedelta(days=29, seconds=1))
    with pytest.raises(ValueError, match="時區"):
        results_expired(status, datetime(2026, 12, 20))


def test_overdue_only_while_not_ended() -> None:
    running = fake_batch_status(BATCH_ID, BatchProcessingStatus.IN_PROGRESS, created_at=CREATED)
    ended = fake_batch_status(BATCH_ID, created_at=CREATED)
    late = CREATED + timedelta(hours=25)

    assert not batch_overdue(running, CREATED + timedelta(hours=23))
    assert batch_overdue(running, late)
    assert not batch_overdue(ended, late)


# --- 計數與要印的字 ---------------------------------------------------------------------------


def test_counts_and_health() -> None:
    counts = BatchImportCounts()
    counts.add(BatchItemOutcome.ACTIVE)
    counts.add(BatchItemOutcome.ACTIVE)
    counts.add(BatchItemOutcome.ALREADY_DONE)
    counts.add(BatchItemOutcome.ERROR)
    row = BatchPollRow(uuid.uuid4(), BatchSubmissionStatus.IMPORTED, counts=counts)

    assert counts.imported == 3
    assert LlmBatchPollResult(rows=[row]).errors == 1
    assert not LlmBatchPollResult(rows=[row]).healthy
    assert not LlmBatchPollResult(needs_attention=1).healthy
    assert LlmBatchPollResult().healthy


def test_poll_row_and_summary_text() -> None:
    batch = uuid.UUID(int=1)
    counts = BatchImportCounts(missing=2, unknown_custom_ids=1)
    counts.add(BatchItemOutcome.REJECTED)
    row = BatchPollRow(
        batch,
        BatchSubmissionStatus.SUBMITTED,
        BatchProcessingStatus.IN_PROGRESS,
        counts,
        overdue=True,
        error="LlmServerError（HTTP 503）",
    )
    weekly = uuid.UUID(int=2)

    text = render_poll_row(row)
    summary = render_poll_summary(
        LlmBatchPollResult(rows=[row], needs_attention=1, ai_done=[weekly])
    )

    assert text.startswith(f"{batch}  submitted  供應商 in_progress")
    for part in ("rejected 1", "missing 2", "不認得的 custom_id 1", "逾期", "HTTP 503"):
        assert part in text
    assert summary[0] == "needs_attention：1 批"
    assert "radar batches resolve" in summary[1]
    assert summary[2] == f"每週更新 {weekly} → ai_done"
    assert render_poll_summary(LlmBatchPollResult())[0] == "沒有要查的批次（submitted／ended）"
    assert render_wait_progress(2, 1) == "已結束 2 批、還在跑 1 批"


@pytest.mark.parametrize(
    ("status", "finished"),
    [
        (BatchSubmissionStatus.IMPORTED, True),
        (BatchSubmissionStatus.FAILED, True),
        (BatchSubmissionStatus.ENDED, False),
        (BatchSubmissionStatus.NEEDS_ATTENTION, False),
    ],
)
def test_finished_statuses(status: BatchSubmissionStatus, finished: bool) -> None:
    assert is_finished(status) is finished


def test_poll_interval_has_a_default_and_bounds() -> None:
    base = {"weekly_planning_enabled": False, "max_requests_per_batch": 500}
    base["month_start_hold_hours"] = 8

    assert LlmBatchConfig.model_validate(base).poll_interval_minutes == 10
    assert (
        LlmBatchConfig.model_validate({**base, "poll_interval_minutes": 3}).poll_interval_minutes
        == 3
    )
    for bad in (0, 1441, "10", 1.5):
        with pytest.raises(ValidationError):
            LlmBatchConfig.model_validate({**base, "poll_interval_minutes": bad})


# --- 拒收報表 ---------------------------------------------------------------------------------


def test_scope_needs_something() -> None:
    with pytest.raises(ValueError, match="至少要給"):
        RunScope()
    with pytest.raises(ValueError, match="時區"):
        RunScope(since=datetime(2026, 11, 15))
    assert RunScope(paper_ids=()).paper_ids == ()


def test_ids_file_skips_blank_and_comment_lines() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    text = f"# 試跑的論文\n\n  {first}\n   # 註解\n{second}\n{first}\n"

    assert parse_ids_file(text) == (first, second)
    with pytest.raises(ValueError, match="第 2 行"):
        parse_ids_file(f"{first}\nnot-a-uuid\n")


def test_since_without_timezone_is_taipei_time() -> None:
    assert parse_since("2026-11-16") == datetime(2026, 11, 16, tzinfo=BUDGET_TIMEZONE)
    assert parse_since("2026-11-16T08:00+00:00") == datetime(2026, 11, 16, 8, tzinfo=UTC)
    with pytest.raises(ValueError, match="ISO 8601"):
        parse_since("last week")


@pytest.mark.parametrize(
    ("run_status", "header", "expected"),
    [
        (RunStatus.SUCCEEDED, ResultStatus.ACTIVE, RunOutcome.ACTIVE),
        (RunStatus.SUCCEEDED, ResultStatus.SUPERSEDED, RunOutcome.SUPERSEDED),
        (RunStatus.SUCCEEDED, ResultStatus.REJECTED, RunOutcome.REJECTED),
        (RunStatus.RUNNING, None, RunOutcome.RUNNING),
        (RunStatus.FAILED, None, RunOutcome.FAILED),
        (RunStatus.SUCCEEDED, None, RunOutcome.FAILED),
        (RunStatus.FAILED, ResultStatus.SUPERSEDED, RunOutcome.SUPERSEDED),
    ],
)
def test_run_outcome(
    run_status: RunStatus, header: ResultStatus | None, expected: RunOutcome
) -> None:
    assert run_outcome(run_status, header) is expected


def test_report_counts_by_task_and_reason() -> None:
    paper = uuid.uuid4()
    rows = [
        RunOutcomeRow(paper, LlmTask.ZH_SUMMARY, RunOutcome.ACTIVE),
        RunOutcomeRow(paper, LlmTask.CLASSIFY, RunOutcome.REJECTED, "llm_output_invalid"),
        RunOutcomeRow(uuid.uuid4(), LlmTask.CLASSIFY, RunOutcome.REJECTED, "llm_output_invalid"),
        RunOutcomeRow(paper, LlmTask.EXTRACT_MODEL, RunOutcome.FAILED, "batch_item_expired"),
    ]

    text = "\n".join(render_run_report(rows))

    for part in ("有效", "已取代", "拒收", "暫時失敗", "還在跑"):
        assert part in text
    assert "拒收依原因代碼（篇數）" in text
    assert "暫時失敗依原因代碼（篇數）" in text
    assert "llm_output_invalid" in text
    assert "batch_item_expired" in text
    assert render_run_report([]) == ["範圍內沒有 AI 工作的 run"]


# --- 有條件的心跳 -----------------------------------------------------------------------------


@pytest.mark.parametrize(("healthy", "sent"), [(True, ["poll-batches"]), (False, [])])
def test_checked_job_sends_heartbeat_only_when_healthy(healthy: bool, sent: list[str]) -> None:
    beats: list[str] = []

    result = run_checked_scheduled_job("poll-batches", lambda: 7, lambda _: healthy, beats.append)

    assert result == 7
    assert beats == sent


def test_checked_job_failure_sends_nothing() -> None:
    beats: list[str] = []

    def boom() -> int:
        raise RuntimeError("壞了")

    with pytest.raises(RuntimeError):
        run_checked_scheduled_job("poll-batches", boom, lambda _: True, beats.append)

    assert beats == []


# --- FakeLlmProvider --------------------------------------------------------------------------


def test_fake_status_queue_pops_until_the_last_one() -> None:
    provider = FakeLlmProvider()
    first = fake_batch_status(BATCH_ID, BatchProcessingStatus.IN_PROGRESS)
    last = fake_batch_status(BATCH_ID)
    provider.batch_statuses[BATCH_ID] = [first, LlmServerError("HTTP 503", status=503), last]

    assert provider.retrieve_batch(BATCH_ID) == first
    with pytest.raises(LlmServerError):
        provider.retrieve_batch(BATCH_ID)
    assert provider.retrieve_batch(BATCH_ID) == last
    assert provider.retrieve_batch(BATCH_ID) == last
    assert provider.retrieved == [BATCH_ID] * 4


def test_fake_unknown_batch_is_404_and_has_no_results() -> None:
    provider = FakeLlmProvider()

    with pytest.raises(LlmConfigurationError) as caught:
        provider.retrieve_batch("msgbatch_unknown")
    assert caught.value.status == 404
    with pytest.raises(FakeRepliesExhaustedError):
        provider.batch_results("msgbatch_unknown")


def test_fake_results_replay_and_raise_midway() -> None:
    provider = FakeLlmProvider()
    one = LlmBatchResult("a" * 32, LlmBatchResultKind.CANCELED)
    provider.batch_result_lines[BATCH_ID] = [one, LlmConnectionError("斷線")]

    stream = provider.batch_results(BATCH_ID)
    assert next(stream) == one
    with pytest.raises(LlmConnectionError):
        next(stream)
    assert next(provider.batch_results(BATCH_ID)) == one  # 每次從頭回
    assert provider.downloaded == [BATCH_ID, BATCH_ID]


def test_fake_status_defaults() -> None:
    running = fake_batch_status(BATCH_ID, BatchProcessingStatus.IN_PROGRESS)
    ended = fake_batch_status(BATCH_ID)

    assert running.expires_at == running.created_at + timedelta(hours=24)
    assert running.ended_at is None
    assert running.results_available is False
    assert ended.ended_at == ended.created_at + timedelta(hours=1)
    assert ended.results_available is True


# --- 審查後（Codex 主審查第 1～3 條） ----------------------------------------------------------


def test_request_counts_must_be_non_negative_integers() -> None:
    counts = BatchRequestCounts(processing=0, succeeded=2, errored=1, canceled=0, expired=1)

    assert counts.finished == 4
    with pytest.raises(ValueError, match="request_counts"):
        BatchRequestCounts(processing=0, succeeded=-1, errored=0, canceled=0, expired=0)
    with pytest.raises(ValueError, match="request_counts"):
        BatchRequestCounts(processing=0, succeeded=True, errored=0, canceled=0, expired=0)


def test_results_incomplete_compares_with_the_provider_counts() -> None:
    counted = fake_batch_status(
        BATCH_ID,
        request_counts=BatchRequestCounts(
            processing=0, succeeded=2, errored=0, canceled=1, expired=0
        ),
    )

    assert results_incomplete(counted, 2) == 3
    assert results_incomplete(counted, 3) is None
    assert results_incomplete(counted, 4) is None  # 重複的行不會讓它變成「沒讀完」
    assert results_incomplete(fake_batch_status(BATCH_ID), 0) is None  # 替身沒給計數
    with pytest.raises(ValueError, match="負數"):
        results_incomplete(counted, -1)


def test_stopped_poll_row_says_so() -> None:
    row = BatchPollRow(uuid.UUID(int=1), BatchSubmissionStatus.NEEDS_ATTENTION, stopped=True)

    assert "停止匯入" in render_poll_row(row)


def _queued(n: int, items: int, cost: str) -> BatchSubmission:
    return BatchSubmission(
        id=uuid.UUID(int=n),
        weekly_run_id=None,
        status=BatchSubmissionStatus.QUEUED,
        provider_batch_id=None,
        item_count=items,
        estimated_max_cost_usd=Decimal(cost),
        failure_reason=None,
    )


def test_queued_batches_are_part_of_the_confirmed_total() -> None:
    estimate = LlmBatchEstimate(
        by_task={LlmTask.CLASSIFY: 2}, estimated_max_cost_usd=Decimal("0.5")
    )

    lines = render_queued_batches([_queued(1, 3, "1.25"), _queued(2, 4, "0.25")], estimate)

    shown = "\n".join(lines)
    assert str(uuid.UUID(int=1)) in shown
    assert str(uuid.UUID(int=2)) in shown
    assert "已排好、還沒送出的批次：2 批" in shown
    assert lines[-1] == "這次會送出：9 筆（新排 2＋已排好 7），保守上限合計 2.0000 美元"
    assert render_queued_batches([], estimate) == [
        "",
        "這次會送出：2 筆，保守上限合計 0.5000 美元",
    ]


# --- 確認審查 D2 ------------------------------------------------------------------------------


def test_incomplete_results_attention_after_has_a_default_and_bounds() -> None:
    base: dict[str, object] = {
        "weekly_planning_enabled": False,
        "max_requests_per_batch": 500,
        "month_start_hold_hours": 8,
    }

    assert LlmBatchConfig.model_validate(base).incomplete_results_attention_after == 3
    assert (
        LlmBatchConfig.model_validate(
            {**base, "incomplete_results_attention_after": 1}
        ).incomplete_results_attention_after
        == 1
    )
    for bad in (0, 101, "3", 2.5, True):
        with pytest.raises(ValidationError):
            LlmBatchConfig.model_validate({**base, "incomplete_results_attention_after": bad})


def test_repo_config_sets_incomplete_results_attention_after_to_three() -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_llm_batch_config({"RADAR_CONFIG_DIR": str(root / "config")})

    assert config.incomplete_results_attention_after == 3
