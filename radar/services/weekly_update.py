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
- `weekly_run.updated_at` 每頁、額度停下、失敗（含建 adapter 失敗）、已經抓完又被排入、因為
  reparse 正在跑而先不做時都更新，sweep 照 2 小時的時限補排（ADR-0025）。

失敗上限（Story 1.11、ADR-0028）：準備抓取、抓取、準備套用、套用每失敗一次，就用一個短交易
更新 `updated_at` 並把 `weekly_run.failure_count` 加一（先保存次數）；從建立起累計到
`max_failures`（設定 `weekly_update_max_failures`）時，再用另一個交易「收尾」：改成 `failed`、刪掉
這一批暫存列，記錯誤日誌、不送心跳。收尾沒做成時次數已經存下了：那一筆仍是 `fetching`，但之後的
工作重試、sweep 補排、下一次 cron 讀到「已到上限」只再試收尾，不抓取也不套用（cron 收尾後開新的
一筆）。改成 `failed` 之後，工作重試、sweep 都看到不是 `fetching` 而略過。額度停下、reparse 正在跑
而先不做、略過、已經抓完又被排入都不算失敗。

抓取每頁以這一筆可以寫為條件（Story 1.11 Codex C1）：寫原始資料（`raw_fetch`）的交易、寫暫存列
與進度的交易，第一句都是 `update_weekly_run_touched_while_fetching`（這一筆仍是 `fetching`、
還沒到失敗上限才改到，並鎖住這一列到交易結束）。沒改到就整個交易不寫、停下這次抓取（記警告、
回 `skipped`、不算失敗、不送心跳、不丟錯，工作正常結束）。佇列鎖平常讓同一筆不會有兩個工作
同時抓，但 sweep 把仍在跑的工作誤判成卡住時可能有；這一道讓「另一個工作已經把它標成 `failed`」
之後，舊的工作不會再寫進資料庫，reparse 也就不會和它重疊（ADR-0026 補充）。

和 `radar reparse` 互斥（ADR-0026 取捨）：開始抓取或套用前讀狀態的那個交易、以及套用的交易，
第一件事是試拿 `INGEST_WRITE_LOCK_KEY` 的共用交易級 advisory lock；拿不到代表 reparse 正在跑，
這次不抓也不套用（不碰 source、物件儲存、論文），更新 `updated_at`、記警告、正常結束（不算失敗），
那筆 `fetching` 留著，sweep 照 2 小時的時限補排後接著做。建立 `weekly_run` 不拿鎖：它不讀寫
原始資料與論文，而且留下這一列 sweep 才補排得到（cron 一週才排一次）。

套用段（Story 1.9、AD-19、ADR-0011「快照」、ADR-0014「寫入分兩段」、ADR-0026）：

- `apply_weekly_update`：14 本都抓完後，一個交易先把 `weekly_run` 從 `fetching` 改成
  `published`（搶不到就不做），建快照、逐批把暫存列寫進論文、指定 `first_snapshot_id`、發佈快照、
  刪掉這一批暫存列；commit 之後才送 `weekly-update` 心跳。中途失敗整個交易退回。
  有傳 `enqueue` 時，心跳之後排入向量化（`embed_papers`，Story 2.8）：新的、標題或摘要變了的
  論文由那個工作照 `plan_for` 與輸入指紋挑出來做；接著排入 AI 工作的 Batch 規劃
  （`plan_llm_batches(weekly_run_id)`，Story 2.9）。排入失敗只記警告，不影響已發佈的快照。
- `advance_weekly_update`（worker 的 `run_weekly_update` 工作用）：先照
  `open_and_run_weekly_update` 抓，抓完了才讀追蹤範圍、接著套用。

AI 處理完成（Story 2.11、AD-14）：`mark_weekly_runs_ai_done`（`poll_llm_batches` 每次最後呼叫）
把這一週的 Batch 都結束了（有批次、全部 `imported`／`failed`）或快照建立已滿 24 小時的
`published` 改成 `ai_done`（條件式 UPDATE，並行的兩個呼叫只會改一次）。

