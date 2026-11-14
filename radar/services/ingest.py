"""抓取：依一本期刊逐頁抓、原始資料先存（FR-2、FR-10、AD-25、AD-29、ADR-0014、ADR-0021）。

這裡只做「抓 → 存原始」兩步；解析與暫存由呼叫端在每頁存好之後的 `on_page` 做
（`services/weekly_update.py`，Story 1.8）。抓取失敗時丟 `JournalFetchError`，帶著續跑要送的
cursor。
任何外部呼叫（source、object store）時都沒有開著的交易：每一步各用自己的短交易。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time

import structlog

from radar.domain.domain_config import Journal, OpenAlexQuota
from radar.domain.permissions import Action, require
from radar.domain.raw_records import (
    ApiCall,
    ApiUsage,
    JournalQuery,
    PaperSourceError,
    QuotaReachedError,
    RawRecord,
    compress_raw,
)
from radar.domain.storage_keys import RAW_CONTENT_TYPE, raw_object_key
from radar.domain.viewer import SystemViewer, Viewer
from radar.ports import ObjectStore, PaperSource
from radar.repo.api_calls import create_api_calls, get_api_usage_since
from radar.repo.db import Database
from radar.repo.raw_fetches import create_raw_fetches, find_stored_sha256s

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class FetchedPage:
    """存好的一頁（交給 `on_page`）：送出的 cursor（None＝第一頁）、這一頁的原始紀錄、
    下一頁的 cursor（None＝這本抓完）。
    """

    sent_cursor: str | None
    records: tuple[RawRecord, ...]
    next_cursor: str | None


class JournalFetchError(PaperSourceError):
    """一本期刊抓到一半失敗（ADR-0025、D12(a)）。

    - `journal_key`：哪一本。
    - `resume_cursor`：續跑時要送的 cursor（None＝第一頁）。source 失敗時是那一頁送出的 cursor；
      cursor 繞圈時是產生壞 `next_cursor` 的那一頁送出的 cursor（那一頁的原始資料已經存好，
      但沒有交給 `on_page`）。
    - `calls`：原本的 `calls`（`fetch_journal` 已經寫進 `api_call`，不要再寫一次）。
    訊息只有原本的原因，不含 cursor 與 key。
    """

    def __init__(
        self,
        reason: str,
        *,
        journal_key: str,
        resume_cursor: str | None,
        calls: Sequence[ApiCall] = (),
    ) -> None:
        super().__init__(reason, calls=calls)
        self.journal_key = journal_key
        self.resume_cursor = resume_cursor


@dataclass(frozen=True)
class JournalFetchResult:
    """一本期刊這一輪抓了什麼。

    - `stored_objects`：新存進物件儲存的物件數；`reused_objects`：內容和以前一樣、沿用的物件數
      （同一頁裡內容重複的只算一次）。
    - `next_cursor`：下一次要送的 cursor。`stopped_for_quota=True` 時是停下那一頁的 cursor
      （None＝從第一頁開始，例如一進來就被額度擋下）；否則 None 代表這本抓完。
    - `stopped_for_quota`：今天的用量到了設定的比例而停下（不算失敗）。
    判斷抓完要看 `next_cursor is None and not stopped_for_quota`。
    """

    journal_key: str
    pages: int
    records: int
    stored_objects: int
    reused_objects: int
    next_cursor: str | None
    stopped_for_quota: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def start_of_utc_day(moment: datetime) -> datetime:
    """`moment` 那一天（UTC）的零點；OpenAlex 的每日額度在 UTC 午夜重置。"""
    return datetime.combine(moment.astimezone(UTC).date(), time.min, tzinfo=UTC)


def fetch_journal(
    actor: Viewer,
    database: Database,
    source: PaperSource,
    store: ObjectStore,
    journal: Journal,
    *,
    start_year: int,
    quota: OpenAlexQuota,
    cursor: str | None = None,
    on_page: Callable[[FetchedPage], None] | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> JournalFetchResult:
    """從 `cursor`（None＝第一頁）起，把這本期刊一頁一頁抓完。每一頁：

    1. 短交易讀今天（UTC）的用量＝max(今天 `cost_usd` 的總和, 最新一筆回報的「上限 − 剩餘」
       ＋那一筆之後的 `cost_usd`)；到 `daily_budget_usd × stop_at_ratio` 就寫警告、回傳
       `stopped_for_quota=True` 與當下的 cursor，不丟例外、不再呼叫。
    2. 呼叫 source。
    3. 不論成功或 `PaperSourceError`，這次的 `calls` 都用自己的短交易立刻寫進 `api_call`
       （失敗時寫完再原樣往上丟）。source 在重試途中發現額度已到（`QuotaReachedError`）時，
       寫完 `calls` 後和第 1 步一樣寫警告、回傳 `stopped_for_quota=True` 與這一頁的 cursor。
    4. 短交易查出哪些內容（sha256）已經存過；其餘的（同頁重複只算一次）壓縮後 put 進物件儲存。
    5. 一個交易寫這一頁的 `raw_fetch` 列（同一個來源、外部鍵、抓取時間已經有了就略過）。
    6. 下一頁的 cursor 是這一輪已經送過的（例如 A → B → A 繞圈）就丟 `JournalFetchError`，
       不然費用為零又沒有額度回報時會一直繞下去。
    7. 有 `on_page` 就交給它這一頁（`FetchedPage`；空的最後一頁也交）；它丟的例外原樣往上丟。

    先 put 再寫 `raw_fetch`：中途當掉時下次會再 put 一次同樣的位元組（無害），不會有
    「有列沒物件」。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫與 source）。
        JournalFetchError: source 抓取失敗（`api_call` 已經寫好），或 cursor 繞回這一輪送過的；
            `resume_cursor` 是續跑時要送的 cursor。
        Exception: 物件儲存（`ObjectStoreUnavailableError`、`ClientError`）、資料庫與 `on_page`
            的錯誤不包成 `JournalFetchError`，原樣往上丟。
    """
    require(actor, Action.INGEST_PAPERS, None)
    viewer = SystemViewer()
    query = JournalQuery(issn_l=journal.issn_l, start_year=start_year)
    pages = records = stored = reused = 0
    stopped_for_quota = False
    sent_cursors: set[str | None] = {cursor}

    while True:
        usage = _read_usage(database, viewer, source, clock)
        if usage.used_usd >= quota.stop_at_usd:
            _warn_quota_stop(source, journal, quota, usage)
            stopped_for_quota = True
            break

        try:
            page = source.fetch_journal_page(query, cursor)
        except QuotaReachedError as exc:
            # 對方在重試途中就說額度到了：照額度停下處理，不算失敗（ADR-0021）。
            _record_calls(database, exc.calls)
            _warn_quota_stop(source, journal, quota, _read_usage(database, viewer, source, clock))
            stopped_for_quota = True
            break
        except PaperSourceError as exc:
            _record_calls(database, exc.calls)
            # 續跑時重送這一頁（訊息只有原本的原因，不帶 cursor）。
            raise JournalFetchError(
                exc.reason, journal_key=journal.key, resume_cursor=cursor, calls=exc.calls
            ) from exc
        _record_calls(database, page.calls)

        new_objects, existing_objects = _store_raw(database, viewer, source, store, page.records)
        if page.records:
            with database.transaction() as connection:
                create_raw_fetches(connection, page.records)

        if page.next_cursor is not None and page.next_cursor in sent_cursors:
            # 訊息不帶 cursor 的值：固定句子就夠，也不會意外帶出任何秘密。這一頁不交給
            # `on_page`、不前進：續跑時從產生壞 cursor 的這一頁重送。
            raise JournalFetchError(
                "下一頁的 cursor 繞回這一輪已經抓過的頁",
                journal_key=journal.key,
                resume_cursor=cursor,
            )
        if on_page is not None:
            on_page(FetchedPage(cursor, page.records, page.next_cursor))

        pages += 1
        records += len(page.records)
        stored += new_objects
        reused += existing_objects
        cursor = page.next_cursor
        if cursor is None:
            break
        sent_cursors.add(cursor)

    result = JournalFetchResult(
        journal_key=journal.key,
        pages=pages,
        records=records,
        stored_objects=stored,
        reused_objects=reused,
        next_cursor=cursor,
        stopped_for_quota=stopped_for_quota,
    )
    log.info(
        "期刊抓取結束",
        source=source.name.value,
        journal=journal.key,
        pages=pages,
        records=records,
        stored_objects=stored,
        reused_objects=reused,
        finished=cursor is None and not stopped_for_quota,
        stopped_for_quota=stopped_for_quota,
    )
    return result


def _read_usage(
    database: Database, viewer: SystemViewer, source: PaperSource, clock: Callable[[], datetime]
) -> ApiUsage:
    """短交易讀今天（UTC）這個來源的用量。"""
    with database.transaction() as connection:
        return get_api_usage_since(viewer, connection, source.name, start_of_utc_day(clock()))


def _warn_quota_stop(
    source: PaperSource, journal: Journal, quota: OpenAlexQuota, usage: ApiUsage
) -> None:
    log.warning(
        "今天的 API 額度用到設定的比例，先停下",
        source=source.name.value,
        journal=journal.key,
        used_usd=str(usage.used_usd),
        daily_budget_usd=str(quota.daily_budget_usd),
        stop_at_ratio=str(quota.stop_at_ratio),
    )


def _record_calls(database: Database, calls: Sequence[ApiCall]) -> None:
    """這次的 API 呼叫紀錄用自己的短交易立刻 commit（Conventions 的例外）。"""
    if calls:
        with database.transaction() as connection:
            create_api_calls(connection, calls)


def _store_raw(
    database: Database,
    viewer: SystemViewer,
    source: PaperSource,
    store: ObjectStore,
    records: Sequence[RawRecord],
) -> tuple[int, int]:
    """還沒存過的內容 put 進物件儲存；回傳（新存的物件數, 沿用的物件數）。"""
    unique: dict[str, RawRecord] = {}
    for record in records:
        unique.setdefault(record.sha256, record)
    if not unique:
        return 0, 0
    with database.transaction() as connection:
        existing = find_stored_sha256s(viewer, connection, source.name, unique)
    new = [sha256 for sha256 in unique if sha256 not in existing]
    for sha256 in new:
        store.put(
            raw_object_key(source.name, sha256),
            compress_raw(unique[sha256].content),
            content_type=RAW_CONTENT_TYPE,
        )
    return len(new), len(unique) - len(new)
