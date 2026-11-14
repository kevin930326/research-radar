"""Port（核心訂的介面，用 Protocol 寫）。

全專案只有四個：PaperSource、LlmProvider、EmbeddingProvider、ObjectStore（ADR-0001）。
各自由第一個用到的 Story 加進來；還沒有用到的不先建。參數與回傳只用 `radar.domain` 的型別。
"""

from typing import Protocol

from radar.domain.raw_records import JournalQuery, RawSource, SourcePage


class PaperSource(Protocol):
    """論文來源（ADR-0014）：只負責抓，回傳原始紀錄，不解析。

    真實的實作是 `radar.adapters.openalex.OpenAlexSource`；替身是
    `radar.adapters.fake.FakePaperSource`。
    """

    @property
    def name(self) -> RawSource:
        """來源名稱（例如 `openalex`）。"""
        ...

    def fetch_journal_page(self, query: JournalQuery, cursor: str | None) -> SourcePage:
        """抓這本期刊的一頁；`cursor=None` 是第一頁。

        重試在 adapter 裡做完；回傳的 `calls` 是這一頁每次 HTTP 嘗試的紀錄。

        Raises:
            QuotaReachedError: 重試途中對方回報額度已到停下的比例（`PaperSourceError` 的子類別，
                不算失敗）；`calls` 帶著每次嘗試的紀錄。
            PaperSourceError: 重試用完、不重試的錯誤或回應看不懂；`calls` 帶著每次嘗試的紀錄。
        """
        ...


class ObjectStore(Protocol):
    """物件儲存（R2／RustFS 這類 S3 相容的服務）。

    真實的實作是 `radar.adapters.object_store.S3ObjectStore`；替身是
    `radar.adapters.fake.FakeObjectStore`。
    """

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        """寫入（同一個鍵已經有東西就覆寫）。"""
        ...

    def get(self, key: str) -> bytes:
        """讀出。

        Raises:
            ObjectNotFoundError: 沒有這個鍵。
        """
        ...