快照與 `first_snapshot_id` 只由這個模組寫（架構測試守著）。抓取段不寫 `paper` 等論文表；任何時候
都不在開著的交易裡呼叫 source、物件儲存或心跳。
"""

import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, ExitStack, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any

import structlog

from radar.domain.collection import collection_skip_reason
from radar.domain.domain_config import ExclusionRulesConfig, Journal, JournalsConfig, OpenAlexQuota
from radar.domain.exclusion import exclusion_reason
from radar.domain.jobs import (
    EMBED_PAPERS,
    PLAN_LLM_BATCHES,
    RUN_WEEKLY_UPDATE,
    WEEKLY_UPDATE,
    Enqueue,
)
from radar.domain.llm_batches import AI_DONE_AFTER
from radar.domain.papers import (
    KeywordSource,
    PaperOrigin,
    ParsedPaper,
    UpsertOutcome,
    UpsertResult,
)
from radar.domain.parsers import ParseError
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.permissions import Action, require
from radar.domain.raw_records import RawRecord
from radar.domain.scope import ScopeSettings
from radar.domain.staging import StagingItem, StagingKind
from radar.domain.viewer import SystemViewer, Viewer
from radar.domain.weekly_run import (
    DEFAULT_MAX_FAILURES,
    JournalProgress,
    PageCounts,
    WeeklyRunStatus,
    check_max_failures,
)
from radar.ports import ObjectStore, PaperSource
from radar.repo.db import INGEST_WRITE_LOCK_KEY, Database
from radar.repo.ingest_staging import (
    delete_staged_papers,
    list_staged_papers,
    upsert_staged_papers,
)
from radar.repo.papers import (
    update_paper_keywords,
    update_papers_first_snapshot,
    upsert_by_external_id,
)
from radar.repo.raw_fetches import create_raw_fetches
from radar.repo.snapshots import create_snapshot, update_snapshot_published
from radar.repo.weekly_runs import (
    create_weekly_run,
    find_fetching_weekly_run,
    get_weekly_run,
    list_weekly_run_journals,
    update_weekly_run_failed,
    update_weekly_run_failure,
    update_weekly_run_fetch_finished,
    update_weekly_run_published,
    update_weekly_run_touched,
    update_weekly_run_touched_while_fetching,
    update_weekly_runs_ai_done,
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

    - `skipped`：那筆 `weekly_run` 不存在、已經不是 `fetching`，或已到失敗上限（只做了收尾）。
      一開始就發現時什麼都沒抓；抓到一半才發現（另一個工作把它標成 `failed` 或記到上限）時，
      那一頁起什麼都沒寫進資料庫，之前暫存好的頁照舊（`journals`、`pages`、`counts` 是這之前的）。
    - `blocked_by_reparse`：`radar reparse` 正在跑（拿不到寫入鎖），這次不抓也不套用；只更新了
      `updated_at`，不算失敗，sweep 之後補排時接著做。
    - `stopped_for_quota`：今天的 OpenAlex 額度用到設定的比例而停下（不算失敗，留到下次）。
    - `fetch_finished`：全部期刊都抓完了（這次抓完，或之前已經抓完）。
    - `journals`：這一次輪到抓的期刊代號（還沒抓完的，依設定的順序）；額度在第一個請求前
      就停下時，那一本也在裡面。
    - `pages`、`counts`：這一次暫存好的頁數與篇數（不含之前已經做完的）。
    """

    weekly_run_id: uuid.UUID
    skipped: bool = False
    blocked_by_reparse: bool = False
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

# 讀追蹤範圍（`config/journals.yaml` 的期刊與起始年）；設定壞掉時丟錯。抓完才呼叫。
type LoadScope = Callable[[], ScopeSettings]

# 送心跳：（檢查名稱）→ 不回傳；送不出去只記日誌、不丟錯（`adapters/heartbeat`）。
type SendHeartbeat = Callable[[str], None]


@dataclass(frozen=True)
class WeeklyApplyResult:
    """`apply_weekly_update` 這一次做了什麼。

    - `skipped`：那筆 `weekly_run` 不存在、不是 `fetching`、還沒抓完，或失敗次數已到上限（等著
      收尾），什麼都沒做（沒有快照、不送心跳）。
    - `blocked_by_reparse`：`radar reparse` 正在跑（拿不到寫入鎖），什麼都沒寫（沒有快照、不送
      心跳），只更新了 `updated_at`；不算失敗，sweep 之後補排時接著套用。
    - `snapshot_id`、`snapshot_number`、`as_of`：這次發佈的快照。
    - `staged`：讀到的暫存列數；`created`、`updated`：新增、更新的論文篇數；`stale`：資料比論文上的
      舊、什麼都沒寫的篇數。
    - `identifiers_split`：DOI 與 OpenAlex ID 屬於兩篇不同的論文、這一筆略過不寫的篇數（兩篇都
      不動，每一筆另有一行警告日誌；ADR-0020 更新 2026-11-22）。
    - `same_scheme_mismatch`：用一個識別碼找到論文、那篇已經有同一種不同值的識別碼、這一筆略過
      不寫的篇數（那篇不動，每一筆另有一行警告日誌；ADR-0020 更新 2026-11-22 第二節）。
    - `changed`：`updated` 裡 `source_hash` 變了的篇數（摘要、撤稿標記、書目欄位改了；只改被引用
      次數或關鍵字不算；ADR-0028）。
    - `first_snapshot_assigned`：這次指到新快照的論文篇數（含之前沒有著落的）。
    """

    weekly_run_id: uuid.UUID
    skipped: bool = False
    blocked_by_reparse: bool = False
    snapshot_id: uuid.UUID | None = None
    snapshot_number: int | None = None
    as_of: datetime | None = None
    staged: int = 0
    created: int = 0
    updated: int = 0
    stale: int = 0
    first_snapshot_assigned: int = 0
    changed: int = 0
    identifiers_split: int = 0
    same_scheme_mismatch: int = 0


@dataclass(frozen=True)
class WeeklyAdvanceResult:
    """`advance_weekly_update` 的結果：抓取段的結果，以及（抓完時）套用段的結果。"""

    fetch: WeeklyFetchResult
    apply: WeeklyApplyResult | None


