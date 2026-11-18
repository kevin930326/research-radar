"""`run_backup`／`run_restore` 對真的 PostgreSQL（radar-ci），外部工具換成替身。

Story 3.18、ADR-0051。

不需要 pg_dump 與 age，本機也跑得到：
- 清單的筆數和 `pg_dump --snapshot` 匯入的快照是同一個時間點（替身真的匯入那個快照）；
- 上傳時匯出快照的交易已經結束（不在開著的交易裡呼叫外部服務）；
- `pg_dump` 太舊、傾印在結尾才失敗時，物件儲存沒有這個鍵；
- 還原前的檢查（目標不是空的、版本太舊）不呼叫還原；只有共用資料的備份把成員資料表交給還原的檢查；
- 筆數不一致時印出哪一項差多少，還原的資料留著。
真的工具的往返在 `test_backup_restore.py`。
"""

import io
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from io import Reader

import psycopg
import pytest
from sqlalchemy import text

from radar.adapters.fake.object_store import FakeObjectStore
from radar.domain.backup import (
    MEMBER_TABLES,
    PAYLOAD_MAGIC,
    BackupFormatError,
    BackupManifest,
    BackupMode,
    BackupToolError,
    DumpRequest,
    PgDumpTooOldError,
    RestoreCountMismatchError,
    RestoreRefusedError,
    RestoreRequest,
    RestoreVerificationError,
    SharedOnlyBackupRefusedError,
    encode_payload_header,
)
from radar.domain.viewer import SystemViewer
from radar.repo.backup import get_restore_request
from radar.repo.db import Database
from radar.services.backup import run_backup, run_restore
from tests.backup_support import Seeded, count_in_snapshot, seed_source, table_rows
from tests.database import TestDatabase, create_database, drop_database, migrate
from tests.llm_support import create_paper, execute

RECIPIENT = "age1" + "q" * 58
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
FAKE_DUMP = b"PGDMP-fake-dump-bytes"


@pytest.fixture
def source(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def seeded(source: TestDatabase) -> Seeded:
    return seed_source(source)


@pytest.fixture
def target() -> Iterator[TestDatabase]:
    """另一個空的資料庫（有兩個擴充套件、還沒遷移），還原的目標。"""
    database = create_database(with_extensions=True)
    try:
        yield database
    finally:
        drop_database(database)


def _open_transactions(db: TestDatabase) -> int:
    with psycopg.connect(db.admin_url, autocommit=True) as connection:
        row = connection.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = %s"
            " AND state LIKE 'idle in transaction%%' AND pid <> pg_backend_pid()",
            (db.name,),
        ).fetchone()
    assert row is not None
    return int(row[0])


class FakeDump:
    """`start_dump` 的替身：記下請求，用另一條連線真的匯入快照，回「清單＋假傾印」的串流。"""

    def __init__(self, db: TestDatabase, *, fail_at_end: bool = False) -> None:
        self.db = db
        self.fail_at_end = fail_at_end
        self.requests: list[DumpRequest] = []
        self.open_at_start: int | None = None
        self.papers_in_snapshot: int | None = None

    @contextmanager
    def __call__(self, request: DumpRequest) -> Iterator[Reader[bytes]]:
        self.requests.append(request)
        self.open_at_start = _open_transactions(self.db)
        # 和 pg_dump --snapshot 一樣匯入快照；匯入之後別人新增（而且算數）的論文看不到。
        with psycopg.connect(self.db.app_url) as dump:
            dump.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            dump.execute(f"SET TRANSACTION SNAPSHOT '{request.snapshot_id}'")
            late = create_paper(
                Database(self.db.app_url, connect_timeout=5),
                doi="10.5555/after-snapshot",
                openalex_id="W9000999",
            )
            [(published,)] = execute(self.db, "SELECT id FROM snapshot WHERE status = 'published'")
            count_in_snapshot(self.db, late, published)
            row = dump.execute("SELECT count(*) FROM counted_paper").fetchone()
            assert row is not None
            self.papers_in_snapshot = int(row[0])
            dump.execute("COMMIT")
        yield _Stream(request.header + FAKE_DUMP, fail_at_end=self.fail_at_end)


