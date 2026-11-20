"""reparse 結束時重判示範站可用（ADR-0052 第 6、9 點，2026-11-21 更新）對真的 PostgreSQL。

排除清單改了之後，reparse 沒有重寫到的論文（沒有原始資料紀錄、`missing_object`、`hash_mismatch`、
`parse_error`、`id_mismatch`、STALE）用存下的授權與出版社欄位經 `is_demo_eligible` 再判一次，
只把 true 改成 false；有重寫到的論文不經這條路；套用與重判同一個交易，中途失敗整個退回、重跑補上。

測試資料全部自己編（`tests/demo_works.py`），不用真的論文。每個測試一個全新、已遷移的資料庫。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from structlog.testing import capture_logs

from radar.adapters.fake import FakeObjectStore
from radar.domain.domain_config import ExclusionRulesConfig
from radar.domain.papers import PaperOrigin, ParsedPaper
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.policy import PublisherExclusions
from radar.domain.raw_records import RawRecord, RawSource, compress_raw
from radar.domain.scope import ScopeSettings
from radar.domain.storage_keys import RAW_CONTENT_TYPE, raw_object_key
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.papers import upsert_by_external_id
from radar.repo.raw_fetches import create_raw_fetches
from radar.services import reparse as reparse_module
from radar.services.reparse import ReparseResult, reparse_papers
from tests.database import APP_ROLE, TestDatabase, migrate
from tests.demo_works import (
    EXCLUDED_PARENT_NAME,
    EXCLUDED_PUBLISHER_NAME,
    OPEN_PUBLISHER_NAME,
    demo_work,
    demo_work_bytes,
)

ISSN = "1471-7727"
T1 = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
T2 = T1 + timedelta(days=7)
SECOND_OPEN_NAME = "Second Open Press"
SECOND_OPEN_ID = "https://openalex.org/P9000000003"
# 寫入時的排除清單：Example Open Press 還可以放。
OLD_EXCLUSIONS = PublisherExclusions(
    ids=frozenset({"P9000000002"}),
    names=frozenset({EXCLUDED_PUBLISHER_NAME, EXCLUDED_PARENT_NAME}),
)
# 改過的排除清單：多排除 Example Open Press（只用名稱）；Second Open Press 仍可以放。
NEW_EXCLUSIONS = PublisherExclusions(
    ids=frozenset({"P9000000002"}),
    names=frozenset({EXCLUDED_PUBLISHER_NAME, EXCLUDED_PARENT_NAME, OPEN_PUBLISHER_NAME}),
)
OLD_SCOPE = ScopeSettings(frozenset({ISSN}), demo_publishers=OLD_EXCLUSIONS)
NEW_SCOPE = ScopeSettings(frozenset({ISSN}), demo_publishers=NEW_EXCLUSIONS)
NO_RULES = ExclusionRulesConfig(rules=())

# 沒有重寫到的幾種情況（ADR-0052 第 9 點「例外」）。
NOT_REWRITTEN = ["no_raw", "missing_object", "hash_mismatch", "parse_error", "id_mismatch", "stale"]


@pytest.fixture
def demo_db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(demo_db: TestDatabase) -> Database:
    return Database(demo_db.app_url, connect_timeout=5, pool_size=2)


def _key(number: int) -> str:
    return f"W8{number:08d}"


def _write(
    database: Database, work: dict[str, Any], *, fetched_at: datetime, scope: ScopeSettings
) -> uuid.UUID:
    paper = parse_openalex_work(demo_work_bytes(work), fetched_at=fetched_at)
    with database.transaction() as connection:
        return upsert_by_external_id(
            connection, paper, origin=PaperOrigin.TRACKED, excluded_reason=None, scope=scope
        ).paper_id


def _seed_raw(
    database: Database, store: FakeObjectStore, key: str, content: bytes, fetched_at: datetime
) -> RawRecord:
    record = RawRecord(RawSource.OPENALEX, key, content, fetched_at)
    store.objects[raw_object_key("openalex", record.sha256)] = compress_raw(content)
    store.content_types[raw_object_key("openalex", record.sha256)] = RAW_CONTENT_TYPE
    with database.transaction() as connection:
        create_raw_fetches(connection, [record])
    return record


def _seed_case(database: Database, store: FakeObjectStore, case: str, number: int) -> uuid.UUID:
    """一篇用舊清單寫成 `demo_eligible = true` 的論文，原始資料準備成 `case` 那種「沒重寫到」。"""
    work = demo_work(number, issn_l=ISSN)
    content = demo_work_bytes(work)
    key = _key(number)
    if case == "stale":
        # 論文上的資料比原始資料新（例如之後單篇查詢寫的）：套用時是 STALE。
        paper_id = _write(database, work, fetched_at=T2, scope=OLD_SCOPE)
        _seed_raw(database, store, key, content, T1)
        return paper_id
    paper_id = _write(database, work, fetched_at=T1, scope=OLD_SCOPE)
    if case == "no_raw":
        return paper_id
    if case == "missing_object":
        record = _seed_raw(database, store, key, content, T2)
        del store.objects[raw_object_key("openalex", record.sha256)]
    elif case == "hash_mismatch":
        record = _seed_raw(database, store, key, content, T2)
        store.objects[raw_object_key("openalex", record.sha256)] = compress_raw(b'{"id": "x"}')
    elif case == "parse_error":
        _seed_raw(database, store, key, b'{"id": "https://openalex.org/W1", "title": 5}', T2)
    elif case == "id_mismatch":
        other = demo_work_bytes(demo_work(number + 500, issn_l=ISSN))
        _seed_raw(database, store, key, other, T2)
    else:
        raise AssertionError(case)
    return paper_id


def _eligible(db: TestDatabase, paper_id: uuid.UUID) -> bool:
    with db.connect(APP_ROLE) as connection:
        [(value,)] = connection.execute(
            "SELECT demo_eligible FROM paper WHERE id = %s", (paper_id,)
        ).fetchall()
    return bool(value)


def _all_rows(db: TestDatabase) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(
            "SELECT id, demo_eligible, primary_license, publisher_ids, publisher_names, updated_at"
            " FROM paper ORDER BY id"
        ).fetchall()


def _reparse(database: Database, store: FakeObjectStore, scope: ScopeSettings) -> ReparseResult:
    return reparse_papers(SystemViewer(), database, store, scope=scope, exclusion_rules=NO_RULES)


@pytest.mark.parametrize("case", NOT_REWRITTEN)
def test_paper_not_rewritten_is_rechecked_and_turned_false(
    demo_db: TestDatabase, database: Database, case: str
) -> None:
    store = FakeObjectStore()
    paper_id = _seed_case(database, store, case, 1)
    # 對照：一篇有重寫到的（新的原始資料），出版社也是 Example Open Press。
    rewritten_work = demo_work(2, issn_l=ISSN)
    rewritten_id = _write(database, rewritten_work, fetched_at=T1, scope=OLD_SCOPE)
    _seed_raw(database, store, _key(2), demo_work_bytes(rewritten_work), T2)
    assert _eligible(demo_db, paper_id) is True
    assert _eligible(demo_db, rewritten_id) is True

    result = _reparse(database, store, NEW_SCOPE)

    assert _eligible(demo_db, paper_id) is False
    assert _eligible(demo_db, rewritten_id) is False  # 由 upsert_by_external_id 寫成 false
    assert result.updated == 1
    # 重判只算沒重寫到的那一篇；重寫到的不經這條路。
    assert (result.demo_rechecked, result.demo_revoked) == (1, 1)


def test_stored_fields_still_eligible_stay_true(demo_db: TestDatabase, database: Database) -> None:
    store = FakeObjectStore()
    work = demo_work(1, issn_l=ISSN, publisher_id=SECOND_OPEN_ID, publisher_name=SECOND_OPEN_NAME)
    paper_id = _write(database, work, fetched_at=T1, scope=OLD_SCOPE)
    before = _all_rows(demo_db)

    result = _reparse(database, store, NEW_SCOPE)

    assert _eligible(demo_db, paper_id) is True
    assert (result.demo_rechecked, result.demo_revoked) == (1, 0)
    assert _all_rows(demo_db) == before  # 連 updated_at 都沒動


def test_false_paper_that_would_compute_true_stays_false(
    demo_db: TestDatabase, database: Database
) -> None:
    store = FakeObjectStore()
    work = demo_work(1, issn_l=ISSN, publisher_id=SECOND_OPEN_ID, publisher_name=SECOND_OPEN_NAME)
    # 寫入時沒帶排除清單＝false；存下的欄位用新清單算是 true，但重判只往 false 改。
    paper_id = _write(database, work, fetched_at=T1, scope=ScopeSettings(frozenset({ISSN})))
    assert _eligible(demo_db, paper_id) is False
    before = _all_rows(demo_db)

    result = _reparse(database, store, NEW_SCOPE)

    assert _eligible(demo_db, paper_id) is False
    assert (result.demo_rechecked, result.demo_revoked) == (0, 0)
    assert _all_rows(demo_db) == before


def test_rechecking_twice_gives_the_same_result(demo_db: TestDatabase, database: Database) -> None:
    store = FakeObjectStore()
    revoked = [_seed_case(database, store, case, n) for n, case in enumerate(NOT_REWRITTEN, 1)]
    kept_work = demo_work(
        50, issn_l=ISSN, publisher_id=SECOND_OPEN_ID, publisher_name=SECOND_OPEN_NAME
    )
    kept = _write(database, kept_work, fetched_at=T1, scope=OLD_SCOPE)

    first = _reparse(database, store, NEW_SCOPE)
    after_first = _all_rows(demo_db)
    second = _reparse(database, store, NEW_SCOPE)

    assert (first.demo_rechecked, first.demo_revoked) == (7, 6)
    assert (second.demo_rechecked, second.demo_revoked) == (1, 0)
    assert all(_eligible(demo_db, paper_id) is False for paper_id in revoked)
    assert _eligible(demo_db, kept) is True
    assert _all_rows(demo_db) == after_first


def test_failure_while_rechecking_rolls_back_and_rerun_fills_in(
    demo_db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeObjectStore()
    paper_id = _seed_case(database, store, "no_raw", 1)
    rewritten_work = demo_work(2, issn_l=ISSN)
    rewritten_id = _write(database, rewritten_work, fetched_at=T1, scope=OLD_SCOPE)
    _seed_raw(database, store, _key(2), demo_work_bytes(rewritten_work), T2)
    before = _all_rows(demo_db)

    def failing(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("測試：重判寫入失敗")

    monkeypatch.setattr(reparse_module, "update_papers_not_demo_eligible", failing)
    with pytest.raises(RuntimeError, match="重判寫入失敗"):
        _reparse(database, store, NEW_SCOPE)

    # 套用與重判同一個交易：重寫到的那篇也退回，沒有「套用了、還沒重判」的狀態。
    assert _all_rows(demo_db) == before
    assert _eligible(demo_db, paper_id) is True
    assert _eligible(demo_db, rewritten_id) is True

    monkeypatch.undo()
    result = _reparse(database, store, NEW_SCOPE)

    assert _eligible(demo_db, paper_id) is False
    assert _eligible(demo_db, rewritten_id) is False
    assert (result.updated, result.demo_rechecked, result.demo_revoked) == (1, 1, 1)


def test_stored_parsed_paper_fields_are_what_gets_rechecked(
    demo_db: TestDatabase, database: Database
) -> None:
    # 存下的出版社名稱只在 lineage 裡對上新清單（直屬是 Second Open Press）：一樣改成 false。
    store = FakeObjectStore()
    work = demo_work(
        1,
        issn_l=ISSN,
        publisher_id=SECOND_OPEN_ID,
        publisher_name=SECOND_OPEN_NAME,
        lineage=[SECOND_OPEN_ID, "https://openalex.org/P9000000001"],
        lineage_names=[SECOND_OPEN_NAME, OPEN_PUBLISHER_NAME.upper()],
    )
    paper_id = _write(database, work, fetched_at=T1, scope=OLD_SCOPE)
    assert _eligible(demo_db, paper_id) is True
    stored = parse_openalex_work(demo_work_bytes(work), fetched_at=T1)
    assert isinstance(stored, ParsedPaper)
    assert OPEN_PUBLISHER_NAME.upper() in stored.publisher_names

    result = _reparse(database, store, NEW_SCOPE)

    assert _eligible(demo_db, paper_id) is False
    assert (result.demo_rechecked, result.demo_revoked) == (1, 1)


def test_finish_log_carries_the_recheck_numbers(demo_db: TestDatabase, database: Database) -> None:
    # CLI 還沒印這兩個數字之前，Kevin 從「重新解析結束」那一行日誌（JSON，寫在 stdout）讀。
    store = FakeObjectStore()
    _seed_case(database, store, "no_raw", 1)

    with capture_logs() as logs:
        _reparse(database, store, NEW_SCOPE)

    [finished] = [log for log in logs if log["event"] == "重新解析結束"]
    assert (finished["demo_rechecked"], finished["demo_revoked"]) == (1, 1)
