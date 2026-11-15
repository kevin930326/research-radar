"""AI 區塊的判定：全專案只有 `find_ai_block` 一個函式（AD-30、ADR-0015、ADR-0035、Story 2.7）。

判定順序固定（前面的條件成立就不往下看）：

1. `plan_for` 說不做 → `not_applicable` 或 `no_input`。
2. 有效值（人工值或 `active`，只讀 `effective_*` 三個 view）→ `ready`（帶值與來歷）或 `empty`
   （只帶來歷）。新的一次被拒收不影響舊的有效值（這一步在第 3 步前面）。
3. 目前這組鍵有平常流程的 `rejected`，或有平常流程的 `running` run 屬於卡在 `needs_attention`
   的批次（Story 2.11、ADR-0004 決定第 4 點）→ `failed`。
4. 這個工作現在預算不准 → `deferred_budget`。
5. 目前這組鍵有互動的 `running` → `pending_interactive`；其餘（什麼都沒有、Batch 的 `running`、
   暫時性失敗等下一次）→ `pending_batch`。

「目前這組鍵」＝現在的 prompt 版本、模型（`AiBlockSettings`）與 `input_sha256`（輸入由
`build_llm_input` 依 `plan_for` 的 `input_kind` 組，設定檔版本照工作帶）。

這個模組不直接讀 AI 表頭表與細項表（`tests/arch/test_ai_table_reads.py`）：有效值讀 view，拒收
與執行中各自經 `repo/ai_outputs.py`、`repo/llm_ledger.py`，批次卡住經 `repo/llm_batches.py`。
可見條件只從 `repo/visibility.py` 來；看不到或不存在回 None。
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, assert_never

from sqlalchemy import Connection, RowMapping, text

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
    research_model_is_empty,
    skip_block_state,
)
from radar.domain.ai_outputs import (
    CallMode,
    InputKind,
    NoLlmInputError,
    RunKey,
    build_llm_input,
)
from radar.domain.fingerprints import input_sha256
from radar.domain.judgments import SummaryField
from radar.domain.llm_schemas import EvidenceType, SupportLevel
from radar.domain.processing import plan_for
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import Viewer
from radar.repo.ai_outputs import find_rejected_ai_result_id
from radar.repo.llm_batches import find_batch_attention_run
from radar.repo.llm_ledger import find_running_call_mode
from radar.repo.papers import find_paper_llm_source, find_paper_processing_facts
from radar.repo.visibility import visible_papers


def find_ai_block(
    viewer: Viewer,
    connection: Connection,
    paper_id: uuid.UUID,
    product: AiProduct,
    settings: AiBlockSettings,
) -> AiBlock | None:
    """一篇論文的一種 AI 產出現在是八種狀態的哪一種（見模組說明）。論文不存在或 `viewer` 看不到
    回 None。

    Raises:
        ValueError: `settings` 的設定檔版本和產出種類對不上（分類要帶分類表版本、研究模型要帶情境值
            清單版本，其他不得帶）。
    """
    product = AiProduct(product)
    settings.check_for(product)
    facts = find_paper_processing_facts(viewer, connection, paper_id)
    if facts is None:
        return None
    plan = plan_for(facts).for_task(product.processing_task)
    if plan.skip is not None:
        return AiBlock(product, skip_block_state(plan.skip))
    assert plan.input_kind is not None

    visible = visible_papers(viewer, alias="paper")
    effective: AiBlock | None
    match product:
        case AiProduct.ZH_SUMMARY:
            row = (
                connection.execute(
                    text(
                        "SELECT e.zh_summary_id, e.input_kind, e.generated_at, e.model,"
                        " e.research_question, e.method, e.theory, e.findings, e.reported_fields"
                        " FROM effective_zh_summary AS e JOIN paper ON paper.id = e.paper_id"
                        f" WHERE {visible} AND e.paper_id = :paper_id"
                    ),
                    {"paper_id": paper_id},
                )
                .mappings()
                .one_or_none()
            )
            effective = None if row is None else _zh_summary_block(row)
        case AiProduct.CLASSIFICATION:
            rows = (
                connection.execute(
                    text(
                        "SELECT e.field, e.keys, e.human_verified, e.fix_member_count,"
                        " e.members_disagree, e.model, e.input_kind, e.generated_at, e.fixed_at"
                        " FROM effective_classification AS e JOIN paper ON paper.id = e.paper_id"
                        f" WHERE {visible} AND e.paper_id = :paper_id"
                    ),
                    {"paper_id": paper_id},
                )
                .mappings()
                .all()
            )
            effective = _classification_block(rows) if rows else None
        case AiProduct.RESEARCH_MODEL:
            row = (
                connection.execute(
                    text(
                        "SELECT e.research_model_id, e.input_kind, e.generated_at, e.model,"
                        " e.is_review_or_meta_analysis, e.country, e.industry,"
                        " e.research_design, e.unit_of_analysis, e.sample_size, e.limitations,"
                        " e.future_research, e.constructs, e.hypotheses"
                        " FROM effective_research_model AS e JOIN paper ON paper.id = e.paper_id"
                        f" WHERE {visible} AND e.paper_id = :paper_id"
                    ),
                    {"paper_id": paper_id},
                )
                .mappings()
                .one_or_none()
            )
            effective = None if row is None else _research_model_block(row)
        case _:
            assert_never(product)
    if effective is not None:
        return effective

    source = find_paper_llm_source(viewer, connection, paper_id, for_update=False)
    if source is None:
        return None
    task = product.llm_task
    try:
        llm_input = build_llm_input(source, plan.input_kind)
    except NoLlmInputError:
        # 兩次讀之間摘要被拿掉（同一個交易的兩句看到不同的資料）：照沒有輸入回報。
        return AiBlock(product, AiBlockState.NO_INPUT)
    key = RunKey(
        paper_id,
        task,
        settings.prompt_version,
        settings.model,
        input_sha256(
            llm_input.blocks,
            task,
            taxonomy_version=settings.taxonomy_version,
            context_values_version=settings.context_values_version,
        ),
    )
    if find_rejected_ai_result_id(viewer, connection, key) is not None:
        return AiBlock(product, AiBlockState.FAILED)
    if find_batch_attention_run(viewer, connection, key):
        return AiBlock(product, AiBlockState.FAILED)
    if not settings.budget_allows:
        return AiBlock(product, AiBlockState.DEFERRED_BUDGET)
    mode = find_running_call_mode(viewer, connection, key)
    if mode is CallMode.INTERACTIVE:
        return AiBlock(product, AiBlockState.PENDING_INTERACTIVE)
    return AiBlock(product, AiBlockState.PENDING_BATCH)


# --- 列 → 區塊 -------------------------------------------------------------------------------


def _ai_provenance(row: RowMapping, *, human_verified: bool) -> Provenance:
    generated_at: datetime = row["generated_at"]
    return Provenance(
        model=row["model"],
        generated_at=generated_at,
        input_kind=InputKind(row["input_kind"]),
        human_verified=human_verified,
    )


def _zh_summary_block(row: RowMapping) -> AiBlock:
    provenance = _ai_provenance(row, human_verified=False)
    texts = [row[field.value] for field in SummaryField]
    if all(value is None for value in texts):
        return AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.EMPTY, provenance=provenance)
    value = ZhSummaryValue(
        zh_summary_id=row["zh_summary_id"],
        research_question=row["research_question"],
        method=row["method"],
        theory=row["theory"],
        findings=row["findings"],
        reported_fields=tuple(SummaryField(field) for field in row["reported_fields"]),
    )
    return AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.READY, value, provenance)


def _classification_block(rows: Sequence[RowMapping]) -> AiBlock:
    """（論文, `field`）的列 → 一個分類區塊。來歷用 AI 那一筆（有的話）；只有人工值時模型與
    `input_kind` 是空的、時間是最新一筆人工值。經人工核對＝兩欄都是人工值（有沒有 AI 值都一樣；
    只修正了一欄、另一欄沒有值時不算）。"""
    fields: dict[TaxonomyField, ClassificationFieldValue] = {}
    ai_row: RowMapping | None = None
    fixed_at: list[datetime] = []
    for row in rows:
        fields[TaxonomyField(row["field"])] = ClassificationFieldValue(
            keys=tuple(row["keys"]),
            human_verified=row["human_verified"],
            fix_member_count=row["fix_member_count"],
            members_disagree=row["members_disagree"],
        )
        if row["model"] is not None:
            ai_row = row
        if row["fixed_at"] is not None:
            fixed_at.append(row["fixed_at"])
    verified = len(fields) == len(TaxonomyField) and all(
        value.human_verified for value in fields.values()
    )
    if ai_row is not None:
        provenance = _ai_provenance(ai_row, human_verified=verified)
    else:
        provenance = Provenance(
            model=None, generated_at=max(fixed_at), input_kind=None, human_verified=verified
        )
    value = ClassificationValue(
        topic=fields.get(TaxonomyField.TOPIC), method=fields.get(TaxonomyField.METHOD)
    )
    return AiBlock(AiProduct.CLASSIFICATION, AiBlockState.READY, value, provenance)


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _construct(item: Mapping[str, Any]) -> ConstructValue:
    return ConstructValue(
        construct_id=uuid.UUID(item["construct_id"]),
        name_en=item["name_en"],
        label_zh=item["label_zh"],
        definition=item["definition"],
        scale_source=item["scale_source"],
        item_location=item["item_location"],
        item_count=item["item_count"],
        cronbach_alpha=_decimal(item["cronbach_alpha"]),
        composite_reliability=_decimal(item["composite_reliability"]),
        ave=_decimal(item["ave"]),
    )


def _hypothesis(item: Mapping[str, Any]) -> HypothesisValue:
    return HypothesisValue(
        ordinal=item["ordinal"],
        sentence=item["sentence"],
        cause_construct_id=uuid.UUID(item["cause_construct_id"]),
        effect_construct_id=uuid.UUID(item["effect_construct_id"]),
        evidence_type=EvidenceType(item["evidence_type"]),
        support=SupportLevel(item["support"]),
        beta=_decimal(item["beta"]),
        p_value=_decimal(item["p_value"]),
        sample_size=item["sample_size"],
        effect_type=item["effect_type"],
        result_quote=item["result_quote"],
    )


def _research_model_block(row: RowMapping) -> AiBlock:
    provenance = _ai_provenance(row, human_verified=False)
    constructs = tuple(_construct(item) for item in row["constructs"])
    hypotheses = tuple(_hypothesis(item) for item in row["hypotheses"])
    context = (row["country"], row["industry"], row["research_design"], row["unit_of_analysis"])
    texts = (row["limitations"], row["future_research"])
    if research_model_is_empty(
        constructs=constructs,
        hypotheses=hypotheses,
        context=context,
        sample_size=row["sample_size"],
        texts=texts,
    ):
        return AiBlock(AiProduct.RESEARCH_MODEL, AiBlockState.EMPTY, provenance=provenance)
    value = ResearchModelValue(
        research_model_id=row["research_model_id"],
        is_review_or_meta_analysis=row["is_review_or_meta_analysis"],
        country=row["country"],
        industry=row["industry"],
        research_design=row["research_design"],
        unit_of_analysis=row["unit_of_analysis"],
        sample_size=row["sample_size"],
        limitations=row["limitations"],
        future_research=row["future_research"],
        constructs=constructs,
        hypotheses=hypotheses,
    )
    return AiBlock(AiProduct.RESEARCH_MODEL, AiBlockState.READY, value, provenance)