class _Stream:
    def __init__(self, data: bytes, *, fail_at_end: bool) -> None:
        self._data = io.BytesIO(data)
        self._fail_at_end = fail_at_end

    def read(self, size: int = -1, /) -> bytes:
        chunk = self._data.read(size)
        if self._fail_at_end and (not chunk or len(chunk) < size or size < 0):
            raise BackupToolError("pg_dump", "失敗（退出碼 1）")
        return chunk


class GuardedStore(FakeObjectStore):
    """上傳時記下這個資料庫還有幾個開著的交易。"""

    def __init__(self, db: TestDatabase) -> None:
        super().__init__()
        self.db = db
        self.open_during_upload: int | None = None

    def put_stream(self, key: str, stream: Reader[bytes], *, content_type: str) -> None:
        self.open_during_upload = _open_transactions(self.db)
        super().put_stream(key, stream, content_type=content_type)


def _backup(
    db: TestDatabase,
    store: FakeObjectStore,
    dump: FakeDump,
    mode: BackupMode,
    *,
    pg_dump_version: str = "18.7",
) -> BackupManifest:
    result = run_backup(
        Database(db.app_url, connect_timeout=5, pool_size=2),
        store,
        mode=mode,
        recipient=RECIPIENT,
        pg_dump_version=lambda: pg_dump_version,
        start_dump=dump,
        now=lambda: NOW,
    )
    assert result.object_key in store.objects
    return result.manifest


# --- 備份 -------------------------------------------------------------------------------------


def test_full_backup_counts_come_from_the_dumped_snapshot(
    source: TestDatabase, seeded: Seeded
) -> None:
    store = GuardedStore(source)
    dump = FakeDump(source)

    manifest = _backup(source, store, dump, BackupMode.FULL)

    # 清單是快照那一刻算數的 1 篇（另一篇沒進已發佈的快照，不算）；替身匯入快照後才新增第 3 篇
    # 並讓它算數，快照裡也只有 1 篇。
    assert manifest.counts.counted_paper == 1
    assert dump.papers_in_snapshot == 1
    assert table_rows(source, "paper") == 3
    assert table_rows(source, "counted_paper") == 2
    assert manifest.counts.zh_summary_active == 1
    assert manifest.counts.classification_active == 1
    assert manifest.counts.research_model_active == 1
    assert manifest.counts.embedding == 1
    assert set(manifest.member_table_rows) == set(MEMBER_TABLES)
    assert manifest.member_table_rows["member"] == 2
    assert manifest.member_table_rows["allowed_email"] == 2
    assert manifest.member_table_rows["session"] == 1
    assert manifest.member_table_rows["judgment_event"] == 1
    assert manifest.member_table_rows["library_item"] == 1
    assert manifest.member_table_rows["deleted_record"] == 1
    assert manifest.member_table_rows["audit_event"] == table_rows(source, "audit_event") > 0
    assert manifest.alembic_revisions == tuple(sorted(source.revisions()))
    assert re.fullmatch(r"\d+(\.\d+)*", manifest.postgres_version)
    assert manifest.pg_dump_version == "18.7"
    # 審查 T5：版本與時間對真的值（記錯 pgvector 版本會讓還原的版本檢查悄悄放行）。
    [(pgvector, server_version_num)] = execute(
        source,
        "SELECT extversion, current_setting('server_version_num')::int"
        " FROM pg_extension WHERE extname = 'vector'",
    )
    assert manifest.pgvector_version == pgvector
    assert manifest.postgres_version_num == server_version_num
    assert manifest.created_at == NOW
    assert dump.requests[0].exclude_table_data == ()
    # 開始傾印時匯出快照的交易還開著；上傳時已經結束。
    assert dump.open_at_start == 1
    assert store.open_during_upload == 0


def test_object_is_the_header_followed_by_the_dump(source: TestDatabase, seeded: Seeded) -> None:
    store = FakeObjectStore()
    manifest = _backup(source, store, FakeDump(source), BackupMode.FULL)

    [(key, data)] = store.objects.items()
    assert key == "backup/20261118T120000Z-full.radar-backup.age"
    assert data == encode_payload_header(manifest) + FAKE_DUMP
    assert store.content_types[key] == "application/octet-stream"


