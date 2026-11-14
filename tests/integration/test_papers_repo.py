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
from radar.domain.papers import PaperOrigin, ParsedPaper, UpsertOutcome, UpsertResult
from radar.domain.scope import ScopeSettings
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer
from radar.repo import papers as papers_module
from radar.repo.db import Database
from radar.repo.papers import get_paper, upsert_by_external_id
from tests.database import APP_ROLE, TestDatabase, connect_admin, migrate

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
    paper_a = _upsert(database, _paper(doi="10.1/a", openalex_id=None, title="A"))
    paper_b = _upsert(database, _paper(doi=None, openalex_id="W2", title="B"))

    with capture_logs() as logs:
        result = _upsert(
            database, _paper(doi="10.1/a", openalex_id="W2", title="A2", fetched_at=T2)
        )

    assert result.paper_id == paper_a.paper_id
    assert result.outcome is UpsertOutcome.UPDATED
    assert result.conflicts == (ExternalId(IdentifierScheme.OPENALEX, "W2"),)
    assert _paper_row(papers_db, paper_a.paper_id)["title"] == "A2"
    assert _paper_row(papers_db, paper_b.paper_id)["title"] == "B"
    assert _rows(
        papers_db,
        "SELECT paper_id FROM external_id WHERE scheme = 'openalex' AND value_norm = 'W2'",
    ) == [(paper_b.paper_id,)]
    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["owner_paper_id"] == str(paper_b.paper_id)


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
) -> tuple[UpsertResult, UpsertResult]:
    """第一個交易寫完、還沒 commit 時，第二個交易開始寫；確認第二個卡在等鎖後才讓第一個 commit。"""
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
    assert second.conflicts == ()
    assert _rows(papers_db, "SELECT count(*) FROM paper") == [(1,)]
    assert _rows(papers_db, "SELECT count(*) FROM external_id") == [(2,)]
    row = _paper_row(papers_db, first.paper_id)
    assert row["origin"] == "tracked"
    assert row["title"] == "From second"


def test_concurrent_inserts_sharing_only_openalex_id_end_with_one_paper(
    papers_db: TestDatabase, database: Database
) -> None:
    # 兩個交易的 DOI 不同、OpenAlex ID 相同：第二個認領到自己的 DOI，卻在 OpenAlex ID 卡住；
    # 第一個 commit 後，第二個要放掉自己剛建的論文與 DOI，改成更新第一個建好的那篇。
    first, second = _upsert_concurrently(
        papers_db,
        database,
        _paper(doi="10.1/a", openalex_id="W9"),
        _paper(doi="10.1/b", openalex_id="W9"),
    )

    assert first.outcome is UpsertOutcome.CREATED
    assert second.outcome is UpsertOutcome.UPDATED
    assert second.paper_id == first.paper_id
    assert _rows(papers_db, "SELECT count(*) FROM paper") == [(1,)]
    assert sorted(_rows(papers_db, "SELECT scheme, value_norm, paper_id FROM external_id")) == [
        ("doi", "10.1/a", first.paper_id),
        ("doi", "10.1/b", first.paper_id),
        ("openalex", "W9", first.paper_id),
    ]


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
    """三個連線交錯，重現「撤回新增」的死結推演（ADR-0020「鎖的順序」）：

    1. A 新增 (10.1/a, W9)，還沒 commit。
    2. B 寫 (10.1/b, W9)：找不到既有的論文 → 新增、認領到 10.1/b、在 W9 等 A。
    3. A commit。B 認領 W9 失敗、撤回自己的新增，正要去鎖 A 那篇論文時（測試在這裡讓 B 停住）……
    4. C 寫 (10.1/b, W9)：經 W9 找到 A 那篇、先鎖住它，再認領 10.1/b。

    撤回時如果還佔著 10.1/b（舊做法：在交易裡 DELETE，刪掉的列要等整個交易結束才放），C 會等 B，
    B 放行後又等 C 的論文鎖，互等成死結。現在 B 撤回是退回保存點，10.1/b 當下就放掉：C 不必等 B
    就能做完並 commit；B 放行後等 C 結束，再更新同一篇。
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
        ("doi", "10.1/a", paper_id),
        ("doi", "10.1/b", paper_id),
        ("openalex", "W9", paper_id),
    ]
    # B 在 C commit 之後才寫，所以留下的是 B 的標題。
    assert _paper_row(papers_db, paper_id)["title"] == "B"


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
    # Story 1.10 之前，PublicViewer 一篇都看不到。
    assert public_shared is None
    assert missing is None


@pytest.mark.skip(
    reason="等 Story 1.10 建好 counted_paper：PublicViewer 只看得到 counted_paper 裡的論文"
)
def test_public_viewer_sees_counted_papers() -> None:
    raise AssertionError("等 Story 1.10")


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
