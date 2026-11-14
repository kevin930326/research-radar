"""reparse 與每週更新共用的寫入鎖（`INGEST_WRITE_LOCK_KEY`；Story 1.9 Codex 補審 C1、
ADR-0026 取捨）。

對真的 PostgreSQL：

1. reparse 正在跑時，每週更新可以建立 `weekly_run`，但抓取與套用都不做、不算失敗、只更新
   `updated_at`；reparse 結束後接著做到快照 1。
2. 反向順序：每週更新的套用正拿著共用鎖時，reparse 拿不到鎖而拒絕、什麼都不寫。
3. 兩個 reparse 同時：第二個拒絕，第一個照常完成。
4. reparse 成功、失敗、被拒絕之後鎖都放掉了；解鎖失敗時那條連線被作廢，鎖跟著連線結束放掉
   （連線斷掉的，和連線還活著、解鎖丟出一般錯誤的兩種）。
5. 持鎖的連線中途斷了（Codex 複查 R1）：原本的 reparse 不能換一條連線繼續寫；別的作業拿到鎖之後，
   它的下一個交易丟 `AdvisoryLockLostError`、什麼都不 commit。

並行用兩條真的連線（不同的 `Database`）與執行緒；用 `threading.Event` 讓一邊停在持有鎖的
那一段，不靠 sleep 的時序，每次等待都有逾時。論文內容取自錄好的 OpenAlex 回應
（`tests/openalex_cassette.py`，CC0）與這裡自己造的假資料；每個測試一個全新、已遷移的資料庫。
"""

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, text
from structlog.testing import capture_logs

from radar.adapters.fake import FakeHeartbeat, FakeObjectStore, FakePaperSource
from radar.adapters.fake.jobs import FakeJobQueue
from radar.domain.domain_config import (
    ExclusionRulesConfig,
    Journal,
    JournalsConfig,
    OpenAlexQuota,
)
from radar.domain.papers import PaperOrigin
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.raw_records import RawRecord, RawSource, compress_raw
from radar.domain.scope import ScopeSettings
from radar.domain.storage_keys import RAW_CONTENT_TYPE, raw_object_key
from radar.domain.sweep import SWEEP_REGISTRY
from radar.domain.viewer import SystemViewer
from radar.repo.db import INGEST_WRITE_LOCK_KEY, AdvisoryLockLostError, Database
from radar.repo.papers import update_papers_first_snapshot, upsert_by_external_id
from radar.repo.raw_fetches import create_raw_fetches
from radar.repo.sweep import find_stuck_ids
from radar.repo.weekly_runs import create_weekly_run
from radar.services import reparse as reparse_module
from radar.services import weekly_update as weekly_update_module
from radar.services.reparse import (
    ReparseBusyError,
    ReparseResult,
    WeeklyUpdateInProgressError,
    reparse_papers,
)
from radar.services.weekly_update import (
    WeeklyAdvanceResult,
    WeeklyApplyResult,
    WeeklyFetchResult,
    WeeklyFetchSetup,
    advance_weekly_update,
    apply_weekly_update,
    run_weekly_update,
    weekly_update,
)
from tests.database import APP_ROLE, OWNER_ROLE, TestDatabase, connect_admin, migrate
from tests.openalex_cassette import ISSN_L, cassette_work, work_bytes

WAIT = 30.0  # 每次等待另一條執行緒的上限（秒）
NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
SYSTEM = SystemViewer()
MISQ_ISSN = "0276-7783"
MISQ = Journal(key="misq", name="MIS Quarterly", issn_l=MISQ_ISSN)
JOURNALS = JournalsConfig(start_year=2016, journals=(MISQ,))
# 追蹤範圍：每週更新抓的 MISQ，加上 reparse 用的那一篇所在的 I&O。
SCOPE = ScopeSettings(frozenset({MISQ_ISSN, ISSN_L}))
RULES = ExclusionRulesConfig(rules=())
QUOTA = OpenAlexQuota(
    daily_budget_usd=Decimal("1.0"),
    stop_at_ratio=Decimal("0.8"),
    default_request_cost_usd=Decimal("0.0001"),
)
[WEEKLY_RUN_SWEEP] = [entry for entry in SWEEP_REGISTRY if entry.table == "weekly_run"]
BLOCKED_EVENT = "reparse 正在執行，每週更新這次先不做，sweep 補排時接著做"
PAPER_TABLES = ("paper", "external_id", "paper_abstract", "paper_keyword")