def test_shared_only_excludes_member_table_data_and_records_zero_rows(
    source: TestDatabase, seeded: Seeded
) -> None:
    dump = FakeDump(source)

    manifest = _backup(source, FakeObjectStore(), dump, BackupMode.SHARED_ONLY)

    assert manifest.mode is BackupMode.SHARED_ONLY
    assert manifest.member_table_rows == dict.fromkeys(MEMBER_TABLES, 0)
    assert dump.requests[0].exclude_table_data == tuple(sorted(MEMBER_TABLES))
    assert manifest.counts.counted_paper == 1


def test_old_pg_dump_stops_before_dumping(source: TestDatabase, seeded: Seeded) -> None:
    store = FakeObjectStore()
    dump = FakeDump(source)

    with pytest.raises(PgDumpTooOldError, match="17 版"):
        run_backup(
            Database(source.app_url, connect_timeout=5),
            store,
            mode=BackupMode.FULL,
            recipient=RECIPIENT,
            pg_dump_version=lambda: "17.6",
            start_dump=dump,
            now=lambda: NOW,
        )

    assert dump.requests == []
    assert store.objects == {}


def test_dump_failing_at_the_end_leaves_no_object(source: TestDatabase, seeded: Seeded) -> None:
    store = FakeObjectStore()

    with pytest.raises(BackupToolError, match="pg_dump"):
        _backup(source, store, FakeDump(source, fail_at_end=True), BackupMode.FULL)

    assert store.objects == {}


# --- 還原（還原本身換成替身） ------------------------------------------------------------------


class FakeRestore:
    """`restore_archive` 的替身：記下請求與讀到的位元組；`schema_from` 有給時把目標遷移好
    （模擬還原了結構、沒有任何資料）。"""

    def __init__(self, schema_from: bool = False) -> None:
        self.schema_from = schema_from
        self.requests: list[RestoreRequest] = []
        self.body: bytes | None = None
        self.target: TestDatabase | None = None

    def __call__(self, request: RestoreRequest, stream: Reader[bytes]) -> None:
        self.requests.append(request)
        self.body = stream.read()
        if self.schema_from:
            assert self.target is not None
            migrate(self.target)


def _payload(manifest: BackupManifest) -> io.BytesIO:
    return io.BytesIO(encode_payload_header(manifest) + FAKE_DUMP)


def _restore(target: TestDatabase, payload: io.BytesIO, restore: FakeRestore) -> None:
    restore.target = target
    run_restore(
        Database(target.owner_url, connect_timeout=5),
        payload,
        pg_restore_version=lambda: "18.7",
        restore_archive=restore,
    )


@pytest.fixture
def manifest(source: TestDatabase, seeded: Seeded) -> BackupManifest:
    return _backup(source, FakeObjectStore(), FakeDump(source), BackupMode.FULL)


def test_non_empty_target_is_refused_before_restoring(
    manifest: BackupManifest, target: TestDatabase
) -> None:
    migrate(target)
    restore = FakeRestore()

    with pytest.raises(RestoreRefusedError, match="不是空的") as caught:
        _restore(target, _payload(manifest), restore)

    assert "public.alembic_version" in caught.value.problems[0]
    assert restore.requests == []


@pytest.mark.parametrize(
    "create",
    ["CREATE TABLE stray (id int)", "CREATE VIEW stray AS SELECT 1 AS id", "CREATE SEQUENCE stray"],
)
def test_one_stray_empty_table_makes_the_target_non_empty(
    manifest: BackupManifest, target: TestDatabase, create: str
) -> None:
    # 「空的」是沒有任何使用者的資料表、view、序列（ADR-0051），不是「每張表都是 0 列」。
    # 審查 T6：表、view、序列各一個。
    with psycopg.connect(target.admin_url, autocommit=True) as connection:
        connection.execute(create)
    restore = FakeRestore()

    with pytest.raises(RestoreRefusedError, match=r"public\.stray"):
        _restore(target, _payload(manifest), restore)

    assert restore.requests == []


def test_extensions_alone_count_as_empty(manifest: BackupManifest, target: TestDatabase) -> None:
    restore = FakeRestore(schema_from=True)

    with pytest.raises(RestoreCountMismatchError):
        _restore(target, _payload(manifest), restore)

    [request] = restore.requests
    # 預設模式：還原前、commit 前的檢查都有，但不查成員資料（審查 C2、C3）。
    assert "RR002" in request.before_restore_sql
    assert "RR003" in request.before_commit_sql
    assert "RR001" not in request.before_commit_sql
    assert restore.body == FAKE_DUMP


