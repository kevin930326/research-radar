"""`radar runs trial` 的 domain 部分（`domain/trial_runs.py`，Story 2.10）：參數解析、抽樣只抽
調整組、seed 固定結果固定、和輸入順序無關、項目狀態、回應形狀的消毒、輸出文字的跳脫與要印的行。
"""

import random
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from radar.domain.ai_outputs import LlmTask
from radar.domain.budget import build_budget_status, month_window
from radar.domain.eval_split import EvalSplit, split_for_doi
from radar.domain.papers import PaperOrigin
from radar.domain.processing import ProcessingFacts, SkipState
from radar.domain.trial_runs import (
    MAX_TRIAL_PAPERS,
    SHOW_OUTPUT_MAX_CHARS,
    LlmCallDiagnostics,
    TrialArgumentError,
    TrialCandidate,
    TrialEstimate,
    TrialItem,
    TrialItemStatus,
    TrialPaper,
    TrialPlanItem,
    TrialReport,
    TrialSample,
    TrialSelection,
    describe_response_shape,
    escape_for_terminal,
    parse_trial_targets,
    render_trial_estimate,
    render_trial_item,
    render_trial_selection,
    render_trial_summary,
    sample_tuning_papers,
    trial_paper,
)
from radar.services.llm_gateway import GatewayOutcome
from tests.llm_support import budget_config

WITH_ABSTRACT = ProcessingFacts(PaperOrigin.TRACKED, None, "We study adoption.")
NO_ABSTRACT = ProcessingFacts(PaperOrigin.TRACKED, None, None)
EXCLUDED = ProcessingFacts(PaperOrigin.TRACKED, "editors_comments", "Editorial.")
MANUAL = ProcessingFacts(PaperOrigin.MANUAL, None, "Abstract.")
PAPER = uuid.UUID("01900000-0000-7000-8000-000000000001")


def _dois(split: EvalSplit, count: int, prefix: str = "5") -> list[str]:
    found: list[str] = []
    n = 0
    while len(found) < count:
        doi = f"10.{prefix}/s{n}"
        if split_for_doi(doi) is split:
            found.append(doi)
        n += 1
    return found


def _candidate(doi: str | None, facts: ProcessingFacts = WITH_ABSTRACT) -> TrialCandidate:
    return TrialCandidate.from_facts(uuid.uuid4(), [] if doi is None else [doi], facts)


# --- 參數 ---------------------------------------------------------------------------------


def test_parses_ids_and_dois_in_order_and_dedups() -> None:
    other = uuid.UUID("01900000-0000-7000-8000-000000000002")
    targets = parse_trial_targets(
        [
            str(PAPER),
            "https://doi.org/10.1/ABC",
            f" {str(PAPER).upper()} ",
            "10.1/abc",
            "doi:10.2/x",
            str(other),
        ]
    )
    assert targets.targets == (PAPER, "10.1/abc", "10.2/x", other)
    assert targets.paper_ids == (PAPER, other)
    assert targets.dois == ("10.1/abc", "10.2/x")


def test_unreadable_targets_are_listed() -> None:
    with pytest.raises(TrialArgumentError, match="看不懂") as caught:
        parse_trial_targets(["10.1/abc", "hello", "11.2/x"])
    assert "'hello'" in str(caught.value)
    assert "'11.2/x'" in str(caught.value)


def test_no_targets_is_an_error() -> None:
    with pytest.raises(TrialArgumentError):
        parse_trial_targets([])


def test_at_most_100_after_dedup() -> None:
    hundred = [f"10.1/p{n}" for n in range(MAX_TRIAL_PAPERS)]
    assert len(parse_trial_targets([*hundred, "10.1/P0"]).targets) == MAX_TRIAL_PAPERS
    with pytest.raises(TrialArgumentError, match="最多 100"):
        parse_trial_targets([*hundred, "10.1/p100"])


# --- 抽樣 ---------------------------------------------------------------------------------


def test_candidate_needs_all_three_tasks() -> None:
    (tuning,) = _dois(EvalSplit.TUNING, 1)
    assert _candidate(tuning).runs_all_tasks
    assert _candidate(tuning).eligible
    for facts in (NO_ABSTRACT, EXCLUDED, MANUAL):
        candidate = _candidate(tuning, facts)
        assert not candidate.runs_all_tasks
        assert not candidate.eligible


