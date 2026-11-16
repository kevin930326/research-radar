"""論文頁的層間資料（Story 3.6、ADR-0048）：repo 讀出來的列（`PaperPageFacts`）與 services 組好的
結果（`PaperPage`）。API 回應的形狀在 `domain/contracts.py`，這裡不管 JSON。

- `PaperPageFacts`：`repo/papers.find_paper_page_facts` 用一句帶可見條件的查詢讀出的論文欄位、
  最早記上的 DOI 與 OpenAlex ID、目前的摘要（`is_current` 那一筆）。
- `PaperPage`：上面那一列，加上期刊代號（由 `JournalsConfig` 以 ISSN-L 對回）、三個 AI 區塊
  （`repo/ai_blocks.find_ai_block` 判定）與目前已發佈的快照（沒有是 None）。
"""

import uuid
from dataclasses import dataclass

from radar.domain.ai_blocks import AiBlock, AiProduct
from radar.domain.ai_outputs import CurrentAbstract
from radar.domain.domain_config import JournalsConfig
from radar.domain.papers import PaperOrigin
from radar.domain.snapshot import Snapshot, SnapshotStatus


@dataclass(frozen=True, slots=True)
class PaperPageFacts:
    """論文頁要的論文欄位（`repo/papers.find_paper_page_facts` 回的列）。

    - `doi`、`openalex_id`：正規化後的值；一篇有好幾個時是最早記上的那一個（`created_at, id`），
      沒有是 None。
    - `abstract`：目前的摘要；沒有是 None。

    Raises:
        ValueError: `authors` 不是字串的 tuple、被引用次數是負的。
    """

    paper_id: uuid.UUID
    origin: PaperOrigin
    title: str
    authors: tuple[str, ...]
    publication_year: int | None
    journal_issn_l: str | None
    volume: str | None
    issue: str | None
    first_page: str | None
    last_page: str | None
    cited_by_count: int | None
    in_scope: bool
    is_retracted: bool
    doi: str | None
    openalex_id: str | None
    abstract: CurrentAbstract | None

    def __post_init__(self) -> None:
        if not isinstance(self.authors, tuple) or not all(
            isinstance(author, str) for author in self.authors
        ):
            raise ValueError("authors 要是字串的 tuple")
        if self.cited_by_count is not None and self.cited_by_count < 0:
            raise ValueError("cited_by_count 不能是負的")


@dataclass(frozen=True, slots=True)
class PaperPage:
    """論文頁的資料（`services/paper_page.get_paper_page` 的結果）。

    Raises:
        ValueError: 三個區塊的產出種類放錯欄位；快照不是已發佈的。
    """

    facts: PaperPageFacts
    journal_key: str | None
    zh_summary: AiBlock
    classification: AiBlock
    research_model: AiBlock
    snapshot: Snapshot | None

    def __post_init__(self) -> None:
        for name, product in (
            ("zh_summary", AiProduct.ZH_SUMMARY),
            ("classification", AiProduct.CLASSIFICATION),
            ("research_model", AiProduct.RESEARCH_MODEL),
        ):
            block = getattr(self, name)
            if not isinstance(block, AiBlock) or block.product is not product:
                raise ValueError(f"{name} 要是 {product.value} 的 AiBlock")
        if self.snapshot is not None and (
            self.snapshot.status is not SnapshotStatus.PUBLISHED or self.snapshot.as_of is None
        ):
            raise ValueError("snapshot 要是已發佈的快照")


def journal_key_for(journals: JournalsConfig, issn_l: str | None) -> str | None:
    """ISSN-L → 設定檔的期刊代號；不是追蹤的期刊（或沒有 ISSN-L）回 None。"""
    if issn_l is None:
        return None
    for journal in journals.journals:
        if journal.issn_l == issn_l:
            return journal.key
    return None
