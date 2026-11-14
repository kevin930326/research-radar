"""每週更新的抓取段：逐本、逐頁抓，存原始資料、解析、過濾後放暫存表（FR-8、FR-9、NFR-5、
AD-14、AD-19、AD-25、ADR-0004、ADR-0014、ADR-0025）。

- `weekly_update`（cron 排入的排程工作）：寫（或找到）那筆 `fetching` 的 `weekly_run`，交易結束後
  才排入 `run_weekly_update(weekly_run_id)`（AD-14：先寫狀態列並完成交易，再排入）。
- `run_weekly_update`：依 `config/journals.yaml` 的順序逐本推進；每頁先存原始資料
  （`fetch_journal`），再解析＋收錄過濾＋排除規則，同一個交易把結果 upsert 進
  `ingest_staging`（`batch_id`＝`weekly_run.id`）並記下下一頁的 cursor 與篇數。
  中途失敗時進度已保存，下次從那一頁接著抓。
- `open_and_run_weekly_update`（worker 用）：先讀狀態，確認是 `fetching` 且還沒抓完，才呼叫
  傳入的函式讀領域設定、建 OpenAlex 與物件儲存的 adapter，再照 `run_weekly_update` 抓；
  建不起來（缺金鑰、設定壞掉）時也更新 `updated_at` 再往上丟。
- `weekly_run.updated_at` 每頁、額度停下、失敗（含建 adapter 失敗）、已經抓完又被排入時都更新，
  sweep 照 2 小時的時限補排（ADR-0025）。

套用、快照、`weekly-update` 心跳屬 Story 1.9；`failed` 屬 1.11。這裡不寫 `paper` 等論文表，
也不在開著的交易裡呼叫 source 或物件儲存。
"""

import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial

import structlog

from radar.domain.collection import collection_skip_reason
from radar.domain.domain_config import ExclusionRulesConfig, Journal, JournalsConfig, OpenAlexQuota
from radar.domain.exclusion import exclusion_reason
from radar.domain.jobs import RUN_WEEKLY_UPDATE, Enqueue
from radar.domain.papers import ParsedPaper
from radar.domain.parsers import ParseError
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.permissions import Action, require
from radar.domain.staging import StagingItem, StagingKind
from radar.domain.viewer import SystemViewer, Viewer
from radar.domain.weekly_run import JournalProgress, PageCounts, WeeklyRunStatus
from radar.ports import ObjectStore, PaperSource
from radar.repo.db import Database
from radar.repo.ingest_staging import upsert_staged_papers
from radar.repo.weekly_runs import (
    create_weekly_run,
    find_fetching_weekly_run,
    get_weekly_run,
    list_weekly_run_journals,
    update_weekly_run_fetch_finished,
    update_weekly_run_touched,
    upsert_weekly_run_journal_page,
)
from radar.services.ingest import FetchedPage, JournalFetchError, fetch_journal

log = structlog.get_logger(__name__)

# 解析函式：`parse(content, fetched_at=...)`，看不懂丟 `ParseError`（測試可以換掉）。
type Parse = Callable[..., ParsedPaper]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class WeeklyUpdateStart:
    """`weekly_update` 的結果：這一次每週更新的 `id`，以及是不是這次新建的（False＝已經有一筆
    `fetching`，排入的是同一筆）。
    """

    weekly_run_id: uuid.UUID
    created: bool


@dataclass(frozen=True)
class WeeklyFetchResult:
    """`run_weekly_update` 這一次做了什麼。

    - `skipped`：那筆 `weekly_run` 不存在或已經不是 `fetching`，什麼都沒做。
    - `stopped_for_quota`：今天的 OpenAlex 額度用到設定的比例而停下（不算失敗，留到下次）。
    - `fetch_finished`：全部期刊都抓完了（這次抓完，或之前已經抓完）。
    - `journals`：這一次輪到抓的期刊代號（還沒抓完的，依設定的順序）；額度在第一個請求前
      就停下時，那一本也在裡面。
    - `pages`、`counts`：這一次暫存好的頁數與篇數（不含之前已經做完的）。
    """

    weekly_run_id: uuid.UUID
    skipped: bool = False
    stopped_for_quota: bool = False
    fetch_finished: bool = False
    journals: tuple[str, ...] = ()
    pages: int = 0
    counts: PageCounts = field(default_factory=PageCounts)


@dataclass(frozen=True)
class WeeklyFetchSetup:
    """抓取要用的 adapter 與設定（`open_and_run_weekly_update` 確認要抓之後才開）。"""

    source: PaperSource
    store: ObjectStore
    journals: JournalsConfig
    exclusion_rules: ExclusionRulesConfig
    quota: OpenAlexQuota


