"""`radar batches create／poll／wait／resolve` 與 `radar runs report`（Story 2.11、ADR-0043）對真的
PostgreSQL（`CliRunner`＋環境變數）。

provider 換成假的（批次 ID、狀態與結果行不是真的錄的，形狀照官方文件）；`wait` 的等待換掉、
不真的睡。
不連網路；環境變數只用測試值；論文與回覆都是測試自己編的。
"""

import dataclasses
import logging
import shutil
import uuid
from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest
from typer.testing import CliRunner, Result

import radar.entrypoints.cli as cli
import radar.services.ai_results as ai_results
import radar.services.llm_gateway as llm_gateway
from radar.adapters.fake import FakeLlmProvider, fake_batch_status
from radar.domain.ai_outputs import LlmTask
from radar.domain.llm import (
    BatchProcessingStatus,
    LlmBatchRequest,
    LlmBatchResult,
    LlmBatchResultKind,
    LlmBatchStatus,
    LlmConfigurationError,
    LlmInvalidRequestError,
)
from radar.domain.run_reports import RunOutcome, RunScope
from radar.repo.db import Database
from radar.services.llm_batches import plan_llm_batches
from radar.services.run_reports import get_run_report
from tests.batch_import_support import (
    POLL_AT,
    SYSTEM,
    banking_paper,
    batch_items,
    by_task,
    ledger,
    line,
    poll,
    provider_with,
    submission_row,
    submitted_batch,
    succeeded,
)
from tests.batch_support import BATCH_ID, NOW, batch_setup, insert_run
from tests.database import ROOT, TestDatabase, migrate
from tests.llm_support import execute, insert_ledger

runner = CliRunner()


