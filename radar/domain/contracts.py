"""跨邊界共用的資料形狀（AR-24、AD-21「共用的形狀只有一個模型」）。

這裡的模型是成員版 API、示範站的 `cube.json`、測試資料檔（`tests/golden/`）共用的唯一形狀；
之後的 Story 要用同一種資料時 import 這裡的模型，不另訂一份。每個模型由第一個用到它的 Story
定義（計數方塊的列：Story 1.10）。

論文頁（Story 3.6、ADR-0048）定義的：快照（`SnapshotRef`）、來歷（`Provenance`）、三種 AI 區塊的
信封（`ZhSummaryBlock`、`ClassificationBlock`、`ResearchModelBlock`）與內容、書目（`PaperBiblio`）、
摘要（`PaperAbstract`）、全文連結（`FullTextLinks`）、顯示名稱（`DisplayNames`）、前端設定值
（`ClientConfig`）。

文獻庫（Story 3.16）定義的：論文列（`PaperRow`，3.13 的論文清單也用它）；前端設定值多一個
`library_page_size`。
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self
from urllib.parse import quote

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

from radar.domain import ai_blocks
from radar.domain.ai_outputs import CurrentAbstract, InputKind
from radar.domain.domain_config import JournalsConfig
from radar.domain.judgments import SummaryField
from radar.domain.library import LIBRARY_PAGE_SIZE
from radar.domain.llm_schemas import EvidenceType, SupportLevel
from radar.domain.paper_list import PAPER_LIST_PAGE_SIZE
from radar.domain.paper_page import PaperPageFacts
from radar.domain.paper_rows import PAPER_ROW_AUTHOR_LIMIT, PaperRowFacts
from radar.domain.papers import AbstractSource, PaperOrigin
from radar.domain.snapshot import DATA_TIMEZONE, Snapshot, SnapshotStatus
from radar.domain.taxonomy import Taxonomy, TaxonomyField
from radar.domain.trend_config import MonthDay, TrendConfig, check_month_day


class CubeDim(StrEnum):
    """計數方塊的一列是哪一種計數（AD-19、ADR-0011「計數方塊」）。剛好這六種。"""

    TOTAL = "total"  # 每個（期刊, 年份）算數的論文篇數
    WITH_ABSTRACT = "with_abstract"  # 其中有目前的摘要的篇數
    PENDING_CLASSIFICATION = "pending_classification"  # 其中還沒分類的篇數
    TOPIC = "topic"  # 每個（期刊, 年份, 主題）的篇數
    TOPIC_PAIR = "topic_pair"  # 每個（期刊, 年份, 主題 A, 主題 B）的篇數（A < B）
    METHOD = "method"  # 每個（期刊, 年份, 方法）的篇數


# 不帶 key 的三種；`topic`、`method` 只有 `key_a`；`topic_pair` 兩個都有。
_NO_KEYS = frozenset({CubeDim.TOTAL, CubeDim.WITH_ABSTRACT, CubeDim.PENDING_CLASSIFICATION})
_KEY_A_ONLY = frozenset({CubeDim.TOPIC, CubeDim.METHOD})

_Key = Annotated[str, StringConstraints(min_length=1)]


class CubeRow(BaseModel):
    """計數方塊的一列（一格）：（`journal`, `year`, `dim`, `key_a`, `key_b`, `n`）。

    - `journal` 是設定檔的期刊代號（不是 ISSN-L）。
    - 主題、方法用設定檔的代號，未分類是 `unclassified`；`topic_pair` 把字母順序在前的放
      `key_a`（ADR-0011）。
    - 只給計數，不給比例：比例由前端的 `web/src/stats/` 用這些格子算。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    journal: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    year: int
    dim: CubeDim
    key_a: _Key | None = None
    key_b: _Key | None = None
    n: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_keys(self) -> Self:
        if self.dim in _NO_KEYS:
            if self.key_a is not None or self.key_b is not None:
                raise ValueError(f"{self.dim} 不帶 key_a、key_b")
        elif self.dim in _KEY_A_ONLY:
            if self.key_a is None or self.key_b is not None:
                raise ValueError(f"{self.dim} 要有 key_a、不能有 key_b")
        else:  # topic_pair
            if self.key_a is None or self.key_b is None:
                raise ValueError("topic_pair 要有 key_a 與 key_b")
            if not self.key_a < self.key_b:
                raise ValueError("topic_pair 的 key_a 要排在 key_b 前面（字母順序、不能相同）")
        return self