def weekly_update(
    actor: Viewer,
    database: Database,
    enqueue: Enqueue,
    *,
    max_failures: int = DEFAULT_MAX_FAILURES,
) -> WeeklyUpdateStart:
    """開始（或接著）一次每週更新：寫（或找到）那筆 `fetching` 的 `weekly_run`，交易結束後
    排入 `run_weekly_update(weekly_run_id)`。

    已經有一筆 `fetching` 時不新增第二筆（部分唯一索引），排入的是同一個 `weekly_run_id`；
    那個工作已經在排隊時不重複排（排隊鎖），都不算失敗。找到的那一筆失敗次數已到 `max_failures`
    （上次收尾沒做成）時，先收尾（改成 `failed`、刪暫存列），再建新的一筆（ADR-0028）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: `max_failures` 不是正整數（不碰資料庫）。
        RuntimeError: 建不了、也找不到 `fetching` 的那一筆（兩步之間那一筆剛好結束，再試一次
            仍然一樣）。
        Exception: 收尾那個交易的錯誤（已記警告），原樣往上丟；工作層重試時再試收尾。
    """
    require(actor, Action.INGEST_PAPERS, None)
    check_max_failures(max_failures)
    viewer = SystemViewer()
    weekly_run_id: uuid.UUID | None = None
    created = False
    vanished = 0
    finished_stale = False
    while weekly_run_id is None:
        with database.transaction() as connection:
            new_id = create_weekly_run(connection)
            existing = None if new_id is not None else find_fetching_weekly_run(viewer, connection)
        if new_id is not None:
            weekly_run_id, created = new_id, True
        elif existing is None:
            vanished += 1
            if vanished == 2:
                raise RuntimeError("建不了也找不到進行中（fetching）的每週更新")
        elif existing.failure_count >= max_failures and not finished_stale:
            # 已到上限、上次沒收尾成功的那一筆：先收尾，下一圈就建得起新的一筆。
            _finish_failed(database, existing.id, max_failures)
            finished_stale = True
        else:
            weekly_run_id = existing.id

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
    max_failures: int = DEFAULT_MAX_FAILURES,
) -> WeeklyFetchResult:
    """推進這一次每週更新的抓取段。

    1. 短交易讀 `weekly_run` 與每本期刊的進度。不存在或不是 `fetching`：記 info、回 `skipped`
       （不碰 source）；拿不到寫入鎖（reparse 正在跑）：更新 `updated_at`、記警告、回
       `blocked_by_reparse`（不碰 source）；失敗次數已到 `max_failures`：只收尾（改成 `failed`、
       刪暫存列）、回 `skipped`（不碰 source、不算失敗）；已經有 `fetch_finished_at`：只更新
       `updated_at` 就回（不抓）。
    2. 依 `journals.journals` 的順序：已經抓完的跳過；其餘從進度的 `next_cursor`（None＝第一頁）
       用 `fetch_journal` 抓。每存好一頁：交易外逐篇解析（`ParseError` 或 OpenAlex ID 對不上外部鍵
       ＝`unparseable`）→ 收錄過濾（擋下的＝`out_of_scope`）→ 排除規則 → 再開一個交易
       upsert 暫存列並記下這一頁的進度（第一頁時計數歸零重算）。寫 `raw_fetch` 與寫暫存的交易
       第一句都確認這一筆仍是 `fetching`、還沒到失敗上限並鎖住它；不是的話整個交易不寫，停下
       這次抓取、記警告、回 `skipped`（不算失敗、不丟錯）。
    3. 額度停下：短交易更新 `weekly_run.updated_at`、記警告、回 `stopped_for_quota=True`
       （不算失敗、不往下抓）。
    4. 全部期刊都抓完：短交易寫 `fetch_finished_at`。
    5. 抓取途中任何例外：記錯誤日誌（只帶期刊代號與原因或錯誤類別）、`_record_failure` 一個短
       交易更新 `updated_at` 並記一次失敗（到 `max_failures` 時另一個交易改成 `failed`、刪暫存列；
       這兩步失敗時只記一筆警告），再原樣往上丟原本的例外。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫與 source）。
        ValueError: `max_failures` 不是正整數（不碰資料庫與 source）。
        JournalFetchError: 一本期刊抓到一半失敗。進度已保存：工作層重試或 sweep 補排時，從保存的
            `next_cursor`（和 `resume_cursor` 是同一頁）接著抓。
        Exception: 物件儲存、資料庫或解析暫存時的其他錯誤，同樣原樣往上丟，續跑方式一樣。已到失敗
            上限而收尾的交易失敗時，那個錯誤（已記警告）也原樣往上丟，不算一次失敗。
    """
    require(actor, Action.INGEST_PAPERS, None)
    check_max_failures(max_failures)
    done, progress = _read_run(database, weekly_run_id, max_failures)
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
        max_failures=max_failures,
    )


