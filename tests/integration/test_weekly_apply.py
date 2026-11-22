"""每週更新的套用段（`apply_weekly_update`、`advance_weekly_update`）對真的 PostgreSQL＋替身
（`FakePaperSource`、`FakeObjectStore`、`FakeHeartbeat`；Story 1.9、ADR-0026）。

第一次套用（快照 1）、中途失敗整個退回（兩種失敗點）、已有快照 1 時第二輪失敗、已發佈的再套用＝
略過、內容相同的下一輪（快照 2、指定 0 篇）、補指定快照（範圍內沒有著落的、指向失敗快照的）、
還沒抓完與不存在、身分不對、中斷後續跑到套用、準備套用失敗、並行的第二個套用等第一個結束後略過；
以及套用時只寫允許的表、心跳送出時沒有開著的交易、repo 的讀者身分與 `snapshot` 的約束。

Story 1.11（ADR-0028）：第二輪整本重掃、有變動與只有引用數／關鍵字改、抓完未套用時被寫入較新資料
（AR-36 第四個）、一直失敗到上限標 `failed`、失敗後下一週開新的一筆、套用一直失敗、額度停下不算
失敗、又排入一次、上限參數不對。

OpenAlex 內容只用錄好的回應（`tests/openalex_cassette.py`，CC0）與這裡自己造的假資料；每個測試
一個全新、已遷移的資料庫，用應用程式角色（`radar_app`）連線。
"""

import re
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import Connection, event
from structlog.testing import capture_logs

