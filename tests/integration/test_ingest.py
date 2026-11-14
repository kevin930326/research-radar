"""`services.ingest.fetch_journal` 對真的 PostgreSQL＋替身（`FakePaperSource`、`FakeObjectStore`）。

存原始資料、同內容再抓、同一頁再寫、額度、抓取失敗、身分、交易與外部呼叫、續跑，以及
`raw_fetch`／`api_call` 的 ON CONFLICT 與 CHECK 約束。每個測試一個全新、已遷移的資料庫，
用應用程式角色（`radar_app`）連線，資料都是這裡自己造的。
"""

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import psycopg
import pytest
import stamina
from sqlalchemy import Connection
from structlog.testing import capture_logs

from radar.adapters.fake import FakeObjectStore, FakePaperSource
from radar.adapters.openalex import OpenAlexSource
from radar.config import load_domain_config
from radar.domain.domain_config import Journal, OpenAlexQuota
from radar.domain.permissions import PermissionDeniedError
from radar.domain.raw_records import (
    ApiCall,
    JournalQuery,
    PaperSourceError,
    RawRecord,
    RawSource,
    SourcePage,
    decompress_raw,
)
from radar.domain.storage_keys import RAW_CONTENT_TYPE, raw_object_key
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.api_calls import create_api_calls, get_api_usage_since
from radar.repo.db import Database
from radar.repo.raw_fetches import create_raw_fetches, find_stored_sha256s
from radar.services.ingest import (
    FetchedPage,
    JournalFetchError,
    fetch_journal,
    start_of_utc_day,
)
from tests.database import APP_ROLE, TestDatabase, migrate

ISSN = "1471-7727"
JOURNAL = Journal(key="io", name="Information and Organization", issn_l=ISSN)
QUOTA = OpenAlexQuota(
    daily_budget_usd=Decimal("1.0"),
    stop_at_ratio=Decimal("0.8"),
    default_request_cost_usd=Decimal("0.0001"),
)
NOW = datetime(2026, 11, 14, 0, 30, tzinfo=UTC)
T1 = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)
T2 = T1 + timedelta(days=7)
SYSTEM = SystemViewer()
ROOT = Path(__file__).resolve().parents[2]


class TrackingDatabase(Database):
    """記下目前有幾個開著的交易、總共開過幾個。"""

    def __init__(self, url: str) -> None:
        super().__init__(url, connect_timeout=5, pool_size=2)
        self.open_transactions = 0
        self.opened = 0

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        self.opened += 1
        self.open_transactions += 1
        try:
            with super().transaction() as connection:
                yield connection
        finally:
            self.open_transactions -= 1


class GuardedSource(FakePaperSource):
    """每次被呼叫時記下當下有沒有開著的交易。"""

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


@pytest.fixture
def ingest_db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(ingest_db: TestDatabase) -> TrackingDatabase:
    return TrackingDatabase(ingest_db.app_url)


def _record(number: int, fetched_at: datetime = T1, content: bytes | None = None) -> RawRecord:
    body = (
        content if content is not None else f'{{"id": "https://openalex.org/W{number}"}}'.encode()
    )
    return RawRecord(RawSource.OPENALEX, f"W{number}", body, fetched_at)


def _fetch(
    database: Database,
    source: FakePaperSource,
    store: FakeObjectStore,
    *,
    cursor: str | None = None,
    quota: OpenAlexQuota = QUOTA,
    actor: Any = SYSTEM,
    on_page: Any = None,
) -> Any:
    return fetch_journal(
        actor,
        database,
        source,
        store,
        JOURNAL,
        start_year=2016,
        quota=quota,
        cursor=cursor,
        on_page=on_page,
        clock=lambda: NOW,
    )