# --- 論文頁（Story 3.6、ADR-0048）---------------------------------------------------------------
#
# 成員版 `GET /api/v1/papers/{paper_id}` 先用；3.7 的示範站匯出用同一批模型（快照、來歷、三種 AI
# 區塊信封、書目、摘要）。規則：
# - 時間一律轉成 UTC，JSON 是 ISO 8601、結尾 `Z`（Conventions「時間」）。
# - `Decimal` 輸出成字串（不經浮點、不用科學記號）。
# - 三種 AI 區塊的內容與信封的不變式不另寫一份：驗證時組回 `domain/ai_blocks.py` 的 dataclass，
#   由那裡檢查（`state` 與 `value`／`provenance` 對不對得上、分類不會是 `empty` 等）。
# - 期刊、主題、方法只給 `key`，顯示名稱由 `DisplayNames` 提供（不放 i18n）。


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("時間要帶時區")
    return value.astimezone(UTC)


def _finite_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("數字要是有限值")
    return value


def _decimal_text(value: Decimal) -> str:
    # `format(..., "f")`：`Decimal("1E-7")` 輸出 `0.0000001`，不是 `1E-7`。
    return format(value, "f")


# 帶時區的時間，轉成 UTC（JSON 結尾是 `Z`）。
UtcDateTime = Annotated[datetime, AfterValidator(_to_utc)]
# 有限的 `Decimal`，JSON 是字串（OpenAPI 的型別也是 string）。
DecimalText = Annotated[
    Decimal, AfterValidator(_finite_decimal), PlainSerializer(_decimal_text, return_type=str)
]
_Text = Annotated[str, StringConstraints(min_length=1)]
_JournalKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def doi_url(doi: str) -> str:
    """DOI（正規化後的寫法）→ `https://doi.org/` 的連結；`/` 以外的特殊字元都編碼。"""
    return "https://doi.org/" + quote(doi, safe="/")


def openalex_url(openalex_id: str) -> str:
    """OpenAlex ID（`W` 開頭的大寫寫法）→ OpenAlex 網站的連結。"""
    return "https://openalex.org/" + quote(openalex_id, safe="")


class SnapshotRef(BaseModel):
    """快照（AD-19）：編號與截止時間（發佈的時間，UTC）。截至日期由前端換成台北時間的日期。"""

    model_config = _FROZEN

    number: int = Field(ge=1, strict=True)
    as_of: UtcDateTime

    @classmethod
    def from_snapshot(cls, snapshot: Snapshot) -> Self:
        """已發佈的快照 → 回應的形狀。

        Raises:
            ValueError: 快照還沒發佈（沒有 `as_of`）。
        """
        if snapshot.status is not SnapshotStatus.PUBLISHED or snapshot.as_of is None:
            raise ValueError("只有已發佈的快照可以放進回應")
        return cls(number=snapshot.number, as_of=snapshot.as_of)