from radar.adapters.fake import FakeHeartbeat, FakeObjectStore, FakePaperSource
from radar.adapters.fake.jobs import FakeJobQueue, QueuedJob
from radar.domain.domain_config import (
    ExclusionRule,
    ExclusionRulesConfig,
    Journal,
    JournalsConfig,
    OpenAlexQuota,
)
from radar.domain.jobs import EMBED_PAPERS, PLAN_LLM_BATCHES, JobSpec
from radar.domain.papers import KeywordSource, PaperOrigin, ParsedKeyword
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.permissions import PermissionDeniedError
from radar.domain.raw_records import JournalQuery, RawRecord, RawSource
from radar.domain.scope import ScopeSettings
from radar.domain.snapshot import SnapshotStatus
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.domain.weekly_run import PageCounts
from radar.repo.db import Database
from radar.repo.ingest_staging import delete_staged_papers, list_staged_papers
from radar.repo.papers import (
    update_paper_keywords,
    update_papers_first_snapshot,
    upsert_by_external_id,
)
from radar.repo.snapshots import get_snapshot_of_weekly_run
from radar.repo.weekly_runs import (
    find_latest_weekly_run,
    get_weekly_run,
    upsert_weekly_run_journal_page,
)
from radar.services import weekly_update as weekly_update_module
from radar.services.ingest import JournalFetchError
from radar.services.weekly_update import (
    WeeklyApplyResult,
    WeeklyFetchResult,
    WeeklyFetchSetup,
    advance_weekly_update,
    apply_weekly_update,
    open_and_run_weekly_update,
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


def test_apply_reads_staging_in_batches_of_batch_size(
    weekly_db: TestDatabase, database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 低記憶體主機靠分批讀暫存列：205 列、每批 50 → 非空的批次是 50、50、50、50、5，
    # 每次都用 batch_size 當上限、游標往前推，最後一次讀到空的才停（Codex 補審 C3）。
    run_id = _fetched_run(database)
    assert _staged_count(weekly_db, run_id) == 205
    batches: list[tuple[int, uuid.UUID | None, int]] = []

    def recording(*args: Any, **kwargs: Any) -> Any:
        staged = list_staged_papers(*args, **kwargs)
        batches.append((kwargs["limit"], kwargs["after_id"], len(staged)))
        return staged

    monkeypatch.setattr(weekly_update_module, "list_staged_papers", recording)
    result = _apply(database, run_id, batch_size=50)

    assert [size for _limit, _after, size in batches] == [50, 50, 50, 50, 5, 0]
    assert {limit for limit, _after, _size in batches} == {50}
    cursors = [after for _limit, after, _size in batches]
    assert cursors[0] is None
    assert None not in cursors[1:]
    assert len(set(cursors)) == len(cursors)
    assert (result.staged, result.created) == (205, 205)


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


def test_failure_after_deleting_staging_rolls_everything_back(
    weekly_db: TestDatabase, database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 故障點在套用尾段：已經指定 first_snapshot_id、發佈快照、真的刪掉暫存列之後才丟錯，
    # 仍然整個退回（Codex 補審 C2）。
    run_id = _fetched_run(database)
    staged = _staged_count(weekly_db, run_id)
    before = _paper_tables(weekly_db)
    touched_before = _backdate(weekly_db, run_id)
    heartbeat = FakeHeartbeat()
    deleted: list[int] = []

    def delete_then_fail(connection: Connection, batch_id: uuid.UUID) -> int:
        deleted.append(delete_staged_papers(connection, batch_id))
        # 這時同一個交易裡：快照已發佈、論文已指到它、暫存列已刪光。
        [(status, number)] = connection.exec_driver_sql("SELECT status, number FROM snapshot").all()
        assert (status, number) == ("published", 1)
        assigned = connection.exec_driver_sql(
            "SELECT count(*) FROM paper WHERE first_snapshot_id IS NOT NULL"
        ).scalar_one()
        assert assigned == staged
        raise _ApplyBrokenError("secret-detail-in-message")

    with monkeypatch.context() as patch:
        patch.setattr(weekly_update_module, "delete_staged_papers", delete_then_fail)
        with capture_logs() as logs, pytest.raises(_ApplyBrokenError):
            _apply(database, run_id, heartbeat)

    # 真的刪掉了（不是在刪之前就丟錯）。
    assert deleted == [staged]
    # 論文四張表、快照、weekly_run 狀態、暫存列都是套用前的樣子；沒有心跳。
    assert _paper_tables(weekly_db) == before
    assert _snapshots(weekly_db) == []
    status, finished_at, updated_at = _run_row(weekly_db, run_id)
    assert (status, finished_at is not None) == ("fetching", True)
    assert updated_at > touched_before
    assert _staged_count(weekly_db, run_id) == staged
    assert heartbeat.sent == []
    [error] = [log for log in logs if log["log_level"] == "error"]
    assert (error["event"], error["error"]) == ("每週更新套用失敗，已整個退回", "_ApplyBrokenError")
    assert "secret-detail-in-message" not in str(logs)

    # 解除故障再跑一次：快照編號仍是 1，結果和一次就成功的相同。
    result = _apply(database, run_id, heartbeat)

    [(snapshot_id, number, snapshot_run, snapshot_status)] = _snapshots(weekly_db)
    assert (number, snapshot_run, snapshot_status) == (1, run_id, "published")
    [(as_of,)] = _rows(weekly_db, "SELECT as_of FROM snapshot")
    assert result == WeeklyApplyResult(
        weekly_run_id=run_id,
        snapshot_id=snapshot_id,
        snapshot_number=1,
        as_of=as_of,
        staged=staged,
        created=staged,
        first_snapshot_assigned=staged,
    )
    assert _count(weekly_db, "paper") == staged
    assert _rows(
        weekly_db, "SELECT count(*) FROM paper WHERE first_snapshot_id = %s", snapshot_id
    ) == [(staged,)]
    assert _run_row(weekly_db, run_id)[0] == "published"
    assert _staged_count(weekly_db, run_id) == 0
    assert heartbeat.sent == ["weekly-update"]


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


def _rows_of_papers(db: TestDatabase, paper_ids: list[uuid.UUID]) -> dict[str, list[Any]]:
    """這幾篇論文在四張表裡的整列（逐欄比對寫入前後用）。"""
    return {
        "paper": _rows(db, "SELECT * FROM paper WHERE id = ANY(%s) ORDER BY id", paper_ids),
        **{
            table: _rows(
                db, f"SELECT * FROM {table} WHERE paper_id = ANY(%s) ORDER BY id", paper_ids
            )
            for table in ("external_id", "paper_abstract", "paper_keyword")
        },
    }


def test_split_identifiers_row_is_skipped_and_the_rest_is_applied(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # 暫存的第 1 篇：DOI 屬於 B、OpenAlex ID 屬於 A（兩篇都是別的路寫的、追蹤範圍外，所以
    # 補指定快照也不會碰到它們）。這一筆略過、兩篇都不動；第 2 篇照常新增；快照照樣發佈
    # （ADR-0020 更新 2026-11-22）。
    staged_work = _work(1)
    paper_a_work = _work(1, issn_l=OTHER_ISSN, title="Paper A")
    paper_a_work["doi"] = None
    paper_b_work = _work(61, issn_l=OTHER_ISSN, title="Paper B")
    paper_b_work["doi"] = staged_work["doi"]
    # 略過的那筆帶自己的關鍵字；A、B 各自先有不同的 OpenAlex 關鍵字。漏掉略過時，`paper_id`
    # 是 DOI 那篇（B），B 的關鍵字會被這筆整批覆寫。
    staged_work["keywords"] = [
        {"id": "https://openalex.org/keywords/staged-only", "display_name": "Staged only"}
    ]
    paper_a = _insert_paper(database, paper_a_work)
    paper_b = _insert_paper(database, paper_b_work)
    with database.transaction() as connection:
        for paper_id, key in ((paper_a, "paper-a-own"), (paper_b, "paper-b-own")):
            update_paper_keywords(
                connection,
                paper_id,
                KeywordSource.OPENALEX,
                [ParsedKeyword(key=key, display_name=key.replace("-", " ").title())],
            )
    keywords = "SELECT * FROM paper_keyword WHERE paper_id = %s ORDER BY source, key"
    keywords_a = _rows(weekly_db, keywords, paper_a)
    keywords_b = _rows(weekly_db, keywords, paper_b)
    assert len(keywords_a) == len(keywords_b) == 1
    before = _rows_of_papers(weekly_db, [paper_a, paper_b])
    run_id = _fetched_run(database, T2, misq=_misq_book(T2, [staged_work, _work(2)]), io=[[]])

    with capture_logs() as logs:
        result = _apply(database, run_id)

    assert _rows(weekly_db, keywords, paper_a) == keywords_a
    assert _rows(weekly_db, keywords, paper_b) == keywords_b
    assert _rows_of_papers(weekly_db, [paper_a, paper_b]) == before
    assert result == WeeklyApplyResult(
        weekly_run_id=run_id,
        snapshot_id=result.snapshot_id,
        snapshot_number=1,
        as_of=result.as_of,
        staged=2,
        created=1,
        first_snapshot_assigned=1,
        identifiers_split=1,
    )
    assert _title_of(weekly_db, _key(_work(2))) == "Synthetic study 2"
    assert _count(weekly_db, "paper") == 3
    [(_id, number, _run, status)] = _snapshots(weekly_db)
    assert (number, status) == (1, "published")
    [split] = [log for log in logs if log["event"].startswith("DOI 與 OpenAlex ID 屬於兩篇")]
    assert (split["doi_paper_id"], split["openalex_paper_id"]) == (str(paper_b), str(paper_a))
    [done] = [log for log in logs if log["event"] == "每週更新已套用並發佈快照"]
    assert (done["identifiers_split"], done["created"], done["updated"]) == (1, 1, 0)


def test_same_scheme_mismatch_row_is_skipped_and_the_rest_is_applied(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # 暫存的第 1 篇：DOI 屬於 P，OpenAlex ID 沒有主人，而 P 已經有另一個 OpenAlex ID（P 是別的路
    # 寫的、追蹤範圍外，補指定快照也不會碰到它）。這一筆略過、P 不動；第 2 篇照常新增；快照照樣
    # 發佈（ADR-0020 更新 2026-11-22 第二節）。
    staged_work = _work(1)
    staged_work["keywords"] = [
        {"id": "https://openalex.org/keywords/staged-only", "display_name": "Staged only"}
    ]
    paper_p_work = _work(62, issn_l=OTHER_ISSN, title="Paper P")
    paper_p_work["doi"] = staged_work["doi"]
    paper_p = _insert_paper(database, paper_p_work)
    with database.transaction() as connection:
        update_paper_keywords(
            connection,
            paper_p,
            KeywordSource.OPENALEX,
            [ParsedKeyword(key="paper-p-own", display_name="Paper P Own")],
        )
    keywords = "SELECT * FROM paper_keyword WHERE paper_id = %s ORDER BY source, key"
    keywords_p = _rows(weekly_db, keywords, paper_p)
    assert len(keywords_p) == 1
    before = _rows_of_papers(weekly_db, [paper_p])
    run_id = _fetched_run(database, T2, misq=_misq_book(T2, [staged_work, _work(2)]), io=[[]])

    with capture_logs() as logs:
        result = _apply(database, run_id)

    assert _rows(weekly_db, keywords, paper_p) == keywords_p
    assert _rows_of_papers(weekly_db, [paper_p]) == before
    assert result == WeeklyApplyResult(
        weekly_run_id=run_id,
        snapshot_id=result.snapshot_id,
        snapshot_number=1,
        as_of=result.as_of,
        staged=2,
        created=1,
        first_snapshot_assigned=1,
        same_scheme_mismatch=1,
    )
    assert _title_of(weekly_db, _key(_work(2))) == "Synthetic study 2"
    assert _count(weekly_db, "paper") == 2
    assert _rows(
        weekly_db,
        "SELECT count(*) FROM external_id WHERE scheme = 'openalex' AND value_norm = %s",
        _key(staged_work),
    ) == [(0,)]
    [(_id, number, _run, status)] = _snapshots(weekly_db)
    assert (number, status) == (1, "published")
    [mismatch] = [log for log in logs if log["event"].startswith("論文已經有同一種、不同值")]
    assert (mismatch["paper_id"], mismatch["scheme"]) == (str(paper_p), "openalex")
    assert (mismatch["stored"], mismatch["incoming"]) == (_key(paper_p_work), _key(staged_work))
    [done] = [log for log in logs if log["event"] == "每週更新已套用並發佈快照"]
    assert (done["same_scheme_mismatch"], done["identifiers_split"]) == (1, 0)
    assert (done["created"], done["updated"]) == (1, 0)


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


# --- Story 1.11：第二輪整本重掃、變動偵測、舊的不蓋掉新的 -----------------------------------------


def test_second_round_rescans_every_journal_from_the_first_page(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    first = _fetched_run(database)
    _apply(database, first)
    [(snapshot_1, *_rest)] = _snapshots(weekly_db)
    papers = _count(weekly_db, "paper")
    # 第二輪：MISQ 多一篇新的；其餘內容相同（抓取時間是第二輪的）。
    second = _start(database)
    source = FakePaperSource(
        {MISQ_ISSN: _misq_book(T2, [*_misq_works(), _work(6)]), ISSN_L: _cassette_book(T2)},
        clock=lambda: NOW,
    )
    _fetch(database, source, second)

    # 每本期刊都從第一頁送（cursor None）；查詢只有 ISSN-L＋起始年（不用「只給有更新的」篩選）。
    assert [(request.query.issn_l, request.cursor is None) for request in source.requests] == [
        (MISQ_ISSN, True),
        (MISQ_ISSN, False),
        (ISSN_L, True),
        (ISSN_L, False),
        (ISSN_L, False),
    ]
    assert {request.query for request in source.requests} == {
        JournalQuery(MISQ_ISSN, 2016),
        JournalQuery(ISSN_L, 2016),
    }

    result = _apply(database, second)

    assert (result.snapshot_number, result.created, result.updated) == (2, 1, papers)
    assert result.first_snapshot_assigned == 1
    assert _count(weekly_db, "paper") == papers + 1
    # 新論文寫進快照 2 的 first_snapshot_id；已有的仍是快照 1。
    first_snapshots = dict(
        _rows(
            weekly_db,
            "SELECT e.value_norm, p.first_snapshot_id FROM paper p"
            " JOIN external_id e ON e.paper_id = p.id AND e.scheme = 'openalex'",
        )
    )
    new_key = _key(_work(6))
    assert first_snapshots.pop(new_key) == result.snapshot_id
    assert set(first_snapshots.values()) == {snapshot_1}


def _changed_by_key(db: TestDatabase) -> dict[str, tuple[Any, ...]]:
    """OpenAlex ID →（source_hash, source_changed_at, cited_by_count）。"""
    rows = _rows(
        db,
        "SELECT e.value_norm, p.source_hash, p.source_changed_at, p.cited_by_count FROM paper p"
        " JOIN external_id e ON e.paper_id = p.id AND e.scheme = 'openalex'",
    )
    return {row[0]: row[1:] for row in rows}


def test_changes_to_abstract_retraction_or_bibliography_are_recorded(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    first = _fetched_run(database)
    _apply(database, first)
    before = _changed_by_key(weekly_db)
    assert all(changed_at is None for _hash, changed_at, _cited in before.values())

    # 第二輪：A 摘要改、B 撤稿、C 卷號改；D 只改被引用次數與關鍵字；E 不變。
    abstract_changed, retracted, volume_changed, citations_only, unchanged = _misq_works()
    abstract_changed["abstract_inverted_index"] = {"Rewritten": [0], "abstract": [1]}
    retracted["is_retracted"] = True
    volume_changed["biblio"]["volume"] = "99"
    citations_only["cited_by_count"] = 4321
    citations_only["keywords"] = [
        {"id": "https://openalex.org/keywords/brand-new-topic", "display_name": "Brand new topic"}
    ]
    works = [abstract_changed, retracted, volume_changed, citations_only, unchanged]
    second = _fetched_run(database, T2, misq=_misq_book(T2, works))

    with capture_logs() as logs:
        result = _apply(database, second)

    after = _changed_by_key(weekly_db)
    changed_keys = {_key(work) for work in (abstract_changed, retracted, volume_changed)}
    assert result.changed == 3
    assert result.updated == len(after)
    for key, (source_hash, changed_at, _cited) in after.items():
        if key in changed_keys:
            assert source_hash != before[key][0]
            assert changed_at is not None
        else:
            # 只改引用數／關鍵字的 D、不變的 E、I&O 錄好的那些：指紋不變、沒有變動時間。
            assert (source_hash, changed_at) == (before[key][0], None)
    # D 的被引用次數與 OpenAlex 關鍵字照樣換成新的。
    d_key = _key(citations_only)
    assert after[d_key][2] == 4321
    assert _rows(
        weekly_db,
        "SELECT k.key FROM paper_keyword k JOIN external_id e ON e.paper_id = k.paper_id"
        " WHERE e.scheme = 'openalex' AND e.value_norm = %s AND k.source = 'openalex'",
        d_key,
    ) == [("brand-new-topic",)]
    [done] = [log for log in logs if log["event"] == "每週更新已套用並發佈快照"]
    assert done["changed"] == 3


def test_newer_data_written_after_fetch_survives_the_apply(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # AR-36 第四個並行情境：每週更新抓完（暫存是 T1）、還沒套用時，另一條路（單篇查詢／成員匯入）
    # 用 T1+1h 寫入同一篇較新的資料；套用之後論文上仍是較新的那一份，stale 計一筆。
    run_id = _fetched_run(database, T1, misq=_misq_book(T1, [_work(1)]), io=[[]])
    newer_at = T1 + timedelta(hours=1)
    paper_id = _insert_paper(database, _work(1, title="Written meanwhile"), at=newer_at)

    result = _apply(database, run_id)

    assert (result.staged, result.stale, result.created, result.updated) == (1, 1, 0, 0)
    assert result.changed == 0
    assert _rows(
        weekly_db,
        "SELECT title, openalex_fetched_at, first_snapshot_id FROM paper WHERE id = %s",
        paper_id,
    ) == [("Written meanwhile", newer_at, result.snapshot_id)]
    assert _count(weekly_db, "paper") == 1


# --- Story 1.11：失敗上限 -----------------------------------------------------------------------


class _BrokenJournalSource(FakePaperSource):
    """`broken_issn` 那本期刊每次請求都丟 `PaperSourceError`（OpenAlex 一直回錯誤）。"""

    def __init__(self, *args: Any, broken_issn: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.broken_issn = broken_issn

    def fetch_journal_page(self, query: JournalQuery, cursor: str | None) -> Any:
        if query.issn_l == self.broken_issn:
            self.fail_once(query.issn_l, self._page_index(query.issn_l, cursor) + 1)
        return super().fetch_journal_page(query, cursor)


def _setup(source: FakePaperSource, quota: OpenAlexQuota = QUOTA) -> WeeklyFetchSetup:
    return WeeklyFetchSetup(
        source=source,
        store=FakeObjectStore(),
        journals=JOURNALS,
        exclusion_rules=RULES,
        quota=quota,
    )


def _advance_limited(
    database: Database,
    opener: _Opener,
    weekly_run_id: uuid.UUID,
    heartbeat: Callable[[str], None],
    max_failures: int,
    load_scope: Callable[[], ScopeSettings] = lambda: SCOPE,
) -> Any:
    return advance_weekly_update(
        SYSTEM,
        database,
        opener,
        load_scope,
        heartbeat,
        weekly_run_id,
        clock=lambda: NOW,
        max_failures=max_failures,
    )


def _failures(db: TestDatabase, weekly_run_id: uuid.UUID) -> tuple[str, int]:
    [(status, failure_count)] = _rows(
        db, "SELECT status, failure_count FROM weekly_run WHERE id = %s", weekly_run_id
    )
    return str(status), int(failure_count)


def _logged(logs: list[Any], event: str) -> list[Any]:
    return [log for log in logs if log["event"] == event]


def test_repeated_fetch_failures_mark_the_run_failed_at_the_limit(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    first = _fetched_run(database)
    _apply(database, first)
    before = _paper_tables(weekly_db)
    second = _start(database)
    source = _BrokenJournalSource(
        {MISQ_ISSN: _misq_book(T2), ISSN_L: _cassette_book(T2)},
        broken_issn=ISSN_L,
        clock=lambda: NOW,
    )
    opener = _Opener(_setup(source))
    heartbeat = FakeHeartbeat()

    # 第 1、2 次：記一次失敗、警告，仍是 fetching；MISQ 抓好的暫存列還在。
    for attempt in (1, 2):
        with capture_logs() as logs, pytest.raises(JournalFetchError):
            _advance_limited(database, opener, second, heartbeat, max_failures=3)
        assert _failures(weekly_db, second) == ("fetching", attempt)
        assert _staged_count(weekly_db, second) == 5
        [warning] = _logged(logs, "每週更新失敗，已記一次")
        assert warning["log_level"] == "warning"
        assert (warning["weekly_run_id"], warning["failures"], warning["max_failures"]) == (
            str(second),
            attempt,
            3,
        )
        assert not _logged(logs, "每週更新重試到上限，標成失敗")

    # 第 3 次：到上限 → failed、錯誤日誌、這一批暫存列刪光。
    with capture_logs() as logs, pytest.raises(JournalFetchError):
        _advance_limited(database, opener, second, heartbeat, max_failures=3)

    assert _failures(weekly_db, second) == ("failed", 3)
    assert _staged_count(weekly_db, second) == 0
    [error] = _logged(logs, "每週更新重試到上限，標成失敗")
    assert error["log_level"] == "error"
    assert error == {
        "event": "每週更新重試到上限，標成失敗",
        "log_level": "error",
        "weekly_run_id": str(second),
        "failures": 3,
        "max_failures": 3,
        "staged_deleted": 5,
    }
    assert not _logged(logs, "每週更新失敗，已記一次")

    # 全程沒有心跳；已發佈的快照 1 與論文不變。
    assert heartbeat.sent == []
    assert _paper_tables(weekly_db) == before
    assert [(number, run, status) for _id, number, run, status in _snapshots(weekly_db)] == [
        (1, first, "published")
    ]

    # 第 4 次被排入（工作重試或 sweep）：略過，不開 adapter、不碰 source、不再加次數。
    requests = len(source.requests)
    again = _advance_limited(database, opener, second, heartbeat, max_failures=3)

    assert again.fetch.skipped
    assert again.apply is None
    assert len(source.requests) == requests
    assert opener.opened == 3
    assert _failures(weekly_db, second) == ("failed", 3)
    assert heartbeat.sent == []


def test_after_a_failed_run_the_next_week_starts_a_new_one(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    failed = _start(database)
    broken = _BrokenJournalSource(
        {MISQ_ISSN: _misq_book(), ISSN_L: _cassette_book()}, broken_issn=ISSN_L, clock=lambda: NOW
    )
    with pytest.raises(JournalFetchError):
        _advance_limited(database, _Opener(_setup(broken)), failed, FakeHeartbeat(), 1)
    assert _failures(weekly_db, failed) == ("failed", 1)

    # 下一週 cron 再排：建新的一筆 fetching，從頭抓、可以正常套用。
    start = weekly_update(SYSTEM, database, FakeJobQueue().enqueue)
    assert start.created
    assert start.weekly_run_id != failed
    assert _failures(weekly_db, start.weekly_run_id) == ("fetching", 0)
    source = FakePaperSource({MISQ_ISSN: _misq_book(), ISSN_L: _cassette_book()}, clock=lambda: NOW)
    heartbeat = FakeHeartbeat()

    result = _advance_limited(database, _Opener(_setup(source)), start.weekly_run_id, heartbeat, 1)

    # 從頭抓：兩本期刊都從第一頁（cursor None）開始。
    assert [(request.query.issn_l, request.cursor is None) for request in source.requests] == [
        (MISQ_ISSN, True),
        (MISQ_ISSN, False),
        (ISSN_L, True),
        (ISSN_L, False),
        (ISSN_L, False),
    ]
    assert result.apply is not None
    assert (result.apply.snapshot_number, result.apply.staged, result.apply.created) == (
        1,
        205,
        205,
    )
    assert heartbeat.sent == ["weekly-update"]
    # 舊那筆仍是 failed、沒有快照；它的暫存列不會被套用（已刪光，再套用也是略過）。
    assert _failures(weekly_db, failed) == ("failed", 1)
    assert _staged_count(weekly_db, failed) == 0
    assert _apply(database, failed).skipped
    assert [run for _id, _number, run, _status in _snapshots(weekly_db)] == [start.weekly_run_id]


def test_repeated_apply_failures_name_the_row_and_mark_the_run_failed(
    weekly_db: TestDatabase, database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _fetched_run(database)
    _apply(database, first)
    renamed = [_work(1, title="Renamed in round two"), *_misq_works()[1:]]
    second = _fetched_run(database, T2, misq=_misq_book(T2, renamed))
    staged = _staged_count(weekly_db, second)
    before = _paper_tables(weekly_db)
    bad_key = _key(_work(4))
    heartbeat = FakeHeartbeat()

    def broken_row(connection: Connection, paper: Any, **kwargs: Any) -> Any:
        if paper.openalex_id == bad_key:
            raise _ApplyBrokenError("secret-detail-in-message")
        return upsert_by_external_id(connection, paper, **kwargs)

    monkeypatch.setattr(weekly_update_module, "upsert_by_external_id", broken_row)

    def apply_limited() -> WeeklyApplyResult:
        return apply_weekly_update(
            SYSTEM, database, second, scope=SCOPE, send_heartbeat=heartbeat, max_failures=2
        )

    with capture_logs() as logs, pytest.raises(_ApplyBrokenError):
        apply_limited()

    # 看得出是哪一列：只帶 weekly_run_id、外部鍵與錯誤類別，不帶內容。
    [row_log] = _logged(logs, "每週更新套用時這一列寫不進去")
    assert row_log == {
        "event": "每週更新套用時這一列寫不進去",
        "log_level": "warning",
        "weekly_run_id": str(second),
        "external_key": bad_key,
        "error": "_ApplyBrokenError",
    }
    assert "secret-detail-in-message" not in str(logs)
    assert _failures(weekly_db, second) == ("fetching", 1)
    assert _staged_count(weekly_db, second) == staged

    with capture_logs() as logs, pytest.raises(_ApplyBrokenError):
        apply_limited()

    assert _logged(logs, "每週更新套用時這一列寫不進去")[0]["external_key"] == bad_key
    [error] = _logged(logs, "每週更新重試到上限，標成失敗")
    assert (error["failures"], error["max_failures"], error["staged_deleted"]) == (2, 2, staged)
    assert _failures(weekly_db, second) == ("failed", 2)
    assert _staged_count(weekly_db, second) == 0
    # 快照 1 與論文不變（第二輪的改名沒有寫進去）；沒有心跳。
    assert _paper_tables(weekly_db) == before
    assert _title_of(weekly_db, _key(_work(1))) == "Synthetic study 1"
    assert [(number, status) for _id, number, _run, status in _snapshots(weekly_db)] == [
        (1, "published")
    ]
    assert heartbeat.sent == []
    # 再被排入：略過，不再加次數。
    assert apply_limited().skipped
    assert _failures(weekly_db, second) == ("failed", 2)


def test_repeated_load_scope_failures_mark_the_run_failed(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    # 準備套用（讀追蹤範圍）失敗也算一次；到上限標 failed、刪暫存列。
    run_id = _fetched_run(database)
    staged = _staged_count(weekly_db, run_id)
    opener = _Opener(error=AssertionError("已抓完不該開"))
    heartbeat = FakeHeartbeat()

    def broken_scope() -> ScopeSettings:
        raise _ScopeBrokenError("/secret/path/journals.yaml")

    with capture_logs() as logs, pytest.raises(_ScopeBrokenError):
        _advance_limited(database, opener, run_id, heartbeat, 2, broken_scope)

    assert _failures(weekly_db, run_id) == ("fetching", 1)
    [warning] = _logged(logs, "每週更新失敗，已記一次")
    assert (warning["failures"], warning["max_failures"]) == (1, 2)
    assert _staged_count(weekly_db, run_id) == staged

    with capture_logs() as logs, pytest.raises(_ScopeBrokenError):
        _advance_limited(database, opener, run_id, heartbeat, 2, broken_scope)

    assert _failures(weekly_db, run_id) == ("failed", 2)
    assert _staged_count(weekly_db, run_id) == 0
    [error] = _logged(logs, "每週更新重試到上限，標成失敗")
    assert (error["failures"], error["staged_deleted"]) == (2, staged)
    assert _snapshots(weekly_db) == []
    assert heartbeat.sent == []


def test_advance_passes_max_failures_to_the_apply(
    weekly_db: TestDatabase, database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 套用段失敗時用的是 advance 收到的上限（1），不是預設的 8。
    run_id = _fetched_run(database)
    heartbeat = FakeHeartbeat()

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise _ApplyBrokenError("secret-detail-in-message")

    monkeypatch.setattr(weekly_update_module, "upsert_by_external_id", broken)

    with pytest.raises(_ApplyBrokenError):
        _advance_limited(
            database, _Opener(error=AssertionError("已抓完不該開")), run_id, heartbeat, 1
        )

    assert _failures(weekly_db, run_id) == ("failed", 1)
    assert _staged_count(weekly_db, run_id) == 0
    assert _snapshots(weekly_db) == []
    assert heartbeat.sent == []


class _BreakableDelete:
    """`delete_staged_papers` 的替身：`broken` 為真時丟錯（模擬收尾的交易逾時），否則照常刪。"""

    def __init__(self) -> None:
        self.broken = True
        self.calls = 0

    def __call__(self, connection: Connection, batch_id: uuid.UUID) -> int:
        self.calls += 1
        if self.broken:
            raise _ApplyBrokenError("secret-detail-in-message")
        return delete_staged_papers(connection, batch_id)


FINISH_FAILED = "每週更新已到失敗上限，收尾沒做成，之後再試"
ONLY_FINISH = "每週更新已到失敗上限，只做收尾"
MARKED_FAILED = "每週更新重試到上限，標成失敗"


def test_finishing_failed_retries_until_it_succeeds_without_fetching_or_applying(
    weekly_db: TestDatabase, database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex C2：失敗次數先在自己的交易存下，再用另一個交易「改 failed＋刪暫存」。收尾一直失敗時
    # 次數不退回；之後被排入只再試收尾（不抓、不開 adapter、不套用），收尾成功才改成 failed。
    run_id = _start(database)
    source = _BrokenJournalSource(
        {MISQ_ISSN: _misq_book(), ISSN_L: _cassette_book()}, broken_issn=ISSN_L, clock=lambda: NOW
    )
    opener = _Opener(_setup(source))
    heartbeat = FakeHeartbeat()
    delete = _BreakableDelete()
    monkeypatch.setattr(weekly_update_module, "delete_staged_papers", delete)

    # 第 1 次：抓 I&O 失敗、到上限 1；收尾的交易丟錯 → 次數已存下（1），但仍是 fetching、暫存列還在
    # （改 failed 與刪暫存同一個交易，一起退回）。往上丟的仍是原本的抓取錯誤。
    with capture_logs() as logs, pytest.raises(JournalFetchError):
        _advance_limited(database, opener, run_id, heartbeat, 1)

    assert _failures(weekly_db, run_id) == ("fetching", 1)
    assert _staged_count(weekly_db, run_id) == 5
    [warning] = _logged(logs, FINISH_FAILED)
    assert warning == {
        "event": FINISH_FAILED,
        "log_level": "warning",
        "weekly_run_id": str(run_id),
        "error": "_ApplyBrokenError",
    }
    assert not _logged(logs, MARKED_FAILED)
    assert not _logged(logs, "每週更新失敗後更新 updated_at 也失敗")
    assert "secret-detail-in-message" not in str(logs)
    requests, opened = len(source.requests), opener.opened

    # 第 2 次（工作重試或 sweep 補排）：讀到已到上限，只再試收尾；又失敗 → 那個錯誤往上丟
    # （工作層會再重試），不抓、不開 adapter、不再加次數。
    with capture_logs() as logs, pytest.raises(_ApplyBrokenError):
        _advance_limited(database, opener, run_id, heartbeat, 1)

    assert (len(source.requests), opener.opened) == (requests, opened)
    assert _failures(weekly_db, run_id) == ("fetching", 1)
    assert _staged_count(weekly_db, run_id) == 5
    assert [log["event"] for log in logs] == [ONLY_FINISH, FINISH_FAILED]
    assert delete.calls == 2

    # 這時就算已經抓完，套用也不做（已到上限的那一筆不能被套用）：沒有快照、暫存列還在。
    _owner(weekly_db, "UPDATE weekly_run SET fetch_finished_at = now() WHERE id = %s", run_id)
    skipped = apply_weekly_update(
        SYSTEM, database, run_id, scope=SCOPE, send_heartbeat=heartbeat, max_failures=1
    )
    assert skipped == WeeklyApplyResult(weekly_run_id=run_id, skipped=True)
    assert _snapshots(weekly_db) == []
    assert _staged_count(weekly_db, run_id) == 5

    # 第 3 次：收尾成功 → failed、暫存列刪光、錯誤日誌；仍不抓、不套用、不送心跳。
    delete.broken = False
    with capture_logs() as logs:
        done = _advance_limited(database, opener, run_id, heartbeat, 1)

    assert done.fetch == WeeklyFetchResult(weekly_run_id=run_id, skipped=True)
    assert done.apply is None
    assert (len(source.requests), opener.opened) == (requests, opened)
    assert _failures(weekly_db, run_id) == ("failed", 1)
    assert _staged_count(weekly_db, run_id) == 0
    [error] = _logged(logs, MARKED_FAILED)
    assert error == {
        "event": MARKED_FAILED,
        "log_level": "error",
        "weekly_run_id": str(run_id),
        "failures": 1,
        "max_failures": 1,
        "staged_deleted": 5,
    }
    assert _snapshots(weekly_db) == []
    assert heartbeat.sent == []

    # 第 4 次：已經是 failed，照舊略過、不再收尾。
    with capture_logs() as logs:
        assert _advance_limited(database, opener, run_id, heartbeat, 1).fetch.skipped
    assert [log["event"] for log in logs] == ["每週更新不是抓取中，略過"]
    assert delete.calls == 3


def test_next_cron_finishes_a_run_stuck_at_the_limit_then_starts_a_new_one(
    weekly_db: TestDatabase, database: TrackingDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 已到上限、收尾一直沒做成的那一筆仍佔著唯一的 fetching：下一次 cron 先收尾，再建新的一筆。
    stuck = _fetched_run(database)
    staged = _staged_count(weekly_db, stuck)
    _owner(weekly_db, "UPDATE weekly_run SET failure_count = 2 WHERE id = %s", stuck)
    delete = _BreakableDelete()
    monkeypatch.setattr(weekly_update_module, "delete_staged_papers", delete)
    queue = FakeJobQueue()

    # 收尾失敗：錯誤往上丟（cron 的工作會重試），什麼都沒排入、沒有新的一筆。
    with capture_logs() as logs, pytest.raises(_ApplyBrokenError):
        weekly_update(SYSTEM, database, queue.enqueue, max_failures=2)
    assert queue.queued == []
    assert _rows(weekly_db, "SELECT id, status FROM weekly_run") == [(stuck, "fetching")]
    assert [log["event"] for log in logs] == [FINISH_FAILED]

    # 收尾成功：舊的那一筆 failed、暫存列刪光；建新的一筆並排入它。
    delete.broken = False
    with capture_logs() as logs:
        start = weekly_update(SYSTEM, database, queue.enqueue, max_failures=2)

    assert start.created
    assert start.weekly_run_id != stuck
    assert _failures(weekly_db, stuck) == ("failed", 2)
    assert _staged_count(weekly_db, stuck) == 0
    assert _failures(weekly_db, start.weekly_run_id) == ("fetching", 0)
    assert len(queue.queued) == 1
    [error] = _logged(logs, MARKED_FAILED)
    assert (error["weekly_run_id"], error["failures"], error["staged_deleted"]) == (
        str(stuck),
        2,
        staged,
    )

    # 還沒到上限的那一筆照舊：排入同一筆，不收尾。
    again = weekly_update(SYSTEM, database, queue.enqueue, max_failures=2)
    assert (again.weekly_run_id, again.created) == (start.weekly_run_id, False)
    assert delete.calls == 2


class _InterruptingSource(FakePaperSource):
    """MISQ 第 2 頁的請求進來時先呼叫一次 `interrupt`（模擬另一個工作在這時改了那一筆）。"""

    def __init__(self, *args: Any, interrupt: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.interrupt: Callable[[], None] | None = interrupt

    def fetch_journal_page(self, query: JournalQuery, cursor: str | None) -> Any:
        if query.issn_l == MISQ_ISSN and cursor is not None and self.interrupt is not None:
            interrupt, self.interrupt = self.interrupt, None
            interrupt()
        return super().fetch_journal_page(query, cursor)


NOT_WRITABLE = "每週更新已經不能再寫（不是抓取中或已到失敗上限），停下這次抓取"


@pytest.mark.parametrize(
    ("when", "change"),
    [
        ("before_raw", "UPDATE weekly_run SET status = 'failed', failure_count = 3 WHERE id = %s"),
        ("before_raw", "UPDATE weekly_run SET failure_count = 3 WHERE id = %s"),
        (
            "before_stage",
            "UPDATE weekly_run SET status = 'failed', failure_count = 3 WHERE id = %s",
        ),
        ("before_stage", "UPDATE weekly_run SET failure_count = 3 WHERE id = %s"),
    ],
    ids=["failed-before-raw", "limit-before-raw", "failed-before-stage", "limit-before-stage"],
)
def test_fetch_stops_writing_once_the_run_is_failed_or_at_the_limit(
    weekly_db: TestDatabase,
    database: TrackingDatabase,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    change: str,
) -> None:
    # Codex C1：抓到一半時另一個工作把同一筆標成 failed（或次數記到上限、還沒收尾）。之後的頁：
    # 寫原始資料（raw_fetch）與寫暫存＋進度的交易都以「仍是 fetching、還沒到上限」為條件，
    # 不是就整個交易不寫、停下；不算失敗、不送心跳、不丟錯（工作層不會一直重試）。
    run_id = _start(database)

    def interrupt() -> None:
        _owner(weekly_db, change, run_id)

    source: FakePaperSource
    if when == "before_raw":
        source = _InterruptingSource(
            {MISQ_ISSN: _misq_book(), ISSN_L: _cassette_book()},
            interrupt=interrupt,
            clock=lambda: NOW,
        )
    else:
        source = FakePaperSource(
            {MISQ_ISSN: _misq_book(), ISSN_L: _cassette_book()}, clock=lambda: NOW
        )
        original = weekly_update_module._write_raw
        raw_writes: list[int] = []

        def write_then_interrupt(*args: Any) -> None:
            # 第 2 頁的原始資料照常寫好（那時還是 fetching），接著另一個工作改了那一筆。
            original(*args)
            raw_writes.append(1)
            if len(raw_writes) == 2:
                interrupt()

        monkeypatch.setattr(weekly_update_module, "_write_raw", write_then_interrupt)
    store = FakeObjectStore()
    heartbeat = FakeHeartbeat()

    with capture_logs() as logs:
        result = advance_weekly_update(
            SYSTEM,
            database,
            _Opener(WeeklyFetchSetup(source, store, JOURNALS, RULES, QUOTA)),
            lambda: SCOPE,
            heartbeat,
            run_id,
            clock=lambda: NOW,
            max_failures=3,
        )

    # 第 1 頁（3 篇）照常；第 2 頁的請求送出去了（API 呼叫照記），但暫存與進度都沒寫，I&O 沒開始。
    assert result.apply is None
    assert result.fetch == WeeklyFetchResult(
        weekly_run_id=run_id,
        skipped=True,
        journals=("misq",),
        pages=1,
        counts=result.fetch.counts,
    )
    assert result.fetch.counts.fetched == 3
    assert [request.cursor is None for request in source.requests] == [True, False]
    assert _count(weekly_db, "api_call") == 2
    # 第 2 頁的原始資料：在改之前就寫好的（before_stage）留著，改了之後才要寫的（before_raw）沒寫。
    assert _count(weekly_db, "raw_fetch") == (3 if when == "before_raw" else 5)
    assert _staged_count(weekly_db, run_id) == 3
    [(pages, finished_at)] = _rows(
        weekly_db,
        "SELECT pages, finished_at FROM weekly_run_journal WHERE weekly_run_id = %s",
        run_id,
    )
    assert (pages, finished_at) == (1, None)
    # 不算一次失敗：次數維持另一個工作改的 3；沒有錯誤日誌、沒有心跳、沒有快照。
    [(failure_count,)] = _rows(
        weekly_db, "SELECT failure_count FROM weekly_run WHERE id = %s", run_id
    )
    assert failure_count == 3
    [warning] = _logged(logs, NOT_WRITABLE)
    assert (warning["log_level"], warning["weekly_run_id"], warning["journal"]) == (
        "warning",
        str(run_id),
        "misq",
    )
    assert not [log for log in logs if log["log_level"] == "error"]
    assert not _logged(logs, "每週更新失敗，已記一次")
    assert heartbeat.sent == []
    assert _snapshots(weekly_db) == []


def test_quota_stop_is_not_counted_as_a_failure(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    tight = OpenAlexQuota(
        daily_budget_usd=Decimal("0.0001"),
        stop_at_ratio=Decimal("0.5"),
        default_request_cost_usd=Decimal("0.0001"),
    )
    source = FakePaperSource({MISQ_ISSN: _misq_book()}, clock=lambda: NOW)

    with capture_logs() as logs:
        result = _advance_limited(
            database, _Opener(_setup(source, tight)), run_id, FakeHeartbeat(), 1
        )

    assert result.fetch.stopped_for_quota
    assert _failures(weekly_db, run_id) == ("fetching", 0)
    assert not _logged(logs, "每週更新失敗，已記一次")
    assert not _logged(logs, "每週更新重試到上限，標成失敗")


def test_enqueueing_again_reuses_the_fetching_run_and_is_not_a_failure(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    queue = FakeJobQueue()
    run_id = weekly_update(SYSTEM, database, queue.enqueue).weekly_run_id
    # 抓到一半：MISQ 第一頁暫存好了，還有下一頁。
    with database.transaction() as connection:
        upsert_weekly_run_journal_page(
            connection,
            run_id,
            "misq",
            first_page=True,
            next_cursor="fake-cursor:0276-7783:1",
            counts=PageCounts(fetched=3),
        )

    with capture_logs() as logs:
        again = weekly_update(SYSTEM, database, queue.enqueue)

    # 不新增、排入的是同一筆；那個工作已經在排隊，不重複排；不丟錯、不算失敗。
    assert (again.weekly_run_id, again.created) == (run_id, False)
    assert len(queue.queued) == 1
    [queued_log] = _logged(logs, "每週更新已排入抓取")
    assert (queued_log["created"], queued_log["queued"]) == (False, False)
    assert _rows(weekly_db, "SELECT id, status, failure_count FROM weekly_run") == [
        (run_id, "fetching", 0)
    ]
    # 第二筆 fetching 被部分唯一索引擋下。
    with (
        weekly_db.connect(APP_ROLE) as connection,
        pytest.raises(psycopg.errors.UniqueViolation, match="uq_weekly_run_fetching"),
    ):
        connection.execute("INSERT INTO weekly_run (status) VALUES ('fetching')")


def test_bad_max_failures_is_rejected_before_touching_anything(
    database: TrackingDatabase,
) -> None:
    source = FakePaperSource()
    opener = _Opener(error=AssertionError("不該開"))
    heartbeat = FakeHeartbeat()
    run_id = uuid.uuid7()

    for bad in (0, -1, True):
        with pytest.raises(ValueError, match="max_failures"):
            run_weekly_update(
                SYSTEM,
                database,
                source,
                FakeObjectStore(),
                run_id,
                journals=JOURNALS,
                exclusion_rules=RULES,
                quota=QUOTA,
                max_failures=bad,
            )
        with pytest.raises(ValueError, match="max_failures"):
            open_and_run_weekly_update(SYSTEM, database, opener, run_id, max_failures=bad)
        with pytest.raises(ValueError, match="max_failures"):
            _advance_limited(database, opener, run_id, heartbeat, bad)
        with pytest.raises(ValueError, match="max_failures"):
            apply_weekly_update(
                SYSTEM, database, run_id, scope=SCOPE, send_heartbeat=heartbeat, max_failures=bad
            )

    assert database.opened == 0
    assert source.requests == []
    assert opener.opened == 0
    assert heartbeat.sent == []


# --- 發佈後排入向量化（Story 2.8） --------------------------------------------------------------


class _RecordingQueue(FakeJobQueue):
    """排入時記下當下開著的交易數與已送的心跳（要在 commit、心跳之後才排）。"""

    def __init__(self, database: TrackingDatabase, heartbeat: FakeHeartbeat) -> None:
        super().__init__()
        self.database = database
        self.heartbeat = heartbeat
        self.seen: list[tuple[int, list[str]]] = []

    def enqueue(self, job: JobSpec, ids: Mapping[str, uuid.UUID]) -> bool:
        self.seen.append((self.database.open_transactions, list(self.heartbeat.sent)))
        return super().enqueue(job, ids)


def test_publish_enqueues_embeddings_after_commit_and_heartbeat(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _fetched_run(database)
    heartbeat = FakeHeartbeat()
    queue = _RecordingQueue(database, heartbeat)

    with capture_logs() as logs:
        result = apply_weekly_update(
            SYSTEM,
            database,
            run_id,
            scope=SCOPE,
            send_heartbeat=heartbeat,
            enqueue=queue.enqueue,
        )

    assert result.snapshot_number == 1
    # Story 2.9：向量化之後接著排入 Batch 規劃（帶這次的 weekly_run_id）。
    assert queue.queued == [
        QueuedJob(EMBED_PAPERS, {}),
        QueuedJob(PLAN_LLM_BATCHES, {"weekly_run_id": run_id}),
    ]
    assert queue.seen == [(0, ["weekly-update"]), (0, ["weekly-update"])]
    [done] = [log for log in logs if log["event"] == "每週更新發佈後已排入向量化"]
    assert (done["weekly_run_id"], done["queued"]) == (str(run_id), True)


def test_skipped_apply_does_not_enqueue_embeddings(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _fetched_run(database)
    _apply(database, run_id)
    queue = FakeJobQueue()

    result = apply_weekly_update(
        SYSTEM,
        database,
        run_id,
        scope=SCOPE,
        send_heartbeat=FakeHeartbeat(),
        enqueue=queue.enqueue,
    )

    assert result.skipped
    assert queue.queued == []


def test_enqueue_failure_only_warns_and_keeps_the_snapshot(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _fetched_run(database)

    def broken(_job: JobSpec, _ids: Mapping[str, uuid.UUID]) -> bool:
        raise RuntimeError("queue down postgresql://secret@host/db")

    with capture_logs() as logs:
        result = apply_weekly_update(
            SYSTEM,
            database,
            run_id,
            scope=SCOPE,
            send_heartbeat=FakeHeartbeat(),
            enqueue=broken,
        )

    assert result.snapshot_number == 1
    assert _run_row(weekly_db, run_id)[0] == "published"
    [warning] = [log for log in logs if log["event"] == "每週更新發佈後排入向量化失敗"]
    assert warning["log_level"] == "warning"
    assert warning["error"] == "RuntimeError"
    assert "secret" not in str(logs)


def test_advance_passes_enqueue_to_the_apply(
    weekly_db: TestDatabase, database: TrackingDatabase
) -> None:
    run_id = _start(database)
    source = FakePaperSource({MISQ_ISSN: _misq_book(), ISSN_L: _cassette_book()}, clock=lambda: NOW)
    opener = _Opener(
        WeeklyFetchSetup(
            source=source,
            store=FakeObjectStore(),
            journals=JOURNALS,
            exclusion_rules=RULES,
            quota=QUOTA,
        )
    )
    queue = FakeJobQueue()

    result = advance_weekly_update(
        SYSTEM,
        database,
        opener,
        lambda: SCOPE,
        FakeHeartbeat(),
        run_id,
        clock=lambda: NOW,
        enqueue=queue.enqueue,
    )

    assert result.apply is not None
    assert result.apply.snapshot_number == 1
    # Story 2.9：向量化之後接著排入 Batch 規劃。
    assert queue.queued == [
        QueuedJob(EMBED_PAPERS, {}),
        QueuedJob(PLAN_LLM_BATCHES, {"weekly_run_id": run_id}),
    ]