def _rows(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(sql, params).fetchall()


def _call(called_at: datetime, cost: str = "0", **changes: Any) -> ApiCall:
    values: dict[str, Any] = {
        "source": RawSource.OPENALEX,
        "operation": "list_works",
        "http_status": 200,
        "cost_usd": Decimal(cost),
        "remaining_usd": None,
        "limit_usd": None,
        "called_at": called_at,
    }
    values.update(changes)
    return ApiCall(**values)


def _seed_calls(database: Database, *calls: ApiCall) -> None:
    with database.transaction() as connection:
        create_api_calls(connection, calls)


# --- 存原始資料 -----------------------------------------------------------------------------


def test_new_content_is_stored_and_recorded(ingest_db: TestDatabase, database: Database) -> None:
    records = [_record(1), _record(2), _record(3)]
    source = FakePaperSource({ISSN: [records[:2], records[2:]]}, clock=lambda: NOW)
    store = FakeObjectStore()

    result = _fetch(database, source, store)

    assert (result.pages, result.records, result.stored_objects, result.reused_objects) == (
        2,
        3,
        3,
        0,
    )
    assert result.next_cursor is None
    assert not result.stopped_for_quota
    assert result.journal_key == "io"
    for record in records:
        key = raw_object_key("openalex", record.sha256)
        assert key == f"raw/openalex/{hashlib.sha256(record.content).hexdigest()}.json.gz"
        assert decompress_raw(store.objects[key]) == record.content
        assert store.content_types[key] == RAW_CONTENT_TYPE
    assert sorted(
        _rows(ingest_db, "SELECT source, external_key, sha256, fetched_at FROM raw_fetch")
    ) == sorted(("openalex", r.external_key, r.sha256, T1) for r in records)
    # 每頁一次 API 呼叫，各記一筆。
    assert _rows(ingest_db, "SELECT source, operation, http_status, cost_usd FROM api_call") == [
        ("openalex", "list_works", 200, Decimal("0.000100")),
        ("openalex", "list_works", 200, Decimal("0.000100")),
    ]


def test_same_content_fetched_later_adds_a_row_without_storing(
    ingest_db: TestDatabase, database: Database
) -> None:
    store = FakeObjectStore()
    _fetch(database, FakePaperSource({ISSN: [[_record(1, T1)]]}), store)
    puts_before = len(store.puts)

    result = _fetch(database, FakePaperSource({ISSN: [[_record(1, T2)]]}), store)

    assert len(store.puts) == puts_before
    assert (result.stored_objects, result.reused_objects) == (0, 1)
    rows = _rows(
        ingest_db, "SELECT external_key, sha256, fetched_at FROM raw_fetch ORDER BY fetched_at"
    )
    assert [(key, fetched_at) for key, _, fetched_at in rows] == [("W1", T1), ("W1", T2)]
    assert rows[0][1] == rows[1][1]


def test_changed_content_is_stored_again(ingest_db: TestDatabase, database: Database) -> None:
    store = FakeObjectStore()
    _fetch(database, FakePaperSource({ISSN: [[_record(1, T1, b'{"id": "W1", "n": 1}')]]}), store)

    result = _fetch(
        database, FakePaperSource({ISSN: [[_record(1, T2, b'{"id": "W1", "n": 2}')]]}), store
    )

    assert (result.stored_objects, result.reused_objects) == (1, 0)
    assert len(store.objects) == 2
    assert len(_rows(ingest_db, "SELECT DISTINCT sha256 FROM raw_fetch")) == 2


def test_same_page_written_twice_keeps_row_count(
    ingest_db: TestDatabase, database: Database
) -> None:
    pages = {ISSN: [[_record(1), _record(2)]]}
    store = FakeObjectStore()
    _fetch(database, FakePaperSource(pages), store)

    result = _fetch(database, FakePaperSource(pages), store)

    assert result.stored_objects == 0
    assert len(_rows(ingest_db, "SELECT id FROM raw_fetch")) == 2


def test_duplicate_content_in_one_page_is_put_once(
    ingest_db: TestDatabase, database: Database
) -> None:
    same = b'{"same": true}'
    store = FakeObjectStore()

    result = _fetch(
        database,
        FakePaperSource({ISSN: [[_record(1, content=same), _record(2, content=same)]]}),
        store,
    )

    assert len(store.puts) == 1
    assert (result.records, result.stored_objects, result.reused_objects) == (2, 1, 0)
    assert len(_rows(ingest_db, "SELECT id FROM raw_fetch")) == 2


def test_resume_from_given_cursor(ingest_db: TestDatabase, database: Database) -> None:
    source = FakePaperSource({ISSN: [[_record(1)], [_record(2)], [_record(3)]]})
    second_page = source.fetch_journal_page(JournalQuery(ISSN, 2016), None).next_cursor
    source.requests.clear()

    result = _fetch(database, source, FakeObjectStore(), cursor=second_page)

    assert source.requests[0].cursor == second_page
    assert result.pages == 2
    assert sorted(row[0] for row in _rows(ingest_db, "SELECT external_key FROM raw_fetch")) == [
        "W2",
        "W3",
    ]


def test_empty_journal_finishes_after_one_call(ingest_db: TestDatabase, database: Database) -> None:
    result = _fetch(database, FakePaperSource(), FakeObjectStore())

    assert (result.pages, result.records, result.next_cursor) == (1, 0, None)
    assert len(_rows(ingest_db, "SELECT id FROM api_call")) == 1


def test_finish_log_has_counts_only(database: Database) -> None:
    with capture_logs() as logs:
        _fetch(database, FakePaperSource({ISSN: [[_record(1)]]}), FakeObjectStore())

    [entry] = [log for log in logs if log["event"] == "期刊抓取結束"]
    assert entry["log_level"] == "info"
    assert (entry["journal"], entry["pages"], entry["records"]) == ("io", 1, 1)
    assert (entry["stored_objects"], entry["reused_objects"]) == (1, 0)


# --- 額度 ---------------------------------------------------------------------------------


def test_stops_when_local_usage_reaches_ratio(ingest_db: TestDatabase, database: Database) -> None:
    _seed_calls(database, _call(NOW - timedelta(minutes=10), "0.5"), _call(NOW, "0.3"))
    source = FakePaperSource({ISSN: [[_record(1)]]})
    store = FakeObjectStore()

    with capture_logs() as logs:
        result = _fetch(database, source, store, cursor="kept-cursor")

    assert source.requests == []
    assert store.puts == []
    assert result.stopped_for_quota
    assert result.next_cursor == "kept-cursor"
    assert result.pages == 0
    [warning] = [log for log in logs if log["log_level"] == "warning"]
    assert warning["used_usd"] == "0.800000"
    assert warning["journal"] == "io"


def test_yesterdays_usage_does_not_count(ingest_db: TestDatabase, database: Database) -> None:
    # NOW 是 UTC 00:30；23:59 是昨天（OpenAlex 在 UTC 午夜重置）。
    _seed_calls(database, _call(datetime(2026, 11, 13, 23, 59, tzinfo=UTC), "5"))
    source = FakePaperSource({ISSN: [[_record(1)]]})

    result = _fetch(database, source, FakeObjectStore())

    assert not result.stopped_for_quota
    assert len(source.requests) == 1


def test_stops_when_reported_usage_reaches_ratio(database: Database) -> None:
    # 本地只花了 0.0001，但 OpenAlex 回報同一把 key 今天已經用了 0.85（例如 spikes 在用）。
    _seed_calls(
        database,
        _call(NOW, "0.0001", remaining_usd=Decimal("0.15"), limit_usd=Decimal(1)),
    )
    source = FakePaperSource({ISSN: [[_record(1)]]})

    result = _fetch(database, source, FakeObjectStore())

    assert result.stopped_for_quota
    assert source.requests == []


def test_only_the_latest_report_counts(database: Database) -> None:
    _seed_calls(
        database,
        _call(NOW - timedelta(minutes=5), remaining_usd=Decimal("0.1"), limit_usd=Decimal(1)),
        _call(NOW, remaining_usd=Decimal("0.9"), limit_usd=Decimal(1)),
    )

    with database.transaction() as connection:
        usage = get_api_usage_since(SYSTEM, connection, RawSource.OPENALEX, start_of_utc_day(NOW))

    assert usage.reported_used_usd == Decimal("0.1")
    assert usage.local_cost_usd == 0
    source = FakePaperSource({ISSN: [[_record(1)]]})
    assert not _fetch(database, source, FakeObjectStore()).stopped_for_quota


def test_quota_reached_midway_stops_and_resumes(
    ingest_db: TestDatabase, database: Database
) -> None:
    pages = {ISSN: [[_record(1)], [_record(2)], [_record(3)]]}
    source = FakePaperSource(pages, cost_usd=Decimal("0.5"))

    first = _fetch(database, source, FakeObjectStore())

    assert first.stopped_for_quota
    assert first.pages == 2
    assert first.next_cursor is not None
    # 換一天（或額度放寬）後從停下的 cursor 接著抓。
    roomy = OpenAlexQuota(
        daily_budget_usd=Decimal(100), stop_at_ratio=Decimal(1), default_request_cost_usd=Decimal(0)
    )
    second = _fetch(database, source, FakeObjectStore(), cursor=first.next_cursor, quota=roomy)
    assert (second.pages, second.next_cursor, second.stopped_for_quota) == (1, None, False)
    assert sorted(row[0] for row in _rows(ingest_db, "SELECT external_key FROM raw_fetch")) == [
        "W1",
        "W2",
        "W3",
    ]


def test_costs_after_the_latest_report_keep_adding_up(database: Database) -> None:
    # Codex 審查第 2 點：最新一次回報已用 0.7999，之後兩次各花 0.0001 卻沒帶額度標頭；
    # 實際至少 0.8001，要在 0.8 停下（不能停在最後一次回報的 0.7999）。
    _seed_calls(
        database,
        # 回報之前的花費已經算在對方回報的數字裡，不能再加一次。
        _call(NOW - timedelta(minutes=20), "0.0005"),
        _call(
            NOW - timedelta(minutes=10),
            "0.0001",
            remaining_usd=Decimal("0.2001"),
            limit_usd=Decimal(1),
        ),
        _call(NOW - timedelta(minutes=5), "0.0001"),
        _call(NOW, "0.0001", http_status=None),
    )

    with database.transaction() as connection:
        usage = get_api_usage_since(SYSTEM, connection, RawSource.OPENALEX, start_of_utc_day(NOW))

    assert usage.reported_used_usd == Decimal("0.8001")
    assert usage.local_cost_usd == Decimal("0.0008")
    assert usage.used_usd == Decimal("0.8001")
    source = FakePaperSource({ISSN: [[_record(1)]]})
    result = _fetch(database, source, FakeObjectStore(), cursor="kept-cursor")
    assert result.stopped_for_quota
    assert result.next_cursor == "kept-cursor"
    assert source.requests == []


def _exhausted_quota_source(seen: list[httpx2.Request]) -> OpenAlexSource:
    """真的 OpenAlex adapter（MockTransport，不連網路）。

    每次都回 429、回報 limit=1、remaining=0。
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        headers = {
            "x-ratelimit-cost-usd": "0",
            "x-ratelimit-remaining-usd": "0",
            "x-ratelimit-limit-usd": "1",
        }
        return httpx2.Response(429, headers=headers)

    # 用 repo 的 config/openalex.yaml（重試 5 次），證明不是靠把次數調低才停下。
    config = load_domain_config({"RADAR_CONFIG_DIR": str(ROOT / "config")}).openalex
    assert config.retry.attempts == 5
    return OpenAlexSource(
        "fake-review-key",
        config,
        transport=httpx2.MockTransport(handler),
        sleep=lambda _seconds: None,
        clock=lambda: NOW,
    )


def test_quota_exhausted_during_retries_is_a_quota_stop_not_a_failure(
    ingest_db: TestDatabase, database: Database
) -> None:
    # Codex 審查第 1 點：首次 429 就回報額度用完。不送滿 5 次、不丟 PaperSourceError；
    # 這次呼叫照樣記進 api_call，回傳正常的「額度停下」結果。
    seen: list[httpx2.Request] = []
    with (
        stamina.set_testing(True, attempts=100, cap=True),
        _exhausted_quota_source(seen) as source,
        capture_logs() as logs,
    ):
        result = fetch_journal(
            SYSTEM,
            database,
            source,
            FakeObjectStore(),
            JOURNAL,
            start_year=2016,
            quota=QUOTA,
            cursor="kept-cursor",
            clock=lambda: NOW,
        )

    assert len(seen) == 1
    assert (result.stopped_for_quota, result.next_cursor, result.pages) == (
        True,
        "kept-cursor",
        0,
    )
    assert _rows(
        ingest_db, "SELECT http_status, cost_usd, remaining_usd, limit_usd FROM api_call"
    ) == [(429, Decimal(0), Decimal(0), Decimal(1))]
    [warning] = [log for log in logs if log["log_level"] == "warning"]
    assert warning["used_usd"] == "1.000000"
    assert "fake-review-key" not in str(logs)


# --- 失敗、身分、交易 ---------------------------------------------------------------------


def test_fetch_failure_commits_calls_then_reraises(
    ingest_db: TestDatabase, database: Database
) -> None:
    source = FakePaperSource({ISSN: [[_record(1)], [_record(2)]]})
    source.fail_once(ISSN, 2)

    with pytest.raises(PaperSourceError, match="第 2 頁"):
        _fetch(database, source, FakeObjectStore())

    assert sorted(row[0] for row in _rows(ingest_db, "SELECT http_status FROM api_call")) == [
        200,
        503,
    ]
    assert [row[0] for row in _rows(ingest_db, "SELECT external_key FROM raw_fetch")] == ["W1"]


class BoundedSource(FakePaperSource):
    """請求超過 `limit` 次就丟 AssertionError：拿掉循環檢查時測試會失敗，不會卡住。"""

    limit = 10

    def fetch_journal_page(self, query: JournalQuery, cursor: str | None) -> SourcePage:
        if len(self.requests) >= self.limit:
            raise AssertionError("請求太多次：cursor 繞圈沒有被擋下")
        return super().fetch_journal_page(query, cursor)


def test_cursor_cycle_across_pages_is_an_error(ingest_db: TestDatabase, database: Database) -> None:
    # Codex 審查第 5 點：* → A → B → A。費用為零、沒有額度回報，不擋就會一直繞。
    source = BoundedSource({ISSN: [[_record(1)], [_record(2)], [_record(3)]]}, cost_usd=Decimal(0))
    source.point_next_cursor(ISSN, 3, 2)

    with pytest.raises(PaperSourceError, match="繞回") as caught:
        _fetch(database, source, FakeObjectStore())

    cursors = [request.cursor for request in source.requests]
    assert len(cursors) == 3
    assert cursors[0] is None
    assert len(set(cursors)) == 3
    for cursor in cursors[1:]:
        assert cursor is not None
        assert cursor not in str(caught.value)
    # 已經抓到的三頁照樣存好、每次呼叫都記了。
    assert sorted(row[0] for row in _rows(ingest_db, "SELECT external_key FROM raw_fetch")) == [
        "W1",
        "W2",
        "W3",
    ]
    assert len(_rows(ingest_db, "SELECT id FROM api_call")) == 3


def test_cursor_cycle_back_to_the_starting_cursor_is_an_error(database: Database) -> None:
    # 從給的 cursor（第 2 頁）續跑時，繞回它自己也要擋。
    source = BoundedSource({ISSN: [[_record(1)], [_record(2)], [_record(3)]]}, cost_usd=Decimal(0))
    source.point_next_cursor(ISSN, 3, 2)
    start = source.fetch_journal_page(JournalQuery(ISSN, 2016), None).next_cursor
    source.requests.clear()

    with pytest.raises(PaperSourceError, match="繞回"):
        _fetch(database, source, FakeObjectStore(), cursor=start)

    assert len(source.requests) == 2


def test_member_cannot_fetch_and_nothing_is_touched(database: TrackingDatabase) -> None:
    source = FakePaperSource({ISSN: [[_record(1)]]})
    member = MemberViewer(member_id=uuid.uuid4(), is_admin=True)

    with pytest.raises(PermissionDeniedError):
        _fetch(database, source, FakeObjectStore(), actor=member)

    assert source.requests == []
    assert database.opened == 0


def test_no_transaction_is_open_during_external_calls(database: TrackingDatabase) -> None:
    source = GuardedSource(database, {ISSN: [[_record(1), _record(2)], [_record(3)]]})
    store = GuardedStore(database)
    source.fail_once(ISSN, 2)

    with pytest.raises(PaperSourceError):
        _fetch(database, source, store)
    _fetch(database, source, store, cursor=source.requests[-1].cursor)

    assert source.open_during_calls == [0, 0, 0]
    assert store.open_during_calls == [0, 0, 0]
    assert database.open_transactions == 0
    assert database.opened > 0


# --- repo：ON CONFLICT、讀者身分、CHECK 約束 ------------------------------------------------


def test_create_raw_fetches_skips_existing_rows(database: Database) -> None:
    with database.transaction() as connection:
        assert create_raw_fetches(connection, [_record(1), _record(2)]) == 2
    with database.transaction() as connection:
        assert create_raw_fetches(connection, [_record(1), _record(3)]) == 1
        assert create_raw_fetches(connection, []) == 0
        stored = find_stored_sha256s(
            SYSTEM,
            connection,
            RawSource.OPENALEX,
            [_record(1).sha256, _record(4).sha256],
        )
    assert stored == frozenset({_record(1).sha256})


def test_repo_reads_are_for_system_only(database: Database) -> None:
    member = MemberViewer(member_id=uuid.uuid4(), is_admin=True)
    with database.transaction() as connection:
        with pytest.raises(TypeError):
            find_stored_sha256s(member, connection, RawSource.OPENALEX, ["a" * 64])  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            get_api_usage_since(member, connection, RawSource.OPENALEX, NOW)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            "INSERT INTO raw_fetch (source, external_key, sha256, fetched_at)"
            " VALUES ('openalex', 'W1', %s, now())",
            ("A" * 64,),
        ),
        (
            "INSERT INTO raw_fetch (source, external_key, sha256, fetched_at)"
            " VALUES ('openalex', 'W1', %s, now())",
            ("a" * 63,),
        ),
        (
            "INSERT INTO raw_fetch (source, external_key, sha256, fetched_at)"
            " VALUES ('crossref', 'W1', %s, now())",
            ("a" * 64,),
        ),
        (
            "INSERT INTO raw_fetch (source, external_key, sha256, fetched_at)"
            " VALUES ('openalex', '', %s, now())",
            ("a" * 64,),
        ),
        (
            "INSERT INTO api_call (source, operation, http_status, cost_usd, called_at)"
            " VALUES ('openalex', 'list_works', %s, 0, now())",
            (600,),
        ),
        (
            "INSERT INTO api_call (source, operation, http_status, cost_usd, called_at)"
            " VALUES ('openalex', 'list_works', %s, 0, now())",
            (99,),
        ),
        (
            "INSERT INTO api_call (source, operation, http_status, cost_usd, called_at)"
            " VALUES ('crossref', 'list_works', %s, 0, now())",
            (200,),
        ),
        (
            "INSERT INTO api_call (source, operation, http_status, cost_usd, called_at)"
            " VALUES ('openalex', 'List-Works', %s, 0, now())",
            (200,),
        ),
        (
            "INSERT INTO api_call (source, operation, http_status, cost_usd, called_at)"
            " VALUES ('openalex', 'list_works', %s, -0.1, now())",
            (200,),
        ),
    ],
)
def test_check_constraints(ingest_db: TestDatabase, sql: str, params: tuple[object, ...]) -> None:
    with ingest_db.connect(APP_ROLE) as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(sql, params)


def test_api_call_without_status_is_allowed(ingest_db: TestDatabase, database: Database) -> None:
    _seed_calls(database, _call(NOW, "0", http_status=None))

    assert _rows(ingest_db, "SELECT http_status, cost_usd FROM api_call") == [
        (None, Decimal("0.000000"))
    ]


class FailingStore(FakeObjectStore):
    """第 `fail_on` 次 put 丟錯（模擬物件儲存重試用完）。"""

    def __init__(self, fail_on: int) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.attempts = 0

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self.attempts += 1
        if self.attempts == self.fail_on:
            raise OSError("測試：物件儲存寫不進去")
        super().put(key, data, content_type=content_type)


def test_put_failure_leaves_no_raw_fetch_rows_and_rerun_fills_in(
    ingest_db: TestDatabase, database: Database
) -> None:
    pages = {ISSN: [[_record(1), _record(2)]]}
    failing = FailingStore(fail_on=2)

    with pytest.raises(OSError, match="寫不進去"):
        _fetch(database, FakePaperSource(pages), failing)

    # 先 put 再寫列：這一頁的 API 呼叫已經 commit，但 raw_fetch 一列都沒有。
    assert len(_rows(ingest_db, "SELECT id FROM api_call")) == 1
    assert _rows(ingest_db, "SELECT id FROM raw_fetch") == []

    store = FakeObjectStore()
    result = _fetch(database, FakePaperSource(pages), store)

    assert (result.stored_objects, result.reused_objects) == (2, 0)
    for record in pages[ISSN][0]:
        assert decompress_raw(store.objects[raw_object_key("openalex", record.sha256)]) == (
            record.content
        )
    assert sorted(row[0] for row in _rows(ingest_db, "SELECT external_key FROM raw_fetch")) == [
        "W1",
        "W2",
    ]


def test_quota_reached_before_first_page_returns_none_cursor(database: Database) -> None:
    _seed_calls(database, _call(NOW, "0.9"))
    source = FakePaperSource({ISSN: [[_record(1)]]})

    result = _fetch(database, source, FakeObjectStore(), cursor=None)

    # next_cursor 是 None 但沒有抓完：要看 stopped_for_quota。
    assert result.stopped_for_quota
    assert result.next_cursor is None
    assert result.pages == 0
    assert source.requests == []


# --- on_page 與續跑用的 cursor（Story 1.8、D12(a)）-------------------------------------------


def test_on_page_gets_each_stored_page_including_the_empty_last_one(
    ingest_db: TestDatabase, database: TrackingDatabase
) -> None:
    # 第 1 頁、第 2 頁有資料，第 3 頁是空的最後一頁：三頁都交給 on_page。
    source = FakePaperSource({ISSN: [[_record(1), _record(2)], [_record(3)], []]})
    store = FakeObjectStore()
    seen: list[tuple[FetchedPage, list[str], int, int]] = []

    def on_page(page: FetchedPage) -> None:
        # 交給 on_page 時：這一頁的原始資料與 raw_fetch 都寫好了，而且沒有開著的交易。
        stored = sorted(row[0] for row in _rows(ingest_db, "SELECT external_key FROM raw_fetch"))
        seen.append((page, stored, len(store.puts), database.open_transactions))

    result = _fetch(database, source, store, on_page=on_page)

    assert result.pages == 3
    cursors = [request.cursor for request in source.requests]
    assert [entry[0].sent_cursor for entry in seen] == cursors
    assert [entry[0].next_cursor for entry in seen] == [cursors[1], cursors[2], None]
    assert [[r.external_key for r in entry[0].records] for entry in seen] == [
        ["W1", "W2"],
        ["W3"],
        [],
    ]
    assert [entry[1] for entry in seen] == [["W1", "W2"], ["W1", "W2", "W3"], ["W1", "W2", "W3"]]
    assert [entry[2] for entry in seen] == [2, 3, 3]
    assert [entry[3] for entry in seen] == [0, 0, 0]


def test_on_page_errors_propagate_unchanged(database: Database) -> None:
    source = FakePaperSource({ISSN: [[_record(1)], [_record(2)]]})

    class StageError(Exception):
        pass

    def on_page(page: FetchedPage) -> None:
        raise StageError("測試：暫存失敗")

    with pytest.raises(StageError, match="暫存失敗"):
        _fetch(database, source, FakeObjectStore(), on_page=on_page)

    # 第 1 頁交給 on_page 就失敗了，不往下抓。
    assert len(source.requests) == 1


def test_source_failure_raises_journal_fetch_error_with_the_failed_pages_cursor(
    ingest_db: TestDatabase, database: Database
) -> None:
    source = FakePaperSource({ISSN: [[_record(1)], [_record(2)], [_record(3)]]})
    source.fail_once(ISSN, 2)
    pages: list[FetchedPage] = []

    with pytest.raises(JournalFetchError) as caught:
        _fetch(database, source, FakeObjectStore(), on_page=pages.append)

    second_page = source.requests[-1].cursor
    assert second_page is not None
    error = caught.value
    assert isinstance(error, PaperSourceError)
    assert (error.journal_key, error.resume_cursor) == ("io", second_page)
    assert [call.http_status for call in error.calls] == [503]
    assert "第 2 頁" in error.reason
    assert second_page not in str(error)
    assert [page.sent_cursor for page in pages] == [None]
    # 失敗那次的 api_call 已經寫好（fetch_journal 寫，不要再寫一次）。
    assert sorted(row[0] for row in _rows(ingest_db, "SELECT http_status FROM api_call")) == [
        200,
        503,
    ]

    # 從 resume_cursor 續跑：只從第 2 頁開始送。
    source.requests.clear()
    result = _fetch(database, source, FakeObjectStore(), cursor=error.resume_cursor)
    assert source.requests[0].cursor == second_page
    assert (result.pages, result.next_cursor) == (2, None)


def test_failure_on_the_first_page_resumes_from_the_first_page(database: Database) -> None:
    source = FakePaperSource({ISSN: [[_record(1)]]})
    source.fail_once(ISSN, 1)

    with pytest.raises(JournalFetchError) as caught:
        _fetch(database, source, FakeObjectStore())

    assert caught.value.resume_cursor is None


def test_cursor_cycle_resumes_from_the_page_that_produced_the_bad_cursor(
    ingest_db: TestDatabase, database: Database
) -> None:
    # * → A → B → A：B 那一頁給的 next_cursor 繞回 A。B 的原始資料照樣存好，但不交給 on_page、
    # 不前進；續跑時要重送 B。
    source = BoundedSource({ISSN: [[_record(1)], [_record(2)], [_record(3)]]}, cost_usd=Decimal(0))
    source.point_next_cursor(ISSN, 3, 2)
    pages: list[FetchedPage] = []

    with pytest.raises(JournalFetchError, match="繞回") as caught:
        _fetch(database, source, FakeObjectStore(), on_page=pages.append)

    cursor_a, cursor_b = source.requests[1].cursor, source.requests[2].cursor
    assert cursor_a is not None
    assert cursor_b is not None
    assert caught.value.resume_cursor == cursor_b
    assert caught.value.journal_key == "io"
    assert caught.value.calls == ()
    for cursor in (cursor_a, cursor_b):
        assert cursor not in str(caught.value)
    assert [(page.sent_cursor, page.next_cursor) for page in pages] == [
        (None, cursor_a),
        (cursor_a, cursor_b),
    ]
    assert sorted(row[0] for row in _rows(ingest_db, "SELECT external_key FROM raw_fetch")) == [
        "W1",
        "W2",
        "W3",
    ]
