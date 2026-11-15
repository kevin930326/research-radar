"""`services/trial_runs.py`（Story 2.10）對真的 PostgreSQL：

- 每一項都經閘門（`derivation_run`／`llm_call`／表頭有記），重跑全部 `skipped`、列數與帳不變。
- 兩個執行緒同時跑同一篇：每組鍵只呼叫模型一次（閘門的 advisory lock＋部分唯一索引）。
- 跑到一半丟例外或 Ctrl-C，再跑只做沒做完的，做完的不重付。
- 熔斷時 `deferred_budget`、沒有摘要時 `no_input`、暫時性失敗與金鑰錯、非預期例外。
- 抽樣只抽調整組，seed 與名單寫進日誌；兩個資料庫（paper_id 不同）同一個 DOI 同組、抽到同一批。

模型一律用依 schema 回覆的替身（`tests/trial_support.py`），不連網路；論文與回覆都是自己編的。
"""

import threading
import uuid
from collections.abc import Iterator

import pytest
from structlog.testing import capture_logs

from radar.domain.ai_outputs import LlmTask
from radar.domain.eval_split import EvalSplit
from radar.domain.llm import LlmConfigurationError, LlmTimeoutError
from radar.domain.permissions import PermissionDeniedError
from radar.domain.trial_runs import (
    TrialItem,
    TrialItemStatus,
    TrialReport,
    TrialSelection,
    parse_trial_targets,
)
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.services import trial_runs
from radar.services.prompt_loader import current_prompt_version
from radar.services.trial_runs import (
    TrialPapersNotFoundError,
    TrialSetup,
    estimate_trial,
    resolve_trial_papers,
    run_trial,
    sample_trial_papers,
)
from tests.batch_support import NOW
from tests.database import TestDatabase, create_database, drop_database, migrate
from tests.llm_support import budget_config, create_paper, execute, insert_ledger, table_counts
from tests.trial_support import (
    WAIT,
    SchemaReplyProvider,
    banking_paper,
    doi_in,
    good_replies,
    reply,
    trial_setup,
)

SYSTEM = SystemViewer()


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def second_db() -> Iterator[TestDatabase]:
    database = create_database(with_extensions=True)
    try:
        migrate(database)
        yield database
    finally:
        drop_database(database)


def _select(database: Database, *targets: str) -> TrialSelection:
    return resolve_trial_papers(SYSTEM, database, parse_trial_targets(list(targets)))