def open_and_run_weekly_update(
    actor: Viewer,
    database: Database,
    open_setup: OpenFetchSetup,
    weekly_run_id: uuid.UUID,
    *,
    parse: Parse = parse_openalex_work,
    clock: Callable[[], datetime] = _utc_now,
    max_failures: int = DEFAULT_MAX_FAILURES,
) -> WeeklyFetchResult:
    """先讀狀態，確認要抓才 `open_setup()` 開 adapter 與設定，再照 `run_weekly_update` 抓。

    不存在、不是 `fetching`、reparse 正在跑、已到失敗上限、已經抓完時，和 `run_weekly_update`
    一樣（略過、只收尾，或只更新 `updated_at`），而且不呼叫 `open_setup`：sweep 補排已經抓完的
    那一筆時不讀設定、不建 adapter。
    `open_setup` 丟錯（缺 OpenAlex 金鑰、物件儲存設定不齊、領域設定壞掉）時：記錯誤日誌（只帶
    錯誤類別；例外是包裝過的另帶 `cause`＝原本的錯誤類別，見 `_cause_type`）、`_record_failure`
    更新 `updated_at` 並記一次失敗，再原樣往上丟；sweep 照 2 小時的時限補排，不會每 15 分鐘就
    補排一次。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫、不呼叫 `open_setup`）。
        ValueError: `max_failures` 不是正整數（不碰資料庫、不呼叫 `open_setup`）。
        Exception: `open_setup` 的錯誤，以及 `run_weekly_update` 會丟的錯誤，原樣往上丟。
    """
    require(actor, Action.INGEST_PAPERS, None)
    check_max_failures(max_failures)
    done, progress = _read_run(database, weekly_run_id, max_failures)
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
                cause=_cause_type(exc),
            )
            _record_failure(database, weekly_run_id, max_failures)
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
            max_failures=max_failures,
        )


def advance_weekly_update(
    actor: Viewer,
    database: Database,
    open_setup: OpenFetchSetup,
    load_scope: LoadScope,
    send_heartbeat: SendHeartbeat,
    weekly_run_id: uuid.UUID,
    *,
    parse: Parse = parse_openalex_work,
    clock: Callable[[], datetime] = _utc_now,
    max_failures: int = DEFAULT_MAX_FAILURES,
    enqueue: Enqueue | None = None,
) -> WeeklyAdvanceResult:
    """推進這一次每週更新：先照 `open_and_run_weekly_update` 抓；全部抓完了才 `load_scope()`
    讀追蹤範圍，再 `apply_weekly_update` 套用。

    抓取段略過（不存在、不是 `fetching`）、因為 reparse 正在跑而先不做、或因額度停下時直接回，
    `apply` 是 None，不讀範圍。
    `load_scope` 丟錯時：記錯誤日誌「每週更新準備套用失敗」（只帶錯誤類別）、`_record_failure`
    更新 `updated_at` 並記一次失敗，再原樣往上丟（sweep 照 2 小時的時限補排）。`max_failures`
    一路傳給抓取段與套用段，`enqueue` 傳給套用段（發佈後排入向量化）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫、不呼叫任何傳入的函式）。
        ValueError: `max_failures` 不是正整數（不碰資料庫、不呼叫任何傳入的函式）。
        Exception: 抓取段、`load_scope` 與套用段的錯誤，原樣往上丟。
    """
    require(actor, Action.INGEST_PAPERS, None)
    check_max_failures(max_failures)
    fetch = open_and_run_weekly_update(
        actor,
        database,
        open_setup,
        weekly_run_id,
        parse=parse,
        clock=clock,
        max_failures=max_failures,
    )
    if not fetch.fetch_finished:
        return WeeklyAdvanceResult(fetch=fetch, apply=None)
    try:
        scope = load_scope()
    except Exception as exc:
        # 只帶錯誤類別：例外訊息可能帶設定值或路徑。
        log.error(
            "每週更新準備套用失敗",
            weekly_run_id=str(weekly_run_id),
            error=type(exc).__name__,
        )
        _record_failure(database, weekly_run_id, max_failures)
        raise
    applied = apply_weekly_update(
        actor,
        database,
        weekly_run_id,
        scope=scope,
        send_heartbeat=send_heartbeat,
        max_failures=max_failures,
        enqueue=enqueue,
    )
    return WeeklyAdvanceResult(fetch=fetch, apply=applied)


