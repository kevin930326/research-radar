"""`PaperSource` 的替身：每本期刊一串頁，cursor 是自己的字串，不連網路。

- `pages`：ISSN-L → 每一頁的原始紀錄（照給的樣子回傳，`fetched_at` 由測試決定）。
- 每次請求記在 `requests`；每頁（含失敗的那次）回一筆 `ApiCall`。
- `fail_once(issn_l, page_number)`：那本期刊第 N 頁（從 1 起算）的下一次請求丟一次
  `PaperSourceError`（帶那一筆 `ApiCall`），之後照常回傳；用來模擬中斷後續跑。
- `point_next_cursor(issn_l, page_number, target_page)`：那本期刊第 N 頁的 `next_cursor` 改指向
  第 `target_page` 頁（都從 1 起算；第 1 頁沒有自己的 cursor，所以目標至少是 2）；用來模擬
  對方的 cursor 繞圈（例如 * → A → B → A）。
- 沒列的期刊回一頁空結果。
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from radar.domain.raw_records import (
    ApiCall,
    JournalQuery,
    PaperSourceError,
    RawRecord,
    RawSource,
    SourcePage,
)

CURSOR_PREFIX = "fake-cursor"


@dataclass(frozen=True)
class FakeRequest:
    query: JournalQuery
    cursor: str | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FakePaperSource:
    name = RawSource.OPENALEX

    def __init__(
        self,
        pages: Mapping[str, Sequence[Sequence[RawRecord]]] | None = None,
        *,
        cost_usd: Decimal = Decimal("0.0001"),
        remaining_usd: Decimal | None = None,
        limit_usd: Decimal | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.pages = {
            issn_l: [tuple(page) for page in book] for issn_l, book in (pages or {}).items()
        }
        self.requests: list[FakeRequest] = []
        self.cost_usd = cost_usd
        self.remaining_usd = remaining_usd
        self.limit_usd = limit_usd
        self._clock = clock
        self._failures: set[tuple[str, int]] = set()
        self._next_page_overrides: dict[tuple[str, int], int] = {}

    def fail_once(self, issn_l: str, page_number: int) -> None:
        """那本期刊第 `page_number` 頁（從 1 起算）的下一次請求丟一次 `PaperSourceError`。"""
        if page_number < 1:
            raise ValueError("page_number 從 1 起算")
        self._failures.add((issn_l, page_number))

    def point_next_cursor(self, issn_l: str, page_number: int, target_page: int) -> None:
        """那本期刊第 `page_number` 頁的 `next_cursor` 改指向第 `target_page` 頁（從 1 起算）。"""
        if page_number < 1:
            raise ValueError("page_number 從 1 起算")
        if target_page < 2:
            raise ValueError("target_page 至少是 2（第 1 頁沒有自己的 cursor）")
        self._next_page_overrides[(issn_l, page_number)] = target_page

    def fetch_journal_page(self, query: JournalQuery, cursor: str | None) -> SourcePage:
        self.requests.append(FakeRequest(query, cursor))
        index = self._page_index(query.issn_l, cursor)
        call = ApiCall(
            source=RawSource.OPENALEX,
            operation="list_works",
            http_status=200,
            cost_usd=self.cost_usd,
            remaining_usd=self.remaining_usd,
            limit_usd=self.limit_usd,
            called_at=self._clock(),
        )
        failure = (query.issn_l, index + 1)
        if failure in self._failures:
            self._failures.discard(failure)
            failed = ApiCall(
                source=call.source,
                operation=call.operation,
                http_status=503,
                cost_usd=call.cost_usd,
                remaining_usd=call.remaining_usd,
                limit_usd=call.limit_usd,
                called_at=call.called_at,
            )
            raise PaperSourceError(f"測試：第 {index + 1} 頁失敗", calls=(failed,))

        book = self.pages.get(query.issn_l, [])
        records = book[index] if index < len(book) else ()
        target = self._next_page_overrides.get((query.issn_l, index + 1))
        if target is not None:
            next_cursor: str | None = _cursor(query.issn_l, target - 1)
        elif index + 1 < len(book):
            next_cursor = _cursor(query.issn_l, index + 1)
        else:
            next_cursor = None
        return SourcePage(records, next_cursor, (call,))

    @staticmethod
    def _page_index(issn_l: str, cursor: str | None) -> int:
        if cursor is None:
            return 0
        prefix, _, rest = cursor.partition(":")
        cursor_issn, _, number = rest.rpartition(":")
        if prefix != CURSOR_PREFIX or cursor_issn != issn_l or not number.isdigit():
            raise ValueError(f"不是這本期刊的 cursor：{cursor!r}")
        return int(number)


def _cursor(issn_l: str, index: int) -> str:
    """第 `index + 1` 頁（`index` 從 0 起算）的 cursor。"""
    return f"{CURSOR_PREFIX}:{issn_l}:{index}"