def _run(
    database: Database,
    provider: SchemaReplyProvider,
    selection: TrialSelection,
    setup: TrialSetup | None = None,
    **kwargs: object,
) -> TrialReport:
    return run_trial(
        SYSTEM,
        database,
        provider,
        setup or trial_setup(),
        selection,
        clock=lambda: NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def _statuses(report: TrialReport) -> list[tuple[uuid.UUID, LlmTask, TrialItemStatus]]:
    return [(item.paper_id, item.task, item.status) for item in report.items]


def test_every_item_goes_through_the_gateway_and_rerun_skips(
    db: TestDatabase, database: Database
) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    selection = _select(database, str(paper))
    provider = SchemaReplyProvider(good_replies())
    seen: list[TrialItem] = []

    report = _run(database, provider, selection, on_item=seen.append)

    assert [item.status for item in report.items] == [TrialItemStatus.ACTIVE] * 3
    assert seen == list(report.items)
    assert all(item.run_id is not None and item.cost_usd for item in report.items)
    assert report.cost_usd == sum(item.cost_usd or 0 for item in report.items)
    assert report.exit_code == 0
    counts = table_counts(db)
    assert (counts["derivation_run"], counts["llm_call"]) == (3, 3)
    assert (counts["zh_summary"], counts["classification"], counts["research_model"]) == (1, 1, 1)
    setup = trial_setup()
    runs = execute(db, "SELECT task, prompt_version, model, call_mode FROM derivation_run")
    assert sorted(runs) == sorted(
        (
            task.value,
            current_prompt_version(task),
            setup.gateway.models.task_model(task).model,
            "interactive",
        )
        for task in (LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL)
    )

    again = SchemaReplyProvider(good_replies())
    rerun = _run(database, again, selection)

    assert [item.status for item in rerun.items] == [TrialItemStatus.SKIPPED] * 3
    assert again.total_calls == 0
    assert rerun.cost_usd == 0
    assert table_counts(db) == counts


def test_estimate_matches_what_runs(db: TestDatabase, database: Database) -> None:
    with_abstract = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    no_abstract = banking_paper(database, doi_in(EvalSplit.TUNING, "1", skip=1), 2, abstract=None)
    setup = trial_setup()
    selection = _select(database, str(with_abstract), str(no_abstract))

    estimate = estimate_trial(SYSTEM, database, setup, selection)

    assert [(item.paper_id, item.task) for item in estimate.items] == [
        (with_abstract, LlmTask.ZH_SUMMARY),
        (with_abstract, LlmTask.CLASSIFY),
        (with_abstract, LlmTask.EXTRACT_MODEL),
        (no_abstract, LlmTask.CLASSIFY),
    ]
    assert all(item.max_cost_usd > 0 and item.system_chars > 0 for item in estimate.items)
    assert table_counts(db)["derivation_run"] == 0

    report = _run(database, SchemaReplyProvider(good_replies()), selection, estimate=estimate)

    assert _statuses(report)[3:] == [
        (no_abstract, LlmTask.ZH_SUMMARY, TrialItemStatus.NO_INPUT),
        (no_abstract, LlmTask.CLASSIFY, TrialItemStatus.ACTIVE),
        (no_abstract, LlmTask.EXTRACT_MODEL, TrialItemStatus.NO_INPUT),
    ]
    actual = sum(item.cost_usd or 0 for item in report.items)
    assert actual <= estimate.max_cost_usd
    assert report.items[0].plan == estimate.items[0]


def test_two_concurrent_trials_call_each_key_once(db: TestDatabase) -> None:
    first_db = Database(db.app_url, connect_timeout=5, pool_size=2)
    other_db = Database(db.app_url, connect_timeout=5, pool_size=2)
    paper = banking_paper(first_db, doi_in(EvalSplit.TUNING, "1"), 1)
    selection = _select(first_db, str(paper))
    provider = SchemaReplyProvider(good_replies(), gated=True)
    reports: list[TrialReport] = []
    errors: list[BaseException] = []

    def worker(database: Database) -> None:
        try:
            reports.append(_run(database, provider, selection))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(d,)) for d in (first_db, other_db)]
    for thread in threads:
        thread.start()
    # 一個卡在第一個工作的呼叫，另一個略過它、卡在下一個工作：兩個都在等模型時才放行。
    deadline = threading.Event()
    for _ in range(int(WAIT * 10)):
        if provider.total_calls >= 2:
            break
        deadline.wait(0.1)
    assert provider.total_calls >= 2
    provider.release.set()
    for thread in threads:
        thread.join(WAIT)

    assert errors == []
    assert len(reports) == 2
    assert dict(provider.calls) == {task: 1 for task in provider.calls}
    assert sum(provider.calls.values()) == 3
    counts = table_counts(db)
    assert (counts["derivation_run"], counts["llm_call"]) == (3, 3)
    by_task: dict[LlmTask, list[TrialItemStatus]] = {}
    for report in reports:
        for item in report.items:
            by_task.setdefault(item.task, []).append(item.status)
    for statuses in by_task.values():
        assert sorted(statuses) == sorted([TrialItemStatus.ACTIVE, TrialItemStatus.SKIPPED])


