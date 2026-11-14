"""每週更新的套用段（`apply_weekly_update`、`advance_weekly_update`）對真的 PostgreSQL＋替身
（`FakePaperSource`、`FakeObjectStore`、`FakeHeartbeat`；Story 1.9、ADR-0026）。

第一次套用（快照 1）、中途失敗整個退回（兩種失敗點）、已有快照 1 時第二輪失敗、已發佈的再套用＝
略過、內容相同的下一輪（快照 2、指定 0 篇）、補指定快照（範圍內沒有著落的、指向失敗快照的）、
還沒抓完與不存在、身分不對、中斷後續跑到套用、準備套用失敗、並行的第二個套用等第一個結束後略過；
以及套用時只寫允許的表、心跳送出時沒有開著的交易、repo 的讀者身分與 `snapshot` 的約束。

OpenAlex 內容只用錄好的回應（`tests/openalex_cassette.py`，CC0）與這裡自己造的假資料；每個測試
一個全新、已遷移的資料庫，用應用程式角色（`radar_app`）連線。
"""

import re
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import Connection, event
from structlog.testing import capture_logs

from radar.adapters.fake import FakeHeartbeat, FakeObjectStore, FakePaperSource
from radar.adapters.fake.jobs import FakeJobQueue
from radar.domain.domain_config import (
    ExclusionRule,
    ExclusionRulesConfig,
    Journal,
    JournalsConfig,
    OpenAlexQuota,
)
from radar.domain.papers import KeywordSource, PaperOrigin
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.permissions import PermissionDeniedError
from radar.domain.raw_records import RawRecord, RawSource
from radar.domain.scope import ScopeSettings
from radar.domain.snapshot import SnapshotStatus
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.repo.papers import (
    update_paper_keywords,
    update_papers_first_snapshot,
    upsert_by_external_id,
)
from radar.repo.snapshots import get_snapshot_of_weekly_run
from radar.repo.weekly_runs import find_latest_weekly_run, get_weekly_run
from radar.services import weekly_update as weekly_update_module
from radar.services.ingest import JournalFetchError
from radar.services.weekly_update import (
    WeeklyApplyResult,
    WeeklyFetchSetup,
    advance_weekly_update,
    apply_weekly_update,
    run_weekly_update,
    weekly_update,
)
from tests.database import APP_ROLE, OWNER_ROLE, TestDatabase, migrate
from tests.openalex_cassette import ISSN_L, cassette_pages, cassette_work, work_bytes

NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
T2 = T1 + timedelta(days=7)
SYSTEM = SystemViewer()
MEMBER = MemberViewer(member_id=uuid.UUID("018f0000-0000-7000-8000-0000000000a1"), is_admin=True)
IO = Journal(key="io", name="Information and Organization", issn_l=ISSN_L)
MISQ_ISSN = "0276-7783"
MISQ = Journal(key="misq", name="MIS Quarterly", issn_l=MISQ_ISSN)
OTHER_ISSN = "0960-085X"  # EJIS：這些測試的追蹤範圍外
JOURNALS = JournalsConfig(start_year=2016, journals=(MISQ, IO))
SCOPE = JOURNALS.scope_settings()
QUOTA = OpenAlexQuota(
    daily_budget_usd=Decimal("1.0"),
    stop_at_ratio=Decimal("0.8"),
    default_request_cost_usd=Decimal("0.0001"),
)
RULES = ExclusionRulesConfig(
    rules=(ExclusionRule(reason="editors_comments", title_starts_with=("Editor's Comments",)),)
)
PAPER_TABLES = ("paper", "external_id", "paper_abstract", "paper_keyword")
APPLY_WRITES = {
    "weekly_run",
    "snapshot",
    "paper",
    "external_id",
    "paper_abstract",
    "paper_keyword",
    "ingest_staging",
}
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


class RecordingHeartbeat(FakeHeartbeat):
    """送心跳時記下當下開著的交易數（要在 commit 之後才送）。"""

    def __init__(self, database: TrackingDatabase) -> None:
        super().__init__()
        self.database = database
        self.open_during_calls: list[int] = []

    def __call__(self, check: str) -> None:
        self.open_during_calls.append(self.database.open_transactions)
        super().__call__(check)


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


