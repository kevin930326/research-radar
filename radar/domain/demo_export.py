"""示範站匯出的白名單與自我檢查（Story 3.7、ADR-0007、ADR-0061、AD-20）。

- **白名單**：每一種資料檔一個「多餘欄位即報錯」的模型（`DEMO_FILE_MODELS`），全部列在
  `demo-openapi.json`（`build_demo_openapi` 產生，repo 裡放一份，測試檢查兩邊一致）。論文只有兩層：
  `DemoPaperBiblio`（全部 `counted_paper`）與 `DemoPaperFull`（只有示範站有完整內容的論文）。
  書目、分類、中文重點、研究模型照 ADR-0007 第 4 點逐欄列（`DemoBiblio` 等），不整個套成員版的
  共用模型；本身整個在清單裡的形狀（快照、來歷、顯示名稱、原文摘要、全文連結、構面與假設）才共用
  `domain/contracts.py` 的模型（ADR-0010 第 12 項、ADR-0061）。
- **目錄**：`preview/manifest.json` 與 `preview/<匯出編號>/…`（`RADAR_DEMO_PUBLISH` 開的時候是
  `data/`，結構相同；這個 epic 不開）。資料檔的路徑由 `data_file_path`、`shard_file_name` 決定。
- **自我檢查**（`check_demo_files`，寫 manifest 之前）：ADR-0007 第 1、2、3 項，與第 6 項的分片
  與索引部分（排除清單裡的出版社或期刊的論文沒有完整內容）。第 4 項（重算計數）、第 5 項與第 6 項的
  關係、問答、首頁部分由之後的 Story 補（ADR-0061「留到之後」）。
- 這裡只有純函式與模型；讀資料庫、寫檔在 `services/demo_export.py`。
"""

import contextlib
import json
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Final, Self, get_args
from urllib.parse import unquote

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic.json_schema import models_json_schema

from radar.domain.ai_blocks import (
    AiBlock,
    AiBlockState,
    AiProduct,
    ClassificationFieldValue,
    ClassificationValue,
    ResearchModelValue,
    ZhSummaryValue,
)
from radar.domain.contracts import (
    ConstructItem,
    DisplayNames,
    FullTextLinks,
    HypothesisItem,
    PaperAbstract,
    Provenance,
    SnapshotRef,
    UtcDateTime,
    doi_url,
    openalex_url,
)
from radar.domain.papers import AbstractSource
from radar.domain.policy import PublisherExclusions
from radar.domain.snapshot import DATA_TIMEZONE

# --- 目錄與檔名 ---------------------------------------------------------------------------------

# `RADAR_DEMO_PUBLISH` 關的時候（預設，這個 epic 不開）寫到這個目錄；正式是 `data/`，結構相同。
PREVIEW_DIR: Final = "preview"
MANIFEST_FILE: Final = "manifest.json"
TAXONOMY_FILE: Final = "taxonomy.json"
PAPERS_INDEX_FILE: Final = "papers-index.json"
TITLES_FILE: Final = "titles.json"
PAPERS_DIR: Final = "papers"
# 論文詳情每 200 篇一個分片檔（ADR-0007「檔案」）：第 i 列在第 ⌊i÷200⌋ 個分片。
SHARD_SIZE: Final = 200
# 分片檔名的位數（`000.json`）：一萬篇是 50 個分片，三位數夠到 20 萬篇。
_SHARD_DIGITS: Final = 3
_MAX_SHARDS: Final = 10**_SHARD_DIGITS

# 匯出編號＝快照編號加這個快照的第幾次匯出（例如 `12-2`）。
EXPORT_ID_PATTERN: Final = r"^[1-9][0-9]{0,8}-[1-9][0-9]{0,8}$"
ExportId = Annotated[str, StringConstraints(pattern=EXPORT_ID_PATTERN)]
_EXPORT_ID = re.compile(EXPORT_ID_PATTERN)

# 分類標籤可以公開的「目前的摘要」來源（AD-20：摘要來源是 Semantic Scholar 的論文，完整內容與分類
# 標籤都不輸出）。沒有目前的摘要的論文（分類只看標題）也輸出標籤。這和 view `demo_full_paper` 的
# 「摘要來源允許公開」是同一個清單（遷移 0021）；整合測試檢查兩邊一致（ADR-0061）。
DEMO_LABEL_ABSTRACT_SOURCES: Final = frozenset({AbstractSource.OPENALEX})

# 示範站論文清單一頁幾篇（epics Story 3.14）：放在 manifest 的 `client_config`，前端不另外寫一份
# （AD-21、ADR-0061）。成員版的找論文清單一頁篇數不變。
DEMO_PAPER_LIST_PAGE_SIZE: Final = 50


def export_id_for(snapshot_number: int, sequence: int) -> str:
    """快照編號與序號 → 匯出編號（`12-2`）。

    Raises:
        ValueError: 任一個不是正整數。
    """
    for name, value in (("snapshot_number", snapshot_number), ("sequence", sequence)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} 要是正整數")
    return f"{snapshot_number}-{sequence}"


def shard_file_name(shard: int) -> str:
    """第 `shard` 個分片的檔名（`000.json`）。

    Raises:
        ValueError: 不是 0 到 999 的整數。
    """
    if type(shard) is not int or not 0 <= shard < _MAX_SHARDS:
        raise ValueError(f"分片編號要是 0 到 {_MAX_SHARDS - 1} 的整數")
    return f"{shard:0{_SHARD_DIGITS}d}.json"


