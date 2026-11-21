"""論文的領域型別：解析後的論文、讀出來的論文、寫入的結果（AD-6、AD-25、ADR-0020）。

`ParsedPaper` 是寫進論文資料的唯一形狀：`repo.papers.upsert_by_external_id` 只收它。
Story 1.5 定了識別碼、標題、期刊、出版年；Story 1.7 加了作者、卷期頁碼、類型、撤稿標記、
摘要與來源、引用清單、被引用次數、關鍵字，以及指紋 `source_hash`（AD-25，算法在
`domain/fingerprints.py`）；Story 3.7a 加了授權與出版社（ADR-0052，不算進指紋）。新欄位都有預設值。
"""

import dataclasses
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from radar.domain.fingerprints import source_hash as compute_source_hash
from radar.domain.identifiers import (
    ExternalId,
    IdentifierScheme,
    normalize_doi,
    normalize_issn_l,
    normalize_openalex_id,
)


def check_storable_text(value: str, field: str) -> None:
    """確認 PostgreSQL 的 text 存得下 `value`：不含 NUL 字元、能編成 UTF-8。

    存不下的字串（例如單獨的代理字元 `\\ud800`）要到寫入時才由 psycopg 丟錯，會讓整批交易
    失敗，所以在驗證時就擋。識別碼欄位不必另外檢查：正規化函式只收得下可印出的字元。

    Raises:
        ValueError: 含 NUL 字元，或無法編成 UTF-8。
    """
    if "\x00" in value:
        raise ValueError(f"「{field}」不能含 NUL 字元")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"「{field}」含無法編成 UTF-8 的字元（例如單獨的代理字元）") from None


class PaperOrigin(StrEnum):
    """論文怎麼進資料庫的（`paper.origin`）。只在新增時寫，之後不改。"""

    TRACKED = "tracked"  # 追蹤期刊的匯入與每週更新
    EXPANSION = "expansion"  # 引用擴展
    IMPORT = "import"  # 成員匯入（上傳、推薦清單）
    MANUAL = "manual"  # 沒有識別碼的手填書目，只能經之後的 `create_manual`


class AbstractSource(StrEnum):
    """摘要的來源（`paper_abstract.source` 的 CHECK 列的就是這三個值）。"""

    OPENALEX = "openalex"
    SEMANTIC_SCHOLAR = "semantic_scholar"  # 要等書面授權才寫入
    UPLOADED_PDF = "uploaded_pdf"  # 只有上傳者看得到，永遠不是「目前的摘要」（ADR-0015）


# 同一篇論文有好幾個來源的摘要時，誰是「目前的摘要」（`is_current`）：排前面的優先。
# `uploaded_pdf` 不在裡面，永遠不是目前的。架構文件寫「依設定檔的來源優先順序」，但現在只有
# `openalex` 會寫入，等第二個來源真的要寫入時再搬進設定檔（ADR-0022）。
ABSTRACT_SOURCE_PRIORITY: Final[tuple[AbstractSource, ...]] = (
    AbstractSource.OPENALEX,
    AbstractSource.SEMANTIC_SCHOLAR,
)


class KeywordSource(StrEnum):
    """關鍵字的來源代號（ADR-0016）。全專案只有這一份清單：`paper_keyword.source` 的 CHECK、
    `config/keyword_sources.yaml` 的 `active` 都對它（測試守著）。
    """

    OPENALEX = "openalex"


# 關鍵字在來源裡的代號：小寫英文與數字，中間可以用單一連字號接起來（例如 `generative-ai`）。
KEYWORD_KEY_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
KEYWORD_KEY_MAX_LENGTH = 200
# 文章類型（OpenAlex 的 `type`，例如 `article`、`paratext`）。
WORK_TYPE_PATTERN = r"^[a-z][a-z0-9-]*$"
# PostgreSQL integer 的上限。
MAX_INT4 = 2_147_483_647


def _clean_text(value: str, field: str) -> str:
    """去頭尾空白後不可是空字串、PostgreSQL 的 text 存得下；回傳去掉空白的值。"""
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"「{field}」去掉空白後不能是空的")
    check_storable_text(stripped, field)
    return stripped


