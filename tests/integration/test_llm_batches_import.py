"""Batch 結果匯入（`poll_llm_batches`、`import_llm_batch_result`，Story 2.11、ADR-0043）對真的
PostgreSQL：spec I/O 矩陣的每一列、匯入兩次、中途中斷、兩個 poller 同時匯入、`needs_attention` 的
AI 區塊、手動規劃與預估。

provider 是 `FakeLlmProvider`：假的批次狀態與結果行不是真的錄的，形狀照官方文件。論文、摘要、模型
回覆都是測試自己編的。
"""

import dataclasses
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

import radar.services.ai_results as ai_results
import radar.services.llm_gateway as llm_gateway
import radar.services.prompt_loader as prompt_loader
from radar.adapters.fake import FakeLlmProvider, fake_batch_status, fake_completion
from radar.domain.ai_blocks import AiBlockSettings, AiBlockState, AiProduct
from radar.domain.ai_outputs import LlmTask
from radar.domain.errors import ErrorCode
from radar.domain.llm import (
    BatchProcessingStatus,
    BatchRequestCounts,
    LlmBatchResult,
    LlmBatchResultKind,
    LlmConnectionError,
    LlmServerError,
    LlmUsage,
)
from radar.domain.llm_batches import (
    BatchItemOutcome,
    BatchItemRun,
    BatchSubmissionStatus,
    PlanStop,
    ResolveOutcome,
)
from radar.domain.pricing import llm_call_cost
from radar.domain.prompts import PromptIntegrityError
from radar.repo.ai_blocks import find_ai_block
from radar.repo.db import Database
from radar.repo.llm_batches import update_batch_submission_ended
from radar.services.llm_batches import (
    LlmBatchSetup,
    estimate_llm_batches,
    llm_job,
    plan_llm_batches,
    resolve_llm_batch,
)
from radar.services.llm_gateway import import_llm_batch_result
from tests.batch_import_support import (
    POLL_AT,
    SYSTEM,
    banking_paper,
    batch_items,
    by_task,
    headers,
    ledger,
    line,
    poll,
    provider_with,
    run_states,
    submission_row,
    submitted_batch,
    succeeded,
)
from tests.batch_support import (
    BATCH_ID,
    NOW,
    WAIT,
    batch_setup,
    insert_pending_run,
    item_cost,
    prepared,
)
from tests.database import TestDatabase, migrate
from tests.llm_support import create_paper, execute, set_abstract
from tests.trial_support import BANKING_ABSTRACT, BANKING_TITLE, REPLIES, reply

OTHER_BATCH_ID = "msgbatch_01FAKEotherBatchIdForTests"


class _Killed(BaseException):
    """模擬行程在匯入途中被中斷（不是 `Exception`）。"""


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def setup() -> LlmBatchSetup:
    # repo 的設定關著每週規劃（Kevin 決定）：這裡用手動規劃，和設定無關。
    return batch_setup(enabled=False)


# --- 正常結束 ---------------------------------------------------------------------------------


def test_ended_batch_is_imported_with_half_price_ledger_and_active_headers(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)
    provider = provider_with([succeeded(item) for item in found.values()])

    with capture_logs() as logs:
        result = poll(database, provider, setup)

    [row] = result.rows
    assert row.status is BatchSubmissionStatus.IMPORTED
    assert row.provider_status is BatchProcessingStatus.ENDED
    assert row.counts.count(BatchItemOutcome.ACTIVE) == 3
    assert result.healthy
    assert submission_row(db, submission_id) == ("imported", BATCH_ID, None, True, True)
    run_ids = [item.run_id for item in found.values()]
    assert set(run_states(db, run_ids).values()) == {("succeeded", None)}
    assert headers(db) == {run_id: ("active", None) for run_id in run_ids}
    # 每項一筆 Batch 帳，五折價。
    usage = reply(REPLIES[LlmTask.ZH_SUMMARY]).usage
    expected = llm_call_cost(
        setup.gateway.models, found[LlmTask.ZH_SUMMARY].key.model, usage, is_batch=True
    ).cost_usd
    assert sorted(ledger(db)) == sorted((run_id, True, expected, None) for run_id in run_ids)
    # 帳的時間是匯入的時間（送出之後）。
    [(late,)] = execute(
        db,
        "SELECT bool_and(c.created_at >= s.submitted_at) FROM llm_call AS c"
        " CROSS JOIN batch_submission AS s",
    )
    assert late is True
    # 日誌不帶論文內容與模型輸出。
    dumped = repr(logs)
    for secret in (BANKING_TITLE, BANKING_ABSTRACT, REPLIES[LlmTask.ZH_SUMMARY]["method"]):
        assert secret not in dumped


def test_partial_failures_follow_each_result_type(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup, papers=(1, 2))
    found = batch_items(database, submission_id)
    first = {item.key.task: item for item in found if item.key.paper_id == found[0].key.paper_id}
    second = {item.key.task: item for item in found if item.key.paper_id != found[0].key.paper_id}
    lines = [
        succeeded(first[LlmTask.ZH_SUMMARY]),
        line(
            first[LlmTask.CLASSIFY],
            LlmBatchResultKind.ERRORED,
            error_type="invalid_request_error",
        ),
        line(first[LlmTask.EXTRACT_MODEL], LlmBatchResultKind.ERRORED, error_type="api_error"),
        line(second[LlmTask.ZH_SUMMARY], LlmBatchResultKind.CANCELED),
        line(second[LlmTask.CLASSIFY], LlmBatchResultKind.EXPIRED),
        line(second[LlmTask.EXTRACT_MODEL], LlmBatchResultKind.UNREADABLE),
    ]

    result = poll(database, provider_with(lines), setup)

    [row] = result.rows
    assert row.status is BatchSubmissionStatus.IMPORTED
    assert row.counts.outcomes == {
        BatchItemOutcome.ACTIVE: 1,
        BatchItemOutcome.REJECTED: 1,
        BatchItemOutcome.FAILED: 4,
    }
    states = run_states(db, [item.run_id for item in found])
    assert states[first[LlmTask.ZH_SUMMARY].run_id] == ("succeeded", None)
    assert states[first[LlmTask.CLASSIFY].run_id] == ("succeeded", None)
    assert states[first[LlmTask.EXTRACT_MODEL].run_id] == ("failed", "batch_item_errored")
    assert states[second[LlmTask.ZH_SUMMARY].run_id] == ("failed", "batch_item_canceled")
    assert states[second[LlmTask.CLASSIFY].run_id] == ("failed", "batch_item_expired")
    assert states[second[LlmTask.EXTRACT_MODEL].run_id] == ("failed", "batch_result_unreadable")
    assert headers(db) == {
        first[LlmTask.ZH_SUMMARY].run_id: ("active", None),
        first[LlmTask.CLASSIFY].run_id: ("rejected", "llm_invalid_request"),
    }
    # 只有 succeeded 的那一項計費。
    assert [row[0] for row in ledger(db)] == [first[LlmTask.ZH_SUMMARY].run_id]


