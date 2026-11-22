"""`services.reparse.reparse_papers` 對真的 PostgreSQL＋`FakeObjectStore`。

只動已匯入的論文、壞資料略過並計數、套用中途失敗整個退回、比較舊的資料不寫、物件儲存的呼叫
時沒有開著的交易、身分不對不碰任何東西、排除規則改了之後跟著變、只寫允許的表也不碰快照、
每週更新正在抓取時拒絕且什麼都不寫；以及 `list_latest_raw_fetches`、暫存表函式的分頁與驗證。

論文內容取自錄好的 OpenAlex 回應（`tests/openalex_cassette.py`，CC0）。每個測試一個全新、
已遷移的資料庫，用應用程式角色（`radar_app`）連線。
"""

import hashlib
import re
import uuid
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.rows import dict_row
from sqlalchemy import Connection, event
from structlog.testing import capture_logs

from radar.adapters.fake import FakeObjectStore
from radar.domain.domain_config import ExclusionRule, ExclusionRulesConfig
from radar.domain.papers import (
    KeywordSource,
    PaperOrigin,
    ParsedKeyword,
    ParsedPaper,
    UpsertResult,
)
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.permissions import PermissionDeniedError
from radar.domain.raw_records import RawRecord, RawSource, compress_raw
from radar.domain.scope import ScopeSettings
from radar.domain.staging import StagingItem, StagingKind
from radar.domain.storage_keys import RAW_CONTENT_TYPE, raw_object_key
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import INGEST_WRITE_LOCK_KEY, Database, SessionLock
from radar.repo.ingest_staging import (
    create_staged_papers,
    delete_staged_papers,
    delete_staged_papers_of_kind,
    list_staged_papers,
)
from radar.repo.papers import update_paper_keywords, upsert_by_external_id
from radar.repo.raw_fetches import create_raw_fetches, list_latest_raw_fetches
from radar.repo.weekly_runs import (
    create_weekly_run,
    update_weekly_run_fetch_finished,
    update_weekly_run_published,
)
from radar.services import reparse as reparse_module
from radar.services.reparse import ReparseResult, WeeklyUpdateInProgressError, reparse_papers
from tests.database import APP_ROLE, TestDatabase, migrate
from tests.openalex_cassette import ISSN_L, cassette_works, work_bytes

T1 = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)
T2 = T1 + timedelta(days=7)
SCOPE = ScopeSettings(frozenset({ISSN_L}))
RULES = ExclusionRulesConfig(
    rules=(ExclusionRule(reason="editors_comments", title_starts_with=("Editor's Comments",)),)
)
NO_RULES = ExclusionRulesConfig(rules=())
SYSTEM = SystemViewer()
MEMBER = MemberViewer(member_id=uuid.UUID("018f0000-0000-7000-8000-0000000000a1"), is_admin=False)
ALLOWED_WRITES = {"ingest_staging", "paper", "external_id", "paper_abstract", "paper_keyword"}
_WRITE_SQL = re.compile(
    r"\b(?:INSERT\s+INTO|DELETE\s+FROM|MERGE\s+INTO)\s+\"?(\w+)\"?"
    r"|\bUPDATE\s+\"?(\w+)\"?(?:\s+(?:AS\s+)?\w+)?\s+SET\b"
    r"|\bTRUNCATE\s+(?:TABLE\s+)?\"?(\w+)\"?",
    re.IGNORECASE,
)