def test_older_target_is_refused_with_both_versions(
    manifest: BackupManifest, target: TestDatabase
) -> None:
    newer = manifest.model_copy(
        update={
            "postgres_version": "99.0",
            "postgres_version_num": 990000,
            "pgvector_version": "9.9",
        }
    )
    restore = FakeRestore()

    with pytest.raises(RestoreRefusedError) as caught:
        _restore(target, _payload(newer), restore)

    assert any("比備份時的 99.0 舊" in p for p in caught.value.problems)
    assert any("比備份時的 9.9 舊" in p for p in caught.value.problems)
    assert restore.requests == []


def test_old_pg_restore_is_refused(manifest: BackupManifest, target: TestDatabase) -> None:
    restore = FakeRestore()

    with pytest.raises(RestoreRefusedError, match="pg_restore 是 17 版"):
        run_restore(
            Database(target.owner_url, connect_timeout=5),
            _payload(manifest),
            pg_restore_version=lambda: "17.6",
            restore_archive=restore,
        )

    assert restore.requests == []


def test_shared_only_backup_asks_the_restore_to_check_member_tables(
    source: TestDatabase, seeded: Seeded, target: TestDatabase
) -> None:
    shared = _backup(source, FakeObjectStore(), FakeDump(source), BackupMode.SHARED_ONLY)
    restore = FakeRestore(schema_from=True)

    with pytest.raises(RestoreCountMismatchError):
        _restore(target, _payload(shared), restore)

    [request] = restore.requests
    assert "RR001" in request.before_commit_sql
    for table in MEMBER_TABLES:
        assert f"'{table}'" in request.before_commit_sql, table
    assert "uploaded_pdf" in request.before_commit_sql


def test_count_mismatch_names_each_item_and_keeps_the_restored_data(
    manifest: BackupManifest, target: TestDatabase
) -> None:
    restore = FakeRestore(schema_from=True)

    with pytest.raises(RestoreCountMismatchError) as caught:
        _restore(target, _payload(manifest), restore)

    assert "算數的論文：清單 1，還原後 0，差 -1" in caught.value.differences
    assert "成員資料表 member：清單 2，還原後 0，差 -2" in caught.value.differences
    # 還原的結構留著，沒有刪。
    assert target.has_table("paper")


def test_encrypted_input_is_rejected_without_touching_the_target(target: TestDatabase) -> None:
    restore = FakeRestore()

    with pytest.raises(BackupFormatError, match="age -d"):
        run_restore(
            Database(target.owner_url, connect_timeout=5),
            io.BytesIO(b"age-encryption.org/v1\n-> X25519 abc\n"),
            pg_restore_version=lambda: "18.7",
            restore_archive=restore,
        )

    assert restore.requests == []
    assert PAYLOAD_MAGIC.startswith(b"RADAR-BACKUP")


# --- 審查第 1 輪：成員上傳的 PDF 摘要（S3）-------------------------------------------------------


def _uploaded_abstract(db: TestDatabase, paper_id: object) -> None:
    """一筆成員上傳的 PDF 摘要（只有上傳者看得到、永遠不是目前的摘要；內容是編的）。"""
    execute(
        db,
        "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
        " VALUES (%s, 'uploaded_pdf', 'A made-up abstract from a member PDF.', false)",
        paper_id,
    )


def test_shared_only_backup_refuses_uploaded_pdf_abstracts(
    source: TestDatabase, seeded: Seeded
) -> None:
    _uploaded_abstract(source, seeded.papers[0])
    store = FakeObjectStore()
    dump = FakeDump(source)

    with pytest.raises(SharedOnlyBackupRefusedError, match="1 筆成員上傳的 PDF 摘要"):
        run_backup(
            Database(source.app_url, connect_timeout=5),
            store,
            mode=BackupMode.SHARED_ONLY,
            recipient=RECIPIENT,
            pg_dump_version=lambda: "18.7",
            start_dump=dump,
            now=lambda: NOW,
        )

    assert dump.requests == []
    assert store.objects == {}
    # 預設模式本來就帶成員資料，照樣備份。
    assert _backup(source, FakeObjectStore(), FakeDump(source), BackupMode.FULL).counts


# --- 審查第 1 輪：--expect-mode、遷移版本（S8）--------------------------------------------------