class ParsedKeyword(BaseModel):
    """一個關鍵字：`key` 是它在來源裡的代號，`display_name` 是顯示名稱。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=KEYWORD_KEY_PATTERN, max_length=KEYWORD_KEY_MAX_LENGTH)
    display_name: str

    @field_validator("display_name")
    @classmethod
    def _clean_display_name(cls, value: str) -> str:
        return _clean_text(value, "關鍵字名稱")


class ParsedPaper(BaseModel):
    """解析後的一篇論文。識別碼欄位在建構時就正規化；`doi` 與 `openalex_id` 至少一個。

    來自外部服務（或 LLM）的資料一律經過這裡驗證，看不懂的識別碼直接擋下。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["openalex"]
    # 這份資料從來源抓下來的時間，必須帶時區；「舊的不蓋掉新的」比的就是它。
    fetched_at: AwareDatetime
    doi: str | None
    openalex_id: str | None
    title: str
    journal_issn_l: str | None
    # 四位數的年份；strict：不收 True、"2020" 這類會被轉型的值（也擋下超出 int4 的數字）。
    publication_year: Annotated[int, Field(strict=True, ge=1000, le=9999)] | None
    # 以下由 Story 1.7 加，都有預設值。字串去頭尾空白後不可是空字串（沒有就是 None）。
    authors: tuple[str, ...] = ()
    volume: str | None = None
    issue: str | None = None
    first_page: str | None = None
    last_page: str | None = None
    work_type: Annotated[str, Field(pattern=WORK_TYPE_PATTERN, max_length=64)] | None = None
    is_retracted: Annotated[bool, Field(strict=True)] = False
    # 「沒有摘要」＝None；空字串或只有空白會被擋下。和 `abstract_source` 要嘛都有、要嘛都沒有。
    abstract: str | None = None
    abstract_source: AbstractSource | None = None
    # 這篇引用的論文（正規化的 OpenAlex ID，`W` 開頭），去重複、保持順序。
    referenced_works: tuple[str, ...] = ()
    cited_by_count: Annotated[int, Field(strict=True, ge=0, le=MAX_INT4)] | None = None
    # 同一個 `key` 只能出現一次。
    keywords: tuple[ParsedKeyword, ...] = ()
    # 以下由 Story 3.7a 加（ADR-0052），都有預設值，都不算進 `source_hash`：
    # 出版社自己那一版（OpenAlex `primary_location`）的授權原字串（看不懂的也照存；沒有＝None），
    # 以及那一版的出版社 ID（正規化的 OpenAlex ID，看不懂的照原字串）與名稱，各自含 lineage、
    # 去重複、保持順序（直屬的出版社在最前面）。兩個都是空的＝沒有出版社欄位。
    primary_license: str | None = None
    publisher_ids: tuple[str, ...] = ()
    publisher_names: tuple[str, ...] = ()

    @field_validator("doi")
    @classmethod
    def _normalize_doi(cls, value: str | None) -> str | None:
        return None if value is None else normalize_doi(value)

    @field_validator("openalex_id")
    @classmethod
    def _normalize_openalex_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_openalex_id(value)
        if not normalized.startswith("W"):
            raise ValueError("論文的 OpenAlex ID 要是 W 開頭")
        return normalized

    @field_validator("journal_issn_l")
    @classmethod
    def _normalize_issn_l(cls, value: str | None) -> str | None:
        return None if value is None else normalize_issn_l(value)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("標題去掉空白後不能是空的")
        check_storable_text(stripped, "標題")
        return stripped

    @field_validator("authors")
    @classmethod
    def _clean_authors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_clean_text(author, "作者") for author in value)

    @field_validator("volume", "issue", "first_page", "last_page", "abstract", "primary_license")
    @classmethod
    def _clean_optional_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        return None if value is None else _clean_text(value, str(info.field_name))

    @field_validator("publisher_ids", "publisher_names")
    @classmethod
    def _clean_publishers(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        cleaned: dict[str, None] = {}
        for item in value:
            cleaned.setdefault(_clean_text(item, str(info.field_name)), None)
        return tuple(cleaned)

    @field_validator("referenced_works")
    @classmethod
    def _normalize_referenced_works(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: dict[str, None] = {}
        for work in value:
            work_id = normalize_openalex_id(work)
            if not work_id.startswith("W"):
                raise ValueError("引用的論文 ID 要是 W 開頭")
            normalized.setdefault(work_id, None)
        return tuple(normalized)

    @field_validator("keywords")
    @classmethod
    def _unique_keyword_keys(cls, value: tuple[ParsedKeyword, ...]) -> tuple[ParsedKeyword, ...]:
        keys = [keyword.key for keyword in value]
        if len(set(keys)) != len(keys):
            raise ValueError("同一個關鍵字 key 只能出現一次")
        return value

    @model_validator(mode="after")
    def _require_identifier(self) -> Self:
        if self.doi is None and self.openalex_id is None:
            raise ValueError("doi 與 openalex_id 至少要有一個")
        return self

    @model_validator(mode="after")
    def _abstract_and_source_together(self) -> Self:
        if (self.abstract is None) != (self.abstract_source is None):
            raise ValueError("abstract 與 abstract_source 要嘛都有、要嘛都沒有")
        return self

    @property
    def source_hash(self) -> str:
        """這份資料的指紋（AD-25）：只由摘要、撤稿標記與書目欄位決定（`domain/fingerprints.py`）。"""
        return compute_source_hash(self)

    def external_ids(self) -> tuple[ExternalId, ...]:
        """這篇論文的識別碼，依比對的優先序排列：先 DOI、再 OpenAlex ID。"""
        ids: list[ExternalId] = []
        if self.doi is not None:
            ids.append(ExternalId(IdentifierScheme.DOI, self.doi))
        if self.openalex_id is not None:
            ids.append(ExternalId(IdentifierScheme.OPENALEX, self.openalex_id))
        return tuple(ids)


@dataclass(frozen=True)
class StoredPaper:
    """從資料庫讀出來的一篇論文（不含可見範圍的欄位）。

    `openalex_fetched_at` 是目前這份 OpenAlex 資料的抓取時間；None＝沒有 OpenAlex 的資料。
    """

    id: uuid.UUID
    origin: PaperOrigin
    title: str
    publication_year: int | None
    journal_issn_l: str | None
    in_scope: bool
    excluded_reason: str | None
    openalex_fetched_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # 以下由 Story 1.7 加。
    authors: tuple[str, ...]
    volume: str | None
    issue: str | None
    first_page: str | None
    last_page: str | None
    work_type: str | None
    is_retracted: bool
    referenced_works: tuple[str, ...]
    cited_by_count: int | None
    source_hash: str | None
    # 由 Story 1.11 加：寫入時 `source_hash` 變了的最後時間；None＝寫入以來指紋沒變過（ADR-0028）。
    source_changed_at: datetime | None = None


@dataclass(frozen=True)
class StoredDemoFacts:
    """資料庫裡存下的、`is_demo_eligible` 要看的欄位（reparse 結束時重判用，ADR-0052 第 9 點）。

    `primary_license`、`publisher_ids`、`publisher_names` 是寫入時照存的值（遷移 0021）。
    """

    paper_id: uuid.UUID
    primary_license: str | None
    publisher_ids: tuple[str, ...]
    publisher_names: tuple[str, ...]


class UpsertOutcome(StrEnum):
    """`upsert_by_external_id` 做了什麼。"""

    CREATED = "created"  # 新增了一篇論文
    UPDATED = "updated"  # 更新了既有的論文（要寫的資料一樣新或比較新）
    STALE = "stale"  # 要寫的資料比資料庫裡的舊，什麼都沒寫
    # DOI 與 OpenAlex ID 屬於兩篇不同的既有論文：兩篇都不寫、識別碼不搬（ADR-0020 更新 2026-11-22）
    IDENTIFIERS_SPLIT = "identifiers_split"


@dataclass(frozen=True)
class SplitIdentifiers:
    """「識別碼分屬兩篇」時的兩個識別碼與各自所屬的論文（ADR-0020 更新 2026-11-22）。"""

    doi: ExternalId
    doi_paper_id: uuid.UUID
    openalex_id: ExternalId
    openalex_paper_id: uuid.UUID


@dataclass(frozen=True)
class UpsertResult:
    """寫入的結果。`conflicts`：已經屬於另一篇論文、所以沒有掛到這篇的識別碼（不搬、不合併）。

    `source_changed`：更新既有論文時 `source_hash` 變了（含原本是 NULL；ADR-0028）；CREATED、
    STALE 一律 False。不參與相等比較：兩個結果相等仍只看（論文, 結果, 衝突, 分屬兩篇）。

    `split`：只有 `IDENTIFIERS_SPLIT` 時有值，帶兩個識別碼與各自所屬的論文；這時 `paper_id` 是
    DOI 所屬的那篇（照優先序本來會寫的那篇），但這筆資料沒有寫進任何論文，呼叫端當作略過。
    """

    paper_id: uuid.UUID
    outcome: UpsertOutcome
    conflicts: tuple[ExternalId, ...] = ()
    source_changed: bool = dataclasses.field(default=False, compare=False)
    split: SplitIdentifiers | None = None