def test_rejected_outputs_are_charged_and_stored_as_rejected(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)
    usage = LlmUsage(input_tokens=900, output_tokens=50)
    lines = [
        succeeded(
            found[LlmTask.ZH_SUMMARY],
            fake_completion("不回答", stop_reason="refusal", usage=usage),
        ),
        succeeded(found[LlmTask.CLASSIFY], fake_completion('{"topics"', stop_reason="max_tokens")),
        # JSON 物件，但不符合 schema（Pydantic 不過）。
        succeeded(found[LlmTask.EXTRACT_MODEL], reply({"unexpected": True})),
    ]

    poll(database, provider_with(lines), setup)

    assert headers(db) == {
        found[LlmTask.ZH_SUMMARY].run_id: ("rejected", "llm_refusal"),
        found[LlmTask.CLASSIFY].run_id: ("rejected", "llm_truncated"),
        found[LlmTask.EXTRACT_MODEL].run_id: ("rejected", "llm_output_invalid"),
    }
    reasons = {row[0]: row[3] for row in ledger(db)}
    assert reasons == {
        found[LlmTask.ZH_SUMMARY].run_id: "llm_refusal",
        found[LlmTask.CLASSIFY].run_id: "llm_truncated",
        # 文字存下來了（驗證是之後的事），所以帳上沒有拒收原因。
        found[LlmTask.EXTRACT_MODEL].run_id: None,
    }


def test_malformed_message_is_rejected_and_charged_only_with_usage(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)
    lines = [
        line(
            found[LlmTask.ZH_SUMMARY],
            LlmBatchResultKind.MALFORMED,
            usage=LlmUsage(input_tokens=900, output_tokens=10),
        ),
        line(found[LlmTask.CLASSIFY], LlmBatchResultKind.MALFORMED),
        succeeded(found[LlmTask.EXTRACT_MODEL]),
    ]

    poll(database, provider_with(lines), setup)

    assert headers(db)[found[LlmTask.ZH_SUMMARY].run_id] == ("rejected", "llm_malformed_response")
    assert headers(db)[found[LlmTask.CLASSIFY].run_id] == ("rejected", "llm_malformed_response")
    charged = {row[0] for row in ledger(db)}
    assert found[LlmTask.ZH_SUMMARY].run_id in charged
    assert found[LlmTask.CLASSIFY].run_id not in charged


# --- 還在跑、拿不到結果 -----------------------------------------------------------------------


@pytest.mark.parametrize("processing", [BatchProcessingStatus.IN_PROGRESS, "canceling"])
def test_running_batch_is_left_alone_and_marked_overdue_after_expiry(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, processing: str
) -> None:
    submission_id = submitted_batch(database, setup)
    status = fake_batch_status(BATCH_ID, BatchProcessingStatus(processing))
    provider = provider_with([], status=status)

    early = poll(database, provider, setup, now=NOW + timedelta(hours=3))
    with capture_logs() as logs:
        late = poll(database, provider, setup, now=status.expires_at + timedelta(minutes=1))

    assert [row.overdue for row in early.rows + late.rows] == [False, True]
    assert {row.status for row in early.rows + late.rows} == {BatchSubmissionStatus.SUBMITTED}
    assert submission_row(db, submission_id)[0] == "submitted"
    assert provider.downloaded == []
    assert [log["log_level"] for log in logs if "處理期限" in log["event"]] == ["warning"]


@pytest.mark.parametrize("reason", ["expired", "no_results_url"])
def test_results_out_of_reach_release_running_runs(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, reason: str
) -> None:
    submission_id = submitted_batch(database, setup)
    if reason == "expired":
        status = fake_batch_status(BATCH_ID)
        now = status.created_at + timedelta(days=29, minutes=1)
    else:
        status = fake_batch_status(BATCH_ID, results_available=False)
        now = POLL_AT
    provider = provider_with([], status=status)

    result = poll(database, provider, setup, now=now)

    assert result.rows[0].counts.unavailable == 3
    assert provider.downloaded == []
    assert submission_row(db, submission_id) == ("imported", BATCH_ID, None, True, True)
    found = batch_items(database, submission_id)
    assert set(run_states(db, [item.run_id for item in found]).values()) == {
        ("failed", "batch_results_unavailable")
    }
    assert ledger(db) == []


# --- 匯入兩次、中途中斷、並行 ----------------------------------------------------------------


def test_duplicate_lines_and_second_import_write_nothing_new(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)
    zh = found[LlmTask.ZH_SUMMARY]
    lines = [succeeded(item) for item in found.values()] + [succeeded(zh)]

    result = poll(database, provider_with(lines), setup)
    before = (ledger(db), headers(db))
    again = import_llm_batch_result(
        SYSTEM,
        database,
        setup.gateway,
        llm_job(setup, zh.key.paper_id, zh.key.task, zh.input_kind),
        zh,
        succeeded(zh),
    )
    repoll = poll(database, provider_with(lines), setup)

    assert result.rows[0].counts.count(BatchItemOutcome.ALREADY_DONE) == 1
    assert again is BatchItemOutcome.ALREADY_DONE
    assert repoll.rows == []  # 已經 imported，不再查
    assert (ledger(db), headers(db)) == before
    assert len(before[0]) == len(before[1]) == 3


