"""`radar backup`／`radar restore`（Story 3.18、FR-48、AD-28、ADR-0051）。

備份：
1. 先問映像裡 `pg_dump` 的大版本（不在交易裡），
2. 開一個唯讀、REPEATABLE READ 的交易：讀版本、檢查 `pg_dump` 不比資料庫舊、匯出快照、數筆數、
   組清單，開始 `pg_dump --snapshot`（等它開始輸出、已經匯入快照）——清單的筆數和備份的內容是
   同一個時間點，
3. 結束交易後才把加密串流上傳到物件儲存的 `backup/`（不在開著的交易裡呼叫外部服務）。
子程序失敗時上傳在完成之前就失敗，`backup/` 不留半個檔。這個 Story 不送 `backup` 心跳（7.5）。

還原（目標是另一個空的資料庫，擁有者角色）：
1. 讀備份檔開頭的清單，
2. 目標不是空的、版本太舊就不執行（什麼都不寫），
3. 整個還原在一個交易裡；只有共用資料的備份在 commit 前再檢查一次成員資料表沒有任何一列，
4. commit 後重新數筆數和清單比對；不一致就失敗，還原的資料留著，不自動刪除。
"""

from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from datetime import datetime
from io import Reader
from typing import IO

import structlog

from radar.domain.backup import (
    BACKUP_CONTENT_TYPE,
    MEMBER_TABLES,
    BackupCounts,
    BackupError,
    BackupManifest,
    BackupMode,
    DumpRequest,
    PgDumpTooOldError,
    RestoreCountMismatchError,
    RestoreRefusedError,
    RestoreRequest,
    backup_object_key,
    count_differences,
    encode_payload_header,
    read_payload_header,
    restore_version_problems,
    version_text,
    version_tuple,
)
from radar.domain.viewer import SystemViewer
from radar.ports import ObjectStore
from radar.repo.backup import (
    count_backup_rows,
    find_user_relations,
    get_database_versions,
    get_exported_snapshot,
)
from radar.repo.db import Database

log = structlog.get_logger(__name__)

# 「目標不是空的」時列出幾個已經存在的物件當例子。
_EXAMPLE_RELATIONS = 5


@dataclass(frozen=True)
class BackupResult:
    """備份寫到哪個鍵，以及它的清單。"""

    object_key: str
    manifest: BackupManifest


@dataclass(frozen=True)
class RestoreResult:
    """還原了哪一份備份（清單），以及還原後數到的筆數（和清單一致）。"""

    manifest: BackupManifest
    counts: BackupCounts
    member_table_rows: dict[str, int]