def _records(works: list[dict[str, Any]], at: datetime = T1) -> list[RawRecord]:
    return [RawRecord(RawSource.OPENALEX, _key(work), work_bytes(work), at) for work in works]


def _cassette_book(at: datetime = T1) -> list[list[RawRecord]]:
    """I&O 錄好的三頁（100＋100＋48 篇；收錄的 article 200 篇）。"""
    return [_records(list(page), at) for page in cassette_pages()]


def _work(
    number: int, *, issn_l: str = MISQ_ISSN, year: int = 2020, title: str | None = None
) -> dict[str, Any]:
    """自己造的一篇（以錄好的一篇為底，換掉 ID、DOI、出版年、期刊、標題）。"""
    work = cassette_work()
    work["id"] = f"https://openalex.org/W9{number:08d}"
    work["doi"] = f"https://doi.org/10.9999/radar-apply.{number}"
    work["publication_year"] = year
    work["primary_location"]["source"]["issn_l"] = issn_l
    work["title"] = work["display_name"] = title or f"Synthetic study {number}"
    return work


def _misq_works() -> list[dict[str, Any]]:
    """MISQ 自己造的 5 篇，其中一篇命中排除規則（進暫存、帶原因）。"""
    return [
        _work(1),
        _work(2),
        _work(3, title="Editor's Comments: The Next Decade"),
        _work(4),
        _work(5),
    ]


def _misq_book(
    at: datetime = T1, works: list[dict[str, Any]] | None = None
) -> list[list[RawRecord]]:
    chosen = works if works is not None else _misq_works()
    return [_records(chosen[:3], at), _records(chosen[3:], at)]


def _articles() -> list[dict[str, Any]]:
    """會被收錄的全部論文：I&O 的 article 加上 MISQ 自己造的。"""
    io = [work for page in cassette_pages() for work in page if work["type"] == "article"]
    return [*io, *_misq_works()]


def _start(database: Database) -> uuid.UUID:
    return weekly_update(SYSTEM, database, FakeJobQueue().enqueue).weekly_run_id


def _fetch(database: Database, source: FakePaperSource, weekly_run_id: uuid.UUID) -> None:
    result = run_weekly_update(
        SYSTEM,
        database,
        source,
        FakeObjectStore(),
        weekly_run_id,
        journals=JOURNALS,
        exclusion_rules=RULES,
        quota=QUOTA,
        clock=lambda: NOW,
    )
    assert result.fetch_finished


def _fetched_run(database: Database, at: datetime = T1, **books: Any) -> uuid.UUID:
    """新的一次每週更新，抓完 MISQ 與 I&O（預設內容）、還沒套用。"""
    run_id = _start(database)
    pages = {
        MISQ_ISSN: books.get("misq", _misq_book(at)),
        ISSN_L: books.get("io", _cassette_book(at)),
    }
    _fetch(database, FakePaperSource(pages, clock=lambda: NOW), run_id)
    return run_id


def _apply(
    database: Database,
    weekly_run_id: uuid.UUID,
    heartbeat: Callable[[str], None] | None = None,
    *,
    batch_size: int = 500,
    actor: Any = SYSTEM,
) -> WeeklyApplyResult:
    return apply_weekly_update(
        actor,
        database,
        weekly_run_id,
        scope=SCOPE,
        send_heartbeat=heartbeat or FakeHeartbeat(),
        batch_size=batch_size,
    )