def test_interrupted_after_ledger_resumes_without_charging_again(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)
    lines = [succeeded(item) for item in found.values()]
    real = ai_results.record_batch_llm_result
    calls = {"n": 0}

    def killed_once(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Killed  # 記完第二項的帳、寫表頭前被中斷
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(llm_gateway, "record_batch_llm_result", killed_once)
    with pytest.raises(_Killed):
        poll(database, provider_with(lines), setup)

    assert submission_row(db, submission_id)[0] == "ended"
    assert len(ledger(db)) == 2
    assert len(headers(db)) == 1
    # 被中斷的那一項 run 還是 running（重跑接著做）。
    states = run_states(db, [item.run_id for item in found.values()])
    assert sorted(state for state, _ in states.values()) == ["running", "running", "succeeded"]

    monkeypatch.setattr(llm_gateway, "record_batch_llm_result", real)
    result = poll(database, provider_with(lines), setup)

    assert result.rows[0].status is BatchSubmissionStatus.IMPORTED
    assert len(ledger(db)) == 3  # 第二項的帳沒有重記
    assert set(headers(db).values()) == {("active", None)}
    assert len(headers(db)) == 3


def test_connection_lost_while_downloading_keeps_batch_ended(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup)
    found = list(by_task(database, submission_id).values())
    broken: list[LlmBatchResult | BaseException] = [
        succeeded(found[0]),
        LlmConnectionError("連線中斷"),
    ]

    first = poll(database, provider_with(broken), setup)

    assert first.rows[0].error == "LlmConnectionError"
    assert first.rows[0].status is BatchSubmissionStatus.ENDED
    assert not first.healthy
    assert len(ledger(db)) == 1

    second = poll(database, provider_with([succeeded(item) for item in found]), setup)

    assert second.rows[0].status is BatchSubmissionStatus.IMPORTED
    assert second.rows[0].counts.count(BatchItemOutcome.ALREADY_DONE) == 1
    assert len(ledger(db)) == 3
    assert len(headers(db)) == 3


def _interrupt_second_item(
    database: Database, setup: LlmBatchSetup, monkeypatch: pytest.MonkeyPatch
) -> tuple[uuid.UUID, list[BatchItemRun]]:
    """匯入時第二項記完帳、寫表頭前被中斷：第一項 active、第二項有帳但 run 還 running、第三項沒動；
    批次留 `ended`。回批次 id 與照結果行順序的三個 item。"""
    submission_id = submitted_batch(database, setup)
    items = list(by_task(database, submission_id).values())
    real = ai_results.record_batch_llm_result
    calls = {"n": 0}

    def killed_on_second(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Killed
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(llm_gateway, "record_batch_llm_result", killed_on_second)
    with pytest.raises(_Killed):
        poll(database, provider_with([succeeded(item) for item in items]), setup)
    monkeypatch.setattr(llm_gateway, "record_batch_llm_result", real)
    return submission_id, items


def test_saved_output_stays_resumable_when_results_become_unavailable(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id, items = _interrupt_second_item(database, setup, monkeypatch)
    gone = provider_with([], status=fake_batch_status(BATCH_ID, results_available=False))

    result = poll(database, gone, setup)

    assert result.rows[0].counts.unavailable == 2
    states = run_states(db, [item.run_id for item in items])
    assert states[items[1].run_id] == ("failed", ErrorCode.LLM_RESULT_PENDING.value)
    assert states[items[2].run_id] == ("failed", "batch_results_unavailable")
    assert len(ledger(db)) == 2
    assert submission_row(db, submission_id)[0] == "imported"


def test_not_found_after_interrupted_import_needs_attention_and_fail_keeps_saved_output(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id, items = _interrupt_second_item(database, setup, monkeypatch)
    assert submission_row(db, submission_id)[0] == "ended"

    result = poll(database, FakeLlmProvider(), setup)  # 沒準備狀態 → 404

    assert result.rows[0].status is BatchSubmissionStatus.NEEDS_ATTENTION
    assert result.rows[0].error is None
    assert submission_row(db, submission_id)[:3] == (
        "needs_attention",
        BATCH_ID,
        "batch_provider_not_found",
    )

    resolved = resolve_llm_batch(SYSTEM, database, submission_id, fail=True)

    assert resolved.outcome is ResolveOutcome.FAILED
    assert resolved.released_runs == 2
    states = run_states(db, [item.run_id for item in items])
    assert states[items[0].run_id] == ("succeeded", None)
    assert states[items[1].run_id] == ("failed", ErrorCode.LLM_RESULT_PENDING.value)
    assert states[items[2].run_id] == ("failed", "batch_resolved_failed")
    assert len(ledger(db)) == 2


def test_header_write_failure_marks_run_pending_and_is_not_charged_again(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = submitted_batch(database, setup)
    items = list(by_task(database, submission_id).values())
    lines = [succeeded(item) for item in items]
    real = ai_results.record_batch_llm_result

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("寫表頭壞了")

    monkeypatch.setattr(llm_gateway, "record_batch_llm_result", broken)
    with capture_logs() as logs:
        first = poll(database, provider_with(lines), setup)

    assert first.rows[0].error == "RuntimeError"
    assert first.rows[0].status is BatchSubmissionStatus.ENDED
    assert not first.healthy
    assert run_states(db, [items[0].run_id])[items[0].run_id] == (
        "failed",
        ErrorCode.LLM_RESULT_PENDING.value,
    )
    assert len(ledger(db)) == 1
    assert headers(db) == {}
    assert all("寫表頭壞了" not in str(log) for log in logs)

    monkeypatch.setattr(llm_gateway, "record_batch_llm_result", real)
    second = poll(database, provider_with(lines), setup)

    assert second.rows[0].status is BatchSubmissionStatus.IMPORTED
    assert second.rows[0].counts.count(BatchItemOutcome.ALREADY_DONE) == 1
    assert second.rows[0].counts.count(BatchItemOutcome.ACTIVE) == 2
    assert len(ledger(db)) == 3
    assert len({row[0] for row in ledger(db)}) == 3
    assert run_states(db, [items[0].run_id])[items[0].run_id] == (
        "failed",
        ErrorCode.LLM_RESULT_PENDING.value,
    )


def test_one_broken_batch_does_not_stop_the_others(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = submitted_batch(database, setup, papers=(1,))
    fine = submitted_batch(database, setup, papers=(2,), batch_id=OTHER_BATCH_ID)
    provider = provider_with([succeeded(item) for item in batch_items(database, broken)])
    provider_with(
        [succeeded(item) for item in batch_items(database, fine)],
        batch_id=OTHER_BATCH_ID,
        provider=provider,
    )
    real = prompt_loader.load_prompt
    calls = {"n": 0}

    def unreadable_once(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:  # 第一批的第一項：run 的 prompt 版本讀不出來
            raise PromptIntegrityError("prompt 檔被改過")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(llm_gateway, "load_prompt", unreadable_once)
    with capture_logs() as logs:
        result = poll(database, provider, setup)

    rows = {row.batch_submission_id: row for row in result.rows}
    assert rows[broken].error == "PromptIntegrityError"
    assert rows[broken].status is BatchSubmissionStatus.ENDED
    assert rows[fine].error is None
    assert rows[fine].status is BatchSubmissionStatus.IMPORTED
    assert not result.healthy
    assert submission_row(db, broken)[0] == "ended"
    assert submission_row(db, fine)[0] == "imported"
    assert result.needs_attention == 0
    assert any(log.get("error_type") == "PromptIntegrityError" for log in logs)
    assert all("prompt 檔被改過" not in str(log) for log in logs)


class _GatedResults(FakeLlmProvider):
    """下載結果時兩個 poller 都到了才開始回（讓兩邊同時匯入同一批）。"""

    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__()
        self.barrier = barrier

    def batch_results(self, provider_batch_id: str) -> Iterator[LlmBatchResult]:
        lines = super().batch_results(provider_batch_id)
        self.barrier.wait(WAIT)
        return lines


def test_two_pollers_at_once_write_each_item_exactly_once(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup, papers=(1, 2))
    found = batch_items(database, submission_id)
    lines = [succeeded(item) for item in found]
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def poller() -> None:
        try:
            own = Database(db.app_url, connect_timeout=5, pool_size=2)
            poll(own, provider_with(lines, provider=_GatedResults(barrier)), setup)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=poller) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT * 2)

    assert errors == []
    assert len(ledger(db)) == 6
    assert len({row[0] for row in ledger(db)}) == 6
    assert len(headers(db)) == 6
    assert submission_row(db, submission_id)[0] == "imported"


# --- 等待期間的變化 ---------------------------------------------------------------------------


def test_changed_input_is_charged_but_not_written(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)
    set_abstract(db, found[LlmTask.ZH_SUMMARY].key.paper_id, BANKING_ABSTRACT + " Revised.")

    result = poll(database, provider_with([succeeded(item) for item in found.values()]), setup)

    assert result.rows[0].counts.outcomes == {BatchItemOutcome.INPUT_CHANGED: 3}
    assert len(ledger(db)) == 3
    assert headers(db) == {}
    assert set(run_states(db, [item.run_id for item in found.values()]).values()) == {
        ("failed", "batch_input_changed")
    }
    assert result.rows[0].status is BatchSubmissionStatus.IMPORTED


def test_excluded_paper_gets_superseded_results(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)
    execute(
        db,
        "UPDATE paper SET excluded_reason = 'editorial' WHERE id = %s",
        found[LlmTask.ZH_SUMMARY].key.paper_id,
    )

    poll(database, provider_with([succeeded(item) for item in found.values()]), setup)

    assert set(headers(db).values()) == {("superseded", None)}
    assert set(run_states(db, [item.run_id for item in found.values()]).values()) == {
        ("succeeded", None)
    }


def test_prompt_bump_validates_with_the_runs_version_and_supersedes(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)
    real = prompt_loader.current_prompt_version
    # 現在的版本往前一版（那一版的檔案不存在：驗證若拿現在的版本讀 prompt 就會失敗）。
    monkeypatch.setattr(ai_results, "current_prompt_version", lambda task: real(task) + 1)

    result = poll(database, provider_with([succeeded(item) for item in found.values()]), setup)

    assert result.rows[0].counts.outcomes == {BatchItemOutcome.SUPERSEDED: 3}
    assert set(headers(db).values()) == {("superseded", None)}


def test_planned_input_kind_changed_is_superseded(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    # 送出時沒有摘要：只排分類、用標題；匯入前補上摘要，現在該用摘要了。
    paper_id = create_paper(database, doi="10.1/title-only", openalex_id="W9", abstract=None)
    submission_id = submitted_batch(database, setup, papers=())
    [item] = batch_items(database, submission_id)
    assert item.key.task is LlmTask.CLASSIFY
    assert item.input_kind.value == "title"
    execute(
        db,
        "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
        " VALUES (%s, 'openalex', %s, true)",
        paper_id,
        BANKING_ABSTRACT,
    )

    result = poll(database, provider_with([succeeded(item)]), setup)

    assert result.rows[0].counts.outcomes == {BatchItemOutcome.SUPERSEDED: 1}
    assert headers(db) == {item.run_id: ("superseded", None)}
    assert len(ledger(db)) == 1


def test_run_failed_between_check_and_header_is_left_alone(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)
    zh = found[LlmTask.ZH_SUMMARY]
    real = llm_gateway._current_batch_input

    def failed_meanwhile(database_: Database, job: object, item: BatchItemRun) -> object:
        if item.run_id == zh.run_id:  # 記完帳之後、寫表頭之前，別人把 run 改成 failed
            execute(
                db,
                "UPDATE derivation_run SET status = 'failed',"
                " failure_reason = 'batch_resolved_failed', finished_at = now() WHERE id = %s",
                item.run_id,
            )
        return real(database_, job, item)  # type: ignore[arg-type]

    monkeypatch.setattr(llm_gateway, "_current_batch_input", failed_meanwhile)
    result = poll(database, provider_with([succeeded(item) for item in found.values()]), setup)

    counts = result.rows[0].counts
    assert counts.count(BatchItemOutcome.ALREADY_DONE) == 1
    assert counts.count(BatchItemOutcome.ACTIVE) == 2
    assert zh.run_id not in headers(db)
    assert run_states(db, [zh.run_id])[zh.run_id] == ("failed", "batch_resolved_failed")
    assert len(ledger(db)) == 3  # 第 2 步已經記了帳
    assert result.rows[0].status is BatchSubmissionStatus.IMPORTED


# --- 對不上的行 -------------------------------------------------------------------------------


def test_unknown_and_unreadable_lines_are_only_counted(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)
    stranger = LlmBatchResult(uuid.uuid4().hex, LlmBatchResultKind.CANCELED)
    unreadable = LlmBatchResult(None, LlmBatchResultKind.UNREADABLE)
    lines = [stranger, unreadable, *(succeeded(item) for item in found.values())]

    with capture_logs() as logs:
        result = poll(database, provider_with(lines), setup)

    counts = result.rows[0].counts
    assert (counts.unknown_custom_ids, counts.unreadable_lines) == (1, 1)
    assert counts.count(BatchItemOutcome.ACTIVE) == 3
    assert len(ledger(db)) == 3
    assert any(log["event"] == "Batch 結果裡有對不上的行" for log in logs)


def test_items_missing_from_results_are_released(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)

    result = poll(database, provider_with([succeeded(found[LlmTask.ZH_SUMMARY])]), setup)

    assert result.rows[0].counts.missing == 2
    states = run_states(db, [item.run_id for item in found.values()])
    assert states[found[LlmTask.CLASSIFY].run_id] == ("failed", "batch_result_missing")
    assert states[found[LlmTask.EXTRACT_MODEL].run_id] == ("failed", "batch_result_missing")
    assert submission_row(db, submission_id)[0] == "imported"


# --- 供應商的錯誤、needs_attention ------------------------------------------------------------


def _zh_block(database: Database, setup: LlmBatchSetup, paper_id: uuid.UUID) -> AiBlockState:
    item = prepared(database, setup, paper_id, LlmTask.ZH_SUMMARY)
    settings = AiBlockSettings(
        prompt_version=item.key.prompt_version,
        model=item.key.model,
        taxonomy_version=None,
        context_values_version=None,
        budget_allows=True,
    )
    with database.transaction() as connection:
        block = find_ai_block(SYSTEM, connection, paper_id, AiProduct.ZH_SUMMARY, settings)
    assert block is not None
    return block.state


def test_provider_not_found_marks_needs_attention_and_block_failed(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup)
    paper_id = batch_items(database, submission_id)[0].key.paper_id
    assert _zh_block(database, setup, paper_id) is AiBlockState.PENDING_BATCH

    with capture_logs() as logs:
        result = poll(database, FakeLlmProvider(), setup)  # 沒準備狀態 → 404

    assert result.rows[0].status is BatchSubmissionStatus.NEEDS_ATTENTION
    assert result.rows[0].error is None
    assert result.needs_attention == 1
    assert not result.healthy
    assert submission_row(db, submission_id)[:3] == (
        "needs_attention",
        BATCH_ID,
        "batch_provider_not_found",
    )
    assert _zh_block(database, setup, paper_id) is AiBlockState.FAILED
    assert any(log["log_level"] == "error" for log in logs)


def test_other_provider_errors_leave_the_batch_and_continue(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    broken = submitted_batch(database, setup, papers=(1,))
    fine = submitted_batch(database, setup, papers=(2,), batch_id=OTHER_BATCH_ID)
    provider = FakeLlmProvider()
    provider.batch_statuses[BATCH_ID] = [LlmServerError("HTTP 503", status=503)]
    provider_with(
        [succeeded(item) for item in batch_items(database, fine)],
        batch_id=OTHER_BATCH_ID,
        provider=provider,
    )

    result = poll(database, provider, setup)

    rows = {row.batch_submission_id: row for row in result.rows}
    assert rows[broken].error == "LlmServerError（HTTP 503）"
    assert rows[broken].status is BatchSubmissionStatus.SUBMITTED
    assert rows[fine].status is BatchSubmissionStatus.IMPORTED
    assert result.errors == 1
    assert not result.healthy
    assert submission_row(db, broken)[0] == "submitted"


def test_poll_without_batches_never_opens_the_provider(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    opened: list[bool] = []

    result = poll(database, FakeLlmProvider(), setup, opened=lambda: opened.append(True))

    assert result.rows == []
    assert opened == []
    assert result.healthy


# --- resolve ----------------------------------------------------------------------------------


def _needs_attention(database: Database, setup: LlmBatchSetup, **kwargs: object) -> uuid.UUID:
    submission_id = submitted_batch(database, setup, **kwargs)  # type: ignore[arg-type]
    poll(database, FakeLlmProvider(), setup, ids=[submission_id])  # 404 → needs_attention
    return submission_id


def test_resolve_with_provider_batch_id_resumes_waiting(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _needs_attention(database, setup)
    new_id = "msgbatch_01FAKEfoundInConsole"

    result = resolve_llm_batch(SYSTEM, database, submission_id, provider_batch_id=new_id)

    assert result.outcome is ResolveOutcome.RESUMED
    assert submission_row(db, submission_id)[:3] == ("submitted", new_id, None)
    found = batch_items(database, submission_id)
    imported = poll(database, provider_with([succeeded(i) for i in found], batch_id=new_id), setup)
    assert imported.rows[0].status is BatchSubmissionStatus.IMPORTED


def test_resolve_refuses_an_id_used_by_another_batch(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    stuck = _needs_attention(database, setup, papers=(1,))
    submitted_batch(database, setup, papers=(2,), batch_id=OTHER_BATCH_ID)

    result = resolve_llm_batch(SYSTEM, database, stuck, provider_batch_id=OTHER_BATCH_ID)

    assert result.outcome is ResolveOutcome.ID_IN_USE
    assert submission_row(db, stuck)[:3] == (
        "needs_attention",
        BATCH_ID,
        "batch_provider_not_found",
    )


def test_resolve_fail_releases_running_runs(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = _needs_attention(database, setup)

    result = resolve_llm_batch(SYSTEM, database, submission_id, fail=True)

    assert result.outcome is ResolveOutcome.FAILED
    assert result.released_runs == 3
    assert submission_row(db, submission_id)[:3] == ("failed", BATCH_ID, "batch_resolved_failed")
    found = batch_items(database, submission_id)
    assert set(run_states(db, [item.run_id for item in found]).values()) == {
        ("failed", "batch_resolved_failed")
    }
    again = resolve_llm_batch(SYSTEM, database, submission_id, fail=True)
    assert again.outcome is ResolveOutcome.NOT_NEEDS_ATTENTION


def test_resolve_only_touches_needs_attention_and_checks_arguments(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    submission_id = submitted_batch(database, setup)

    result = resolve_llm_batch(SYSTEM, database, submission_id, fail=True)

    assert result.outcome is ResolveOutcome.NOT_NEEDS_ATTENTION
    assert submission_row(db, submission_id)[0] == "submitted"
    with pytest.raises(ValueError, match="剛好給一個"):
        resolve_llm_batch(SYSTEM, database, submission_id)
    with pytest.raises(ValueError, match="剛好給一個"):
        resolve_llm_batch(SYSTEM, database, submission_id, provider_batch_id=BATCH_ID, fail=True)
    with pytest.raises(ValueError, match="格式不對"):
        resolve_llm_batch(SYSTEM, database, submission_id, provider_batch_id="not an id!")
    with pytest.raises(LookupError):
        resolve_llm_batch(SYSTEM, database, uuid.uuid4(), fail=True)


# --- weekly_run → ai_done ---------------------------------------------------------------------


def _weekly_run(
    db: TestDatabase,
    *,
    snapshot_age: timedelta | None,
    number: int,
    built_age: timedelta | None = None,
) -> uuid.UUID:
    """`snapshot_age` 是距發布（`as_of`）多久；`built_age` 是距開始建立（`created_at`）多久，
    沒給就和發布同時。"""
    [(weekly_run_id,)] = execute(
        db, "INSERT INTO weekly_run (status) VALUES ('published') RETURNING id"
    )
    if snapshot_age is not None:
        execute(
            db,
            "INSERT INTO snapshot (number, weekly_run_id, status, as_of, created_at)"
            " VALUES (%s, %s, 'published', %s, %s)",
            number,
            weekly_run_id,
            POLL_AT - snapshot_age,
            POLL_AT - (snapshot_age if built_age is None else built_age),
        )
    return uuid.UUID(str(weekly_run_id))


def _weekly_status(db: TestDatabase, weekly_run_id: uuid.UUID) -> str:
    [(status,)] = execute(db, "SELECT status FROM weekly_run WHERE id = %s", weekly_run_id)
    return str(status)


def test_weekly_run_reaches_ai_done_when_batches_finish_or_after_24_hours(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    finished = _weekly_run(db, snapshot_age=timedelta(hours=1), number=1)
    waiting = _weekly_run(db, snapshot_age=timedelta(hours=1), number=2)
    old = _weekly_run(db, snapshot_age=timedelta(hours=25), number=3)
    nothing_yet = _weekly_run(db, snapshot_age=timedelta(hours=1), number=4)
    # 很早就開始建、最近才發布：24 小時從發布算，還不到
    slow_build = _weekly_run(
        db, snapshot_age=timedelta(hours=1), built_age=timedelta(hours=30), number=5
    )
    done_batch = submitted_batch(database, setup, papers=(1,))
    still_running = submitted_batch(database, setup, papers=(2,), batch_id=OTHER_BATCH_ID)
    execute(
        db, "UPDATE batch_submission SET weekly_run_id = %s WHERE id = %s", finished, done_batch
    )
    execute(
        db, "UPDATE batch_submission SET weekly_run_id = %s WHERE id = %s", waiting, still_running
    )
    provider = provider_with([succeeded(item) for item in batch_items(database, done_batch)])
    provider_with(
        [],
        status=fake_batch_status(OTHER_BATCH_ID, BatchProcessingStatus.IN_PROGRESS),
        batch_id=OTHER_BATCH_ID,
        provider=provider,
    )

    result = poll(database, provider, setup)

    assert sorted(result.ai_done) == sorted([finished, old])
    assert _weekly_status(db, finished) == "ai_done"
    assert _weekly_status(db, old) == "ai_done"
    assert _weekly_status(db, waiting) == "published"
    assert _weekly_status(db, nothing_yet) == "published"
    assert _weekly_status(db, slow_build) == "published"
    assert poll(database, provider, setup).ai_done == []  # 第二次不會再改


# --- 手動規劃與預估 ---------------------------------------------------------------------------


def test_manual_plan_ignores_the_weekly_switch_and_estimate_matches(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    paper_id = banking_paper(database)
    expected = sum(
        (item_cost(setup, prepared(database, setup, paper_id, task)) for task in REPLIES),
        Decimal(0),
    )

    estimate = estimate_llm_batches(SYSTEM, database, setup)
    weekly = plan_llm_batches(
        SYSTEM, database, setup, weekly_run_id=None, clock=lambda: NOW, enqueue=lambda *_: True
    )
    manual = plan_llm_batches(
        SYSTEM,
        database,
        setup,
        weekly_run_id=None,
        clock=lambda: NOW,
        enqueue=lambda *_: True,
        manual=True,
    )
    after = estimate_llm_batches(SYSTEM, database, setup)

    assert estimate.by_task == dict.fromkeys(REPLIES, 1)
    assert estimate.estimated_max_cost_usd == expected
    assert weekly.stop is PlanStop.DISABLED
    assert manual.stop is PlanStop.COMPLETED
    assert manual.planned == 3
    assert manual.estimated_max_cost_usd == expected
    assert after.planned == 0
    assert after.skipped == 3


def test_estimate_writes_nothing(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    banking_paper(database)

    estimate_llm_batches(SYSTEM, database, setup)

    assert execute(db, "SELECT count(*) FROM derivation_run") == [(0,)]
    assert execute(db, "SELECT count(*) FROM batch_submission") == [(0,)]


def test_estimate_counts_resumable_runs_like_the_plan(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    paper_id = banking_paper(database)
    insert_pending_run(db, prepared(database, setup, paper_id, LlmTask.ZH_SUMMARY).key)
    expected = sum(
        (
            item_cost(setup, prepared(database, setup, paper_id, task))
            for task in REPLIES
            if task is not LlmTask.ZH_SUMMARY
        ),
        Decimal(0),
    )

    estimate = estimate_llm_batches(SYSTEM, database, setup)
    plan = plan_llm_batches(
        SYSTEM,
        database,
        setup,
        weekly_run_id=None,
        clock=lambda: NOW,
        enqueue=lambda *_: True,
        manual=True,
    )

    assert estimate.resumable == 1
    assert estimate.by_task.get(LlmTask.ZH_SUMMARY, 0) == 0
    assert estimate.by_task[LlmTask.CLASSIFY] == estimate.by_task[LlmTask.EXTRACT_MODEL] == 1
    assert estimate.estimated_max_cost_usd == expected
    assert plan.resumable == estimate.resumable
    assert plan.planned == sum(estimate.by_task.values()) == 2
    assert plan.estimated_max_cost_usd == estimate.estimated_max_cost_usd


def test_failed_validation_marks_run_pending_and_reports_error(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("驗證函式壞了")

    monkeypatch.setattr(llm_gateway, "_validated_output", broken)
    result = poll(database, provider_with([succeeded(item) for item in found.values()]), setup)

    assert result.rows[0].counts.outcomes == {BatchItemOutcome.ERROR: 3}
    assert result.errors == 3
    assert not result.healthy
    assert set(run_states(db, [item.run_id for item in found.values()]).values()) == {
        ("failed", ErrorCode.LLM_RESULT_PENDING.value)
    }
    assert len(ledger(db)) == 3
    assert headers(db) == {}


# --- 審查後：Codex 主審查第 1、3、4 條 ------------------------------------------------------


class _PausingProvider(FakeLlmProvider):
    """下載結果時，讀完前 `after` 行先執行 `pause`（模擬 poll A 拿到 ended、匯入到一半停住，期間
    別的行程動了這批），再接著回剩下的行。"""

    def __init__(self, pause: Callable[[], None], *, after: int) -> None:
        super().__init__()
        self._pause = pause
        self._after = after

    def batch_results(self, provider_batch_id: str) -> Iterator[LlmBatchResult]:
        lines = super().batch_results(provider_batch_id)

        def paused() -> Iterator[LlmBatchResult]:
            for n, result in enumerate(lines):
                if n == self._after:
                    self._pause()
                yield result

        return paused()


@pytest.mark.parametrize("after", [0, 1])
def test_stale_poll_writes_nothing_after_another_poll_marks_needs_attention(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, after: int
) -> None:
    """A 拿到 ended 後停住；B 查同一批收到 404、標 needs_attention；A 恢復後一筆都不寫。"""
    submission_id = submitted_batch(database, setup)
    found = list(by_task(database, submission_id).values())

    def other_poll() -> None:
        marked = poll(database, FakeLlmProvider(), setup)  # 沒準備狀態 → 404
        assert marked.rows[0].status is BatchSubmissionStatus.NEEDS_ATTENTION

    stale = provider_with(
        [succeeded(item) for item in found], provider=_PausingProvider(other_poll, after=after)
    )

    result = poll(database, stale, setup)

    [row] = result.rows
    assert row.stopped
    assert row.status is BatchSubmissionStatus.NEEDS_ATTENTION
    assert result.needs_attention == 1
    assert not result.healthy
    assert submission_row(db, submission_id)[:3] == (
        "needs_attention",
        BATCH_ID,
        "batch_provider_not_found",
    )
    # 停住之前已經匯入的（after 行）照舊；之後的一筆都沒寫：沒有帳、沒有表頭、run 還是 running。
    assert len(ledger(db)) == after
    assert len(headers(db)) == after
    states = run_states(db, [item.run_id for item in found])
    assert [states[item.run_id][0] for item in found[after:]] == ["running"] * (3 - after)
    # 底下還在跑的項目，AI 區塊照 needs_attention 的規則顯示 failed（不是 A 寫的 active）。
    zh = next(item for item in found if item.key.task is LlmTask.ZH_SUMMARY)
    if zh in found[after:]:
        assert _zh_block(database, setup, zh.key.paper_id) is AiBlockState.FAILED


def test_stale_poll_writes_nothing_after_the_batch_id_is_replaced(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    """A 停住期間，人工確認把這批換成別的供應商 batch ID、而且新的 ID 也已經結束（狀態又是
    ended）：A 下載的是舊 ID 的結果，一筆都不寫。"""
    submission_id = submitted_batch(database, setup)
    found = list(by_task(database, submission_id).values())

    def replaced() -> None:
        poll(database, FakeLlmProvider(), setup)  # 404 → needs_attention
        resolved = resolve_llm_batch(
            SYSTEM, database, submission_id, provider_batch_id=OTHER_BATCH_ID
        )
        assert resolved.outcome is ResolveOutcome.RESUMED
        with database.transaction() as connection:
            assert update_batch_submission_ended(connection, submission_id, OTHER_BATCH_ID)

    stale = provider_with(
        [succeeded(item) for item in found], provider=_PausingProvider(replaced, after=0)
    )

    result = poll(database, stale, setup)

    assert result.rows[0].stopped
    assert submission_row(db, submission_id)[:2] == ("ended", OTHER_BATCH_ID)
    assert ledger(db) == []
    assert headers(db) == {}
    assert set(run_states(db, [item.run_id for item in found]).values()) == {("running", None)}


def _counts(succeeded_items: int) -> BatchRequestCounts:
    return BatchRequestCounts(
        processing=0, succeeded=succeeded_items, errored=0, canceled=0, expired=0
    )


def test_truncated_results_are_not_treated_as_missing_and_are_read_again(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    """串流正常結束但只回了 1 行（供應商說有 3 項）：不判缺項、不放掉預留、批次留 ended；下次重讀
    補齊，帳不重記。"""
    submission_id = submitted_batch(database, setup)
    found = list(by_task(database, submission_id).values())
    status = fake_batch_status(BATCH_ID, request_counts=_counts(3))

    first = poll(database, provider_with([succeeded(found[0])], status=status), setup)

    [row] = first.rows
    assert row.error == "BatchResultsIncomplete（讀到 1／3 項）"
    assert row.status is BatchSubmissionStatus.ENDED
    assert row.counts.missing == 0
    assert not first.healthy
    states = run_states(db, [item.run_id for item in found])
    assert [states[item.run_id][0] for item in found] == ["succeeded", "running", "running"]
    assert submission_row(db, submission_id)[0] == "ended"

    second = poll(
        database, provider_with([succeeded(item) for item in found], status=status), setup
    )

    assert second.rows[0].status is BatchSubmissionStatus.IMPORTED
    assert second.rows[0].counts.count(BatchItemOutcome.ALREADY_DONE) == 1
    assert second.healthy
    assert len(ledger(db)) == 3
    assert set(headers(db).values()) == {("active", None)}


def test_unreadable_lines_count_toward_the_provider_total(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    """讀不到 custom_id 的行也算讀到一項：供應商那邊真的有一行壞掉時，批次照樣收得完（那一項
    判缺項），不會每次重讀卡住。"""
    submission_id = submitted_batch(database, setup)
    found = list(by_task(database, submission_id).values())
    status = fake_batch_status(BATCH_ID, request_counts=_counts(3))
    lines = [
        succeeded(found[0]),
        succeeded(found[1]),
        LlmBatchResult(None, LlmBatchResultKind.UNREADABLE),
    ]

    result = poll(database, provider_with(lines, status=status), setup)

    [row] = result.rows
    assert row.error is None
    assert row.status is BatchSubmissionStatus.IMPORTED
    assert row.counts.unreadable_lines == 1
    assert row.counts.missing == 1
    assert run_states(db, [found[2].run_id])[found[2].run_id] == (
        "failed",
        "batch_result_missing",
    )


@pytest.mark.parametrize(
    "processing", [BatchProcessingStatus.IN_PROGRESS, BatchProcessingStatus.CANCELING]
)
def test_unfinished_batch_past_retention_is_released_like_expired_results(
    db: TestDatabase, database: Database, setup: LlmBatchSetup, processing: BatchProcessingStatus
) -> None:
    """建立超過 29 天還回 in_progress／canceling：照「拿不到結果」結束剩下的 run、釋放預留。"""
    submission_id = submitted_batch(database, setup)
    provider = provider_with([], status=fake_batch_status(BATCH_ID, processing))

    result = poll(database, provider, setup, now=NOW + timedelta(days=29, hours=1))

    [row] = result.rows
    assert row.overdue
    assert row.counts.unavailable == 3
    assert row.status is BatchSubmissionStatus.IMPORTED
    assert provider.downloaded == []
    assert submission_row(db, submission_id) == ("imported", BATCH_ID, None, True, True)
    found = batch_items(database, submission_id)
    assert set(run_states(db, [item.run_id for item in found]).values()) == {
        ("failed", "batch_results_unavailable")
    }


# --- 確認審查 D2：計數一直對不上要有人工出口 ---------------------------------------------------


def _incomplete_reads(db: TestDatabase, submission_id: uuid.UUID) -> int:
    [(value,)] = execute(
        db, "SELECT incomplete_reads FROM batch_submission WHERE id = %s", submission_id
    )
    return int(value)


def test_results_short_of_the_provider_count_three_times_need_attention(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    """供應商說 4 項、結果檔一直只有 3 行：前兩次留 ended 重讀，第三次（設定預設 3）改
    needs_attention（batch_results_incomplete）；`resolve --fail` 收得掉，沒讀到的那項釋放。"""
    assert setup.batches.incomplete_results_attention_after == 3
    submission_id = submitted_batch(database, setup)
    found = list(by_task(database, submission_id).values())
    status = fake_batch_status(BATCH_ID, request_counts=_counts(4))

    for expected_reads in (1, 2):
        result = poll(database, provider_with([succeeded(i) for i in found], status=status), setup)
        [row] = result.rows
        assert row.status is BatchSubmissionStatus.ENDED
        assert row.error == "BatchResultsIncomplete（讀到 3／4 項）"
        assert _incomplete_reads(db, submission_id) == expected_reads
        assert result.needs_attention == 0

    third = poll(database, provider_with([succeeded(i) for i in found], status=status), setup)

    [row] = third.rows
    assert row.status is BatchSubmissionStatus.NEEDS_ATTENTION
    assert third.needs_attention == 1
    assert not third.healthy
    assert submission_row(db, submission_id)[:3] == (
        "needs_attention",
        BATCH_ID,
        "batch_results_incomplete",
    )
    assert _incomplete_reads(db, submission_id) == 3
    assert len(ledger(db)) == 3  # 三次都讀到同樣 3 項，帳只記一次
    # 之後的 poll 不再查它（不是 submitted／ended），不會一直重下載。
    assert poll(database, FakeLlmProvider(), setup).rows == []

    resolved = resolve_llm_batch(SYSTEM, database, submission_id, fail=True)

    assert resolved.outcome is ResolveOutcome.FAILED
    assert submission_row(db, submission_id)[:3] == ("failed", BATCH_ID, "batch_resolved_failed")


def test_results_completed_before_the_limit_are_imported_normally(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    """對不上兩次、第三次讀齊了：照常匯入（imported），不會進 needs_attention。"""
    submission_id = submitted_batch(database, setup)
    found = list(by_task(database, submission_id).values())
    status = fake_batch_status(BATCH_ID, request_counts=_counts(3))
    for _ in range(2):
        poll(database, provider_with([succeeded(found[0])], status=status), setup)
    assert _incomplete_reads(db, submission_id) == 2

    result = poll(database, provider_with([succeeded(i) for i in found], status=status), setup)

    assert result.rows[0].status is BatchSubmissionStatus.IMPORTED
    assert result.healthy
    assert submission_row(db, submission_id) == ("imported", BATCH_ID, None, True, True)
    assert len(ledger(db)) == 3
    assert set(headers(db).values()) == {("active", None)}


def test_resolving_with_the_same_id_starts_counting_again(
    db: TestDatabase, database: Database, setup: LlmBatchSetup
) -> None:
    """needs_attention（batch_results_incomplete）後用同一個 ID `resolve --provider-batch-id`：
    回到 submitted、下一次看到 ended 時次數歸零，再讀一次對不上只算第 1 次。"""
    tight = dataclasses.replace(
        setup,
        batches=setup.batches.model_copy(update={"incomplete_results_attention_after": 1}),
    )
    submission_id = submitted_batch(database, tight)
    found = list(by_task(database, submission_id).values())
    status = fake_batch_status(BATCH_ID, request_counts=_counts(4))
    poll(database, provider_with([succeeded(i) for i in found], status=status), tight)
    assert submission_row(db, submission_id)[2] == "batch_results_incomplete"

    resumed = resolve_llm_batch(SYSTEM, database, submission_id, provider_batch_id=BATCH_ID)

    assert resumed.outcome is ResolveOutcome.RESUMED
    result = poll(database, provider_with([succeeded(i) for i in found], status=status), setup)
    assert result.rows[0].status is BatchSubmissionStatus.ENDED
    assert _incomplete_reads(db, submission_id) == 1
