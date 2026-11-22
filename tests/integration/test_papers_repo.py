"""`repo.papers` 對真的 PostgreSQL：upsert 的各種情境、讀者身分、並行新增、CHECK 約束。

用應用程式角色（`radar_app`）連線，交易經 `Database.transaction()`（和正式的 services 一樣）。
每個測試一個全新、已遷移的資料庫，資料都是這裡自己造的。
"""

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

from radar.domain.identifiers import ExternalId, IdentifierScheme
from radar.domain.papers import (
    KeywordSource,
    PaperOrigin,
    ParsedKeyword,
    ParsedPaper,
    SameSchemeMismatch,
    SplitIdentifiers,
    UpsertOutcome,
    UpsertResult,
)
from radar.domain.policy import PublisherExclusions, is_demo_eligible
from radar.domain.scope import ScopeSettings
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer
from radar.repo import papers as papers_module
from radar.repo.db import Connection, Database
from radar.repo.papers import get_paper, upsert_by_external_id
from tests.database import APP_ROLE, OWNER_ROLE, TestDatabase, connect_admin, migrate

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
    }
    fields.update(overrides)
    return ParsedPaper.model_validate(fields)


def _upsert(
    database: Database,
    paper: ParsedPaper,
    *,
    origin: PaperOrigin = PaperOrigin.TRACKED,
    excluded_reason: str | None = None,
) -> UpsertResult:
    with database.transaction() as connection:
        return upsert_by_external_id(
            connection, paper, origin=origin, excluded_reason=excluded_reason, scope=SCOPE
        )


