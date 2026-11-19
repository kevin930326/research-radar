"""論文列（清單上的一篇論文）的層間資料（Story 3.16；3.13 的論文清單也用同一份）。

`repo/paper_rows.find_paper_rows` 讀出來的欄位（`PaperRowFacts`）；API 回應的形狀是
`domain/contracts.PaperRow`，這裡不管 JSON。

- `authors` 只有前 `PAPER_ROW_AUTHOR_LIMIT` 位，`author_count` 是全部的人數。
- `doi` 是正規化後的寫法；一篇有好幾個時是最早記上的那一個（`external_id.created_at, id`），和論文頁
  一樣（ADR-0048 第 3 點）。
- `has_abstract`：有沒有目前的摘要（`paper_abstract.is_current` 那一筆）。
"""

import uuid
from dataclasses import dataclass
from typing import Final

# 論文列只列前幾位作者（其餘以人數表示）。
PAPER_ROW_AUTHOR_LIMIT: Final = 3


@dataclass(frozen=True, slots=True)
class PaperRowFacts:
    """論文列要的論文欄位（`repo/paper_rows.find_paper_rows` 回的一列）。

    Raises:
        ValueError: `authors` 不是字串的 tuple、超過 `PAPER_ROW_AUTHOR_LIMIT` 位、比 `author_count`
            多，或 `author_count` 是負的。
    """

    paper_id: uuid.UUID
    title: str
    authors: tuple[str, ...]
    author_count: int
    publication_year: int | None
    journal_issn_l: str | None
    doi: str | None
    has_abstract: bool
    in_scope: bool
    is_retracted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.authors, tuple) or not all(
            isinstance(author, str) for author in self.authors
        ):
            raise ValueError("authors 要是字串的 tuple")
        if len(self.authors) > PAPER_ROW_AUTHOR_LIMIT:
            raise ValueError(f"authors 最多 {PAPER_ROW_AUTHOR_LIMIT} 位")
        if type(self.author_count) is not int or self.author_count < len(self.authors):
            raise ValueError("author_count 要是整數，而且不少於 authors 的人數")