@pytest.fixture
def lock_db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(lock_db: TestDatabase) -> Database:
    return Database(lock_db.app_url, connect_timeout=5, pool_size=2)


# --- 替身與工具 ------------------------------------------------------------------------------


class PausingStore(FakeObjectStore):
    """第一次 `get` 時通知 `entered`，等 `release` 才繼續（reparse 的 GET 在持有鎖的期間）。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.gets: list[str] = []

    def get(self, key: str) -> bytes:
        self.gets.append(key)
        self.entered.set()
        assert self.release.wait(WAIT), "等太久：主執行緒沒有放行"
        return super().get(key)


class Background:
    """在另一條執行緒跑 `target`，記下結果或例外（讓主執行緒看得到）。"""

    def __init__(self, target: Callable[[], Any]) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self._target = target
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.result = self._target()
        except BaseException as exc:
            self.error = exc

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join(WAIT)
        assert not self._thread.is_alive(), "等太久：背景執行緒還沒結束"


class _Opener:
    """`open_setup` 的替身：記下被呼叫幾次。"""

    def __init__(self, setup: WeeklyFetchSetup) -> None:
        self.setup = setup
        self.opened = 0

    @contextmanager
    def __call__(self) -> Iterator[WeeklyFetchSetup]:
        self.opened += 1
        yield self.setup


def _rows(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(sql, params).fetchall()


def _count(db: TestDatabase, table: str) -> int:
    count: int = _rows(db, f"SELECT count(*) FROM {table}")[0][0]
    return count


def _paper_tables(db: TestDatabase) -> dict[str, list[tuple[Any, ...]]]:
    return {
        "paper": _rows(
            db, "SELECT id, title, referenced_works, first_snapshot_id FROM paper ORDER BY id"
        ),
        "external_id": _rows(
            db, "SELECT paper_id, scheme, value_norm FROM external_id ORDER BY scheme, value_norm"
        ),
        "paper_abstract": _rows(
            db, "SELECT paper_id, source, content, is_current FROM paper_abstract ORDER BY id"
        ),
        "paper_keyword": _rows(
            db, "SELECT paper_id, source, key FROM paper_keyword ORDER BY paper_id, key"
        ),
    }


def _run_row(db: TestDatabase, weekly_run_id: uuid.UUID) -> tuple[Any, ...]:
    [row] = _rows(
        db,
        "SELECT status, fetch_finished_at, updated_at FROM weekly_run WHERE id = %s",
        weekly_run_id,
    )
    return row


def _backdate(db: TestDatabase, weekly_run_id: uuid.UUID) -> datetime:
    with db.connect(OWNER_ROLE) as connection:
        connection.execute(
            "UPDATE weekly_run SET updated_at = now() - interval '1 day' WHERE id = %s",
            (weekly_run_id,),
        )
    updated_at: datetime = _run_row(db, weekly_run_id)[2]
    return updated_at


def _stuck(database: Database) -> list[uuid.UUID]:
    """sweep 現在會補排的 `weekly_run`（`fetching` 而且 `updated_at` 早於 2 小時前）。"""
    with database.transaction() as connection:
        return find_stuck_ids(SYSTEM, connection, WEEKLY_RUN_SWEEP, 10)


def _lock_holders(db: TestDatabase) -> int:
    """這個資料庫裡持有 `INGEST_WRITE_LOCK_KEY`（任何模式）的連線數。"""
    with connect_admin(db.name) as admin:
        row = admin.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
            " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            " AND classid::bigint = %s AND objid::bigint = %s AND objsubid = 1",
            (INGEST_WRITE_LOCK_KEY >> 32, INGEST_WRITE_LOCK_KEY & 0xFFFF_FFFF),
        ).fetchone()
    assert row is not None
    holders: int = row[0]
    return holders


def _lock_holder_pid(db: TestDatabase) -> int:
    """持有 `INGEST_WRITE_LOCK_KEY` 的那一條連線的 PID（只能有一條）。"""
    with connect_admin(db.name) as admin:
        [(pid,)] = admin.execute(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted"
            " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            " AND classid::bigint = %s AND objid::bigint = %s AND objsubid = 1",
            (INGEST_WRITE_LOCK_KEY >> 32, INGEST_WRITE_LOCK_KEY & 0xFFFF_FFFF),
        ).fetchall()
    holder: int = pid
    return holder


def _terminate(db: TestDatabase, pid: int) -> None:
    """從資料庫端切斷那條連線（等它真的結束，最多 5 秒）。"""
    with connect_admin(db.name) as admin:
        [(terminated,)] = admin.execute("SELECT pg_terminate_backend(%s, 5000)", (pid,)).fetchall()
    assert terminated


def _backends(db: TestDatabase) -> dict[int, str | None]:
    """這個資料庫裡 `radar_app` 的連線：PID → 狀態（`idle`、`idle in transaction`…）。"""
    with connect_admin(db.name) as admin:
        rows = admin.execute(
            "SELECT pid, state FROM pg_stat_activity"
            " WHERE datname = current_database() AND usename = %s",
            (APP_ROLE,),
        ).fetchall()
    return {pid: state for pid, state in rows}


def _eventually(check: Callable[[], bool]) -> bool:
    """`check` 在 `WAIT` 秒內變成真就回 True（關掉的連線在資料庫端是非同步結束的）。"""
    deadline = time.monotonic() + WAIT
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _free_for_others(db: TestDatabase) -> bool:
    """另一條連線拿得到獨佔鎖（拿到就馬上放掉）。"""
    with db.connect(APP_ROLE) as connection:
        row = connection.execute(
            "SELECT pg_try_advisory_lock(%s)", (INGEST_WRITE_LOCK_KEY,)
        ).fetchone()
        assert row is not None
        if row[0]:
            connection.execute("SELECT pg_advisory_unlock(%s)", (INGEST_WRITE_LOCK_KEY,))
        return bool(row[0])


# --- 資料 ------------------------------------------------------------------------------------


def _reparse_input(database: Database, store: FakeObjectStore) -> str:
    """reparse 會處理的一篇：I&O 錄好的一篇，原始資料已存、論文已匯入。回 OpenAlex ID。"""
    work = cassette_work()
    key = str(work["id"]).rsplit("/", 1)[-1]
    content = work_bytes(work)
    record = RawRecord(RawSource.OPENALEX, key, content, T1)
    store.objects[raw_object_key("openalex", record.sha256)] = compress_raw(content)
    store.content_types[raw_object_key("openalex", record.sha256)] = RAW_CONTENT_TYPE
    with database.transaction() as connection:
        create_raw_fetches(connection, [record])
        upsert_by_external_id(
            connection,
            parse_openalex_work(content, fetched_at=T1),
            origin=PaperOrigin.TRACKED,
            excluded_reason=None,
            scope=SCOPE,
        )
    return key


def _misq_book() -> list[list[RawRecord]]:
    """MISQ 自己造的 3 篇，分兩頁。"""
    records = []
    for number in (1, 2, 3):
        work = cassette_work()
        work["id"] = f"https://openalex.org/W8{number:08d}"
        work["doi"] = f"https://doi.org/10.9999/radar-lock.{number}"
        work["publication_year"] = 2020
        work["primary_location"]["source"]["issn_l"] = MISQ_ISSN
        work["title"] = work["display_name"] = f"Lock study {number}"
        key = str(work["id"]).rsplit("/", 1)[-1]
        records.append(RawRecord(RawSource.OPENALEX, key, work_bytes(work), T1))
    return [records[:2], records[2:]]


def _source() -> FakePaperSource:
    return FakePaperSource({MISQ_ISSN: _misq_book()}, clock=lambda: NOW)


def _reparse(database: Database, store: FakeObjectStore) -> ReparseResult:
    return reparse_papers(SYSTEM, database, store, scope=SCOPE, exclusion_rules=RULES)


def _other_database(db: TestDatabase) -> Database:
    """另一個行程的連線池（自己的連線，和 `database` fixture 不共用）。"""
    return Database(db.app_url, connect_timeout=5, pool_size=2)


def _advance(
    database: Database, opener: _Opener, weekly_run_id: uuid.UUID, heartbeat: FakeHeartbeat
) -> WeeklyAdvanceResult:
    return advance_weekly_update(
        SYSTEM, database, opener, lambda: SCOPE, heartbeat, weekly_run_id, clock=lambda: NOW
    )


def _apply(
    database: Database, weekly_run_id: uuid.UUID, heartbeat: FakeHeartbeat
) -> WeeklyApplyResult:
    return apply_weekly_update(
        SYSTEM, database, weekly_run_id, scope=SCOPE, send_heartbeat=heartbeat
    )


def _fetch_all(database: Database, weekly_run_id: uuid.UUID) -> None:
    result = run_weekly_update(
        SYSTEM,
        database,
        _source(),
        FakeObjectStore(),
        weekly_run_id,
        journals=JOURNALS,
        exclusion_rules=RULES,
        quota=QUOTA,
        clock=lambda: NOW,
    )
    assert result.fetch_finished


# --- 1. reparse 持有鎖時，每週更新不抓、不套用，之後接著做 ---------------------------------------


def test_weekly_update_waits_while_reparse_runs_then_resumes(
    lock_db: TestDatabase, database: Database
) -> None:
    store = PausingStore()
    _reparse_input(database, store)
    reparse = Background(lambda: _reparse(_other_database(lock_db), store))
    reparse.start()
    try:
        # reparse 停在 GET：已經拿到鎖、也查過沒有 fetching 的 weekly_run。
        assert store.entered.wait(WAIT)
        assert _lock_holders(lock_db) == 1

        # cron 排入的 weekly_update 照常建立那一筆並排入抓取（建立不讀寫原始資料與論文）。
        queue = FakeJobQueue()
        start = weekly_update(SYSTEM, database, queue.enqueue)
        assert start.created
        run_id = start.weekly_run_id
        assert len(queue.queued) == 1
        touched_before = _backdate(lock_db, run_id)
        assert _stuck(database) == [run_id]

        source = _source()
        opener = _Opener(WeeklyFetchSetup(source, FakeObjectStore(), JOURNALS, RULES, QUOTA))
        heartbeat = FakeHeartbeat()
        with capture_logs() as logs:
            fetched = run_weekly_update(
                SYSTEM,
                database,
                source,
                FakeObjectStore(),
                run_id,
                journals=JOURNALS,
                exclusion_rules=RULES,
                quota=QUOTA,
                clock=lambda: NOW,
            )
            advanced = _advance(database, opener, run_id, heartbeat)

        # 兩次都先不做：不抓（source 沒被呼叫、沒開 adapter）、不套用、不送心跳、不丟錯。
        blocked = WeeklyFetchResult(weekly_run_id=run_id, blocked_by_reparse=True)
        assert fetched == blocked
        assert advanced == WeeklyAdvanceResult(fetch=blocked, apply=None)
        assert source.requests == []
        assert opener.opened == 0
        assert heartbeat.sent == []
        # 沒有寫任何原始資料、暫存、進度、快照、論文（只有 reparse 那一篇）。
        assert _count(lock_db, "raw_fetch") == 1
        assert _count(lock_db, "ingest_staging") == 0
        assert _count(lock_db, "weekly_run_journal") == 0
        assert _count(lock_db, "snapshot") == 0
        assert _count(lock_db, "paper") == 1
        # 仍是 fetching、沒抓完；updated_at 更新了，sweep 2 小時後才補排（不是每 15 分鐘）。
        status, finished_at, updated_at = _run_row(lock_db, run_id)
        assert (status, finished_at) == ("fetching", None)
        assert updated_at > touched_before
        assert _stuck(database) == []
        assert [log["event"] for log in logs if log["log_level"] == "warning"] == [
            BLOCKED_EVENT,
            BLOCKED_EVENT,
        ]
        assert not [log for log in logs if log["log_level"] == "error"]
    finally:
        store.release.set()
    reparse.join()

    # reparse 照常完成，鎖放掉了。
    assert reparse.error is None
    assert reparse.result.updated == 1
    assert _lock_holders(lock_db) == 0

    # sweep 補排（或工作重試）時接著做：抓完、套用、發佈快照 1。
    heartbeat = FakeHeartbeat()
    resumed = _advance(database, opener, run_id, heartbeat)
    assert resumed.fetch.fetch_finished
    assert resumed.apply is not None
    assert (resumed.apply.snapshot_number, resumed.apply.created) == (1, 3)
    assert _run_row(lock_db, run_id)[0] == "published"
    assert heartbeat.sent == ["weekly-update"]


def test_apply_is_not_done_while_the_lock_is_held_then_resumes(
    lock_db: TestDatabase, database: Database
) -> None:
    # 真的 reparse 走不到這個情況（有 fetching 的 weekly_run 時它拿到鎖也會拒絕），這裡用同一個
    # 方法直接拿著鎖，證明套用那一道檢查本身也擋得住。
    run_id = weekly_update(SYSTEM, database, FakeJobQueue().enqueue).weekly_run_id
    _fetch_all(database, run_id)
    staged = _count(lock_db, "ingest_staging")
    assert staged == 3
    touched_before = _backdate(lock_db, run_id)
    opener = _Opener(WeeklyFetchSetup(_source(), FakeObjectStore(), JOURNALS, RULES, QUOTA))
    heartbeat = FakeHeartbeat()

    holder = Database(lock_db.app_url, connect_timeout=5, pool_size=1)
    with holder.session_lock(INGEST_WRITE_LOCK_KEY) as acquired:
        assert acquired
        with capture_logs() as logs:
            applied = _apply(database, run_id, heartbeat)
            advanced = _advance(database, opener, run_id, heartbeat)

        assert applied == WeeklyApplyResult(weekly_run_id=run_id, blocked_by_reparse=True)
        assert advanced == WeeklyAdvanceResult(
            fetch=WeeklyFetchResult(weekly_run_id=run_id, blocked_by_reparse=True), apply=None
        )
        assert _count(lock_db, "snapshot") == 0
        assert all(_count(lock_db, table) == 0 for table in PAPER_TABLES)
        assert _count(lock_db, "ingest_staging") == staged
        status, finished_at, updated_at = _run_row(lock_db, run_id)
        assert (status, finished_at is not None) == ("fetching", True)
        assert updated_at > touched_before
        assert heartbeat.sent == []
        assert opener.opened == 0
        assert [log["event"] for log in logs if log["log_level"] == "warning"] == [
            BLOCKED_EVENT,
            BLOCKED_EVENT,
        ]
        assert not [log for log in logs if log["log_level"] == "error"]

    # 鎖放掉之後接著套用：快照 1、和一次就成功的結果相同。
    result = _apply(database, run_id, heartbeat)
    assert (result.snapshot_number, result.staged, result.created) == (1, staged, staged)
    assert _run_row(lock_db, run_id)[0] == "published"
    assert heartbeat.sent == ["weekly-update"]


# --- 2. 反向順序：每週更新的套用拿著共用鎖時，reparse 拒絕 ----------------------------------------


def test_reparse_is_refused_while_a_weekly_apply_is_running(
    lock_db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeObjectStore()
    _reparse_input(database, store)
    run_id = weekly_update(SYSTEM, database, FakeJobQueue().enqueue).weekly_run_id
    _fetch_all(database, run_id)
    entered = threading.Event()
    release = threading.Event()

    def slow(connection: Connection, snapshot_id: uuid.UUID) -> int:
        entered.set()
        assert release.wait(WAIT)
        return update_papers_first_snapshot(connection, snapshot_id)

    monkeypatch.setattr(weekly_update_module, "update_papers_first_snapshot", slow)
    before = _paper_tables(lock_db)
    apply = Background(lambda: _apply(_other_database(lock_db), run_id, FakeHeartbeat()))
    apply.start()
    try:
        # 套用停在交易中段：論文已經寫了（還沒 commit），共用鎖拿著。
        assert entered.wait(WAIT)
        reparse_store = PausingStore()
        reparse_store.objects.update(store.objects)
        reparse_store.release.set()
        with pytest.raises(ReparseBusyError) as caught:
            _reparse(database, reparse_store)
        assert "等它結束後再跑 reparse" in str(caught.value)
        # 什麼都沒讀、沒寫。
        assert reparse_store.gets == []
        assert _paper_tables(lock_db) == before
        assert _rows(lock_db, "SELECT count(*) FROM ingest_staging WHERE kind = 'reparse'") == [
            (0,)
        ]
    finally:
        release.set()
    apply.join()

    assert apply.error is None
    assert apply.result.snapshot_number == 1
    # 套用結束之後 reparse 就能跑。這一週 3 篇的原始內容存在抓取用的另一個替身裡，這裡讀不到、
    # 略過；更新的是 reparse 那一篇。
    after = _reparse(database, store)
    assert (after.raw_keys, after.missing_object, after.updated) == (4, 3, 1)


# --- 3. 兩個 reparse 同時 -----------------------------------------------------------------------


def test_second_reparse_is_refused_while_the_first_runs(
    lock_db: TestDatabase, database: Database
) -> None:
    store = PausingStore()
    _reparse_input(database, store)
    before = _paper_tables(lock_db)
    first = Background(lambda: _reparse(_other_database(lock_db), store))
    first.start()
    try:
        assert store.entered.wait(WAIT)
        second_store = FakeObjectStore()
        second_store.objects.update(store.objects)
        with capture_logs() as logs, pytest.raises(ReparseBusyError):
            _reparse(database, second_store)
        # 第二個什麼都沒做：沒清暫存、沒寫論文；第一個還拿著鎖。
        assert _paper_tables(lock_db) == before
        assert _lock_holders(lock_db) == 1
        assert not [log for log in logs if log["event"] == "重新解析結束"]
    finally:
        store.release.set()
    first.join()

    assert first.error is None
    assert first.result == ReparseResult(raw_keys=1, staged=1, updated=1)
    assert len(store.gets) == 1
    assert _count(lock_db, "ingest_staging") == 0
    assert _lock_holders(lock_db) == 0


# --- 4. 結束後鎖一定放掉 ----------------------------------------------------------------------


class _UpsertBrokenError(Exception):
    """模擬 reparse 套用中途失敗。"""


def test_lock_is_released_after_success_failure_and_refusal(
    lock_db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeObjectStore()
    _reparse_input(database, store)

    # 成功：連線還在連線池裡，但已經不持有鎖。
    assert _reparse(database, store).updated == 1
    assert _lock_holders(lock_db) == 0
    assert _free_for_others(lock_db)

    # 中途失敗（例外往上丟）。
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise _UpsertBrokenError

    with monkeypatch.context() as patch:
        patch.setattr(reparse_module, "upsert_by_external_id", broken)
        with pytest.raises(_UpsertBrokenError):
            _reparse(database, store)
    assert _lock_holders(lock_db) == 0
    assert _free_for_others(lock_db)

    # 拿到鎖後發現每週更新正在抓取而拒絕。
    with database.transaction() as connection:
        assert create_weekly_run(connection) is not None
    with pytest.raises(WeeklyUpdateInProgressError):
        _reparse(database, store)
    assert _lock_holders(lock_db) == 0
    assert _free_for_others(lock_db)


def test_session_lock_discards_the_connection_when_unlock_fails(lock_db: TestDatabase) -> None:
    holder = Database(lock_db.app_url, connect_timeout=5, pool_size=1)
    with capture_logs() as logs, holder.session_lock(INGEST_WRITE_LOCK_KEY) as acquired:
        assert acquired
        # 持有鎖的那條連線被資料庫端切斷：離開區塊時解鎖會失敗。
        with connect_admin(lock_db.name) as admin:
            [(pid,)] = admin.execute(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted"
                " AND database = (SELECT oid FROM pg_database"
                " WHERE datname = current_database()) AND objid::bigint = %s",
                (INGEST_WRITE_LOCK_KEY & 0xFFFF_FFFF,),
            ).fetchall()
            [(terminated,)] = admin.execute(
                "SELECT pg_terminate_backend(%s, 5000)", (pid,)
            ).fetchall()
            assert terminated

    # 不丟錯，只記一筆警告（只帶錯誤類別）；連線結束，鎖跟著放掉。
    [warning] = [log for log in logs if log["log_level"] == "warning"]
    assert warning["event"] == "advisory lock 解鎖失敗，關掉這條連線"
    assert set(warning) == {"event", "log_level", "error_type"}
    assert _lock_holders(lock_db) == 0
    # 壞掉的連線沒有放回池子（池子只有 1 條）：再借一次拿到的是新連線，鎖拿得到、交易開得了。
    with holder.session_lock(INGEST_WRITE_LOCK_KEY) as again:
        assert again
    with holder.transaction() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
    assert _lock_holders(lock_db) == 0


def test_session_lock_discards_a_live_connection_when_unlock_raises(lock_db: TestDatabase) -> None:
    # 連線還活著、解鎖丟出一般的資料庫錯誤（不是斷線）：程式要自己把連線作廢，否則它帶著鎖
    # 回到連線池，別人再也拿不到。用一個同名函式讓 pg_advisory_unlock 在資料庫端真的丟錯：
    # 持鎖的連線把這個 schema 排在 pg_catalog 前面，解鎖那一句就會呼叫到它。
    with connect_admin(lock_db.name) as admin:
        admin.execute("CREATE SCHEMA unlock_trap")
        admin.execute(
            "CREATE FUNCTION unlock_trap.pg_advisory_unlock(bigint) RETURNS boolean"
            " LANGUAGE plpgsql AS $$ BEGIN"
            " RAISE EXCEPTION 'unlock trap' USING ERRCODE = 'data_exception'; END $$"
        )
        admin.execute(f"GRANT USAGE ON SCHEMA unlock_trap TO {APP_ROLE}")
    holder = Database(lock_db.app_url, connect_timeout=5, pool_size=1)
    with capture_logs() as logs, holder.session_lock(INGEST_WRITE_LOCK_KEY) as lock:
        assert lock is not None
        pid = _lock_holder_pid(lock_db)
        with lock.transaction() as connection:
            connection.exec_driver_sql("SET search_path TO unlock_trap, pg_catalog, public")
        # 離開區塊前這條連線還活著、沒有開著的交易。
        assert _backends(lock_db)[pid] == "idle"

    # 解鎖丟的是資料庫回的一般錯誤，不是斷線：錯誤碼 22000 換成 DataError（權限不足、找不到
    # 函式這類錯誤是 ProgrammingError），證明真的是上面那個函式丟的。
    [warning] = [log for log in logs if log["log_level"] == "warning"]
    assert warning == {
        "event": "advisory lock 解鎖失敗，關掉這條連線",
        "log_level": "warning",
        "error_type": "DataError",
    }
    # 原本那條連線被關掉（資料庫端看不到它了），鎖跟著放掉，別的連線拿得到。
    assert _eventually(lambda: pid not in _backends(lock_db))
    assert _eventually(lambda: _lock_holders(lock_db) == 0)
    assert _free_for_others(lock_db)
    # 池子只有 1 條：再借一次是新的連線（不是帶著 search_path 的那條），鎖拿得到、也解得開。
    with holder.session_lock(INGEST_WRITE_LOCK_KEY) as again:
        assert again is not None
        assert _lock_holder_pid(lock_db) != pid
    assert _lock_holders(lock_db) == 0


def test_session_lock_transaction_checks_the_lock_and_never_reconnects(
    lock_db: TestDatabase,
) -> None:
    holder = Database(lock_db.app_url, connect_timeout=5, pool_size=1)

    # 連線還在、鎖卻不在這條連線上了：交易第一句就發現，區塊不會執行。
    with capture_logs() as logs, holder.session_lock(INGEST_WRITE_LOCK_KEY) as lock:
        assert lock is not None
        with lock.transaction() as connection:
            unlocked = connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": INGEST_WRITE_LOCK_KEY}
            ).scalar_one()
            assert unlocked
        with (
            pytest.raises(AdvisoryLockLostError, match="鎖已經不在持有它的連線上"),
            lock.transaction(),
        ):
            pytest.fail("鎖不在了，不該進到交易裡")
    assert [log["event"] for log in logs if log["log_level"] == "warning"] == [
        "advisory lock 已經不在這條連線上，關掉這條連線"
    ]
    assert _lock_holders(lock_db) == 0

    # 持鎖的連線被資料庫端切斷：下一個交易丟錯；之後再開也直接拒絕，不會偷偷重連
    # （資料庫端看不到這個 Database 的任何新連線）。
    with capture_logs() as logs, holder.session_lock(INGEST_WRITE_LOCK_KEY) as lock:
        assert lock is not None
        _terminate(lock_db, _lock_holder_pid(lock_db))
        with pytest.raises(AdvisoryLockLostError, match="持有鎖的連線斷了"), lock.transaction():
            pytest.fail("連線斷了，不該進到交易裡")
        with pytest.raises(AdvisoryLockLostError, match="持有鎖的連線已經斷了"), lock.transaction():
            pytest.fail("連線斷了，不該進到交易裡")
        assert _backends(lock_db) == {}
    assert [log["event"] for log in logs if log["log_level"] == "warning"] == [
        "持有 advisory lock 的連線已經斷了，鎖跟著連線放掉"
    ]
    assert _backends(lock_db) == {}
    assert _lock_holders(lock_db) == 0
    # 壞掉的連線沒有放回池子：再借一次是新的連線，鎖拿得到。
    with holder.session_lock(INGEST_WRITE_LOCK_KEY) as again:
        assert again is not None
    assert _lock_holders(lock_db) == 0


# --- 5. 持鎖的連線中途斷了 ---------------------------------------------------------------------


def test_reparse_that_lost_its_lock_connection_commits_nothing(
    lock_db: TestDatabase, database: Database
) -> None:
    store = PausingStore()
    _reparse_input(database, store)
    before = _paper_tables(lock_db)
    original = Background(lambda: _reparse(_other_database(lock_db), store))
    second_store = PausingStore()
    second_store.objects.update(store.objects)
    second = Background(lambda: _reparse(_other_database(lock_db), second_store))
    original.start()
    try:
        # 原本的 reparse 停在 GET：拿著鎖，持鎖的連線這時沒有開著的交易。
        assert store.entered.wait(WAIT)
        pid = _lock_holder_pid(lock_db)
        assert _backends(lock_db)[pid] == "idle"

        # 持鎖的連線被切斷（例如資料庫重啟）：鎖跟著消失。
        _terminate(lock_db, pid)
        assert _lock_holders(lock_db) == 0

        # 另一個 reparse 開始、拿到鎖，停在它自己的 GET。
        second.start()
        assert second_store.entered.wait(WAIT)
        assert _lock_holder_pid(lock_db) != pid

        # 放行原本的：它的下一個交易（寫暫存列）發現鎖不在了，整次作業中止。
        with capture_logs() as logs:
            store.release.set()
            original.join()
        assert isinstance(original.error, AdvisoryLockLostError)
        assert "持有鎖的連線斷了" in str(original.error)
        # 什麼都沒 commit：沒有暫存列、論文不變；第二個還拿著鎖。
        assert _count(lock_db, "ingest_staging") == 0
        assert _paper_tables(lock_db) == before
        assert _lock_holders(lock_db) == 1
        events = [log["event"] for log in logs]
        assert "持有 advisory lock 的連線已經斷了，鎖跟著連線放掉" in events
        assert "重新解析結束" not in events
    finally:
        store.release.set()
        second_store.release.set()
    second.join()

    # 第二個照常完成，鎖放掉。
    assert second.error is None
    assert second.result == ReparseResult(raw_keys=1, staged=1, updated=1)
    assert _count(lock_db, "ingest_staging") == 0
    assert _lock_holders(lock_db) == 0


def test_session_lock_is_not_acquired_twice(lock_db: TestDatabase) -> None:
    first = Database(lock_db.app_url, connect_timeout=5, pool_size=1)
    second = Database(lock_db.app_url, connect_timeout=5, pool_size=1)
    with first.session_lock(INGEST_WRITE_LOCK_KEY) as acquired:
        assert acquired
        with second.session_lock(INGEST_WRITE_LOCK_KEY) as also:
            assert not also
        # 共用的交易級鎖也拿不到；交易結束不影響獨佔鎖。
        with second.transaction() as connection:
            assert not Database.try_shared_transaction_lock(connection, INGEST_WRITE_LOCK_KEY)
        assert _lock_holders(lock_db) == 1
    assert _lock_holders(lock_db) == 0
    # 兩個共用鎖可以同時拿。
    with first.transaction() as one, second.transaction() as two:
        assert Database.try_shared_transaction_lock(one, INGEST_WRITE_LOCK_KEY)
        assert Database.try_shared_transaction_lock(two, INGEST_WRITE_LOCK_KEY)
        assert _lock_holders(lock_db) == 2
    assert _lock_holders(lock_db) == 0
