"""AI 區塊的信封（Story 2.7、`radar/domain/ai_blocks.py`）：八個狀態、`AiBlock` 的不變式（只有
`ready`／`empty` 帶來歷、只有 `ready` 帶值、值的型別對得上），以及沒有時間預估或進度的欄位。"""

import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from radar.domain.ai_blocks import (
    AiBlock,
    AiBlockSettings,
    AiBlockState,
    AiProduct,
    ClassificationFieldValue,
    ClassificationValue,
    ConstructValue,
    HypothesisValue,
    Provenance,
    ResearchModelValue,
    ZhSummaryValue,
    skip_block_state,
)
from radar.domain.ai_outputs import InputKind, LlmTask
from radar.domain.judgments import SummaryField
from radar.domain.llm_schemas import EvidenceType, SupportLevel
from radar.domain.processing import ProcessingTask, SkipState

AT = datetime(2026, 11, 15, 4, 0, tzinfo=UTC)
PROVENANCE = Provenance(
    model="claude-haiku-5-5", generated_at=AT, input_kind=InputKind.ABSTRACT, human_verified=False
)
ZH = ZhSummaryValue(
    zh_summary_id=uuid.uuid4(),
    research_question="遠距團隊如何採用協作平台？",
    method=None,
    theory=None,
    findings=None,
    reported_fields=(),
)
TOPIC = ClassificationFieldValue(
    keys=("erp",), human_verified=False, fix_member_count=0, members_disagree=False
)
CLASSIFICATION = ClassificationValue(topic=TOPIC, method=None)
TRUST = uuid.uuid4()
LOYALTY = uuid.uuid4()
RESEARCH_MODEL = ResearchModelValue(
    research_model_id=uuid.uuid4(),
    is_review_or_meta_analysis=False,
    country=None,
    industry=None,
    research_design=None,
    unit_of_analysis=None,
    sample_size=None,
    limitations=None,
    future_research=None,
    constructs=(
        ConstructValue(
            construct_id=TRUST,
            name_en="Trust",
            label_zh="信任",
            definition=None,
            scale_source=None,
            item_location=None,
            item_count=None,
            cronbach_alpha=Decimal("0.91"),
            composite_reliability=None,
            ave=None,
        ),
    ),
    hypotheses=(
        HypothesisValue(
            ordinal=1,
            sentence="Trust increases loyalty.",
            cause_construct_id=TRUST,
            effect_construct_id=LOYALTY,
            evidence_type=EvidenceType.EMPIRICAL_TEST,
            support=SupportLevel.SUPPORTED,
            beta=Decimal("0.42"),
            p_value=None,
            sample_size=None,
            effect_type=None,
        ),
    ),
)
VALUES: dict[AiProduct, Any] = {
    AiProduct.ZH_SUMMARY: ZH,
    AiProduct.CLASSIFICATION: CLASSIFICATION,
    AiProduct.RESEARCH_MODEL: RESEARCH_MODEL,
}
NO_DETAIL_STATES = sorted(set(AiBlockState) - {AiBlockState.READY, AiBlockState.EMPTY})


def test_there_are_exactly_eight_states() -> None:
    assert {state.value for state in AiBlockState} == {
        "ready",
        "empty",
        "pending_interactive",
        "pending_batch",
        "deferred_budget",
        "no_input",
        "failed",
        "not_applicable",
    }


def test_products_map_to_llm_and_processing_tasks() -> None:
    assert {product: product.llm_task for product in AiProduct} == {
        AiProduct.ZH_SUMMARY: LlmTask.ZH_SUMMARY,
        AiProduct.CLASSIFICATION: LlmTask.CLASSIFY,
        AiProduct.RESEARCH_MODEL: LlmTask.EXTRACT_MODEL,
    }
    assert {product.processing_task for product in AiProduct} == {
        ProcessingTask.ZH_SUMMARY,
        ProcessingTask.CLASSIFY,
        ProcessingTask.EXTRACT_MODEL,
    }


def test_skip_states_map_to_block_states() -> None:
    assert skip_block_state(SkipState.NOT_APPLICABLE) is AiBlockState.NOT_APPLICABLE
    assert skip_block_state(SkipState.NO_INPUT) is AiBlockState.NO_INPUT


# --- 不變式 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("product", list(AiProduct))
def test_ready_carries_value_and_provenance(product: AiProduct) -> None:
    block = AiBlock(product, AiBlockState.READY, VALUES[product], PROVENANCE)

    assert block.value is VALUES[product]
    assert block.provenance is PROVENANCE


@pytest.mark.parametrize("product", [AiProduct.ZH_SUMMARY, AiProduct.RESEARCH_MODEL])
def test_empty_carries_provenance_only(product: AiProduct) -> None:
    block = AiBlock(product, AiBlockState.EMPTY, provenance=PROVENANCE)

    assert block.value is None
    with pytest.raises(ValueError, match="只有 ready 帶 value"):
        AiBlock(product, AiBlockState.EMPTY, VALUES[product], PROVENANCE)
    with pytest.raises(ValueError, match="provenance"):
        AiBlock(product, AiBlockState.EMPTY)


def test_classification_is_never_empty() -> None:
    with pytest.raises(ValueError, match="unclassified"):
        AiBlock(AiProduct.CLASSIFICATION, AiBlockState.EMPTY, provenance=PROVENANCE)


@pytest.mark.parametrize("state", NO_DETAIL_STATES)
@pytest.mark.parametrize("product", list(AiProduct))
def test_other_states_carry_nothing(product: AiProduct, state: AiBlockState) -> None:
    block = AiBlock(product, state)

    assert (block.value, block.provenance) == (None, None)
    with pytest.raises(ValueError, match="provenance"):
        AiBlock(product, state, provenance=PROVENANCE)
    with pytest.raises(ValueError, match="value"):
        AiBlock(product, state, VALUES[product])


