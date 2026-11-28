"""遷移 0021（示範站可用：`paper` 的授權、出版社、`demo_eligible` 與 view `demo_full_paper`，
Story 3.7a、ADR-0052）對真的 PostgreSQL：在有資料的資料庫上降版、升版。

- 降到 0020 後用 SQL 直接寫一篇會計入統計、有 OpenAlex 摘要的論文（不經現在的寫入函式：0020 的
  schema 沒有 0021 的欄位）。
- 升到 0021：那篇的四個新欄位是初值（NULL、空陣列、空陣列、false），原本的資料都還在，不在
  `demo_full_paper`（補資料之前一篇都不算可用）。
- 再降到 0020：四欄與 view 都不見、論文還在；升回 0021 後 `alembic check` 沒有差異。
- CHECK `ck_paper_primary_license_not_empty`（資料庫裡的名稱是 `ck_paper_ck_paper_…`）擋空字串。
資料全是測試自己編的（DOI 前綴 `10.9999`、假標題、假摘要）。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from tests.database import TestDatabase, connect_admin, migrate
from tests.integration.test_migration_0016 import _alembic
from tests.llm_support import execute

NEW_COLUMNS = ("primary_license", "publisher_ids", "publisher_names", "demo_eligible")
FETCHED_AT = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
ISSN = "1471-7727"
TITLE = "Made up study of migration 0021"
ABSTRACT = "Made up abstract for migration 0021."
SOURCE_HASH = "a" * 64


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _downgrade_to_0020(db: TestDatabase) -> None:
    down = _alembic(db, "downgrade", "0020")
    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0020"}


def _paper_columns(db: TestDatabase) -> set[str]:
    return {
        str(row[0])
        for row in execute(
            db,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'paper'",
        )
    }


def _relations(db: TestDatabase) -> set[str]:
    with connect_admin(db.name) as admin:
        rows = admin.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def _seed_counted_paper_at_0020(db: TestDatabase) -> uuid.UUID:
    """0020 的 schema 上用一句 SQL 寫一篇計入統計的論文：已發布的快照、在追蹤範圍、沒有排除、
    一筆目前用的 OpenAlex 摘要、DOI 與 OpenAlex 兩個識別碼。"""
    [(paper_id,)] = execute(
        db,
        "WITH r AS (INSERT INTO weekly_run (status) VALUES ('published') RETURNING id),"
        " s AS (INSERT INTO snapshot (number, weekly_run_id, status, as_of)"
        " SELECT 1, r.id, 'published', %s FROM r RETURNING id),"
        " p AS (INSERT INTO paper (origin, title, publication_year, journal_issn_l, in_scope,"
        " excluded_reason, lab_visible, openalex_fetched_at, work_type, authors, cited_by_count,"
        " source_hash, first_snapshot_id)"
        " SELECT 'tracked', %s, 2022, %s, true, NULL, true, %s, 'article',"
        " ARRAY['Test Author'], 0, %s, s.id FROM s RETURNING id),"
        " i AS (INSERT INTO external_id (paper_id, scheme, value_norm)"
        " SELECT p.id, v.scheme, v.value_norm FROM p,"
        " (VALUES (0, 'doi', '10.9999/m21.1'), (1, 'openalex', 'W800000021'))"
        " AS v(ord, scheme, value_norm) ORDER BY v.ord),"
        " a AS (INSERT INTO paper_abstract (paper_id, source, content, is_current)"
        " SELECT id, 'openalex', %s, true FROM p)"
        " SELECT id FROM p",
        FETCHED_AT,
        TITLE,
        ISSN,
        FETCHED_AT,
        SOURCE_HASH,
        ABSTRACT,
    )
    return uuid.UUID(str(paper_id))


def _original(db: TestDatabase, paper_id: uuid.UUID) -> tuple[Any, ...]:
    """0020 就有的資料：論文本身、識別碼、摘要。升降版前後要一樣。"""
    [paper] = execute(
        db,
        "SELECT origin, title, publication_year, journal_issn_l, in_scope, excluded_reason,"
        " lab_visible, openalex_fetched_at, work_type, authors, cited_by_count, source_hash,"
        " first_snapshot_id, created_at, updated_at FROM paper WHERE id = %s",
        paper_id,
    )
    ids = execute(
        db,
        "SELECT scheme, value_norm FROM external_id WHERE paper_id = %s ORDER BY id",
        paper_id,
    )
    abstracts = execute(
        db,
        "SELECT source, content, is_current FROM paper_abstract WHERE paper_id = %s",
        paper_id,
    )
    return (tuple(paper), ids, abstracts)


def _counted_ids(db: TestDatabase) -> list[uuid.UUID]:
    return [uuid.UUID(str(row[0])) for row in execute(db, "SELECT id FROM counted_paper")]


def test_upgrade_on_existing_rows_starts_them_not_eligible(db: TestDatabase) -> None:
    _downgrade_to_0020(db)
    paper_id = _seed_counted_paper_at_0020(db)
    before = _original(db, paper_id)
    assert _counted_ids(db) == [paper_id]

    migrate(db)

    assert db.revisions() == {"0024"}
    [new_values] = execute(
        db,
        "SELECT primary_license, publisher_ids, publisher_names, demo_eligible"
        " FROM paper WHERE id = %s",
        paper_id,
    )
    assert tuple(new_values) == (None, [], [], False)
    assert _original(db, paper_id) == before
    # 還在統計裡，但補資料（`radar reparse`）之前不在示範站。
    assert _counted_ids(db) == [paper_id]
    assert execute(db, "SELECT count(*) FROM demo_full_paper") == [(0,)]


def test_downgrade_to_0020_and_upgrade_again_keeps_the_paper(db: TestDatabase) -> None:
    _downgrade_to_0020(db)
    paper_id = _seed_counted_paper_at_0020(db)
    before = _original(db, paper_id)
    migrate(db)
    # 升版後把這篇改成可用，確認降版時這些值跟著欄位一起丟掉。
    execute(
        db,
        "UPDATE paper SET primary_license = 'cc-by', publisher_ids = ARRAY['P9000000001'],"
        " publisher_names = ARRAY['Example Open Press'], demo_eligible = true WHERE id = %s",
        paper_id,
    )
    assert execute(db, "SELECT id FROM demo_full_paper") == [(paper_id,)]

    _downgrade_to_0020(db)

    assert not set(NEW_COLUMNS) & _paper_columns(db)
    assert "demo_full_paper" not in _relations(db)
    assert "counted_paper" in _relations(db)
    # 0020 就有的資料完全不變（上面的 UPDATE 只改 0021 的欄位，`paper` 沒有觸發器）。
    assert _original(db, paper_id) == before

    migrate(db)

    assert db.revisions() == {"0024"}
    assert set(NEW_COLUMNS) <= _paper_columns(db)
    [new_values] = execute(
        db,
        "SELECT primary_license, publisher_ids, publisher_names, demo_eligible"
        " FROM paper WHERE id = %s",
        paper_id,
    )
    assert tuple(new_values) == (None, [], [], False)
    assert execute(db, "SELECT count(*) FROM demo_full_paper") == [(0,)]
    check = _alembic(db, "check")
    assert check.returncode == 0, check.stdout + check.stderr


def test_primary_license_rejects_empty_string(db: TestDatabase) -> None:
    _downgrade_to_0020(db)
    paper_id = _seed_counted_paper_at_0020(db)
    migrate(db)

    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        execute(db, "UPDATE paper SET primary_license = '' WHERE id = %s", paper_id)

    # 專案的命名規則會在遷移寫的名稱前再加一次 `ck_paper_`（其他 CHECK 也是這樣）。
    assert caught.value.diag.constraint_name == "ck_paper_ck_paper_primary_license_not_empty"
    execute(db, "UPDATE paper SET primary_license = 'weird' WHERE id = %s", paper_id)
    assert execute(db, "SELECT primary_license FROM paper WHERE id = %s", paper_id) == [("weird",)]