# 開抓取要用的東西：讀領域設定、建 adapter；離開 `with` 時關掉。缺金鑰、設定壞掉時丟錯。
type OpenFetchSetup = Callable[[], AbstractContextManager[WeeklyFetchSetup]]


def weekly_update(actor: Viewer, database: Database, enqueue: Enqueue) -> WeeklyUpdateStart:
    """開始（或接著）一次每週更新：寫（或找到）那筆 `fetching` 的 `weekly_run`，交易結束後
    排入 `run_weekly_update(weekly_run_id)`。

    已經有一筆 `fetching` 時不新增第二筆（部分唯一索引），排入的是同一個 `weekly_run_id`；
    那個工作已經在排隊時不重複排（排隊鎖），都不算失敗。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        RuntimeError: 建不了、也找不到 `fetching` 的那一筆（兩步之間那一筆剛好結束，再試一次
            仍然一樣）。
    """
    require(actor, Action.INGEST_PAPERS, None)
    viewer = SystemViewer()
    weekly_run_id: uuid.UUID | None = None
    created = False
    for _ in range(2):
        with database.transaction() as connection:
            new_id = create_weekly_run(connection)
            if new_id is not None:
                weekly_run_id, created = new_id, True
                break
            existing = find_fetching_weekly_run(viewer, connection)
            if existing is not None:
                weekly_run_id = existing.id
                break
    if weekly_run_id is None:
        raise RuntimeError("建不了也找不到進行中（fetching）的每週更新")

    queued = enqueue(RUN_WEEKLY_UPDATE, {"weekly_run_id": weekly_run_id})
    log.info(
        "每週更新已排入抓取",
        weekly_run_id=str(weekly_run_id),
        created=created,
        queued=queued,
    )
    return WeeklyUpdateStart(weekly_run_id=weekly_run_id, created=created)


def run_weekly_update(
    actor: Viewer,
    database: Database,
    source: PaperSource,
    store: ObjectStore,
    weekly_run_id: uuid.UUID,
    *,
    journals: JournalsConfig,
    exclusion_rules: ExclusionRulesConfig,
    quota: OpenAlexQuota,
    parse: Parse = parse_openalex_work,
    clock: Callable[[], datetime] = _utc_now,
) -> WeeklyFetchResult:
    """推進這一次每週更新的抓取段。

    1. 短交易讀 `weekly_run` 與每本期刊的進度。不存在或不是 `fetching`：記 info、回 `skipped`
       （不碰 source）；已經有 `fetch_finished_at`：只更新 `updated_at` 就回（不抓）。
    2. 依 `journals.journals` 的順序：已經抓完的跳過；其餘從進度的 `next_cursor`（None＝第一頁）
       用 `fetch_journal` 抓。每存好一頁：交易外逐篇解析（`ParseError` 或 OpenAlex ID 對不上外部鍵
       ＝`unparseable`）→ 收錄過濾（擋下的＝`out_of_scope`）→ 排除規則 → 再開一個交易
       upsert 暫存列並記下這一頁的進度（第一頁時計數歸零重算）。
    3. 額度停下：短交易更新 `weekly_run.updated_at`、記警告、回 `stopped_for_quota=True`
       （不算失敗、不往下抓）。
    4. 全部期刊都抓完：短交易寫 `fetch_finished_at`。
    5. 抓取途中任何例外：記錯誤日誌（只帶期刊代號與原因或錯誤類別）、短交易更新
       `updated_at`（這一步也失敗時只記一筆警告），再原樣往上丟原本的例外。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫與 source）。
        JournalFetchError: 一本期刊抓到一半失敗。進度已保存：工作層重試或 sweep 補排時，從保存的
            `next_cursor`（和 `resume_cursor` 是同一頁）接著抓。
        Exception: 物件儲存、資料庫或解析暫存時的其他錯誤，同樣原樣往上丟，續跑方式一樣。
    """
    require(actor, Action.INGEST_PAPERS, None)
    done, progress = _read_run(database, weekly_run_id)
    if done is not None:
        return done
    return _fetch_journals(
        actor,
        database,
        source,
        store,
        weekly_run_id,
        progress,
        journals=journals,
        exclusion_rules=exclusion_rules,
        quota=quota,
        parse=parse,
        clock=clock,
    )


