"""示範站可用（Story 3.7a、ADR-0052）對真的 PostgreSQL：遷移 0021 的欄位與 view `demo_full_paper`、
`upsert_by_external_id` 寫授權／出版社／`demo_eligible`、授權變了但指紋沒變時照樣更新且不算變動、
`reparse_papers` 用存下的原始資料把三類欄位補上。

測試資料全部自己編（`tests/demo_works.py`），不用真的論文。每個測試一個全新、已遷移的資料庫。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from radar.adapters.fake import FakeObjectStore
from radar.domain.domain_config import ExclusionRulesConfig
from radar.domain.papers import PaperOrigin, ParsedPaper, UpsertOutcome, UpsertResult
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.raw_records import RawRecord, RawSource, compress_raw
from radar.domain.scope import ScopeSettings
from radar.domain.storage_keys import RAW_CONTENT_TYPE, raw_object_key
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.papers import upsert_by_external_id
from radar.repo.raw_fetches import create_raw_fetches
from radar.services.reparse import reparse_papers
from tests.database import APP_ROLE, OWNER_ROLE, TestDatabase, migrate
from tests.demo_works import (
    EXCLUDED_PUBLISHER_ID,
    EXCLUDED_PUBLISHER_NAME,
    TEST_EXCLUSIONS,
    demo_work,
    demo_work_bytes,
)

ISSN = "1471-7727"
T1 = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
T2 = T1 + timedelta(days=7)
SCOPE = ScopeSettings(frozenset({ISSN}), demo_publishers=TEST_EXCLUSIONS)


@pytest.fixture
def demo_db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(demo_db: TestDatabase) -> Database:
    return Database(demo_db.app_url, connect_timeout=5, pool_size=2)


def _owner(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(OWNER_ROLE) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def _rows(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(sql, params).fetchall()


def _published_snapshot(db: TestDatabase) -> uuid.UUID:
    [(run_id,)] = _owner(db, "INSERT INTO weekly_run (status) VALUES ('published') RETURNING id")
    [(snapshot_id,)] = _owner(
        db,
        "INSERT INTO snapshot (number, weekly_run_id, status, as_of)"
        " VALUES (1, %s, 'published', %s) RETURNING id",
        run_id,
        T1,
    )
    return uuid.UUID(str(snapshot_id))


def _parse(work: dict[str, Any], fetched_at: datetime = T1) -> ParsedPaper:
    return parse_openalex_work(demo_work_bytes(work), fetched_at=fetched_at)


def _write(
    database: Database,
    paper: ParsedPaper,
    *,
    excluded_reason: str | None = None,
    scope: ScopeSettings = SCOPE,
) -> UpsertResult:
    with database.transaction() as connection:
        return upsert_by_external_id(
            connection,
            paper,
            origin=PaperOrigin.TRACKED,
            excluded_reason=excluded_reason,
            scope=scope,
        )


def _counted(
    db: TestDatabase,
    database: Database,
    snapshot_id: uuid.UUID,
    work: dict[str, Any],
    *,
    excluded_reason: str | None = None,
) -> uuid.UUID:
    paper_id = _write(database, _parse(work), excluded_reason=excluded_reason).paper_id
    _owner(db, "UPDATE paper SET first_snapshot_id = %s WHERE id = %s", snapshot_id, paper_id)
    return paper_id


def _paper_row(db: TestDatabase, paper_id: uuid.UUID) -> tuple[Any, ...]:
    [row] = _rows(
        db,
        "SELECT primary_license, publisher_ids, publisher_names, demo_eligible"
        " FROM paper WHERE id = %s",
        paper_id,
    )
    return tuple(row)


def test_demo_full_paper_has_only_the_one_open_paper(
    demo_db: TestDatabase, database: Database
) -> None:
    snapshot = _published_snapshot(demo_db)
    papers = {
        "open": _counted(demo_db, database, snapshot, demo_work(1, issn_l=ISSN)),
        "excluded_publisher_cc_by": _counted(
            demo_db,
            database,
            snapshot,
            demo_work(
                2,
                issn_l=ISSN,
                publisher_id=EXCLUDED_PUBLISHER_ID,
                publisher_name=EXCLUDED_PUBLISHER_NAME,
            ),
        ),
        "cc_by_nc": _counted(
            demo_db, database, snapshot, demo_work(3, issn_l=ISSN, license="cc-by-nc")
        ),
        "only_best_oa_cc_by": _counted(
            demo_db,
            database,
            snapshot,
            demo_work(4, issn_l=ISSN, license=None, best_oa_license="cc-by"),
        ),
        "no_abstract": _counted(
            demo_db, database, snapshot, demo_work(5, issn_l=ISSN, abstract=False)
        ),
        "excluded_not_counted": _counted(
            demo_db,
            database,
            snapshot,
            demo_work(6, issn_l=ISSN),
            excluded_reason="editors_comments",
        ),
        "semantic_scholar_abstract": _counted(
            demo_db, database, snapshot, demo_work(7, issn_l=ISSN, abstract=False)
        ),
        "no_publisher": _counted(
            demo_db,
            database,
            snapshot,
            demo_work(8, issn_l=ISSN, source=True, publisher_id=None, publisher_name=None),
        ),
        "openalex_kept_semantic_scholar_current": _counted(
            demo_db, database, snapshot, demo_work(9, issn_l=ISSN)
        ),
        "id_without_name": _counted(
            demo_db,
            database,
            snapshot,
            demo_work(10, issn_l=ISSN, publisher_name=None, lineage_names=[]),
        ),
    }
    # 摘要來源是 Semantic Scholar 的那篇：寫入函式只寫 OpenAlex 的摘要，這裡直接插一筆目前用的。
    _owner(
        demo_db,
        "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
        " VALUES (%s, 'semantic_scholar', 'Made up abstract.', true)",
        papers["semantic_scholar_abstract"],
    )
    # 兩筆摘要：OpenAlex 那筆留著但不是目前用的，目前用的是 Semantic Scholar（不在 view，也不會
    # 因為多一筆摘要而多出一列）。
    _owner(
        demo_db,
        "UPDATE paper_abstract SET is_current = false WHERE paper_id = %s",
        papers["openalex_kept_semantic_scholar_current"],
    )
    _owner(
        demo_db,
        "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
        " VALUES (%s, 'semantic_scholar', 'Made up abstract.', true)",
        papers["openalex_kept_semantic_scholar_current"],
    )

    eligible = {name for name, paper_id in papers.items() if _paper_row(demo_db, paper_id)[3]}
    view_rows = [row[0] for row in _rows(demo_db, "SELECT id FROM demo_full_paper")]
    in_view = set(view_rows)

    assert in_view == {papers["open"]}
    # 一篇論文在 view 裡最多一列（`paper_abstract` 每篇最多一筆 `is_current`，部分唯一索引）。
    assert len(view_rows) == len(in_view)
    # 授權與出版社判定過關的：自己這篇，以及另外因為其他條件（摘要、統計、摘要來源）出局的四篇。
    assert eligible == {
        "open",
        "no_abstract",
        "excluded_not_counted",
        "semantic_scholar_abstract",
        "openalex_kept_semantic_scholar_current",
    }
    assert _paper_row(demo_db, papers["open"]) == (
        "cc-by",
        ["P9000000001"],
        ["Example Open Press"],
        True,
    )
    assert _paper_row(demo_db, papers["only_best_oa_cc_by"])[0] is None
    assert _paper_row(demo_db, papers["no_publisher"])[1:] == ([], [], False)
    # 只有出版社 ID、沒有名稱：排除清單目前只靠名稱，無法確認，不算可用（ADR-0052 第 4 點）。
    assert _paper_row(demo_db, papers["id_without_name"])[1:] == (["P9000000001"], [], False)


def test_view_is_read_only_for_the_app_role(demo_db: TestDatabase) -> None:
    [(insert, update, delete, select)] = _rows(
        demo_db,
        "SELECT has_table_privilege(current_user, 'demo_full_paper', 'INSERT'),"
        " has_table_privilege(current_user, 'demo_full_paper', 'UPDATE'),"
        " has_table_privilege(current_user, 'demo_full_paper', 'DELETE'),"
        " has_table_privilege(current_user, 'demo_full_paper', 'SELECT')",
    )

    assert (insert, update, delete, select) == (False, False, False, True)


def test_column_defaults_start_not_eligible(demo_db: TestDatabase) -> None:
    # 遷移 0021 的預設值：還沒補資料之前一篇都不算可用、沒有授權與出版社（遷移前就在的列另見
    # `test_migration_0021.py`）。
    [(default_eligible, default_ids, default_names)] = _rows(
        demo_db,
        "SELECT"
        " (SELECT column_default FROM information_schema.columns"
        "  WHERE table_name = 'paper' AND column_name = 'demo_eligible'),"
        " (SELECT column_default FROM information_schema.columns"
        "  WHERE table_name = 'paper' AND column_name = 'publisher_ids'),"
        " (SELECT column_default FROM information_schema.columns"
        "  WHERE table_name = 'paper' AND column_name = 'publisher_names')",
    )

    assert default_eligible == "false"
    assert default_ids == default_names == "'{}'::text[]"


def test_license_change_updates_columns_without_counting_as_a_change(
    demo_db: TestDatabase, database: Database
) -> None:
    first = _write(database, _parse(demo_work(1, issn_l=ISSN, license="cc-by-nc")))
    assert _paper_row(demo_db, first.paper_id)[3] is False
    [(hash_before, changed_at_before)] = _rows(
        demo_db, "SELECT source_hash, source_changed_at FROM paper WHERE id = %s", first.paper_id
    )

    second = _write(database, _parse(demo_work(1, issn_l=ISSN, license="cc-by"), T2))

    assert second.outcome is UpsertOutcome.UPDATED
    assert second.source_changed is False
    assert _paper_row(demo_db, first.paper_id) == (
        "cc-by",
        ["P9000000001"],
        ["Example Open Press"],
        True,
    )
    [(hash_after, changed_at_after)] = _rows(
        demo_db, "SELECT source_hash, source_changed_at FROM paper WHERE id = %s", first.paper_id
    )
    # 指紋沒變、`source_changed_at` 沒動：向量化與 AI 工作都不會把它當成變了的論文重做。
    assert (hash_after, changed_at_after) == (hash_before, changed_at_before)
    assert changed_at_after is None

    # 出版社換成排除清單裡的：一樣只改欄位、不算變動。
    third = _write(
        database,
        _parse(demo_work(1, issn_l=ISSN, publisher_name=EXCLUDED_PUBLISHER_NAME), T2),
    )
    assert third.source_changed is False
    assert _paper_row(demo_db, first.paper_id)[3] is False


def test_stale_write_does_not_touch_the_columns(demo_db: TestDatabase, database: Database) -> None:
    first = _write(database, _parse(demo_work(1, issn_l=ISSN), T2))
    stale = _write(database, _parse(demo_work(1, issn_l=ISSN, license="cc-by-nc"), T1))

    assert stale.outcome is UpsertOutcome.STALE
    assert _paper_row(demo_db, first.paper_id)[0] == "cc-by"
    assert _paper_row(demo_db, first.paper_id)[3] is True


def test_scope_without_exclusions_never_marks_eligible(
    demo_db: TestDatabase, database: Database
) -> None:
    result = _write(
        database, _parse(demo_work(1, issn_l=ISSN)), scope=ScopeSettings(frozenset({ISSN}))
    )

    assert _paper_row(demo_db, result.paper_id)[3] is False


def test_reparse_fills_the_three_kinds_of_columns(
    demo_db: TestDatabase, database: Database
) -> None:
    # 模擬遷移前就在的論文：寫入時還沒有授權與出版社（舊的解析結果），demo_eligible 是 false。
    work = demo_work(1, issn_l=ISSN)
    content = demo_work_bytes(work)
    parsed = parse_openalex_work(content, fetched_at=T1)
    old = ParsedPaper.model_validate(
        {**parsed.model_dump(), "primary_license": None, "publisher_ids": (), "publisher_names": ()}
    )
    seeded = _write(database, old)
    assert _paper_row(demo_db, seeded.paper_id) == (None, [], [], False)

    # 存下的原始資料（和每週更新寫的一樣）；reparse 只讀它，不連 OpenAlex。
    store = FakeObjectStore()
    record = RawRecord(RawSource.OPENALEX, "W800000001", content, T1)
    store.objects[raw_object_key("openalex", record.sha256)] = compress_raw(content)
    store.content_types[raw_object_key("openalex", record.sha256)] = RAW_CONTENT_TYPE
    with database.transaction() as connection:
        create_raw_fetches(connection, [record])

    result = reparse_papers(
        SystemViewer(),
        database,
        store,
        scope=SCOPE,
        exclusion_rules=ExclusionRulesConfig(rules=()),
    )

    assert (result.updated, result.parse_error) == (1, 0)
    assert _paper_row(demo_db, seeded.paper_id) == (
        "cc-by",
        ["P9000000001"],
        ["Example Open Press"],
        True,
    )
    [(changed_at,)] = _rows(
        demo_db, "SELECT source_changed_at FROM paper WHERE id = %s", seeded.paper_id
    )
    assert changed_at is None
