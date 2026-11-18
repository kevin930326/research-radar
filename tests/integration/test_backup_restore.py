"""備份與還原的往返：真的 `pg_dump`、`age`、`pg_restore`、`psql`（Story 3.18、ADR-0051）。

本機沒有這些工具時跳過；CI 設 `RADAR_REQUIRE_BACKUP_TOOLS=1`，找不到就失敗。
age 的金鑰在測試執行時產生在 pytest 的臨時資料夾；物件儲存用記憶體裡的替身，不碰 R2。
來源、目標都是 radar-ci（55432）上臨時建的資料庫，測試結束就刪掉。

- 預設模式往返：成員資料完整還原，筆數比對通過；
- 只有共用資料往返：成員資料表有結構、0 列，共用資料完整；
- 目標不是空的、版本太舊：不執行，目標不變；
- 預設模式的備份被改成「只有共用資料」：commit 前的檢查退回整個交易，目標還是空的；
- 清單的筆數不對：印出差多少，還原的資料留著；
- 傾印失敗：物件儲存沒有這個鍵。
"""

import io
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from radar.adapters.fake.object_store import FakeObjectStore
from radar.config import load_migration_settings, load_settings
from radar.domain.backup import (
    MEMBER_TABLES,
    BackupManifest,
    BackupMode,
    BackupToolError,
    RestoreCountMismatchError,
    RestoreRefusedError,
    SharedOnlyBackupHasMemberRowsError,
    encode_payload_header,
    read_payload_header,
)
from radar.entrypoints.bootstrap import (
    build_backup_tools,
    build_restore_database,
    build_restore_tools,
)
from radar.repo.db import Database
from radar.services.backup import BackupResult, RestoreResult, run_backup, run_restore
from tests.backup_support import (
    AgeKey,
    Seeded,
    make_age_key,
    require_backup_tools,
    seed_source,
    table_rows,
)
from tests.database import TestDatabase, create_database, drop_database, migrate
from tests.llm_support import execute

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _tools() -> None:
    require_backup_tools()


@pytest.fixture
def age_key(tmp_path: Path) -> AgeKey:
    return make_age_key(tmp_path)


