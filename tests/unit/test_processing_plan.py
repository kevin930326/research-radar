"""`domain/processing.plan_for`（AD-30、ADR-0015）：四列逐列、由上往下的優先順序、只有空白的摘要、
`for_task`、`TaskPlan` 剛好一個有值。"""

import pytest

from radar.domain.ai_outputs import InputKind
from radar.domain.papers import PaperOrigin
from radar.domain.processing import (
    PaperKind,
    ProcessingFacts,
    ProcessingTask,
    SkipState,
    TaskPlan,
    paper_kind,
    plan_for,
)

ABSTRACT = "We study how teams adopt information systems."
NA = TaskPlan(None, SkipState.NOT_APPLICABLE)
NO_INPUT = TaskPlan(None, SkipState.NO_INPUT)
TITLE = TaskPlan(InputKind.TITLE, None)
FULL = TaskPlan(InputKind.ABSTRACT, None)


def _facts(
    *,
    origin: PaperOrigin = PaperOrigin.TRACKED,
    excluded_reason: str | None = None,
    abstract: str | None = ABSTRACT,
) -> ProcessingFacts:
    return ProcessingFacts(origin=origin, excluded_reason=excluded_reason, abstract=abstract)


def _row(facts: ProcessingFacts) -> dict[ProcessingTask, TaskPlan]:
    plan = plan_for(facts)
    return {task: plan.for_task(task) for task in ProcessingTask}


# --- AD-30 的四列 ---------------------------------------------------------------------------


def test_row_1_excluded_does_nothing() -> None:
    facts = _facts(excluded_reason="editorial")

    assert plan_for(facts).kind is PaperKind.EXCLUDED
    assert _row(facts) == {
        ProcessingTask.ZH_SUMMARY: NA,
        ProcessingTask.CLASSIFY: NA,
        ProcessingTask.EXTRACT_MODEL: NA,
        ProcessingTask.EMBED: NA,
    }


def test_row_2_manual_only_embeds_the_title() -> None:
    facts = _facts(origin=PaperOrigin.MANUAL)

    assert plan_for(facts).kind is PaperKind.MANUAL
    assert _row(facts) == {
        ProcessingTask.ZH_SUMMARY: NA,
        ProcessingTask.CLASSIFY: NA,
        ProcessingTask.EXTRACT_MODEL: NA,
        ProcessingTask.EMBED: TITLE,
    }


def test_row_3_with_abstract_does_everything() -> None:
    facts = _facts()

    assert plan_for(facts).kind is PaperKind.WITH_ABSTRACT
    assert _row(facts) == {
        ProcessingTask.ZH_SUMMARY: FULL,
        ProcessingTask.CLASSIFY: FULL,
        ProcessingTask.EXTRACT_MODEL: FULL,
        ProcessingTask.EMBED: FULL,
    }


def test_row_4_without_abstract_classifies_and_embeds_the_title() -> None:
    facts = _facts(abstract=None)

    assert plan_for(facts).kind is PaperKind.NO_ABSTRACT
    assert _row(facts) == {
        ProcessingTask.ZH_SUMMARY: NO_INPUT,
        ProcessingTask.CLASSIFY: TITLE,
        ProcessingTask.EXTRACT_MODEL: NO_INPUT,
        ProcessingTask.EMBED: TITLE,
    }


# --- 優先順序與邊界 -------------------------------------------------------------------------


@pytest.mark.parametrize("origin", list(PaperOrigin))
@pytest.mark.parametrize("abstract", [ABSTRACT, None])
def test_excluded_wins_over_everything(origin: PaperOrigin, abstract: str | None) -> None:
    assert paper_kind(_facts(origin=origin, excluded_reason="erratum", abstract=abstract)) is (
        PaperKind.EXCLUDED
    )


@pytest.mark.parametrize("abstract", [ABSTRACT, None])
def test_manual_wins_over_abstract(abstract: str | None) -> None:
    assert paper_kind(_facts(origin=PaperOrigin.MANUAL, abstract=abstract)) is PaperKind.MANUAL


@pytest.mark.parametrize("origin", [PaperOrigin.TRACKED, PaperOrigin.EXPANSION, PaperOrigin.IMPORT])
def test_scope_and_origin_other_than_manual_do_not_matter(origin: PaperOrigin) -> None:
    assert paper_kind(_facts(origin=origin)) is PaperKind.WITH_ABSTRACT
    assert paper_kind(_facts(origin=origin, abstract=None)) is PaperKind.NO_ABSTRACT


@pytest.mark.parametrize("blank", ["", "   ", "\n\t", "\u3000"])
def test_blank_abstract_counts_as_no_abstract(blank: str) -> None:
    plan = plan_for(_facts(abstract=blank))

    assert plan.kind is PaperKind.NO_ABSTRACT
    assert plan.for_task(ProcessingTask.ZH_SUMMARY) == NO_INPUT


def test_for_task_accepts_the_string_value_and_rejects_unknown_tasks() -> None:
    plan = plan_for(_facts())

    assert plan.for_task("zh_summary") == FULL  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="merge_judge"):
        plan.for_task("merge_judge")  # type: ignore[arg-type]


def test_every_plan_lists_all_four_tasks_and_cannot_be_changed() -> None:
    for facts in (
        _facts(excluded_reason="x"),
        _facts(origin=PaperOrigin.MANUAL),
        _facts(),
        _facts(abstract=None),
    ):
        plan = plan_for(facts)
        assert set(plan.tasks) == set(ProcessingTask)
        with pytest.raises(TypeError):
            plan.tasks[ProcessingTask.EMBED] = NA  # type: ignore[index]


def test_task_plan_needs_exactly_one_of_input_kind_and_skip() -> None:
    with pytest.raises(ValueError, match="剛好一個"):
        TaskPlan(None, None)
    with pytest.raises(ValueError, match="剛好一個"):
        TaskPlan(InputKind.TITLE, SkipState.NO_INPUT)
    assert TITLE.runs
    assert not NO_INPUT.runs


def test_processing_tasks_use_the_llm_task_codes() -> None:
    assert {task.value for task in ProcessingTask} == {
        "zh_summary",
        "classify",
        "extract_model",
        "embed",
    }
