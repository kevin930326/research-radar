"""`radar runs trial` 審查後的修正（Story 2.10、Codex 主審查第 1、2、3、4 條）的 domain 部分：

- 第 1 條：最壞花費＝單次上限 × adapter 每個請求最多送幾次（`attempts`，含第一次）；確認畫面與
  `--verbose` 標明「含重試的最壞情況」；adapter 讀得到自己的 `attempts`。
- 第 2 條：`input_changed`（確認後輸入變了）退出碼 1、在總表列出。
- 第 3 條：名單檔（`--save-ids` 寫、`--ids-file` 讀）。
- 第 4 條：總表分清「這次呼叫模型」與「沒有呼叫模型」（拒收率的分母）。

都不碰資料庫、不連網路；金額與論文 ID 都是自己編的。
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from radar.adapters.anthropic import AnthropicLlmProvider
from radar.config import load_settings
from radar.domain.ai_outputs import LlmTask
from radar.domain.budget import build_budget_status, month_window
from radar.domain.eval_split import EvalSplit, split_for_doi
from radar.domain.trial_runs import (
    MAX_TRIAL_PAPERS,
    TrialArgumentError,
    TrialEstimate,
    TrialItem,
    TrialItemStatus,
    TrialPlanItem,
    TrialReport,
    format_trial_ids,
    parse_trial_ids_file,
    render_trial_estimate,
    render_trial_item,
    render_trial_summary,
    trial_paper,
)
from radar.entrypoints.bootstrap import build_llm_provider
from tests.llm_support import budget_config

PAPER = uuid.UUID("01900000-0000-7000-8000-000000000001")
OTHER = uuid.UUID("01900000-0000-7000-8000-000000000002")
RUN = uuid.UUID("01900000-0000-7000-8000-0000000000aa")
FAKE_KEY = "sk-ant-FAKE-worst-case-key"


def _plan(cost: str, attempts: int = 1) -> TrialPlanItem:
    return TrialPlanItem(
        PAPER, LlmTask.CLASSIFY, Decimal(cost), 1200, 512, 900, 300, 400, attempts=attempts
    )


def _tuning_doi() -> str:
    for n in range(1000):
        doi = f"10.6/w{n}"
        if split_for_doi(doi) is EvalSplit.TUNING:
            return doi
    raise AssertionError("找不到調整組的 DOI")


def _item(status: TrialItemStatus, **changes: object) -> TrialItem:
    values: dict[str, object] = {"paper_id": PAPER, "task": LlmTask.CLASSIFY, "status": status}
    values.update(changes)
    return TrialItem(**values)  # type: ignore[arg-type]


# --- 第 1 條：含重試的最壞花費 --------------------------------------------------------------


@pytest.mark.parametrize("attempts", [1, 3, 5])
def test_worst_cost_is_single_call_times_attempts(attempts: int) -> None:
    plan = _plan("0.000512", attempts)
    estimate = TrialEstimate((plan, plan), attempts=attempts)

    assert plan.worst_cost_usd == Decimal("0.000512") * attempts
    assert estimate.max_cost_usd == Decimal("0.001024") * attempts


def test_estimate_line_says_worst_case_with_retries() -> None:
    plan = _plan("0.000512", 3)
    status = build_budget_status(
        budget_config(), month_window(datetime(2026, 11, 15, tzinfo=UTC)), {}
    )

    lines = render_trial_estimate(TrialEstimate((plan, plan), attempts=3), status)

    assert lines[0] == (
        "預估：會交給閘門 2 項，費用上限 0.003072 美元（含重試的最壞情況：每項最多送 3 次；"
        "已經有結果、會略過的也算進去）"
    )


def test_verbose_item_shows_single_and_worst_cost() -> None:
    item = _item(TrialItemStatus.ACTIVE, plan=_plan("0.000512", 3))

    lines = render_trial_item(1, 3, item, verbose=True, show_output=False)

    assert lines[-1] == (
        "    費用上限：單次 0.000512 美元；含重試的最壞情況 0.001536 美元（最多送 3 次）"
    )


@pytest.mark.parametrize("attempts", [0, -1, True, 1.0])
def test_attempts_must_be_a_positive_int(attempts: object) -> None:
    with pytest.raises(ValueError, match="attempts"):
        _plan("0.1", attempts)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="attempts"):
        TrialEstimate((), attempts=attempts)  # type: ignore[arg-type]


def test_estimate_items_must_share_attempts() -> None:
    with pytest.raises(ValueError, match="attempts"):
        TrialEstimate((_plan("0.1", 2),), attempts=3)


def test_adapter_exposes_its_attempts() -> None:
    provider = AnthropicLlmProvider(
        FAKE_KEY,
        timeout=5,
        attempts=4,
        retry_wait_initial=0.1,
        retry_wait_max=1,
        retry_wait_jitter=0,
        retry_after_max=10,
    )
    try:
        assert provider.attempts == 4
    finally:
        provider.close()


def test_built_provider_attempts_come_from_settings() -> None:
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql://radar@db:5432/radar",
            "RADAR_ANTHROPIC_API_KEY": FAKE_KEY,
            "RADAR_ANTHROPIC_ATTEMPTS": "6",
        }
    )
    with build_llm_provider(settings) as provider:
        assert provider.attempts == 6


# --- 第 2 條：確認後輸入變了 ----------------------------------------------------------------


def test_input_changed_exits_1() -> None:
    report = TrialReport((), (_item(TrialItemStatus.ACTIVE), _item(TrialItemStatus.INPUT_CHANGED)))
    assert report.exit_code == 1


def test_summary_lists_input_changed_items() -> None:
    papers = (trial_paper(PAPER, [_tuning_doi()]),)
    items = (
        _item(TrialItemStatus.INPUT_CHANGED, task=LlmTask.ZH_SUMMARY),
        _item(TrialItemStatus.ACTIVE, run_id=RUN, cost_usd=Decimal("0.0001")),
        _item(TrialItemStatus.INPUT_CHANGED, task=LlmTask.EXTRACT_MODEL),
    )

    lines = render_trial_summary(TrialReport(papers, items), verbose=False)

    assert lines[2].split() == [str(PAPER), "調整組", "input_changed", "active", "input_changed"]
    start = lines.index("確認後輸入變了、沒有呼叫模型：2 項（重跑同一個指令會重新預估、確認）")
    assert lines[start + 1 : start + 3] == [
        f"  {PAPER} zh_summary",
        f"  {PAPER} extract_model",
    ]


# --- 第 4 條：這次呼叫模型的與沒有呼叫的 ----------------------------------------------------


def test_called_model_only_counts_new_runs() -> None:
    assert _item(TrialItemStatus.ACTIVE, run_id=RUN).called_model
    assert _item(TrialItemStatus.TRANSIENT_FAILURE, run_id=RUN).called_model
    assert not _item(TrialItemStatus.ACTIVE, run_id=RUN, resumed=True).called_model
    assert not _item(TrialItemStatus.SKIPPED).called_model
    assert not _item(TrialItemStatus.DEFERRED_BUDGET).called_model
    assert not _item(TrialItemStatus.NOT_RUN).called_model


def test_summary_separates_called_from_not_called() -> None:
    papers = (trial_paper(PAPER, [_tuning_doi()]), trial_paper(OTHER, [_tuning_doi()]))
    items = (
        _item(TrialItemStatus.ACTIVE, task=LlmTask.ZH_SUMMARY, run_id=RUN),
        _item(TrialItemStatus.REJECTED, reason="llm_refusal", run_id=RUN),
        _item(TrialItemStatus.SKIPPED, task=LlmTask.EXTRACT_MODEL),
        _item(TrialItemStatus.REJECTED, paper_id=OTHER, reason="llm_output_invalid", run_id=RUN),
        _item(
            TrialItemStatus.ACTIVE,
            paper_id=OTHER,
            task=LlmTask.ZH_SUMMARY,
            run_id=RUN,
            resumed=True,
        ),
        _item(TrialItemStatus.DEFERRED_BUDGET, paper_id=OTHER, task=LlmTask.EXTRACT_MODEL),
    )
    report = TrialReport(papers, items)

    assert len(report.called_items()) == 3
    assert report.not_called_by_status() == {"deferred_budget": 1, "resumed": 1, "skipped": 1}
    lines = render_trial_summary(report, verbose=False)
    assert (
        "這次呼叫模型：3 項（其中拒收 2 項）；沒有呼叫模型：3 項"
        "（deferred_budget 1、resumed 1、skipped 1）"
    ) in lines


def test_summary_when_everything_was_called() -> None:
    papers = (trial_paper(PAPER, [_tuning_doi()]),)
    items = tuple(
        _item(TrialItemStatus.ACTIVE, task=task, run_id=RUN)
        for task in LlmTask
        if task in (LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL)
    )
    lines = render_trial_summary(TrialReport(papers, items), verbose=False)
    assert "這次呼叫模型：3 項（其中拒收 0 項）；沒有呼叫模型：0 項" in lines


# --- 第 3 條：名單檔 ------------------------------------------------------------------------


def test_ids_file_round_trips_in_order() -> None:
    papers = (trial_paper(OTHER, [_tuning_doi()]), trial_paper(PAPER, []))

    text = format_trial_ids(papers)

    assert text == f"{OTHER}\n{PAPER}\n"
    assert parse_trial_ids_file(text).targets == (OTHER, PAPER)


def test_ids_file_skips_blank_and_comment_lines_and_accepts_dois() -> None:
    text = f"# seed 20261116\n\n  {PAPER}  \n# 備註\nhttps://doi.org/10.1/ABC\n{PAPER}\n"

    assert parse_trial_ids_file(text).targets == (PAPER, "10.1/abc")


@pytest.mark.parametrize("text", ["", "\n\n", "# 只有註解\n"])
def test_empty_ids_file_is_an_error(text: str) -> None:
    with pytest.raises(TrialArgumentError, match="名單檔裡沒有論文"):
        parse_trial_ids_file(text)


def test_unreadable_line_in_ids_file_is_an_error() -> None:
    with pytest.raises(TrialArgumentError, match="看不懂"):
        parse_trial_ids_file(f"{PAPER}\nnot-a-doi\n")


def test_ids_file_has_the_same_limit() -> None:
    text = "".join(f"{uuid.UUID(int=n)}\n" for n in range(MAX_TRIAL_PAPERS + 1))
    with pytest.raises(TrialArgumentError, match="一次最多"):
        parse_trial_ids_file(text)