def apply_weekly_update(
    actor: Viewer,
    database: Database,
    weekly_run_id: uuid.UUID,
    *,
    scope: ScopeSettings,
    send_heartbeat: SendHeartbeat,
    batch_size: int = 500,
    max_failures: int = DEFAULT_MAX_FAILURES,
    enqueue: Enqueue | None = None,
) -> WeeklyApplyResult:
    """套用這一次每週更新：一個交易把暫存列寫進論文並發佈快照，commit 之後才送心跳。

    交易裡依序：

    0. 試拿寫入鎖的共用鎖（`try_shared_transaction_lock`）；拿不到代表 reparse 正在跑：同一個
       交易只更新 `updated_at`，記警告、回 `blocked_by_reparse`，不建快照、不送心跳、不算失敗。
       拿到的共用鎖到交易結束才放，套用的整段 reparse 都開始不了。
    1. `update_weekly_run_published`：只在 `fetching`、已經抓完、而且失敗次數還沒到
       `max_failures` 時改成 `published`；沒改到（不存在、已經套用過、還沒抓完、已到上限等著
       收尾，或並行的另一個套用先做完）就記 info、回 `skipped`，不建快照、不送心跳。
    2. `create_snapshot`（`building`，編號＝目前最大值＋1）。
    3. 依暫存列的 `id` 每批 `batch_size` 筆讀這一批（`batch_id`＝`weekly_run_id`）；逐筆
       `upsert_by_external_id`（`tracked`、暫存列的 `excluded_reason`），寫進論文時（CREATED、
       UPDATED）整批覆寫 OpenAlex 的關鍵字；STALE、「識別碼分屬兩篇」與「同一種識別碼對不上」
       （這一筆略過、分別計進 `identifiers_split`、`same_scheme_mismatch`，整批照常）不寫關鍵字。
       某一列丟例外時先記一筆警告「每週更新套用時這一列寫不進去」（只帶
       `weekly_run_id`、`external_key` 與錯誤類別，不帶內容）再往上丟。`source_hash` 變了的
       既有論文計進 `changed`。
    4. `update_papers_first_snapshot`：追蹤範圍內、還沒有著落的論文一律指到這個快照。
    5. `update_snapshot_published`（`as_of`＝發佈的時間）。
    6. `delete_staged_papers`：刪掉這一批暫存列。

    commit 之後送 `weekly-update` 心跳一次、記 info（篇數、快照編號、秒數）；有傳 `enqueue` 時
    再排入 `embed_papers`（向量化新的與變了的論文，AD-30；排入失敗只記警告、不丟錯，下一次
    發佈或 `radar embeddings backfill` 會補上），接著排入 `plan_llm_batches(weekly_run_id)`
    （AI 工作的 Batch 規劃，Story 2.9；排入失敗只記警告、不丟錯）。略過、被 reparse 擋下時
    不排。任何例外：記錯誤日誌（只帶 `weekly_run_id` 與錯誤類別）、`_record_failure` 更新
    `updated_at` 並記一次失敗，再原樣往上丟；交易整個退回，快照、論文、`weekly_run` 狀態與
    暫存列都是套用前的樣子，重跑一次就好。累計到 `max_failures` 時另一個交易把那一筆改成
    `failed`、暫存列刪掉，之後不再套用；這個收尾沒做成時那一筆仍是 `fetching`，但上面第 1 步
    不會再套用它（ADR-0028）。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫、不送心跳）。
        ValueError: `batch_size` 或 `max_failures` 不是正整數（不碰資料庫）。
        Exception: 套用時的任何錯誤（交易已整個退回）。
    """
    require(actor, Action.INGEST_PAPERS, None)
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError("batch_size 要是正整數")
    check_max_failures(max_failures)
    viewer = SystemViewer()
    started = time.monotonic()
    tally = _ApplyTally()
    try:
        with database.transaction() as connection:
            if not database.try_shared_transaction_lock(connection, INGEST_WRITE_LOCK_KEY):
                tally.blocked_by_reparse = True
                update_weekly_run_touched(connection, weekly_run_id)
            elif not update_weekly_run_published(
                connection, weekly_run_id, max_failures=max_failures
            ):
                tally.skipped = True
            else:
                snapshot_id, number = create_snapshot(connection, weekly_run_id)
                tally.snapshot_id, tally.snapshot_number = snapshot_id, number
                after_id: uuid.UUID | None = None
                while staged := list_staged_papers(
                    viewer, connection, weekly_run_id, after_id=after_id, limit=batch_size
                ):
                    tally.staged += len(staged)
                    for row in staged:
                        try:
                            written = upsert_by_external_id(
                                connection,
                                row.paper,
                                origin=PaperOrigin.TRACKED,
                                excluded_reason=row.excluded_reason,
                                scope=scope,
                            )
                            if written.outcome in _WRITTEN:
                                update_paper_keywords(
                                    connection,
                                    written.paper_id,
                                    KeywordSource.OPENALEX,
                                    row.paper.keywords,
                                )
                        except Exception as exc:
                            # 看得出是哪一列（1.9 deferred）；只帶外部鍵與錯誤類別，不帶內容。
                            log.warning(
                                "每週更新套用時這一列寫不進去",
                                weekly_run_id=str(weekly_run_id),
                                external_key=row.external_key,
                                error=type(exc).__name__,
                            )
                            raise
                        tally.count(written)
                    after_id = staged[-1].id
                tally.first_snapshot_assigned = update_papers_first_snapshot(
                    connection, snapshot_id
                )
                tally.as_of = update_snapshot_published(connection, snapshot_id)
                delete_staged_papers(connection, weekly_run_id)
    except Exception as exc:
        # 只帶錯誤類別：例外訊息可能帶 SQL、資料內容或連線資訊。
        log.error(
            "每週更新套用失敗，已整個退回",
            weekly_run_id=str(weekly_run_id),
            error=type(exc).__name__,
        )
        _record_failure(database, weekly_run_id, max_failures)
        raise

    if tally.blocked_by_reparse:
        _warn_blocked_by_reparse(weekly_run_id)
        return WeeklyApplyResult(weekly_run_id=weekly_run_id, blocked_by_reparse=True)
    if tally.skipped:
        log.info("每週更新不是抓完待套用，略過套用", weekly_run_id=str(weekly_run_id))
        return WeeklyApplyResult(weekly_run_id=weekly_run_id, skipped=True)

    # commit 之後才送：送的時候沒有開著的交易，送不出去也不影響已發佈的快照。
    send_heartbeat(WEEKLY_UPDATE.check)
    if enqueue is not None:
        _enqueue_embeddings(enqueue, weekly_run_id)
        _enqueue_llm_batches(enqueue, weekly_run_id)
    result = WeeklyApplyResult(
        weekly_run_id=weekly_run_id,
        snapshot_id=tally.snapshot_id,
        snapshot_number=tally.snapshot_number,
        as_of=tally.as_of,
        staged=tally.staged,
        created=tally.created,
        updated=tally.updated,
        stale=tally.stale,
        first_snapshot_assigned=tally.first_snapshot_assigned,
        changed=tally.changed,
        identifiers_split=tally.identifiers_split,
        same_scheme_mismatch=tally.same_scheme_mismatch,
    )
    if tally.staged == 0:
        # 每週整本重掃，正常不會 0 篇；0 篇多半是查詢或解析壞了。照樣發佈（行為不變），
        # 但留一筆警告讓人看得到（1.9 審查第 6 條）。
        log.warning("這次每週更新沒有任何暫存論文", weekly_run_id=str(weekly_run_id))
    log.info(
        "每週更新已套用並發佈快照",
        weekly_run_id=str(weekly_run_id),
        snapshot_number=tally.snapshot_number,
        staged=tally.staged,
        created=tally.created,
        updated=tally.updated,
        stale=tally.stale,
        first_snapshot_assigned=tally.first_snapshot_assigned,
        changed=tally.changed,
        identifiers_split=tally.identifiers_split,
        same_scheme_mismatch=tally.same_scheme_mismatch,
        seconds=round(time.monotonic() - started, 1),
    )
    return result