def open_and_run_weekly_update(
    actor: Viewer,
    database: Database,
    open_setup: OpenFetchSetup,
    weekly_run_id: uuid.UUID,
    *,
    parse: Parse = parse_openalex_work,
    clock: Callable[[], datetime] = _utc_now,
) -> WeeklyFetchResult:
    """先讀狀態，確認要抓才 `open_setup()` 開 adapter 與設定，再照 `run_weekly_update` 抓。

    不存在、不是 `fetching`、已經抓完時，和 `run_weekly_update` 一樣（略過；或只更新
    `updated_at`），而且不呼叫 `open_setup`：sweep 補排已經抓完的那一筆時不讀設定、不建 adapter。
    `open_setup` 丟錯（缺 OpenAlex 金鑰、物件儲存設定不齊、領域設定壞掉）時：記錯誤日誌（只帶
    錯誤類別）、短交易更新 `updated_at`，再原樣往上丟；sweep 照 2 小時的時限補排，不會每 15 分鐘
    就補排一次。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫、不呼叫 `open_setup`）。
        Exception: `open_setup` 的錯誤，以及 `run_weekly_update` 會丟的錯誤，原樣往上丟。
    """
    require(actor, Action.INGEST_PAPERS, None)
    done, progress = _read_run(database, weekly_run_id)
    if done is not None:
        return done
    with ExitStack() as stack:
        try:
            setup = stack.enter_context(open_setup())
        except Exception as exc:
            # 只帶錯誤類別：例外訊息可能帶設定值或網址。
            log.error(
                "每週更新準備抓取失敗",
                weekly_run_id=str(weekly_run_id),
                error=type(exc).__name__,
            )
            _touch_after_failure(database, weekly_run_id)
            raise
        return _fetch_journals(
            actor,
            database,
            setup.source,
            setup.store,
            weekly_run_id,
            progress,
            journals=setup.journals,
            exclusion_rules=setup.exclusion_rules,
            quota=setup.quota,
            parse=parse,
            clock=clock,
        )


def _read_run(
    database: Database, weekly_run_id: uuid.UUID
) -> tuple[WeeklyFetchResult | None, dict[str, JournalProgress]]:
    """短交易讀 `weekly_run` 與每本期刊的進度。

    回（結果, 進度）：不存在或不是 `fetching` 時結果是 `skipped`；已經抓完時先更新 `updated_at`，
    結果是 `fetch_finished`；要接著抓時結果是 None。
    """
    viewer = SystemViewer()
    with database.transaction() as connection:
        run = get_weekly_run(viewer, connection, weekly_run_id)
        progress = list_weekly_run_journals(viewer, connection, weekly_run_id) if run else {}
    if run is None or run.status is not WeeklyRunStatus.FETCHING:
        log.info(
            "每週更新不是抓取中，略過",
            weekly_run_id=str(weekly_run_id),
            status=None if run is None else run.status.value,
        )
        return WeeklyFetchResult(weekly_run_id=weekly_run_id, skipped=True), {}
    if run.fetch_finished_at is not None:
        # 更新 updated_at：sweep 2 小時後才再補排，不是每 15 分鐘（1.9 之前抓完仍是 fetching）。
        _touch(database, weekly_run_id)
        log.info("每週更新已經抓完，不再抓", weekly_run_id=str(weekly_run_id))
        return WeeklyFetchResult(weekly_run_id=weekly_run_id, fetch_finished=True), {}
    return None, progress


def _fetch_journals(
    actor: Viewer,
    database: Database,
    source: PaperSource,
    store: ObjectStore,
    weekly_run_id: uuid.UUID,
    progress: dict[str, JournalProgress],
    *,
    journals: JournalsConfig,
    exclusion_rules: ExclusionRulesConfig,
    quota: OpenAlexQuota,
    parse: Parse,
    clock: Callable[[], datetime],
) -> WeeklyFetchResult:
    """`run_weekly_update` 的第 2～5 步：依設定的順序逐本抓還沒抓完的期刊。"""
    tally = _Tally()
    fetched_journals: list[str] = []
    for journal in journals.journals:
        current = progress.get(journal.key)
        if current is not None and current.finished:
            continue
        fetched_journals.append(journal.key)
        stage = partial(
            _stage_page,
            database,
            weekly_run_id,
            journal,
            start_year=journals.start_year,
            exclusion_rules=exclusion_rules,
            parse=parse,
            tally=tally,
        )
        try:
            result = fetch_journal(
                actor,
                database,
                source,
                store,
                journal,
                start_year=journals.start_year,
                quota=quota,
                cursor=None if current is None else current.next_cursor,
                on_page=stage,
                clock=clock,
            )
        except Exception as exc:
            if isinstance(exc, JournalFetchError):
                log.error(
                    "每週更新抓取失敗，進度已保存",
                    weekly_run_id=str(weekly_run_id),
                    journal=exc.journal_key,
                    reason=exc.reason,
                )
            else:
                # 只帶錯誤類別：例外訊息可能帶網址或內容。
                log.error(
                    "每週更新抓取中斷，進度已保存",
                    weekly_run_id=str(weekly_run_id),
                    journal=journal.key,
                    error=type(exc).__name__,
                )
            # 失敗也更新 updated_at：sweep 照 2 小時的時限補排，不會每 15 分鐘就補排一次。
            _touch_after_failure(database, weekly_run_id)
            raise
        if result.stopped_for_quota:
            _touch(database, weekly_run_id)
            log.warning(
                "每週更新因額度停下，下次續跑",
                weekly_run_id=str(weekly_run_id),
                journal=journal.key,
            )
            return WeeklyFetchResult(
                weekly_run_id=weekly_run_id,
                stopped_for_quota=True,
                journals=tuple(fetched_journals),
                pages=tally.pages,
                counts=tally.counts,
            )

    with database.transaction() as connection:
        finished = update_weekly_run_fetch_finished(connection, weekly_run_id)
    log.info(
        "每週更新抓取完成",
        weekly_run_id=str(weekly_run_id),
        journals=len(fetched_journals),
        fetch_finished=finished,
        pages=tally.pages,
        fetched=tally.counts.fetched,
        staged=tally.counts.staged,
    )
    return WeeklyFetchResult(
        weekly_run_id=weekly_run_id,
        fetch_finished=finished,
        journals=tuple(fetched_journals),
        pages=tally.pages,
        counts=tally.counts,
    )


