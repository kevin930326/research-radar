"""view `counted_paper` 與 `repo/stats.list_cube_counts` 對真的 PostgreSQL（Story 1.10、ADR-0027）。

哪些論文算數（範圍內＋已發佈的快照；被排除、範圍外、沒有快照、指向建立中或失敗的快照都不算）、
「有摘要」只看目前的那一筆、三種讀者的計數相同（`PublicViewer` 的 EXISTS 條件在真的 PostgreSQL
上可執行）、`radar_app` 不能經 view 寫入。

論文經正式的 `upsert_by_external_id` 寫入；快照、`first_snapshot_id` 與不是 current 的摘要用擁有者
角色直接寫（只在測試裡）。每個測試一個全新、已遷移的資料庫。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from radar.domain.cube import CountedCell
from radar.domain.papers import AbstractSource, PaperOrigin, ParsedPaper
from radar.domain.scope import ScopeSettings
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer
from radar.repo.db import Database
from radar.repo.papers import upsert_by_external_id
from radar.repo.snapshots import find_latest_published_snapshot
from radar.repo.stats import list_cube_counts
from tests.database import APP_ROLE, OWNER_ROLE, TestDatabase, migrate

T1 = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
AS_OF = datetime(2026, 11, 16, 2, 30, tzinfo=UTC)
TRACKED_ISSN = "0276-7783"
OTHER_ISSN = "0960-085X"  # 追蹤範圍外
SCOPE = ScopeSettings(frozenset({TRACKED_ISSN}), start_year=2016)
MEMBER = MemberViewer(member_id=uuid.UUID("018f0000-0000-7000-8000-0000000000a1"), is_admin=False)


@pytest.fixture
def stats_db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(stats_db: TestDatabase) -> Database:
    return Database(stats_db.app_url, connect_timeout=5, pool_size=2)


def _owner(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(OWNER_ROLE) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def _rows(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(sql, params).fetchall()


def _snapshot(db: TestDatabase, number: int, status: str) -> uuid.UUID:
    [(run_id,)] = _owner(db, "INSERT INTO weekly_run (status) VALUES ('failed') RETURNING id")
    [(snapshot_id,)] = _owner(
        db,
        "INSERT INTO snapshot (number, weekly_run_id, status, as_of) VALUES (%s, %s, %s, %s)"
        " RETURNING id",
        number,
        run_id,
        status,
        AS_OF if status == "published" else None,
    )
    return uuid.UUID(str(snapshot_id))


def _paper(
    database: Database,
    db: TestDatabase,
    number: int,
    *,
    snapshot_id: uuid.UUID | None,
    issn_l: str = TRACKED_ISSN,
    year: int = 2020,
    abstract: bool = True,
    excluded_reason: str | None = None,
) -> uuid.UUID:
    fields: dict[str, object] = {
        "source": "openalex",
        "fetched_at": T1,
        "doi": f"10.9999/stats.{number}",
        "openalex_id": f"W7{number:08d}",
        "title": f"Stats study {number}",
        "journal_issn_l": issn_l,
        "publication_year": year,
    }
    if abstract:
        fields |= {"abstract": f"Abstract {number}.", "abstract_source": "openalex"}
    with database.transaction() as connection:
        result = upsert_by_external_id(
            connection,
            ParsedPaper.model_validate(fields),
            origin=PaperOrigin.TRACKED if issn_l == TRACKED_ISSN else PaperOrigin.IMPORT,
            excluded_reason=excluded_reason,
            scope=SCOPE,
        )
    if snapshot_id is not None:
        _owner(
            db,
            "UPDATE paper SET first_snapshot_id = %s WHERE id = %s",
            snapshot_id,
            result.paper_id,
        )
    return result.paper_id


@dataclass(frozen=True)
class Papers:
    counted: uuid.UUID  # A：範圍內＋已發佈的快照，有 openalex 摘要
    excluded: uuid.UUID  # B
    out_of_scope: uuid.UUID  # C（已指定已發佈的快照也一樣）
    unassigned: uuid.UUID  # D：first_snapshot_id 是 NULL
    on_building: uuid.UUID  # E
    on_failed: uuid.UUID  # F
    no_current: uuid.UUID  # G：算數，但只有不是 current 的 semantic_scholar 與 uploaded_pdf


@pytest.fixture
def papers(stats_db: TestDatabase, database: Database) -> Papers:
    published = _snapshot(stats_db, 1, "published")
    building = _snapshot(stats_db, 2, "building")
    failed = _snapshot(stats_db, 3, "failed")
    made = Papers(
        counted=_paper(database, stats_db, 1, snapshot_id=published),
        excluded=_paper(
            database, stats_db, 2, snapshot_id=published, excluded_reason="editors_comments"
        ),
        out_of_scope=_paper(database, stats_db, 3, snapshot_id=published, issn_l=OTHER_ISSN),
        unassigned=_paper(database, stats_db, 4, snapshot_id=None),
        on_building=_paper(database, stats_db, 5, snapshot_id=building),
        on_failed=_paper(database, stats_db, 6, snapshot_id=failed),
        no_current=_paper(database, stats_db, 7, snapshot_id=published, year=2021, abstract=False),
    )
    _owner(
        stats_db,
        "INSERT INTO paper_abstract (paper_id, source, content, is_current) VALUES"
        " (%s, 'semantic_scholar', 'S2 abstract.', false),"
        " (%s, 'uploaded_pdf', 'From my PDF.', false)",
        made.no_current,
        made.no_current,
    )
    return made


EXPECTED_CELLS = [
    CountedCell(TRACKED_ISSN, 2020, AbstractSource.OPENALEX, 1),
    CountedCell(TRACKED_ISSN, 2021, None, 1),
]


def test_counted_paper_has_only_counted_papers(stats_db: TestDatabase, papers: Papers) -> None:
    rows = _rows(stats_db, "SELECT id FROM counted_paper ORDER BY id")

    assert sorted(row[0] for row in rows) == sorted([papers.counted, papers.no_current])


def test_counted_paper_columns_are_listed(stats_db: TestDatabase, papers: Papers) -> None:
    with stats_db.connect(APP_ROLE) as connection:
        cursor = connection.execute("SELECT * FROM counted_paper LIMIT 0")
        columns = [column.name for column in cursor.description or []]

    assert columns == [
        "id",
        "journal_issn_l",
        "publication_year",
        "first_snapshot_id",
        "lab_visible",
    ]


def test_cube_counts_use_only_the_current_abstract(database: Database, papers: Papers) -> None:
    with database.transaction() as connection:
        cells = list_cube_counts(SystemViewer(), connection)

    assert cells == EXPECTED_CELLS


@pytest.mark.parametrize("viewer", [SystemViewer(), MEMBER, PublicViewer()])
def test_three_viewers_get_the_same_counts(
    database: Database, papers: Papers, viewer: SystemViewer | MemberViewer | PublicViewer
) -> None:
    with database.transaction() as connection:
        cells = list_cube_counts(viewer, connection)

    assert cells == EXPECTED_CELLS


def test_no_counted_papers_no_cells(database: Database) -> None:
    with database.transaction() as connection:
        assert list_cube_counts(PublicViewer(), connection) == []


def test_app_role_only_has_select_on_the_view(stats_db: TestDatabase) -> None:
    privileges = _rows(
        stats_db,
        "SELECT p, has_table_privilege(%s, 'counted_paper', p)"
        " FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) AS p",
        APP_ROLE,
    )

    assert privileges == [("SELECT", True), ("INSERT", False), ("UPDATE", False), ("DELETE", False)]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO counted_paper (id, journal_issn_l, publication_year, lab_visible)"
        " VALUES (uuidv7(), '0276-7783', 2020, true)",
        "UPDATE counted_paper SET publication_year = 2000",
        "DELETE FROM counted_paper",
    ],
)
def test_app_role_cannot_write_through_the_view(
    stats_db: TestDatabase, papers: Papers, statement: str
) -> None:
    # 這個 view 有 JOIN，PostgreSQL 在檢查權限之前就先以「不能自動更新的 view」擋下
    # （ObjectNotInPrerequisiteState）；之後有人加了 INSTEAD OF 觸發器讓它可以寫，撤掉的權限
    # 會擋（InsufficientPrivilege，上一個測試確認權限真的撤了）。
    with (
        stats_db.connect(APP_ROLE) as connection,
        pytest.raises(
            (psycopg.errors.InsufficientPrivilege, psycopg.errors.ObjectNotInPrerequisiteState)
        ),
    ):
        connection.execute(statement)

    assert len(_rows(stats_db, "SELECT id FROM counted_paper")) == 2


# --- 最新已發佈的快照（`radar coverage` 前後各讀一次用的） -------------------------------------


def test_latest_published_snapshot_skips_building_and_failed(
    database: Database, papers: Papers
) -> None:
    # 快照 1 已發佈，編號比較大的 2（建立中）、3（失敗）都不算。
    with database.transaction() as connection:
        latest = find_latest_published_snapshot(SystemViewer(), connection)

    assert latest is not None
    assert latest.number == 1
    assert latest.as_of == AS_OF


def test_latest_published_snapshot_is_none_without_published(
    stats_db: TestDatabase, database: Database
) -> None:
    _snapshot(stats_db, 1, "building")
    _snapshot(stats_db, 2, "failed")

    with database.transaction() as connection:
        assert find_latest_published_snapshot(SystemViewer(), connection) is None
