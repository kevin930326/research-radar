"""AI 區塊的信封（ADR-0015「API 與示範站怎麼表示一個 AI 區塊」、ADR-0035、Story 2.7）。

每個 AI 區塊（一篇論文的中文重點、分類、研究模型）回傳同一個外層：`state`、`value`、`provenance`。
判定只寫在 `repo/ai_blocks.find_ai_block`，所有端點與示範站匯出共用；這裡只定形狀與不變式：

- `state` 只有八個值（`AiBlockState`）。
- 只有 `ready`、`empty` 帶 `provenance`（模型、產生時間、`input_kind`、是否經人工核對）；只有
  `ready` 帶 `value`，而且 `value` 的型別要和區塊的產出種類對得上。
- 不帶預計完成時間或進度百分比：`AiBlock` 只有四個欄位（`slots`），多給任何欄位都建不出來。

`AiBlockSettings` 是判定「目前這組鍵」要用的「現在」的設定（prompt 版本、模型、設定檔版本）與預算
是否准許，由 `services/ai_blocks.py` 組好傳進 repo（repo 不讀 prompt 登記、不讀帳本）。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import assert_never

from radar.domain.ai_outputs import InputKind, LlmTask
from radar.domain.judgments import SummaryField
from radar.domain.llm_schemas import EvidenceType, SupportLevel
from radar.domain.processing import ProcessingTask, SkipState


class AiProduct(StrEnum):
    """三種有 AI 區塊的產出（各自對到一個 LLM 工作與 `plan_for` 的一項處理）。"""

    ZH_SUMMARY = "zh_summary"
    CLASSIFICATION = "classification"
    RESEARCH_MODEL = "research_model"

    @property
    def llm_task(self) -> LlmTask:
        match self:
            case AiProduct.ZH_SUMMARY:
                return LlmTask.ZH_SUMMARY
            case AiProduct.CLASSIFICATION:
                return LlmTask.CLASSIFY
            case AiProduct.RESEARCH_MODEL:
                return LlmTask.EXTRACT_MODEL
            case _:
                assert_never(self)

    @property
    def processing_task(self) -> ProcessingTask:
        return ProcessingTask(self.llm_task.value)


class AiBlockState(StrEnum):
    """AI 區塊的八種狀態（ADR-0015）。"""

    READY = "ready"
    EMPTY = "empty"
    PENDING_INTERACTIVE = "pending_interactive"
    PENDING_BATCH = "pending_batch"
    DEFERRED_BUDGET = "deferred_budget"
    NO_INPUT = "no_input"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


_WITH_PROVENANCE = frozenset({AiBlockState.READY, AiBlockState.EMPTY})


def skip_block_state(skip: SkipState) -> AiBlockState:
    """`plan_for` 說不做時的區塊狀態。"""
    match SkipState(skip):
        case SkipState.NOT_APPLICABLE:
            return AiBlockState.NOT_APPLICABLE
        case SkipState.NO_INPUT:
            return AiBlockState.NO_INPUT
        case other:
            assert_never(other)


def _check_aware(name: str, value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 要是帶時區的 datetime")


@dataclass(frozen=True, slots=True)
class Provenance:
    """來歷：哪個模型、什麼時候產生、模型看的是什麼、是否經人工核對。

    只有人工值、沒有 AI 值時（例如分類只有人工修正），`model` 與 `input_kind` 都是 None，
    `generated_at` 是最新一筆人工值的時間。

    Raises:
        ValueError: `model` 與 `input_kind` 一個有值一個沒有、`model` 是空字串、時間沒有時區、
            只有人工值卻沒標經人工核對。
    """

    model: str | None
    generated_at: datetime
    input_kind: InputKind | None
    human_verified: bool

    def __post_init__(self) -> None:
        if (self.model is None) != (self.input_kind is None):
            raise ValueError("model 與 input_kind 要一起有值或一起是 None")
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("model 有值時不能是空字串")
        if self.input_kind is not None:
            InputKind(self.input_kind)
        _check_aware("generated_at", self.generated_at)
        if type(self.human_verified) is not bool:
            raise ValueError("human_verified 要是布林")
        if self.model is None and not self.human_verified:
            raise ValueError("沒有 AI 來歷時一定是人工值")


# --- 值 -----------------------------------------------------------------------------------


def _check_optional_text(name: str, value: object) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{name} 有值時要是不只有空白的字串")


@dataclass(frozen=True, slots=True)
class ZhSummaryValue:
    """有效的中文重點（`effective_zh_summary` 的一列）。

    `reported_fields`：有成員回報可能有誤、還沒有處理結果的欄位（照四欄的固定順序）；回報只對
    `zh_summary_id` 這一筆有效。

    Raises:
        ValueError: 四欄全空（那是 `empty`，不帶值）、文字只有空白、`reported_fields` 不是四欄之一、
            重複或順序不對。
    """

    zh_summary_id: uuid.UUID
    research_question: str | None
    method: str | None
    theory: str | None
    findings: str | None
    reported_fields: tuple[SummaryField, ...]

    def __post_init__(self) -> None:
        values = (self.research_question, self.method, self.theory, self.findings)
        for field, value in zip(SummaryField, values, strict=True):
            _check_optional_text(field.value, value)
        if all(value is None for value in values):
            raise ValueError("四欄全空是 empty，不帶值")
        order = list(SummaryField)
        if not isinstance(self.reported_fields, tuple) or not all(
            isinstance(field, SummaryField) for field in self.reported_fields
        ):
            raise ValueError("reported_fields 要是 SummaryField 的 tuple")
        if list(self.reported_fields) != sorted(set(self.reported_fields), key=order.index):
            raise ValueError("reported_fields 要照四欄的順序、不重複")


@dataclass(frozen=True, slots=True)
class ClassificationFieldValue:
    """有效分類的一欄（`effective_classification` 的一列；單位是（論文, `field`））。

    - `keys`：排序過、不重複的 `key`（人工值優先，沒有才是 `active` 的 AI 值）。
    - `human_verified`：這一欄是人工值（「已核對」）。
    - `fix_member_count`：修正過這一欄的成員人數。
    - `members_disagree`：每位成員各取最新一筆，兩人以上、而且整組 key 不同（「成員意見不同」）。

    Raises:
        ValueError: `keys` 寫法不對、沒排序或重複；人數是負的；人數不到 2 卻標意見不同；是人工值卻
            沒有人修正過。
    """

    keys: tuple[str, ...]
    human_verified: bool
    fix_member_count: int
    members_disagree: bool

    def __post_init__(self) -> None:
        if not isinstance(self.keys, tuple) or not all(isinstance(key, str) for key in self.keys):
            raise ValueError("keys 要是字串的 tuple")
        if list(self.keys) != sorted(set(self.keys)):
            raise ValueError("keys 要排序過、不重複")
        if type(self.human_verified) is not bool or type(self.members_disagree) is not bool:
            raise ValueError("human_verified、members_disagree 要是布林")
        if type(self.fix_member_count) is not int or self.fix_member_count < 0:
            raise ValueError("fix_member_count 要是 ≥ 0 的整數")
        if self.members_disagree and self.fix_member_count < 2:
            raise ValueError("意見不同至少要有兩位成員修正過")
        if self.human_verified and self.fix_member_count < 1:
            raise ValueError("人工值至少要有一位成員修正過")


@dataclass(frozen=True, slots=True)
class ClassificationValue:
    """有效的分類：主題與方法兩欄，沒有任何值的那一欄是 None（只會發生在只有人工修正、沒有 AI 值
    的時候）。

    Raises:
        ValueError: 兩欄都是 None。
    """

    topic: ClassificationFieldValue | None
    method: ClassificationFieldValue | None

    def __post_init__(self) -> None:
        for name in ("topic", "method"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, ClassificationFieldValue):
                raise ValueError(f"{name} 要是 ClassificationFieldValue 或 None")
        if self.topic is None and self.method is None:
            raise ValueError("分類至少要有一欄有值")


def _check_decimal(name: str, value: object) -> None:
    if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
        raise ValueError(f"{name} 有值時要是有限的 Decimal")


@dataclass(frozen=True, slots=True)
class ConstructValue:
    """研究模型提到的一個構面（`construct_mention` 的一列；數字是 `Decimal`，不經浮點）。"""

    construct_id: uuid.UUID
    name_en: str
    label_zh: str
    definition: str | None
    scale_source: str | None
    item_location: str | None
    item_count: int | None
    cronbach_alpha: Decimal | None
    composite_reliability: Decimal | None
    ave: Decimal | None

    def __post_init__(self) -> None:
        for name in ("name_en", "label_zh"):
            if getattr(self, name) is None:
                raise ValueError(f"{name} 不能是 None")
        for name in ("name_en", "label_zh", "definition", "scale_source", "item_location"):
            _check_optional_text(name, getattr(self, name))
        for name in ("cronbach_alpha", "composite_reliability", "ave"):
            _check_decimal(name, getattr(self, name))


@dataclass(frozen=True, slots=True)
class HypothesisValue:
    """研究模型的一條關係（`hypothesis` 的一列；因與果是原本的構面）。"""

    ordinal: int
    sentence: str
    cause_construct_id: uuid.UUID
    effect_construct_id: uuid.UUID
    evidence_type: EvidenceType
    support: SupportLevel
    beta: Decimal | None
    p_value: Decimal | None
    sample_size: int | None
    effect_type: str | None
    # 判定了支持與否時，摘要裡寫出檢定結果的原文（2.6 的 FR-3 擷取規則 1）；沒判定時是 None。
    result_quote: str | None

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("ordinal 要是 ≥ 1 的整數")
        if self.sentence is None:
            raise ValueError("sentence 不能是 None")
        _check_optional_text("sentence", self.sentence)
        _check_optional_text("effect_type", self.effect_type)
        _check_optional_text("result_quote", self.result_quote)
        EvidenceType(self.evidence_type)
        SupportLevel(self.support)
        _check_decimal("beta", self.beta)
        _check_decimal("p_value", self.p_value)


@dataclass(frozen=True, slots=True)
class ResearchModelValue:
    """有效的研究模型（`effective_research_model` 的一列）。

    `is_review_or_meta_analysis` 在 2.2 留下的表頭是 None。

    Raises:
        ValueError: 沒有任何可擷取的內容（那是 `empty`，不帶值）、構面或假設的型別不對。
    """

    research_model_id: uuid.UUID
    is_review_or_meta_analysis: bool | None
    country: str | None
    industry: str | None
    research_design: str | None
    unit_of_analysis: str | None
    sample_size: int | None
    limitations: str | None
    future_research: str | None
    constructs: tuple[ConstructValue, ...]
    hypotheses: tuple[HypothesisValue, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.constructs, tuple) or not all(
            isinstance(item, ConstructValue) for item in self.constructs
        ):
            raise ValueError("constructs 要是 ConstructValue 的 tuple")
        if not isinstance(self.hypotheses, tuple) or not all(
            isinstance(item, HypothesisValue) for item in self.hypotheses
        ):
            raise ValueError("hypotheses 要是 HypothesisValue 的 tuple")
        if research_model_is_empty(
            constructs=self.constructs,
            hypotheses=self.hypotheses,
            context=(self.country, self.industry, self.research_design, self.unit_of_analysis),
            sample_size=self.sample_size,
            texts=(self.limitations, self.future_research),
        ):
            raise ValueError("沒有任何可擷取的內容是 empty，不帶值")


def research_model_is_empty(
    *,
    constructs: tuple[object, ...],
    hypotheses: tuple[object, ...],
    context: tuple[str | None, ...],
    sample_size: int | None,
    texts: tuple[str | None, ...],
) -> bool:
    """研究模型的 `empty`：沒有構面、假設、情境、樣本數、限制、未來研究（是不是文獻回顧不算
    內容）。"""
    return (
        not constructs
        and not hypotheses
        and all(value is None for value in context)
        and sample_size is None
        and all(value is None for value in texts)
    )


type AiBlockValue = ZhSummaryValue | ClassificationValue | ResearchModelValue


def _value_type(product: AiProduct) -> type:
    match product:
        case AiProduct.ZH_SUMMARY:
            return ZhSummaryValue
        case AiProduct.CLASSIFICATION:
            return ClassificationValue
        case AiProduct.RESEARCH_MODEL:
            return ResearchModelValue
        case _:
            assert_never(product)


# --- 信封 ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AiBlock:
    """一個 AI 區塊。

    Raises:
        ValueError: 不是 `ready`／`empty` 卻帶來歷、`ready`／`empty` 沒帶來歷；不是 `ready` 卻帶值、
            `ready` 沒帶值；值的型別和產出種類對不上；分類是 `empty`（分類至少有 `unclassified`）。
    """

    product: AiProduct
    state: AiBlockState
    value: AiBlockValue | None = None
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        product = AiProduct(self.product)
        state = AiBlockState(self.state)
        if (state in _WITH_PROVENANCE) != (self.provenance is not None):
            raise ValueError("只有 ready 與 empty 帶 provenance，而且一定要帶")
        if self.provenance is not None and not isinstance(self.provenance, Provenance):
            raise ValueError("provenance 要是 Provenance")
        if (state is AiBlockState.READY) != (self.value is not None):
            raise ValueError("只有 ready 帶 value，而且一定要帶")
        if self.value is not None and not isinstance(self.value, _value_type(product)):
            raise ValueError(f"{product.value} 的 value 型別不對")
        if state is AiBlockState.EMPTY and product is AiProduct.CLASSIFICATION:
            raise ValueError("分類不會是 empty（至少有 unclassified）")


# --- 「現在」的設定 ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AiBlockSettings:
    """判定一個區塊時「現在」的設定：目前的 prompt 版本與模型、會算進 `input_sha256` 的設定檔版本
    （分類表、情境值清單）、這個工作現在預算准不准。

    Raises:
        ValueError: prompt 版本不是 ≥ 1 的整數、模型是空字串、版本是空字串、`budget_allows`
            不是布林。
    """

    prompt_version: int
    model: str
    taxonomy_version: str | None
    context_values_version: str | None
    budget_allows: bool

    def __post_init__(self) -> None:
        if type(self.prompt_version) is not int or self.prompt_version < 1:
            raise ValueError("prompt_version 要是 ≥ 1 的整數")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model 不能是空字串")
        for name in ("taxonomy_version", "context_values_version"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} 有值時不能是空字串")
        if type(self.budget_allows) is not bool:
            raise ValueError("budget_allows 要是布林")

    def check_for(self, product: AiProduct) -> None:
        """分類一定要帶分類表版本、研究模型一定要帶情境值清單版本，其他不得帶（和 `input_sha256`
        的規則一樣，提早擋）。

        Raises:
            ValueError: 版本和產出種類對不上。
        """
        product = AiProduct(product)
        if (product is AiProduct.CLASSIFICATION) != (self.taxonomy_version is not None):
            raise ValueError("只有分類帶 taxonomy_version，而且一定要帶")
        if (product is AiProduct.RESEARCH_MODEL) != (self.context_values_version is not None):
            raise ValueError("只有研究模型帶 context_values_version，而且一定要帶")