def _touch(database: Database, weekly_run_id: uuid.UUID) -> None:
    """短交易更新 `weekly_run.updated_at`（sweep 判斷卡住用的時間欄）。"""
    with database.transaction() as connection:
        update_weekly_run_touched(connection, weekly_run_id)


def _touch_after_failure(database: Database, weekly_run_id: uuid.UUID) -> None:
    """失敗之後更新 `updated_at`；這一步也失敗（例如資料庫正連不上）時只記一筆警告（只帶錯誤
    類別），不丟錯：呼叫端接著丟的是原本的例外，不會被這裡的錯誤蓋掉。
    """
    try:
        _touch(database, weekly_run_id)
    except Exception as exc:
        log.warning(
            "每週更新失敗後更新 updated_at 也失敗",
            weekly_run_id=str(weekly_run_id),
            error=type(exc).__name__,
        )


@dataclass
class _Tally:
    """這一次 `run_weekly_update` 暫存好的頁數與篇數。"""

    pages: int = 0
    counts: PageCounts = field(default_factory=PageCounts)


def _skip(record_key: str, reason: str) -> None:
    # 只帶外部鍵與原因代碼，不帶內容。
    log.warning("每週更新略過一篇（解析不了）", external_key=record_key, reason=reason)


def _stage_page(
    database: Database,
    weekly_run_id: uuid.UUID,
    journal: Journal,
    page: FetchedPage,
    *,
    start_year: int,
    exclusion_rules: ExclusionRulesConfig,
    parse: Parse,
    tally: _Tally,
) -> None:
    """一頁：交易外逐篇解析、過濾，再一個交易寫暫存列與這本期刊的進度。"""
    items: list[StagingItem] = []
    by_year: Counter[int] = Counter()
    unparseable = out_of_scope = excluded = 0
    for record in page.records:
        try:
            parsed = parse(record.content, fetched_at=record.fetched_at)
        except ParseError:
            unparseable += 1
            _skip(record.external_key, "parse_error")
            continue
        if parsed.openalex_id != record.external_key:
            unparseable += 1
            _skip(record.external_key, "id_mismatch")
            continue
        if parsed.publication_year is not None:
            by_year[parsed.publication_year] += 1
        if collection_skip_reason(parsed, journal_issn_l=journal.issn_l, start_year=start_year):
            out_of_scope += 1
            continue
        reason = exclusion_reason(parsed, exclusion_rules)
        if reason is not None:
            excluded += 1
        items.append(
            StagingItem(external_key=record.external_key, paper=parsed, excluded_reason=reason)
        )

    counts = PageCounts(
        fetched=len(page.records),
        fetched_by_year=by_year,
        staged=len(items),
        excluded=excluded,
        out_of_scope=out_of_scope,
        unparseable=unparseable,
    )
    with database.transaction() as connection:
        upsert_staged_papers(
            connection, batch_id=weekly_run_id, kind=StagingKind.WEEKLY_UPDATE, items=items
        )
        upsert_weekly_run_journal_page(
            connection,
            weekly_run_id,
            journal.key,
            first_page=page.sent_cursor is None,
            next_cursor=page.next_cursor,
            counts=counts,
        )
    tally.pages += 1
    tally.counts = tally.counts.plus(counts)