class TrackingDatabase(Database):
    """記下開著的交易數、開過幾個交易，以及（開啟記錄時）執行過的每一句 SQL。

    1.9 起 reparse 的交易都走 `session_lock` 交出的 `SessionLock.transaction()`，不呼叫
    `transaction()`；所以兩條路都要算：`opened` 含 `transaction()`、`session_lock` 拿鎖的那個
    短交易、每個 `SessionLock.transaction()`；`open_transactions` 是此刻開著的
    `transaction()` 與 `SessionLock.transaction()`（拿鎖的短交易在交出鎖之前就結束了）。
    """

    def __init__(self, url: str) -> None:
        super().__init__(url, connect_timeout=5, pool_size=2)
        self.open_transactions = 0
        self.opened = 0
        self.statements: list[str] = []
        event.listen(self._engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        self.statements.append(statement)

    @contextmanager
    def counting(self) -> Iterator[None]:
        """區塊算一個開著的交易。"""
        self.opened += 1
        self.open_transactions += 1
        try:
            yield
        finally:
            self.open_transactions -= 1

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.counting(), super().transaction() as connection:
            yield connection

    @contextmanager
    def session_lock(self, key: int) -> Iterator[SessionLock | None]:
        self.opened += 1  # 拿鎖本身是一個短交易
        with super().session_lock(key) as lock:
            yield None if lock is None else TrackingSessionLock(lock, self)


class TrackingSessionLock(SessionLock):
    """包住真的 `SessionLock`：交易照樣由它開（同一條持鎖的連線、先確認鎖還在），
    同時記進 `TrackingDatabase` 的計數。只轉交 `transaction()`，不用基底類別的狀態。
    """

    def __init__(self, inner: SessionLock, database: TrackingDatabase) -> None:
        self._inner = inner
        self._database = database

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self._database.counting(), self._inner.transaction() as connection:
            yield connection


class GuardedStore(FakeObjectStore):
    """每次 get 記下鍵與當下開著的交易數。"""

    def __init__(self, database: TrackingDatabase) -> None:
        super().__init__()
        self.database = database
        self.gets: list[str] = []
        self.open_during_gets: list[int] = []

    def get(self, key: str) -> bytes:
        self.gets.append(key)
        self.open_during_gets.append(self.database.open_transactions)
        return super().get(key)


@pytest.fixture
def reparse_db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(reparse_db: TestDatabase) -> TrackingDatabase:
    return TrackingDatabase(reparse_db.app_url)


@pytest.fixture
def store(database: TrackingDatabase) -> GuardedStore:
    return GuardedStore(database)


def _works(count: int) -> list[dict[str, Any]]:
    """有摘要、有關鍵字、有引用的前 `count` 篇（依 ID 排序，順序固定）。"""
    works = [
        work
        for work in cassette_works()
        if work["abstract_inverted_index"] and work["keywords"] and work["referenced_works"]
    ]
    works.sort(key=lambda work: work["id"])
    assert len(works) >= count
    return works[:count]


def _key(work: dict[str, Any]) -> str:
    return str(work["id"]).rsplit("/", 1)[-1]


def _seed_raw(
    database: Database, store: FakeObjectStore, key: str, content: bytes, fetched_at: datetime
) -> RawRecord:
    record = RawRecord(RawSource.OPENALEX, key, content, fetched_at)
    store.objects[raw_object_key("openalex", record.sha256)] = compress_raw(content)
    store.content_types[raw_object_key("openalex", record.sha256)] = RAW_CONTENT_TYPE
    with database.transaction() as connection:
        create_raw_fetches(connection, [record])
    return record


def _old_parse(work: dict[str, Any], fetched_at: datetime) -> ParsedPaper:
    """模擬「還沒加 referenced_works、cited_by_count、關鍵字、摘要」的舊解析函式。"""
    parsed = parse_openalex_work(work_bytes(work), fetched_at=fetched_at)
    return _changed(
        parsed,
        referenced_works=(),
        cited_by_count=None,
        keywords=(),
        abstract=None,
        abstract_source=None,
    )


def _changed(paper: ParsedPaper, **changes: object) -> ParsedPaper:
    """改幾個欄位後重新驗證（`model_copy` 不驗證，不用它）。"""
    return ParsedPaper.model_validate({**paper.model_dump(), **changes})


def _seed_paper(database: Database, paper: ParsedPaper) -> UpsertResult:
    with database.transaction() as connection:
        return upsert_by_external_id(
            connection, paper, origin=PaperOrigin.TRACKED, excluded_reason=None, scope=SCOPE
        )


def _rows(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(sql, params).fetchall()


def _reparse(
    database: Database,
    store: FakeObjectStore,
    *,
    rules: ExclusionRulesConfig = RULES,
    batch_size: int = 2,
    actor: Any = SYSTEM,
) -> ReparseResult:
    return reparse_papers(
        actor, database, store, scope=SCOPE, exclusion_rules=rules, batch_size=batch_size
    )


def _snapshot(db: TestDatabase) -> dict[str, list[tuple[Any, ...]]]:
    return {
        "paper": _rows(
            db,
            "SELECT id, title, referenced_works, cited_by_count, source_hash, excluded_reason,"
            " openalex_fetched_at, updated_at FROM paper ORDER BY id",
        ),
        "abstract": _rows(
            db, "SELECT paper_id, source, content, is_current FROM paper_abstract ORDER BY 1, 2"
        ),
        "keyword": _rows(db, "SELECT paper_id, source, key FROM paper_keyword ORDER BY 1, 2, 3"),
        "external_id": _rows(db, "SELECT paper_id, scheme, value_norm FROM external_id ORDER BY 1"),
    }


# --- 只動已匯入的 ---------------------------------------------------------------------------


def test_only_known_papers_are_fetched_and_updated(
    reparse_db: TestDatabase, database: TrackingDatabase, store: GuardedStore
) -> None:
    works = _works(5)
    records = [_seed_raw(database, store, _key(w), work_bytes(w), T1) for w in works]
    known = works[:3]
    for work in known:
        _seed_paper(database, _old_parse(work, T1))
    assert _rows(reparse_db, "SELECT count(*) FROM paper_abstract") == [(0,)]

    database.statements.clear()
    result = _reparse(database, store, batch_size=2)

    assert result == ReparseResult(raw_keys=5, not_in_database=2, staged=3, updated=3, stale=0)
    # 只 GET 已匯入的 3 篇，而且 GET 的時候沒有開著的交易。
    assert sorted(store.gets) == sorted(
        raw_object_key("openalex", record.sha256) for record in records[:3]
    )
    assert store.open_during_gets == [0, 0, 0]
    # 沒有新增論文；欄位、摘要、關鍵字都照新的解析結果寫好。
    assert _rows(reparse_db, "SELECT count(*) FROM paper") == [(3,)]
    for work in known:
        parsed = parse_openalex_work(work_bytes(work), fetched_at=T1)
        (row,) = _rows(
            reparse_db,
            "SELECT p.id, p.referenced_works, p.cited_by_count, p.source_hash FROM paper p"
            " JOIN external_id e ON e.paper_id = p.id"
            " WHERE e.scheme = 'openalex' AND e.value_norm = %s",
            _key(work),
        )
        paper_id, referenced_works, cited_by_count, source_hash = row
        assert tuple(referenced_works) == parsed.referenced_works != ()
        assert cited_by_count == work["cited_by_count"]
        assert source_hash == parsed.source_hash
        assert _rows(
            reparse_db,
            "SELECT source, content, is_current FROM paper_abstract WHERE paper_id = %s",
            paper_id,
        ) == [("openalex", parsed.abstract, True)]
        assert {
            key
            for (key,) in _rows(
                reparse_db,
                "SELECT key FROM paper_keyword WHERE paper_id = %s AND source = 'openalex'",
                paper_id,
            )
        } == {keyword.key for keyword in parsed.keywords}
    assert _rows(reparse_db, "SELECT count(*) FROM ingest_staging") == [(0,)]

    # 只寫允許的表，沒有任何一句提到快照。
    written: set[str] = set()
    for statement in database.statements:
        assert "snapshot" not in statement.lower(), statement
        for match in _WRITE_SQL.finditer(statement):
            written.add(next(group for group in match.groups() if group).lower())
    assert written <= ALLOWED_WRITES, written
    assert {"ingest_staging", "paper", "paper_abstract", "paper_keyword"} <= written


def test_running_twice_keeps_counts(
    reparse_db: TestDatabase, database: TrackingDatabase, store: GuardedStore
) -> None:
    works = _works(3)
    for work in works:
        _seed_raw(database, store, _key(work), work_bytes(work), T1)
        _seed_paper(database, _old_parse(work, T1))

    first = _reparse(database, store)
    before = _snapshot(reparse_db)
    second = _reparse(database, store)

    assert first.updated == second.updated == 3
    after = _snapshot(reparse_db)
    assert {name: len(rows) for name, rows in after.items()} == {
        name: len(rows) for name, rows in before.items()
    }
    assert after["abstract"] == before["abstract"]
    assert after["keyword"] == before["keyword"]


def test_reparse_records_source_changes(
    reparse_db: TestDatabase, database: TrackingDatabase, store: GuardedStore
) -> None:
    # ADR-0028 決定第 6 點：reparse 經同一個寫入函式，指紋變了也寫 source_changed_at。
    same, changed = _works(2)
    for work in (same, changed):
        _seed_raw(database, store, _key(work), work_bytes(work), T1)
    # 一篇和原始資料解析出來的一樣（指紋不變）；一篇是舊解析（沒有摘要，指紋不同）。
    _seed_paper(database, parse_openalex_work(work_bytes(same), fetched_at=T1))
    _seed_paper(database, _old_parse(changed, T1))

    result = _reparse(database, store)

    assert result.updated == 2
    changed_at = dict(
        _rows(
            reparse_db,
            "SELECT e.value_norm, p.source_changed_at FROM paper p"
            " JOIN external_id e ON e.paper_id = p.id AND e.scheme = 'openalex'",
        )
    )
    assert changed_at[_key(same)] is None
    assert changed_at[_key(changed)] is not None


# --- 壞資料 ---------------------------------------------------------------------------------


def test_bad_raw_data_is_skipped_and_counted(
    reparse_db: TestDatabase, database: TrackingDatabase, store: GuardedStore
) -> None:
    works = _works(6)
    keys = [_key(work) for work in works]
    for work in works:
        _seed_raw(database, store, _key(work), work_bytes(work), T1)
        _seed_paper(database, _old_parse(work, T1))
    # 0：物件不見；1：內容和 SHA-256 對不上；2：解析失敗；3：解析出的 ID 是別篇；4：gzip 壞掉。
    missing = _seed_raw(database, store, keys[0], work_bytes(works[0]) + b" ", T2)
    del store.objects[raw_object_key("openalex", missing.sha256)]
    mismatch = _seed_raw(database, store, keys[1], work_bytes(works[1]) + b"  ", T2)
    store.objects[raw_object_key("openalex", mismatch.sha256)] = compress_raw(b'{"id": "x"}')
    _seed_raw(database, store, keys[2], b'{"id": "https://openalex.org/W1", "title": 5}', T2)
    _seed_raw(database, store, keys[3], work_bytes(works[5]), T2)
    corrupt = _seed_raw(database, store, keys[4], work_bytes(works[4]) + b"   ", T2)
    store.objects[raw_object_key("openalex", corrupt.sha256)] = b"not gzip at all"
    before = _snapshot(reparse_db)

    with capture_logs() as logs:
        result = _reparse(database, store)

    assert (
        result.missing_object,
        result.hash_mismatch,
        result.parse_error,
        result.id_mismatch,
    ) == (1, 2, 1, 1)
    assert (result.raw_keys, result.staged, result.updated) == (6, 1, 1)
    warnings = [log for log in logs if log["log_level"] == "warning"]
    assert sorted((log["external_key"], log["reason"]) for log in warnings) == sorted(
        [
            (keys[0], "missing_object"),
            (keys[1], "hash_mismatch"),
            (keys[2], "parse_error"),
            (keys[3], "id_mismatch"),
            (keys[4], "hash_mismatch"),
        ]
    )
    assert all(set(log) == {"event", "log_level", "external_key", "reason"} for log in warnings)
    # 壞掉的 5 篇維持原樣；第 6 篇照常更新。
    after = _snapshot(reparse_db)
    assert len(after["paper"]) == 6
    changed = [new for old, new in zip(before["paper"], after["paper"], strict=True) if old != new]
    assert len(changed) == 1
    assert changed[0][2] != []  # referenced_works 補上了


# --- 套用中途失敗 ---------------------------------------------------------------------------


def test_failure_while_applying_rolls_everything_back(
    reparse_db: TestDatabase,
    database: TrackingDatabase,
    store: GuardedStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    works = _works(4)
    for work in works:
        _seed_raw(database, store, _key(work), work_bytes(work), T1)
        _seed_paper(database, _old_parse(work, T1))
    before = _snapshot(reparse_db)
    real_upsert = upsert_by_external_id
    calls = 0

    def failing_upsert(*args: Any, **kwargs: Any) -> UpsertResult:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("測試：寫到第 3 筆失敗")
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(reparse_module, "upsert_by_external_id", failing_upsert)
    with pytest.raises(RuntimeError, match="第 3 筆"):
        _reparse(database, store)

    assert _snapshot(reparse_db) == before
    assert _rows(reparse_db, "SELECT kind, count(*) FROM ingest_staging GROUP BY kind") == [
        ("reparse", 4)
    ]

    # 下一次執行一開始就清掉上次留下的暫存列，正常跑完。
    monkeypatch.setattr(reparse_module, "upsert_by_external_id", real_upsert)
    result = _reparse(database, store)
    assert result.updated == 4
    assert _rows(reparse_db, "SELECT count(*) FROM ingest_staging") == [(0,)]


# --- 比較舊的資料 ---------------------------------------------------------------------------


def test_older_raw_data_is_stale(
    reparse_db: TestDatabase, database: TrackingDatabase, store: GuardedStore
) -> None:
    (work,) = _works(1)
    _seed_raw(database, store, _key(work), work_bytes(work), T1)
    # 論文上已經是比較新的資料（例如之後單篇查詢寫進來的）。
    newer = _changed(_old_parse(work, T2), abstract="Newer abstract.", abstract_source="openalex")
    created = _seed_paper(database, newer)
    with database.transaction() as connection:
        update_paper_keywords(connection, created.paper_id, KeywordSource.OPENALEX, [])
    before = _snapshot(reparse_db)

    result = _reparse(database, store)

    assert (result.staged, result.updated, result.stale) == (1, 0, 1)
    assert _snapshot(reparse_db) == before


# --- 排除規則改了之後 -----------------------------------------------------------------------


def test_exclusion_rules_follow_the_config(
    reparse_db: TestDatabase, database: TrackingDatabase, store: GuardedStore
) -> None:
    works = _works(2)
    works[0]["title"] = "Editor’s Comments: Rethinking platforms"
    for work in works:
        _seed_raw(database, store, _key(work), work_bytes(work), T1)
        _seed_paper(database, _old_parse(work, T1))

    _reparse(database, store, rules=RULES)
    assert sorted(_rows(reparse_db, "SELECT excluded_reason FROM paper"), key=str) == sorted(
        [("editors_comments",), (None,)], key=str
    )

    _reparse(database, store, rules=NO_RULES)
    assert _rows(reparse_db, "SELECT excluded_reason FROM paper") == [(None,), (None,)]
    assert _rows(reparse_db, "SELECT count(*) FROM paper") == [(2,)]  # 標記不刪除


# --- 身分不對 -------------------------------------------------------------------------------


def test_member_cannot_reparse(database: TrackingDatabase, store: GuardedStore) -> None:
    with pytest.raises(PermissionDeniedError):
        _reparse(database, store, actor=MEMBER)

    assert database.opened == 0
    assert database.statements == []
    assert store.gets == []


def test_batch_size_must_be_positive(database: TrackingDatabase, store: GuardedStore) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        _reparse(database, store, batch_size=0)
    assert database.opened == 0


def test_tracking_database_counts_the_lock_transactions(
    database: TrackingDatabase, store: GuardedStore
) -> None:
    # 替身要真的數到 reparse 走的 `session_lock`／`SessionLock.transaction()`，上面的
    # `open_during_gets`、`opened == 0` 才有作用（K8）。空資料庫的 reparse：拿鎖 1 個、
    # 檢查每週更新＋清暫存 1 個、讀第一批 1 個、套用 1 個。
    assert _reparse(database, store) == ReparseResult()
    assert database.opened == 4
    assert database.open_transactions == 0


def test_tracking_session_lock_counts_while_the_transaction_is_open(
    database: TrackingDatabase,
) -> None:
    # 交易進行中 `open_transactions` 是 1、離開後回 0，例外離開也一樣；
    # 少了 `counting()` 的加減，GET 時的零交易斷言就又永遠成立（K8，Codex 低）。
    with database.session_lock(INGEST_WRITE_LOCK_KEY) as lock:
        assert lock is not None
        with lock.transaction():
            assert database.open_transactions == 1
        assert database.open_transactions == 0
        seen: list[int] = []

        def fail_inside() -> None:
            with lock.transaction():
                seen.append(database.open_transactions)
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            fail_inside()
        assert seen == [1]
        assert database.open_transactions == 0
    assert database.opened == 3


def test_empty_database_does_nothing(database: TrackingDatabase, store: GuardedStore) -> None:
    assert _reparse(database, store) == ReparseResult()
    assert store.gets == []


# --- repo：每個外部鍵最新的 raw_fetch、暫存表 -----------------------------------------------


def test_list_latest_raw_fetches_pages_by_external_key(
    database: TrackingDatabase, store: GuardedStore
) -> None:
    older = _seed_raw(database, store, "W2", b'{"v": 1}', T1)
    newer = _seed_raw(database, store, "W2", b'{"v": 2}', T2)
    first = _seed_raw(database, store, "W1", b'{"v": 3}', T1)
    last = _seed_raw(database, store, "W3", b'{"v": 4}', T1)
    assert older.sha256 != newer.sha256

    with database.transaction() as connection:
        page1 = list_latest_raw_fetches(
            SYSTEM, connection, RawSource.OPENALEX, after_external_key=None, limit=2
        )
        page2 = list_latest_raw_fetches(
            SYSTEM, connection, RawSource.OPENALEX, after_external_key="W2", limit=2
        )

    assert [(ref.external_key, ref.sha256, ref.fetched_at) for ref in page1] == [
        ("W1", first.sha256, T1),
        ("W2", newer.sha256, T2),
    ]
    assert [(ref.external_key, ref.sha256) for ref in page2] == [("W3", last.sha256)]
    with pytest.raises(TypeError), database.transaction() as connection:
        list_latest_raw_fetches(
            MEMBER,  # type: ignore[arg-type]
            connection,
            RawSource.OPENALEX,
            after_external_key=None,
            limit=1,
        )


def test_staging_round_trip_and_keyset(reparse_db: TestDatabase, database: Database) -> None:
    works = _works(3)
    papers = [parse_openalex_work(work_bytes(w), fetched_at=T1) for w in works]
    batch = uuid.uuid7()
    other = uuid.uuid7()
    items = [
        StagingItem(external_key=_key(w), paper=p, excluded_reason=reason)
        for w, p, reason in zip(works, papers, ["erratum", None, None], strict=True)
    ]

    with database.transaction() as connection:
        assert (
            create_staged_papers(connection, batch_id=batch, kind=StagingKind.REPARSE, items=items)
            == 3
        )
        create_staged_papers(connection, batch_id=other, kind=StagingKind.REPARSE, items=items[:1])
    with database.transaction() as connection:
        page1 = list_staged_papers(SYSTEM, connection, batch, after_id=None, limit=2)
        page2 = list_staged_papers(SYSTEM, connection, batch, after_id=page1[-1].id, limit=2)

    staged = page1 + page2
    assert len(page1) == 2
    assert len(page2) == 1
    assert [row.paper for row in staged] == papers
    assert [row.excluded_reason for row in staged] == ["erratum", None, None]
    assert [row.external_key for row in staged] == [_key(w) for w in works]
    (row,) = _rows(
        reparse_db,
        "SELECT source, source_hash, fetched_at FROM ingest_staging WHERE batch_id = %s"
        " ORDER BY id LIMIT 1",
        batch,
    )
    assert row == ("openalex", papers[0].source_hash, T1)

    with database.transaction() as connection:
        assert delete_staged_papers(connection, batch) == 3
        assert delete_staged_papers_of_kind(connection, StagingKind.REPARSE) == 1
    assert _rows(reparse_db, "SELECT count(*) FROM ingest_staging") == [(0,)]


def test_staged_parsed_json_is_revalidated(reparse_db: TestDatabase, database: Database) -> None:
    (work,) = _works(1)
    paper = parse_openalex_work(work_bytes(work), fetched_at=T1)
    batch = uuid.uuid7()
    with database.transaction() as connection:
        create_staged_papers(
            connection,
            batch_id=batch,
            kind=StagingKind.REPARSE,
            items=[StagingItem(external_key=_key(work), paper=paper, excluded_reason=None)],
        )
    with reparse_db.connect(APP_ROLE) as connection:
        connection.execute(
            "UPDATE ingest_staging SET parsed = jsonb_set(parsed, '{title}', '\"  \"')"
        )

    with pytest.raises(ValueError, match="title"), database.transaction() as connection:
        list_staged_papers(SYSTEM, connection, batch, after_id=None, limit=10)


def test_sha256_of_seeded_objects_matches_rows(
    reparse_db: TestDatabase, database: TrackingDatabase, store: GuardedStore
) -> None:
    # 前提檢查：測試自己造的原始資料，物件與 raw_fetch 對得上（壞資料測試才有意義）。
    (work,) = _works(1)
    record = _seed_raw(database, store, _key(work), work_bytes(work), T1)

    assert _rows(reparse_db, "SELECT sha256 FROM raw_fetch") == [
        (hashlib.sha256(work_bytes(work)).hexdigest(),)
    ]
    assert record.sha256 == hashlib.sha256(work_bytes(work)).hexdigest()


# --- 重疊執行、識別碼衝突、不該新增論文 -----------------------------------------------------


def test_staged_rows_removed_by_another_run_roll_back(
    reparse_db: TestDatabase,
    database: TrackingDatabase,
    store: GuardedStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    works = _works(3)
    for work in works:
        _seed_raw(database, store, _key(work), work_bytes(work), T1)
        _seed_paper(database, _old_parse(work, T1))
    before = _snapshot(reparse_db)
    real_list = list_staged_papers
    removed = False

    def list_after_other_run(*args: Any, **kwargs: Any) -> Any:
        # 模擬另一個 reparse 一開始刪掉這一批的部分暫存列（已經 commit）。
        nonlocal removed
        if not removed:
            removed = True
            with reparse_db.connect(APP_ROLE) as other:
                other.execute(
                    "DELETE FROM ingest_staging"
                    " WHERE id = (SELECT id FROM ingest_staging ORDER BY id LIMIT 1)"
                )
        return real_list(*args, **kwargs)

    monkeypatch.setattr(reparse_module, "list_staged_papers", list_after_other_run)
    with pytest.raises(RuntimeError, match="暫存列數和放進去的不一樣"):
        _reparse(database, store)

    assert removed
    assert _snapshot(reparse_db) == before


def test_identifier_conflict_is_counted(
    reparse_db: TestDatabase, database: TrackingDatabase, store: GuardedStore
) -> None:
    (work,) = _works(1)
    parsed = _old_parse(work, T1)
    assert parsed.doi is not None
    # A 掛著這篇的 OpenAlex ID、沒有 DOI；B 掛著這篇的 DOI 和另一個 OpenAlex ID。
    # 識別碼分屬兩篇：兩篇都不寫，計進 identifiers_split（ADR-0020 更新 2026-11-22；
    # 原本是更新 B、conflicts 計 1）。
    paper_a = _seed_paper(database, _changed(parsed, doi=None))
    paper_b = _seed_paper(database, _changed(parsed, openalex_id="W999999999"))
    _seed_raw(database, store, _key(work), work_bytes(work), T1)
    before = _full_rows(reparse_db)

    with capture_logs() as logs:
        result = _reparse(database, store)

    assert _full_rows(reparse_db) == before
    assert (result.updated, result.identifiers_split) == (0, 1)
    assert _rows(reparse_db, "SELECT count(*) FROM paper") == [(2,)]
    [split] = [log for log in logs if log["event"].startswith("DOI 與 OpenAlex ID 屬於兩篇")]
    assert (split["doi_paper_id"], split["openalex_paper_id"]) == (
        str(paper_b.paper_id),
        str(paper_a.paper_id),
    )
    [done] = [log for log in logs if log["event"] == "重新解析結束"]
    assert done["identifiers_split"] == 1


def _full_rows(db: TestDatabase) -> dict[str, list[tuple[Any, ...]]]:
    """論文四張表的整列（逐欄比對寫入前後用）。"""
    return {
        table: _rows(db, f"SELECT * FROM {table} ORDER BY id")
        for table in ("paper", "external_id", "paper_abstract", "paper_keyword")
    }


# 每張表用哪一欄判斷「屬於這篇論文」：paper 看 id，其他三張看 paper_id。
_PAPER_ID_COLUMN = {
    "paper": "id",
    "external_id": "paper_id",
    "paper_abstract": "paper_id",
    "paper_keyword": "paper_id",
}


def _rows_of_papers(
    db: TestDatabase, paper_ids: Collection[uuid.UUID]
) -> dict[str, list[dict[str, Any]]]:
    """論文四張表裡屬於這些論文的整列（用欄位名稱篩，逐欄比對寫入前後用）。"""
    with db.connect(APP_ROLE) as connection:
        cursor = connection.cursor(row_factory=dict_row)
        return {
            table: [
                row
                for row in cursor.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                if row[column] in paper_ids
            ]
            for table, column in _PAPER_ID_COLUMN.items()
        }


def test_split_identifiers_row_is_skipped_and_the_rest_is_applied(
    reparse_db: TestDatabase, database: TrackingDatabase, store: GuardedStore
) -> None:
    # 一批 3 篇：第 1 篇的 DOI 與 OpenAlex ID 分屬兩篇（略過、兩篇都不動），另外 2 篇照常更新。
    works = _works(3)
    parsed = _old_parse(works[0], T1)
    paper_a = _seed_paper(database, _changed(parsed, doi=None))
    paper_b = _seed_paper(database, _changed(parsed, openalex_id="W999999999"))
    others = [_seed_paper(database, _old_parse(work, T1)).paper_id for work in works[1:]]
    for work in works:
        _seed_raw(database, store, _key(work), work_bytes(work), T1)
    # A、B 各自先有不同的 OpenAlex 關鍵字；第 1 篇的原始資料帶的是另一組。漏掉略過時，`paper_id`
    # 是 DOI 那篇（B），B 的關鍵字會被這筆整批覆寫。
    source_keys = {
        keyword.key for keyword in parse_openalex_work(work_bytes(works[0]), fetched_at=T1).keywords
    }
    with database.transaction() as connection:
        for paper_id, key in ((paper_a.paper_id, "paper-a-own"), (paper_b.paper_id, "paper-b-own")):
            update_paper_keywords(
                connection,
                paper_id,
                KeywordSource.OPENALEX,
                [ParsedKeyword(key=key, display_name=key.replace("-", " ").title())],
            )
    assert source_keys
    assert source_keys.isdisjoint({"paper-a-own", "paper-b-own"})
    keywords = "SELECT * FROM paper_keyword WHERE paper_id = %s ORDER BY source, key"
    keywords_a = _rows(reparse_db, keywords, paper_a.paper_id)
    keywords_b = _rows(reparse_db, keywords, paper_b.paper_id)
    assert len(keywords_a) == len(keywords_b) == 1
    split_ids = [paper_a.paper_id, paper_b.paper_id]
    before = _rows_of_papers(reparse_db, split_ids)
    assert all(before[table] for table in ("paper", "external_id", "paper_keyword"))

    with capture_logs() as logs:
        result = _reparse(database, store)

    assert _rows(reparse_db, keywords, paper_a.paper_id) == keywords_a
    assert _rows(reparse_db, keywords, paper_b.paper_id) == keywords_b
    after = _rows_of_papers(reparse_db, split_ids)
    assert after == before
    assert result == ReparseResult(raw_keys=3, staged=3, updated=2, stale=0, identifiers_split=1)
    for work, paper_id in zip(works[1:], others, strict=True):
        assert _rows(reparse_db, "SELECT referenced_works FROM paper WHERE id = %s", paper_id) == [
            (list(parse_openalex_work(work_bytes(work), fetched_at=T1).referenced_works),)
        ]
    [done] = [log for log in logs if log["event"] == "重新解析結束"]
    assert (done["identifiers_split"], done["updated"]) == (1, 2)


def test_same_scheme_mismatch_row_is_skipped_and_the_rest_is_applied(
    reparse_db: TestDatabase, database: TrackingDatabase, store: GuardedStore
) -> None:
    # 一批 3 篇：第 1 篇用 OpenAlex ID 找到 P，原始資料的 DOI 沒有主人，而 P 已經有另一個 DOI
    # （同一種識別碼對不上：略過、P 不動，ADR-0020 更新 2026-11-22 第二節），另外 2 篇照常更新。
    works = _works(3)
    parsed = _old_parse(works[0], T1)
    assert parsed.doi is not None
    paper_p = _seed_paper(database, _changed(parsed, doi="10.9999/paper-p-own-doi"))
    others = [_seed_paper(database, _old_parse(work, T1)).paper_id for work in works[1:]]
    for work in works:
        _seed_raw(database, store, _key(work), work_bytes(work), T1)
    source_keys = {
        keyword.key for keyword in parse_openalex_work(work_bytes(works[0]), fetched_at=T1).keywords
    }
    with database.transaction() as connection:
        update_paper_keywords(
            connection,
            paper_p.paper_id,
            KeywordSource.OPENALEX,
            [ParsedKeyword(key="paper-p-own", display_name="Paper P Own")],
        )
    assert source_keys
    assert "paper-p-own" not in source_keys
    keywords = "SELECT * FROM paper_keyword WHERE paper_id = %s ORDER BY source, key"
    keywords_p = _rows(reparse_db, keywords, paper_p.paper_id)
    assert len(keywords_p) == 1
    before = _rows_of_papers(reparse_db, [paper_p.paper_id])
    assert all(before[table] for table in ("paper", "external_id", "paper_keyword"))

    with capture_logs() as logs:
        result = _reparse(database, store)

    assert _rows(reparse_db, keywords, paper_p.paper_id) == keywords_p
    assert _rows_of_papers(reparse_db, [paper_p.paper_id]) == before
    assert result == ReparseResult(
        raw_keys=3, staged=3, updated=2, stale=0, identifiers_split=0, same_scheme_mismatch=1
    )
    assert _rows(
        reparse_db, "SELECT count(*) FROM external_id WHERE value_norm = %s", parsed.doi
    ) == [(0,)]
    for work, paper_id in zip(works[1:], others, strict=True):
        assert _rows(reparse_db, "SELECT referenced_works FROM paper WHERE id = %s", paper_id) == [
            (list(parse_openalex_work(work_bytes(work), fetched_at=T1).referenced_works),)
        ]
    [mismatch] = [log for log in logs if log["event"].startswith("論文已經有同一種、不同值")]
    assert (mismatch["paper_id"], mismatch["scheme"]) == (str(paper_p.paper_id), "doi")
    assert (mismatch["stored"], mismatch["incoming"]) == ("10.9999/paper-p-own-doi", parsed.doi)
    [done] = [log for log in logs if log["event"] == "重新解析結束"]
    assert (done["same_scheme_mismatch"], done["identifiers_split"], done["updated"]) == (1, 0, 2)


def test_creating_a_paper_while_applying_rolls_back(
    reparse_db: TestDatabase,
    database: TrackingDatabase,
    store: GuardedStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    works = _works(2)
    for work in works:
        _seed_raw(database, store, _key(work), work_bytes(work), T1)
    _seed_paper(database, _old_parse(works[0], T1))
    before = _snapshot(reparse_db)

    def everything_known(_viewer: Any, _connection: Any, ids: Any) -> frozenset[Any]:
        return frozenset(ids)

    monkeypatch.setattr(reparse_module, "find_known_external_ids", everything_known)
    with pytest.raises(RuntimeError, match="不該新增論文"):
        _reparse(database, store)

    assert _rows(reparse_db, "SELECT count(*) FROM paper") == [(1,)]
    assert _snapshot(reparse_db) == before


# --- 每週更新正在抓取 -----------------------------------------------------------------------


def test_refuses_while_weekly_update_is_fetching(
    reparse_db: TestDatabase, database: TrackingDatabase, store: GuardedStore
) -> None:
    # 有一筆 fetching 的 weekly_run：丟錯、什麼都不寫（連上次留下的暫存列都不清）、不讀物件。
    works = _works(2)
    for work in works:
        _seed_raw(database, store, _key(work), work_bytes(work), T1)
        _seed_paper(database, _old_parse(work, T1))
    leftover = StagingItem(
        external_key=_key(works[0]),
        paper=parse_openalex_work(work_bytes(works[0]), fetched_at=T1),
        excluded_reason=None,
    )
    with database.transaction() as connection:
        create_staged_papers(
            connection, batch_id=uuid.uuid7(), kind=StagingKind.REPARSE, items=[leftover]
        )
        run_id = create_weekly_run(connection)
    assert run_id is not None
    before = _snapshot(reparse_db)
    database.statements.clear()

    with pytest.raises(WeeklyUpdateInProgressError) as caught:
        _reparse(database, store)

    assert caught.value.weekly_run_id == run_id
    assert str(run_id) in str(caught.value)
    assert "等它發佈或失敗後再跑 reparse" in str(caught.value)
    assert _snapshot(reparse_db) == before
    assert _rows(reparse_db, "SELECT kind, count(*) FROM ingest_staging GROUP BY kind") == [
        ("reparse", 1)
    ]
    assert store.gets == []
    for statement in database.statements:
        assert _WRITE_SQL.search(statement) is None, statement

    # 發佈之後就能跑：清掉留下的暫存列、照常更新。
    with database.transaction() as connection:
        assert update_weekly_run_fetch_finished(connection, run_id)
        assert update_weekly_run_published(connection, run_id)
    result = _reparse(database, store)
    assert result.updated == 2
    assert _rows(reparse_db, "SELECT count(*) FROM ingest_staging") == [(0,)]
