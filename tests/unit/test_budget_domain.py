"""`domain/budget.py`：台北月份的邊界、到達即擋、功能預算、只檢索、金額格式與
`radar costs` 的輸出。"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from radar.domain.ai_outputs import LlmTask
from radar.domain.budget import (
    BUDGET_TIMEZONE,
    BudgetConfig,
    BudgetStatus,
    MonthWindow,
    build_budget_status,
    format_usd,
    month_window,
    render_cost_report,
)

TAIPEI = BUDGET_TIMEZONE
NOVEMBER = MonthWindow(
    start=datetime(2026, 11, 1, tzinfo=TAIPEI), end=datetime(2026, 12, 1, tzinfo=TAIPEI)
)


def _config(**budgets: str | None) -> BudgetConfig:
    task_budgets: dict[str, str | None] = {task.value: None for task in LlmTask}
    task_budgets.update(budgets)
    return BudgetConfig.model_validate(
        {
            "workspace_limit_usd": "70",
            "monthly_breaker_usd": "50",
            "task_budgets_usd": task_budgets,
        }
    )


def _status(costs: Mapping[LlmTask, str], **budgets: str | None) -> BudgetStatus:
    return build_budget_status(
        _config(**budgets), NOVEMBER, {task: Decimal(value) for task, value in costs.items()}
    )


# --- 月份 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 11, 1, 0, 0, tzinfo=TAIPEI),
        datetime(2026, 11, 30, 23, 59, 59, 999999, tzinfo=TAIPEI),
        datetime(2026, 11, 15, 12, 0, tzinfo=TAIPEI),
    ],
)
def test_month_window_first_and_last_moment(now: datetime) -> None:
    window = month_window(now)

    assert window == NOVEMBER
    assert window.start.utcoffset() == timedelta(hours=8)
    assert window.label == "2026-11"


def test_utc_and_taipei_in_different_months_counts_taipei() -> None:
    # UTC 10/31 16:00＝台北 11/01 00:00。
    assert month_window(datetime(2026, 10, 31, 16, 0, tzinfo=UTC)) == NOVEMBER
    # UTC 10/31 15:59:59＝台北 10/31 23:59:59，還是 10 月。
    october = month_window(datetime(2026, 10, 31, 15, 59, 59, tzinfo=UTC))
    assert october.start == datetime(2026, 10, 1, tzinfo=TAIPEI)
    assert october.end == NOVEMBER.start


def test_other_timezones_are_converted() -> None:
    new_york = timezone(timedelta(hours=-5))
    # 紐約 11/30 12:00＝UTC 11/30 17:00＝台北 12/01 01:00。
    december = month_window(datetime(2026, 11, 30, 12, 0, tzinfo=new_york))
    assert december.start == datetime(2026, 12, 1, tzinfo=TAIPEI)
    assert december.end == datetime(2027, 1, 1, tzinfo=TAIPEI)
    assert december.label == "2026-12"


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="時區"):
        month_window(datetime(2026, 11, 1))


# --- 判定 ---------------------------------------------------------------------------------


def test_breaker_reached_blocks_every_task_and_means_retrieval_only() -> None:
    status = _status({LlmTask.ZH_SUMMARY: "30", LlmTask.CLASSIFY: "20.000000"})

    assert status.total_usd == Decimal("50")
    assert status.breaker_tripped
    assert not any(status.allows(task) for task in LlmTask)
    assert status.retrieval_only
    assert status.remaining_usd == Decimal(0)


def test_just_below_breaker_allows() -> None:
    status = _status({LlmTask.ZH_SUMMARY: "49.999999"})

    assert not status.breaker_tripped
    assert all(status.allows(task) for task in LlmTask)
    assert not status.retrieval_only
    assert status.remaining_usd == Decimal("0.000001")


def test_over_breaker_remaining_is_zero() -> None:
    status = _status({LlmTask.ZH_SUMMARY: "50.16"})

    assert status.breaker_tripped
    assert status.remaining_usd == Decimal(0)


def test_task_budget_reached_blocks_only_that_task() -> None:
    status = _status({LlmTask.ZH_SUMMARY: "1"}, zh_summary="1")

    assert status.task_exhausted(LlmTask.ZH_SUMMARY)
    assert not status.allows(LlmTask.ZH_SUMMARY)
    assert status.allows(LlmTask.CLASSIFY)
    assert not status.breaker_tripped
    assert not status.retrieval_only


def test_task_budget_just_below_allows() -> None:
    status = _status({LlmTask.ZH_SUMMARY: "0.999999"}, zh_summary="1")

    assert not status.task_exhausted(LlmTask.ZH_SUMMARY)
    assert status.allows(LlmTask.ZH_SUMMARY)


def test_null_budget_is_only_bound_by_breaker() -> None:
    status = _status({LlmTask.CLASSIFY: "49"})

    assert not status.task_exhausted(LlmTask.CLASSIFY)
    assert status.allows(LlmTask.CLASSIFY)


def test_zero_budget_blocks_that_task_from_the_start() -> None:
    status = _status({}, merge_judge="0")

    assert not status.allows(LlmTask.MERGE_JUDGE)
    assert status.allows(LlmTask.ZH_SUMMARY)


def test_qa_budget_reached_means_retrieval_only_but_others_run() -> None:
    status = _status({LlmTask.QA_ANSWER: "2"}, qa_answer="2")

    assert status.retrieval_only
    assert not status.breaker_tripped
    assert all(status.allows(task) for task in LlmTask if task is not LlmTask.QA_ANSWER)


def test_missing_tasks_count_as_zero() -> None:
    status = _status({LlmTask.ZH_SUMMARY: "1.5"})

    assert set(status.by_task) == set(LlmTask)
    assert status.by_task[LlmTask.EVAL_JUDGE] == Decimal(0)
    assert status.total_usd == Decimal("1.5")


@pytest.mark.parametrize("bad", [Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity"), 1.5, 1])
def test_bad_costs_are_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="zh_summary"):
        build_budget_status(_config(), NOVEMBER, {LlmTask.ZH_SUMMARY: bad})  # type: ignore[dict-item]


def test_unknown_task_in_costs_is_rejected() -> None:
    with pytest.raises(ValueError, match="不認得"):
        build_budget_status(_config(), NOVEMBER, {"embed": Decimal(1)})  # type: ignore[dict-item]


# --- 格式與輸出 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("0", "0.0000"),
        ("1.23456", "1.2346"),
        ("1.23455", "1.2346"),  # half-up（不是銀行家捨入）
        ("1.23445", "1.2345"),
        ("0.00005", "0.0001"),
        ("0.000049", "0.0000"),
        ("50", "50.0000"),
        ("48.26544", "48.2654"),
        ("123456.5", "123456.5000"),
    ],
)
def test_format_usd(amount: str, expected: str) -> None:
    assert format_usd(Decimal(amount)) == expected


def test_render_cost_report() -> None:
    status = _status({LlmTask.ZH_SUMMARY: "1.23456", LlmTask.CLASSIFY: "0.5"}, qa_answer="3")

    assert render_cost_report(status) == [
        "2026-11 費用（台北時間，美元）",
        "",
        "工作             費用  功能預算  可呼叫",
        "zh_summary     1.2346      未設  是",
        "classify       0.5000      未設  是",
        "extract_model  0.0000      未設  是",
        "merge_judge    0.0000      未設  是",
        "qa_answer      0.0000    3.0000  是",
        "eval_judge     0.0000      未設  是",
        "合計           1.7346",
        "",
        "熔斷線：50.0000",
        "離熔斷線：48.2654",
        "只檢索：否",
    ]


def test_render_cost_report_when_tripped() -> None:
    status = _status({LlmTask.ZH_SUMMARY: "50.00001"})
    lines = render_cost_report(status)

    assert "zh_summary     50.0000      未設  否" in lines
    assert "classify        0.0000      未設  否" in lines
    assert lines[-3:] == ["熔斷線：50.0000", "離熔斷線：0.0000", "只檢索：是"]


# --- Batch 預留（Codex 主審查第 1 條、ADR-0041） --------------------------------------------------


def _reserved_status(
    costs: Mapping[LlmTask, str], reserved: Mapping[LlmTask, str], **budgets: str | None
) -> BudgetStatus:
    return build_budget_status(
        _config(**budgets),
        NOVEMBER,
        {task: Decimal(value) for task, value in costs.items()},
        {task: Decimal(value) for task, value in reserved.items()},
    )


def test_reservations_count_toward_the_breaker() -> None:
    # 已花 10、Batch 預留 30：互動再花 10 就到 50，之後全部擋下（不是只看帳本的 20）。
    before = _reserved_status({LlmTask.ZH_SUMMARY: "10"}, {LlmTask.CLASSIFY: "30"})
    after = _reserved_status({LlmTask.ZH_SUMMARY: "20"}, {LlmTask.CLASSIFY: "30"})

    assert before.allows(LlmTask.ZH_SUMMARY)
    assert before.remaining_usd == Decimal(10)
    assert (after.total_usd, after.reserved_usd, after.committed_usd) == (
        Decimal(20),
        Decimal(30),
        Decimal(50),
    )
    assert not after.ledger_breaker_tripped
    assert after.breaker_tripped
    assert not any(after.allows(task) for task in LlmTask)
    assert after.retrieval_only
    assert after.remaining_usd == Decimal(0)


def test_reservations_count_toward_their_task_budget_only() -> None:
    status = _reserved_status(
        {LlmTask.CLASSIFY: "0.3"}, {LlmTask.CLASSIFY: "0.7"}, classify="1", zh_summary="1"
    )

    assert status.task_committed(LlmTask.CLASSIFY) == Decimal(1)
    assert status.task_exhausted(LlmTask.CLASSIFY)
    assert not status.allows(LlmTask.CLASSIFY)
    assert status.allows(LlmTask.ZH_SUMMARY)
    assert not status.breaker_tripped


def test_over_committed_only_when_strictly_above() -> None:
    # Batch 送出前的最後確認：規劃剛好排滿（等於）不算超過，不會擋住自己。
    exact = _reserved_status({LlmTask.QA_ANSWER: "20"}, {LlmTask.CLASSIFY: "30"}, classify="30")
    over = _reserved_status({LlmTask.QA_ANSWER: "20.000001"}, {LlmTask.CLASSIFY: "30"})
    task_over = _reserved_status(
        {LlmTask.CLASSIFY: "0.000001"}, {LlmTask.CLASSIFY: "30"}, classify="30"
    )

    assert exact.breaker_tripped  # 到達即擋：互動已經不准
    assert not exact.over_committed([LlmTask.CLASSIFY])
    assert over.over_committed([LlmTask.CLASSIFY])
    assert task_over.over_committed([LlmTask.CLASSIFY])
    # 別的工作超過自己的功能預算，不算這批（這批只有 zh_summary）。
    assert not task_over.over_committed([LlmTask.ZH_SUMMARY])


def test_no_reservations_behaves_like_the_ledger_alone() -> None:
    status = _status({LlmTask.ZH_SUMMARY: "49.999999"})

    assert status.reserved_usd == Decimal(0)
    assert set(status.reserved_by_task) == set(LlmTask)
    assert status.committed_usd == status.total_usd
    assert status.ledger_breaker_tripped is status.breaker_tripped is False


@pytest.mark.parametrize("bad", [Decimal("-0.01"), Decimal("NaN"), 1])
def test_bad_reservations_are_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="classify 的預留"):
        build_budget_status(_config(), NOVEMBER, {}, {LlmTask.CLASSIFY: bad})  # type: ignore[dict-item]


def test_render_cost_report_shows_reservations() -> None:
    status = _reserved_status({LlmTask.ZH_SUMMARY: "10"}, {LlmTask.CLASSIFY: "30.5"})

    assert render_cost_report(status)[-4:] == [
        "熔斷線：50.0000",
        "Batch 預留：30.5000",
        "離熔斷線：9.5000",
        "只檢索：否",
    ]
