"""`services.weekly_update` 對真的 PostgreSQL＋替身（`FakePaperSource`、`FakeObjectStore`、
`FakeJobQueue`）。

第一次跑與已有 `fetching`、抓完一本（錄好的 I&O 三頁）、收錄過濾、排除規則、解析失敗、中斷續跑、
cursor 繞圈、額度停下、抓完再跑、從頭重抓、不是 `fetching`、身分不對；以及論文相關表筆數不變、
只寫允許的表、外部呼叫時沒有開著的交易、部分唯一索引、新增一本期刊只抓那一本、repo 的寫入規則。

OpenAlex 內容只用錄好的回應（`tests/openalex_cassette.py`，CC0）與這裡自己造的假資料；每個測試
一個全新、已遷移的資料庫，用應用程式角色（`radar_app`）連線。
"""

import re
import uuid
from collections import Counter
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import Connection, event, text
from structlog.testing import capture_logs

from radar.adapters.fake import FakeObjectStore, FakePaperSource
from radar.adapters.fake.jobs import FakeJobQueue
from radar.adapters.object_store import ObjectStoreUnavailableError
from radar.domain.domain_config import (
    ExclusionRule,
    ExclusionRulesConfig,
    Journal,
    JournalsConfig,
    OpenAlexQuota,
)
from radar.domain.jobs import RUN_WEEKLY_UPDATE
from radar.domain.permissions import PermissionDeniedError
from radar.domain.raw_records import ApiCall, JournalQuery, RawRecord, RawSource, SourcePage
from radar.domain.staging import StagingItem, StagingKind
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.domain.weekly_run import JournalProgress, PageCounts
from radar.repo.api_calls import create_api_calls
from radar.repo.db import Database
from radar.repo.ingest_staging import upsert_staged_papers
from radar.repo.weekly_runs import (
    create_weekly_run,
    find_fetching_weekly_run,
    get_weekly_run,
    list_weekly_run_journals,
    update_weekly_run_failed,
    update_weekly_run_failure,
    update_weekly_run_published,
    update_weekly_run_touched_while_fetching,
    upsert_weekly_run_journal_page,
)
from radar.services import weekly_update as weekly_update_module
from radar.services.ingest import JournalFetchError
from radar.services.weekly_update import (
    WeeklyFetchResult,
    WeeklyFetchSetup,
    open_and_run_weekly_update,
    run_weekly_update,
    weekly_update,
)
from tests.database import APP_ROLE, OWNER_ROLE, TestDatabase, migrate
from tests.openalex_cassette import ISSN_L, cassette_pages, cassette_work, work_bytes

NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
T2 = T1 + timedelta(minutes=30)
SYSTEM = SystemViewer()
MEMBER = MemberViewer(member_id=uuid.UUID("018f0000-0000-7000-8000-0000000000a1"), is_admin=True)
IO = Journal(key="io", name="Information and Organization", issn_l=ISSN_L)
MISQ_ISSN = "0276-7783"
MISQ = Journal(key="misq", name="MIS Quarterly", issn_l=MISQ_ISSN)
PMJ_ISSN = "1938-9507"
PMJ = Journal(key="pmj", name="Project Management Journal", issn_l=PMJ_ISSN)
QUOTA = OpenAlexQuota(
    daily_budget_usd=Decimal("1.0"),
    stop_at_ratio=Decimal("0.8"),
    default_request_cost_usd=Decimal("0.0001"),
)
RULES = ExclusionRulesConfig(
    rules=(ExclusionRule(reason="editors_comments", title_starts_with=("Editor's Comments",)),)
)
PAPER_TABLES = ("paper", "external_id", "paper_abstract", "paper_keyword")
ALLOWED_WRITES = {"raw_fetch", "api_call", "ingest_staging", "weekly_run", "weekly_run_journal"}
_WRITE_SQL = re.compile(
    r"\b(?:INSERT\s+INTO|DELETE\s+FROM|MERGE\s+INTO)\s+\"?(\w+)\"?"
    r"|\bUPDATE\s+\"?(\w+)\"?(?:\s+(?:AS\s+)?\w+)?\s+SET\b"
    r"|\bTRUNCATE\s+(?:TABLE\s+)?\"?(\w+)\"?",
    re.IGNORECASE,
)