def run_backup(
    database: Database,
    store: ObjectStore,
    *,
    mode: BackupMode,
    recipient: str,
    pg_dump_version: Callable[[], str],
    start_dump: Callable[[DumpRequest], AbstractContextManager[Reader[bytes]]],
    now: Callable[[], datetime],
) -> BackupResult:
    """備份整個資料庫（`SHARED_ONLY` 時成員資料表只有結構）到物件儲存的 `backup/`。

    `pg_dump_version` 回映像裡 `pg_dump` 的版本（例如 `18.7`）；`start_dump` 開始一次加密傾印，
    等到 `pg_dump` 開始輸出（已經匯入快照）才回來，回傳的串流讀到結尾時確認子程序都成功。

    Raises:
        PgDumpTooOldError: `pg_dump` 的大版本比資料庫舊（沒有備份）。
        BackupToolError: `pg_dump` 或 `age` 找不到或失敗（`backup/` 不留這個鍵）。
        BackupError: 資料庫沒有 pgvector。
    """
    viewer = SystemViewer()
    dump_version = version_text(pg_dump_version())
    dump_major = version_tuple(dump_version)[0]
    with ExitStack() as stack:
        with database.read_only_snapshot_transaction() as connection:
            versions = get_database_versions(viewer, connection)
            if dump_major < versions.postgres_major:
                raise PgDumpTooOldError(dump_major, versions.postgres_major)
            if versions.pgvector_version is None:
                raise BackupError("資料庫沒有 pgvector，不是 radar 的資料庫；沒有備份")
            snapshot_id = get_exported_snapshot(viewer, connection)
            counts, member_rows = count_backup_rows(viewer, connection, MEMBER_TABLES)
            if mode is BackupMode.SHARED_ONLY:
                member_rows = dict.fromkeys(member_rows, 0)
            manifest = BackupManifest(
                mode=mode,
                created_at=now(),
                postgres_version=versions.postgres_version,
                postgres_version_num=versions.postgres_version_num,
                pgvector_version=versions.pgvector_version,
                pg_dump_version=dump_version,
                alembic_revisions=versions.alembic_revisions,
                counts=counts,
                member_table_rows=member_rows,
            )
            excluded = tuple(sorted(MEMBER_TABLES)) if mode is BackupMode.SHARED_ONLY else ()
            stream = stack.enter_context(
                start_dump(
                    DumpRequest(
                        snapshot_id=snapshot_id,
                        header=encode_payload_header(manifest),
                        exclude_table_data=excluded,
                        recipient=recipient,
                    )
                )
            )
        # 交易已結束（pg_dump 自己持有匯入的快照），才開始上傳。
        key = backup_object_key(manifest.created_at, mode)
        store.put_stream(key, stream, content_type=BACKUP_CONTENT_TYPE)
    log.info(
        "備份完成",
        object_key=key,
        mode=mode.value,
        counted_papers=manifest.counts.counted_paper,
        member_tables=len(manifest.member_table_rows),
    )
    return BackupResult(object_key=key, manifest=manifest)


def run_restore(
    database: Database,
    source: IO[bytes],
    *,
    pg_restore_version: Callable[[], str],
    restore_archive: Callable[[RestoreRequest, Reader[bytes]], None],
) -> RestoreResult:
    """把解密後的備份（`source`：檔案或標準輸入）還原到 `database`（擁有者角色、要是空的）。

    Raises:
        BackupFormatError: `source` 不是解密後的 radar 備份檔（沒有寫任何東西）。
        RestoreRefusedError: 目標不是空的、或版本太舊（沒有寫任何東西）。
        SharedOnlyBackupHasMemberRowsError: 只有共用資料的備份裡成員資料表有資料（已整個退回）。
        BackupToolError: `pg_restore` 或 `psql` 失敗（已整個退回）。
        RestoreCountMismatchError: 還原完成但筆數和清單不一致（還原的資料留著，不刪除）。
    """
    viewer = SystemViewer()
    manifest = read_payload_header(source)
    with database.read_only_snapshot_transaction() as connection:
        relations = find_user_relations(viewer, connection)
        versions = get_database_versions(viewer, connection)
    if relations:
        examples = "、".join(relations[:_EXAMPLE_RELATIONS])
        raise RestoreRefusedError(
            [
                f"目標資料庫不是空的：已經有 {len(relations)} 個資料表、view 或序列"
                f"（例如 {examples}）；只能還原到空的資料庫"
            ]
        )
    restore_major = version_tuple(pg_restore_version())[0]
    problems = restore_version_problems(manifest, versions, restore_major)
    if problems:
        raise RestoreRefusedError(problems)
    guarded: tuple[str, ...] = ()
    if manifest.mode is BackupMode.SHARED_ONLY:
        guarded = tuple(sorted(set(MEMBER_TABLES) | set(manifest.member_table_rows)))
    restore_archive(RestoreRequest(member_tables_must_be_empty=guarded), source)
    with database.read_only_snapshot_transaction() as connection:
        counts, member_rows = count_backup_rows(
            viewer, connection, manifest.member_table_rows.keys()
        )
    differences = count_differences(manifest, counts, member_rows)
    if differences:
        log.error("還原後的筆數和清單不一致", differences=len(differences))
        raise RestoreCountMismatchError(differences)
    log.info("還原完成", mode=manifest.mode.value, counted_papers=counts.counted_paper)
    return RestoreResult(manifest=manifest, counts=counts, member_table_rows=member_rows)