class Provenance(BaseModel):
    """AI 區塊的來歷（ADR-0015）：模型、產生時間、模型看的是什麼、是否經人工核對。

    只有人工值時 `model` 與 `input_kind` 都是 null。
    """

    model_config = _FROZEN

    model: _Text | None
    generated_at: UtcDateTime
    input_kind: InputKind | None
    human_verified: StrictBool

    @model_validator(mode="after")
    def _check(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> ai_blocks.Provenance:
        return ai_blocks.Provenance(
            model=self.model,
            generated_at=self.generated_at,
            input_kind=self.input_kind,
            human_verified=self.human_verified,
        )

    @classmethod
    def from_domain(cls, provenance: ai_blocks.Provenance) -> Self:
        return cls(
            model=provenance.model,
            generated_at=provenance.generated_at,
            input_kind=provenance.input_kind,
            human_verified=provenance.human_verified,
        )


# --- 三種 AI 區塊的內容 -------------------------------------------------------------------------


class ZhSummaryContent(BaseModel):
    """有效的中文重點。`reported_fields`：有成員回報可能有誤、還沒處理的欄位（照四欄的順序）。"""

    model_config = _FROZEN

    zh_summary_id: uuid.UUID
    research_question: str | None
    method: str | None
    theory: str | None
    findings: str | None
    reported_fields: tuple[SummaryField, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> ai_blocks.ZhSummaryValue:
        return ai_blocks.ZhSummaryValue(
            zh_summary_id=self.zh_summary_id,
            research_question=self.research_question,
            method=self.method,
            theory=self.theory,
            findings=self.findings,
            reported_fields=self.reported_fields,
        )

    @classmethod
    def from_domain(cls, value: ai_blocks.ZhSummaryValue) -> Self:
        return cls(
            zh_summary_id=value.zh_summary_id,
            research_question=value.research_question,
            method=value.method,
            theory=value.theory,
            findings=value.findings,
            reported_fields=value.reported_fields,
        )


class ClassificationField(BaseModel):
    """分類的一欄（主題或方法）。

    - `keys`：排序過、不重複的 `key`（可能是 `unclassified`）。
    - `human_verified`：這一欄是人工值（「已核對」）。
    - `fix_member_count`：修正過這一欄的成員人數。
    - `members_disagree`：兩位以上成員各自最新的修正不一樣（「意見不同」）。
    """

    model_config = _FROZEN

    keys: tuple[str, ...]
    human_verified: StrictBool
    fix_member_count: int = Field(ge=0, strict=True)
    members_disagree: StrictBool

    @model_validator(mode="after")
    def _check(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> ai_blocks.ClassificationFieldValue:
        return ai_blocks.ClassificationFieldValue(
            keys=self.keys,
            human_verified=self.human_verified,
            fix_member_count=self.fix_member_count,
            members_disagree=self.members_disagree,
        )

    @classmethod
    def from_domain(cls, value: ai_blocks.ClassificationFieldValue) -> Self:
        return cls(
            keys=value.keys,
            human_verified=value.human_verified,
            fix_member_count=value.fix_member_count,
            members_disagree=value.members_disagree,
        )


class ClassificationContent(BaseModel):
    """有效的分類：主題與方法兩欄；沒有值的那一欄是 null（只有人工修正、沒有 AI 值時才會發生）。

    - `fix_member_count`：修正過這篇分類（主題或方法）的成員人數，整篇去重
      （「有 N 位成員修正過」）。
    """

    model_config = _FROZEN

    topic: ClassificationField | None
    method: ClassificationField | None
    fix_member_count: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _check(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> ai_blocks.ClassificationValue:
        return ai_blocks.ClassificationValue(
            topic=None if self.topic is None else self.topic.to_domain(),
            method=None if self.method is None else self.method.to_domain(),
            fix_member_count=self.fix_member_count,
        )

    @classmethod
    def from_domain(cls, value: ai_blocks.ClassificationValue) -> Self:
        return cls(
            topic=None if value.topic is None else ClassificationField.from_domain(value.topic),
            method=None if value.method is None else ClassificationField.from_domain(value.method),
            fix_member_count=value.fix_member_count,
        )


class ConstructItem(BaseModel):
    """研究模型提到的一個構面。信效度的數字是字串（`Decimal`，不經浮點）。"""

    model_config = _FROZEN

    construct_id: uuid.UUID
    name_en: str
    label_zh: str
    definition: str | None
    scale_source: str | None
    item_location: str | None
    item_count: int | None = Field(strict=True)
    cronbach_alpha: DecimalText | None
    composite_reliability: DecimalText | None
    ave: DecimalText | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> ai_blocks.ConstructValue:
        return ai_blocks.ConstructValue(
            construct_id=self.construct_id,
            name_en=self.name_en,
            label_zh=self.label_zh,
            definition=self.definition,
            scale_source=self.scale_source,
            item_location=self.item_location,
            item_count=self.item_count,
            cronbach_alpha=self.cronbach_alpha,
            composite_reliability=self.composite_reliability,
            ave=self.ave,
        )

    @classmethod
    def from_domain(cls, value: ai_blocks.ConstructValue) -> Self:
        return cls(
            construct_id=value.construct_id,
            name_en=value.name_en,
            label_zh=value.label_zh,
            definition=value.definition,
            scale_source=value.scale_source,
            item_location=value.item_location,
            item_count=value.item_count,
            cronbach_alpha=value.cronbach_alpha,
            composite_reliability=value.composite_reliability,
            ave=value.ave,
        )


class HypothesisItem(BaseModel):
    """研究模型的一條關係（因與果是 `ConstructItem.construct_id`）。`beta`、`p_value` 是字串。"""

    model_config = _FROZEN

    ordinal: int = Field(ge=1, strict=True)
    sentence: str
    cause_construct_id: uuid.UUID
    effect_construct_id: uuid.UUID
    evidence_type: EvidenceType
    support: SupportLevel
    beta: DecimalText | None
    p_value: DecimalText | None
    sample_size: int | None = Field(strict=True)
    effect_type: str | None
    result_quote: str | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> ai_blocks.HypothesisValue:
        return ai_blocks.HypothesisValue(
            ordinal=self.ordinal,
            sentence=self.sentence,
            cause_construct_id=self.cause_construct_id,
            effect_construct_id=self.effect_construct_id,
            evidence_type=self.evidence_type,
            support=self.support,
            beta=self.beta,
            p_value=self.p_value,
            sample_size=self.sample_size,
            effect_type=self.effect_type,
            result_quote=self.result_quote,
        )

    @classmethod
    def from_domain(cls, value: ai_blocks.HypothesisValue) -> Self:
        return cls(
            ordinal=value.ordinal,
            sentence=value.sentence,
            cause_construct_id=value.cause_construct_id,
            effect_construct_id=value.effect_construct_id,
            evidence_type=value.evidence_type,
            support=value.support,
            beta=value.beta,
            p_value=value.p_value,
            sample_size=value.sample_size,
            effect_type=value.effect_type,
            result_quote=value.result_quote,
        )


class ResearchModelContent(BaseModel):
    """有效的研究模型：研究情境、樣本數、限制、未來研究、構面與關係。"""

    model_config = _FROZEN

    research_model_id: uuid.UUID
    is_review_or_meta_analysis: StrictBool | None
    country: str | None
    industry: str | None
    research_design: str | None
    unit_of_analysis: str | None
    sample_size: int | None = Field(strict=True)
    limitations: str | None
    future_research: str | None
    constructs: tuple[ConstructItem, ...]
    hypotheses: tuple[HypothesisItem, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> ai_blocks.ResearchModelValue:
        return ai_blocks.ResearchModelValue(
            research_model_id=self.research_model_id,
            is_review_or_meta_analysis=self.is_review_or_meta_analysis,
            country=self.country,
            industry=self.industry,
            research_design=self.research_design,
            unit_of_analysis=self.unit_of_analysis,
            sample_size=self.sample_size,
            limitations=self.limitations,
            future_research=self.future_research,
            constructs=tuple(item.to_domain() for item in self.constructs),
            hypotheses=tuple(item.to_domain() for item in self.hypotheses),
        )

    @classmethod
    def from_domain(cls, value: ai_blocks.ResearchModelValue) -> Self:
        return cls(
            research_model_id=value.research_model_id,
            is_review_or_meta_analysis=value.is_review_or_meta_analysis,
            country=value.country,
            industry=value.industry,
            research_design=value.research_design,
            unit_of_analysis=value.unit_of_analysis,
            sample_size=value.sample_size,
            limitations=value.limitations,
            future_research=value.future_research,
            constructs=tuple(ConstructItem.from_domain(item) for item in value.constructs),
            hypotheses=tuple(HypothesisItem.from_domain(item) for item in value.hypotheses),
        )


# --- 三種 AI 區塊的信封（`state`／`value`／`provenance`）---------------------------------------


def _check_envelope(
    product: ai_blocks.AiProduct,
    state: ai_blocks.AiBlockState,
    value: ZhSummaryContent | ClassificationContent | ResearchModelContent | None,
    provenance: Provenance | None,
) -> None:
    """組回 `domain/ai_blocks.AiBlock`，由它檢查信封的不變式。"""
    ai_blocks.AiBlock(
        product,
        state,
        None if value is None else value.to_domain(),
        None if provenance is None else provenance.to_domain(),
    )


def _checked_block(block: ai_blocks.AiBlock, product: ai_blocks.AiProduct) -> ai_blocks.AiBlock:
    if not isinstance(block, ai_blocks.AiBlock) or block.product is not product:
        raise ValueError(f"要是 {product.value} 的 AiBlock")
    return block


def _provenance_of(block: ai_blocks.AiBlock) -> Provenance | None:
    return None if block.provenance is None else Provenance.from_domain(block.provenance)


class ZhSummaryBlock(BaseModel):
    """中文重點區塊。只有 `ready` 帶 `value`；只有 `ready`、`empty` 帶 `provenance`。"""

    model_config = _FROZEN

    state: ai_blocks.AiBlockState
    value: ZhSummaryContent | None
    provenance: Provenance | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_envelope(ai_blocks.AiProduct.ZH_SUMMARY, self.state, self.value, self.provenance)
        return self

    @classmethod
    def from_block(cls, block: ai_blocks.AiBlock) -> Self:
        """`repo/ai_blocks.find_ai_block` 判定的區塊 → 回應的形狀（只轉換，不判斷狀態）。

        Raises:
            ValueError: 區塊不是中文重點。
        """
        block = _checked_block(block, ai_blocks.AiProduct.ZH_SUMMARY)
        value = block.value
        assert value is None or isinstance(value, ai_blocks.ZhSummaryValue)
        return cls(
            state=block.state,
            value=None if value is None else ZhSummaryContent.from_domain(value),
            provenance=_provenance_of(block),
        )


class ClassificationBlock(BaseModel):
    """分類區塊。不會是 `empty`（主題一個都不剩時是 `unclassified`）。"""

    model_config = _FROZEN

    state: ai_blocks.AiBlockState
    value: ClassificationContent | None
    provenance: Provenance | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_envelope(ai_blocks.AiProduct.CLASSIFICATION, self.state, self.value, self.provenance)
        return self

    @classmethod
    def from_block(cls, block: ai_blocks.AiBlock) -> Self:
        """`find_ai_block` 判定的區塊 → 回應的形狀。

        Raises:
            ValueError: 區塊不是分類。
        """
        block = _checked_block(block, ai_blocks.AiProduct.CLASSIFICATION)
        value = block.value
        assert value is None or isinstance(value, ai_blocks.ClassificationValue)
        return cls(
            state=block.state,
            value=None if value is None else ClassificationContent.from_domain(value),
            provenance=_provenance_of(block),
        )


class ResearchModelBlock(BaseModel):
    """研究模型區塊。"""

    model_config = _FROZEN

    state: ai_blocks.AiBlockState
    value: ResearchModelContent | None
    provenance: Provenance | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_envelope(ai_blocks.AiProduct.RESEARCH_MODEL, self.state, self.value, self.provenance)
        return self

    @classmethod
    def from_block(cls, block: ai_blocks.AiBlock) -> Self:
        """`find_ai_block` 判定的區塊 → 回應的形狀。

        Raises:
            ValueError: 區塊不是研究模型。
        """
        block = _checked_block(block, ai_blocks.AiProduct.RESEARCH_MODEL)
        value = block.value
        assert value is None or isinstance(value, ai_blocks.ResearchModelValue)
        return cls(
            state=block.state,
            value=None if value is None else ResearchModelContent.from_domain(value),
            provenance=_provenance_of(block),
        )


# --- 書目、摘要、全文連結 -----------------------------------------------------------------------


class PaperBiblio(BaseModel):
    """論文的書目與狀態標記需要的欄位。

    - `journal` 是設定檔的期刊代號（不是追蹤的期刊時是 null，這時看 `journal_issn_l`）。
    - `doi`、`openalex_id` 是正規化後的寫法；一篇有好幾個時是最早記上的那一個。
    - `doi_url`、`openalex_url` 和它們一起有值或一起是 null。
    """

    model_config = _FROZEN

    id: uuid.UUID
    title: _Text
    authors: tuple[str, ...]
    publication_year: int | None = Field(strict=True)
    journal: _JournalKey | None
    journal_issn_l: str | None
    volume: str | None
    issue: str | None
    first_page: str | None
    last_page: str | None
    doi: _Text | None
    doi_url: str | None
    openalex_id: _Text | None
    openalex_url: str | None
    cited_by_count: int | None = Field(ge=0, strict=True)
    in_scope: StrictBool
    origin: PaperOrigin
    is_retracted: StrictBool

    @model_validator(mode="after")
    def _check_links(self) -> Self:
        expected_doi = None if self.doi is None else doi_url(self.doi)
        expected_openalex = None if self.openalex_id is None else openalex_url(self.openalex_id)
        if self.doi_url != expected_doi:
            raise ValueError("doi_url 要由 doi 產生（沒有 doi 時是 null）")
        if self.openalex_url != expected_openalex:
            raise ValueError("openalex_url 要由 openalex_id 產生（沒有 openalex_id 時是 null）")
        return self

    @classmethod
    def from_facts(cls, facts: PaperPageFacts, journal_key: str | None) -> Self:
        return cls(
            id=facts.paper_id,
            title=facts.title,
            authors=facts.authors,
            publication_year=facts.publication_year,
            journal=journal_key,
            journal_issn_l=facts.journal_issn_l,
            volume=facts.volume,
            issue=facts.issue,
            first_page=facts.first_page,
            last_page=facts.last_page,
            doi=facts.doi,
            doi_url=None if facts.doi is None else doi_url(facts.doi),
            openalex_id=facts.openalex_id,
            openalex_url=None if facts.openalex_id is None else openalex_url(facts.openalex_id),
            cited_by_count=facts.cited_by_count,
            in_scope=facts.in_scope,
            origin=facts.origin,
            is_retracted=facts.is_retracted,
        )


class PaperRow(BaseModel):
    """清單上的一篇論文（文獻庫、3.13 的論文清單共用）。

    - `authors` 只有前 3 位，`author_count` 是全部的人數。
    - `journal` 是設定檔的期刊代號（不是追蹤的期刊時是 null，這時看 `journal_issn_l`）。
    - `doi` 是正規化後的寫法（一篇有好幾個時是最早記上的那一個）；`doi_url` 由它產生，和它一起有值
      或一起是 null。
    - `has_abstract`：有沒有目前的摘要。
    - `in_my_library`：這篇在不在**目前登入的成員自己**的文獻庫（Story 3.17、ADR-0053）；
      別人的文獻庫不算。
    """

    model_config = _FROZEN

    id: uuid.UUID
    title: _Text
    authors: tuple[str, ...] = Field(max_length=PAPER_ROW_AUTHOR_LIMIT)
    author_count: int = Field(ge=0, strict=True)
    publication_year: int | None = Field(strict=True)
    journal: _JournalKey | None
    journal_issn_l: str | None
    doi: _Text | None
    doi_url: str | None
    has_abstract: StrictBool
    in_scope: StrictBool
    is_retracted: StrictBool
    in_my_library: StrictBool

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.author_count < len(self.authors):
            raise ValueError("author_count 不能比 authors 的人數少")
        expected = None if self.doi is None else doi_url(self.doi)
        if self.doi_url != expected:
            raise ValueError("doi_url 要由 doi 產生（沒有 doi 時是 null）")
        return self

    @classmethod
    def from_facts(cls, facts: PaperRowFacts, journal_key: str | None) -> Self:
        """`repo/paper_rows.find_paper_rows` 讀出的一列 → 回應的形狀（期刊代號由呼叫端對好）。"""
        return cls(
            id=facts.paper_id,
            title=facts.title,
            authors=facts.authors,
            author_count=facts.author_count,
            publication_year=facts.publication_year,
            journal=journal_key,
            journal_issn_l=facts.journal_issn_l,
            doi=facts.doi,
            doi_url=None if facts.doi is None else doi_url(facts.doi),
            has_abstract=facts.has_abstract,
            in_scope=facts.in_scope,
            is_retracted=facts.is_retracted,
            in_my_library=facts.in_my_library,
        )


class PaperAbstract(BaseModel):
    """目前的原文摘要（全站只有一筆 `is_current`，ADR-0015）與它的來源。"""

    model_config = _FROZEN

    content: _Text
    source: AbstractSource

    @classmethod
    def from_current(cls, abstract: CurrentAbstract) -> Self:
        return cls(content=abstract.content, source=abstract.source)


class FullTextLinks(BaseModel):
    """取得全文的兩個連結（FR-32）：DOI 與開放取用的版本。

    `open_access_url` 目前一律是 null：資料庫沒有存開放取用的位置（parser 不讀），要不要另外做
    由 Kevin 決定（ADR-0048）。
    """

    model_config = _FROZEN

    doi_url: str | None
    open_access_url: str | None

    @classmethod
    def from_facts(cls, facts: PaperPageFacts) -> Self:
        return cls(doi_url=None if facts.doi is None else doi_url(facts.doi), open_access_url=None)


# --- 顯示名稱與前端的設定值 ---------------------------------------------------------------------


class DisplayName(BaseModel):
    """一個代號的顯示名稱。"""

    model_config = _FROZEN

    key: _Text
    name: _Text


class DisplayNames(BaseModel):
    """期刊、主題、方法的顯示名稱（照設定檔的順序）。`unclassified` 不在裡面（介面固定文字）。"""

    model_config = _FROZEN

    journals: tuple[DisplayName, ...]
    topics: tuple[DisplayName, ...]
    methods: tuple[DisplayName, ...]


def build_display_names(journals: JournalsConfig, taxonomy: Taxonomy) -> DisplayNames:
    """設定檔（`journals.yaml`、`taxonomy.yaml`）→ 顯示名稱，順序照設定檔。"""
    return DisplayNames(
        journals=tuple(DisplayName(key=item.key, name=item.name) for item in journals.journals),
        topics=tuple(
            DisplayName(key=item.key, name=item.name)
            for item in taxonomy.items(TaxonomyField.TOPIC)
        ),
        methods=tuple(
            DisplayName(key=item.key, name=item.name)
            for item in taxonomy.items(TaxonomyField.METHOD)
        ),
    )


class ClientConfig(BaseModel):
    """前端要用的設定值（AD-18）。

    - `timezone`：畫面上的日期換成哪個時區（IANA 名稱）。
    - `library_page_size`：文獻庫清單一頁幾篇（Story 3.16）。
    - `paper_list_page_size`：找論文清單一頁幾篇（Story 3.13）。
    """

    model_config = _FROZEN

    timezone: _Text
    library_page_size: int = Field(ge=1, strict=True)
    paper_list_page_size: int = Field(ge=1, strict=True)


def build_client_config() -> ClientConfig:
    """前端的設定值：時區是資料截止日用的那一個（`domain/snapshot.DATA_TIMEZONE`）；文獻庫一頁的
    篇數是 `domain/library.LIBRARY_PAGE_SIZE`，找論文清單一頁的篇數是
    `domain/paper_list.PAPER_LIST_PAGE_SIZE`。"""
    return ClientConfig(
        timezone=DATA_TIMEZONE.key,
        library_page_size=LIBRARY_PAGE_SIZE,
        paper_list_page_size=PAPER_LIST_PAGE_SIZE,
    )


# --- 計數方塊（Story 3.15、AD-19、ADR-0011、ADR-0062）-------------------------------------------


class TrendParams(BaseModel):
    """趨勢與覆蓋率的規則用到的設定值（ADR-0011 的 `params`）：成員版的計數方塊回應、示範站的
    `cube.json`、`tests/golden/stats/` 的資料檔都用這一個形狀。前端的 `web/src/stats/` 只用傳進來
    的值，不寫死。

    - `timezone`：資料截止日用的時區（IANA 名稱）。
    - `tracking_start_year`：追蹤起始年（`journals.yaml` 的 `start_year`）。
    - `current_year_cutoff`：當年度納入「成長最快」的分界日（`MM-DD`，含當天）。
    - `growth_window_years`：成長最快每組幾年。
    - `baseline_years`：原始篇數的對照值取最早幾個完整年度。
    - `low_coverage_threshold`：摘要覆蓋率低於這個比例就標成偏低（0 到 1）。
    """

    model_config = _FROZEN

    timezone: _Text
    tracking_start_year: int = Field(ge=1900, le=9999, strict=True)
    current_year_cutoff: MonthDay
    growth_window_years: int = Field(ge=1, strict=True)
    baseline_years: int = Field(ge=1, strict=True)
    low_coverage_threshold: float = Field(ge=0, le=1)

    @field_validator("current_year_cutoff")
    @classmethod
    def _real_date(cls, value: str) -> str:
        return check_month_day(value)


def build_trend_params(trend: TrendConfig, journals: JournalsConfig) -> TrendParams:
    """設定值 → `params`：`trend.yaml` 的四個值、`journals.yaml` 的追蹤起始年、資料截止日的時區
    （`domain/snapshot.DATA_TIMEZONE`）。由 services 與 api 的組裝呼叫，`repo` 不碰（AD-1）。"""
    return TrendParams(
        timezone=DATA_TIMEZONE.key,
        tracking_start_year=journals.start_year,
        current_year_cutoff=trend.current_year_cutoff,
        growth_window_years=trend.growth_window_years,
        baseline_years=trend.baseline_years,
        low_coverage_threshold=trend.low_coverage_threshold,
    )


class CubeResponse(BaseModel):
    """成員版的計數方塊（`GET /api/v1/cube`）：只有計數，不給比例（AD-19）。

    - `rows`：Story 1.10 定義的 `total` 與 `with_abstract` 兩種列（期刊照設定的順序、年份遞增）；
      主題、方法等其他種類等用到的 Story 再加（ADR-0062）。
    - `params`：規則用到的設定值。
    - `snapshot`：目前已發佈的快照；一個都還沒發佈時是 null，這時 `rows` 是空的（算數的論文只算
      已發佈快照的，AD-5）。
    """

    model_config = _FROZEN

    rows: tuple[CubeRow, ...]
    params: TrendParams
    snapshot: SnapshotRef | None

    @model_validator(mode="after")
    def _rows_need_snapshot(self) -> Self:
        if self.snapshot is None and self.rows:
            raise ValueError("還沒有已發佈的快照時不會有計數")
        return self
