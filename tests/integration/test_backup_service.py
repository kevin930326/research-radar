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
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from io import Reader

import psycopg
import pytest

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
    encode_payload_header,
)
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


def test_one_stray_empty_table_makes_the_target_non_empty(
    manifest: BackupManifest, target: TestDatabase
) -> None:
    # 「空的」是沒有任何使用者的資料表、view、序列（ADR-0051），不是「每張表都是 0 列」。
    with psycopg.connect(target.admin_url, autocommit=True) as connection:
        connection.execute("CREATE TABLE stray (id int)")
    restore = FakeRestore()

    with pytest.raises(RestoreRefusedError, match=r"public\.stray"):
        _restore(target, _payload(manifest), restore)

    assert restore.requests == []


def test_extensions_alone_count_as_empty(manifest: BackupManifest, target: TestDatabase) -> None:
    restore = FakeRestore(schema_from=True)

    with pytest.raises(RestoreCountMismatchError):
        _restore(target, _payload(manifest), restore)

    assert restore.requests == [RestoreRequest(())]
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

    assert restore.requests == [RestoreRequest(tuple(sorted(MEMBER_TABLES)))]


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