def test_expect_mode_mismatch_is_refused_before_touching_the_target(
    manifest: BackupManifest, target: TestDatabase
) -> None:
    restore = FakeRestore()

    with pytest.raises(RestoreRefusedError, match="--expect-mode") as caught:
        run_restore(
            Database(target.owner_url, connect_timeout=5),
            _payload(manifest),
            pg_restore_version=lambda: "18.7",
            restore_archive=restore,
            expect_mode=BackupMode.SHARED_ONLY,
        )

    assert caught.value.problems == [
        "這份備份是「完整（含成員資料）」，和 --expect-mode 指定的「只有共用資料」不同"
    ]
    assert restore.requests == []


def test_backup_newer_than_the_image_is_refused(
    manifest: BackupManifest, target: TestDatabase
) -> None:
    newer = manifest.model_copy(update={"alembic_revisions": ("0099_future",)})
    restore = FakeRestore()

    with pytest.raises(RestoreRefusedError, match="0099_future 這個映像不認得"):
        run_restore(
            Database(target.owner_url, connect_timeout=5),
            _payload(newer),
            pg_restore_version=lambda: "18.7",
            restore_archive=restore,
            known_revisions=frozenset(manifest.alembic_revisions),
        )

    assert restore.requests == []


# --- 審查第 1 輪：commit 之後重數失敗（S6）------------------------------------------------------


class _RestoreThenBreakCounting(FakeRestore):
    """還原了結構（遷移好），再把 `embedding` 改名，讓之後的重數一定失敗。"""

    def __call__(self, request: RestoreRequest, stream: Reader[bytes]) -> None:
        super().__call__(request, stream)
        assert self.target is not None
        execute(self.target, "ALTER TABLE embedding RENAME TO embedding_renamed")


def test_recount_failure_after_commit_says_the_data_is_kept(
    manifest: BackupManifest, target: TestDatabase
) -> None:
    restore = _RestoreThenBreakCounting(schema_from=True)

    with pytest.raises(RestoreVerificationError, match="資料已還原") as caught:
        _restore(target, _payload(manifest), restore)

    assert "UndefinedTable" in str(caught.value) or "ProgrammingError" in str(caught.value)
    assert "留著" in str(caught.value)
    assert target.has_table("paper")


def test_snapshot_transaction_can_set_local_timeouts(source: TestDatabase) -> None:
    database = Database(source.app_url, connect_timeout=5)

    with database.read_only_snapshot_transaction(
        statement_timeout_ms=600_000, idle_in_transaction_timeout_ms=360_000
    ) as connection:
        assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "10min"
        assert (
            connection.execute(text("SHOW idle_in_transaction_session_timeout")).scalar_one()
            == "6min"
        )
    with database.read_only_snapshot_transaction() as connection:
        # SET LOCAL：交易結束就回到連線原本的設定。
        assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "5s"
        assert (
            connection.execute(text("SHOW idle_in_transaction_session_timeout")).scalar_one() == "0"
        )
    with (
        pytest.raises(ValueError, match="正整數"),
        database.read_only_snapshot_transaction(statement_timeout_ms=0),
    ):
        pass


# --- 審查第 1 輪：還原交易裡的檢查（C2、C3、S3），直接在目標上執行 repo 組的 SQL -----------------
#
# 真的還原由 `psql` 依序送「BEGIN、還原前的檢查、備份的 SQL、commit 前的檢查、COMMIT」
# （`tests/integration/test_backup_tools_fake.py` 驗這個順序）。這裡用一條連線照同樣的順序執行，
# 在「還原前的檢查之後、commit 之前」由另一條連線動手，時間點由測試控制，不靠 sleep。


def _guarded_restore(
    target: TestDatabase,
    request: RestoreRequest,
    restored_sql: str,
    *,
    meanwhile: Callable[[], None] | None = None,
) -> None:
    """照還原的順序在一個交易裡執行；任何一步失敗就退回、原樣丟出 psycopg 的錯誤。"""
    with psycopg.connect(target.owner_url, autocommit=True) as connection:
        connection.execute("BEGIN")
        try:
            connection.execute(request.before_restore_sql)
            connection.execute(restored_sql)
            if meanwhile is not None:
                meanwhile()
            connection.execute(request.before_commit_sql)
        except psycopg.Error:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")


