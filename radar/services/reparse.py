"""重新解析：只靠已經存下來的原始資料重跑「解析 → 寫入」（FR-2、ADR-0014 決定第 4 項、ADR-0022）。

不收 `PaperSource`、不連任何外部來源；只讀 `raw_fetch` 與物件儲存。只更新已經匯入的論文
（OpenAlex ID 已經在 `external_id`），不新增論文。解析結果先放 `ingest_staging`，最後一個交易
套用；不寫 `first_snapshot_id`、不建立也不發佈快照。任何物件儲存的呼叫時都沒有開著的交易。

和每週更新、和另一個 reparse 互斥（ADR-0026 取捨）：整段持有原始資料與論文的寫入鎖
（`INGEST_WRITE_LOCK_KEY` 的獨佔 session 級 advisory lock），拿不到、或拿到後發現有 `fetching`
的 `weekly_run`，就拒絕執行、不寫任何東西。持有鎖的期間每個交易都開在持有鎖的同一條連線上、
第一句確認鎖還在；那條連線斷了（鎖跟著消失）就中止整次作業，不換一條連線繼續寫。
"""

import hashlib
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog

from radar.domain.domain_config import ExclusionRulesConfig
from radar.domain.exclusion import exclusion_reason
from radar.domain.identifiers import ExternalId, IdentifierScheme
from radar.domain.papers import KeywordSource, PaperOrigin, ParsedPaper, UpsertOutcome
from radar.domain.parsers import ParseError
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.permissions import Action, require
from radar.domain.policy import is_demo_eligible
from radar.domain.raw_records import RawFetchRef, RawSource, decompress_raw
from radar.domain.scope import ScopeSettings
from radar.domain.staging import StagingItem, StagingKind
from radar.domain.storage_keys import ObjectNotFoundError, raw_object_key
from radar.domain.viewer import SystemViewer, Viewer
from radar.ports import ObjectStore
from radar.repo.db import INGEST_WRITE_LOCK_KEY, Connection, Database, SessionLock

# 入口不能直接 import radar.repo；reparse 會丟這個例外，從這裡轉出去給 `radar reparse` 攔。
from radar.repo.db import AdvisoryLockLostError as AdvisoryLockLostError
from radar.repo.ingest_staging import (
    create_staged_papers,
    delete_staged_papers,
    delete_staged_papers_of_kind,
    list_staged_papers,
)
from radar.repo.papers import (
    find_known_external_ids,
    list_demo_eligible_facts,
    update_paper_keywords,
    update_papers_not_demo_eligible,
    upsert_by_external_id,
)
from radar.repo.raw_fetches import list_latest_raw_fetches
from radar.repo.weekly_runs import find_fetching_weekly_run

log = structlog.get_logger(__name__)

# 解析函式：`parse(content, fetched_at=...)`，看不懂丟 `ParseError`（測試可以換掉）。
type Parse = Callable[..., ParsedPaper]


class WeeklyUpdateInProgressError(Exception):
    """每週更新正在進行（有一筆 `fetching` 的 `weekly_run`），所以不跑 reparse。

    reparse 讀每篇最新的原始資料，會包含這一週抓了、還沒套用的；這時跑會把它們提早寫進論文，
    每週套用失敗退回時也不會跟著退回。
    """

    def __init__(self, weekly_run_id: uuid.UUID) -> None:
        super().__init__(
            f"每週更新正在進行（weekly_run {weekly_run_id}），等它發佈或失敗後再跑 reparse"
        )
        self.weekly_run_id = weekly_run_id


class ReparseBusyError(Exception):
    """拿不到原始資料與論文的寫入鎖：另一個 reparse 正在跑，或每週更新正在開始抓取或套用。

    兩個 reparse 同時跑會互刪暫存列；和每週更新重疊會把還沒套用的資料提早寫進論文。
    """

    def __init__(self) -> None:
        super().__init__("另一個 reparse 或每週更新正在寫入論文，等它結束後再跑 reparse")