class TrackingDatabase(Database):
    """記下開著的交易數、開過幾個交易，以及執行過的每一句 SQL。"""

    def __init__(self, url: str) -> None:
        super().__init__(url, connect_timeout=5, pool_size=2)
        self.open_transactions = 0
        self.opened = 0
        self.statements: list[str] = []
        event.listen(self._engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        self.statements.append(statement)

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        self.opened += 1
        self.open_transactions += 1
        try:
            with super().transaction() as connection:
                yield connection
        finally:
            self.open_transactions -= 1

    def written_tables(self) -> set[str]:
        written: set[str] = set()
        for statement in self.statements:
            for match in _WRITE_SQL.finditer(statement):
                written.add(next(group for group in match.groups() if group).lower())
        return written


class GuardedSource(FakePaperSource):
    """每次被呼叫時記下當下開著的交易數。"""

    def __init__(self, database: TrackingDatabase, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.database = database
        self.open_during_calls: list[int] = []

    def fetch_journal_page(self, query: JournalQuery, cursor: str | None) -> SourcePage:
        self.open_during_calls.append(self.database.open_transactions)
        return super().fetch_journal_page(query, cursor)


class GuardedStore(FakeObjectStore):
    def __init__(self, database: TrackingDatabase) -> None:
        super().__init__()
        self.database = database
        self.open_during_calls: list[int] = []

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self.open_during_calls.append(self.database.open_transactions)
        super().put(key, data, content_type=content_type)

    def get(self, key: str) -> bytes:
        self.open_during_calls.append(self.database.open_transactions)
        return super().get(key)


class RecordingQueue(FakeJobQueue):
    """排入時記下當下開著的交易數（要在交易結束後才排入）。"""

    def __init__(self, database: TrackingDatabase) -> None:
        super().__init__()
        self.database = database
        self.open_during_enqueue: list[int] = []

    def enqueue(self, job: Any, ids: Any) -> bool:
        self.open_during_enqueue.append(self.database.open_transactions)
        return super().enqueue(job, ids)


@pytest.fixture
def weekly_db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(weekly_db: TestDatabase) -> TrackingDatabase:
    return TrackingDatabase(weekly_db.app_url)


# --- 資料 ---------------------------------------------------------------------------------


def _key(work: dict[str, Any]) -> str:
    return str(work["id"]).rsplit("/", 1)[-1]


def _records(
    works: list[dict[str, Any]] | tuple[dict[str, Any], ...], at: datetime = T1
) -> list[RawRecord]:
    return [RawRecord(RawSource.OPENALEX, _key(work), work_bytes(work), at) for work in works]


def _cassette_book(at: datetime = T1) -> list[list[RawRecord]]:
    """I&O 錄好的三頁（100＋100＋48 篇）。"""
    return [_records(page, at) for page in cassette_pages()]


def _work(
    number: int,
    *,
    issn_l: str = MISQ_ISSN,
    year: int | None = 2020,
    work_type: str = "article",
    doi: bool = True,
    title: str | None = None,
) -> dict[str, Any]:
    """自己造的一篇（以錄好的一篇為底，換掉 ID、DOI、類型、出版年、期刊、標題）。"""
    work = cassette_work()
    work["id"] = f"https://openalex.org/W9{number:08d}"
    work["doi"] = f"https://doi.org/10.9999/radar-test.{number}" if doi else None
    work["type"] = work_type
    work["publication_year"] = year
    work["primary_location"]["source"]["issn_l"] = issn_l
    work["title"] = work["display_name"] = title or f"Synthetic study {number}"
    return work


def _misq_book(at: datetime = T1) -> list[list[RawRecord]]:
    """MISQ 兩頁自己造的資料：5 篇，全部收錄。"""
    return [_records([_work(1), _work(2), _work(3)], at), _records([_work(4), _work(5)], at)]


def _journals(*journals: Journal) -> JournalsConfig:
    return JournalsConfig(start_year=2016, journals=journals)


def _start(database: Database, queue: FakeJobQueue | None = None) -> uuid.UUID:
    return weekly_update(SYSTEM, database, (queue or FakeJobQueue()).enqueue).weekly_run_id


def _run(
    database: Database,
    source: FakePaperSource,
    store: FakeObjectStore,
    weekly_run_id: uuid.UUID,
    *,
    journals: JournalsConfig,
    rules: ExclusionRulesConfig = RULES,
    quota: OpenAlexQuota = QUOTA,
    actor: Any = SYSTEM,
) -> WeeklyFetchResult:
    return run_weekly_update(
        actor,
        database,
        source,
        store,
        weekly_run_id,
        journals=journals,
        exclusion_rules=rules,
        quota=quota,
        clock=lambda: NOW,
    )


def _rows(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(sql, params).fetchall()


def _owner(db: TestDatabase, sql: str, *params: object) -> None:
    with db.connect(OWNER_ROLE) as connection:
        connection.execute(sql, params)


def _staged(db: TestDatabase, weekly_run_id: uuid.UUID) -> dict[str, tuple[str, str | None]]:
    """這一批暫存列：外部鍵 →（source_hash, excluded_reason）。"""
    rows = _rows(
        db,
        "SELECT external_key, source_hash, excluded_reason, kind FROM ingest_staging"
        " WHERE batch_id = %s",
        weekly_run_id,
    )
    assert {row[3] for row in rows} <= {"weekly_update"}
    return {row[0]: (row[1], row[2]) for row in rows}


def _progress(database: Database, weekly_run_id: uuid.UUID) -> dict[str, JournalProgress]:
    with database.transaction() as connection:
        return list_weekly_run_journals(SYSTEM, connection, weekly_run_id)


def _paper_counts(db: TestDatabase) -> dict[str, int]:
    return {table: _rows(db, f"SELECT count(*) FROM {table}")[0][0] for table in PAPER_TABLES}


def _cassette_years() -> dict[int, int]:
    return dict(Counter(work["publication_year"] for page in cassette_pages() for work in page))


def _run_row(db: TestDatabase, weekly_run_id: uuid.UUID) -> tuple[Any, ...]:
    [row] = _rows(
        db,
        "SELECT status, fetch_finished_at, updated_at FROM weekly_run WHERE id = %s",
        weekly_run_id,
    )
    return row


# --- weekly_update：寫狀態列、排入 ---------------------------------------------------------


def test_first_run_creates_fetching_row_then_enqueues(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    queue = RecordingQueue(database)

    with capture_logs() as logs:
        start = weekly_update(SYSTEM, database, queue.enqueue)

    assert start.created
    assert _rows(weekly_db, "SELECT id, status, fetch_finished_at FROM weekly_run") == [
        (start.weekly_run_id, "fetching", None)
    ]
    [queued] = queue.queued
    assert queued.job is RUN_WEEKLY_UPDATE
    assert queued.ids == {"weekly_run_id": start.weekly_run_id}
    # 交易結束後才排入（AD-14）。
    assert queue.open_during_enqueue == [0]
    [entry] = [log for log in logs if log["event"] == "每週更新已排入抓取"]
    assert (entry["weekly_run_id"], entry["created"], entry["queued"]) == (
        str(start.weekly_run_id),
        True,
        True,
    )


def test_existing_fetching_row_is_reused_and_not_queued_twice(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    queue = FakeJobQueue()
    first = weekly_update(SYSTEM, database, queue.enqueue)

    second = weekly_update(SYSTEM, database, queue.enqueue)

    assert not second.created
    assert second.weekly_run_id == first.weekly_run_id
    assert len(_rows(weekly_db, "SELECT id FROM weekly_run")) == 1
    # 同一個 weekly_run_id 已經在排隊：不重複排（排隊鎖），也不算失敗。
    assert len(queue.queued) == 1


def test_finished_row_does_not_block_a_new_run(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    first = _start(database)
    _owner(weekly_db, "UPDATE weekly_run SET status = 'published' WHERE id = %s", first)

    second = weekly_update(SYSTEM, database, FakeJobQueue().enqueue)

    assert second.created
    assert second.weekly_run_id != first
    assert sorted(row[0] for row in _rows(weekly_db, "SELECT status FROM weekly_run")) == [
        "fetching",
        "published",
    ]


def test_second_fetching_row_is_blocked_by_the_partial_unique_index(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    _start(database)

    with (
        weekly_db.connect(APP_ROLE) as connection,
        pytest.raises(psycopg.errors.UniqueViolation, match="uq_weekly_run_fetching"),
    ):
        connection.execute("INSERT INTO weekly_run (status) VALUES ('fetching')")

    # 其他狀態不受限制。
    _owner(weekly_db, "INSERT INTO weekly_run (status) VALUES ('published'), ('published')")
    with database.transaction() as connection:
        assert create_weekly_run(connection) is None


def test_weekly_run_status_check(weekly_db: TestDatabase) -> None:
    with (
        weekly_db.connect(APP_ROLE) as connection,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        connection.execute("INSERT INTO weekly_run (status) VALUES ('applying')")


def test_weekly_update_retries_once_when_the_row_vanishes_in_between(
    database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 建不了（已有 fetching）、要找的時候那一筆剛好結束：再試一次；仍然一樣就丟 RuntimeError。
    monkeypatch.setattr(weekly_update_module, "create_weekly_run", lambda _connection: None)
    monkeypatch.setattr(weekly_update_module, "find_fetching_weekly_run", lambda *_args: None)
    queue = FakeJobQueue()

    with pytest.raises(RuntimeError, match="fetching"):
        weekly_update(SYSTEM, database, queue.enqueue)

    assert database.opened == 2
    assert queue.queued == []


# --- run_weekly_update：抓完一本 -------------------------------------------------------------


def test_one_journal_from_the_cassette(weekly_db: TestDatabase, database: TrackingDatabase) -> None:
    run_id = _start(database)
    source = FakePaperSource({ISSN_L: _cassette_book()}, clock=lambda: NOW)
    papers_before = _paper_counts(weekly_db)

    with capture_logs() as logs:
        result = _run(database, source, FakeObjectStore(), run_id, journals=_journals(IO))

    assert result.fetch_finished
    assert not result.stopped_for_quota
    assert not result.skipped
    assert result.journals == ("io",)
    assert result.pages == 3
    assert (result.counts.fetched, result.counts.staged, result.counts.out_of_scope) == (
        248,
        200,
        48,
    )
    staged = _staged(weekly_db, run_id)
    assert len(staged) == 200
    articles = {
        _key(work) for page in cassette_pages() for work in page if work["type"] == "article"
    }
    assert set(staged) == articles
    [progress] = _progress(database, run_id).values()
    assert progress == JournalProgress(
        journal_key="io",
        next_cursor=None,
        finished=True,
        pages=3,
        fetched=248,
        fetched_by_year=_cassette_years(),
        staged=200,
        excluded=0,
        out_of_scope=48,
        unparseable=0,
    )
    # JSON 物件的鍵是字串（讀出時轉成整數）。
    [(by_year,)] = _rows(weekly_db, "SELECT fetched_by_year FROM weekly_run_journal")
    assert by_year == {str(year): count for year, count in _cassette_years().items()}
    status, finished_at, _updated = _run_row(weekly_db, run_id)
    assert status == "fetching"
    assert finished_at is not None
    assert _paper_counts(weekly_db) == papers_before
    assert len(_rows(weekly_db, "SELECT id FROM raw_fetch")) == 248
    assert [log for log in logs if log["event"] == "每週更新抓取完成"]


def test_only_allowed_tables_are_written_and_no_transaction_is_open_outside(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    queue = RecordingQueue(database)
    run_id = weekly_update(SYSTEM, database, queue.enqueue).weekly_run_id
    source = GuardedSource(database, {ISSN_L: _cassette_book(), MISQ_ISSN: _misq_book()})
    store = GuardedStore(database)
    source.fail_once(ISSN_L, 2)

    with pytest.raises(JournalFetchError):
        _run(database, source, store, run_id, journals=_journals(MISQ, IO))
    _run(database, source, store, run_id, journals=_journals(MISQ, IO))

    assert source.open_during_calls == [0] * len(source.open_during_calls)
    assert len(source.open_during_calls) == 2 + 1 + 3  # MISQ 兩頁、I&O 失敗一次、續跑三頁
    assert store.open_during_calls == [0] * len(store.open_during_calls)
    assert len(store.open_during_calls) == 5 + 248
    assert queue.open_during_enqueue == [0]
    assert database.open_transactions == 0
    written = database.written_tables()
    assert written <= ALLOWED_WRITES, written
    assert written == ALLOWED_WRITES
    assert all("snapshot" not in statement.lower() for statement in database.statements)
    assert all(count == 0 for count in _paper_counts(weekly_db).values())


# --- 收錄過濾、排除規則、解析失敗 ---------------------------------------------------------------


def test_collection_filter_and_exclusion_rules(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    works = [
        _work(1, doi=False),  # 沒有 DOI
        _work(2, issn_l="0960-085X"),  # 主要出處是別本
        _work(3, year=2015),  # 起始年之前
        _work(4, work_type="book-chapter"),
        _work(5, year=2027),  # 比今年晚：照收
        _work(6, title="Editor's Comments: The Next Decade"),  # 排除規則：進暫存、帶原因
        _work(7, year=None),  # 沒有出版年
        _work(8, work_type="review"),
    ]
    run_id = _start(database)
    source = FakePaperSource({MISQ_ISSN: [_records(works)]})

    result = _run(database, source, FakeObjectStore(), run_id, journals=_journals(MISQ))

    staged = _staged(weekly_db, run_id)
    assert {key: reason for key, (_hash, reason) in staged.items()} == {
        _key(works[4]): None,
        _key(works[5]): "editors_comments",
        _key(works[7]): None,
    }
    [progress] = _progress(database, run_id).values()
    assert (progress.fetched, progress.staged, progress.excluded) == (8, 3, 1)
    assert (progress.out_of_scope, progress.unparseable) == (5, 0)
    # 過濾前的每年篇數：沒有出版年的不分年。
    assert progress.fetched_by_year == {2015: 1, 2020: 5, 2027: 1}
    assert result.counts.excluded == 1


def test_unparseable_records_are_counted_and_logged_with_key_and_reason_only(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    no_title = _work(1)
    no_title["title"] = no_title["display_name"] = None
    other_id = _work(2)
    good = _work(3)
    records = [
        RawRecord(RawSource.OPENALEX, _key(no_title), work_bytes(no_title), T1),
        # 內容的 id 是 W900000002，外部鍵卻是 W900000099。
        RawRecord(RawSource.OPENALEX, "W900000099", work_bytes(other_id), T1),
        RawRecord(RawSource.OPENALEX, _key(good), work_bytes(good), T1),
    ]
    run_id = _start(database)
    source = FakePaperSource({MISQ_ISSN: [records]})

    with capture_logs() as logs:
        result = _run(database, source, FakeObjectStore(), run_id, journals=_journals(MISQ))

    assert result.fetch_finished
    assert set(_staged(weekly_db, run_id)) == {_key(good)}
    [progress] = _progress(database, run_id).values()
    assert (progress.fetched, progress.unparseable, progress.staged) == (3, 2, 1)
    # 解析失敗的不分年。
    assert progress.fetched_by_year == {2020: 1}
    warnings = [log for log in logs if log["event"] == "每週更新略過一篇（解析不了）"]
    assert sorted((log["external_key"], log["reason"]) for log in warnings) == [
        (_key(no_title), "parse_error"),
        ("W900000099", "id_mismatch"),
    ]
    for log in warnings:
        assert set(log) == {"event", "log_level", "external_key", "reason"}


# --- 中斷續跑、cursor 繞圈 -------------------------------------------------------------------


def test_interrupted_run_resumes_where_it_stopped(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    source = FakePaperSource({MISQ_ISSN: _misq_book(), ISSN_L: _cassette_book()})
    source.fail_once(ISSN_L, 2)
    journals = _journals(MISQ, IO)

    with capture_logs() as logs, pytest.raises(JournalFetchError) as caught:
        _run(database, source, FakeObjectStore(), run_id, journals=journals)

    io_page_2 = source.requests[-1].cursor
    assert io_page_2 is not None
    assert (caught.value.journal_key, caught.value.resume_cursor) == ("io", io_page_2)
    [error] = [log for log in logs if log["log_level"] == "error"]
    assert (error["journal"], error["weekly_run_id"]) == ("io", str(run_id))
    assert io_page_2 not in str(logs)
    progress = _progress(database, run_id)
    assert progress["misq"].finished
    assert (progress["io"].finished, progress["io"].next_cursor, progress["io"].pages) == (
        False,
        io_page_2,
        1,
    )
    assert _run_row(weekly_db, run_id)[1] is None

    source.requests.clear()
    result = _run(database, source, FakeObjectStore(), run_id, journals=journals)

    # 只從第 2 本第 2 頁開始送；第 1 本、第 2 本第 1 頁不重抓。
    assert source.requests[0].cursor == io_page_2
    assert {request.query.issn_l for request in source.requests} == {ISSN_L}
    assert len(source.requests) == 2
    assert result.journals == ("io",)
    assert result.fetch_finished
    # 最後的暫存與進度和一次跑完的一樣：計數不重複加。
    expected_keys = {
        _key(work) for page in cassette_pages() for work in page if work["type"] == "article"
    }
    expected_keys |= {_key(_work(n)) for n in range(1, 6)}
    assert set(_staged(weekly_db, run_id)) == expected_keys
    progress = _progress(database, run_id)
    assert (progress["io"].pages, progress["io"].fetched, progress["io"].staged) == (3, 248, 200)
    assert progress["io"].fetched_by_year == _cassette_years()
    assert (progress["misq"].pages, progress["misq"].fetched) == (2, 5)
    assert _run_row(weekly_db, run_id)[1] is not None


def test_progress_write_failure_rolls_back_the_whole_page_with_its_staging(
    weekly_db: TestDatabase, database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex 補審 C3：這一頁的暫存已寫入、同一交易裡的進度寫入失敗時，整頁一起退回。
    journals = _journals(MISQ, IO)

    def book() -> FakePaperSource:
        return FakePaperSource({MISQ_ISSN: _misq_book(), ISSN_L: _cassette_book()})

    # 先一次跑完當作對照（同樣的資料、另一筆每週更新）。
    reference_run = _start(database)
    _run(database, book(), FakeObjectStore(), reference_run, journals=journals)
    expected_staged = _staged(weekly_db, reference_run)
    expected_progress = _progress(database, reference_run)
    _owner(weekly_db, "UPDATE weekly_run SET status = 'published' WHERE id = %s", reference_run)

    run_id = _start(database)
    source = book()
    real_upsert = upsert_weekly_run_journal_page
    staged_seen_in_transaction: list[int] = []

    def progress_write_fails_on_io_page_2(
        connection: Connection, weekly_run_id: uuid.UUID, journal_key: str, **kwargs: Any
    ) -> None:
        if journal_key != "io" or kwargs["first_page"] or staged_seen_in_transaction:
            real_upsert(connection, weekly_run_id, journal_key, **kwargs)
            return
        # 同一個交易裡看得到這一頁剛寫的暫存列；進度也真的寫了，然後才失敗。
        count = connection.execute(
            text("SELECT count(*) FROM ingest_staging WHERE batch_id = :id"),
            {"id": weekly_run_id},
        ).scalar_one()
        staged_seen_in_transaction.append(count)
        real_upsert(connection, weekly_run_id, journal_key, **kwargs)
        raise psycopg.OperationalError("connection to server at 10.9.8.7 failed: secret-host")

    monkeypatch.setattr(
        weekly_update_module, "upsert_weekly_run_journal_page", progress_write_fails_on_io_page_2
    )

    with pytest.raises(psycopg.OperationalError):
        _run(database, source, FakeObjectStore(), run_id, journals=journals)

    io_page_2 = source.requests[-1].cursor
    assert io_page_2 is not None
    io_page_1_articles = {_key(work) for work in cassette_pages()[0] if work["type"] == "article"}
    staged_before_page_2 = {_key(_work(n)) for n in range(1, 6)} | io_page_1_articles
    # 交易裡寫過第 2 頁的暫存，但退回後只剩第 2 頁之前的。
    [seen] = staged_seen_in_transaction
    assert seen > len(staged_before_page_2)
    assert set(_staged(weekly_db, run_id)) == staged_before_page_2
    progress = _progress(database, run_id)
    assert progress["misq"] == expected_progress["misq"]
    io = progress["io"]
    assert (io.finished, io.next_cursor, io.pages, io.fetched) == (False, io_page_2, 1, 100)
    assert io.staged == len(io_page_1_articles)
    assert sum(io.fetched_by_year.values()) == 100

    # 解除故障後續跑：從第 2 頁接著抓，最後的暫存內容與計數等同一次跑完。
    monkeypatch.setattr(weekly_update_module, "upsert_weekly_run_journal_page", real_upsert)
    source.requests.clear()
    result = _run(database, source, FakeObjectStore(), run_id, journals=journals)

    assert source.requests[0].cursor == io_page_2
    assert result.fetch_finished
    assert _staged(weekly_db, run_id) == expected_staged
    assert _progress(database, run_id) == expected_progress


def test_cursor_cycle_keeps_progress_at_the_bad_page(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # * → A → B → A：B 那一頁的原始資料存了，暫存沒寫，進度停在 B。
    pages = [_records([_work(1)]), _records([_work(2)]), _records([_work(3)])]
    source = FakePaperSource({MISQ_ISSN: pages}, cost_usd=Decimal(0))
    source.point_next_cursor(MISQ_ISSN, 3, 2)
    run_id = _start(database)

    with pytest.raises(JournalFetchError, match="繞回") as caught:
        _run(database, source, FakeObjectStore(), run_id, journals=_journals(MISQ))

    cursor_b = source.requests[2].cursor
    assert caught.value.resume_cursor == cursor_b
    assert cursor_b is not None
    assert cursor_b not in str(caught.value)
    [progress] = _progress(database, run_id).values()
    assert (progress.next_cursor, progress.finished, progress.pages) == (cursor_b, False, 2)
    assert set(_staged(weekly_db, run_id)) == {_key(_work(1)), _key(_work(2))}
    assert len(_rows(weekly_db, "SELECT id FROM raw_fetch")) == 3


# --- 額度、抓完再跑、從頭重抓 ---------------------------------------------------------------


def _seed_usage(database: Database, cost: str) -> None:
    call = ApiCall(
        source=RawSource.OPENALEX,
        operation="list_works",
        http_status=200,
        cost_usd=Decimal(cost),
        remaining_usd=None,
        limit_usd=None,
        called_at=NOW - timedelta(minutes=1),
    )
    with database.transaction() as connection:
        create_api_calls(connection, [call])


def test_quota_stop_is_not_a_failure_and_touches_the_run(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    _owner(
        weekly_db,
        "UPDATE weekly_run SET updated_at = now() - interval '1 day' WHERE id = %s",
        run_id,
    )
    before = _run_row(weekly_db, run_id)[2]
    _seed_usage(database, "0.8")
    source = FakePaperSource({ISSN_L: _cassette_book()})

    with capture_logs() as logs:
        result = _run(database, source, FakeObjectStore(), run_id, journals=_journals(IO))

    assert result.stopped_for_quota
    assert not result.fetch_finished
    assert source.requests == []
    status, finished_at, updated_at = _run_row(weekly_db, run_id)
    assert (status, finished_at) == ("fetching", None)
    assert updated_at > before
    assert [log for log in logs if log["event"] == "每週更新因額度停下，下次續跑"]
    assert _staged(weekly_db, run_id) == {}


def _backdate(db: TestDatabase, weekly_run_id: uuid.UUID) -> datetime:
    """把 `updated_at` 改到一天前（像 sweep 會當成卡住的那樣），回傳改好的值。"""
    _owner(
        db,
        "UPDATE weekly_run SET updated_at = now() - interval '1 day' WHERE id = %s",
        weekly_run_id,
    )
    updated_at: datetime = _run_row(db, weekly_run_id)[2]
    return updated_at


def test_each_staged_page_touches_the_run(
    weekly_db: TestDatabase, database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 只看「每頁的進度交易」本身：把失敗時的那次更新拿掉，第 1 頁暫存好、第 2 頁失敗。
    run_id = _start(database)
    before = _backdate(weekly_db, run_id)
    source = FakePaperSource({ISSN_L: _cassette_book()})
    source.fail_once(ISSN_L, 2)
    monkeypatch.setattr(weekly_update_module, "_touch", lambda *_args: None)

    with pytest.raises(JournalFetchError):
        _run(database, source, FakeObjectStore(), run_id, journals=_journals(IO))

    assert _progress(database, run_id)["io"].pages == 1
    assert _run_row(weekly_db, run_id)[2] > before


def test_source_failure_touches_the_run(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # 第 1 頁就失敗（沒有任何一頁暫存）：失敗本身也更新 updated_at，sweep 照 2 小時補排。
    run_id = _start(database)
    before = _backdate(weekly_db, run_id)
    source = FakePaperSource({ISSN_L: _cassette_book()})
    source.fail_once(ISSN_L, 1)

    with pytest.raises(JournalFetchError):
        _run(database, source, FakeObjectStore(), run_id, journals=_journals(IO))

    assert _progress(database, run_id) == {}
    assert _run_row(weekly_db, run_id)[2] > before


class BrokenStore(FakeObjectStore):
    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        raise ObjectStoreUnavailableError("EndpointConnectionError")


def test_store_failure_is_logged_with_context_and_touches_the_run(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    before = _backdate(weekly_db, run_id)
    source = FakePaperSource({ISSN_L: _cassette_book()})

    with capture_logs() as logs, pytest.raises(ObjectStoreUnavailableError):
        _run(database, source, BrokenStore(), run_id, journals=_journals(IO))

    [error] = [log for log in logs if log["log_level"] == "error"]
    assert error["event"] == "每週更新抓取中斷，進度已保存"
    assert (error["weekly_run_id"], error["journal"], error["error"]) == (
        str(run_id),
        "io",
        "ObjectStoreUnavailableError",
    )
    assert _progress(database, run_id) == {}
    assert _run_row(weekly_db, run_id)[2] > before


def test_quota_stop_midway_resumes_from_the_next_journal(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # MISQ 兩頁各花 0.4：抓完 MISQ 後用量 0.8，I&O 一開始就停下；換一天（額度放寬）後只抓 I&O。
    run_id = _start(database)
    source = FakePaperSource(
        {MISQ_ISSN: _misq_book(), ISSN_L: _cassette_book()},
        cost_usd=Decimal("0.4"),
        clock=lambda: NOW,
    )
    journals = _journals(MISQ, IO)

    first = _run(database, source, FakeObjectStore(), run_id, journals=journals)

    assert first.stopped_for_quota
    assert list(_progress(database, run_id)) == ["misq"]
    source.requests.clear()
    roomy = OpenAlexQuota(
        daily_budget_usd=Decimal(100), stop_at_ratio=Decimal(1), default_request_cost_usd=Decimal(0)
    )
    second = _run(database, source, FakeObjectStore(), run_id, journals=journals, quota=roomy)

    assert second.fetch_finished
    assert {request.query.issn_l for request in source.requests} == {ISSN_L}


def test_finished_run_is_not_fetched_again(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    source = FakePaperSource({ISSN_L: _cassette_book()})
    _run(database, source, FakeObjectStore(), run_id, journals=_journals(IO))
    staged_before = _staged(weekly_db, run_id)
    finished_before = _run_row(weekly_db, run_id)[1]
    touched_before = _backdate(weekly_db, run_id)
    source.requests.clear()

    result = _run(database, source, FakeObjectStore(), run_id, journals=_journals(IO))

    assert result.fetch_finished
    assert (result.pages, result.journals) == (0, ())
    assert source.requests == []
    assert _staged(weekly_db, run_id) == staged_before
    finished_at, updated_at = _run_row(weekly_db, run_id)[1:]
    assert finished_at == finished_before
    # 1.9 之前抓完仍是 fetching：更新 updated_at，sweep 2 小時後才再補排，不是每 15 分鐘。
    assert updated_at > touched_before


def test_refetching_from_scratch_keeps_staging_and_recounts(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    _run(
        database,
        FakePaperSource({ISSN_L: _cassette_book(T1)}),
        FakeObjectStore(),
        run_id,
        journals=_journals(IO),
    )
    staged_before = _staged(weekly_db, run_id)
    progress_before = _progress(database, run_id)
    # 從頭重抓：刪掉進度列、清掉「全部抓完」的時間（模擬整次重來）。
    _owner(weekly_db, "DELETE FROM weekly_run_journal WHERE weekly_run_id = %s", run_id)
    _owner(weekly_db, "UPDATE weekly_run SET fetch_finished_at = NULL WHERE id = %s", run_id)
    source = FakePaperSource({ISSN_L: _cassette_book(T2)})

    _run(database, source, FakeObjectStore(), run_id, journals=_journals(IO))

    assert source.requests[0].cursor is None
    assert len(source.requests) == 3
    staged_after = _staged(weekly_db, run_id)
    assert set(staged_after) == set(staged_before)
    assert len(_rows(weekly_db, "SELECT id FROM ingest_staging")) == 200
    # 計數重算、不重複加。
    assert _progress(database, run_id) == progress_before
    # 新抓的比較新（T2），覆寫了暫存列的抓取時間。
    assert {row[0] for row in _rows(weekly_db, "SELECT fetched_at FROM ingest_staging")} == {T2}


def test_restarting_from_the_first_page_resets_counts_even_with_a_progress_row(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # 進度列還在、但這一頁送的是第一頁（cursor=None）：計數歸零再加，不是接著加。
    run_id = _start(database)
    counts = PageCounts(fetched=3, fetched_by_year={2020: 3}, staged=2, excluded=1, out_of_scope=1)
    with database.transaction() as connection:
        upsert_weekly_run_journal_page(
            connection, run_id, "misq", first_page=True, next_cursor="c2", counts=counts
        )
        upsert_weekly_run_journal_page(
            connection, run_id, "misq", first_page=False, next_cursor="c3", counts=counts
        )
    assert _progress(database, run_id)["misq"].fetched == 6
    assert _progress(database, run_id)["misq"].fetched_by_year == {2020: 6}

    with database.transaction() as connection:
        upsert_weekly_run_journal_page(
            connection, run_id, "misq", first_page=True, next_cursor=None, counts=counts
        )

    progress = _progress(database, run_id)["misq"]
    assert (progress.pages, progress.fetched, progress.staged, progress.excluded) == (1, 3, 2, 1)
    assert progress.fetched_by_year == {2020: 3}
    assert progress.finished
    assert progress.next_cursor is None


# --- 不是 fetching、身分不對、設定新增一本 -----------------------------------------------------


def test_missing_or_published_run_is_skipped_without_touching_the_source(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    _owner(weekly_db, "UPDATE weekly_run SET status = 'published' WHERE id = %s", run_id)
    source = FakePaperSource({ISSN_L: _cassette_book()})

    for weekly_run_id in (run_id, uuid.uuid7()):
        with capture_logs() as logs:
            result = _run(
                database, source, FakeObjectStore(), weekly_run_id, journals=_journals(IO)
            )
        assert result.skipped
        assert not result.fetch_finished
        assert [log for log in logs if log["event"] == "每週更新不是抓取中，略過"]

    assert source.requests == []
    assert _rows(weekly_db, "SELECT id FROM weekly_run_journal") == []


def test_member_cannot_start_or_run_and_nothing_is_touched(database: TrackingDatabase) -> None:
    source = FakePaperSource({ISSN_L: _cassette_book()})
    queue = FakeJobQueue()

    with pytest.raises(PermissionDeniedError):
        weekly_update(MEMBER, database, queue.enqueue)
    with pytest.raises(PermissionDeniedError):
        _run(
            database, source, FakeObjectStore(), uuid.uuid7(), journals=_journals(IO), actor=MEMBER
        )

    assert database.opened == 0
    assert source.requests == []
    assert queue.queued == []


def test_a_journal_added_to_the_config_is_the_only_one_fetched(
    weekly_db: TestDatabase, database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 設定只有 MISQ 時抓完了，但還沒寫「全部抓完」就當掉（模擬 worker 被關掉）；
    # 之後在 config/journals.yaml 加一本 PMJ：續跑時只抓 PMJ（設定即資料）。
    run_id = _start(database)
    source = FakePaperSource(
        {MISQ_ISSN: _misq_book(), PMJ_ISSN: [_records([_work(9, issn_l=PMJ_ISSN)])]}
    )

    def crash(*_args: object) -> None:
        raise RuntimeError("測試：寫完成時間前當掉")

    with monkeypatch.context() as patch:
        patch.setattr(weekly_update_module, "update_weekly_run_fetch_finished", crash)
        with pytest.raises(RuntimeError, match="當掉"):
            _run(database, source, FakeObjectStore(), run_id, journals=_journals(MISQ))
    source.requests.clear()

    result = _run(database, source, FakeObjectStore(), run_id, journals=_journals(MISQ, PMJ))

    assert result.journals == ("pmj",)
    assert {request.query.issn_l for request in source.requests} == {PMJ_ISSN}
    assert result.fetch_finished
    assert _key(_work(9)) in _staged(weekly_db, run_id)
    assert set(_progress(database, run_id)) == {"misq", "pmj"}


# --- repo：讀者身分、暫存表 upsert -------------------------------------------------------------


def test_repo_reads_are_for_system_only(database: TrackingDatabase) -> None:
    with database.transaction() as connection:
        for read in (
            lambda: get_weekly_run(MEMBER, connection, uuid.uuid7()),  # type: ignore[arg-type]
            lambda: find_fetching_weekly_run(MEMBER, connection),  # type: ignore[arg-type]
            lambda: list_weekly_run_journals(MEMBER, connection, uuid.uuid7()),  # type: ignore[arg-type]
        ):
            with pytest.raises(TypeError):
                read()


def test_get_weekly_run_returns_the_row(database: TrackingDatabase) -> None:
    run_id = _start(database)

    with database.transaction() as connection:
        run = get_weekly_run(SYSTEM, connection, run_id)
        fetching = find_fetching_weekly_run(SYSTEM, connection)
        missing = get_weekly_run(SYSTEM, connection, uuid.uuid7())

    assert run is not None
    assert run == fetching
    assert (run.id, run.status.value, run.fetch_finished_at) == (run_id, "fetching", None)
    assert missing is None


def test_upsert_staged_papers_keeps_the_last_duplicate_and_never_older_data(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    from radar.domain.parsers.openalex import parse_openalex_work

    batch = uuid.uuid7()

    def item(work: dict[str, Any], at: datetime) -> StagingItem:
        return StagingItem(_key(work), parse_openalex_work(work_bytes(work), fetched_at=at), None)

    first = _work(1, title="First title")
    renamed = _work(1, title="Renamed title")
    older = _work(1, title="Older title")
    with database.transaction() as connection:
        # 同一次呼叫裡重複的外部鍵只留最後一筆。
        written = upsert_staged_papers(
            connection,
            batch_id=batch,
            kind=StagingKind.WEEKLY_UPDATE,
            items=[item(first, T1), item(_work(2), T1), item(renamed, T1)],
        )
    assert written == 2
    [(title,)] = _rows(
        weekly_db,
        "SELECT parsed->>'title' FROM ingest_staging WHERE external_key = %s",
        _key(first),
    )
    assert title == "Renamed title"

    with database.transaction() as connection:
        # 比較舊的不蓋掉比較新的；一樣新或比較新的才覆寫。
        assert (
            upsert_staged_papers(
                connection,
                batch_id=batch,
                kind=StagingKind.WEEKLY_UPDATE,
                items=[item(older, T1 - timedelta(hours=1))],
            )
            == 0
        )
        assert (
            upsert_staged_papers(
                connection, batch_id=batch, kind=StagingKind.WEEKLY_UPDATE, items=[item(first, T2)]
            )
            == 1
        )
        assert (
            upsert_staged_papers(
                connection, batch_id=batch, kind=StagingKind.WEEKLY_UPDATE, items=[]
            )
            == 0
        )
    assert _rows(
        weekly_db,
        "SELECT parsed->>'title', fetched_at FROM ingest_staging WHERE external_key = %s",
        _key(first),
    ) == [("First title", T2)]
    assert len(_rows(weekly_db, "SELECT id FROM ingest_staging")) == 2

    with database.transaction() as connection, pytest.raises(ValueError, match="StagingItem"):
        upsert_staged_papers(
            connection,
            batch_id=batch,
            kind=StagingKind.WEEKLY_UPDATE,
            items=["x"],  # type: ignore[list-item]
        )


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO weekly_run_journal (weekly_run_id, journal_key) VALUES (%s, 'IO')",
        "INSERT INTO weekly_run_journal (weekly_run_id, journal_key, next_cursor)"
        " VALUES (%s, 'io', '')",
        "INSERT INTO weekly_run_journal (weekly_run_id, journal_key, fetched)"
        " VALUES (%s, 'io', -1)",
        "INSERT INTO weekly_run_journal (weekly_run_id, journal_key, fetched_by_year)"
        " VALUES (%s, 'io', '[]')",
    ],
)
def test_weekly_run_journal_check_constraints(
    weekly_db: TestDatabase, database: TrackingDatabase, sql: str
) -> None:
    run_id = _start(database)

    with weekly_db.connect(APP_ROLE) as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(sql, (run_id,))


def test_staging_kind_check_accepts_weekly_update_only_besides_reparse(
    weekly_db: TestDatabase,
) -> None:
    sql = (
        "INSERT INTO ingest_staging"
        " (kind, batch_id, source, external_key, source_hash, fetched_at, parsed)"
        " VALUES (%s, uuidv7(), 'openalex', 'W1', %s, now(), '{}')"
    )
    with weekly_db.connect(APP_ROLE) as connection:
        connection.execute(sql, ("weekly_update", "a" * 64))
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(sql, ("first_import", "a" * 64))


# --- repo：失敗次數（Story 1.11、ADR-0028） --------------------------------------------------


def _failure(database: Database, run_id: uuid.UUID) -> int | None:
    with database.transaction() as connection:
        return update_weekly_run_failure(connection, run_id)


def _mark_failed(database: Database, run_id: uuid.UUID, max_failures: int) -> int | None:
    with database.transaction() as connection:
        return update_weekly_run_failed(connection, run_id, max_failures=max_failures)


def _failure_row(db: TestDatabase, run_id: uuid.UUID) -> tuple[Any, ...]:
    [row] = _rows(
        db, "SELECT status, failure_count, updated_at FROM weekly_run WHERE id = %s", run_id
    )
    return row


def test_update_weekly_run_failure_only_counts_up(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # Codex C2：加次數與改成 failed 分成兩個交易；加次數這一句只加、不改狀態。
    run_id = _start(database)
    touched = _backdate(weekly_db, run_id)
    assert _failure_row(weekly_db, run_id)[:2] == ("fetching", 0)

    assert [_failure(database, run_id) for _ in range(3)] == [1, 2, 3]

    # 仍是 fetching（即使次數早就超過任何上限）；updated_at 不由它改。
    assert _failure_row(weekly_db, run_id) == ("fetching", 3, touched)
    with database.transaction() as connection:
        run = get_weekly_run(SYSTEM, connection, run_id)
    assert run is not None
    assert run.failure_count == 3


def test_update_weekly_run_failed_marks_only_at_the_limit(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    touched = _backdate(weekly_db, run_id)
    _failure(database, run_id)
    _failure(database, run_id)

    # 還沒到上限（2 < 3）：不改、回 None，updated_at 也不動。
    assert _mark_failed(database, run_id, 3) is None
    assert _failure_row(weekly_db, run_id) == ("fetching", 2, touched)

    # 到上限：改成 failed、更新 updated_at，回累計次數。
    assert _failure(database, run_id) == 3
    assert _mark_failed(database, run_id, 3) == 3
    status, count, updated_at = _failure_row(weekly_db, run_id)
    assert (status, count) == ("failed", 3)
    assert updated_at > touched

    # 已經不是 fetching：兩句都不改、回 None。部分唯一索引也放開，可以建新的一筆。
    assert _failure(database, run_id) is None
    assert _mark_failed(database, run_id, 3) is None
    assert _failure_row(weekly_db, run_id)[:2] == ("failed", 3)
    assert _start(database) != run_id


def test_update_weekly_run_failed_with_a_lower_limit_marks_at_once(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # 次數已經超過（例如上限調低）：≥ 上限就改成 failed。
    run_id = _start(database)
    _owner(weekly_db, "UPDATE weekly_run SET failure_count = 5 WHERE id = %s", run_id)

    assert _mark_failed(database, run_id, 2) == 5
    assert _failure_row(weekly_db, run_id)[:2] == ("failed", 5)


def test_failure_functions_leave_published_and_missing_runs_alone(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    _owner(weekly_db, "UPDATE weekly_run SET status = 'published' WHERE id = %s", run_id)

    for weekly_run_id in (run_id, uuid.uuid7()):
        assert _failure(database, weekly_run_id) is None
        assert _mark_failed(database, weekly_run_id, 1) is None
    assert _failure_row(weekly_db, run_id)[:2] == ("published", 0)


def test_touched_while_fetching_needs_fetching_and_room_below_the_limit(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # Codex C1：抓取每頁的交易第一句。仍是 fetching、還沒到上限才改到（並更新 updated_at）。
    run_id = _start(database)
    touched = _backdate(weekly_db, run_id)

    def touch(weekly_run_id: uuid.UUID, max_failures: int) -> bool:
        with database.transaction() as connection:
            return update_weekly_run_touched_while_fetching(
                connection, weekly_run_id, max_failures=max_failures
            )

    assert touch(run_id, 2)
    status, count, updated_at = _failure_row(weekly_db, run_id)
    assert (status, count) == ("fetching", 0)
    assert updated_at > touched

    # 次數 1 < 上限 2：仍改到。
    _failure(database, run_id)
    assert touch(run_id, 2)
    # 次數到上限 2（還是 fetching，等著收尾）：不改，updated_at 不動。
    _failure(database, run_id)
    touched = _backdate(weekly_db, run_id)
    assert not touch(run_id, 2)
    assert _failure_row(weekly_db, run_id) == ("fetching", 2, touched)

    # 不是 fetching、不存在：不改。
    for status_value in ("failed", "published"):
        _owner(weekly_db, "UPDATE weekly_run SET status = %s WHERE id = %s", status_value, run_id)
        assert not touch(run_id, 100)
    assert not touch(uuid.uuid7(), 100)


def test_published_respects_the_failure_limit_when_given(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # Codex C2：已到上限、等著收尾的那一筆不能被套用；沒給上限時照 1.9 的行為。
    run_id = _start(database)
    _owner(
        weekly_db,
        "UPDATE weekly_run SET fetch_finished_at = now(), failure_count = 2 WHERE id = %s",
        run_id,
    )

    def publish(**kwargs: Any) -> bool:
        with database.transaction() as connection:
            return update_weekly_run_published(connection, run_id, **kwargs)

    assert not publish(max_failures=2)
    assert _failure_row(weekly_db, run_id)[:2] == ("fetching", 2)
    assert publish(max_failures=3)
    assert _failure_row(weekly_db, run_id)[:2] == ("published", 2)


def test_failure_limit_functions_reject_a_bad_limit_without_touching_the_database() -> None:
    class Untouchable:
        def __getattr__(self, name: str) -> Any:
            pytest.fail(f"不該碰連線（{name}）")

    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="max_failures"):
            update_weekly_run_failed(Untouchable(), uuid.uuid7(), max_failures=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="max_failures"):
            update_weekly_run_touched_while_fetching(Untouchable(), uuid.uuid7(), max_failures=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="max_failures"):
            update_weekly_run_published(Untouchable(), uuid.uuid7(), max_failures=bad)  # type: ignore[arg-type]


def test_weekly_run_failure_count_check(weekly_db: TestDatabase) -> None:
    with (
        weekly_db.connect(APP_ROLE) as connection,
        pytest.raises(
            psycopg.errors.CheckViolation, match="ck_weekly_run_failure_count_not_negative"
        ),
    ):
        connection.execute("INSERT INTO weekly_run (status, failure_count) VALUES ('failed', -1)")


# --- open_and_run_weekly_update：確認要抓才開 adapter（對抗式審查第 1 條） ---------------------


class _SetupBrokenError(Exception):
    """模擬開 adapter 失敗（缺金鑰、設定壞掉）；訊息帶一段不該進日誌的內容。"""


class _Opener:
    """`open_setup` 的替身：記下被呼叫幾次、有沒有關掉；`error` 有值時開的時候就丟。"""

    def __init__(self, setup: WeeklyFetchSetup | None = None, error: Exception | None = None):
        self.setup = setup
        self.error = error
        self.opened = 0
        self.closed = 0

    @contextmanager
    def __call__(self) -> Iterator[WeeklyFetchSetup]:
        self.opened += 1
        if self.error is not None:
            raise self.error
        assert self.setup is not None
        try:
            yield self.setup
        finally:
            self.closed += 1


def _setup(source: FakePaperSource, journals: JournalsConfig) -> WeeklyFetchSetup:
    return WeeklyFetchSetup(
        source=source,
        store=FakeObjectStore(),
        journals=journals,
        exclusion_rules=RULES,
        quota=QUOTA,
    )


def _open_and_run(database: Database, opener: _Opener, weekly_run_id: uuid.UUID) -> Any:
    return open_and_run_weekly_update(SYSTEM, database, opener, weekly_run_id, clock=lambda: NOW)


def test_open_and_run_fetches_with_the_opened_setup_then_closes_it(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    source = FakePaperSource({ISSN_L: _cassette_book()}, clock=lambda: NOW)
    opener = _Opener(_setup(source, _journals(IO)))

    result = _open_and_run(database, opener, run_id)

    assert result.fetch_finished
    assert (opener.opened, opener.closed) == (1, 1)
    assert len(source.requests) == 3
    assert len(_staged(weekly_db, run_id)) == 200


def test_setup_failure_touches_the_run_and_raises_the_original_error(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    before = _backdate(weekly_db, run_id)
    opener = _Opener(error=_SetupBrokenError("secret-value-in-message"))

    with capture_logs() as logs, pytest.raises(_SetupBrokenError):
        _open_and_run(database, opener, run_id)

    assert opener.opened == 1
    # 建 adapter 失敗也更新 updated_at：sweep 照 2 小時的時限補排，不會每 15 分鐘就補排一次。
    assert _run_row(weekly_db, run_id)[2] > before
    assert _progress(database, run_id) == {}
    [error] = [log for log in logs if log["log_level"] == "error"]
    assert error["event"] == "每週更新準備抓取失敗"
    assert (error["weekly_run_id"], error["error"]) == (str(run_id), "_SetupBrokenError")
    assert "secret-value-in-message" not in str(logs)


class _WrappedSetupError(Exception):
    """模擬包裝過的建 adapter 錯誤：`error_type` 帶原本的錯誤類別（或被塞了別的內容）。"""

    def __init__(self, error_type: object) -> None:
        self.error_type = error_type
        super().__init__("wrapped")


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (_WrappedSetupError("ValueError"), "ValueError"),
        (_WrappedSetupError("Invalid endpoint: http://leak.example:9000"), None),
        (_WrappedSetupError(42), None),
        (_WrappedSetupError("abc123account"), None),
        (_WrappedSetupError("secret_value"), None),
        (_WrappedSetupError("A" * 65), None),
        (_SetupBrokenError("secret-value-in-message"), None),
    ],
    ids=[
        "class-name",
        "url-in-error-type",
        "not-a-string",
        "lowercase-setting-value",
        "underscore-word",
        "too-long",
        "no-error-type",
    ],
)
def test_setup_failure_log_carries_only_a_class_name_as_cause(
    weekly_db: TestDatabase, database: TrackingDatabase, error: Exception, cause: str | None
) -> None:
    # `cause` 只收像類別名稱的字串；`error_type` 剛好是訊息或網址時不收（K7）。
    run_id = _start(database)

    with capture_logs() as logs, pytest.raises(type(error)):
        _open_and_run(database, _Opener(error=error), run_id)

    [logged] = [log for log in logs if log["log_level"] == "error"]
    assert logged["event"] == "每週更新準備抓取失敗"
    assert (logged["error"], logged["cause"]) == (type(error).__name__, cause)
    assert "leak.example" not in str(logs)
    assert "secret-value-in-message" not in str(logs)


def test_finished_or_skipped_run_does_not_open_the_setup(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    _run(
        database,
        FakePaperSource({ISSN_L: _cassette_book()}),
        FakeObjectStore(),
        run_id,
        journals=_journals(IO),
    )
    before = _backdate(weekly_db, run_id)
    opener = _Opener(error=AssertionError("已經抓完時不該開 adapter"))

    finished = _open_and_run(database, opener, run_id)

    assert finished.fetch_finished
    assert (finished.pages, finished.journals) == (0, ())
    # 已經抓完：只更新 updated_at，不讀設定、不建 adapter。
    assert _run_row(weekly_db, run_id)[2] > before

    _owner(weekly_db, "UPDATE weekly_run SET status = 'published' WHERE id = %s", run_id)
    for weekly_run_id in (run_id, uuid.uuid7()):
        assert _open_and_run(database, opener, weekly_run_id).skipped
    assert opener.opened == 0


def test_member_cannot_open_and_run_and_nothing_is_opened(database: TrackingDatabase) -> None:
    opener = _Opener(error=AssertionError("不該開"))

    with pytest.raises(PermissionDeniedError):
        open_and_run_weekly_update(MEMBER, database, opener, uuid.uuid7())

    assert database.opened == 0
    assert opener.opened == 0


# --- 失敗後更新 updated_at 也失敗（對抗式審查第 6 條） ------------------------------------------


def _break_touch(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_connection: Connection, _weekly_run_id: uuid.UUID) -> None:
        raise psycopg.OperationalError("connection to server at 10.9.8.7 failed: secret-host")

    monkeypatch.setattr(weekly_update_module, "update_weekly_run_touched", broken)


def _touch_warning(logs: list[MutableMapping[str, Any]], run_id: uuid.UUID) -> None:
    [warning] = [log for log in logs if log["event"] == "每週更新失敗後更新 updated_at 也失敗"]
    assert warning["log_level"] == "warning"
    assert (warning["weekly_run_id"], warning["error"]) == (str(run_id), "OperationalError")
    assert "secret-host" not in str(logs)


def test_touch_failure_after_a_fetch_failure_keeps_the_original_error(
    database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _start(database)
    source = FakePaperSource({ISSN_L: _cassette_book()})
    source.fail_once(ISSN_L, 2)
    _break_touch(monkeypatch)

    with capture_logs() as logs, pytest.raises(JournalFetchError) as caught:
        _run(database, source, FakeObjectStore(), run_id, journals=_journals(IO))

    # 往上丟的是原本的抓取錯誤，不是更新 updated_at 時的 OperationalError。
    assert caught.value.journal_key == "io"
    _touch_warning(logs, run_id)
    assert [log["event"] for log in logs if log["log_level"] == "error"] == [
        "每週更新抓取失敗，進度已保存"
    ]
    # 第 1 頁的進度照樣保存（那個交易本身沒有被拿掉）。
    assert _progress(database, run_id)["io"].pages == 1


def test_touch_failure_after_a_setup_failure_keeps_the_original_error(
    database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _start(database)
    _break_touch(monkeypatch)

    with capture_logs() as logs, pytest.raises(_SetupBrokenError):
        _open_and_run(database, _Opener(error=_SetupBrokenError("x")), run_id)

    _touch_warning(logs, run_id)