def _request(mode: BackupMode) -> RestoreRequest:
    return get_restore_request(SystemViewer(), mode=mode, member_tables=MEMBER_TABLES)


def _create_intruder(target: TestDatabase) -> None:
    """另一條連線建一張名稱不撞的表並 commit。"""
    with psycopg.connect(target.admin_url, autocommit=True) as other:
        other.execute("CREATE TABLE public.intruder (id int)")


# 還原的 SQL 會做的幾種事（建表、索引、主鍵、權限、序列、view、資料）；pg_class 的列都在這個交易
# 裡建或改，COMMIT 前的檢查（xmin 是目前的交易）要放行。
RESTORED = (
    "CREATE TABLE public.restored (id int); INSERT INTO public.restored VALUES (1);"
    " CREATE INDEX restored_id ON public.restored (id);"
    " ALTER TABLE public.restored ADD PRIMARY KEY (id);"
    " GRANT SELECT ON public.restored TO radar_app;"
    " CREATE SEQUENCE public.restored_seq OWNED BY public.restored.id;"
    " SELECT pg_catalog.setval('public.restored_seq', 5, true);"
    " CREATE VIEW public.restored_view AS SELECT 1 AS id;"
    " CREATE MATERIALIZED VIEW public.restored_matview AS SELECT id FROM public.restored;"
    " CREATE TABLE public.member (id int);"
    " CREATE TABLE public.paper_abstract (source text);"
    " INSERT INTO public.paper_abstract VALUES ('openalex');"
)


@pytest.mark.parametrize("mode", list(BackupMode))
def test_guards_let_a_clean_restore_commit(target: TestDatabase, mode: BackupMode) -> None:
    _guarded_restore(target, _request(mode), RESTORED)

    assert target.has_table("public.restored")


def test_table_created_by_another_connection_during_restore_is_rolled_back(
    target: TestDatabase,
) -> None:
    """審查 C2：空檢查之後、還原 commit 之前，別的連線建了一張不撞名的表並 commit。"""
    with pytest.raises(psycopg.Error) as caught:
        _guarded_restore(
            target,
            _request(BackupMode.FULL),
            RESTORED,
            meanwhile=lambda: _create_intruder(target),
        )

    assert caught.value.sqlstate == "RR003"
    assert target.has_table("public.intruder")
    assert not target.has_table("public.restored")


def test_table_created_before_the_restore_transaction_is_refused(target: TestDatabase) -> None:
    """審查 C2：還原前的唯讀檢查之後、BEGIN 之前就有人建表：BEGIN 之後的檢查擋下。"""
    _create_intruder(target)

    with pytest.raises(psycopg.Error) as caught:
        _guarded_restore(target, _request(BackupMode.FULL), RESTORED)

    assert caught.value.sqlstate == "RR002"
    assert not target.has_table("public.restored")


@pytest.mark.parametrize("mode", list(BackupMode))
def test_large_objects_in_the_restore_are_rolled_back(
    target: TestDatabase, mode: BackupMode
) -> None:
    """審查 C1、C3：備份帶了大型物件（內容是編的）就整個退回。"""
    with pytest.raises(psycopg.Error) as caught:
        _guarded_restore(
            target,
            _request(mode),
            RESTORED + " SELECT lo_from_bytea(0, convert_to('backup-a@example.com note', 'UTF8'));",
        )

    assert caught.value.sqlstate == "RR004"
    [(objects,)] = execute(target, "SELECT count(*) FROM pg_largeobject_metadata")
    assert objects == 0


@pytest.mark.parametrize(
    "member_content",
    [
        "INSERT INTO public.member VALUES (1);",
        "INSERT INTO public.paper_abstract VALUES ('uploaded_pdf');",
    ],
)
def test_member_content_in_a_shared_only_restore_is_rolled_back(
    target: TestDatabase, member_content: str
) -> None:
    """審查 S3：只有共用資料的備份帶了成員資料表的列、或成員上傳的 PDF 摘要。"""
    with pytest.raises(psycopg.Error) as caught:
        _guarded_restore(target, _request(BackupMode.SHARED_ONLY), RESTORED + member_content)

    assert caught.value.sqlstate == "RR001"
    assert not target.has_table("public.restored")
    # 預設模式本來就帶成員資料，同樣的內容照樣 commit。
    _guarded_restore(target, _request(BackupMode.FULL), RESTORED + member_content)