@dataclass(frozen=True)
class ReparseResult:
    """一次重新解析做了什麼。

    - `raw_keys`：讀到的外部鍵數（每個外部鍵只看最新的一份原始資料）。
    - `not_in_database`：OpenAlex ID 不在資料庫、所以略過的（reparse 不新增論文）。
    - `missing_object`、`hash_mismatch`、`parse_error`、`id_mismatch`：各種失敗而略過的篇數
      （物件不見、內容的 SHA-256 對不上或解壓不了、解析失敗、解析出的 OpenAlex ID 和外部鍵不同）。
    - `staged`：放進暫存表的篇數。
    - `updated`：套用時寫進論文的篇數；`stale`：資料比論文上的舊、什麼都沒寫的篇數。
    - `conflicts`：識別碼已經屬於另一篇論文、沒有掛上去的個數。
    - `demo_rechecked`：套用後重判「示範站可用」的篇數（這次沒有重寫到、原本 `demo_eligible` 為
      真的論文；ADR-0052 第 9 點）；`demo_revoked`：其中從 true 改成 false 的篇數。
    """

    raw_keys: int = 0
    not_in_database: int = 0
    missing_object: int = 0
    hash_mismatch: int = 0
    parse_error: int = 0
    id_mismatch: int = 0
    staged: int = 0
    updated: int = 0
    stale: int = 0
    conflicts: int = 0
    demo_rechecked: int = 0
    demo_revoked: int = 0


@dataclass
class _Counts:
    raw_keys: int = 0
    not_in_database: int = 0
    failures: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("missing_object", "hash_mismatch", "parse_error", "id_mismatch"), 0
        )
    )
    staged: int = 0
    updated: int = 0
    stale: int = 0
    conflicts: int = 0
    demo_rechecked: int = 0
    demo_revoked: int = 0


def reparse_papers(
    actor: Viewer,
    database: Database,
    store: ObjectStore,
    *,
    scope: ScopeSettings,
    exclusion_rules: ExclusionRulesConfig,
    batch_size: int = 500,
    parse: Parse = parse_openalex_work,
) -> ReparseResult:
    """用已存的原始資料重新解析已經匯入的論文。

    0. 試拿寫入鎖（`database.session_lock(INGEST_WRITE_LOCK_KEY)`，專用連線、不等），拿不到就丟
       `ReparseBusyError`（什麼都不寫）。拿到後下面每一步都在持有鎖的期間做，結束時（含例外）
       解鎖；每週更新在開始抓取與套用時試拿同一把鍵的共用鎖，所以 reparse 跑的時候不會開始。
       下面每個交易都用 `lock.transaction()`：開在持有鎖的同一條連線上，第一句確認鎖還在。
       持鎖的連線中途斷了（例如資料庫重啟），鎖就跟著消失、別的作業可能已經拿到，所以下一個
       交易丟 `AdvisoryLockLostError`、整次作業中止；已經 commit 的只有暫存列（下次一開始清掉）。
    1. 短交易：有 `fetching` 的 `weekly_run` 就丟 `WeeklyUpdateInProgressError`（什麼都不寫）；
       沒有才刪掉上一次沒做完留下的 `kind='reparse'` 暫存列；產生新的 `batch_id`。
       這一步在拿到鎖之後：鎖擋住「之後才開始」的每週更新，這裡擋住「已經開始」的。
    2. 每批：短交易讀 `batch_size` 個外部鍵最新的 `raw_fetch`，篩出 OpenAlex ID 已經在資料庫的；
       交易結束後逐一 GET 物件 → 解壓 → 核對 SHA-256 → 解析 → 核對 OpenAlex ID → 排除規則。
       任何一步失敗就計數、記警告日誌（只帶外部鍵與原因代碼）、略過那一篇。最後短交易寫這一批的
       暫存列。
    3. 一個交易：依 `id` 分批讀這個 `batch_id` 的暫存列，逐筆 `upsert_by_external_id`（`tracked`、
       暫存列的 `excluded_reason`），不是 STALE 時整批覆寫 OpenAlex 的關鍵字；全部寫完在同一個
       交易刪掉這一批的暫存列。接著在**同一個交易**重判示範站可用（ADR-0052 第 9 點）：
       `demo_eligible` 為真、而這次沒有寫到（UPDATED）的論文（沒有原始資料紀錄、各種失敗略過、
       STALE），用存下的授權與出版社欄位經 `is_demo_eligible`（同一份 `scope.demo_publishers`）
       再算一次，算出 False 的才改成 false；只往 false 改。中途失敗整個交易退回（論文、摘要、
       關鍵字、`demo_eligible` 都不變），暫存列留著，下次執行一開始清掉；套用與重判在同一個交易，
       不會有「套用了、還沒重判」的中間狀態，重跑幾次結果一樣。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫與物件儲存）。
        ValueError: `batch_size` 不是正整數。
        ReparseBusyError: 另一個 reparse 或每週更新正在寫入（不寫任何東西、不碰物件儲存）。
        WeeklyUpdateInProgressError: 每週更新正在進行（不寫任何東西、不碰物件儲存）。
        AdvisoryLockLostError: 持鎖的連線中途斷了（那個交易沒有 commit，論文不變）。
    """
    require(actor, Action.INGEST_PAPERS, None)
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError("batch_size 要是正整數")
    with database.session_lock(INGEST_WRITE_LOCK_KEY) as lock:
        if lock is None:
            raise ReparseBusyError
        return _reparse_locked(
            lock,
            store,
            scope=scope,
            exclusion_rules=exclusion_rules,
            batch_size=batch_size,
            parse=parse,
        )