def test_ctrl_c_midway_then_rerun_does_not_pay_twice(db: TestDatabase, database: Database) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    selection = _select(database, str(paper))
    replies = good_replies()
    replies[LlmTask.CLASSIFY] = KeyboardInterrupt()
    interrupted = SchemaReplyProvider(replies)

    with pytest.raises(KeyboardInterrupt):
        _run(database, interrupted, selection)

    assert table_counts(db)["llm_call"] == 1
    (failed,) = execute(db, "SELECT failure_reason FROM derivation_run WHERE task = 'classify'")
    assert failed == ("llm_gateway_error",)

    rerun_provider = SchemaReplyProvider(good_replies())
    rerun = _run(database, rerun_provider, selection)

    assert [item.status for item in rerun.items] == [
        TrialItemStatus.SKIPPED,
        TrialItemStatus.ACTIVE,
        TrialItemStatus.ACTIVE,
    ]
    assert rerun_provider.calls[LlmTask.ZH_SUMMARY] == 0
    counts = table_counts(db)
    assert (counts["llm_call"], counts["derivation_run"]) == (3, 4)


def test_unexpected_error_marks_the_item_and_continues(
    db: TestDatabase, database: Database
) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    selection = _select(database, str(paper))
    replies = good_replies()
    replies[LlmTask.CLASSIFY] = RuntimeError("替身故意出錯")

    with capture_logs() as logs:
        report = _run(database, SchemaReplyProvider(replies), selection)

    assert [item.status for item in report.items] == [
        TrialItemStatus.ACTIVE,
        TrialItemStatus.ERROR,
        TrialItemStatus.ACTIVE,
    ]
    assert report.items[1].reason == "RuntimeError"
    assert report.exit_code == 1
    assert "替身故意出錯" not in repr(logs)

    rerun_provider = SchemaReplyProvider(good_replies())
    rerun = _run(database, rerun_provider, selection)
    assert [item.status for item in rerun.items][1] is TrialItemStatus.ACTIVE
    assert dict(rerun_provider.calls) == {LlmTask.CLASSIFY: 1}


def test_transient_failure_is_reported_and_others_continue(
    db: TestDatabase, database: Database
) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    replies = good_replies()
    replies[LlmTask.ZH_SUMMARY] = LlmTimeoutError("逾時")

    report = _run(database, SchemaReplyProvider(replies), _select(database, str(paper)))

    assert report.items[0].status is TrialItemStatus.TRANSIENT_FAILURE
    assert report.items[0].reason == "llm_timeout"
    assert [item.status for item in report.items[1:]] == [TrialItemStatus.ACTIVE] * 2
    assert report.exit_code == 1


def test_configuration_error_stops_and_marks_the_rest_not_run(
    db: TestDatabase, database: Database
) -> None:
    first = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    second = banking_paper(database, doi_in(EvalSplit.TUNING, "1", skip=1), 2)
    replies = good_replies()
    replies[LlmTask.CLASSIFY] = LlmConfigurationError("HTTP 401", status=401)
    provider = SchemaReplyProvider(replies)

    report = _run(database, provider, _select(database, str(first), str(second)))

    assert [item.status for item in report.items] == [
        TrialItemStatus.ACTIVE,
        TrialItemStatus.ERROR,
        *[TrialItemStatus.NOT_RUN] * 4,
    ]
    assert (report.items[1].reason, report.items[1].http_status) == ("llm_configuration", 401)
    assert report.stopped_by_configuration
    assert report.exit_code == 1
    assert provider.total_calls == 2


def test_breaker_defers_every_call(db: TestDatabase, database: Database) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    insert_ledger(db, paper, LlmTask.QA_ANSWER, "50", NOW)
    before = table_counts(db)
    provider = SchemaReplyProvider(good_replies())

    report = _run(database, provider, _select(database, str(paper)))

    assert [item.status for item in report.items] == [TrialItemStatus.DEFERRED_BUDGET] * 3
    assert provider.total_calls == 0
    assert table_counts(db) == before
    assert report.exit_code == 0


def test_task_budget_only_defers_that_task(db: TestDatabase, database: Database) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    insert_ledger(db, paper, LlmTask.CLASSIFY, "1", NOW)
    setup = trial_setup(budget_config(classify="1"))

    report = _run(
        database, SchemaReplyProvider(good_replies()), _select(database, str(paper)), setup
    )

    assert [item.status for item in report.items] == [
        TrialItemStatus.ACTIVE,
        TrialItemStatus.DEFERRED_BUDGET,
        TrialItemStatus.ACTIVE,
    ]