def _enqueue_embeddings(enqueue: Enqueue, weekly_run_id: uuid.UUID) -> None:
    """發佈之後排入向量化（Story 2.8）。排不進去只記警告：快照已經發佈，向量下次再補。"""
    try:
        queued = enqueue(EMBED_PAPERS, {})
    except Exception as exc:
        log.warning(
            "每週更新發佈後排入向量化失敗",
            weekly_run_id=str(weekly_run_id),
            error=type(exc).__name__,
        )
        return
    log.info("每週更新發佈後已排入向量化", weekly_run_id=str(weekly_run_id), queued=queued)


def _enqueue_llm_batches(enqueue: Enqueue, weekly_run_id: uuid.UUID) -> None:
    """發佈之後排入 AI 工作的 Batch 規劃（Story 2.9）。排不進去只記警告：快照已經發佈，下一次
    發佈再排（規劃照 `plan_for` 與 `should_run` 挑，不會漏）。"""
    try:
        queued = enqueue(PLAN_LLM_BATCHES, {"weekly_run_id": weekly_run_id})
    except Exception as exc:
        log.warning(
            "每週更新發佈後排入 Batch 規劃失敗",
            weekly_run_id=str(weekly_run_id),
            error=type(exc).__name__,
        )
        return
    log.info("每週更新發佈後已排入 Batch 規劃", weekly_run_id=str(weekly_run_id), queued=queued)


# 寫進了論文的結果：只有這兩種要接著寫關鍵字。
_WRITTEN = frozenset({UpsertOutcome.CREATED, UpsertOutcome.UPDATED})


@dataclass
class _ApplyTally:
    """`apply_weekly_update` 交易裡累計的結果。"""

    skipped: bool = False
    blocked_by_reparse: bool = False
    snapshot_id: uuid.UUID | None = None
    snapshot_number: int | None = None
    as_of: datetime | None = None
    staged: int = 0
    created: int = 0
    updated: int = 0
    stale: int = 0
    first_snapshot_assigned: int = 0
    changed: int = 0
    identifiers_split: int = 0
    same_scheme_mismatch: int = 0

    def count(self, result: UpsertResult) -> None:
        """記下一筆寫入的結果。"""
        if result.outcome is UpsertOutcome.STALE:
            self.stale += 1
            return
        if result.outcome is UpsertOutcome.IDENTIFIERS_SPLIT:
            self.identifiers_split += 1
            return
        if result.outcome is UpsertOutcome.SAME_SCHEME_MISMATCH:
            self.same_scheme_mismatch += 1
            return
        if result.outcome is UpsertOutcome.CREATED:
            self.created += 1
        else:
            self.updated += 1
            if result.source_changed:
                self.changed += 1