def _reparse_locked(
    lock: SessionLock,
    store: ObjectStore,
    *,
    scope: ScopeSettings,
    exclusion_rules: ExclusionRulesConfig,
    batch_size: int,
    parse: Parse,
) -> ReparseResult:
    """`reparse_papers` 的第 1～3 步（呼叫端已經持有寫入鎖；交易都開在 `lock` 的連線上）。"""
    viewer = SystemViewer()
    counts = _Counts()

    with lock.transaction() as connection:
        fetching = find_fetching_weekly_run(viewer, connection)
        if fetching is not None:
            raise WeeklyUpdateInProgressError(fetching.id)
        delete_staged_papers_of_kind(connection, StagingKind.REPARSE)
    batch_id = uuid.uuid7()

    after: str | None = None
    while True:
        with lock.transaction() as connection:
            refs = list_latest_raw_fetches(
                viewer, connection, RawSource.OPENALEX, after_external_key=after, limit=batch_size
            )
            known = find_known_external_ids(viewer, connection, [_openalex_id(r) for r in refs])
        if not refs:
            break
        after = refs[-1].external_key
        counts.raw_keys += len(refs)

        items: list[StagingItem] = []
        for ref in refs:
            if _openalex_id(ref) not in known:
                counts.not_in_database += 1
                continue
            item = _stage_one(store, ref, parse, exclusion_rules, counts)
            if item is not None:
                items.append(item)
        if items:
            with lock.transaction() as connection:
                counts.staged += create_staged_papers(
                    connection, batch_id=batch_id, kind=StagingKind.REPARSE, items=items
                )
        if len(refs) < batch_size:
            break

    _apply(lock, viewer, batch_id, scope, batch_size, counts)

    result = ReparseResult(
        raw_keys=counts.raw_keys,
        not_in_database=counts.not_in_database,
        missing_object=counts.failures["missing_object"],
        hash_mismatch=counts.failures["hash_mismatch"],
        parse_error=counts.failures["parse_error"],
        id_mismatch=counts.failures["id_mismatch"],
        staged=counts.staged,
        updated=counts.updated,
        stale=counts.stale,
        conflicts=counts.conflicts,
        demo_rechecked=counts.demo_rechecked,
        demo_revoked=counts.demo_revoked,
    )
    log.info("重新解析結束", source=RawSource.OPENALEX.value, **vars(result))
    return result


def _openalex_id(ref: RawFetchRef) -> ExternalId:
    return ExternalId(IdentifierScheme.OPENALEX, ref.external_key)


def _skip(counts: _Counts, ref: RawFetchRef, reason: str) -> None:
    counts.failures[reason] += 1
    log.warning("重新解析略過一篇", external_key=ref.external_key, reason=reason)


def _stage_one(
    store: ObjectStore,
    ref: RawFetchRef,
    parse: Parse,
    exclusion_rules: ExclusionRulesConfig,
    counts: _Counts,
) -> StagingItem | None:
    """讀出一篇的原始內容、核對、解析、套排除規則；失敗就計數、記警告、回 None。"""
    try:
        data = store.get(raw_object_key(RawSource.OPENALEX, ref.sha256))
    except ObjectNotFoundError:
        _skip(counts, ref, "missing_object")
        return None
    try:
        content = decompress_raw(data)
    except OSError, EOFError, zlib.error:
        # 壞掉的 gzip（BadGzipFile 是 OSError）：內容核對不了，和 SHA-256 對不上同一類。
        _skip(counts, ref, "hash_mismatch")
        return None
    if hashlib.sha256(content).hexdigest() != ref.sha256:
        _skip(counts, ref, "hash_mismatch")
        return None
    try:
        parsed = parse(content, fetched_at=ref.fetched_at)
    except ParseError:
        _skip(counts, ref, "parse_error")
        return None
    if parsed.openalex_id != ref.external_key:
        _skip(counts, ref, "id_mismatch")
        return None
    return StagingItem(
        external_key=ref.external_key,
        paper=parsed,
        excluded_reason=exclusion_reason(parsed, exclusion_rules),
    )


