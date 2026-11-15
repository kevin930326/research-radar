"""管理者 AI 指令的純函式（Story 2.12、ADR-0044）：重跑計畫的欄位檢查、最壞花費、退出碼、要印的字、
依來源移除的稽核事件、稽核事件的形狀、改版上限設定、摘要來源跟著輸入走。"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from radar.domain.admin_runs import (
    REMOVAL_TABLES,
    SOURCE_REMOVAL_ACTION,
    SOURCE_REMOVAL_TARGET,
    AiHistoryRow,
    PaperAiReport,
    RemovalBlock,
    RetryBlock,
    RetryItem,
    RetryItemStatus,
    RetryPlan,
    RetryPlanItem,
    RetryReport,
    RevisionEstimate,
    SourceAiCounts,
    SourceRemovalPreview,
    SourceRemovalResult,
    removal_blocks,
    render_paper_ai_report,
    render_removal_blocks,
    render_retry_plan,
    render_retry_report,
    render_revision_estimate,
    render_source_removal_preview,
    render_source_removal_result,
    source_removal_audit,
    terminal_text,
)
from radar.domain.ai_blocks import (
    AiBlock,
    AiBlockState,
    AiProduct,
    ClassificationFieldValue,
    ClassificationValue,
    Provenance,
    ZhSummaryValue,
)
from radar.domain.ai_outputs import (
    CurrentAbstract,
    InputKind,
    LlmTask,
    PaperLlmSource,
    ResultStatus,
    RunKey,
    RunStatus,
    abstract_source_usable,
    build_llm_input,
)
from radar.domain.audit import AuditActorRole, NewAuditEvent
from radar.domain.budget import build_budget_status, month_window
from radar.domain.llm_batches import LlmBatchConfig, PlanStop
from radar.domain.papers import AbstractSource
from radar.domain.policy import CloudPolicy
from radar.services.llm_gateway import GatewayOutcome
from tests.llm_support import budget_config

PAPER = uuid.UUID("01900000-0000-7000-8000-0000000000a1")
REJECTED = uuid.UUID("01900000-0000-7000-8000-0000000000b1")
RUN = uuid.UUID("01900000-0000-7000-8000-0000000000c1")
KEY = RunKey(PAPER, LlmTask.CLASSIFY, 1, "claude-haiku-5-5", "a" * 64)
WHEN = datetime(2026, 11, 15, 4, 0, tzinfo=UTC)


def _retryable(task: LlmTask = LlmTask.CLASSIFY, cost: str = "0.01") -> RetryPlanItem:
    return RetryPlanItem(
        task,
        key=KEY,
        input_kind=InputKind.ABSTRACT,
        rejected_id=REJECTED,
        reject_reason="schema_invalid",
        max_cost_usd=Decimal(cost),
    )


# --- 重跑計畫 ---------------------------------------------------------------------------------


def test_retryable_item_needs_every_field() -> None:
    assert _retryable().retryable
    for missing in ("key", "input_kind", "rejected_id", "max_cost_usd"):
        values: dict[str, Any] = {
            "key": KEY,
            "input_kind": InputKind.ABSTRACT,
            "rejected_id": REJECTED,
            "max_cost_usd": Decimal(1),
        }
        values[missing] = None
        with pytest.raises(ValueError, match="可以重跑"):
            RetryPlanItem(LlmTask.CLASSIFY, **values)


def test_blocked_item_carries_no_key() -> None:
    item = RetryPlanItem(LlmTask.CLASSIFY, block=RetryBlock.HAS_ACTIVE)
    assert not item.retryable
    with pytest.raises(ValueError, match="不帶鍵"):
        RetryPlanItem(LlmTask.CLASSIFY, block=RetryBlock.HAS_ACTIVE, key=KEY)
    with pytest.raises(ValueError, match="不帶鍵"):
        RetryPlanItem(LlmTask.CLASSIFY, block=RetryBlock.RUNNING, rejected_id=REJECTED)


def test_worst_case_cost_is_single_call_cap_times_attempts() -> None:
    plan = RetryPlan(
        PAPER,
        (
            _retryable(LlmTask.ZH_SUMMARY, "0.01"),
            RetryPlanItem(LlmTask.CLASSIFY, block=RetryBlock.HAS_ACTIVE),
            _retryable(LlmTask.EXTRACT_MODEL, "0.02"),
        ),
        attempts=3,
    )
    assert len(plan.retryable) == 2
    assert plan.max_cost_usd == Decimal("0.09")
    assert RetryPlan(PAPER, (), attempts=3).max_cost_usd == 0


def test_render_retry_plan_shows_reasons_estimate_and_budget() -> None:
    window = month_window(WHEN)
    status = build_budget_status(budget_config(), window, {})
    plan = RetryPlan(
        PAPER,
        (
            RetryPlanItem(LlmTask.ZH_SUMMARY, block=RetryBlock.NO_REJECTED, detail="active"),
            _retryable(LlmTask.CLASSIFY, "0.01"),
        ),
        attempts=2,
    )
    lines = render_retry_plan(plan, status)
    assert lines[0] == f"論文 {PAPER}"
    assert lines[1].startswith("  zh_summary：不能重跑：目前的輸入沒有被拒收的結果")
    assert lines[1].endswith("（active）")
    assert lines[2] == "  classify：會重跑（被拒收：schema_invalid；單次上限 0.0100 美元）"
    assert lines[3].startswith("預估：重跑 1 項，費用上限 0.0200 美元")
    assert "每項最多送 2 次" in lines[3]
    assert lines[4].startswith("本月（2026-11，台北時間）")
    assert not any(line.startswith("已熔斷") for line in lines)


def test_render_retry_plan_without_retryable_stops_after_reasons() -> None:
    status = build_budget_status(budget_config(), month_window(WHEN), {})
    plan = RetryPlan(PAPER, (RetryPlanItem(LlmTask.CLASSIFY, block=RetryBlock.HAS_ACTIVE),), 3)
    assert render_retry_plan(plan, status) == [
        f"論文 {PAPER}",
        "  classify：不能重跑：目前的輸入已經有有效結果",
    ]


def test_render_retry_plan_warns_when_breaker_tripped() -> None:
    status = build_budget_status(
        budget_config(), month_window(WHEN), {LlmTask.ZH_SUMMARY: Decimal(50)}
    )
    lines = render_retry_plan(RetryPlan(PAPER, (_retryable(),), 3), status)
    assert lines[-1].startswith("已熔斷")


def test_every_gateway_outcome_has_a_retry_status() -> None:
    # 閘門回的結果都要對得到重跑的狀態，不然模型付過錢之後轉換才丟 ValueError。
    assert {o.value for o in GatewayOutcome} <= {s.value for s in RetryItemStatus}


@pytest.mark.parametrize("status", sorted(RetryItemStatus))
def test_retry_exit_code_follows_failing_statuses(status: RetryItemStatus) -> None:
    report = RetryReport(PAPER, (RetryItem(LlmTask.CLASSIFY, status),))
    succeeded = {
        RetryItemStatus.ACTIVE,
        RetryItemStatus.CANDIDATE,
        RetryItemStatus.SUPERSEDED,
        RetryItemStatus.REJECTED,
        RetryItemStatus.SKIPPED,
    }
    assert report.exit_code == (0 if status in succeeded else 1)


def test_render_retry_report_lists_items_cost_and_skip_note() -> None:
    report = RetryReport(
        PAPER,
        (
            RetryItem(
                LlmTask.CLASSIFY, RetryItemStatus.ACTIVE, run_id=RUN, cost_usd=Decimal("0.002")
            ),
            RetryItem(LlmTask.ZH_SUMMARY, RetryItemStatus.SKIPPED),
            RetryItem(
                LlmTask.EXTRACT_MODEL,
                RetryItemStatus.ERROR,
                reason="llm_configuration",
                http_status=401,
            ),
        ),
    )
    lines = render_retry_report(report)
    assert lines[0] == f"  classify：active  run {RUN}"
    assert lines[1] == "  zh_summary：skipped"
    assert lines[2] == "  extract_model：error（llm_configuration） HTTP 401"
    assert lines[3] == "這次新記的費用：0.0020 美元"
    assert lines[4].startswith("skipped：")


# --- 查詢 -------------------------------------------------------------------------------------


def _history(
    task: LlmTask,
    header: ResultStatus | None,
    *,
    version: int = 1,
    is_eval: bool = False,
    run_status: RunStatus = RunStatus.SUCCEEDED,
) -> AiHistoryRow:
    return AiHistoryRow(
        run_id=uuid.uuid4(),
        task=task,
        run_status=run_status,
        failure_reason="transient" if run_status is RunStatus.FAILED else None,
        header_status=header,
        reject_reason="schema_invalid" if header is ResultStatus.REJECTED else None,
        model="claude-haiku-5-5",
        prompt_version=version,
        input_kind=InputKind.ABSTRACT,
        abstract_source=AbstractSource.OPENALEX,
        created_at=WHEN,
        is_eval=is_eval,
    )


def test_history_row_outcome_and_reason() -> None:
    rejected = _history(LlmTask.CLASSIFY, ResultStatus.REJECTED)
    assert (rejected.outcome, rejected.reason) == ("rejected", "schema_invalid")
    failed = _history(LlmTask.CLASSIFY, None, run_status=RunStatus.FAILED)
    assert (failed.outcome, failed.reason) == ("failed", "transient")
    active = _history(LlmTask.CLASSIFY, ResultStatus.ACTIVE)
    assert (active.outcome, active.reason) == ("active", None)


def _report(history: tuple[AiHistoryRow, ...]) -> PaperAiReport:
    provenance = Provenance("claude-haiku-5-5", WHEN, InputKind.ABSTRACT, False)
    human = Provenance(None, WHEN, None, True)
    field = ClassificationFieldValue(("is.topic",), True, 1, False)
    return PaperAiReport(
        PAPER,
        ("10.1000/x",),
        {
            AiProduct.ZH_SUMMARY: AiBlock(
                AiProduct.ZH_SUMMARY,
                AiBlockState.READY,
                ZhSummaryValue(uuid.uuid4(), "問題‮反轉", None, None, "發現", ()),
                provenance,
            ),
            AiProduct.CLASSIFICATION: AiBlock(
                AiProduct.CLASSIFICATION,
                AiBlockState.READY,
                ClassificationValue(field, None),
                human,
            ),
            AiProduct.RESEARCH_MODEL: AiBlock(AiProduct.RESEARCH_MODEL, AiBlockState.FAILED),
        },
        history,
    )


def test_active_prompt_version_ignores_eval_and_other_states() -> None:
    report = _report(
        (
            _history(LlmTask.ZH_SUMMARY, ResultStatus.SUPERSEDED, version=1),
            _history(LlmTask.ZH_SUMMARY, ResultStatus.ACTIVE, version=2),
            _history(LlmTask.ZH_SUMMARY, ResultStatus.ACTIVE, version=3, is_eval=True),
            _history(LlmTask.CLASSIFY, ResultStatus.REJECTED, version=4),
        )
    )
    assert report.active_prompt_version(LlmTask.ZH_SUMMARY) == 2
    assert report.active_prompt_version(LlmTask.CLASSIFY) is None


def test_render_paper_ai_report_shows_blocks_provenance_and_history() -> None:
    report = _report(
        (
            _history(LlmTask.ZH_SUMMARY, ResultStatus.ACTIVE, version=2),
            _history(LlmTask.EXTRACT_MODEL, ResultStatus.REJECTED),
            _history(LlmTask.CLASSIFY, ResultStatus.ACTIVE, is_eval=True),
        )
    )
    text = "\n".join(render_paper_ai_report(report))
    assert f"論文 {PAPER}（10.1000/x）" in text
    assert "中文重點：ready（有結果）" in text
    assert "模型 claude-haiku-5-5、prompt v2、看 abstract" in text
    # 控制字元（雙向文字控制）跳脫後才印。
    assert "‮" not in text
    assert "問題\\u202e反轉" in text
    assert "    方法：—" in text
    assert "分類：ready（有結果）" in text
    assert "來歷：人工（沒有 AI 值）" in text
    assert "主題：is.topic（人工值）" in text
    assert (
        "研究模型：failed（沒有產生成功（被拒收：radar runs retry 重跑；"
        "Batch 卡住：radar batches resolve））"
    ) in text
    assert "schema_invalid" in text
    assert "active（評估）" in text
    assert "openalex" in text


def test_render_paper_ai_report_without_history() -> None:
    lines = render_paper_ai_report(_report(()))
    assert lines[-1] == "  （沒有）"


def test_terminal_text_escapes_control_and_format_characters() -> None:
    assert terminal_text("a\x1b[31mb c​d") == "a\\u001b[31mb\\u2028c\\u200bd"
    assert terminal_text("中文 ok") == "中文 ok"


# --- 依來源移除 -------------------------------------------------------------------------------


def _counts() -> SourceAiCounts:
    return SourceAiCounts(
        headers={
            LlmTask.ZH_SUMMARY: {ResultStatus.ACTIVE: 2, ResultStatus.REJECTED: 1},
            LlmTask.CLASSIFY: {ResultStatus.SUPERSEDED: 1},
            LlmTask.EXTRACT_MODEL: {},
        },
        details={"classification_label": 3, "construct_mention": 0, "hypothesis": 0},
        unknown_source=4,
    )


def test_counts_by_table_cover_every_removal_table() -> None:
    counts = _counts()
    assert counts.total_headers == 4
    assert counts.by_table() == {
        "zh_summary": 3,
        "classification": 1,
        "classification_label": 3,
        "research_model": 0,
        "construct_mention": 0,
        "hypothesis": 0,
        "construct": 0,
        "embedding": 0,
    }
    assert tuple(counts.by_table()) == REMOVAL_TABLES


def test_removal_blocks() -> None:
    assert removal_blocks(0, 0) == ()
    assert removal_blocks(1, 0) == (RemovalBlock.RUNNING_RUNS,)
    assert removal_blocks(0, 2) == (RemovalBlock.CURRENT_ABSTRACTS,)
    assert removal_blocks(3, 2) == (RemovalBlock.RUNNING_RUNS, RemovalBlock.CURRENT_ABSTRACTS)
    preview = SourceRemovalPreview(AbstractSource.OPENALEX, _counts(), 1, 0)
    assert preview.blocks == (RemovalBlock.RUNNING_RUNS,)


def test_render_preview_lists_counts_and_unknown_source() -> None:
    lines = render_source_removal_preview(
        SourceRemovalPreview(AbstractSource.SEMANTIC_SCHOLAR, _counts(), 1, 2)
    )
    text = "\n".join(lines)
    assert lines[0].startswith("摘要來源 semantic_scholar 衍生的 AI 產出")
    assert "會刪：表頭 4 列；細項 classification_label 3、construct_mention 0、hypothesis 0" in text
    assert "還在跑、用這個來源的 run：1" in text
    assert "目前摘要仍是這個來源的論文：2" in text
    assert "來源不明的表頭（遷移前或摘要列已刪，不會刪）：4" in text


def test_render_removal_blocks_gives_next_steps() -> None:
    both = render_removal_blocks(
        AbstractSource.OPENALEX, (RemovalBlock.RUNNING_RUNS, RemovalBlock.CURRENT_ABSTRACTS)
    )
    assert both[0] == "沒有刪任何一列："
    assert len(both) == 3
    assert "openalex" in both[2]
    assert "先停用這個來源" in both[2]


def test_render_removal_result() -> None:
    empty = SourceRemovalResult(AbstractSource.OPENALEX, (), 0, 0)
    assert render_source_removal_result(empty) == [
        "摘要來源 openalex 沒有可刪的 AI 產出（沒有寫稽核）"
    ]
    audit = uuid.uuid4()
    done = SourceRemovalResult(
        AbstractSource.OPENALEX, (), 0, 0, {"zh_summary": 2, "classification": 1}, audit
    )
    lines = render_source_removal_result(done)
    assert lines[0].startswith("已刪除摘要來源 openalex 衍生的 AI 產出：zh_summary 2、")
    assert lines[1] == f"稽核事件：{audit}"


def test_source_removal_audit_records_only_source_and_counts() -> None:
    event = source_removal_audit(AbstractSource.UPLOADED_PDF, {"zh_summary": 2, "hypothesis": 1})
    assert event.actor_role is AuditActorRole.SYSTEM
    assert event.actor_member_id is None
    assert event.action == SOURCE_REMOVAL_ACTION
    assert event.target_type == SOURCE_REMOVAL_TARGET
    assert event.target_id is None
    assert dict(event.detail) == {
        "source": "uploaded_pdf",
        "counts": {
            table: {"zh_summary": 2, "hypothesis": 1}.get(table, 0) for table in REMOVAL_TABLES
        },
    }


@pytest.mark.parametrize(
    "deleted",
    [{"paper": 1}, {"zh_summary": -1}, {"zh_summary": 1.0}, {"zh_summary": True}],
)
def test_source_removal_audit_rejects_bad_counts(deleted: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"不認得的表|計數要是"):
        source_removal_audit(AbstractSource.OPENALEX, deleted)


# --- 稽核事件的形狀 ---------------------------------------------------------------------------


def _event(**change: Any) -> NewAuditEvent:
    values: dict[str, Any] = {
        "actor_role": AuditActorRole.SYSTEM,
        "actor_member_id": None,
        "action": "remove_source_ai_outputs",
        "target_type": "abstract_source",
        "target_id": None,
        "detail": {"source": "openalex"},
    }
    values.update(change)
    return NewAuditEvent(**values)


def test_audit_event_detail_json_is_sorted() -> None:
    event = _event(detail={"b": 1, "a": "摘要"})
    assert event.detail_json() == '{"a": "摘要", "b": 1}'
    member = _event(actor_role=AuditActorRole.ADMIN, actor_member_id=uuid.uuid4())
    assert member.actor_role is AuditActorRole.ADMIN


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"actor_member_id": uuid.uuid4()}, "成員 id"),
        ({"actor_role": AuditActorRole.MEMBER}, "成員 id"),
        ({"actor_role": "robot"}, "AuditActorRole"),
        ({"action": "Remove"}, "action 要是小寫識別字"),
        ({"action": "1remove"}, "action 要是小寫識別字"),
        ({"target_type": "abstract-source"}, "target_type 要是小寫識別字"),
        ({"detail": ["not", "an", "object"]}, "JSON 物件"),
        ({"detail": {"x": float("nan")}}, "NaN"),
        ({"detail": {"x": "a\x00b"}}, "NUL"),
        ({"detail": {1: "key"}}, "鍵要是字串"),
        ({"detail": {"x": object()}}, "寫得成 JSON"),
    ],
)
def test_bad_audit_events_are_rejected(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _event(**change)


# --- 改版預估與上限設定 -----------------------------------------------------------------------


def test_revision_estimate_totals_and_cap() -> None:
    estimate = RevisionEstimate((LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL), Decimal(5))
    estimate.for_task(LlmTask.CLASSIFY).rerun = 10
    estimate.for_task(LlmTask.CLASSIFY).max_cost_usd = Decimal(3)
    estimate.for_task(LlmTask.EXTRACT_MODEL).rerun = 5
    estimate.for_task(LlmTask.EXTRACT_MODEL).max_cost_usd = Decimal(2)
    assert estimate.reruns == 15
    assert estimate.max_cost_usd == Decimal(5)
    assert not estimate.exceeds_cap  # 剛好等於上限不算超過
    estimate.for_task(LlmTask.EXTRACT_MODEL).max_cost_usd = Decimal("2.0001")
    assert estimate.exceeds_cap
    lines = render_revision_estimate(estimate)
    assert any(line.startswith("分類表改版 → classify") for line in lines)
    assert any("上限 revision_rerun_max_usd：5.0000 美元" in line for line in lines)
    assert lines[-1].startswith("超過上限")
    assert PlanStop.RERUN_CAP_EXCEEDED.value in lines[-1]


def test_revision_estimate_under_cap_has_no_warning() -> None:
    estimate = RevisionEstimate((LlmTask.CLASSIFY,), Decimal(5))
    lines = render_revision_estimate(estimate)
    assert not lines[-1].startswith("超過上限")


_BASE: dict[str, Any] = {
    "weekly_planning_enabled": False,
    "max_requests_per_batch": 500,
    "month_start_hold_hours": 8,
}


def test_revision_cap_has_a_default_and_bounds() -> None:
    assert LlmBatchConfig.model_validate(_BASE).revision_rerun_max_usd == Decimal(5)
    for good in ("0", "50", "12.5"):
        config = LlmBatchConfig.model_validate({**_BASE, "revision_rerun_max_usd": good})
        assert config.revision_rerun_max_usd == Decimal(good)
    for bad in ("-0.01", "50.01", "NaN", "abc", 5, 5.0, None):
        # 只收字串（YAML 浮點數會失真），和其他金額設定一樣。
        with pytest.raises(ValidationError, match="revision_rerun_max_usd"):
            LlmBatchConfig.model_validate({**_BASE, "revision_rerun_max_usd": bad})


# --- 摘要來源跟著輸入走 -----------------------------------------------------------------------


def test_llm_input_carries_abstract_source_only_for_abstract_input() -> None:
    abstract = CurrentAbstract(uuid.uuid4(), AbstractSource.SEMANTIC_SCHOLAR, "We study things.")
    source = PaperLlmSource(PAPER, "A study of IS", abstract)
    assert build_llm_input(source, InputKind.ABSTRACT).abstract_source is (
        AbstractSource.SEMANTIC_SCHOLAR
    )
    assert build_llm_input(source, InputKind.TITLE).abstract_source is None


def test_abstract_source_usable_requires_the_same_current_non_blank_cleared_source() -> None:
    def paper(source: AbstractSource, content: str = "We study things.") -> PaperLlmSource:
        abstract = CurrentAbstract(uuid.uuid4(), source, content)
        return PaperLlmSource(PAPER, "A study of IS", abstract)

    policy = CloudPolicy()
    assert abstract_source_usable(paper(AbstractSource.OPENALEX), AbstractSource.OPENALEX, policy)
    assert not abstract_source_usable(
        paper(AbstractSource.SEMANTIC_SCHOLAR), AbstractSource.OPENALEX, policy
    )
    assert not abstract_source_usable(
        paper(AbstractSource.OPENALEX, "   "), AbstractSource.OPENALEX, policy
    )
    assert not abstract_source_usable(
        PaperLlmSource(PAPER, "A study of IS", None), AbstractSource.OPENALEX, policy
    )
    assert not abstract_source_usable(None, AbstractSource.OPENALEX, policy)
    # 上傳的 PDF 預設不放行送雲端。
    assert not abstract_source_usable(
        paper(AbstractSource.UPLOADED_PDF), AbstractSource.UPLOADED_PDF, policy
    )