def test_candidate_dois_are_normalized_and_sorted() -> None:
    candidate = TrialCandidate.from_facts(PAPER, ["10.9/B", "10.9/a", "10.9/b"], WITH_ABSTRACT)
    assert candidate.dois == ("10.9/a", "10.9/b")


def test_sample_only_draws_from_tuning_split_with_all_tasks() -> None:
    tuning = _dois(EvalSplit.TUNING, 6)
    test = _dois(EvalSplit.TEST, 6)
    eligible = [_candidate(doi) for doi in tuning[:4]]
    others = [
        *(_candidate(doi) for doi in test),
        _candidate(None),
        _candidate(tuning[4], NO_ABSTRACT),
        _candidate(tuning[5], EXCLUDED),
        # 一個調整組加一個測試組的 DOI：算測試組。
        TrialCandidate.from_facts(uuid.uuid4(), [tuning[0], test[0]], WITH_ABSTRACT),
    ]
    result = sample_tuning_papers([*others, *eligible], 20, seed=7)
    assert result.eligible == 4
    assert {candidate.paper_id for candidate in result.selected} == {
        candidate.paper_id for candidate in eligible
    }


def test_same_seed_same_result_and_order_does_not_matter() -> None:
    candidates = [_candidate(doi) for doi in _dois(EvalSplit.TUNING, 30)]
    first = sample_tuning_papers(candidates, 10, seed=42)
    shuffled = list(candidates)
    random.Random(3).shuffle(shuffled)
    again = sample_tuning_papers(shuffled, 10, seed=42)
    assert [c.dois for c in again.selected] == [c.dois for c in first.selected]
    assert len(first.selected) == 10
    other_seed = sample_tuning_papers(candidates, 10, seed=43)
    assert [c.dois for c in other_seed.selected] != [c.dois for c in first.selected]


def test_adding_papers_keeps_relative_order() -> None:
    dois = _dois(EvalSplit.TUNING, 40)
    base = sample_tuning_papers([_candidate(doi) for doi in dois[:20]], 20, seed=9)
    more = sample_tuning_papers([_candidate(doi) for doi in dois], 40, seed=9)
    kept = [c.dois for c in more.selected if c.dois[0] in dois[:20]]
    assert kept == [c.dois for c in base.selected]


def test_sample_takes_all_when_short() -> None:
    candidates = [_candidate(doi) for doi in _dois(EvalSplit.TUNING, 3)]
    result = sample_tuning_papers(candidates, 20, seed=1)
    assert (len(result.selected), result.eligible) == (3, 3)


def test_sample_size_is_checked() -> None:
    with pytest.raises(TrialArgumentError):
        sample_tuning_papers([], 0, seed=1)
    with pytest.raises(TrialArgumentError):
        sample_tuning_papers([], MAX_TRIAL_PAPERS + 1, seed=1)


def test_trial_paper_computes_split() -> None:
    (tuning,) = _dois(EvalSplit.TUNING, 1)
    assert trial_paper(PAPER, [tuning.upper()]).split is EvalSplit.TUNING
    assert trial_paper(PAPER, []) == TrialPaper(PAPER, (), EvalSplit.TEST)


# --- 狀態 ---------------------------------------------------------------------------------


def test_item_status_covers_gateway_outcomes_and_skip_states() -> None:
    values = {status.value for status in TrialItemStatus}
    assert {outcome.value for outcome in GatewayOutcome} <= values
    assert {state.value for state in SkipState} <= values


def _item(status: TrialItemStatus, **changes: object) -> TrialItem:
    values: dict[str, object] = {"paper_id": PAPER, "task": LlmTask.CLASSIFY, "status": status}
    values.update(changes)
    return TrialItem(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (TrialItemStatus.ACTIVE, 0),
        (TrialItemStatus.SKIPPED, 0),
        (TrialItemStatus.REJECTED, 0),
        (TrialItemStatus.DEFERRED_BUDGET, 0),
        (TrialItemStatus.NO_INPUT, 0),
        (TrialItemStatus.TRANSIENT_FAILURE, 1),
        (TrialItemStatus.ERROR, 1),
        (TrialItemStatus.NOT_RUN, 1),
    ],
)
def test_exit_code(status: TrialItemStatus, code: int) -> None:
    report = TrialReport((), (_item(TrialItemStatus.ACTIVE), _item(status)))
    assert report.exit_code == code