def test_excluded_paper_is_not_applicable(db: TestDatabase, database: Database) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    execute(db, "UPDATE paper SET excluded_reason = 'editors_comments' WHERE id = %s", paper)
    provider = SchemaReplyProvider(good_replies())

    report = _run(database, provider, _select(database, str(paper)))

    assert [item.status for item in report.items] == [TrialItemStatus.NOT_APPLICABLE] * 3
    assert provider.total_calls == 0


def test_deleted_paper_is_not_found(db: TestDatabase, database: Database) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    selection = _select(database, str(paper))
    execute(db, "DELETE FROM paper_abstract WHERE paper_id = %s", paper)
    execute(db, "DELETE FROM external_id WHERE paper_id = %s", paper)
    execute(db, "DELETE FROM paper WHERE id = %s", paper)

    report = _run(database, SchemaReplyProvider(good_replies()), selection)

    assert [item.status for item in report.items] == [TrialItemStatus.NOT_FOUND] * 3


def test_diagnostics_and_rejected_output(db: TestDatabase, database: Database) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    replies = good_replies()
    replies[LlmTask.EXTRACT_MODEL] = reply('{"not": "a research model"}')

    report = _run(
        database,
        SchemaReplyProvider(replies),
        _select(database, str(paper)),
        diagnostics=True,
        show_output=True,
    )

    zh, _classify, model = report.items
    assert zh.diagnostics is not None
    assert (zh.diagnostics.input_tokens, zh.diagnostics.output_tokens) == (900, 120)
    assert zh.diagnostics.raw_response is not None
    assert isinstance(zh.output, dict)
    assert not zh.output_is_raw
    assert model.status is TrialItemStatus.REJECTED
    assert model.reason == "llm_output_invalid"
    assert (model.output, model.output_is_raw) == ('{"not": "a research model"}', True)
    assert report.rejected_by_reason() == {"llm_output_invalid": 1}


def test_resolve_dedups_and_reports_missing(database: Database) -> None:
    doi = doi_in(EvalSplit.TEST, "1")
    paper = banking_paper(database, doi, 1)

    selection = _select(database, f"https://doi.org/{doi.upper()}", str(paper))

    assert [(p.paper_id, p.dois, p.split) for p in selection.papers] == [
        (paper, (doi,), EvalSplit.TEST)
    ]
    missing_id = uuid.UUID("01900000-0000-7000-8000-00000000abcd")
    with pytest.raises(TrialPapersNotFoundError) as caught:
        _select(database, str(paper), "10.1/missing", str(missing_id))
    assert caught.value.missing == ("10.1/missing", str(missing_id))


def test_sample_only_draws_tuning_and_logs_seed(db: TestDatabase, database: Database) -> None:
    tuning = [banking_paper(database, doi_in(EvalSplit.TUNING, "1", skip=n), n) for n in range(4)]
    test = [banking_paper(database, doi_in(EvalSplit.TEST, "1", skip=n), 10 + n) for n in range(4)]
    no_doi = create_paper(database, doi="10.1/placeholder", openalex_id="W77777")
    execute(db, "DELETE FROM external_id WHERE paper_id = %s AND scheme = 'doi'", no_doi)
    no_abstract = banking_paper(database, doi_in(EvalSplit.TUNING, "1", skip=10), 20, abstract=None)

    with capture_logs() as logs:
        selection = sample_trial_papers(SYSTEM, database, 3, seed=7)

    picked = [paper.paper_id for paper in selection.papers]
    assert len(picked) == 3
    assert set(picked) <= set(tuning)
    assert not set(picked) & {*test, no_doi, no_abstract}
    assert selection.sample is not None
    assert (selection.sample.seed, selection.sample.eligible) == (7, 4)
    [entry] = [entry for entry in logs if entry["event"] == "試跑抽樣"]
    assert entry["seed"] == 7
    assert entry["papers"] == [
        {"paper_id": str(paper.paper_id), "dois": list(paper.dois)} for paper in selection.papers
    ]

    again = sample_trial_papers(SYSTEM, database, 3, seed=7)
    assert [paper.paper_id for paper in again.papers] == picked
    everything = sample_trial_papers(SYSTEM, database, 20, seed=7)
    assert {paper.paper_id for paper in everything.papers} == set(tuning)
    assert table_counts(db)["derivation_run"] == 0