def export_order(export_id: str) -> tuple[int, int]:
    """匯出編號 → （快照編號, 序號），用來比哪一次比較新（大的比較新）。

    Raises:
        ValueError: 匯出編號的格式不對。
    """
    if not isinstance(export_id, str) or not _EXPORT_ID.fullmatch(export_id):
        raise ValueError("匯出編號的格式不對")
    snapshot_number, _, sequence = export_id.partition("-")
    return int(snapshot_number), int(sequence)


def data_file_path(export_id: str, name: str) -> str:
    """資料檔相對於 `preview/`（或 `data/`）的路徑：`<匯出編號>/<name>`。

    Raises:
        ValueError: 匯出編號的格式不對。
    """
    if not _EXPORT_ID.fullmatch(export_id):
        raise ValueError("匯出編號的格式不對")
    return f"{export_id}/{name}"


# --- 狀態表 `demo_export`（AD-14、AR-17）------------------------------------------------------


class DemoExportStatus(StrEnum):
    """一次匯出的狀態。同一個快照同時只能有一筆 `queued` 或 `running`（部分唯一索引）。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DemoExportFailure(StrEnum):
    """匯出失敗的原因代碼（`demo_export.failure_reason`；細節只在日誌）。"""

    NO_OUTPUT_DIR = "no_output_dir"  # 沒有設定匯出資料夾（RADAR_DEMO_EXPORT_DIR）
    CONFIG_ERROR = "config_error"  # worker 讀設定檔失敗（還沒開始匯出）
    PUBLISH_NOT_READY = "publish_not_ready"  # 開了 RADAR_DEMO_PUBLISH，但正式發佈還沒做
    SNAPSHOT_CHANGED = "snapshot_changed"  # 排入之後發佈了新的快照（照新的快照再排一次）
    SELF_CHECK_FAILED = "self_check_failed"  # 寫 manifest 之前的自我檢查沒過
    OUTPUT_EXISTS = "output_exists"  # 這個匯出編號的目錄已經存在（不覆寫）
    OUTPUT_UNSAFE = "output_unsafe"  # 輸出位置是符號連結或跑出輸出資料夾（不寫）
    SUPERSEDED = "superseded"  # manifest 已經指向比這次新的匯出（不蓋回舊的）
    MANIFEST_UNREADABLE = "manifest_unreadable"  # 現有的 manifest 讀不懂（不知道新舊，不蓋）
    OUTPUT_BUSY = "output_busy"  # 等發佈的檔案鎖超過時限（拿著的一方卡住了）
    # 行程被中斷（Ctrl-C 這類變成例外的中斷）。SIGTERM 不會變成例外：被系統結束的那一筆停在
    # running，6 小時後由 sweep 補排。
    INTERRUPTED = "interrupted"
    ERROR = "error"  # 其他錯誤（類別記在日誌）


class DemoExportBusyError(Exception):
    """最新的快照已經有一筆排隊中或執行中的匯出。"""


@dataclass(frozen=True, slots=True)
class DemoExportRun:
    """`demo_export` 的一列（含它的快照編號）。`run_token`：接手執行的那一次的代號（`running` 才有；
    只有拿著這個代號的行程能寫檔、能把它結束）。"""

    id: uuid.UUID
    snapshot_id: uuid.UUID
    snapshot_number: int
    sequence: int
    status: DemoExportStatus
    run_token: uuid.UUID | None = None

    @property
    def export_id(self) -> str:
        return export_id_for(self.snapshot_number, self.sequence)


# --- 匯出要讀的論文欄位（repo 讀、services 組）------------------------------------------------


def _check_text_tuple(name: str, value: object) -> None:
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} 要是字串的 tuple")


@dataclass(frozen=True, slots=True)
class DemoPaperFacts:
    """`repo/papers.list_demo_paper_facts` 讀出的一篇論文（`PublicViewer` 看得到的，也就是
    `counted_paper` 裡的）。不含摘要內容：摘要只對 `in_demo_full_paper` 的論文另外讀。

    - `doi`、`openalex_id`：正規化後的值；一篇有好幾個時是最早記上的那一個。
    - `publisher_ids`、`publisher_names`：出版社自己那一版的出版社 ID 與名稱（含上層出版社），
      匯出時再用排除清單直接判一次（ADR-0007 第 6 項、ADR-0061）；不寫進任何檔。
    - `abstract_source`：目前的摘要的來源；沒有目前的摘要是 None。
    - `in_demo_full_paper`：在 view `demo_full_paper` 裡（示範站有完整內容）。
    - `is_new_this_week`：第一次算進的快照（`first_snapshot_id`）就是最新已發佈的快照。

    Raises:
        ValueError: `authors`、`publisher_ids`、`publisher_names` 不是字串的 tuple、
            被引用次數是負的。
    """

    paper_id: uuid.UUID
    title: str
    authors: tuple[str, ...]
    publication_year: int | None
    journal_issn_l: str | None
    volume: str | None
    issue: str | None
    first_page: str | None
    last_page: str | None
    cited_by_count: int | None
    doi: str | None
    openalex_id: str | None
    publisher_ids: tuple[str, ...]
    publisher_names: tuple[str, ...]
    abstract_source: AbstractSource | None
    in_demo_full_paper: bool
    is_new_this_week: bool

    def __post_init__(self) -> None:
        _check_text_tuple("authors", self.authors)
        _check_text_tuple("publisher_ids", self.publisher_ids)
        _check_text_tuple("publisher_names", self.publisher_names)
        if self.cited_by_count is not None and self.cited_by_count < 0:
            raise ValueError("cited_by_count 不能是負的")

    @property
    def labels_allowed(self) -> bool:
        """分類標籤可以公開：沒有目前的摘要，或目前的摘要來源在 `DEMO_LABEL_ABSTRACT_SOURCES`。"""
        return self.abstract_source is None or self.abstract_source in DEMO_LABEL_ABSTRACT_SOURCES


@dataclass(frozen=True, slots=True)
class DemoPublisherFacts:
    """`repo/papers.list_demo_publisher_facts` 讀出的一篇論文的出版社與期刊欄位（自我檢查第 6 項：
    直接用出版社欄位查，不經過 `demo_eligible`，ADR-0007）。

    Raises:
        ValueError: `publisher_ids`、`publisher_names` 不是字串的 tuple。
    """

    paper_id: uuid.UUID
    publisher_ids: tuple[str, ...]
    publisher_names: tuple[str, ...]
    journal_issn_l: str | None

    def __post_init__(self) -> None:
        _check_text_tuple("publisher_ids", self.publisher_ids)
        _check_text_tuple("publisher_names", self.publisher_names)


def excluded_publisher_paper_ids(
    papers: Iterable[DemoPublisherFacts], exclusions: PublisherExclusions
) -> frozenset[uuid.UUID]:
    """出版社（ID 或名稱，含上層出版社）或期刊 ISSN-L 對上「示範站不放完整內容」清單的論文 id。

    直接用 `PublisherExclusions.matches` 判（和 `is_demo_eligible` 同一份清單、同一個比對規則），
    不看資料庫存的 `demo_eligible`：清單改了卻還沒 `radar reparse` 時，舊的 `demo_eligible` 可能
    還是 true（ADR-0052），這一道照樣擋得住。
    """
    return frozenset(
        paper.paper_id
        for paper in papers
        if exclusions.matches(
            paper.publisher_ids, paper.publisher_names, journal_issn_l=paper.journal_issn_l
        )
    )


# --- 白名單模型 ---------------------------------------------------------------------------------
#
# 每一個欄位都照 ADR-0007 第 4 點的清單逐欄列出（ADR-0061「欄位」）：不直接套成員版的共用模型，
# 免得成員版多出來的欄位（修正人數、意見不一、回報的欄位、`in_scope`、`origin`、`is_retracted`、
# 內部的產出 ID）跟著出去。只有本身就整個在清單裡的形狀（快照、來歷、顯示名稱、原文摘要、全文連結、
# 構面與假設）才共用成員版的模型。

_WHITELIST = ConfigDict(frozen=True, extra="forbid")
_Text = Annotated[str, StringConstraints(min_length=1)]
_JournalKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
_WITH_PROVENANCE: Final = frozenset({AiBlockState.READY, AiBlockState.EMPTY})


def _check_demo_envelope(
    state: AiBlockState,
    value: object | None,
    provenance: Provenance | None,
    *,
    classification: bool,
) -> None:
    """AI 區塊信封的不變式（和 `domain/ai_blocks.AiBlock` 相同）：只有 `ready`、`empty` 帶來歷而且
    一定要帶；只有 `ready` 帶值而且一定要帶；分類不會是 `empty`。"""
    if (state in _WITH_PROVENANCE) != (provenance is not None):
        raise ValueError("只有 ready 與 empty 帶 provenance，而且一定要帶")
    if (state is AiBlockState.READY) != (value is not None):
        raise ValueError("只有 ready 帶 value，而且一定要帶")
    if classification and state is AiBlockState.EMPTY:
        raise ValueError("分類不會是 empty（至少有 unclassified）")


def _demo_provenance(block: AiBlock) -> Provenance | None:
    return None if block.provenance is None else Provenance.from_domain(block.provenance)


def _checked(block: AiBlock, product: AiProduct) -> AiBlock:
    if not isinstance(block, AiBlock) or block.product is not product:
        raise ValueError(f"要是 {product.value} 的 AiBlock")
    return block


class DemoBiblio(BaseModel):
    """論文的書目（ADR-0007 第 4 點）：論文 ID（內部 ID，和成員版的網址一致）、標題、作者、期刊、
    年份、卷期頁碼、DOI 與 OpenAlex 連結、被引用次數。`doi_url`、`openalex_url` 由 `doi`、
    `openalex_id` 產生，和它們一起有值或一起是 null。"""

    model_config = _WHITELIST

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

    @model_validator(mode="after")
    def _check_links(self) -> Self:
        if self.doi_url != (None if self.doi is None else doi_url(self.doi)):
            raise ValueError("doi_url 要由 doi 產生（沒有 doi 時是 null）")
        expected = None if self.openalex_id is None else openalex_url(self.openalex_id)
        if self.openalex_url != expected:
            raise ValueError("openalex_url 要由 openalex_id 產生（沒有 openalex_id 時是 null）")
        return self


class DemoClassificationField(BaseModel):
    """分類的一欄（主題或方法）：代號（排序過、不重複，可能是 `unclassified`）與是否已核對
    （人工值）。
    誰修正的、幾位成員修正過、意見是否不同都不在白名單裡（ADR-0007 第 6 點）。"""

    model_config = _WHITELIST

    keys: tuple[_Text, ...]
    human_verified: StrictBool

    @classmethod
    def from_domain(cls, value: ClassificationFieldValue) -> Self:
        return cls(keys=value.keys, human_verified=value.human_verified)


class DemoClassificationContent(BaseModel):
    """有效的分類：主題與方法兩欄；沒有值的那一欄是 null。"""

    model_config = _WHITELIST

    topic: DemoClassificationField | None
    method: DemoClassificationField | None


class DemoClassificationBlock(BaseModel):
    """分類標籤與它的 AI 標章資料（來歷含是否已核對；`input_kind` 是 `title`＝只根據標題分類）。"""

    model_config = _WHITELIST

    state: AiBlockState
    value: DemoClassificationContent | None
    provenance: Provenance | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_demo_envelope(self.state, self.value, self.provenance, classification=True)
        return self

    @classmethod
    def from_block(cls, block: AiBlock) -> Self:
        """`repo/ai_blocks.find_ai_block` 判定的分類區塊 → 示範站的形狀（逐欄挑，不整個帶）。

        Raises:
            ValueError: 區塊不是分類。
        """
        block = _checked(block, AiProduct.CLASSIFICATION)
        value = block.value
        assert value is None or isinstance(value, ClassificationValue)
        return cls(
            state=block.state,
            value=None
            if value is None
            else DemoClassificationContent(
                topic=None
                if value.topic is None
                else DemoClassificationField.from_domain(value.topic),
                method=None
                if value.method is None
                else DemoClassificationField.from_domain(value.method),
            ),
            provenance=_demo_provenance(block),
        )


class DemoZhSummaryContent(BaseModel):
    """有效的中文重點：研究問題、方法、理論、發現（成員回報的欄位、內部 ID 不在白名單裡）。"""

    model_config = _WHITELIST

    research_question: str | None
    method: str | None
    theory: str | None
    findings: str | None


class DemoZhSummaryBlock(BaseModel):
    """中文重點區塊（狀態、內容、來歷）。"""

    model_config = _WHITELIST

    state: AiBlockState
    value: DemoZhSummaryContent | None
    provenance: Provenance | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_demo_envelope(self.state, self.value, self.provenance, classification=False)
        return self

    @classmethod
    def from_block(cls, block: AiBlock) -> Self:
        """中文重點區塊 → 示範站的形狀。

        Raises:
            ValueError: 區塊不是中文重點。
        """
        block = _checked(block, AiProduct.ZH_SUMMARY)
        value = block.value
        assert value is None or isinstance(value, ZhSummaryValue)
        return cls(
            state=block.state,
            value=None
            if value is None
            else DemoZhSummaryContent(
                research_question=value.research_question,
                method=value.method,
                theory=value.theory,
                findings=value.findings,
            ),
            provenance=_demo_provenance(block),
        )


class DemoResearchModelContent(BaseModel):
    """有效的研究模型：研究情境、樣本數、限制、未來研究、構面與假設（構面與假設和成員版同一個形狀；
    研究模型本身的內部 ID 不在白名單裡）。"""

    model_config = _WHITELIST

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


class DemoResearchModelBlock(BaseModel):
    """研究模型區塊（狀態、內容、來歷）。"""

    model_config = _WHITELIST

    state: AiBlockState
    value: DemoResearchModelContent | None
    provenance: Provenance | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_demo_envelope(self.state, self.value, self.provenance, classification=False)
        return self

    @classmethod
    def from_block(cls, block: AiBlock) -> Self:
        """研究模型區塊 → 示範站的形狀。

        Raises:
            ValueError: 區塊不是研究模型。
        """
        block = _checked(block, AiProduct.RESEARCH_MODEL)
        value = block.value
        assert value is None or isinstance(value, ResearchModelValue)
        return cls(
            state=block.state,
            value=None
            if value is None
            else DemoResearchModelContent(
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
            ),
            provenance=_demo_provenance(block),
        )


class DemoPaperBiblio(BaseModel):
    """第一層：全部論文（`counted_paper`）都有（ADR-0007 第 4 點）。

    - `biblio`：書目（`DemoBiblio`）。
    - `links`：取得全文的連結（開放取用版本目前一律是 null：成員版也還沒有來源，ADR-0061）。
    - `classification`：分類標籤與 AI 標章的資料；摘要來源不允許公開的論文是 null（AD-20）。
    - `has_abstract`：有沒有目前的摘要；`has_full_content`：示範站有沒有完整內容；
      `is_new_this_week`：本週新增。
    """

    model_config = _WHITELIST

    biblio: DemoBiblio
    links: FullTextLinks
    classification: DemoClassificationBlock | None
    has_abstract: StrictBool
    has_full_content: StrictBool
    is_new_this_week: StrictBool

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.has_full_content and not self.has_abstract:
            raise ValueError("有完整內容的論文一定有摘要")
        if self.links.doi_url != self.biblio.doi_url:
            raise ValueError("links.doi_url 要和書目的 doi_url 一樣")
        return self


class DemoPaperFull(BaseModel):
    """第二層：只有示範站有完整內容的論文才有（ADR-0007 第 4 點）。原文摘要、中文重點、研究模型
    （構面與假設），後兩者帶各自的 AI 區塊狀態與來歷。關鍵字的代號與名稱還沒輸出（ADR-0061）。"""

    model_config = _WHITELIST

    paper_id: uuid.UUID
    abstract: PaperAbstract
    zh_summary: DemoZhSummaryBlock
    research_model: DemoResearchModelBlock


class DemoPaper(BaseModel):
    """分片檔裡的一篇：第一層一定有；第二層只有 `has_full_content` 的論文有，其他一律是 null。"""

    model_config = _WHITELIST

    biblio: DemoPaperBiblio
    full: DemoPaperFull | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.biblio.has_full_content != (self.full is not None):
            raise ValueError("只有 has_full_content 的論文有 full，其他一律是 null")
        if self.full is not None and self.full.paper_id != self.biblio.biblio.id:
            raise ValueError("full 的 paper_id 要和書目的 id 一樣")
        return self


class DemoPaperShard(BaseModel):
    """`papers/NNN.json`：第 `shard` 個分片，`papers-index.json` 第 `shard × 200` 列起的論文。"""

    model_config = _WHITELIST

    snapshot: SnapshotRef
    export_id: ExportId
    shard: int = Field(ge=0, lt=_MAX_SHARDS, strict=True)
    papers: tuple[DemoPaper, ...] = Field(min_length=1, max_length=SHARD_SIZE)


_Year = Annotated[int, Field(strict=True)]
_Key = Annotated[str, StringConstraints(min_length=1)]


class DemoPapersIndex(BaseModel):
    """`papers-index.json`：論文清單篩選與每一列要用的欄位，一欄一個陣列（ADR-0007「檔案」）。
    每一欄的第 i 個值是同一篇；列順序和 `titles.json`、分片檔相同（AD-20「列順序是契約」）。

    - `provenances`：分類的 AI 標章資料的對照表；`classification_provenance` 存它的位置（沒有來歷
      是 null）。「是否只根據標題分類」就是來歷的 `input_kind` 是 `title`。
    - `classification_state`：分類區塊的狀態；摘要來源不允許公開的論文是 null（沒有標籤）。
    - `topics`、`methods`：有效分類的代號；沒有值（狀態不是 `ready`、或那一欄沒有值）是 null。
    - `topics_verified`、`methods_verified`：那一欄是不是人工核對過的值；沒有值是 null。
    """

    model_config = _WHITELIST

    snapshot: SnapshotRef
    export_id: ExportId
    provenances: tuple[Provenance, ...]
    id: tuple[uuid.UUID, ...]
    doi: tuple[str | None, ...]
    journal: tuple[_JournalKey | None, ...]
    year: tuple[_Year | None, ...]
    classification_state: tuple[AiBlockState | None, ...]
    classification_provenance: tuple[Annotated[int, Field(ge=0, strict=True)] | None, ...]
    topics: tuple[tuple[_Key, ...] | None, ...]
    methods: tuple[tuple[_Key, ...] | None, ...]
    topics_verified: tuple[StrictBool | None, ...]
    methods_verified: tuple[StrictBool | None, ...]
    has_abstract: tuple[StrictBool, ...]
    has_full_content: tuple[StrictBool, ...]
    is_new_this_week: tuple[StrictBool, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        count = len(self.id)
        for name in _INDEX_COLUMNS:
            if len(getattr(self, name)) != count:
                raise ValueError(f"每一欄的長度要一樣（{name}）")
        if len(set(self.id)) != count:
            raise ValueError("論文 ID 不能重複")
        for position in self.classification_provenance:
            if position is not None and position >= len(self.provenances):
                raise ValueError("classification_provenance 超出對照表")
        return self


_INDEX_COLUMNS: Final = (
    "doi",
    "journal",
    "year",
    "classification_state",
    "classification_provenance",
    "topics",
    "methods",
    "topics_verified",
    "methods_verified",
    "has_abstract",
    "has_full_content",
    "is_new_this_week",
)


class DemoTitles(BaseModel):
    """`titles.json`：論文標題，列順序和 `papers-index.json` 相同（用標題裡的字篩選）。"""

    model_config = _WHITELIST

    snapshot: SnapshotRef
    export_id: ExportId
    titles: tuple[Annotated[str, StringConstraints(min_length=1)], ...]


class DemoTaxonomy(BaseModel):
    """`taxonomy.json`：期刊、主題、方法的代號與顯示名稱（和成員版同一個形狀）。"""

    model_config = _WHITELIST

    snapshot: SnapshotRef
    export_id: ExportId
    names: DisplayNames


# manifest 列的資料檔路徑：`<匯出編號>/` 加上認得的檔名，或 `<匯出編號>/papers/NNN.json`（不會有
# `..` 這種路徑段）。
DEMO_FILE_PATH_PATTERN: Final = (
    r"^[1-9][0-9]{0,8}-[1-9][0-9]{0,8}/"
    r"(?:taxonomy\.json|papers-index\.json|titles\.json|papers/[0-9]{3}\.json)$"
)


class DemoFileEntry(BaseModel):
    """manifest 列的一個資料檔：路徑（相對於 `preview/`，只能是認得的資料檔）、SHA-256、
    位元組數。"""

    model_config = _WHITELIST

    path: Annotated[str, StringConstraints(pattern=DEMO_FILE_PATH_PATTERN)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    size: int = Field(ge=0, strict=True)


class DemoSummary(BaseModel):
    """首頁一次請求就拿到的讀數（ADR-0007「檔案」）。年份範圍沒有論文時是 null。"""

    model_config = _WHITELIST

    paper_count: int = Field(ge=0, strict=True)
    with_abstract_count: int = Field(ge=0, strict=True)
    new_this_week_count: int = Field(ge=0, strict=True)
    full_content_count: int = Field(ge=0, strict=True)
    first_year: _Year | None
    last_year: _Year | None


class DemoClientConfig(BaseModel):
    """示範站前端要用的設定值（AD-18、AD-21：前端不另外寫一份）。

    - `timezone`：畫面上的日期換成哪個時區（IANA 名稱）。
    - `paper_list_page_size`：示範站論文清單一頁幾篇（Story 3.14，`DEMO_PAPER_LIST_PAGE_SIZE`）。

    成員版的 `ClientConfig` 不變（示範站沒有文獻庫，不帶它的篇數）。
    """

    model_config = _WHITELIST

    timezone: _Text
    paper_list_page_size: int = Field(ge=1, strict=True)


def build_demo_client_config() -> DemoClientConfig:
    """示範站的設定值：時區和成員版同一個（`domain/snapshot.DATA_TIMEZONE`）；清單一頁
    `DEMO_PAPER_LIST_PAGE_SIZE` 篇。"""
    return DemoClientConfig(
        timezone=DATA_TIMEZONE.key, paper_list_page_size=DEMO_PAPER_LIST_PAGE_SIZE
    )


class DemoManifest(BaseModel):
    """`manifest.json`：現在該讀哪一次匯出（最後才寫，只有它會被覆寫）。列的每個檔都在這次的匯出
    目錄底下（路徑開頭是自己的 `export_id`），不重複，而且是完整的一批：分類表、索引、標題與每一個
    分片（個數照 `summary.paper_count` 算）都列了。"""

    model_config = _WHITELIST

    snapshot: SnapshotRef
    export_id: ExportId
    exported_at: UtcDateTime
    files: tuple[DemoFileEntry, ...]
    summary: DemoSummary
    client_config: DemoClientConfig

    @model_validator(mode="after")
    def _check(self) -> Self:
        paths = [entry.path for entry in self.files]
        if any(not path.startswith(f"{self.export_id}/") for path in paths):
            raise ValueError("manifest 列的檔要在自己這次的匯出目錄底下")
        if len(set(paths)) != len(paths):
            raise ValueError("manifest 列的檔不能重複")
        # 一批完整的匯出（G7）：分類表、索引、標題都在，分片從 000 起連號、個數和篇數對得上
        # （每 SHARD_SIZE 篇一個；0 篇沒有分片）。
        shard_count = -(-self.summary.paper_count // SHARD_SIZE)
        expected = {
            data_file_path(self.export_id, name)
            for name in (TAXONOMY_FILE, PAPERS_INDEX_FILE, TITLES_FILE)
        } | {
            data_file_path(self.export_id, f"{PAPERS_DIR}/{shard_file_name(shard)}")
            for shard in range(min(shard_count, _MAX_SHARDS))
        }
        if set(paths) != expected:
            raise ValueError("manifest 列的檔不齊全，或和篇數對不上")
        return self


# 每一種資料檔對應的模型；順序也是 `demo-openapi.json` 的端點順序。
DEMO_FILE_MODELS: Final[tuple[tuple[str, type[BaseModel]], ...]] = (
    (MANIFEST_FILE, DemoManifest),
    (TAXONOMY_FILE, DemoTaxonomy),
    (PAPERS_INDEX_FILE, DemoPapersIndex),
    (TITLES_FILE, DemoTitles),
    (f"{PAPERS_DIR}/{{shard}}.json", DemoPaperShard),
)

_SHARD_PATH = re.compile(rf"^{PAPERS_DIR}/[0-9]{{{_SHARD_DIGITS}}}\.json$")


def model_for_path(path: str) -> type[BaseModel] | None:
    """相對於 `preview/` 的路徑 → 它的白名單模型；不是認得的資料檔回 None。"""
    if path == MANIFEST_FILE:
        return DemoManifest
    export_id, _, name = path.partition("/")
    if not _EXPORT_ID.fullmatch(export_id):
        return None
    if _SHARD_PATH.fullmatch(name):
        return DemoPaperShard
    for file_name, model in DEMO_FILE_MODELS[1:]:
        if name == file_name and "{" not in file_name:
            return model
    return None


# --- demo-openapi.json --------------------------------------------------------------------------

DEMO_OPENAPI_TITLE: Final = "Research Radar 示範站資料檔"
# 資料檔格式的版本：改了任何一個白名單模型的形狀就加一。
DEMO_FORMAT_VERSION: Final = "1"


def _operation_id(file_name: str) -> str:
    words = re.split(r"[^a-z0-9]+", file_name.removesuffix(".json").replace("{shard}", "shard"))
    return "getDemo" + "".join(word.capitalize() for word in words if word)


def build_demo_openapi() -> dict[str, Any]:
    """把示範站的每一種靜態檔描述成一個只能讀取的端點（ADR-0010 第 4 點），格式和成員版的
    `openapi.json` 相同，前端可以用同一個工具產生型別。輸出固定（同一份程式每次一樣）。"""
    _, top = models_json_schema(
        [(model, "serialization") for _, model in DEMO_FILE_MODELS],
        ref_template="#/components/schemas/{model}",
    )
    schemas = top.get("$defs", {})
    paths: dict[str, Any] = {}
    for file_name, model in DEMO_FILE_MODELS:
        parameters: list[dict[str, Any]] = []
        if file_name == MANIFEST_FILE:
            path = f"/{file_name}"
        else:
            path = f"/{{export_id}}/{file_name}"
            parameters.append(
                {
                    "name": "export_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "pattern": EXPORT_ID_PATTERN},
                }
            )
        if "{shard}" in file_name:
            parameters.append(
                {
                    "name": "shard",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "pattern": rf"^[0-9]{{{_SHARD_DIGITS}}}$"},
                }
            )
        operation: dict[str, Any] = {
            "operationId": _operation_id(file_name),
            "summary": (model.__doc__ or "").strip().splitlines()[0],
            "responses": {
                "200": {
                    "description": "Successful Response",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{model.__name__}"}
                        }
                    },
                }
            },
        }
        if parameters:
            operation["parameters"] = parameters
        paths[path] = {"get": operation}
    return {
        "openapi": "3.1.0",
        "info": {"title": DEMO_OPENAPI_TITLE, "version": DEMO_FORMAT_VERSION},
        "paths": paths,
        "components": {"schemas": dict(sorted(schemas.items()))},
    }


def render_demo_openapi() -> str:
    """`demo-openapi.json` 的內容：縮排 2、中文不跳脫、結尾換行（和 `openapi.json` 同一個寫法）。"""
    return json.dumps(build_demo_openapi(), ensure_ascii=False, indent=2) + "\n"


# --- 序列化 -------------------------------------------------------------------------------------


def dump_demo_file(model: BaseModel) -> bytes:
    """白名單模型 → 寫進檔案的 UTF-8 JSON（中文不跳脫、不縮排、鍵照模型的欄位順序）。"""
    return model.model_dump_json().encode("utf-8")


def index_columns(
    papers: Sequence[DemoPaperBiblio],
) -> tuple[tuple[Provenance, ...], dict[str, tuple[Any, ...]]]:
    """全部論文的第一層 → `papers-index.json` 的來歷對照表與各欄（列順序照 `papers`）。"""
    provenances: list[Provenance] = []
    position: dict[Provenance, int] = {}
    columns: dict[str, list[Any]] = {name: [] for name in ("id", *_INDEX_COLUMNS)}
    for paper in papers:
        block = paper.classification
        value = None if block is None else block.value
        provenance = None if block is None else block.provenance
        slot: int | None = None
        if provenance is not None:
            if provenance not in position:
                position[provenance] = len(provenances)
                provenances.append(provenance)
            slot = position[provenance]
        topic = None if value is None else value.topic
        method = None if value is None else value.method
        columns["id"].append(paper.biblio.id)
        columns["doi"].append(paper.biblio.doi)
        columns["journal"].append(paper.biblio.journal)
        columns["year"].append(paper.biblio.publication_year)
        columns["classification_state"].append(None if block is None else block.state)
        columns["classification_provenance"].append(slot)
        columns["topics"].append(None if topic is None else topic.keys)
        columns["methods"].append(None if method is None else method.keys)
        columns["topics_verified"].append(None if topic is None else topic.human_verified)
        columns["methods_verified"].append(None if method is None else method.human_verified)
        columns["has_abstract"].append(paper.has_abstract)
        columns["has_full_content"].append(paper.has_full_content)
        columns["is_new_this_week"].append(paper.is_new_this_week)
    return tuple(provenances), {name: tuple(values) for name, values in columns.items()}


# --- 自我檢查 -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ForbiddenText:
    """自我檢查要找的字串。`label` 印在失敗原因裡（不印字串本身：成員 email 不該進日誌）。"""

    label: str
    text: str


def _fold(text: str) -> str:
    """比對用的寫法：Unicode NFKC 正規化（全形與半形、組合字元與合成字元統一）後 casefold，再做
    一次 NFKC（casefold 之後可能又出現可以合成的組合，Unicode 的 NFKC_Casefold 做法）。"""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())


def _searchable_text(content: bytes) -> str:
    """檔案的原始文字，加上 JSON 解開後的每一個字串（鍵與值；跳脫過的寫法在這裡已經還原），每一段
    再多一份百分比編碼還原後的寫法（`%40` → `@`）；全部經 `_fold`。"""
    raw = content.decode("utf-8", errors="replace")
    parts = [raw]
    with contextlib.suppress(ValueError):  # 不是合法的 JSON：只找原始文字（模型那一項會報）
        parts.extend(_strings(json.loads(content)))
    parts.extend([unquote(part) for part in parts])
    return "\n".join(_fold(part) for part in parts)


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _model_field_names(models: Iterable[type[BaseModel]]) -> frozenset[str]:
    """這些模型（含巢狀的模型）的全部欄位名稱：失敗原因的位置只印這些已知的名稱。"""
    names: set[str] = set()
    seen: set[type[BaseModel]] = set()
    pending = list(models)
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        for name, info in model.model_fields.items():
            names.add(name)
            pending.extend(_nested_models(info.annotation))
    return frozenset(names)


def _nested_models(annotation: object) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for arg in get_args(annotation) for model in _nested_models(arg)]


def _safe_location(location: Sequence[object], known: frozenset[str]) -> str:
    """驗證錯誤的位置：陣列位置與已知的欄位名稱照印，其他（多出來的鍵名等）一律印成 `?`，免得被擋下
    的字串跟著鍵名進日誌（F12）。"""
    parts = [
        str(part) if isinstance(part, int) or (isinstance(part, str) and part in known) else "?"
        for part in location
    ]
    return ".".join(parts) or "?"


def _safe_error_type(value: object) -> str:
    text = str(value)
    return text if re.fullmatch(r"[a-z_]{1,64}", text) else "?"


def check_demo_files(
    files: Mapping[str, bytes],
    *,
    full_paper_ids: Iterable[uuid.UUID],
    forbidden: Sequence[ForbiddenText],
    excluded_paper_ids: Iterable[uuid.UUID] = (),
) -> list[str]:
    """寫 `manifest.json` 之前的自我檢查（ADR-0007「寫出之前的自我檢查」第 1、2、3 項與第 6 項的
    分片與索引部分）。回傳失敗原因（空的＝全部通過）；原因只寫檔名、已知的欄位名稱與陣列位置、錯誤
    代碼、解析得出的論文 ID，不印檔案內容、多出來的鍵名或看不懂的 ID（檔名由匯出程式自己組，照印）。

    1. 每個檔都是認得的資料檔，而且通過它的白名單模型（多一個欄位就失敗）。
    2. 不在 `full_paper_ids`（view `demo_full_paper`）的論文沒有完整內容：分片檔裡它的 `full` 是
       null、`has_full_content` 是假；`papers-index.json` 的 `has_full_content` 也是假。`full` 的
       `paper_id` 也要是這一篇自己的 ID。這一項直接看 JSON，不經過模型（模型壞掉時照樣檢查得到）。
    3. 每個檔的文字裡都找不到 `forbidden` 的任何一個（Unicode NFKC 正規化、不分大小寫；原始文字、
       JSON 解開後的每個字串、百分比編碼還原後的寫法都找）。
    6. `excluded_paper_ids`（出版社或期刊在「示範站不放完整內容」清單裡的論文，直接用出版社欄位查）
       沒有完整內容，就算它在 `full_paper_ids` 裡。
    """
    allowed = frozenset(full_paper_ids)
    excluded = frozenset(excluded_paper_ids)
    known = _model_field_names(model for _, model in DEMO_FILE_MODELS)
    needles = [(item.label, _fold(item.text)) for item in forbidden if item.text]
    problems: list[str] = []
    for path in sorted(files):
        content = files[path]
        model = model_for_path(path)
        if model is None:
            problems.append(f"{path}：不是認得的資料檔")
        else:
            try:
                model.model_validate_json(content)
            except ValidationError as exc:
                errors = exc.errors(include_input=False, include_url=False, include_context=False)
                first = errors[0] if errors else None
                where = "?" if first is None else _safe_location(first["loc"], known)
                kind = "?" if first is None else _safe_error_type(first["type"])
                problems.append(
                    f"{path}：不符合白名單模型 {model.__name__}"
                    f"（{len(errors)} 個錯誤，第一個在 {where}：{kind}）"
                )
        problems.extend(_full_content_problems(path, content, allowed, excluded))
        text = _searchable_text(content)
        problems.extend(f"{path}：出現{label}" for label, needle in needles if needle in text)
    return problems


def _full_content_problems(
    path: str, content: bytes, allowed: frozenset[uuid.UUID], excluded: frozenset[uuid.UUID]
) -> list[str]:
    model = model_for_path(path)
    if model not in (DemoPaperShard, DemoPapersIndex):
        return []
    try:
        data = json.loads(content)
    except ValueError:
        return [f"{path}：不是合法的 JSON"]
    problems: list[str] = []
    if model is DemoPaperShard:
        papers = data.get("papers") if isinstance(data, dict) else None
        for position, item in enumerate(papers if isinstance(papers, list) else []):
            paper_id = _paper_id_of(item)
            who = _who(paper_id, position)
            full = item.get("full") if isinstance(item, dict) else None
            if isinstance(full, dict) and _as_uuid(full.get("paper_id")) != paper_id:
                problems.append(f"{path}：{who} 的完整內容不是它自己的（paper_id 對不上）")
            if paper_id in allowed and paper_id not in excluded:
                continue
            biblio = item.get("biblio") if isinstance(item, dict) else None
            has_full = isinstance(biblio, dict) and biblio.get("has_full_content") is not False
            if not isinstance(item, dict) or item.get("full") is not None or has_full:
                problems.append(f"{path}：{_reason(paper_id, excluded, who)}帶了完整內容")
    else:
        ids = data.get("id") if isinstance(data, dict) else None
        flags = data.get("has_full_content") if isinstance(data, dict) else None
        if isinstance(ids, list) and isinstance(flags, list):
            for position, (raw_id, flag) in enumerate(zip(ids, flags, strict=False)):
                paper_id = _as_uuid(raw_id)
                if flag is False or (paper_id in allowed and paper_id not in excluded):
                    continue
                who = _who(paper_id, position)
                problems.append(f"{path}：{_reason(paper_id, excluded, who)}標成有完整內容")
    return problems


def _who(paper_id: uuid.UUID | None, position: int) -> str:
    """失敗原因裡指一篇論文：看得懂的 ID 照印；看不懂的只印第幾列（不印原字串）。"""
    return f"論文 {paper_id}" if paper_id is not None else f"第 {position} 列（ID 看不懂）的論文"


def _reason(paper_id: uuid.UUID | None, excluded: frozenset[uuid.UUID], who: str) -> str:
    if paper_id is not None and paper_id in excluded:
        return f"排除清單裡的出版社或期刊的{who} "
    return f"不在 demo_full_paper 的{who} "


def _paper_id_of(item: object) -> uuid.UUID | None:
    if not isinstance(item, dict):
        return None
    biblio = item.get("biblio")
    inner = biblio.get("biblio") if isinstance(biblio, dict) else None
    return _as_uuid(inner.get("id")) if isinstance(inner, dict) else None


def _as_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