def _apply(
    lock: SessionLock,
    viewer: SystemViewer,
    batch_id: uuid.UUID,
    scope: ScopeSettings,
    batch_size: int,
    counts: _Counts,
) -> None:
    """一個交易：把這一批的暫存列逐筆寫進論文，寫完刪掉暫存列，再重判沒寫到的論文的示範站可用。

    讀到的暫存列數和放進去的不一樣（例如另一個 reparse 同時在跑、一開始把這一批刪掉）就丟錯，
    整個交易退回，不會只寫一半。
    """
    rewritten: set[uuid.UUID] = set()
    with lock.transaction() as connection:
        after_id: uuid.UUID | None = None
        applied = 0
        while True:
            staged = list_staged_papers(
                viewer, connection, batch_id, after_id=after_id, limit=batch_size
            )
            if not staged:
                break
            applied += len(staged)
            for row in staged:
                result = upsert_by_external_id(
                    connection,
                    row.paper,
                    origin=PaperOrigin.TRACKED,
                    excluded_reason=row.excluded_reason,
                    scope=scope,
                )
                if result.outcome is UpsertOutcome.CREATED:
                    # 暫存的都是 OpenAlex ID 已經在資料庫的論文，又沒有任何地方刪論文：
                    # 走到這裡代表資料不一致，整個套用退回。
                    raise RuntimeError("重新解析不該新增論文")
                if result.outcome is UpsertOutcome.STALE:
                    counts.stale += 1
                    continue
                counts.updated += 1
                rewritten.add(result.paper_id)
                counts.conflicts += len(result.conflicts)
                update_paper_keywords(
                    connection, result.paper_id, KeywordSource.OPENALEX, row.paper.keywords
                )
            after_id = staged[-1].id
        if applied != counts.staged:
            raise RuntimeError(
                "暫存列數和放進去的不一樣，可能有另一個 reparse 同時在跑；已整個退回"
            )
        delete_staged_papers(connection, batch_id)
        _recheck_demo_eligible(connection, viewer, scope, rewritten, batch_size, counts)


def _recheck_demo_eligible(
    connection: Connection,
    viewer: SystemViewer,
    scope: ScopeSettings,
    rewritten: set[uuid.UUID],
    batch_size: int,
    counts: _Counts,
) -> None:
    """重判這次沒有重寫到的論文的 `demo_eligible`，只把 true 改成 false（ADR-0052 第 6、9 點）。

    判定只經 `is_demo_eligible`，用存下的 `primary_license`、`publisher_ids`、`publisher_names` 與
    套用用的同一份 `scope.demo_publishers`。排除清單沒有載入（None）時 `is_demo_eligible` 一律
    False，所以全部改成 false：和同一次套用寫到的論文一致（`upsert_by_external_id` 也會寫 false），
    方向是「少放」；正式路徑（`radar reparse`）一定帶 `DomainConfig.scope_settings()`。
    """
    after_id: uuid.UUID | None = None
    while True:
        page = list_demo_eligible_facts(viewer, connection, after_id=after_id, limit=batch_size)
        if not page:
            break
        revoke: list[uuid.UUID] = []
        for facts in page:
            if facts.paper_id in rewritten:
                continue
            counts.demo_rechecked += 1
            if not is_demo_eligible(
                facts.primary_license,
                facts.publisher_ids,
                facts.publisher_names,
                scope.demo_publishers,
            ):
                revoke.append(facts.paper_id)
        counts.demo_revoked += update_papers_not_demo_eligible(connection, revoke)
        after_id = page[-1].paper_id
    if counts.demo_revoked:
        log.info(
            "重判示範站可用：沒重寫到的論文有幾篇改成不可用",
            rechecked=counts.demo_rechecked,
            revoked=counts.demo_revoked,
        )