def _read_run(
    database: Database, weekly_run_id: uuid.UUID, max_failures: int
) -> tuple[WeeklyFetchResult | None, dict[str, JournalProgress]]:
    """短交易讀 `weekly_run` 與每本期刊的進度；是 `fetching` 時在同一個交易試拿寫入鎖的共用鎖。

    回（結果, 進度）：不存在或不是 `fetching` 時結果是 `skipped`；拿不到鎖（reparse 正在跑）時
    同一個交易更新 `updated_at`，結果是 `blocked_by_reparse`；失敗次數已到 `max_failures`（上次
    收尾沒做成）時交易結束後 `_finish_failed` 收尾，結果是 `skipped`（收尾失敗時那個錯誤往上丟）；
    已經抓完時先更新 `updated_at`，結果是 `fetch_finished`；要接著抓時結果是 None。

    鎖只在這個交易裡拿、交易結束就放；之後的抓取不必一直拿著：這一筆在這個交易裡確定是
    `fetching`，reparse 拿到獨佔鎖之後會查到它而拒絕，直到套用 commit（ADR-0026 取捨）。
    """
    viewer = SystemViewer()
    blocked = False
    with database.transaction() as connection:
        run = get_weekly_run(viewer, connection, weekly_run_id)
        progress = list_weekly_run_journals(viewer, connection, weekly_run_id) if run else {}
        if (
            run is not None
            and run.status is WeeklyRunStatus.FETCHING
            and not database.try_shared_transaction_lock(connection, INGEST_WRITE_LOCK_KEY)
        ):
            blocked = True
            update_weekly_run_touched(connection, weekly_run_id)
    if blocked:
        _warn_blocked_by_reparse(weekly_run_id)
        return WeeklyFetchResult(weekly_run_id=weekly_run_id, blocked_by_reparse=True), {}
    if run is None or run.status is not WeeklyRunStatus.FETCHING:
        log.info(
            "每週更新不是抓取中，略過",
            weekly_run_id=str(weekly_run_id),
            status=None if run is None else run.status.value,
        )
        return WeeklyFetchResult(weekly_run_id=weekly_run_id, skipped=True), {}
    if run.failure_count >= max_failures:
        # 次數已經存下、收尾（改 failed＋刪暫存）上次沒做成：只再試收尾，不抓也不套用。
        log.info(
            "每週更新已到失敗上限，只做收尾",
            weekly_run_id=str(weekly_run_id),
            failures=run.failure_count,
            max_failures=max_failures,
        )
        _finish_failed(database, weekly_run_id, max_failures)
        return WeeklyFetchResult(weekly_run_id=weekly_run_id, skipped=True), {}
    if run.fetch_finished_at is not None:
        # 抓完、還沒套用成功（例如套用失敗）的那一筆仍是 fetching：更新 updated_at，sweep 2 小時後
        # 才再補排，不是每 15 分鐘。
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
    max_failures: int,
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
            max_failures=max_failures,
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
                write_raw=partial(_write_raw, database, weekly_run_id, max_failures),
                clock=clock,
            )
        except _NotWritableError:
            # 另一個工作已經把這一筆標成 failed（或記到上限）：這一頁什麼都沒寫進資料庫（物件儲存
            # 裡剛存的原始物件留著，內容定址、之後重抓會重用），停下、不算失敗、不送心跳；正常
            # 結束，工作層不會重試。
            log.warning(
                "每週更新已經不能再寫（不是抓取中或已到失敗上限），停下這次抓取",
                weekly_run_id=str(weekly_run_id),
                journal=journal.key,
            )
            return WeeklyFetchResult(
                weekly_run_id=weekly_run_id,
                skipped=True,
                journals=tuple(fetched_journals),
                pages=tally.pages,
                counts=tally.counts,
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
            # 失敗也更新 updated_at：sweep 照 2 小時的時限補排，不會每 15 分鐘就補排一次；
            # 同一個交易記一次失敗，到上限再用另一個交易標 failed（ADR-0028）。
            _record_failure(database, weekly_run_id, max_failures)
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


def _warn_blocked_by_reparse(weekly_run_id: uuid.UUID) -> None:
    log.warning(
        "reparse 正在執行，每週更新這次先不做，sweep 補排時接著做",
        weekly_run_id=str(weekly_run_id),
    )


_CLASS_NAME = re.compile(r"[A-Z][A-Za-z0-9]{0,63}")


def _cause_type(exc: BaseException) -> str | None:
    """包裝過的例外帶的原本錯誤類別名稱（例如 worker 的 `AdapterSetupError.error_type`）。

    只讀 `error_type` 這個屬性，而且只收 Python 類別命名慣例的字串（大寫英文字母開頭、
    只有英文字母與數字、最多 64 字，見 `_CLASS_NAME`）：其他例外剛好有同名屬性、內容卻是
    訊息、網址、小寫的設定值時一律不收，回 None。這是第二道防線：目前會設這個屬性的
    `AdapterSetupError`（worker）與 `ObjectStoreUnavailableError`（物件儲存 adapter）填的都是
    `type(exc).__name__`。不讀例外訊息、`__cause__`、`__context__`。
    """
    value = getattr(exc, "error_type", None)
    if isinstance(value, str) and _CLASS_NAME.fullmatch(value):
        return value
    return None


def _touch(database: Database, weekly_run_id: uuid.UUID) -> None:
    """短交易更新 `weekly_run.updated_at`（sweep 判斷卡住用的時間欄）。"""
    with database.transaction() as connection:
        update_weekly_run_touched(connection, weekly_run_id)


def _record_failure(database: Database, weekly_run_id: uuid.UUID, max_failures: int) -> None:
    """失敗之後先用一個短交易保存次數：更新 `updated_at`、把 `failure_count` 加一（只在 `fetching`
    時）。沒到 `max_failures` 記警告「每週更新失敗，已記一次」；到了就 `_finish_failed` 收尾（另一個
    交易改成 `failed`＋刪暫存列，ADR-0028）。已經不是 `fetching` 時只更新 `updated_at`、不記。

    兩個交易分開（Story 1.11 Codex C2）：收尾一直失敗（例如逾時）時次數不會跟著退回，下一次被排入
    讀到「已到上限」就只再試收尾。

    保存次數的交易失敗時只記一筆警告（只帶錯誤類別）；收尾失敗時 `_finish_failed` 已記警告。兩種
    都不丟錯：呼叫端接著丟的是原本的例外，不會被這裡的錯誤蓋掉。
    """
    try:
        with database.transaction() as connection:
            update_weekly_run_touched(connection, weekly_run_id)
            failures = update_weekly_run_failure(connection, weekly_run_id)
    except Exception as exc:
        log.warning(
            "每週更新失敗後更新 updated_at 也失敗",
            weekly_run_id=str(weekly_run_id),
            error=type(exc).__name__,
        )
        return
    if failures is None:
        return
    if failures < max_failures:
        log.warning(
            "每週更新失敗，已記一次",
            weekly_run_id=str(weekly_run_id),
            failures=failures,
            max_failures=max_failures,
        )
        return
    with suppress(Exception):  # 已記警告；次數已存下，之後被排入時再試收尾
        _finish_failed(database, weekly_run_id, max_failures)


def _finish_failed(database: Database, weekly_run_id: uuid.UUID, max_failures: int) -> None:
    """已到失敗上限的那一筆收尾：一個交易 `update_weekly_run_failed`（仍是 `fetching`、次數已到
    `max_failures` 才改成 `failed`），改到了就同一個交易 `delete_staged_papers`；commit 後記錯誤
    「每週更新重試到上限，標成失敗」（不送心跳）。沒改到（已經不是 `fetching`）什麼都不記。

    改成 `failed` 與刪暫存同一個交易：不會留下「已經 `failed`、暫存還在」。

    Raises:
        Exception: 這個交易的錯誤：先記警告「每週更新已到失敗上限，收尾沒做成，之後再試」（只帶
            錯誤類別）再原樣往上丟。整個交易退回，那一筆仍是 `fetching`、次數不變。
    """
    try:
        with database.transaction() as connection:
            failures = update_weekly_run_failed(
                connection, weekly_run_id, max_failures=max_failures
            )
            deleted = 0 if failures is None else delete_staged_papers(connection, weekly_run_id)
    except Exception as exc:
        log.warning(
            "每週更新已到失敗上限，收尾沒做成，之後再試",
            weekly_run_id=str(weekly_run_id),
            error=type(exc).__name__,
        )
        raise
    if failures is None:
        return
    log.error(
        "每週更新重試到上限，標成失敗",
        weekly_run_id=str(weekly_run_id),
        failures=failures,
        max_failures=max_failures,
        staged_deleted=deleted,
    )


class _NotWritableError(Exception):
    """抓取途中發現這筆 `weekly_run` 不能再寫：已經不是 `fetching`，或失敗次數已到上限。只在這個
    模組裡丟、在 `_fetch_journals` 接住（丟的時候那個交易已經整個退回）。
    """


def _ensure_writable(connection: Any, weekly_run_id: uuid.UUID, max_failures: int) -> None:
    """交易的第一句：這一筆仍是 `fetching`、還沒到失敗上限才往下（並鎖住它到交易結束）；不然丟
    `_NotWritableError`，讓整個交易退回。
    """
    if not update_weekly_run_touched_while_fetching(
        connection, weekly_run_id, max_failures=max_failures
    ):
        raise _NotWritableError


def _write_raw(
    database: Database,
    weekly_run_id: uuid.UUID,
    max_failures: int,
    records: Sequence[RawRecord],
) -> None:
    """`fetch_journal` 的 `write_raw`：一個交易先確認這一筆還能寫，再寫這一頁的 `raw_fetch` 列。"""
    with database.transaction() as connection:
        _ensure_writable(connection, weekly_run_id, max_failures)
        create_raw_fetches(connection, records)


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
    max_failures: int,
) -> None:
    """一頁：交易外逐篇解析、過濾，再一個交易寫暫存列與這本期刊的進度；那個交易第一句先確認
    這一筆還能寫（不能寫就丟 `_NotWritableError`、什麼都不寫）。
    """
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
        _ensure_writable(connection, weekly_run_id, max_failures)
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


def mark_weekly_runs_ai_done(
    actor: Viewer, database: Database, *, now: datetime
) -> list[uuid.UUID]:
    """把 AI 處理完成的 `published` 每週更新改成 `ai_done`（見模組說明），回改到的 id。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: `now` 沒有時區（不碰資料庫）。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 要帶時區（不能是 naive datetime）")
    with database.transaction() as connection:
        changed = update_weekly_runs_ai_done(connection, now - AI_DONE_AFTER)
    for weekly_run_id in changed:
        log.info("每週更新的 AI 處理完成", weekly_run_id=str(weekly_run_id))
    return changed