def test_ready_without_value_or_provenance_is_rejected() -> None:
    with pytest.raises(ValueError, match="provenance"):
        AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.READY, ZH)
    with pytest.raises(ValueError, match="value"):
        AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.READY, provenance=PROVENANCE)


def test_value_type_must_match_product() -> None:
    with pytest.raises(ValueError, match="型別"):
        AiBlock(AiProduct.CLASSIFICATION, AiBlockState.READY, ZH, PROVENANCE)
    with pytest.raises(ValueError, match="型別"):
        AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.READY, cast(Any, {"eta": 3}), PROVENANCE)


def test_block_has_no_eta_or_progress_fields() -> None:
    assert [field.name for field in dataclasses.fields(AiBlock)] == [
        "product",
        "state",
        "value",
        "provenance",
    ]
    assert [field.name for field in dataclasses.fields(Provenance)] == [
        "model",
        "generated_at",
        "input_kind",
        "human_verified",
    ]
    with pytest.raises(TypeError):
        AiBlock(  # type: ignore[call-arg]
            AiProduct.ZH_SUMMARY, AiBlockState.PENDING_BATCH, eta_seconds=60
        )
    block = AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.PENDING_BATCH)
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(block, "progress_percent", 50)


# --- 來歷 ---------------------------------------------------------------------------------


def test_provenance_with_only_human_values_has_no_model() -> None:
    provenance = Provenance(model=None, generated_at=AT, input_kind=None, human_verified=True)

    assert provenance.model is None
    with pytest.raises(ValueError, match="人工值"):
        Provenance(model=None, generated_at=AT, input_kind=None, human_verified=False)
    with pytest.raises(ValueError, match="一起"):
        Provenance(model="m", generated_at=AT, input_kind=None, human_verified=False)
    with pytest.raises(ValueError, match="時區"):
        Provenance(
            model="m",
            generated_at=datetime(2026, 11, 15),
            input_kind=InputKind.TITLE,
            human_verified=False,
        )


# --- 值 -----------------------------------------------------------------------------------


def test_zh_summary_value_rules() -> None:
    with pytest.raises(ValueError, match="empty"):
        dataclasses.replace(ZH, research_question=None)
    with pytest.raises(ValueError, match="順序"):
        dataclasses.replace(ZH, reported_fields=(SummaryField.THEORY, SummaryField.METHOD))
    with pytest.raises(ValueError, match="順序"):
        dataclasses.replace(ZH, reported_fields=(SummaryField.THEORY, SummaryField.THEORY))
    reported = dataclasses.replace(ZH, reported_fields=(SummaryField.METHOD, SummaryField.THEORY))
    assert reported.reported_fields == (SummaryField.METHOD, SummaryField.THEORY)


def test_classification_value_rules() -> None:
    with pytest.raises(ValueError, match="至少要有一欄"):
        ClassificationValue(topic=None, method=None)
    with pytest.raises(ValueError, match="排序"):
        dataclasses.replace(TOPIC, keys=("supply_chain", "erp"))
    with pytest.raises(ValueError, match="兩位"):
        dataclasses.replace(TOPIC, human_verified=True, fix_member_count=1, members_disagree=True)
    with pytest.raises(ValueError, match="人工值"):
        dataclasses.replace(TOPIC, human_verified=True)
    verified = dataclasses.replace(
        TOPIC, human_verified=True, fix_member_count=2, members_disagree=True
    )
    assert verified.members_disagree


def test_research_model_value_rules() -> None:
    with pytest.raises(ValueError, match="empty"):
        dataclasses.replace(RESEARCH_MODEL, constructs=(), hypotheses=())
    only_context = dataclasses.replace(RESEARCH_MODEL, constructs=(), hypotheses=(), country="tw")
    assert only_context.country == "tw"
    with pytest.raises(ValueError, match="Decimal"):
        dataclasses.replace(RESEARCH_MODEL.constructs[0], cronbach_alpha=cast(Any, 0.91))


# --- 現在的設定 ---------------------------------------------------------------------------------


def _settings(**changes: Any) -> AiBlockSettings:
    values: dict[str, Any] = {
        "prompt_version": 1,
        "model": "claude-haiku-5-5",
        "taxonomy_version": None,
        "context_values_version": None,
        "budget_allows": True,
    } | changes
    return AiBlockSettings(**values)


def test_settings_must_match_product() -> None:
    _settings().check_for(AiProduct.ZH_SUMMARY)
    _settings(taxonomy_version="1").check_for(AiProduct.CLASSIFICATION)
    _settings(context_values_version="1").check_for(AiProduct.RESEARCH_MODEL)
    with pytest.raises(ValueError, match="taxonomy_version"):
        _settings().check_for(AiProduct.CLASSIFICATION)
    with pytest.raises(ValueError, match="taxonomy_version"):
        _settings(taxonomy_version="1").check_for(AiProduct.ZH_SUMMARY)
    with pytest.raises(ValueError, match="context_values_version"):
        _settings().check_for(AiProduct.RESEARCH_MODEL)


@pytest.mark.parametrize(
    "changes",
    [
        {"prompt_version": 0},
        {"prompt_version": True},
        {"model": " "},
        {"taxonomy_version": ""},
        {"budget_allows": 1},
    ],
)
def test_settings_reject_bad_values(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"prompt_version|model|taxonomy_version|budget_allows"):
        _settings(**changes)