def test_report_cost_and_rejections() -> None:
    report = TrialReport(
        (),
        (
            _item(TrialItemStatus.ACTIVE, cost_usd=Decimal("0.000150")),
            _item(TrialItemStatus.REJECTED, reason="llm_output_invalid", cost_usd=Decimal("1E-6")),
            _item(TrialItemStatus.REJECTED, reason="llm_refusal"),
            _item(TrialItemStatus.REJECTED, reason="llm_output_invalid"),
            _item(TrialItemStatus.SKIPPED),
        ),
    )
    assert report.cost_usd == Decimal("0.000151")
    assert report.rejected_by_reason() == {"llm_output_invalid": 2, "llm_refusal": 1}


# --- 形狀與跳脫 ---------------------------------------------------------------------------


def test_response_shape_keeps_only_names() -> None:
    raw = {
        "id": "msg_1",
        "content": [{"type": "thinking", "thinking": "秘密"}, {"type": "text", "text": "秘密"}],
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "stop_details": {"type": "refusal", "category": "cyber", "explanation": "秘密"},
        "bad key!": 1,
        "x" * 65: 2,
    }
    shape = describe_response_shape(raw)
    assert shape is not None
    assert shape.top_keys == ("<?>", "<?>", "content", "id", "stop_details", "usage")
    assert shape.content_types == ("thinking", "text")
    assert shape.usage_keys == ("input_tokens", "output_tokens")
    assert shape.stop_details_keys == ("category", "explanation", "type")
    assert (shape.stop_details_type, shape.stop_details_category) == ("refusal", "cyber")
    assert "秘密" not in repr(shape)


def test_response_shape_sanitizes_odd_types() -> None:
    raw = {
        "content": [{"type": "\x1b[31mred"}, "not a block", {"no": "type"}],
        "usage": "not a mapping",
        "stop_details": {"type": "日本語", "category": None},
    }
    shape = describe_response_shape(raw)
    assert shape is not None
    assert shape.content_types == ("<?>", "<?>", "<?>")
    assert shape.usage_keys == ()
    assert (shape.stop_details_type, shape.stop_details_category) == ("<?>", None)
    assert describe_response_shape(None) is None
    no_details = describe_response_shape({"content": "x", "stop_details": None})
    assert no_details is not None
    assert no_details.stop_details_keys is None
    assert no_details.content_types == ()


def test_escape_for_terminal_escapes_controls_and_truncates() -> None:
    text = "a\x1bb\u009bc‮d e\tf中文"
    shown = escape_for_terminal(text)
    assert shown == '"a\\u001bb\\u009bc\\u202ed\\u2028e\\tf中文"'
    assert escape_for_terminal({"k": "值"}) == '{"k": "值"}'
    long = escape_for_terminal("x" * (SHOW_OUTPUT_MAX_CHARS + 10))
    assert len(long) == SHOW_OUTPUT_MAX_CHARS + len("…（截斷）")
    assert long.endswith("…（截斷）")


def test_show_output_limit_fits_a_research_model_reply() -> None:
    assert SHOW_OUTPUT_MAX_CHARS == 20_000
    assert escape_for_terminal("字" * 10_000).endswith('"')


@pytest.mark.parametrize("char", ["\x1b", '"', "\\"], ids=["esc", "quote", "backslash"])
@pytest.mark.parametrize("shift", range(8))
def test_truncation_never_cuts_inside_an_escape(char: str, shift: int) -> None:
    # 讓跳脫序列落在截斷點附近的每一個位置：結果在「…（截斷）」之前一定是完整的序列。
    text = "x" * (SHOW_OUTPUT_MAX_CHARS - 8 + shift) + char * 5
    shown = escape_for_terminal(text)
    assert shown.endswith("…（截斷）")
    body = shown.removesuffix("…（截斷）")
    assert len(body) <= SHOW_OUTPUT_MAX_CHARS
    trailing = len(body) - len(body.rstrip("\\"))
    assert trailing % 2 == 0  # 沒有落單的反斜線
    assert not re.search(r"\\u[0-9a-f]{0,3}$", body)


# --- 要印的行 -----------------------------------------------------------------------------


