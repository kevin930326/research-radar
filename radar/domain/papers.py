"""論文的領域型別：解析後的論文、讀出來的論文、寫入的結果（AD-6、AD-25、ADR-0020）。

`ParsedPaper` 是寫進論文資料的唯一形狀：`repo.papers.upsert_by_external_id` 只收它。
這裡只有 Story 1.5 需要的欄位；作者、卷期頁碼、摘要、關鍵字等由 Story 1.7 加。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from radar.domain.identifiers import (
    ExternalId,
    IdentifierScheme,
    normalize_doi,
    normalize_issn_l,
    normalize_openalex_id,
)


class PaperOrigin(StrEnum):
    """論文怎麼進資料庫的（`paper.origin`）。只在新增時寫，之後不改。"""

    TRACKED = "tracked"  # 追蹤期刊的匯入與每週更新
    EXPANSION = "expansion"  # 引用擴展
    IMPORT = "import"  # 成員匯入（上傳、推薦清單）
    MANUAL = "manual"  # 沒有識別碼的手填書目，只能經之後的 `create_manual`


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
        if "\x00" in stripped:
            # PostgreSQL 的 text 不能存 NUL，寫入時會讓整批交易失敗。
            raise ValueError("標題不能含 NUL 字元")
        return stripped

    @model_validator(mode="after")
    def _require_identifier(self) -> Self:
        if self.doi is None and self.openalex_id is None:
            raise ValueError("doi 與 openalex_id 至少要有一個")
        return self

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


class UpsertOutcome(StrEnum):
    """`upsert_by_external_id` 做了什麼。"""

    CREATED = "created"  # 新增了一篇論文
    UPDATED = "updated"  # 更新了既有的論文（要寫的資料一樣新或比較新）
    STALE = "stale"  # 要寫的資料比資料庫裡的舊，什麼都沒寫


@dataclass(frozen=True)
class UpsertResult:
    """寫入的結果。`conflicts`：已經屬於另一篇論文、所以沒有掛到這篇的識別碼（不搬、不合併）。"""

    paper_id: uuid.UUID
    outcome: UpsertOutcome
    conflicts: tuple[ExternalId, ...] = ()