def test_two_databases_agree_on_split_and_sample(
    db: TestDatabase, database: Database, second_db: TestDatabase
) -> None:
    other = Database(second_db.app_url, connect_timeout=5, pool_size=2)
    tuning = [doi_in(EvalSplit.TUNING, "3", skip=n) for n in range(5)]
    test = doi_in(EvalSplit.TEST, "3")
    # 兩邊建立的順序不同，paper_id 也不同。
    for n, doi in enumerate([*tuning, test]):
        banking_paper(database, doi, n)
    for n, doi in enumerate(reversed([*tuning, test])):
        banking_paper(other, doi, n)

    here = _select(database, test.upper(), *tuning)
    there = _select(other, f"https://doi.org/{test}", *tuning)

    assert [p.paper_id for p in here.papers] != [p.paper_id for p in there.papers]
    assert [(p.dois, p.split) for p in here.papers] == [(p.dois, p.split) for p in there.papers]
    assert here.papers[0].split is EvalSplit.TEST
    sample_here = sample_trial_papers(SYSTEM, database, 3, seed=11)
    sample_there = sample_trial_papers(SYSTEM, other, 3, seed=11)
    assert [p.dois for p in sample_here.papers] == [p.dois for p in sample_there.papers]


def test_paper_with_any_test_doi_is_test_and_never_sampled(
    db: TestDatabase, database: Database
) -> None:
    tuning = [banking_paper(database, doi_in(EvalSplit.TUNING, "4", skip=n), n) for n in range(3)]
    tuning_doi = doi_in(EvalSplit.TUNING, "4", skip=3)
    test_doi = doi_in(EvalSplit.TEST, "4")
    mixed = banking_paper(database, tuning_doi, 3)
    execute(
        db,
        "INSERT INTO external_id (paper_id, scheme, value_norm) VALUES (%s, 'doi', %s)",
        mixed,
        test_doi,
    )

    (resolved,) = _select(database, str(mixed)).papers
    assert resolved.split is EvalSplit.TEST
    assert set(resolved.dois) == {tuning_doi, test_doi}
    for seed in range(5):
        sample = sample_trial_papers(SYSTEM, database, 10, seed=seed)
        assert {paper.paper_id for paper in sample.papers} == set(tuning)
        assert sample.sample is not None
        assert sample.sample.eligible == 3


def test_sampling_pages_give_the_same_result(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(7):
        banking_paper(database, doi_in(EvalSplit.TUNING, "5", skip=n), n)
    banking_paper(database, doi_in(EvalSplit.TEST, "5"), 7)
    unpaged = sample_trial_papers(SYSTEM, database, 4, seed=3)

    monkeypatch.setattr(trial_runs, "PAGE_SIZE", 2)
    paged = sample_trial_papers(SYSTEM, database, 4, seed=3)

    assert paged == unpaged
    assert paged.sample is not None
    assert paged.sample.eligible == 7


def test_member_viewer_cannot_run_trials(db: TestDatabase, database: Database) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    selection = _select(database, str(paper))
    admin = MemberViewer(member_id=uuid.uuid4(), is_admin=True)
    before = table_counts(db)
    provider = SchemaReplyProvider(good_replies())

    with pytest.raises(PermissionDeniedError):
        resolve_trial_papers(admin, database, parse_trial_targets([str(paper)]))
    with pytest.raises(PermissionDeniedError):
        sample_trial_papers(admin, database, 1, seed=1)
    with pytest.raises(PermissionDeniedError):
        estimate_trial(admin, database, trial_setup(), selection)
    with pytest.raises(PermissionDeniedError):
        run_trial(admin, database, provider, trial_setup(), selection, clock=lambda: NOW)

    assert provider.total_calls == 0
    assert table_counts(db) == before