def test_render_selection_marks_test_split_and_shortfall() -> None:
    (tuning,) = _dois(EvalSplit.TUNING, 1)
    other = uuid.UUID("01900000-0000-7000-8000-000000000002")
    selection = TrialSelection(
        (trial_paper(PAPER, [tuning]), trial_paper(other, [])),
        TrialSample(seed=7, requested=20, eligible=2),
    )
    lines = render_trial_selection(selection)
    assert lines[0] == "抽樣：seed 7，從調整組符合條件的 2 篇抽 20 篇"
    assert lines[1] == "調整組符合條件的只有 2 篇，少於 20 篇，全部抽出"
    assert lines[2] == "選到的論文（2 篇）："
    assert any(str(PAPER) in line and tuning in line and "調整組" in line for line in lines)
    assert any("（沒有 DOI）" in line and "測試組" in line for line in lines)
    assert lines[-1] == f"警告：{other} 是測試組，結果不要拿來改提示詞"


def test_render_estimate_includes_budget_and_breaker() -> None:
    plan = TrialPlanItem(PAPER, LlmTask.CLASSIFY, Decimal("0.000512"), 1200, 512, 900, 300, 400)
    window = month_window(datetime(2026, 11, 15, tzinfo=UTC))
    status = build_budget_status(budget_config(), window, {LlmTask.ZH_SUMMARY: Decimal(50)})
    lines = render_trial_estimate(TrialEstimate((plan, plan)), status)
    assert lines[0].startswith("預估：會交給閘門 2 項，費用上限 0.001024 美元")
    assert lines[1] == (
        "本月（2026-11，台北時間）：已花 50.0000、Batch 預留 0.0000、離熔斷線 0.0000"
        "（熔斷線 50.0000）"
    )
    assert lines[2].startswith("已熔斷")


def test_render_item_plain_verbose_and_output() -> None:
    plan = TrialPlanItem(PAPER, LlmTask.CLASSIFY, Decimal("0.000512"), 1200, 512, 900, 300, 400)
    diagnostics = LlmCallDiagnostics(
        input_tokens=800,
        output_tokens=128,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        stop_reason="end_turn",
        price_tier="standard",
        response_id="msg_ok",
        request_id="req bad\x1b",
        raw_response={"content": [{"type": "text", "text": "秘密"}], "usage": {"input_tokens": 1}},
        output_text="秘密",
        output_reject_reason=None,
    )
    item = _item(
        TrialItemStatus.REJECTED,
        reason="llm_output_invalid",
        cost_usd=Decimal("0.000120"),
        elapsed_seconds=1.234,
        plan=plan,
        diagnostics=diagnostics,
        output="{\n\x1b}",
        output_is_raw=True,
    )
    plain = render_trial_item(2, 6, item, verbose=False, show_output=False)
    assert plain == [f"[2/6] {PAPER} classify rejected （llm_output_invalid） 0.000120 美元"]
    verbose = render_trial_item(2, 6, item, verbose=True, show_output=False)
    assert verbose[1] == (
        "    耗時 1.23 秒；輸入 800／估計上限 1200 tokens；輸出 128／max_tokens 512；"
        "快取寫入 0、讀取 0"
    )
    assert verbose[2] == (
        "    stop_reason end_turn；price_tier standard；response_id msg_ok；request_id <?>"
    )
    assert verbose[3] == (
        "    回應形狀：頂層鍵 [content, usage]；content 區塊 [text]；usage 鍵 [input_tokens]；"
        "stop_details 無"
    )
    assert verbose[4] == "    送出字元：系統提示 900、內容 300、schema 400"
    assert all("秘密" not in line for line in verbose)
    shown = render_trial_item(2, 6, item, verbose=False, show_output=True)
    assert shown[1] == '    模型原文（拒收）："{\\n\\u001b}"'


def test_render_summary() -> None:
    (tuning,) = _dois(EvalSplit.TUNING, 1)
    papers = (trial_paper(PAPER, [tuning]),)
    items = (
        _item(TrialItemStatus.NO_INPUT, task=LlmTask.ZH_SUMMARY),
        _item(TrialItemStatus.REJECTED, reason="llm_refusal", cost_usd=Decimal("0.0001")),
        _item(TrialItemStatus.ERROR, task=LlmTask.EXTRACT_MODEL, reason="RuntimeError"),
    )
    lines = render_trial_summary(TrialReport(papers, items), verbose=False)
    assert lines[0] == "結果（1 篇 × 3 種工作）："
    assert lines[1].split() == ["論文", "ID", "組別", "zh_summary", "classify", "extract_model"]
    assert lines[2].split() == [str(PAPER), "調整組", "no_input", "rejected", "error"]
    assert "拒收：llm_refusal 1 項" in lines
    assert "這次費用：0.000100 美元（接續與略過是 0）" in lines
    assert "有項目出錯：這次費用可能少算，以 radar costs 為準" in lines


