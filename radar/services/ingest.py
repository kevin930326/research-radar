"""抓取：依一本期刊逐頁抓、原始資料先存（FR-2、FR-10、AD-25、AD-29、ADR-0014、ADR-0021）。

這裡只做「抓 → 存原始」兩步；解析（Story 1.7）、暫存與續跑的狀態（Story 1.8）不在這裡。
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
    JournalQuery,
    PaperSourceError,
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
    clock: Callable[[], datetime] = _utc_now,
) -> JournalFetchResult:
    """從 `cursor`（None＝第一頁）起，把這本期刊一頁一頁抓完。每一頁：

    1. 短交易讀今天（UTC）的用量＝max(今天 `cost_usd` 的總和, 最新一筆回報的「上限 − 剩餘」)；
       到 `daily_budget_usd × stop_at_ratio` 就寫警告、回傳 `stopped_for_quota=True` 與當下的
       cursor，不丟例外、不再呼叫。
    2. 呼叫 source。
    3. 不論成功或 `PaperSourceError`，這次的 `calls` 都用自己的短交易立刻寫進 `api_call`
       （失敗時寫完再原樣往上丟）。
    4. 短交易查出哪些內容（sha256）已經存過；其餘的（同頁重複只算一次）壓縮後 put 進物件儲存。
    5. 一個交易寫這一頁的 `raw_fetch` 列（同一個來源、外部鍵、抓取時間已經有了就略過）。

    先 put 再寫 `raw_fetch`：中途當掉時下次會再 put 一次同樣的位元組（無害），不會有
    「有列沒物件」。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫與 source）。
        PaperSourceError: source 抓取失敗（`api_call` 已經寫好）。
    """
    require(actor, Action.INGEST_PAPERS, None)
    viewer = SystemViewer()
    query = JournalQuery(issn_l=journal.issn_l, start_year=start_year)
    pages = records = stored = reused = 0
    stopped_for_quota = False

    while True:
        with database.transaction() as connection:
            usage = get_api_usage_since(viewer, connection, source.name, start_of_utc_day(clock()))
        if usage.used_usd >= quota.stop_at_usd:
            log.warning(
                "今天的 API 額度用到設定的比例，先停下",
                source=source.name.value,
                journal=journal.key,
                used_usd=str(usage.used_usd),
                daily_budget_usd=str(quota.daily_budget_usd),
                stop_at_ratio=str(quota.stop_at_ratio),
            )
            stopped_for_quota = True
            break

        try:
            page = source.fetch_journal_page(query, cursor)
        except PaperSourceError as exc:
            _record_calls(database, exc.calls)
            raise
        _record_calls(database, page.calls)

        new_objects, existing_objects = _store_raw(database, viewer, source, store, page.records)
        if page.records:
            with database.transaction() as connection:
                create_raw_fetches(connection, page.records)

        pages += 1
        records += len(page.records)
        stored += new_objects
        reused += existing_objects
        cursor = page.next_cursor
        if cursor is None:
            break

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