def _rows(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(sql, params).fetchall()


def _owner(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(OWNER_ROLE) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def _count(db: TestDatabase, table: str) -> int:
    count: int = _rows(db, f"SELECT count(*) FROM {table}")[0][0]
    return count


def _paper_tables(db: TestDatabase) -> dict[str, list[tuple[Any, ...]]]:
    """論文四張表的全部內容（拿掉 `updated_at` 以外都比；排序固定）。"""
    return {
        "paper": _rows(
            db,
            "SELECT id, title, in_scope, excluded_reason, first_snapshot_id, openalex_fetched_at"
            " FROM paper ORDER BY id",
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


def _staged_count(db: TestDatabase, weekly_run_id: uuid.UUID) -> int:
    count: int = _rows(
        db, "SELECT count(*) FROM ingest_staging WHERE batch_id = %s", weekly_run_id
    )[0][0]
    return count


def _run_row(db: TestDatabase, weekly_run_id: uuid.UUID) -> tuple[Any, ...]:
    [row] = _rows(
        db,
        "SELECT status, fetch_finished_at, updated_at FROM weekly_run WHERE id = %s",
        weekly_run_id,
    )
    return row


def _backdate(db: TestDatabase, weekly_run_id: uuid.UUID) -> datetime:
    _owner(
        db,
        "UPDATE weekly_run SET updated_at = now() - interval '1 day' WHERE id = %s",
        weekly_run_id,
    )
    updated_at: datetime = _run_row(db, weekly_run_id)[2]
    return updated_at


def _snapshots(db: TestDatabase) -> list[tuple[Any, ...]]:
    return _rows(db, "SELECT id, number, weekly_run_id, status FROM snapshot ORDER BY number")


def _title_of(db: TestDatabase, openalex_id: str) -> str:
    [(title,)] = _rows(
        db,
        "SELECT p.title FROM paper p JOIN external_id e ON e.paper_id = p.id"
        " WHERE e.scheme = 'openalex' AND e.value_norm = %s",
        openalex_id,
    )
    return str(title)


# --- 第一次套用 -------------------------------------------------------------------------------


def test_first_apply_writes_papers_and_publishes_snapshot_1(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _fetched_run(database)
    staged = _staged_count(weekly_db, run_id)
    assert staged == 205
    assert all(count == 0 for count in map(len, _paper_tables(weekly_db).values()))
    heartbeat = RecordingHeartbeat(database)
    database.statements.clear()

    with capture_logs() as logs:
        # 每批 50 筆：205 列要讀 5 批。
        result = _apply(database, run_id, heartbeat, batch_size=50)

    # 論文篇數＝暫存列數；追蹤範圍內的都指向快照 1。
    assert _count(weekly_db, "paper") == staged
    [(snapshot_id, number, snapshot_run, status)] = _snapshots(weekly_db)
    assert (number, snapshot_run, status) == (1, run_id, "published")
    assert _rows(weekly_db, "SELECT count(*) FROM paper WHERE in_scope") == [(staged,)]
    assert _rows(
        weekly_db, "SELECT count(*) FROM paper WHERE first_snapshot_id = %s", snapshot_id
    ) == [(staged,)]
    [(as_of, created_at)] = _rows(weekly_db, "SELECT as_of, created_at FROM snapshot")
    assert as_of is not None
    # 截至時間用 clock_timestamp()（這一句的時間），一定晚於交易開始時寫的 created_at；
    # 換成 now() 會相等，報表就永遠印「套用 0.0 秒」。
    assert as_of > created_at
    assert result == WeeklyApplyResult(
        weekly_run_id=run_id,
        snapshot_id=snapshot_id,
        snapshot_number=1,
        as_of=as_of,
        staged=staged,
        created=staged,
        first_snapshot_assigned=staged,
    )
    # weekly_run 變 published；這一批暫存列刪光。
    assert _run_row(weekly_db, run_id)[0] == "published"
    assert _staged_count(weekly_db, run_id) == 0
    # 心跳 commit 之後才送、只送一次。
    assert heartbeat.sent == ["weekly-update"]
    assert heartbeat.open_during_calls == [0]
    # 摘要、關鍵字、排除原因都寫好。
    articles = _articles()
    assert _rows(
        weekly_db, "SELECT count(*) FROM paper_abstract WHERE is_current AND source = 'openalex'"
    ) == [(sum(1 for work in articles if work["abstract_inverted_index"]),)]
    assert _rows(weekly_db, "SELECT count(*) FROM paper_keyword WHERE source = 'openalex'") == [
        (sum(len({keyword["id"] for keyword in work["keywords"]}) for work in articles),)
    ]
    assert _rows(
        weekly_db, "SELECT title FROM paper WHERE excluded_reason = 'editors_comments'"
    ) == [("Editor's Comments: The Next Decade",)]
    # 套用時只寫這幾張表。
    assert database.written_tables() == APPLY_WRITES
    [done] = [log for log in logs if log["event"] == "每週更新已套用並發佈快照"]
    assert (done["snapshot_number"], done["staged"], done["created"]) == (1, staged, staged)
    assert isinstance(done["seconds"], float)
    # 有暫存論文時，沒有「0 篇」的警告（1.9 審查第 6 條）。
    assert not [log for log in logs if log["event"] == "這次每週更新沒有任何暫存論文"]


# --- 中途失敗 ---------------------------------------------------------------------------------


class _ApplyBrokenError(Exception):
    """模擬套用中途失敗；訊息帶一段不該進日誌的內容。"""


def _fail_first_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_connection: Connection, _snapshot_id: uuid.UUID) -> int:
        raise _ApplyBrokenError("secret-detail-in-message")

    monkeypatch.setattr(weekly_update_module, "update_papers_first_snapshot", broken)


def _fail_nth_upsert(monkeypatch: pytest.MonkeyPatch, nth: int = 3) -> None:
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == nth:
            raise _ApplyBrokenError("secret-detail-in-message")
        return upsert_by_external_id(*args, **kwargs)

    monkeypatch.setattr(weekly_update_module, "upsert_by_external_id", flaky)


@pytest.mark.parametrize("break_at", [_fail_first_snapshot, _fail_nth_upsert])
def test_failure_midway_rolls_back_everything(
    weekly_db: TestDatabase,
    database: TrackingDatabase,
    monkeypatch: pytest.MonkeyPatch,
    break_at: Callable[[pytest.MonkeyPatch], None],
) -> None:
    run_id = _fetched_run(database)
    staged = _staged_count(weekly_db, run_id)
    before = _paper_tables(weekly_db)
    touched_before = _backdate(weekly_db, run_id)
    heartbeat = FakeHeartbeat()

    with monkeypatch.context() as patch:
        break_at(patch)
        with capture_logs() as logs, pytest.raises(_ApplyBrokenError):
            _apply(database, run_id, heartbeat)

    # 沒有快照；論文四張表和套用前一樣；仍是 fetching、updated_at 更新；暫存列還在；沒有心跳。
    assert _snapshots(weekly_db) == []
    assert _paper_tables(weekly_db) == before
    status, finished_at, updated_at = _run_row(weekly_db, run_id)
    assert (status, finished_at is not None) == ("fetching", True)
    assert updated_at > touched_before
    assert _staged_count(weekly_db, run_id) == staged
    assert heartbeat.sent == []
    [error] = [log for log in logs if log["log_level"] == "error"]
    assert error["event"] == "每週更新套用失敗，已整個退回"
    assert set(error) == {"event", "log_level", "weekly_run_id", "error"}
    assert (error["weekly_run_id"], error["error"]) == (str(run_id), "_ApplyBrokenError")
    assert "secret-detail-in-message" not in str(logs)

    # 再跑一次成功：和一次就成功的結果相同。
    result = _apply(database, run_id, heartbeat)

    assert (result.snapshot_number, result.created, result.staged) == (1, staged, staged)
    [(snapshot_id, number, _run, status)] = _snapshots(weekly_db)
    assert (number, status) == (1, "published")
    assert _count(weekly_db, "paper") == staged
    assert _rows(
        weekly_db, "SELECT count(*) FROM paper WHERE first_snapshot_id = %s", snapshot_id
    ) == [(staged,)]
    assert _staged_count(weekly_db, run_id) == 0
    assert heartbeat.sent == ["weekly-update"]


def test_second_round_failure_keeps_snapshot_1_and_old_titles(
    weekly_db: TestDatabase, database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _fetched_run(database)
    _apply(database, first)
    renamed = [_work(1, title="Renamed in round two"), *_misq_works()[1:]]
    second = _fetched_run(database, T2, misq=_misq_book(T2, renamed))
    before = _paper_tables(weekly_db)

    with monkeypatch.context() as patch:
        _fail_first_snapshot(patch)
        with pytest.raises(_ApplyBrokenError):
            _apply(database, second)

    assert [(number, status) for _id, number, _run, status in _snapshots(weekly_db)] == [
        (1, "published")
    ]
    assert _title_of(weekly_db, _key(_work(1))) == "Synthetic study 1"
    assert _paper_tables(weekly_db) == before
    assert _run_row(weekly_db, second)[0] == "fetching"
    assert _run_row(weekly_db, first)[0] == "published"


# --- 再套用、內容相同的下一輪、補指定快照 ---------------------------------------------------------


def test_reapplying_a_published_run_is_skipped(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _fetched_run(database)
    _apply(database, run_id)
    before = _paper_tables(weekly_db)
    heartbeat = FakeHeartbeat()

    with capture_logs() as logs:
        result = _apply(database, run_id, heartbeat)

    assert result == WeeklyApplyResult(weekly_run_id=run_id, skipped=True)
    assert _paper_tables(weekly_db) == before
    assert len(_snapshots(weekly_db)) == 1
    assert heartbeat.sent == []
    assert [log["event"] for log in logs] == ["每週更新不是抓完待套用，略過套用"]


def test_same_content_next_round_adds_snapshot_2_and_assigns_nothing(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    first = _fetched_run(database)
    _apply(database, first)
    [(snapshot_1, *_rest)] = _snapshots(weekly_db)
    papers = _count(weekly_db, "paper")
    second = _fetched_run(database, T2)

    result = _apply(database, second)

    assert _count(weekly_db, "paper") == papers
    assert (result.snapshot_number, result.created, result.updated) == (2, 0, papers)
    assert result.first_snapshot_assigned == 0
    snapshots = _snapshots(weekly_db)
    assert [(number, run, status) for _id, number, run, status in snapshots] == [
        (1, first, "published"),
        (2, second, "published"),
    ]
    assert _rows(weekly_db, "SELECT DISTINCT first_snapshot_id FROM paper") == [(snapshot_1,)]
    # 被引用次數等欄位照新的覆寫（抓取時間是第二輪的）。
    assert _rows(weekly_db, "SELECT DISTINCT openalex_fetched_at FROM paper") == [(T2,)]


def _insert_paper(database: Database, work: dict[str, Any], at: datetime = T1) -> uuid.UUID:
    """另一條路（成員匯入）寫入的一篇，不經每週更新；`at` 是抓取時間。"""
    parsed = parse_openalex_work(work_bytes(work), fetched_at=at)
    with database.transaction() as connection:
        result = upsert_by_external_id(
            connection, parsed, origin=PaperOrigin.IMPORT, excluded_reason=None, scope=SCOPE
        )
    return result.paper_id


def test_unassigned_and_failed_snapshot_papers_get_the_new_snapshot(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _fetched_run(database)
    in_scope = _insert_paper(database, _work(50))
    out_of_scope = _insert_paper(database, _work(51, issn_l=OTHER_ISSN))
    # 上一次失敗的每週更新留下的快照（1.11 才會寫 failed；這裡用擁有者直接寫）。
    [(failed_run,)] = _owner(
        weekly_db, "INSERT INTO weekly_run (status) VALUES ('failed') RETURNING id"
    )
    [(failed_snapshot,)] = _owner(
        weekly_db,
        "INSERT INTO snapshot (number, weekly_run_id, status) VALUES (7, %s, 'failed')"
        " RETURNING id",
        failed_run,
    )
    on_failed = _insert_paper(database, _work(52))
    _owner(
        weekly_db,
        "UPDATE paper SET first_snapshot_id = %s WHERE id = %s",
        failed_snapshot,
        on_failed,
    )

    result = _apply(database, run_id)

    # 編號接在最大值後面（失敗的 7 也算）。
    assert result.snapshot_number == 8
    first = dict(_rows(weekly_db, "SELECT id, first_snapshot_id FROM paper"))
    assert first[in_scope] == result.snapshot_id
    assert first[on_failed] == result.snapshot_id
    assert first[out_of_scope] is None
    assert result.first_snapshot_assigned == result.staged + 2


def test_older_staged_rows_are_stale_and_keep_newer_keywords(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # 論文表裡已經有 T2 抓的版本（標題、關鍵字、摘要都不同）；暫存的是 T1 抓的同一篇 → STALE，
    # 論文、摘要、關鍵字都不動，計成 stale、不算 updated；快照照樣發佈。
    newer = _work(1, title="Newer title fetched later")
    newer["keywords"] = [
        {"id": "https://openalex.org/keywords/newer-keyword", "display_name": "Newer keyword"}
    ]
    newer["abstract_inverted_index"] = {"Newer": [0], "abstract": [1]}
    paper_id = _insert_paper(database, newer, at=T2)
    with database.transaction() as connection:
        parsed = parse_openalex_work(work_bytes(newer), fetched_at=T2)
        update_paper_keywords(connection, paper_id, KeywordSource.OPENALEX, parsed.keywords)
    keywords = "SELECT source, key, display_name FROM paper_keyword WHERE paper_id = %s"
    abstracts = "SELECT source, is_current, content FROM paper_abstract WHERE paper_id = %s"
    before_keywords = _rows(weekly_db, keywords, paper_id)
    before_abstracts = _rows(weekly_db, abstracts, paper_id)
    assert [display for _source, _key, display in before_keywords] == ["Newer keyword"]
    assert len(before_abstracts) == 1
    run_id = _fetched_run(database, T1, misq=_misq_book(T1, [_work(1)]), io=[[]])

    result = _apply(database, run_id)

    assert (result.staged, result.stale, result.created, result.updated) == (1, 1, 0, 0)
    assert _title_of(weekly_db, _key(newer)) == "Newer title fetched later"
    assert _rows(weekly_db, keywords, paper_id) == before_keywords
    assert _rows(weekly_db, abstracts, paper_id) == before_abstracts
    assert _rows(weekly_db, "SELECT openalex_fetched_at FROM paper WHERE id = %s", paper_id) == [
        (T2,)
    ]
    [(_id, number, _run, status)] = _snapshots(weekly_db)
    assert (number, status) == (1, "published")


# --- 還沒抓完、不存在、身分不對 ---------------------------------------------------------------


def test_unfinished_or_missing_run_is_skipped(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    heartbeat = FakeHeartbeat()

    for weekly_run_id in (run_id, uuid.uuid7()):
        assert _apply(database, weekly_run_id, heartbeat).skipped

    assert _snapshots(weekly_db) == []
    assert _run_row(weekly_db, run_id)[0] == "fetching"
    assert heartbeat.sent == []


def test_member_cannot_apply_or_advance_and_nothing_is_touched(
    database: TrackingDatabase,
) -> None:
    heartbeat = FakeHeartbeat()
    opener = _Opener(error=AssertionError("不該開"))

    with pytest.raises(PermissionDeniedError):
        _apply(database, uuid.uuid7(), heartbeat, actor=MEMBER)
    with pytest.raises(PermissionDeniedError):
        advance_weekly_update(MEMBER, database, opener, _no_scope, heartbeat, uuid.uuid7())

    assert database.opened == 0
    assert heartbeat.sent == []
    assert opener.opened == 0


def test_batch_size_must_be_positive(database: TrackingDatabase) -> None:
    for bad in (0, -1, True):
        with pytest.raises(ValueError, match="batch_size"):
            _apply(database, uuid.uuid7(), batch_size=bad)
    assert database.opened == 0


# --- advance_weekly_update：中斷後續跑到套用、準備套用失敗 ---------------------------------------


class _Opener:
    """`open_setup` 的替身：記下被呼叫幾次；`error` 有值時開的時候就丟。"""

    def __init__(self, setup: WeeklyFetchSetup | None = None, error: Exception | None = None):
        self.setup = setup
        self.error = error
        self.opened = 0

    @contextmanager
    def __call__(self) -> Iterator[WeeklyFetchSetup]:
        self.opened += 1
        if self.error is not None:
            raise self.error
        assert self.setup is not None
        yield self.setup


def _no_scope() -> ScopeSettings:
    raise AssertionError("不該讀追蹤範圍")


def _advance(
    database: Database,
    opener: _Opener,
    weekly_run_id: uuid.UUID,
    heartbeat: Callable[[str], None],
    load_scope: Callable[[], ScopeSettings] = lambda: SCOPE,
) -> Any:
    return advance_weekly_update(
        SYSTEM, database, opener, load_scope, heartbeat, weekly_run_id, clock=lambda: NOW
    )


def test_advance_resumes_after_interruption_then_applies(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    source = FakePaperSource({MISQ_ISSN: _misq_book(), ISSN_L: _cassette_book()}, clock=lambda: NOW)
    source.fail_once(ISSN_L, 2)
    opener = _Opener(
        WeeklyFetchSetup(
            source=source,
            store=FakeObjectStore(),
            journals=JOURNALS,
            exclusion_rules=RULES,
            quota=QUOTA,
        )
    )
    heartbeat = FakeHeartbeat()
    scopes: list[str] = []

    def load_scope() -> ScopeSettings:
        scopes.append("read")
        return SCOPE

    with pytest.raises(JournalFetchError):
        _advance(database, opener, run_id, heartbeat, load_scope)

    assert _snapshots(weekly_db) == []
    assert scopes == []
    assert heartbeat.sent == []

    result = _advance(database, opener, run_id, heartbeat, load_scope)

    assert result.fetch.fetch_finished
    assert result.fetch.journals == ("io",)
    assert result.apply is not None
    assert (result.apply.snapshot_number, result.apply.created) == (1, 205)
    assert scopes == ["read"]
    assert heartbeat.sent == ["weekly-update"]
    assert _run_row(weekly_db, run_id)[0] == "published"
    assert _count(weekly_db, "paper") == 205

    # 已發佈的那一筆被補排：抓取段略過，不讀範圍、不套用、不送心跳。
    again = _advance(database, opener, run_id, heartbeat, _no_scope)

    assert again.fetch.skipped
    assert again.apply is None
    assert heartbeat.sent == ["weekly-update"]
    assert opener.opened == 2


def test_advance_stops_for_quota_without_applying(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    tight = OpenAlexQuota(
        daily_budget_usd=Decimal("0.0001"),
        stop_at_ratio=Decimal("0.5"),
        default_request_cost_usd=Decimal("0.0001"),
    )
    source = FakePaperSource({MISQ_ISSN: _misq_book()}, clock=lambda: NOW)
    opener = _Opener(
        WeeklyFetchSetup(
            source=source,
            store=FakeObjectStore(),
            journals=JOURNALS,
            exclusion_rules=RULES,
            quota=tight,
        )
    )

    result = _advance(database, opener, run_id, FakeHeartbeat(), _no_scope)

    assert result.fetch.stopped_for_quota
    assert result.apply is None
    assert _snapshots(weekly_db) == []


class _ScopeBrokenError(Exception):
    """模擬讀追蹤範圍失敗（journals.yaml 抓完後壞掉）；訊息帶一段不該進日誌的內容。"""


def test_load_scope_failure_touches_the_run_and_raises(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _fetched_run(database)
    touched_before = _backdate(weekly_db, run_id)
    heartbeat = FakeHeartbeat()

    def broken_scope() -> ScopeSettings:
        raise _ScopeBrokenError("/secret/path/journals.yaml")

    with capture_logs() as logs, pytest.raises(_ScopeBrokenError):
        _advance(
            database, _Opener(error=AssertionError("已抓完不該開")), run_id, heartbeat, broken_scope
        )

    assert _run_row(weekly_db, run_id)[0] == "fetching"
    assert _run_row(weekly_db, run_id)[2] > touched_before
    assert _snapshots(weekly_db) == []
    assert heartbeat.sent == []
    [error] = [log for log in logs if log["log_level"] == "error"]
    assert error["event"] == "每週更新準備套用失敗"
    assert (error["weekly_run_id"], error["error"]) == (str(run_id), "_ScopeBrokenError")
    assert "/secret/path" not in str(logs)


# --- 並行的第二個套用 -------------------------------------------------------------------------


def test_concurrent_second_apply_waits_then_skips(
    weekly_db: TestDatabase, database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _fetched_run(database)
    entered = threading.Event()
    release = threading.Event()
    real = update_papers_first_snapshot

    def slow(connection: Connection, snapshot_id: uuid.UUID) -> int:
        entered.set()
        assert release.wait(30)
        return real(connection, snapshot_id)

    monkeypatch.setattr(weekly_update_module, "update_papers_first_snapshot", slow)
    results: dict[str, WeeklyApplyResult] = {}
    errors: list[BaseException] = []

    def run(name: str) -> None:
        try:
            results[name] = _apply(Database(weekly_db.app_url, connect_timeout=5), run_id)
        except BaseException as exc:  # 讓主執行緒看得到
            errors.append(exc)

    first = threading.Thread(target=run, args=("first",))
    first.start()
    assert entered.wait(30)
    second = threading.Thread(target=run, args=("second",))
    second.start()
    second.join(1.0)
    # 第二個卡在 weekly_run 那一列的鎖上，等第一個交易結束。
    assert second.is_alive()
    release.set()
    first.join(30)
    second.join(30)

    assert errors == []
    assert results["first"].snapshot_number == 1
    assert results["second"].skipped
    assert len(_snapshots(weekly_db)) == 1


# --- repo：讀者身分、最新一筆、snapshot 的約束 ----------------------------------------------------


def test_repo_reads_are_for_system_only(database: TrackingDatabase) -> None:
    with database.transaction() as connection:
        with pytest.raises(TypeError):
            get_snapshot_of_weekly_run(MEMBER, connection, uuid.uuid7())  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            find_latest_weekly_run(MEMBER, connection)  # type: ignore[arg-type]


def test_snapshot_of_weekly_run_and_latest_run(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    with database.transaction() as connection:
        assert find_latest_weekly_run(SYSTEM, connection) is None
    first = _fetched_run(database)
    _apply(database, first)
    second = _start(database)

    with database.transaction() as connection:
        latest = find_latest_weekly_run(SYSTEM, connection)
        snapshot = get_snapshot_of_weekly_run(SYSTEM, connection, first)
        none_yet = get_snapshot_of_weekly_run(SYSTEM, connection, second)
        run = get_weekly_run(SYSTEM, connection, first)

    assert latest is not None
    assert latest.id == second
    assert none_yet is None
    assert snapshot is not None
    assert (snapshot.number, snapshot.weekly_run_id, snapshot.status) == (
        1,
        first,
        SnapshotStatus.PUBLISHED,
    )
    assert snapshot.apply_duration is not None
    assert snapshot.apply_duration > timedelta(0)
    assert run is not None
    assert run.status.value == "published"


@pytest.mark.parametrize(
    ("sql", "error"),
    [
        (
            "INSERT INTO snapshot (number, weekly_run_id, status) VALUES (0, %s, 'building')",
            psycopg.errors.CheckViolation,
        ),
        (
            "INSERT INTO snapshot (number, weekly_run_id, status) VALUES (1, %s, 'published')",
            psycopg.errors.CheckViolation,
        ),
        (
            "INSERT INTO snapshot (number, weekly_run_id, status) VALUES (1, %s, 'done')",
            psycopg.errors.CheckViolation,
        ),
        (
            "INSERT INTO snapshot (number, weekly_run_id, status) VALUES (1, %s, 'building'),"
            " (2, %s, 'building')",
            psycopg.errors.UniqueViolation,
        ),
    ],
    ids=["number-zero", "published-without-as-of", "unknown-status", "two-per-run"],
)
def test_snapshot_constraints(
    weekly_db: TestDatabase, database: TrackingDatabase, sql: str, error: type[Exception]
) -> None:
    run_id = _start(database)

    with weekly_db.connect(APP_ROLE) as connection, pytest.raises(error):
        connection.execute(sql, (run_id,) * sql.count("%s"))