class _ClosingFake(FakeLlmProvider):
    """可以 `with` 的假 provider；記下有沒有關、關了以後不能再用。"""

    def __init__(self, batch_replies: tuple[str | BaseException, ...] = ()) -> None:
        super().__init__(batch_replies=batch_replies)
        self.closed = False

    def submit_batch(self, requests: Sequence[LlmBatchRequest]) -> str:
        assert not self.closed
        return super().submit_batch(requests)

    def retrieve_batch(self, provider_batch_id: str) -> LlmBatchStatus:
        assert not self.closed
        return super().retrieve_batch(provider_batch_id)

    def batch_results(self, provider_batch_id: str) -> Iterator[LlmBatchResult]:
        assert not self.closed
        return super().batch_results(provider_batch_id)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def env(db: TestDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """指令要用的環境變數（repo 的設定複本）與固定的時鐘；沒有 Anthropic 金鑰。"""
    config_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", config_dir)
    monkeypatch.setenv("DATABASE_URL", db.app_url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("RADAR_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_utc_now", lambda: POLL_AT)
    return config_dir


def _closing(
    lines: list[LlmBatchResult | BaseException], *, status: LlmBatchStatus | None = None
) -> _ClosingFake:
    """查 `BATCH_ID` 回 `status`（預設已結束）、下載回 `lines` 的可關閉假 provider。"""
    fake = _ClosingFake()
    provider_with(lines, status=status, provider=fake)
    return fake


def _use(monkeypatch: pytest.MonkeyPatch, provider: FakeLlmProvider) -> list[object]:
    """CLI 建 provider 時拿到 `provider`；回傳建過幾次的紀錄。"""
    built: list[object] = []

    def build(settings: object) -> FakeLlmProvider:
        built.append(settings)
        return provider

    monkeypatch.setattr(cli, "build_llm_provider", build)
    return built


def _invoke(args: list[str], *, input: str | None = None) -> Result:
    """執行指令；結束後把 root logger 的 handler 換回原本的（見 test_costs_cli）。"""
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        return runner.invoke(cli.app, args, input=input)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)


def _lines(output: str) -> list[str]:
    # 日誌是 JSON（以 { 開頭）；指令本身印的不是。
    return [text for text in output.splitlines() if not text.startswith("{")]


# --- create -----------------------------------------------------------------------------------


def test_create_estimate_only_writes_nothing_and_needs_no_key(
    db: TestDatabase, database: Database, env: Path
) -> None:
    banking_paper(database)

    result = _invoke(["batches", "create", "--estimate"])

    assert result.exit_code == 0, result.output
    shown = "\n".join(_lines(result.output))
    assert "預估要排的 Batch" in shown
    assert "保守上限（Batch 價）" in shown
    assert "本月" in shown
    assert execute(db, "SELECT count(*) FROM batch_submission") == [(0,)]
    assert execute(db, "SELECT count(*) FROM derivation_run") == [(0,)]


def test_create_plans_manually_and_submits_in_process(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    banking_paper(database)
    provider = _ClosingFake((BATCH_ID,))
    built = _use(monkeypatch, provider)

    result = _invoke(["batches", "create", "--yes"])

    assert result.exit_code == 0, result.output
    assert len(built) == 1
    assert provider.closed
    assert execute(db, "SELECT status, provider_batch_id, item_count FROM batch_submission") == [
        ("submitted", BATCH_ID, 3)
    ]
    shown = "\n".join(_lines(result.output))
    assert f"→ {BATCH_ID}" in shown
    assert "radar batches wait" in shown


def test_create_without_confirmation_does_nothing(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    banking_paper(database)
    built = _use(monkeypatch, _ClosingFake((BATCH_ID,)))

    result = _invoke(["batches", "create"], input="n\n")

    assert result.exit_code == 1
    assert built == []
    assert execute(db, "SELECT count(*) FROM batch_submission") == [(0,)]


def test_create_exits_1_when_a_batch_is_not_sent(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    banking_paper(database)
    _use(monkeypatch, _ClosingFake((LlmInvalidRequestError("HTTP 400", status=400),)))

    result = _invoke(["batches", "create"], input="y\n")

    assert result.exit_code == 1
    assert "LlmInvalidRequestError" in result.output
    assert execute(db, "SELECT status FROM batch_submission") == [("failed",)]


def test_create_without_key_keeps_the_batch_queued(
    db: TestDatabase, database: Database, env: Path
) -> None:
    banking_paper(database)

    result = _invoke(["batches", "create", "--yes"])

    assert result.exit_code == 1
    assert "RADAR_ANTHROPIC_API_KEY" in result.output
    assert execute(db, "SELECT status FROM batch_submission") == [("queued",)]


# --- poll -------------------------------------------------------------------------------------


def test_poll_imports_and_prints_one_line_per_batch(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = submitted_batch(database, batch_setup())
    provider = _closing([succeeded(item) for item in batch_items(database, submission_id)])
    _use(monkeypatch, provider)

    result = _invoke(["batches", "poll"])

    assert result.exit_code == 0, result.output
    shown = _lines(result.output)
    assert any(str(submission_id) in text and "imported" in text for text in shown)
    assert "needs_attention：0 批" in shown
    assert submission_row(db, submission_id)[0] == "imported"


def test_poll_without_batches_does_not_need_a_key(db: TestDatabase, env: Path) -> None:
    result = _invoke(["batches", "poll"])

    assert result.exit_code == 0, result.output
    assert "沒有要查的批次（submitted／ended）" in result.output


def test_poll_exits_1_when_a_batch_needs_attention(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = submitted_batch(database, batch_setup())
    _use(monkeypatch, _ClosingFake())  # 沒準備狀態 → 404

    result = _invoke(["batches", "poll"])

    assert result.exit_code == 1
    assert "needs_attention：1 批" in result.output
    assert submission_row(db, submission_id)[0] == "needs_attention"


# --- wait -------------------------------------------------------------------------------------


def _sleeps(monkeypatch: pytest.MonkeyPatch, effect: BaseException | None = None) -> list[float]:
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if effect is not None:
            raise effect

    monkeypatch.setattr(cli, "_sleep", sleep)
    return slept


def test_wait_polls_until_every_target_is_finished(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = submitted_batch(database, batch_setup())
    provider = _closing([succeeded(item) for item in batch_items(database, submission_id)])
    provider.batch_statuses[BATCH_ID] = [
        fake_batch_status(BATCH_ID, BatchProcessingStatus.IN_PROGRESS),
        fake_batch_status(BATCH_ID),
    ]
    built = _use(monkeypatch, provider)
    slept = _sleeps(monkeypatch)

    result = _invoke(["batches", "wait", "--interval-minutes", "3"])

    assert result.exit_code == 0, result.output
    shown = _lines(result.output)
    assert "已結束 0 批、還在跑 1 批" in shown
    assert "已結束 1 批、還在跑 0 批" in shown
    assert slept == [180]
    assert len(built) == 1  # 兩次檢查共用同一個 provider
    assert provider.closed
    assert submission_row(db, submission_id)[0] == "imported"


def test_wait_uses_the_configured_interval(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted_batch(database, batch_setup())
    provider = _closing([], status=fake_batch_status(BATCH_ID, BatchProcessingStatus.IN_PROGRESS))
    _use(monkeypatch, provider)
    slept = _sleeps(monkeypatch, KeyboardInterrupt())

    result = _invoke(["batches", "wait"])

    assert result.exit_code == 130
    assert slept == [600]  # config/llm_batches.yaml：poll_interval_minutes 10


def test_wait_interrupted_then_rerun_imports_once(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = submitted_batch(database, batch_setup())
    found = by_task(database, submission_id)
    items = list(found.values())
    lines: list[LlmBatchResult | BaseException] = [succeeded(items[0]), KeyboardInterrupt()]
    _use(monkeypatch, _closing(lines))

    interrupted = _invoke(["batches", "wait"])

    assert interrupted.exit_code == 130
    assert len(ledger(db)) == 1
    assert submission_row(db, submission_id)[0] == "ended"

    _use(monkeypatch, _closing([succeeded(item) for item in items]))
    rerun = _invoke(["batches", "wait"])

    assert rerun.exit_code == 0, rerun.output
    assert len(ledger(db)) == 3
    assert submission_row(db, submission_id)[0] == "imported"


def test_wait_stops_when_a_target_needs_attention(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = submitted_batch(database, batch_setup())
    _use(monkeypatch, _ClosingFake())  # 404
    slept = _sleeps(monkeypatch)

    result = _invoke(["batches", "wait", "--id", str(submission_id)])

    assert result.exit_code == 1
    assert slept == []
    assert "radar batches resolve" in result.output


def test_wait_without_targets_returns_immediately(db: TestDatabase, env: Path) -> None:
    result = _invoke(["batches", "wait"])

    assert result.exit_code == 0, result.output
    assert "沒有要等的批次" in result.output


def test_wait_refuses_unknown_ids(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = submitted_batch(database, batch_setup())
    missing = uuid.uuid4()
    built = _use(monkeypatch, _closing([]))
    slept = _sleeps(monkeypatch)

    result = _invoke(["batches", "wait", "--id", str(known), "--id", str(missing)])

    assert result.exit_code == 1
    assert "找不到這些批次" in result.output
    assert str(missing) in result.output
    assert built == []
    assert slept == []
    assert submission_row(db, known)[0] == "submitted"


def test_wait_refuses_batches_not_sent_yet(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    banking_paper(database)
    plan = plan_llm_batches(
        SYSTEM,
        database,
        batch_setup(),
        weekly_run_id=None,
        clock=lambda: NOW,
        enqueue=lambda *_: True,
        manual=True,
    )
    [queued] = plan.submission_ids
    built = _use(monkeypatch, _closing([]))
    slept = _sleeps(monkeypatch)

    result = _invoke(["batches", "wait", "--id", str(queued)])

    assert result.exit_code == 1
    assert "wait 只等已經送出的批次" in result.output
    assert f"{queued}（queued）" in result.output
    assert built == []
    assert slept == []


def test_wait_gives_up_after_three_rounds_of_errors(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_id = submitted_batch(database, batch_setup())
    provider = _ClosingFake()
    provider.batch_statuses[BATCH_ID] = [LlmConfigurationError("HTTP 401", status=401)]
    _use(monkeypatch, provider)
    slept = _sleeps(monkeypatch)

    result = _invoke(["batches", "wait", "--interval-minutes", "1"])

    assert result.exit_code == 1
    assert len(provider.retrieved) == 3
    assert slept == [60, 60]
    assert "連續 3 次查詢都有錯誤" in result.output
    assert submission_row(db, submission_id)[0] == "submitted"


# --- resolve ----------------------------------------------------------------------------------


def _stuck(database: Database) -> uuid.UUID:
    setup = batch_setup()
    submission_id = submitted_batch(database, setup)
    poll(database, FakeLlmProvider(), setup)  # 404 → needs_attention
    return submission_id


@pytest.mark.parametrize(
    "extra",
    [[], ["--fail", "--provider-batch-id", BATCH_ID], ["--provider-batch-id", "has space"]],
)
def test_resolve_argument_errors_exit_2(db: TestDatabase, env: Path, extra: list[str]) -> None:
    result = _invoke(["batches", "resolve", str(uuid.uuid4()), *extra])

    assert result.exit_code == 2


def test_resolve_with_provider_batch_id(db: TestDatabase, database: Database, env: Path) -> None:
    submission_id = _stuck(database)

    result = _invoke(
        ["batches", "resolve", str(submission_id), "--provider-batch-id", "msgbatch_found"]
    )

    assert result.exit_code == 0, result.output
    assert submission_row(db, submission_id)[:3] == ("submitted", "msgbatch_found", None)


def test_resolve_fail_asks_first(db: TestDatabase, database: Database, env: Path) -> None:
    submission_id = _stuck(database)

    declined = _invoke(["batches", "resolve", str(submission_id), "--fail"], input="n\n")

    assert declined.exit_code == 1
    assert "3 筆 run" in declined.output
    assert "Console" in declined.output
    assert submission_row(db, submission_id)[0] == "needs_attention"

    accepted = _invoke(["batches", "resolve", str(submission_id), "--fail", "--yes"])

    assert accepted.exit_code == 0, accepted.output
    assert submission_row(db, submission_id)[:3] == ("failed", BATCH_ID, "batch_resolved_failed")


def test_resolve_refuses_other_states_and_unknown_batches(
    db: TestDatabase, database: Database, env: Path
) -> None:
    submission_id = submitted_batch(database, batch_setup())

    not_stuck = _invoke(["batches", "resolve", str(submission_id), "--fail", "--yes"])
    unknown = _invoke(["batches", "resolve", str(uuid.uuid4()), "--fail", "--yes"])

    assert not_stuck.exit_code == 1
    assert "不是 needs_attention" in not_stuck.output
    assert unknown.exit_code == 1
    assert submission_row(db, submission_id)[0] == "submitted"


# --- runs report ------------------------------------------------------------------------------


def _imported_with_rejection(database: Database) -> uuid.UUID:
    setup = batch_setup()
    submission_id = submitted_batch(database, setup)
    found = list(by_task(database, submission_id).values())
    lines = [
        succeeded(found[0]),
        line(found[1], LlmBatchResultKind.ERRORED, error_type="invalid_request_error"),
        line(found[2], LlmBatchResultKind.CANCELED),
    ]
    poll(database, provider_with(lines), setup)
    return submission_id


def test_report_needs_a_scope(db: TestDatabase, env: Path) -> None:
    assert _invoke(["runs", "report"]).exit_code == 2
    assert _invoke(["runs", "report", "--since", "yesterday"]).exit_code == 2


def test_report_by_batch_counts_outcomes_and_reasons(
    db: TestDatabase, database: Database, env: Path
) -> None:
    submission_id = _imported_with_rejection(database)

    result = _invoke(["runs", "report", "--batch", str(submission_id)])

    assert result.exit_code == 0, result.output
    shown = "\n".join(_lines(result.output))
    assert "llm_invalid_request" in shown
    assert "batch_item_canceled" in shown
    assert "拒收依原因代碼" in shown


def test_report_by_ids_file_and_since(
    db: TestDatabase, database: Database, env: Path, tmp_path: Path
) -> None:
    submission_id = _imported_with_rejection(database)
    paper_id = batch_items(database, submission_id)[0].key.paper_id
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text(f"# 這次的論文\n\n  {paper_id}\n", encoding="utf-8")
    bad_file = tmp_path / "bad.txt"
    bad_file.write_text("not-a-uuid\n", encoding="utf-8")

    by_ids = _invoke(["runs", "report", "--ids-file", str(ids_file)])
    by_since = _invoke(["runs", "report", "--since", "2000-01-01"])
    future = _invoke(["runs", "report", "--since", "2999-01-01T08:00"])
    bad = _invoke(["runs", "report", "--ids-file", str(bad_file)])

    assert by_ids.exit_code == 0, by_ids.output
    assert "llm_invalid_request" in by_ids.output
    assert by_since.exit_code == 0, by_since.output
    assert "llm_invalid_request" in by_since.output
    assert future.exit_code == 0, future.output
    assert "範圍內沒有 AI 工作的 run" in future.output
    assert bad.exit_code == 2


def test_report_reads_an_ids_file_with_utf8_bom(
    db: TestDatabase, database: Database, env: Path, tmp_path: Path
) -> None:
    """和 2.10 的 `--ids-file` 一樣收 BOM（有些編輯器或試算表匯出時會加）與 CRLF。"""
    submission_id = _imported_with_rejection(database)
    paper_id = batch_items(database, submission_id)[0].key.paper_id
    ids_file = tmp_path / "ids-bom.txt"
    ids_file.write_text(f"﻿{paper_id}\r\n", encoding="utf-8")

    result = _invoke(["runs", "report", "--ids-file", str(ids_file)])

    assert result.exit_code == 0, result.output
    assert "llm_invalid_request" in result.output


def test_report_takes_the_newest_normal_run_and_skips_eval_runs(
    db: TestDatabase, database: Database, env: Path
) -> None:
    setup = batch_setup()
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)
    zh = found[LlmTask.ZH_SUMMARY]
    # 同一篇、同一個工作：更早一筆被取消的 run，和一筆最新的評估 run（不算）。
    older = insert_run(db, zh.key, status="failed", failure_reason="batch_item_canceled")
    execute(
        db,
        "UPDATE derivation_run SET created_at = now() - interval '1 day' WHERE id = %s",
        older,
    )
    poll(database, provider_with([succeeded(item) for item in found.values()]), setup)
    insert_ledger(
        db,
        zh.key.paper_id,
        LlmTask.ZH_SUMMARY,
        "0.01",
        POLL_AT + timedelta(days=3650),
        eval_run_id=uuid.uuid4(),
    )

    rows = get_run_report(SYSTEM, database, RunScope(paper_ids=(zh.key.paper_id,)))

    assert len(rows) == 3
    assert [row.outcome for row in rows if row.task is LlmTask.ZH_SUMMARY] == [RunOutcome.ACTIVE]
    assert {row.outcome for row in rows} == {RunOutcome.ACTIVE}


# --- 審查後：Codex 主審查第 2、5、6 條 ------------------------------------------------------

OTHER_BATCH_ID = "msgbatch_01FAKEotherBatchIdForTests"


def _queued_batch(database: Database, n: int) -> uuid.UUID:
    """第 n 篇論文排成一批 queued（還沒送出），回它的 id。"""
    banking_paper(database, n)
    plan = plan_llm_batches(
        SYSTEM,
        database,
        batch_setup(),
        weekly_run_id=None,
        clock=lambda: NOW,
        enqueue=lambda *_: True,
        manual=True,
    )
    [queued] = plan.submission_ids
    return queued


def _status_of(db: TestDatabase, submission_id: uuid.UUID) -> str:
    return str(submission_row(db, submission_id)[0])


def test_create_shows_and_sends_batches_already_queued(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """之前留下的 queued 批次列在確認畫面、算進合計上限，確認後一起送出。"""
    left_over = _queued_batch(database, 1)
    banking_paper(database, 2)
    provider = _ClosingFake((BATCH_ID, OTHER_BATCH_ID))
    _use(monkeypatch, provider)

    estimate = _invoke(["batches", "create", "--estimate"])

    assert estimate.exit_code == 0, estimate.output
    shown = "\n".join(_lines(estimate.output))
    assert "已排好、還沒送出的批次：1 批" in shown
    assert str(left_over) in shown
    assert "這次會送出：6 筆（新排 3＋已排好 3）" in shown

    result = _invoke(["batches", "create", "--yes"])

    assert result.exit_code == 0, result.output
    assert execute(db, "SELECT status, count(*) FROM batch_submission GROUP BY status") == [
        ("submitted", 2)
    ]
    assert submission_row(db, left_over)[:2] == ("submitted", BATCH_ID)


def test_create_does_not_send_batches_planned_while_confirming(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """確認畫面出來之後，別的規劃（例如每週）才建的 queued 批次不在確認範圍：不送、列出來。"""
    confirmed = _queued_batch(database, 1)
    planned_later: list[uuid.UUID] = []

    def confirm_while_another_plan_runs(question: str) -> bool:
        planned_later.append(_queued_batch(database, 2))
        return True

    monkeypatch.setattr(cli, "_confirm", confirm_while_another_plan_runs)
    _use(monkeypatch, _ClosingFake((BATCH_ID,)))

    result = _invoke(["batches", "create"])

    assert result.exit_code == 0, result.output
    [later] = planned_later
    assert submission_row(db, confirmed)[:2] == ("submitted", BATCH_ID)
    assert _status_of(db, later) == "queued"
    assert "另有 1 批 queued 不在這次確認的範圍" in result.output
    assert str(later) in result.output


def test_wait_without_targets_still_reports_needs_attention(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """沒有 submitted／ended，但上次留下 needs_attention：不能當成成功，印 resolve 指令、
    退出碼 1。"""
    stuck = _stuck(database)
    built = _use(monkeypatch, _closing([]))

    result = _invoke(["batches", "wait"])

    assert result.exit_code == 1
    assert str(stuck) in result.output
    assert "radar batches resolve" in result.output
    assert built == []


def test_wait_exits_1_while_a_result_is_left_waiting_to_resume(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第一輪寫表頭失敗（run 改 llm_result_pending），第二輪其他項目收完、批次 imported：那一項
    仍沒有表頭，wait 不能以 0 結束，要印論文 ID 與接續方式。"""
    submission_id = submitted_batch(database, batch_setup())
    items = list(by_task(database, submission_id).values())
    real = ai_results.record_batch_llm_result
    calls = {"n": 0}

    def broken_once(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("寫表頭壞了")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(llm_gateway, "record_batch_llm_result", broken_once)
    _use(monkeypatch, _closing([succeeded(item) for item in items]))
    slept = _sleeps(monkeypatch)

    result = _invoke(["batches", "wait"])

    assert result.exit_code == 1, result.output
    assert slept == [600]
    assert _status_of(db, submission_id) == "imported"
    assert "已結束 1 批、還在跑 0 批" in result.output
    assert "radar runs trial" in result.output
    assert str(items[0].key.paper_id) in result.output
    assert len(ledger(db)) == 3  # 接續不用再付錢：帳都在


def test_rerun_without_targets_still_reports_results_left_waiting_to_resume(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """確認審查 D3：第一次 wait 已經把批次收到 imported、因為等接續的結果以 1 結束；重跑時沒有
    submitted／ended，也不能以 0 結束，要再印論文 ID 與接續方式。同一篇同一個工作之後有更新的 run
    （例如 prompt 升版後重跑過）時，那筆接續不到，就不再列。"""
    submission_id = submitted_batch(database, batch_setup())
    items = list(by_task(database, submission_id).values())
    real = ai_results.record_batch_llm_result
    calls = {"n": 0}

    def broken_once(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("寫表頭壞了")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(llm_gateway, "record_batch_llm_result", broken_once)
    built = _use(monkeypatch, _closing([succeeded(item) for item in items]))
    _sleeps(monkeypatch)
    assert _invoke(["batches", "wait"]).exit_code == 1
    assert _status_of(db, submission_id) == "imported"
    opened = len(built)

    rerun = _invoke(["batches", "wait"])

    assert rerun.exit_code == 1, rerun.output
    assert "沒有要等的批次" in rerun.output
    assert "radar runs trial" in rerun.output
    assert str(items[0].key.paper_id) in rerun.output
    assert len(built) == opened  # 沒有目標：不建 provider

    newer = dataclasses.replace(items[0].key, prompt_version=items[0].key.prompt_version + 1)
    insert_run(db, newer, status="succeeded")

    after = _invoke(["batches", "wait"])

    assert after.exit_code == 0, after.output
    assert "radar runs trial" not in after.output