@pytest.fixture
def source(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def seeded(source: TestDatabase) -> Seeded:
    return seed_source(source)


@pytest.fixture
def target() -> Iterator[TestDatabase]:
    """空的目標：管理者建好兩個擴充套件、還沒遷移（和試用主機第一次還原前一樣）。"""
    database = create_database(with_extensions=True)
    try:
        yield database
    finally:
        drop_database(database)


def _backup(
    db: TestDatabase, store: FakeObjectStore, recipient: str, mode: BackupMode
) -> BackupResult:
    settings = load_settings({"DATABASE_URL": db.app_url, "RADAR_BACKUP_AGE_RECIPIENT": recipient})
    pg_dump_version, start_dump = build_backup_tools(settings)
    return run_backup(
        Database(db.app_url, connect_timeout=5, pool_size=2),
        store,
        mode=mode,
        recipient=recipient,
        pg_dump_version=pg_dump_version,
        start_dump=start_dump,
        now=lambda: NOW,
    )


def _restore(target: TestDatabase, payload: bytes) -> RestoreResult:
    settings = load_migration_settings({"RADAR_MIGRATION_DATABASE_URL": target.owner_url})
    pg_restore_version, restore_archive = build_restore_tools(settings)
    return run_restore(
        build_restore_database(settings),
        io.BytesIO(payload),
        pg_restore_version=pg_restore_version,
        restore_archive=restore_archive,
    )


def _decrypted(store: FakeObjectStore, result: BackupResult, key: AgeKey) -> bytes:
    encrypted = store.objects[result.object_key]
    assert encrypted.startswith(b"age-encryption.org/v1\n")
    return key.decrypt(encrypted)


def _with_manifest(payload: bytes, **changes: object) -> bytes:
    """把解密後的備份換一份清單（`changes` 改的欄位），傾印本身不動。"""
    stream = io.BytesIO(payload)
    manifest = read_payload_header(stream)
    changed = BackupManifest.model_validate(manifest.model_dump() | changes)
    return encode_payload_header(changed) + stream.read()


def _user_tables(db: TestDatabase) -> list[str]:
    rows = execute(
        db,
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S', 'v', 'm')"
        " AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')",
    )
    return sorted(str(row[0]) for row in rows)


@pytest.fixture
def full_payload(source: TestDatabase, seeded: Seeded, age_key: AgeKey) -> bytes:
    store = FakeObjectStore()
    result = _backup(source, store, age_key.recipient, BackupMode.FULL)
    return _decrypted(store, result, age_key)


# --- 往返 -------------------------------------------------------------------------------------


def test_full_round_trip_restores_member_data(
    source: TestDatabase, seeded: Seeded, age_key: AgeKey, target: TestDatabase
) -> None:
    store = FakeObjectStore()
    backup = _backup(source, store, age_key.recipient, BackupMode.FULL)
    assert backup.object_key == "backup/20261118T120000Z-full.radar-backup.age"
    # 沒加密的傾印不出現在物件儲存。
    assert b"backup-a@example.com" not in store.objects[backup.object_key]

    restored = _restore(target, _decrypted(store, backup, age_key))

    assert restored.manifest == backup.manifest
    assert restored.counts == backup.manifest.counts
    assert restored.member_table_rows == backup.manifest.member_table_rows
    assert target.revisions() == source.revisions()
    for table in ("paper", "zh_summary", "classification", "embedding", "audit_event"):
        assert table_rows(target, table) == table_rows(source, table)
    emails = execute(target, "SELECT email FROM allowed_email ORDER BY email")
    assert [row[0] for row in emails] == ["backup-a@example.com", "backup-b@example.com"]
    [(owner,)] = execute(target, "SELECT owner_id FROM library_item")
    assert owner == seeded.member_a
    vectors = "SELECT vector::text FROM embedding ORDER BY item_id"
    assert execute(target, vectors) == execute(source, vectors)
    # 還原後應用程式角色照樣讀得到（權限跟著還原）。
    with target.connect("radar_app") as app:
        assert app.execute("SELECT count(*) FROM paper").fetchone() == (2,)


def test_shared_only_round_trip_keeps_member_tables_empty(
    source: TestDatabase, seeded: Seeded, age_key: AgeKey, target: TestDatabase
) -> None:
    store = FakeObjectStore()
    backup = _backup(source, store, age_key.recipient, BackupMode.SHARED_ONLY)
    assert backup.object_key.endswith("-shared-only.radar-backup.age")

    restored = _restore(target, _decrypted(store, backup, age_key))

    assert restored.manifest.mode is BackupMode.SHARED_ONLY
    assert restored.counts == backup.manifest.counts
    assert restored.counts.counted_paper == 1
    assert table_rows(target, "paper") == 2
    for table in MEMBER_TABLES:
        if source.has_table(table):
            assert target.has_table(table), table
            assert table_rows(target, table) == 0, table
    assert table_rows(target, "embedding") == 1
    assert table_rows(target, "zh_summary") == table_rows(source, "zh_summary")


# --- 還原前就拒絕 -------------------------------------------------------------------------------


def test_non_empty_target_is_left_alone(full_payload: bytes, target: TestDatabase) -> None:
    migrate(target)
    before = _user_tables(target)

    with pytest.raises(RestoreRefusedError, match="不是空的"):
        _restore(target, full_payload)

    assert _user_tables(target) == before
    assert table_rows(target, "paper") == 0


def test_older_target_versions_are_refused(full_payload: bytes, target: TestDatabase) -> None:
    newer = _with_manifest(
        full_payload, postgres_version="99.0", postgres_version_num=990000, pgvector_version="9.9"
    )

    with pytest.raises(RestoreRefusedError) as caught:
        _restore(target, newer)

    assert len(caught.value.problems) == 2
    assert _user_tables(target) == []


# --- 還原時退回、還原後比對 ---------------------------------------------------------------------


def test_full_dump_labelled_shared_only_is_rolled_back(
    full_payload: bytes, target: TestDatabase
) -> None:
    relabelled = _with_manifest(
        full_payload,
        mode=BackupMode.SHARED_ONLY,
        member_table_rows=dict.fromkeys(MEMBER_TABLES, 0),
    )

    with pytest.raises(SharedOnlyBackupHasMemberRowsError):
        _restore(target, relabelled)

    # 整個還原在一個交易裡：退回後連資料表都沒有。
    assert _user_tables(target) == []


def test_count_mismatch_reports_and_keeps_the_data(
    full_payload: bytes, target: TestDatabase
) -> None:
    manifest = read_payload_header(io.BytesIO(full_payload))
    inflated = _with_manifest(
        full_payload, counts=manifest.counts.model_copy(update={"counted_paper": 3}).model_dump()
    )

    with pytest.raises(RestoreCountMismatchError) as caught:
        _restore(target, inflated)

    assert caught.value.differences == ["算數的論文：清單 3，還原後 1，差 -2"]
    assert table_rows(target, "paper") == 2


def test_truncated_dump_is_rolled_back(full_payload: bytes, target: TestDatabase) -> None:
    with pytest.raises(BackupToolError, match="交易已退回"):
        _restore(target, full_payload[: len(full_payload) // 2])

    assert _user_tables(target) == []


# --- 備份失敗 ---------------------------------------------------------------------------------


def test_failed_dump_leaves_no_object(
    source: TestDatabase, seeded: Seeded, age_key: AgeKey
) -> None:
    """`pg_dump` 連到不存在的資料庫而失敗：上傳在完成前中斷，不留鍵，錯誤不印連線字串。"""
    store = FakeObjectStore()
    wrong = TestDatabase(f"{source.name}_missing")
    settings = load_settings(
        {"DATABASE_URL": wrong.app_url, "RADAR_BACKUP_AGE_RECIPIENT": age_key.recipient}
    )
    pg_dump_version, start_dump = build_backup_tools(settings)

    with pytest.raises(BackupToolError, match="pg_dump") as caught:
        run_backup(
            Database(source.app_url, connect_timeout=5),
            store,
            mode=BackupMode.FULL,
            recipient=age_key.recipient,
            pg_dump_version=pg_dump_version,
            start_dump=start_dump,
            now=lambda: NOW,
        )

    assert store.objects == {}
    assert wrong.name not in str(caught.value)