def test_render_estimate_warns_when_over_remaining_or_task_budget_is_used() -> None:
    plan = TrialPlanItem(PAPER, LlmTask.CLASSIFY, Decimal("0.6"), 1200, 512, 900, 300, 400)
    window = month_window(datetime(2026, 11, 15, tzinfo=UTC))
    status = build_budget_status(
        budget_config(classify="1"),
        window,
        {LlmTask.ZH_SUMMARY: Decimal("48.5"), LlmTask.CLASSIFY: Decimal("1")},
    )
    lines = render_trial_estimate(TrialEstimate((plan, plan)), status)
    assert lines[2:] == [
        "預估上限超過離熔斷線的金額：後面的項目可能變成 deferred_budget",
        "classify 的功能預算已用完：這項會是 deferred_budget",
    ]
    roomy = build_budget_status(budget_config(), window, {})
    assert len(render_trial_estimate(TrialEstimate((plan,)), roomy)) == 2


def _diagnostics(output_tokens: int, raw: dict[str, object] | None) -> LlmCallDiagnostics:
    return LlmCallDiagnostics(
        input_tokens=800,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        stop_reason="refusal",
        price_tier="standard",
        response_id=None,
        request_id=None,
        raw_response=raw,
        output_text=None,
        output_reject_reason="llm_refusal",
    )


def test_verbose_stop_details_and_summary_ratios() -> None:
    (tuning,) = _dois(EvalSplit.TUNING, 1)
    plan = TrialPlanItem(PAPER, LlmTask.CLASSIFY, Decimal("0.0005"), 1200, 512, 900, 300, 400)
    raw: dict[str, object] = {
        "content": [],
        "stop_details": {"type": "refusal", "category": "cyber", "explanation": "秘密"},
    }
    refused = _item(
        TrialItemStatus.REJECTED,
        reason="llm_refusal",
        elapsed_seconds=2.5,
        plan=plan,
        diagnostics=_diagnostics(384, raw),
    )
    lines = render_trial_item(1, 3, refused, verbose=True, show_output=False)
    assert lines[2] == "    stop_reason refusal；price_tier standard；response_id —；request_id —"
    assert lines[3] == (
        "    回應形狀：頂層鍵 [content, stop_details]；content 區塊 []；usage 鍵 []；"
        "stop_details 鍵 [category, explanation, type]、type refusal、category cyber"
    )
    null_category = _diagnostics(1, {"stop_details": {"type": "refusal", "category": None}})
    item = _item(TrialItemStatus.REJECTED, diagnostics=null_category)
    assert render_trial_item(1, 1, item, verbose=True, show_output=False)[3].endswith(
        "stop_details 鍵 [category, type]、type refusal、category null"
    )

    faster = _item(
        TrialItemStatus.ACTIVE, elapsed_seconds=1.0, plan=plan, diagnostics=_diagnostics(128, None)
    )
    report = TrialReport((trial_paper(PAPER, [tuning]),), (faster, refused))
    summary = render_trial_summary(report, verbose=True)
    assert "zh_summary：最長耗時 —；最大 output／max_tokens —" in summary
    assert "classify：最長耗時 2.50 秒；最大 output／max_tokens 75%" in summary


def test_show_output_notes_for_skipped_and_rejected_without_text() -> None:
    skipped = _item(TrialItemStatus.SKIPPED)
    assert render_trial_item(1, 1, skipped, verbose=False, show_output=True)[1:] == [
        "    這一版 prompt 已經有結果，這次沒有新的輸出（要並排比較得先升 prompt 版本）"
    ]
    rejected = _item(TrialItemStatus.REJECTED, reason="llm_refusal")
    assert render_trial_item(1, 1, rejected, verbose=False, show_output=True)[1:] == [
        "    沒有存下模型原文（拒答、截斷或存不進資料庫）"
    ]
    assert render_trial_item(1, 1, skipped, verbose=False, show_output=False)[1:] == []
    assert (
        render_trial_item(
            1, 1, _item(TrialItemStatus.DEFERRED_BUDGET), verbose=False, show_output=True
        )[1:]
        == []
    )
