"""`repo.papers` 在 Story 1.7 加的部分，對真的 PostgreSQL：新欄位、摘要與 `is_current`、
關鍵字整批覆寫、`find_known_external_ids` 的可見範圍、CHECK 約束與來源代號的清單。

用應用程式角色（`radar_app`）連線；每個測試一個全新、已遷移的資料庫，資料都是這裡自己造的。
`paper_keyword` 只在這裡直接下 SQL 檢查（程式裡只有之後的 `repo/keyword_stats.py` 可以讀）。
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from radar.domain.identifiers import ExternalId, IdentifierScheme
from radar.domain.papers import (
    AbstractSource,
    KeywordSource,
    PaperOrigin,
    ParsedKeyword,
    ParsedPaper,
    UpsertOutcome,
    UpsertResult,
)
from radar.domain.scope import ScopeSettings
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer
from radar.repo.db import Database
from radar.repo.papers import (
    find_known_external_ids,
    get_paper,
    update_paper_keywords,
    upsert_by_external_id,
)
from tests.database import APP_ROLE, TestDatabase, migrate

T1 = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
T2 = T1 + timedelta(days=7)
TRACKED_ISSN = "0276-7783"
SCOPE = ScopeSettings(frozenset({TRACKED_ISSN}))
MEMBER = MemberViewer(member_id=uuid.UUID("018f0000-0000-7000-8000-0000000000a1"), is_admin=False)


@pytest.fixture
def papers_db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(papers_db: TestDatabase) -> Database:
    return Database(papers_db.app_url, connect_timeout=5, pool_size=2)


def _paper(**overrides: object) -> ParsedPaper:
    fields: dict[str, object] = {
        "source": "openalex",
        "fetched_at": T1,
        "doi": "10.1/abc",
        "openalex_id": "W1",
        "title": "A study",
        "journal_issn_l": TRACKED_ISSN,
        "publication_year": 2020,
        "authors": ("Ann", "Bob"),
        "volume": "40",
        "issue": "2",
        "first_page": "1",
        "last_page": "30",
        "work_type": "article",
        "is_retracted": False,
        "abstract": "First abstract.",
        "abstract_source": "openalex",
        "referenced_works": ("W7", "W8"),
        "cited_by_count": 12,
        "keywords": ({"key": "a", "display_name": "A"},),
    }
    fields.update(overrides)
    return ParsedPaper.model_validate(fields)


def _upsert(database: Database, paper: ParsedPaper) -> UpsertResult:
    with database.transaction() as connection:
        return upsert_by_external_id(
            connection, paper, origin=PaperOrigin.TRACKED, excluded_reason=None, scope=SCOPE
        )


def _rows(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(sql, params).fetchall()


def _abstracts(db: TestDatabase, paper_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return _rows(
        db,
        "SELECT source, content, is_current FROM paper_abstract"
        " WHERE paper_id = %s ORDER BY source",
        paper_id,
    )


def _keywords(db: TestDatabase, paper_id: uuid.UUID) -> set[tuple[str, str, str]]:
    rows = _rows(
        db, "SELECT source, key, display_name FROM paper_keyword WHERE paper_id = %s", paper_id
    )
    return {(row[0], row[1], row[2]) for row in rows}


def _set_keywords(database: Database, paper_id: uuid.UUID, *pairs: tuple[str, str]) -> None:
    with database.transaction() as connection:
        update_paper_keywords(
            connection,
            paper_id,
            KeywordSource.OPENALEX,
            [ParsedKeyword(key=key, display_name=name) for key, name in pairs],
        )


# --- 新欄位 ---------------------------------------------------------------------------------


def test_new_fields_are_written_and_read(papers_db: TestDatabase, database: Database) -> None:
    paper = _paper()
    created = _upsert(database, paper)

    with database.transaction() as connection:
        stored = get_paper(SystemViewer(), connection, created.paper_id)

    assert stored is not None
    assert stored.authors == ("Ann", "Bob")
    assert (stored.volume, stored.issue, stored.first_page, stored.last_page) == (
        "40",
        "2",
        "1",
        "30",
    )
    assert stored.work_type == "article"
    assert stored.is_retracted is False
    assert stored.referenced_works == ("W7", "W8")
    assert stored.cited_by_count == 12
    assert stored.source_hash == paper.source_hash

    newer = _paper(
        fetched_at=T2,
        authors=(),
        volume=None,
        is_retracted=True,
        referenced_works=(),
        cited_by_count=None,
        work_type="review",
    )
    assert _upsert(database, newer).outcome is UpsertOutcome.UPDATED
    with database.transaction() as connection:
        updated = get_paper(SystemViewer(), connection, created.paper_id)
    assert updated is not None
    assert updated.authors == ()
    assert updated.volume is None
    assert updated.is_retracted is True
    assert updated.referenced_works == ()
    assert updated.cited_by_count is None
    assert updated.work_type == "review"
    assert updated.source_hash == newer.source_hash != paper.source_hash


def test_stale_write_changes_nothing(papers_db: TestDatabase, database: Database) -> None:
    created = _upsert(database, _paper(fetched_at=T2, abstract="Newer.", cited_by_count=50))

    stale = _upsert(database, _paper(fetched_at=T1, abstract="Older.", cited_by_count=1))

    assert stale.outcome is UpsertOutcome.STALE
    assert _abstracts(papers_db, created.paper_id) == [("openalex", "Newer.", True)]
    assert _rows(papers_db, "SELECT cited_by_count FROM paper") == [(50,)]


# --- 摘要與 is_current ----------------------------------------------------------------------


def test_abstract_then_no_abstract(papers_db: TestDatabase, database: Database) -> None:
    created = _upsert(database, _paper())
    assert _abstracts(papers_db, created.paper_id) == [("openalex", "First abstract.", True)]

    _upsert(database, _paper(fetched_at=T2, abstract="Second abstract."))
    assert _abstracts(papers_db, created.paper_id) == [("openalex", "Second abstract.", True)]

    _upsert(
        database, _paper(fetched_at=T2 + timedelta(days=1), abstract=None, abstract_source=None)
    )
    assert _abstracts(papers_db, created.paper_id) == []
    assert _rows(papers_db, "SELECT count(*) FROM paper_abstract WHERE is_current") == [(0,)]


def test_paper_without_abstract_has_no_row(papers_db: TestDatabase, database: Database) -> None:
    created = _upsert(database, _paper(abstract=None, abstract_source=None))

    assert _abstracts(papers_db, created.paper_id) == []


def test_current_abstract_follows_priority(papers_db: TestDatabase, database: Database) -> None:
    created = _upsert(database, _paper(abstract=None, abstract_source=None))
    with papers_db.connect(APP_ROLE) as connection:
        connection.execute(
            "INSERT INTO paper_abstract (paper_id, source, content, is_current) VALUES"
            " (%s, 'semantic_scholar', 'From S2.', true), (%s, 'uploaded_pdf', 'From PDF.', false)",
            (created.paper_id, created.paper_id),
        )

    # OpenAlex 的摘要排在 Semantic Scholar 前面，成為目前的那一筆。
    _upsert(database, _paper(fetched_at=T2))
    assert _abstracts(papers_db, created.paper_id) == [
        ("openalex", "First abstract.", True),
        ("semantic_scholar", "From S2.", False),
        ("uploaded_pdf", "From PDF.", False),
    ]

    # OpenAlex 不再提供摘要：那一列刪掉，換 Semantic Scholar；上傳的 PDF 永遠不是目前的。
    _upsert(
        database, _paper(fetched_at=T2 + timedelta(days=1), abstract=None, abstract_source=None)
    )
    assert _abstracts(papers_db, created.paper_id) == [
        ("semantic_scholar", "From S2.", True),
        ("uploaded_pdf", "From PDF.", False),
    ]


def test_partial_unique_index_blocks_second_current(
    papers_db: TestDatabase, database: Database
) -> None:
    created = _upsert(database, _paper())

    with (
        papers_db.connect(APP_ROLE) as connection,
        pytest.raises(psycopg.errors.UniqueViolation, match="uq_paper_abstract_current"),
    ):
        connection.execute(
            "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
            " VALUES (%s, 'semantic_scholar', 'x', true)",
            (created.paper_id,),
        )


def test_uploaded_pdf_cannot_be_current(papers_db: TestDatabase, database: Database) -> None:
    created = _upsert(database, _paper(abstract=None, abstract_source=None))

    with (
        papers_db.connect(APP_ROLE) as connection,
        pytest.raises(psycopg.errors.CheckViolation, match="uploaded_pdf_not_current"),
    ):
        connection.execute(
            "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
            " VALUES (%s, 'uploaded_pdf', 'x', true)",
            (created.paper_id,),
        )


# --- 關鍵字 ---------------------------------------------------------------------------------


def test_keywords_are_overwritten(papers_db: TestDatabase, database: Database) -> None:
    created = _upsert(database, _paper())

    _set_keywords(database, created.paper_id, ("a", "A"), ("b", "B"))
    assert _keywords(papers_db, created.paper_id) == {
        ("openalex", "a", "A"),
        ("openalex", "b", "B"),
    }

    _set_keywords(database, created.paper_id, ("b", "B2"), ("c", "C"))
    assert _keywords(papers_db, created.paper_id) == {
        ("openalex", "b", "B2"),
        ("openalex", "c", "C"),
    }

    _set_keywords(database, created.paper_id)
    assert _keywords(papers_db, created.paper_id) == set()


def test_keywords_of_other_papers_are_untouched(
    papers_db: TestDatabase, database: Database
) -> None:
    first = _upsert(database, _paper())
    second = _upsert(database, _paper(doi="10.1/other", openalex_id="W2"))
    _set_keywords(database, first.paper_id, ("a", "A"))
    _set_keywords(database, second.paper_id, ("z", "Z"))

    _set_keywords(database, first.paper_id, ("b", "B"))

    assert _keywords(papers_db, second.paper_id) == {("openalex", "z", "Z")}


@pytest.mark.parametrize(
    ("source", "keywords", "error"),
    [
        ("crossref", [], ValueError),
        (KeywordSource.OPENALEX, [("a", "A"), ("a", "B")], ValueError),
        (KeywordSource.OPENALEX, ["not a keyword"], ValueError),
    ],
)
def test_update_keywords_rejects_bad_input_before_database(
    source: Any, keywords: list[Any], error: type[Exception]
) -> None:
    items = [
        ParsedKeyword(key=item[0], display_name=item[1]) if isinstance(item, tuple) else item
        for item in keywords
    ]
    with pytest.raises(error):
        update_paper_keywords(None, uuid.uuid4(), source, items)  # type: ignore[arg-type]


# --- find_known_external_ids ----------------------------------------------------------------


def test_find_known_external_ids_respects_visibility(
    papers_db: TestDatabase, database: Database
) -> None:
    _upsert(database, _paper())  # 追蹤範圍內 → 成員看得到
    _upsert(database, _paper(doi="10.1/outside", openalex_id="W2", journal_issn_l="1471-7727"))
    wanted = [
        ExternalId(IdentifierScheme.OPENALEX, "W1"),
        ExternalId(IdentifierScheme.OPENALEX, "W2"),
        ExternalId(IdentifierScheme.OPENALEX, "W3"),
        ExternalId(IdentifierScheme.DOI, "10.1/abc"),
    ]

    with database.transaction() as connection:
        system = find_known_external_ids(SystemViewer(), connection, wanted)
        member = find_known_external_ids(MEMBER, connection, wanted)
        public = find_known_external_ids(PublicViewer(), connection, wanted)
        empty = find_known_external_ids(SystemViewer(), connection, [])

    assert system == frozenset(wanted[:2] + wanted[3:])
    assert member == frozenset({wanted[0], wanted[3]})
    assert public == frozenset()
    assert empty == frozenset()


# --- CHECK 約束與來源代號的清單 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        (
            "INSERT INTO paper_keyword (paper_id, source, key, display_name)"
            " VALUES (%s, 'openalex', 'Bad Key', 'x')",
            "ck_paper_keyword_key_format",
        ),
        (
            "INSERT INTO paper_keyword (paper_id, source, key, display_name)"
            " VALUES (%s, 'crossref', 'ok', 'x')",
            "ck_paper_keyword_source",
        ),
        (
            "INSERT INTO paper_keyword (paper_id, source, key, display_name)"
            " VALUES (%s, 'openalex', 'ok', '')",
            "ck_paper_keyword_display_name_not_empty",
        ),
        (
            "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
            " VALUES (%s, 'openalex', '', false)",
            "ck_paper_abstract_content_not_empty",
        ),
        (
            "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
            " VALUES (%s, 'crossref', 'x', false)",
            "ck_paper_abstract_source",
        ),
        ("UPDATE paper SET source_hash = 'ABC' WHERE id = %s", "ck_paper_source_hash_format"),
        ("UPDATE paper SET cited_by_count = -1 WHERE id = %s", "ck_paper_cited_by_count"),
    ],
)
def test_check_constraints(
    papers_db: TestDatabase, database: Database, sql: str, constraint: str
) -> None:
    created = _upsert(database, _paper())

    with (
        papers_db.connect(APP_ROLE) as connection,
        pytest.raises(psycopg.errors.CheckViolation, match=constraint),
    ):
        connection.execute(sql, (created.paper_id,))


def _source_check_values(db: TestDatabase, table: str) -> list[str]:
    """`table` 的 `source` CHECK 列的值（約束名稱以 `_source` 結尾的那一條）。"""
    rows = _rows(
        db,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = %s::regclass AND contype = 'c' AND conname LIKE %s",
        table,
        "%\\_source",
    )
    assert len(rows) == 1, rows
    return re.findall(r"'([a-z_]+)'::text", rows[0][0])


def test_source_lists_match_the_enums(papers_db: TestDatabase) -> None:
    assert _source_check_values(papers_db, "paper_keyword") == [s.value for s in KeywordSource]
    assert _source_check_values(papers_db, "paper_abstract") == [s.value for s in AbstractSource]