def _rows(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(sql, params).fetchall()


def _paper_row(db: TestDatabase, paper_id: uuid.UUID) -> dict[str, Any]:
    with db.connect(APP_ROLE) as connection:
        cursor = connection.execute(
            "SELECT origin, title, in_scope, lab_visible, excluded_reason, openalex_fetched_at"
            " FROM paper WHERE id = %s",
            (paper_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        return dict(zip([column.name for column in cursor.description or []], row, strict=True))


# --- 同一 DOI、舊的不蓋新的 -----------------------------------------------------------------


def test_same_doi_written_two_ways_is_one_paper(
    papers_db: TestDatabase, database: Database
) -> None:
    first = _upsert(database, _paper(doi="10.1/ABC", openalex_id=None))
    second = _upsert(
        database, _paper(doi="https://doi.org/10.1/abc", openalex_id=None, fetched_at=T2)
    )

    assert first.outcome is UpsertOutcome.CREATED
    assert second.outcome is UpsertOutcome.UPDATED
    assert second.paper_id == first.paper_id
    assert _rows(papers_db, "SELECT count(*) FROM paper") == [(1,)]
    assert _rows(papers_db, "SELECT scheme, value_norm FROM external_id") == [("doi", "10.1/abc")]


def test_older_data_does_not_overwrite_newer(papers_db: TestDatabase, database: Database) -> None:
    created = _upsert(database, _paper(fetched_at=T2, title="Newer title"))

    result = _upsert(database, _paper(fetched_at=T1, title="Older title"))

    assert result == UpsertResult(created.paper_id, UpsertOutcome.STALE)
    row = _paper_row(papers_db, created.paper_id)
    assert row["title"] == "Newer title"
    assert row["openalex_fetched_at"] == T2


def test_same_fetch_time_is_written(papers_db: TestDatabase, database: Database) -> None:
    created = _upsert(database, _paper(title="First"))

    result = _upsert(database, _paper(title="Second"))

    assert result.outcome is UpsertOutcome.UPDATED
    assert _paper_row(papers_db, created.paper_id)["title"] == "Second"


# --- 識別碼 -------------------------------------------------------------------------------


def test_identifier_owned_by_another_paper_is_reported_not_moved(
    papers_db: TestDatabase, database: Database
) -> None:
    # DOI 屬於 A、OpenAlex ID 屬於 B：兩篇都不寫、識別碼不搬，回「識別碼分屬兩篇」
    # （ADR-0020 更新 2026-11-22；原本是更新 A、把 W2 放進 conflicts）。
    paper_a = _upsert(database, _paper(doi="10.1/a", openalex_id=None, title="A"))
    paper_b = _upsert(database, _paper(doi=None, openalex_id="W2", title="B"))
    before = _write_targets(papers_db)

    with capture_logs() as logs:
        result = _upsert(
            database, _paper(doi="10.1/a", openalex_id="W2", title="A2", fetched_at=T2)
        )

    assert _write_targets(papers_db) == before
    assert result == _split_result(paper_a.paper_id, "10.1/a", paper_b.paper_id, "W2")
    assert _paper_row(papers_db, paper_a.paper_id)["title"] == "A"
    assert _paper_row(papers_db, paper_b.paper_id)["title"] == "B"
    assert _rows(
        papers_db,
        "SELECT paper_id FROM external_id WHERE scheme = 'openalex' AND value_norm = 'W2'",
    ) == [(paper_b.paper_id,)]
    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["doi_paper_id"] == str(paper_a.paper_id)
    assert warnings[0]["openalex_paper_id"] == str(paper_b.paper_id)


# --- 識別碼分屬兩篇：兩篇都不寫（ADR-0020 更新 2026-11-22） ------------------------------------


WRITE_TARGET_TABLES = ("paper", "external_id", "paper_abstract", "paper_keyword")


def _write_targets(db: TestDatabase) -> dict[str, list[tuple[Any, ...]]]:
    """論文的四張表，整列、依主鍵排序（逐欄比對寫入前後用）。"""
    with db.connect(APP_ROLE) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in WRITE_TARGET_TABLES
        }


def _paper_target_sql(table: str) -> str:
    column = "id" if table == "paper" else "paper_id"
    return f"SELECT * FROM {table} WHERE {column} = %s ORDER BY id"


def _paper_targets(db: TestDatabase, paper_id: uuid.UUID) -> dict[str, list[tuple[Any, ...]]]:
    """一篇論文在四張表的整列（論文列、識別碼、摘要、關鍵字），依主鍵排序。"""
    with db.connect(APP_ROLE) as connection:
        return {
            table: connection.execute(_paper_target_sql(table), (paper_id,)).fetchall()
            for table in WRITE_TARGET_TABLES
        }


def _paper_targets_in(
    connection: Connection, paper_id: uuid.UUID
) -> dict[str, list[tuple[Any, ...]]]:
    """同 `_paper_targets`，但在還沒 commit 的交易連線上讀（直接給 psycopg，值的型別相同）。"""
    return {
        table: [
            tuple(row)
            for row in connection.exec_driver_sql(_paper_target_sql(table), (paper_id,)).fetchall()
        ]
        for table in WRITE_TARGET_TABLES
    }


def _assert_only_these_papers_unchanged(
    db: TestDatabase, snapshots: dict[uuid.UUID, dict[str, list[tuple[Any, ...]]]]
) -> None:
    """每篇論文四張表的整列和快照逐欄相同，而且四張表裡沒有這幾篇以外的列。"""
    for paper_id, snapshot in snapshots.items():
        assert _paper_targets(db, paper_id) == snapshot
    totals = {table: len(rows) for table, rows in _write_targets(db).items()}
    assert totals == {
        table: sum(len(snapshot[table]) for snapshot in snapshots.values())
        for table in WRITE_TARGET_TABLES
    }


def _split_result(
    doi_paper_id: uuid.UUID, doi: str, openalex_paper_id: uuid.UUID, openalex_id: str
) -> UpsertResult:
    return UpsertResult(
        doi_paper_id,
        UpsertOutcome.IDENTIFIERS_SPLIT,
        split=SplitIdentifiers(
            doi=ExternalId(IdentifierScheme.DOI, doi),
            doi_paper_id=doi_paper_id,
            openalex_id=ExternalId(IdentifierScheme.OPENALEX, openalex_id),
            openalex_paper_id=openalex_paper_id,
        ),
    )


DEMO_SCOPE = ScopeSettings(
    frozenset({TRACKED_ISSN}),
    demo_publishers=PublisherExclusions(ids=frozenset(), names=frozenset({"Closed Press"})),
)


def test_split_identifiers_write_nothing_to_either_paper(
    papers_db: TestDatabase, database: Database
) -> None:
    # B 有 DOI 10.1/b、A 有 W2；這筆資料是 (10.1/b, W2)、比兩篇都新，還帶摘要與不同的欄位。
    paper_b = _upsert(
        database,
        _paper(
            doi="10.1/b",
            openalex_id=None,
            title="B",
            abstract="B abstract",
            abstract_source="openalex",
        ),
        origin=PaperOrigin.IMPORT,
    )
    paper_a = _upsert(database, _paper(doi=None, openalex_id="W2", title="A"))
    before = _write_targets(papers_db)
    assert len(before["paper"]) == 2

    with capture_logs() as logs, database.transaction() as connection:
        result = upsert_by_external_id(
            connection,
            _paper(
                doi="10.1/b",
                openalex_id="W2",
                title="Other work",
                fetched_at=T2,
                publication_year=2024,
                abstract="Other abstract",
                abstract_source="openalex",
                primary_license="cc-by",
                publisher_names=("Open Press",),
            ),
            origin=PaperOrigin.EXPANSION,
            excluded_reason="some_rule",
            scope=DEMO_SCOPE,
        )

    assert _write_targets(papers_db) == before
    assert result == _split_result(paper_b.paper_id, "10.1/b", paper_a.paper_id, "W2")
    assert result.source_changed is False
    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert warnings == [
        {
            "event": "DOI 與 OpenAlex ID 屬於兩篇不同的論文，這一筆略過不寫",
            "log_level": "warning",
            "doi": "10.1/b",
            "doi_paper_id": str(paper_b.paper_id),
            "openalex_id": "W2",
            "openalex_paper_id": str(paper_a.paper_id),
        }
    ]


def test_split_identifiers_keep_doi_paper_not_demo_eligible(
    papers_db: TestDatabase, database: Database
) -> None:
    # C2：B 沒有授權（demo_eligible = false）；這筆資料的授權與出版社算出來是 true。
    # 以前會把這筆寫進 B（DOI 優先），B 變成示範站可用；現在 B 不動。
    paper_b = _upsert(database, _paper(doi="10.1/b", openalex_id=None, title="B"))
    paper_a = _upsert(database, _paper(doi=None, openalex_id="W2", title="A"))
    before = _write_targets(papers_db)
    record = _paper(
        doi="10.1/b",
        openalex_id="W2",
        title="Other work",
        fetched_at=T2,
        primary_license="cc-by",
        publisher_names=("Open Press",),
    )
    assert is_demo_eligible(
        record.primary_license,
        record.publisher_ids,
        record.publisher_names,
        DEMO_SCOPE.demo_publishers,
        journal_issn_l=None,
    )

    with database.transaction() as connection:
        result = upsert_by_external_id(
            connection,
            record,
            origin=PaperOrigin.TRACKED,
            excluded_reason=None,
            scope=DEMO_SCOPE,
        )

    assert _rows(papers_db, "SELECT demo_eligible FROM paper WHERE id = %s", paper_b.paper_id) == [
        (False,)
    ]
    assert _write_targets(papers_db) == before
    assert result == _split_result(paper_b.paper_id, "10.1/b", paper_a.paper_id, "W2")


def test_split_identifiers_win_over_stale(papers_db: TestDatabase, database: Database) -> None:
    # 判斷順序：先看識別碼是否分屬兩篇，再比新舊。這筆資料比兩篇都舊，仍回「分屬兩篇」。
    paper_b = _upsert(database, _paper(doi="10.1/b", openalex_id=None, fetched_at=T2))
    paper_a = _upsert(database, _paper(doi=None, openalex_id="W2", fetched_at=T2))
    before = _write_targets(papers_db)

    result = _upsert(database, _paper(doi="10.1/b", openalex_id="W2", fetched_at=T1))

    assert _write_targets(papers_db) == before
    assert result == _split_result(paper_b.paper_id, "10.1/b", paper_a.paper_id, "W2")


def test_split_identifiers_keep_out_of_scope_paper_private(
    papers_db: TestDatabase, database: Database
) -> None:
    # A 在追蹤範圍外、匯入的（lab_visible = false），B 在範圍內。這筆 (10.1/a, W2) 是引用擴展：
    # 以前會寫進 A（DOI 優先），A 的 lab_visible 變成 true；現在兩篇都不動。
    paper_a = _upsert(
        database,
        _paper(doi="10.1/a", openalex_id=None, title="A", journal_issn_l="1234-5678"),
        origin=PaperOrigin.IMPORT,
    )
    paper_b = _upsert(database, _paper(doi=None, openalex_id="W2", title="B"))
    assert _paper_row(papers_db, paper_a.paper_id)["lab_visible"] is False
    before = _write_targets(papers_db)

    result = _upsert(
        database,
        _paper(doi="10.1/a", openalex_id="W2", title="Other work", fetched_at=T2),
        origin=PaperOrigin.EXPANSION,
    )

    assert result == _split_result(paper_a.paper_id, "10.1/a", paper_b.paper_id, "W2")
    assert result.outcome is UpsertOutcome.IDENTIFIERS_SPLIT
    assert _paper_row(papers_db, paper_a.paper_id)["lab_visible"] is False
    assert _write_targets(papers_db) == before


# --- 同一種識別碼對不上：什麼都不寫（ADR-0020 更新 2026-11-22 第二節） --------------------------


def _mismatch_result(paper_id: uuid.UUID, scheme: str, stored: str, incoming: str) -> UpsertResult:
    identifier_scheme = IdentifierScheme(scheme)
    return UpsertResult(
        paper_id,
        UpsertOutcome.SAME_SCHEME_MISMATCH,
        mismatch=SameSchemeMismatch(
            paper_id=paper_id,
            stored=ExternalId(identifier_scheme, stored),
            incoming=ExternalId(identifier_scheme, incoming),
        ),
    )


def _mismatch_warning(paper_id: uuid.UUID, scheme: str, stored: str, incoming: str) -> object:
    return {
        "event": "論文已經有同一種、不同值的識別碼，這一筆略過不寫",
        "log_level": "warning",
        "paper_id": str(paper_id),
        "scheme": scheme,
        "stored": stored,
        "incoming": incoming,
    }


def _existing_paper_with_everything(database: Database) -> UpsertResult:
    """P＝(10.1/p, W2)：匯入的、追蹤範圍外（lab_visible = false），有摘要與關鍵字。"""
    created = _upsert(
        database,
        _paper(
            doi="10.1/p",
            openalex_id="W2",
            title="P",
            journal_issn_l="1234-5678",
            abstract="P abstract",
            abstract_source="openalex",
        ),
        origin=PaperOrigin.IMPORT,
    )
    with database.transaction() as connection:
        papers_module.update_paper_keywords(
            connection,
            created.paper_id,
            KeywordSource.OPENALEX,
            [ParsedKeyword(key="p-keyword", display_name="P keyword")],
        )
    return created


def _other_work(**overrides: object) -> ParsedPaper:
    """比 P 新、書目全部不同、授權與出版社算出來示範站可用、在追蹤範圍內的一筆資料。"""
    fields: dict[str, object] = {
        "title": "Other work",
        "fetched_at": T2,
        "publication_year": 2024,
        "abstract": "Other abstract",
        "abstract_source": "openalex",
        "primary_license": "cc-by",
        "publisher_names": ("Open Press",),
        "journal_issn_l": TRACKED_ISSN,
    }
    fields.update(overrides)
    return _paper(**fields)


@pytest.mark.parametrize(
    ("record", "scheme", "stored", "incoming"),
    [
        # 方向一：用 DOI 找到 P，帶來的 OpenAlex ID（W1）沒有主人，P 已經有 W2。
        pytest.param({"doi": "10.1/p", "openalex_id": "W1"}, "openalex", "W2", "W1", id="by-doi"),
        # 方向二：用 OpenAlex ID 找到 P，帶來的 DOI（10.1/q）沒有主人，P 已經有 10.1/p。
        pytest.param(
            {"doi": "10.1/q", "openalex_id": "W2"}, "doi", "10.1/p", "10.1/q", id="by-openalex"
        ),
    ],
)
def test_same_scheme_mismatch_writes_nothing(
    papers_db: TestDatabase,
    database: Database,
    record: dict[str, object],
    scheme: str,
    stored: str,
    incoming: str,
) -> None:
    paper_p = _existing_paper_with_everything(database)
    before = _write_targets(papers_db)
    assert before["paper_abstract"]
    assert before["paper_keyword"]

    with capture_logs() as logs, database.transaction() as connection:
        result = upsert_by_external_id(
            connection,
            _other_work(**record),
            origin=PaperOrigin.EXPANSION,
            excluded_reason="some_rule",
            scope=DEMO_SCOPE,
        )

    assert _write_targets(papers_db) == before
    assert {table: len(rows) for table, rows in _write_targets(papers_db).items()} == {
        "paper": 1,
        "external_id": 2,
        "paper_abstract": 1,
        "paper_keyword": 1,
    }
    assert result == _mismatch_result(paper_p.paper_id, scheme, stored, incoming)
    assert result.split is None
    assert result.source_changed is False
    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert warnings == [_mismatch_warning(paper_p.paper_id, scheme, stored, incoming)]


def test_same_scheme_mismatch_keeps_paper_not_demo_eligible(
    papers_db: TestDatabase, database: Database
) -> None:
    # C-H2：P 沒有授權（demo_eligible = false）；這筆 (10.1/p, W1) 的授權與出版社算出來是 true。
    # 以前會把這筆寫進 P、掛上 W1，P 變成示範站可用；現在 P 不動。
    paper_p = _upsert(database, _paper(doi="10.1/p", openalex_id="W2", title="P"))
    before = _write_targets(papers_db)
    record = _other_work(doi="10.1/p", openalex_id="W1")
    assert is_demo_eligible(
        record.primary_license,
        record.publisher_ids,
        record.publisher_names,
        DEMO_SCOPE.demo_publishers,
        journal_issn_l=record.journal_issn_l,
    )

    with database.transaction() as connection:
        result = upsert_by_external_id(
            connection, record, origin=PaperOrigin.TRACKED, excluded_reason=None, scope=DEMO_SCOPE
        )

    assert _rows(papers_db, "SELECT demo_eligible FROM paper WHERE id = %s", paper_p.paper_id) == [
        (False,)
    ]
    assert _write_targets(papers_db) == before
    assert result == _mismatch_result(paper_p.paper_id, "openalex", "W2", "W1")


def test_same_scheme_mismatch_wins_over_stale(papers_db: TestDatabase, database: Database) -> None:
    # 判斷順序：和「分屬兩篇」一樣，先看同一種識別碼對不對得上，再比新舊。這筆比 P 舊，仍回
    # 「同一種識別碼對不上」（兩種都什麼都不寫，只差在結果與警告）。
    paper_p = _upsert(database, _paper(doi="10.1/p", openalex_id="W2", fetched_at=T2))
    before = _write_targets(papers_db)

    result = _upsert(database, _paper(doi="10.1/p", openalex_id="W1", fetched_at=T1))

    assert _write_targets(papers_db) == before
    assert result == _mismatch_result(paper_p.paper_id, "openalex", "W2", "W1")


@pytest.mark.parametrize(
    "record",
    [
        # 方向一：用 DOI 找到 P，OpenAlex ID 只是寫法不同（網址、小寫），正規化後就是 W2。
        pytest.param(
            {"doi": "10.1/p", "openalex_id": "https://openalex.org/w2"}, id="by-doi-openalex-url"
        ),
        # 方向二：用 OpenAlex ID 找到 P，DOI 只是寫法不同（大寫、網址前綴），正規化後就是 10.1/p。
        pytest.param(
            {"doi": "https://doi.org/10.1/P", "openalex_id": "W2"}, id="by-openalex-doi-url"
        ),
    ],
)
def test_same_identifier_written_differently_is_not_a_mismatch(
    papers_db: TestDatabase, database: Database, record: dict[str, object]
) -> None:
    created = _upsert(database, _paper(doi="10.1/p", openalex_id="W2", title="Old"))

    result = _upsert(database, _paper(title="New", fetched_at=T2, **record))

    assert result == UpsertResult(created.paper_id, UpsertOutcome.UPDATED)
    assert _paper_row(papers_db, created.paper_id)["title"] == "New"
    assert sorted(_rows(papers_db, "SELECT scheme, value_norm, paper_id FROM external_id")) == [
        ("doi", "10.1/p", created.paper_id),
        ("openalex", "W2", created.paper_id),
    ]


@pytest.mark.parametrize(
    "record",
    [
        pytest.param({"doi": "10.1/p", "openalex_id": None}, id="doi-only"),
        pytest.param({"doi": None, "openalex_id": "W2"}, id="openalex-only"),
    ],
)
def test_only_one_identifier_updates_paper_that_has_both(
    papers_db: TestDatabase, database: Database, record: dict[str, object]
) -> None:
    # 只帶一個識別碼：沒有「另一種」可以對不上，照舊更新（行為不變）。
    created = _upsert(database, _paper(doi="10.1/p", openalex_id="W2", title="Old"))

    result = _upsert(database, _paper(title="New", fetched_at=T2, **record))

    assert result == UpsertResult(created.paper_id, UpsertOutcome.UPDATED)
    assert _paper_row(papers_db, created.paper_id)["title"] == "New"
    assert sorted(_rows(papers_db, "SELECT scheme, value_norm, paper_id FROM external_id")) == [
        ("doi", "10.1/p", created.paper_id),
        ("openalex", "W2", created.paper_id),
    ]


def test_found_by_doi_gets_the_new_openalex_id(papers_db: TestDatabase, database: Database) -> None:
    # 只有一個識別碼找到、另一個沒有主人：照舊更新並掛上（行為不變）。
    created = _upsert(database, _paper(doi="10.1/four", openalex_id=None, title="Old"))

    result = _upsert(database, _paper(doi="10.1/four", openalex_id="W4", title="New"))

    assert result == UpsertResult(created.paper_id, UpsertOutcome.UPDATED)
    assert _paper_row(papers_db, created.paper_id)["title"] == "New"
    assert sorted(_rows(papers_db, "SELECT scheme, value_norm, paper_id FROM external_id")) == [
        ("doi", "10.1/four", created.paper_id),
        ("openalex", "W4", created.paper_id),
    ]


def test_found_by_openalex_id_gets_the_new_doi(papers_db: TestDatabase, database: Database) -> None:
    created = _upsert(database, _paper(doi=None, openalex_id="W3"))

    result = _upsert(database, _paper(doi="10.1/three", openalex_id="w3"))

    assert result == UpsertResult(created.paper_id, UpsertOutcome.UPDATED)
    assert sorted(_rows(papers_db, "SELECT scheme, value_norm, paper_id FROM external_id")) == [
        ("doi", "10.1/three", created.paper_id),
        ("openalex", "W3", created.paper_id),
    ]


def _upsert_concurrently(
    db: TestDatabase,
    database: Database,
    first_paper: ParsedPaper,
    second_paper: ParsedPaper,
    first_rows: dict[str, dict[str, list[tuple[Any, ...]]]] | None = None,
) -> tuple[UpsertResult, UpsertResult]:
    """第一個交易寫完、還沒 commit 時，第二個交易開始寫；確認第二個卡在等鎖後才讓第一個 commit。

    給了 `first_rows` 時，第一個交易寫完、放行第二個之前，在同一個交易裡把它那篇四張表的整列
    存進 `first_rows["rows"]`（同 `_hold_uncommitted`：commit 不會改這些列，所以它就是第一個
    commit 後、第二個寫入前的樣子）。
    """
    first_done = threading.Event()
    release = threading.Event()
    results: dict[str, UpsertResult] = {}
    errors: list[BaseException] = []

    def first() -> None:
        try:
            with database.transaction() as connection:
                results["first"] = upsert_by_external_id(
                    connection,
                    first_paper,
                    origin=PaperOrigin.TRACKED,
                    excluded_reason=None,
                    scope=SCOPE,
                )
                if first_rows is not None:
                    first_rows["rows"] = _paper_targets_in(connection, results["first"].paper_id)
                first_done.set()
                assert release.wait(30)
        except BaseException as exc:
            errors.append(exc)
        finally:
            first_done.set()

    def second() -> None:
        try:
            assert first_done.wait(30)
            with database.transaction() as connection:
                results["second"] = upsert_by_external_id(
                    connection,
                    second_paper,
                    origin=PaperOrigin.IMPORT,
                    excluded_reason=None,
                    scope=SCOPE,
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    try:
        assert first_done.wait(30)
        _wait_until_a_backend_waits_on_a_lock(db)
    finally:
        release.set()
        for thread in threads:
            thread.join(30)

    assert errors == []
    return results["first"], results["second"]


def test_concurrent_inserts_end_with_one_paper(papers_db: TestDatabase, database: Database) -> None:
    # 第一個交易新增、還沒 commit 時，第二個交易也新增同一篇：第二個要等第一個結束，
    # 然後刪掉自己那一列、改成更新第一個建好的論文。
    first, second = _upsert_concurrently(papers_db, database, _paper(), _paper(title="From second"))

    assert first.outcome is UpsertOutcome.CREATED
    assert second.outcome is UpsertOutcome.UPDATED
    assert second.paper_id == first.paper_id
    assert _rows(papers_db, "SELECT count(*) FROM paper") == [(1,)]
    assert _rows(papers_db, "SELECT count(*) FROM external_id") == [(2,)]
    row = _paper_row(papers_db, first.paper_id)
    assert row["origin"] == "tracked"
    assert row["title"] == "From second"


def test_concurrent_inserts_sharing_only_openalex_id_end_with_one_paper(
    papers_db: TestDatabase, database: Database
) -> None:
    # 兩個交易的 DOI 不同、OpenAlex ID 相同：第二個認領到自己的 DOI，卻在 OpenAlex ID 卡住；
    # 第一個 commit 後，第二個要放掉自己剛建的論文與 DOI，重查到第一個建好的那篇。那篇已經有
    # DOI 10.1/a、帶來的 10.1/b 沒有主人：「同一種識別碼對不上」，什麼都不寫（ADR-0020 更新
    # 2026-11-22 第二節；原本是更新那篇並掛上 10.1/b）。
    first_rows: dict[str, dict[str, list[tuple[Any, ...]]]] = {}
    first, second = _upsert_concurrently(
        papers_db,
        database,
        _paper(doi="10.1/a", openalex_id="W9"),
        _paper(doi="10.1/b", openalex_id="W9"),
        first_rows,
    )

    # 四張表的整列與列數，和第一個交易寫完（commit 後、第二個寫入前）的樣子逐欄相同。
    _assert_only_these_papers_unchanged(papers_db, {first.paper_id: first_rows["rows"]})
    assert first.outcome is UpsertOutcome.CREATED
    assert second == _mismatch_result(first.paper_id, "doi", "10.1/a", "10.1/b")
    assert second.paper_id == first.paper_id
    assert _rows(papers_db, "SELECT count(*) FROM paper") == [(1,)]
    assert sorted(_rows(papers_db, "SELECT scheme, value_norm, paper_id FROM external_id")) == [
        ("doi", "10.1/a", first.paper_id),
        ("openalex", "W9", first.paper_id),
    ]
    # 論文列停在第一個交易新增時的樣子（沒有被更新過：updated_at 仍等於 created_at）。
    assert _rows(
        papers_db,
        "SELECT origin, title, openalex_fetched_at, updated_at = created_at,"
        " source_changed_at FROM paper",
    ) == [("tracked", "A study", T1, True, None)]
    assert _rows(papers_db, "SELECT count(*) FROM paper_abstract") == [(0,)]
    assert _rows(papers_db, "SELECT count(*) FROM paper_keyword") == [(0,)]


def test_concurrent_older_write_waits_for_lock_then_is_stale(
    papers_db: TestDatabase, database: Database
) -> None:
    # 「先鎖再比」：交易一寫比較新的資料、還沒 commit；交易二寫比較舊的資料，要等交易一放開
    # 論文的鎖，才讀到交易一寫的抓取時間、判成 stale。沒有 FOR UPDATE 的話，交易二會先讀到
    # commit 前的舊時間、判定可以寫，等到鎖之後把較新的資料蓋掉。
    original = _upsert(database, _paper(fetched_at=T1 - timedelta(days=7), title="Original"))

    newer, older = _upsert_concurrently(
        papers_db,
        database,
        _paper(fetched_at=T2, title="Newer"),
        _paper(fetched_at=T1, title="Older"),
    )

    assert newer == UpsertResult(original.paper_id, UpsertOutcome.UPDATED)
    assert older == UpsertResult(original.paper_id, UpsertOutcome.STALE)
    row = _paper_row(papers_db, original.paper_id)
    assert row["title"] == "Newer"
    assert row["openalex_fetched_at"] == T2


def test_withdrawn_insert_releases_identifiers_before_waiting_for_paper_lock(
    papers_db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """三個連線交錯，原本重現「撤回新增」的死結推演（ADR-0020「鎖的順序」）；現在守的是 B、C
    撤回或重查後都判成「同一種識別碼對不上」、A 那篇什麼都沒被寫（ADR-0020 更新 2026-11-22
    第二節）：

    1. A 新增 (10.1/a, W9)，還沒 commit。
    2. B 寫 (10.1/b, W9)：找不到既有的論文 → 新增、認領到 10.1/b、在 W9 等 A。
    3. A commit。B 認領 W9 失敗、退回保存點撤回自己的新增，正要去鎖 A 那篇論文時（測試在這裡讓
       B 停住）……
    4. C 寫 (10.1/b, W9)：經 W9 找到 A 那篇、先鎖住它，讀到它已經有 10.1/a、帶來的 10.1/b 沒有
       主人 → 回 `same_scheme_mismatch`，不認領 10.1/b、不寫，C 結束。
    5. B 放行後鎖住 A 那篇，同樣回 `same_scheme_mismatch`、不寫。

    C 不再認領 10.1/b，所以這條已經守不住「撤回時當下放掉識別碼、不死結」（改回在交易裡 DELETE 的
    舊做法，這條仍會通過）；那個推演改由
    `test_withdrawn_insert_then_paper_without_doi_gets_claimed_without_deadlock` 守。A 那篇四張表的
    整列在 A 寫完時存下，C 結束後、B 結束後各比一次。
    """
    database = Database(papers_db.app_url, connect_timeout=5, pool_size=3)
    a_written = threading.Event()
    release_a = threading.Event()
    b_paused = threading.Event()
    release_b = threading.Event()
    results: dict[str, UpsertResult] = {}
    a_rows: dict[str, dict[str, list[tuple[Any, ...]]]] = {}
    errors: list[tuple[str, BaseException]] = []
    original_update_existing = papers_module._update_existing

    def pause_b_before_paper_lock(*args: Any, **kwargs: Any) -> UpsertResult:
        # B 撤回新增、重查完，要去鎖既有論文之前停住，讓 C 先拿到論文的鎖。
        if threading.current_thread().name == "B":
            b_paused.set()
            assert release_b.wait(30)
        return original_update_existing(*args, **kwargs)

    monkeypatch.setattr(papers_module, "_update_existing", pause_b_before_paper_lock)

    def run(name: str, paper: ParsedPaper, before_commit: threading.Event | None = None) -> None:
        try:
            with database.transaction() as connection:
                results[name] = upsert_by_external_id(
                    connection,
                    paper,
                    origin=PaperOrigin.TRACKED,
                    excluded_reason=None,
                    scope=SCOPE,
                )
                if before_commit is not None:
                    # A 寫完、commit 前存下 A 那篇四張表的整列（commit 不會改這些列，所以它就是
                    # A commit 後、B 與 C 寫入前的樣子）。
                    a_rows["rows"] = _paper_targets_in(connection, results[name].paper_id)
                    a_written.set()
                    assert before_commit.wait(30)
        except BaseException as exc:
            errors.append((name, exc))
        finally:
            # 出錯時也放行主執行緒，讓它看到 errors 而不是空等。
            if name == "A":
                a_written.set()
            if name == "B":
                b_paused.set()

    thread_a = threading.Thread(
        target=run, name="A", args=("A", _paper(doi="10.1/a", openalex_id="W9"), release_a)
    )
    thread_b = threading.Thread(
        target=run, name="B", args=("B", _paper(doi="10.1/b", openalex_id="W9", title="B"))
    )
    thread_c = threading.Thread(
        target=run, name="C", args=("C", _paper(doi="10.1/b", openalex_id="W9", title="C"))
    )
    threads = [thread_a, thread_b, thread_c]
    try:
        thread_a.start()
        assert a_written.wait(30)
        thread_b.start()
        _wait_until_a_backend_waits_on_a_lock(papers_db)  # B 在 W9 等 A
        release_a.set()
        assert b_paused.wait(30)
        assert errors == []
        assert "B" not in results  # B 確實停在撤回之後、鎖論文之前

        thread_c.start()
        thread_c.join(15)
        # C 不必等 B：B 撤回時已經放掉 10.1/b。舊做法下 C 會卡在 10.1/b 等 B（到語句逾時）。
        assert not thread_c.is_alive()
        assert errors == []
        # A 那篇已經有 DOI 10.1/a、帶來的 10.1/b 沒有主人：「同一種識別碼對不上」，C 鎖住 A 那篇
        # 後什麼都不寫（ADR-0020 更新 2026-11-22 第二節；原本是 UPDATED）。死結的推演改由
        # `test_withdrawn_insert_then_paper_without_doi_gets_claimed_without_deadlock` 守著。
        _assert_only_these_papers_unchanged(papers_db, {results["A"].paper_id: a_rows["rows"]})
        assert results["C"] == _mismatch_result(results["A"].paper_id, "doi", "10.1/a", "10.1/b")
    finally:
        release_a.set()
        release_b.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(30)

    assert errors == []
    paper_id = results["A"].paper_id
    # B 結束後再比一次：四張表的整列與列數仍是 A 寫完時的樣子。
    _assert_only_these_papers_unchanged(papers_db, {paper_id: a_rows["rows"]})
    assert results["A"].outcome is UpsertOutcome.CREATED
    assert results["B"] == _mismatch_result(paper_id, "doi", "10.1/a", "10.1/b")
    assert _rows(papers_db, "SELECT count(*) FROM paper") == [(1,)]
    assert sorted(_rows(papers_db, "SELECT scheme, value_norm, paper_id FROM external_id")) == [
        ("doi", "10.1/a", paper_id),
        ("openalex", "W9", paper_id),
    ]
    # B、C 都沒寫：留下的是 A 新增時的論文列（updated_at 仍等於 created_at）。
    assert _paper_row(papers_db, paper_id)["title"] == "A study"
    assert _rows(papers_db, "SELECT updated_at = created_at FROM paper") == [(True,)]


def _hold_uncommitted(
    database: Database, paper: ParsedPaper, written: threading.Event, release: threading.Event
) -> tuple[
    threading.Thread,
    dict[str, UpsertResult],
    dict[str, dict[str, list[tuple[Any, ...]]]],
    list[BaseException],
]:
    """另開一個執行緒寫 `paper`、寫完先不 commit，等 `release` 才結束交易。

    寫完時在同一個交易裡把這篇四張表的整列存進 `snapshots["rows"]`：commit 不會改這些列，
    所以它就是這篇 commit 後的樣子。呼叫的測試都等另一筆寫入開始、而且已經在等鎖之後才放行
    commit，沒有別的時間點能存。那筆寫入等的不一定是這個交易：
    `test_concurrent_split_found_after_withdrawn_insert_writes_nothing` 先放行交易二時，那筆寫入
    在 10.1/a 等的是交易一，不是交易二；`test_concurrent_split_found_while_claiming_writes_nothing`
    放行的就是那筆寫入在等的交易（W2）。
    """
    results: dict[str, UpsertResult] = {}
    snapshots: dict[str, dict[str, list[tuple[Any, ...]]]] = {}
    errors: list[BaseException] = []

    def run() -> None:
        try:
            with database.transaction() as connection:
                result = upsert_by_external_id(
                    connection, paper, origin=PaperOrigin.TRACKED, excluded_reason=None, scope=SCOPE
                )
                results["result"] = result
                snapshots["rows"] = _paper_targets_in(connection, result.paper_id)
                written.set()
                assert release.wait(30)
        except BaseException as exc:
            errors.append(exc)
        finally:
            written.set()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, results, snapshots, errors


def test_concurrent_split_found_after_withdrawn_insert_writes_nothing(
    papers_db: TestDatabase,
) -> None:
    """退回保存點後重查到兩篇不同論文，也走「分屬兩篇」：

    1. 交易一新增 (10.1/a, 沒有 OpenAlex ID)、交易二新增 (沒有 DOI, W2)，都還沒 commit。
    2. 這筆 (10.1/a, W2)：找的時候兩個都沒有主人 → 新增、在 10.1/a 等交易一。
    3. 交易二先 commit，交易一再 commit：認領失敗、退回保存點，重查到 10.1/a 屬交易一那篇、
       W2 屬交易二那篇（兩個都已 commit，所以是在重查時發現，不是之後認領時）。
    """
    database = Database(papers_db.app_url, connect_timeout=5, pool_size=3)
    release_first, release_second = threading.Event(), threading.Event()
    first_written, second_written = threading.Event(), threading.Event()
    first, first_results, first_rows, first_errors = _hold_uncommitted(
        database,
        _paper(
            doi="10.1/a",
            openalex_id=None,
            title="A",
            abstract="A abstract",
            abstract_source="openalex",
        ),
        first_written,
        release_first,
    )
    second, second_results, second_rows, second_errors = _hold_uncommitted(
        database,
        _paper(
            doi=None, openalex_id="W2", title="B", abstract="B abstract", abstract_source="openalex"
        ),
        second_written,
        release_second,
    )
    results: dict[str, UpsertResult] = {}
    errors: list[BaseException] = []

    def mine() -> None:
        try:
            results["mine"] = _upsert(
                database,
                _paper(
                    doi="10.1/a",
                    openalex_id="W2",
                    title="Mine",
                    fetched_at=T2,
                    abstract="Mine abstract",
                    abstract_source="openalex",
                ),
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=mine)
    try:
        assert first_written.wait(30)
        assert second_written.wait(30)
        thread.start()
        _wait_until_a_backend_waits_on_a_lock(papers_db)  # 這筆在 10.1/a 等交易一
        release_second.set()
        second.join(30)
        assert not second.is_alive()
    finally:
        release_second.set()
        release_first.set()
        for each in (first, second, thread):
            each.join(30)

    assert first_errors == second_errors == errors == []
    paper_a = first_results["result"].paper_id
    paper_b = second_results["result"].paper_id
    assert results["mine"] == _split_result(paper_a, "10.1/a", paper_b, "W2")
    assert sorted(_rows(papers_db, "SELECT scheme, value_norm, paper_id FROM external_id")) == [
        ("doi", "10.1/a", paper_a),
        ("openalex", "W2", paper_b),
    ]
    assert sorted(_rows(papers_db, "SELECT title FROM paper")) == [("A",), ("B",)]
    assert first_rows["rows"]["paper_abstract"]
    assert second_rows["rows"]["paper_abstract"]
    _assert_only_these_papers_unchanged(
        papers_db, {paper_a: first_rows["rows"], paper_b: second_rows["rows"]}
    )


def test_concurrent_split_found_while_claiming_writes_nothing(
    papers_db: TestDatabase,
) -> None:
    """更新既有論文、認領還沒有主人的識別碼時才發現它屬於別篇，也走「分屬兩篇」：

    1. B 已經有 10.1/b（已 commit）；交易一新增 (沒有 DOI, W2) 還沒 commit。
    2. 這筆 (10.1/b, W2)：經 10.1/b 找到 B、W2 還沒有主人 → 鎖住 B、在 W2 等交易一。
    3. 交易一 commit：W2 認領失敗、屬交易一那篇 → B 什麼都不寫。
    """
    database = Database(papers_db.app_url, connect_timeout=5, pool_size=3)
    paper_b = _upsert(database, _paper(doi="10.1/b", openalex_id=None, title="B"))
    before_b = _rows(papers_db, "SELECT * FROM paper WHERE id = %s", paper_b.paper_id)
    targets_b = _paper_targets(papers_db, paper_b.paper_id)
    release = threading.Event()
    written = threading.Event()
    other, other_results, other_rows, other_errors = _hold_uncommitted(
        database,
        _paper(
            doi=None, openalex_id="W2", title="A", abstract="A abstract", abstract_source="openalex"
        ),
        written,
        release,
    )
    results: dict[str, UpsertResult] = {}
    errors: list[BaseException] = []

    def mine() -> None:
        try:
            results["mine"] = _upsert(
                database,
                _paper(
                    doi="10.1/b",
                    openalex_id="W2",
                    title="Mine",
                    fetched_at=T2,
                    abstract="Mine abstract",
                    abstract_source="openalex",
                ),
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=mine)
    try:
        assert written.wait(30)
        thread.start()
        _wait_until_a_backend_waits_on_a_lock(papers_db)  # 這筆在 W2 等交易一
    finally:
        release.set()
        for each in (other, thread):
            each.join(30)

    assert other_errors == errors == []
    paper_a = other_results["result"].paper_id
    assert results["mine"] == _split_result(paper_b.paper_id, "10.1/b", paper_a, "W2")
    assert _rows(papers_db, "SELECT * FROM paper WHERE id = %s", paper_b.paper_id) == before_b
    assert sorted(_rows(papers_db, "SELECT scheme, value_norm, paper_id FROM external_id")) == [
        ("doi", "10.1/b", paper_b.paper_id),
        ("openalex", "W2", paper_a),
    ]
    assert _rows(papers_db, "SELECT title FROM paper WHERE id = %s", paper_a) == [("A",)]
    assert other_rows["rows"]["paper_abstract"]
    _assert_only_these_papers_unchanged(
        papers_db, {paper_b.paper_id: targets_b, paper_a: other_rows["rows"]}
    )


def test_withdrawn_insert_then_paper_without_doi_gets_claimed_without_deadlock(
    papers_db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同 `test_withdrawn_insert_releases_identifiers_before_waiting_for_paper_lock` 的死結推演，
    但 A 只有 OpenAlex ID（沒有 DOI），所以 C 鎖住 A 那篇後真的會去認領 10.1/b（不會變成
    「同一種識別碼對不上」，ADR-0020 更新 2026-11-22 第二節）：

    1. A 新增 (沒有 DOI, W9)，還沒 commit。
    2. B 寫 (10.1/b, W9)：新增、認領到 10.1/b、在 W9 等 A。
    3. A commit。B 撤回自己的新增，正要去鎖 A 那篇時停住。
    4. C 寫 (10.1/b, W9)：經 W9 鎖住 A 那篇、認領 10.1/b。B 撤回時已經放掉 10.1/b，C 不必等 B。
    5. B 放行後等 C 結束，再更新同一篇（10.1/b 已經是那篇的，值相同，不算對不上）。
    """
    database = Database(papers_db.app_url, connect_timeout=5, pool_size=3)
    a_written = threading.Event()
    release_a = threading.Event()
    b_paused = threading.Event()
    release_b = threading.Event()
    results: dict[str, UpsertResult] = {}
    errors: list[tuple[str, BaseException]] = []
    original_update_existing = papers_module._update_existing

    def pause_b_before_paper_lock(*args: Any, **kwargs: Any) -> UpsertResult:
        if threading.current_thread().name == "B":
            b_paused.set()
            assert release_b.wait(30)
        return original_update_existing(*args, **kwargs)

    monkeypatch.setattr(papers_module, "_update_existing", pause_b_before_paper_lock)

    def run(name: str, paper: ParsedPaper, before_commit: threading.Event | None = None) -> None:
        try:
            with database.transaction() as connection:
                results[name] = upsert_by_external_id(
                    connection,
                    paper,
                    origin=PaperOrigin.TRACKED,
                    excluded_reason=None,
                    scope=SCOPE,
                )
                if before_commit is not None:
                    a_written.set()
                    assert before_commit.wait(30)
        except BaseException as exc:
            errors.append((name, exc))
        finally:
            if name == "A":
                a_written.set()
            if name == "B":
                b_paused.set()

    thread_a = threading.Thread(
        target=run, name="A", args=("A", _paper(doi=None, openalex_id="W9"), release_a)
    )
    thread_b = threading.Thread(
        target=run, name="B", args=("B", _paper(doi="10.1/b", openalex_id="W9", title="B"))
    )
    thread_c = threading.Thread(
        target=run, name="C", args=("C", _paper(doi="10.1/b", openalex_id="W9", title="C"))
    )
    threads = [thread_a, thread_b, thread_c]
    try:
        thread_a.start()
        assert a_written.wait(30)
        thread_b.start()
        _wait_until_a_backend_waits_on_a_lock(papers_db)  # B 在 W9 等 A
        release_a.set()
        assert b_paused.wait(30)
        assert errors == []
        assert "B" not in results

        thread_c.start()
        thread_c.join(15)
        assert not thread_c.is_alive()
        assert errors == []
        assert results["C"] == UpsertResult(results["A"].paper_id, UpsertOutcome.UPDATED)
    finally:
        release_a.set()
        release_b.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(30)

    assert errors == []
    paper_id = results["A"].paper_id
    assert results["A"].outcome is UpsertOutcome.CREATED
    assert results["B"] == UpsertResult(paper_id, UpsertOutcome.UPDATED)
    assert _rows(papers_db, "SELECT count(*) FROM paper") == [(1,)]
    assert sorted(_rows(papers_db, "SELECT scheme, value_norm, paper_id FROM external_id")) == [
        ("doi", "10.1/b", paper_id),
        ("openalex", "W9", paper_id),
    ]
    assert _paper_row(papers_db, paper_id)["title"] == "B"


def test_concurrent_different_openalex_ids_for_one_paper_attach_at_most_one(
    papers_db: TestDatabase,
) -> None:
    """兩個交易同時替同一篇 P 掛不同值的 OpenAlex ID，最多只有一個掛上：

    1. P 只有 10.1/p（已 commit）。交易一寫 (10.1/p, W1)：鎖住 P、掛上 W1、更新 P，還沒 commit。
    2. 這筆 (10.1/p, W2)：經 10.1/p 找到 P，在 P 的鎖上等交易一。
    3. 交易一 commit：這筆拿到鎖後重讀 P 的識別碼，看到 W1 → 「同一種識別碼對不上」，什麼都不寫。
    """
    database = Database(papers_db.app_url, connect_timeout=5, pool_size=3)
    paper_p = _upsert(database, _paper(doi="10.1/p", openalex_id=None, title="P"))
    release = threading.Event()
    written = threading.Event()
    other, other_results, other_rows, other_errors = _hold_uncommitted(
        database,
        _paper(doi="10.1/p", openalex_id="W1", title="From first", fetched_at=T2),
        written,
        release,
    )
    results: dict[str, UpsertResult] = {}
    captured: dict[str, list[Any]] = {}
    errors: list[BaseException] = []

    def mine() -> None:
        try:
            with capture_logs() as logs:
                results["mine"] = _upsert(
                    database,
                    _paper(
                        doi="10.1/p",
                        openalex_id="W2",
                        title="Mine",
                        fetched_at=T2 + timedelta(days=1),
                        abstract="Mine abstract",
                        abstract_source="openalex",
                    ),
                )
            captured["logs"] = logs
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=mine)
    try:
        assert written.wait(30)
        thread.start()
        _wait_until_a_backend_waits_on_a_lock(papers_db)  # 這筆在 P 的鎖上等交易一
    finally:
        release.set()
        for each in (other, thread):
            each.join(30)

    assert other_errors == errors == []
    assert other_results["result"] == UpsertResult(paper_p.paper_id, UpsertOutcome.UPDATED)
    assert results["mine"] == _mismatch_result(paper_p.paper_id, "openalex", "W1", "W2")
    assert sorted(_rows(papers_db, "SELECT scheme, value_norm, paper_id FROM external_id")) == [
        ("doi", "10.1/p", paper_p.paper_id),
        ("openalex", "W1", paper_p.paper_id),
    ]
    _assert_only_these_papers_unchanged(papers_db, {paper_p.paper_id: other_rows["rows"]})
    warnings = [entry for entry in captured["logs"] if entry["log_level"] == "warning"]
    assert warnings == [_mismatch_warning(paper_p.paper_id, "openalex", "W1", "W2")]


class _RollBackError(Exception):
    """測試用：讓交易一丟出例外、退回。"""


def test_concurrent_attach_after_first_rolls_back_attaches_the_second(
    papers_db: TestDatabase,
) -> None:
    """先到的交易替 P 掛 W1 後退回，後到的拿到鎖後照常掛上 W2（ADR-0020 更新 2026-11-22
    第二節第 4 點）：

    1. P 只有 10.1/p（已 commit）。交易一寫 (10.1/p, W1)：鎖住 P、掛上 W1、更新 P，還沒結束。
    2. 這筆 (10.1/p, W2)：經 10.1/p 找到 P，在 P 的鎖上等交易一。
    3. 交易一退回：這筆拿到鎖後重讀 P 的識別碼，讀不到 W1 → 不算對不上，照常掛上 W2、更新 P。
    """
    database = Database(papers_db.app_url, connect_timeout=5, pool_size=3)
    paper_p = _upsert(database, _paper(doi="10.1/p", openalex_id=None, title="P"))
    written = threading.Event()
    release = threading.Event()
    first_results: dict[str, UpsertResult] = {}
    first_errors: list[BaseException] = []

    def first() -> None:
        try:
            with database.transaction() as connection:
                first_results["result"] = upsert_by_external_id(
                    connection,
                    _paper(doi="10.1/p", openalex_id="W1", title="From first", fetched_at=T2),
                    origin=PaperOrigin.TRACKED,
                    excluded_reason=None,
                    scope=SCOPE,
                )
                written.set()
                assert release.wait(30)
                raise _RollBackError
        except _RollBackError:
            pass
        except BaseException as exc:
            first_errors.append(exc)
        finally:
            written.set()

    results: dict[str, UpsertResult] = {}
    errors: list[BaseException] = []

    def mine() -> None:
        try:
            results["mine"] = _upsert(
                database,
                _paper(
                    doi="10.1/p", openalex_id="W2", title="Mine", fetched_at=T2 + timedelta(days=1)
                ),
            )
        except BaseException as exc:
            errors.append(exc)

    first_thread = threading.Thread(target=first)
    thread = threading.Thread(target=mine)
    try:
        first_thread.start()
        assert written.wait(30)
        thread.start()
        _wait_until_a_backend_waits_on_a_lock(papers_db)  # 這筆在 P 的鎖上等交易一
    finally:
        release.set()
        for each in (first_thread, thread):
            each.join(30)

    assert first_errors == errors == []
    assert first_results["result"] == UpsertResult(paper_p.paper_id, UpsertOutcome.UPDATED)
    assert results["mine"] == UpsertResult(paper_p.paper_id, UpsertOutcome.UPDATED)
    assert sorted(_rows(papers_db, "SELECT scheme, value_norm, paper_id FROM external_id")) == [
        ("doi", "10.1/p", paper_p.paper_id),
        ("openalex", "W2", paper_p.paper_id),
    ]
    assert _paper_row(papers_db, paper_p.paper_id)["title"] == "Mine"
    assert _rows(papers_db, "SELECT count(*) FROM paper") == [(1,)]


def _wait_until_a_backend_waits_on_a_lock(db: TestDatabase) -> None:
    """等到這個資料庫有一條連線在等鎖（證明第二個交易真的卡在第一個後面）。"""
    deadline = time.monotonic() + 10
    with connect_admin(db.name) as admin:
        while time.monotonic() < deadline:
            row = admin.execute(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = %s AND wait_event_type = 'Lock'",
                (db.name,),
            ).fetchone()
            if row is not None and row[0] > 0:
                return
            time.sleep(0.05)
    pytest.fail("第二個交易沒有等第一個交易的鎖")


# --- 追蹤範圍與 lab_visible ------------------------------------------------------------------


def test_tracked_paper_in_scope_is_lab_visible(papers_db: TestDatabase, database: Database) -> None:
    result = _upsert(database, _paper())

    row = _paper_row(papers_db, result.paper_id)
    assert (row["origin"], row["in_scope"], row["lab_visible"]) == ("tracked", True, True)


def test_imported_paper_out_of_scope_is_private(
    papers_db: TestDatabase, database: Database
) -> None:
    result = _upsert(database, _paper(journal_issn_l="1234-5678"), origin=PaperOrigin.IMPORT)

    row = _paper_row(papers_db, result.paper_id)
    assert (row["origin"], row["in_scope"], row["lab_visible"]) == ("import", False, False)


def test_expansion_out_of_scope_is_lab_visible(papers_db: TestDatabase, database: Database) -> None:
    result = _upsert(database, _paper(publication_year=2010), origin=PaperOrigin.EXPANSION)

    row = _paper_row(papers_db, result.paper_id)
    assert (row["in_scope"], row["lab_visible"]) == (False, True)


def test_lab_visible_never_goes_back_and_origin_is_kept(
    papers_db: TestDatabase, database: Database
) -> None:
    created = _upsert(database, _paper())

    _upsert(database, _paper(journal_issn_l="1234-5678", fetched_at=T2), origin=PaperOrigin.IMPORT)

    row = _paper_row(papers_db, created.paper_id)
    assert (row["origin"], row["in_scope"], row["lab_visible"]) == ("tracked", False, True)


def test_expansion_update_makes_private_paper_lab_visible(
    papers_db: TestDatabase, database: Database
) -> None:
    created = _upsert(database, _paper(journal_issn_l="1234-5678"), origin=PaperOrigin.IMPORT)
    with database.transaction() as connection:
        assert get_paper(MEMBER, connection, created.paper_id) is None

    result = _upsert(
        database,
        _paper(journal_issn_l="1234-5678", fetched_at=T2),
        origin=PaperOrigin.EXPANSION,
    )

    assert result == UpsertResult(created.paper_id, UpsertOutcome.UPDATED)
    row = _paper_row(papers_db, created.paper_id)
    assert (row["origin"], row["in_scope"], row["lab_visible"]) == ("import", False, True)
    with database.transaction() as connection:
        assert get_paper(MEMBER, connection, created.paper_id) is not None


def test_update_into_scope_turns_on_both_flags(papers_db: TestDatabase, database: Database) -> None:
    created = _upsert(
        database,
        _paper(journal_issn_l="1234-5678", publication_year=2010),
        origin=PaperOrigin.IMPORT,
    )

    _upsert(database, _paper(fetched_at=T2), origin=PaperOrigin.IMPORT)

    row = _paper_row(papers_db, created.paper_id)
    assert (row["in_scope"], row["lab_visible"]) == (True, True)


def test_excluded_reason_is_overwritten_by_each_write(
    papers_db: TestDatabase, database: Database
) -> None:
    created = _upsert(database, _paper())

    _upsert(database, _paper(fetched_at=T2), excluded_reason="editorial")
    assert _paper_row(papers_db, created.paper_id)["excluded_reason"] == "editorial"

    _upsert(database, _paper(fetched_at=T2 + timedelta(days=7)), excluded_reason=None)
    assert _paper_row(papers_db, created.paper_id)["excluded_reason"] is None


def test_excluded_reason_is_stored(papers_db: TestDatabase, database: Database) -> None:
    result = _upsert(database, _paper(title="Editor's Comments"), excluded_reason="editorial")

    assert _paper_row(papers_db, result.paper_id)["excluded_reason"] == "editorial"


# --- 變動偵測：source_hash 變了才記 source_changed_at（Story 1.11、ADR-0028） --------------------


def _changed_at(db: TestDatabase, paper_id: uuid.UUID) -> tuple[Any, ...]:
    [row] = _rows(db, "SELECT source_hash, source_changed_at FROM paper WHERE id = %s", paper_id)
    return row


def test_new_paper_has_no_source_change(papers_db: TestDatabase, database: Database) -> None:
    created = _upsert(database, _paper())

    assert created.outcome is UpsertOutcome.CREATED
    assert created.source_changed is False
    source_hash, changed_at = _changed_at(papers_db, created.paper_id)
    assert source_hash == _paper().source_hash
    assert changed_at is None


def test_same_fingerprint_is_not_a_change_even_if_citations_change(
    papers_db: TestDatabase, database: Database
) -> None:
    created = _upsert(database, _paper(cited_by_count=1))

    # 被引用次數、引用清單、文章類型不在指紋裡：照樣覆寫，但不算變動。
    result = _upsert(
        database,
        _paper(fetched_at=T2, cited_by_count=9, referenced_works=("W77",), work_type="review"),
    )

    assert result.outcome is UpsertOutcome.UPDATED
    assert result.source_changed is False
    assert _changed_at(papers_db, created.paper_id) == (_paper().source_hash, None)
    assert _rows(papers_db, "SELECT cited_by_count FROM paper") == [(9,)]


@pytest.mark.parametrize(
    "change",
    [
        {"abstract": "A new abstract.", "abstract_source": "openalex"},
        {"is_retracted": True},
        {"volume": "42"},
    ],
    ids=["abstract", "retracted", "volume"],
)
def test_changed_fingerprint_sets_source_changed_at(
    papers_db: TestDatabase, database: Database, change: dict[str, object]
) -> None:
    created = _upsert(database, _paper())
    changed = _paper(fetched_at=T2, **change)
    assert changed.source_hash != _paper().source_hash

    with database.transaction() as connection:
        before_write = connection.exec_driver_sql("SELECT now()").scalar_one()
    result = _upsert(database, changed)

    assert result.outcome is UpsertOutcome.UPDATED
    assert result.source_changed is True
    source_hash, changed_at = _changed_at(papers_db, created.paper_id)
    assert source_hash == changed.source_hash
    assert changed_at is not None
    assert changed_at >= before_write
    with database.transaction() as connection:
        stored = get_paper(SystemViewer(), connection, created.paper_id)
    assert stored is not None
    assert stored.source_changed_at == changed_at

    # 之後同指紋再寫一次：保留原本的變動時間，不算變動。
    again = _upsert(database, _paper(fetched_at=T2 + timedelta(days=1), **change))
    assert again.source_changed is False
    assert _changed_at(papers_db, created.paper_id) == (changed.source_hash, changed_at)


def test_missing_old_fingerprint_counts_as_a_change(
    papers_db: TestDatabase, database: Database
) -> None:
    # 舊的 source_hash 是 NULL（例如 1.7 之前寫的）：要寫的不同，算變動。
    created = _upsert(database, _paper())
    _owner(papers_db, "UPDATE paper SET source_hash = NULL WHERE id = %s", created.paper_id)

    result = _upsert(database, _paper(fetched_at=T2))

    assert result.source_changed is True
    source_hash, changed_at = _changed_at(papers_db, created.paper_id)
    assert source_hash == _paper().source_hash
    assert changed_at is not None


def test_source_changed_at_is_the_statement_time_not_the_transaction_start(
    papers_db: TestDatabase, database: Database
) -> None:
    # Codex C3：同一個交易裡先停一下再寫，記下的變動時間要是那一句執行的時間（clock_timestamp），
    # 不是交易開始的時間（now()）。
    created = _upsert(database, _paper())

    with database.transaction() as connection:
        started = connection.exec_driver_sql("SELECT now()").scalar_one()
        connection.exec_driver_sql("SELECT pg_sleep(0.3)")
        result = upsert_by_external_id(
            connection,
            _paper(fetched_at=T2, volume="7"),
            origin=PaperOrigin.TRACKED,
            excluded_reason=None,
            scope=SCOPE,
        )
        same_transaction_now = connection.exec_driver_sql("SELECT now()").scalar_one()

    assert result.source_changed is True
    _hash, changed_at = _changed_at(papers_db, created.paper_id)
    assert same_transaction_now == started
    assert changed_at - started >= timedelta(seconds=0.3)


def test_source_changed_at_is_taken_after_waiting_for_the_paper_lock(
    papers_db: TestDatabase, database: Database
) -> None:
    # Codex C3：較新的單篇寫入先開交易，等每週套用持有的論文列鎖；套用在最後發佈快照
    # （as_of＝clock_timestamp()）後 commit。它拿到鎖之後才寫的 source_changed_at 要晚於那個
    # as_of，用「前後兩次快照的 as_of」篩選時才不會漏掉。
    created = _upsert(database, _paper())
    results: dict[str, Any] = {}
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            with database.transaction() as connection:
                results["started"] = connection.exec_driver_sql("SELECT now()").scalar_one()
                results["upsert"] = upsert_by_external_id(
                    connection,
                    _paper(fetched_at=T2, volume="8"),
                    origin=PaperOrigin.IMPORT,
                    excluded_reason=None,
                    scope=SCOPE,
                )
        except BaseException as exc:
            errors.append(exc)

    with papers_db.connect(APP_ROLE, autocommit=False) as apply_connection:
        # 每週套用的交易：鎖著這篇論文。
        apply_connection.execute(
            "SELECT id FROM paper WHERE id = %s FOR UPDATE", (created.paper_id,)
        )
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            _wait_until_a_backend_waits_on_a_lock(papers_db)
            # 寫入已經開了交易、正在等鎖；套用這時才發佈快照、commit。
            row = apply_connection.execute("SELECT clock_timestamp()").fetchone()
            assert row is not None
            as_of = row[0]
            apply_connection.commit()
        finally:
            thread.join(30)
    assert not thread.is_alive()

    assert errors == []
    assert results["upsert"].source_changed is True
    _hash, changed_at = _changed_at(papers_db, created.paper_id)
    assert results["started"] < as_of < changed_at


def test_stale_write_is_not_a_change(papers_db: TestDatabase, database: Database) -> None:
    created = _upsert(database, _paper(fetched_at=T2))

    result = _upsert(database, _paper(fetched_at=T1, volume="99"))

    assert result.outcome is UpsertOutcome.STALE
    assert result.source_changed is False
    assert _changed_at(papers_db, created.paper_id) == (_paper().source_hash, None)


# --- 讀者身分 -----------------------------------------------------------------------------


def test_get_paper_follows_viewer(database: Database) -> None:
    shared = _upsert(database, _paper(doi="10.1/shared", openalex_id=None))
    private = _upsert(
        database,
        _paper(doi="10.1/private", openalex_id=None, journal_issn_l=None),
        origin=PaperOrigin.IMPORT,
    )

    with database.transaction() as connection:
        member_shared = get_paper(MEMBER, connection, shared.paper_id)
        member_private = get_paper(MEMBER, connection, private.paper_id)
        system_private = get_paper(SystemViewer(), connection, private.paper_id)
        public_shared = get_paper(PublicViewer(), connection, shared.paper_id)
        missing = get_paper(SystemViewer(), connection, uuid.uuid4())

    assert member_shared is not None
    assert member_shared.id == shared.paper_id
    assert member_shared.origin is PaperOrigin.TRACKED
    assert member_shared.in_scope is True
    assert member_shared.openalex_fetched_at == T1
    assert member_private is None
    assert system_private is not None
    assert system_private.in_scope is False
    # 沒有發佈的快照，不在 counted_paper。
    assert public_shared is None
    assert missing is None


def _owner(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(OWNER_ROLE) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def _snapshot(db: TestDatabase, number: int, status: str) -> uuid.UUID:
    """用擁有者直接造一筆每週更新與它的快照（只在測試裡；正式只由每週更新的套用寫）。"""
    [(run_id,)] = _owner(db, "INSERT INTO weekly_run (status) VALUES ('failed') RETURNING id")
    as_of = T2 if status == "published" else None
    [(snapshot_id,)] = _owner(
        db,
        "INSERT INTO snapshot (number, weekly_run_id, status, as_of) VALUES (%s, %s, %s, %s)"
        " RETURNING id",
        number,
        run_id,
        status,
        as_of,
    )
    return uuid.UUID(str(snapshot_id))


def test_public_viewer_sees_counted_papers(papers_db: TestDatabase, database: Database) -> None:
    # PublicViewer 看得到的論文 ⇔ 在 counted_paper 裡（AD-4、AD-5）：追蹤範圍內、沒有被排除、
    # first_snapshot_id 指向已發佈的快照。
    published = _snapshot(papers_db, 1, "published")
    building = _snapshot(papers_db, 2, "building")
    failed = _snapshot(papers_db, 3, "failed")
    counted = _upsert(database, _paper(doi="10.1/a", openalex_id=None))
    excluded = _upsert(
        database, _paper(doi="10.1/b", openalex_id=None), excluded_reason="editors_comments"
    )
    out_of_scope = _upsert(
        database,
        _paper(doi="10.1/c", openalex_id=None, journal_issn_l="0960-085X"),
        origin=PaperOrigin.IMPORT,
    )
    unassigned = _upsert(database, _paper(doi="10.1/d", openalex_id=None))
    on_building = _upsert(database, _paper(doi="10.1/e", openalex_id=None))
    on_failed = _upsert(database, _paper(doi="10.1/f", openalex_id=None))
    for result, snapshot_id in (
        (counted, published),
        (excluded, published),
        (out_of_scope, published),  # 範圍外的即使指定了已發佈的快照也一樣不算
        (on_building, building),
        (on_failed, failed),
    ):
        _owner(
            papers_db,
            "UPDATE paper SET first_snapshot_id = %s WHERE id = %s",
            snapshot_id,
            result.paper_id,
        )

    public = PublicViewer()
    with database.transaction() as connection:
        seen = {
            name: get_paper(public, connection, result.paper_id)
            for name, result in {
                "counted": counted,
                "excluded": excluded,
                "out_of_scope": out_of_scope,
                "unassigned": unassigned,
                "on_building": on_building,
                "on_failed": on_failed,
            }.items()
        }
        system_sees = get_paper(SystemViewer(), connection, out_of_scope.paper_id)

    counted_paper = seen.pop("counted")
    assert counted_paper is not None
    assert counted_paper.id == counted.paper_id
    assert seen == dict.fromkeys(seen)  # 其他都是 None
    assert system_sees is not None


# --- CHECK 約束 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        # in_scope 為真、lab_visible 為假
        "INSERT INTO paper (origin, title, in_scope, lab_visible)"
        " VALUES ('tracked', 't', true, false)",
        # origin 不在四種值
        "INSERT INTO paper (origin, title, in_scope, lab_visible) VALUES ('x', 't', false, false)",
        # excluded_reason 是空字串
        "INSERT INTO paper (origin, title, in_scope, lab_visible, excluded_reason)"
        " VALUES ('import', 't', false, false, '')",
    ],
)
def test_paper_check_constraints(papers_db: TestDatabase, statement: str) -> None:
    with papers_db.connect(APP_ROLE) as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(statement)


def test_lab_visible_cannot_be_turned_off_for_in_scope_paper(
    papers_db: TestDatabase, database: Database
) -> None:
    result = _upsert(database, _paper())

    with papers_db.connect(APP_ROLE) as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute("UPDATE paper SET lab_visible = false WHERE id = %s", (result.paper_id,))


@pytest.mark.parametrize(("scheme", "value"), [("isbn", "123"), ("doi", "")])
def test_external_id_check_constraints(
    papers_db: TestDatabase, database: Database, scheme: str, value: str
) -> None:
    result = _upsert(database, _paper())

    with papers_db.connect(APP_ROLE) as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "INSERT INTO external_id (paper_id, scheme, value_norm) VALUES (%s, %s, %s)",
            (result.paper_id, scheme, value),
        )


def test_external_id_is_unique_per_scheme(papers_db: TestDatabase, database: Database) -> None:
    result = _upsert(database, _paper())

    with papers_db.connect(APP_ROLE) as connection, pytest.raises(psycopg.errors.UniqueViolation):
        connection.execute(
            "INSERT INTO external_id (paper_id, scheme, value_norm) VALUES (%s, 'doi', '10.1/abc')",
            (result.paper_id,),
        )
