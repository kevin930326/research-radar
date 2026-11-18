"""`radar backup`／`radar restore`（Story 3.18、FR-48、AD-28、ADR-0051）。

備份：
1. 先問映像裡 `pg_dump` 的大版本（不在交易裡），
2. 開一個唯讀、REPEATABLE READ 的交易（閒置逾時當最後保險）：讀版本、檢查 `pg_dump` 不比資料庫
   舊、`--shared-only` 時檢查沒有成員上傳的 PDF 摘要、匯出快照、數筆數、組清單，開始
   `pg_dump --snapshot`（等它開始輸出、已經匯入快照）——清單的筆數和備份的內容是同一個時間點，
3. 結束交易後才把加密串流上傳到物件儲存的 `backup/`（不在開著的交易裡呼叫外部服務）。
子程序失敗時上傳在完成之前就失敗，`backup/` 不留半個檔。這個 Story 不送 `backup` 心跳（7.5）。

還原（目標是另一個空的資料庫，擁有者角色）：
1. 讀備份檔開頭的清單；`--expect-mode` 有給而模式不同、遷移版本這個映像不認得就不執行，
2. 目標不是空的、版本太舊就不執行（什麼都不寫），
3. 整個還原在一個交易裡：BEGIN 之後再檢查一次目標是空的；COMMIT 前檢查所有 relation 都是這個
   交易建的、沒有大型物件，只有共用資料的備份另外檢查成員資料表沒有任何一列、沒有成員上傳的
   PDF 摘要，最後確認沒有別的用戶端連著目標（自己池子裡閒著的連線先關掉），
4. commit 後重新數筆數和清單比對；不一致就失敗，還原的資料留著，不自動刪除。重數時出錯也一樣
   （資料已還原、留著）。
"""

import threading
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
    RECOUNT_STATEMENT_TIMEOUT_MS,
    RESTORE_INPUT_IDLE_TIMEOUT_SECONDS,
    SNAPSHOT_IDLE_TIMEOUT_MS,
    BackupCounts,
    BackupError,
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
    backup_object_key,
    count_differences,
    encode_payload_header,
    expect_mode_problems,
    read_payload_header,
    restore_version_problems,
    unknown_revision_problems,
    version_text,
    version_tuple,
)
from radar.domain.papers import AbstractSource
from radar.domain.viewer import SystemViewer
from radar.ports import ObjectStore
from radar.repo.backup import (
    count_backup_rows,
    find_user_relations,
    get_database_versions,
    get_exported_snapshot,
    get_restore_request,
)
from radar.repo.db import Database
from radar.repo.papers import count_abstracts_from_source

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
        SharedOnlyBackupRefusedError: `SHARED_ONLY` 而有成員上傳的 PDF 摘要（沒有備份）。
        BackupToolError: `pg_dump` 或 `age` 找不到、失敗或逾時（`backup/` 不留這個鍵）。
        BackupError: 資料庫沒有 pgvector。
    """
    viewer = SystemViewer()
    dump_version = version_text(pg_dump_version())
    dump_major = version_tuple(dump_version)[0]
    with ExitStack() as stack:
        with database.read_only_snapshot_transaction(
            idle_in_transaction_timeout_ms=SNAPSHOT_IDLE_TIMEOUT_MS
        ) as connection:
            versions = get_database_versions(viewer, connection)
            if dump_major < versions.postgres_major:
                raise PgDumpTooOldError(dump_major, versions.postgres_major)
            if versions.pgvector_version is None:
                raise BackupError("資料庫沒有 pgvector，不是 radar 的資料庫；沒有備份")
            if mode is BackupMode.SHARED_ONLY:
                uploaded = count_abstracts_from_source(
                    viewer, connection, AbstractSource.UPLOADED_PDF
                )
                if uploaded:
                    raise SharedOnlyBackupRefusedError(uploaded)
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


def _read_header_within_limit(source: IO[bytes]) -> BackupManifest:
    """讀備份檔開頭的清單；超過 `RESTORE_INPUT_IDLE_TIMEOUT_SECONDS` 還沒讀完就放棄。

    審查第 2 輪 O3：清單只有幾百位元組，整段讀完的時間就等於「多久沒收到資料」。這時還沒有連
    目標資料庫。逾時時讀檔頭的執行緒（daemon）可能還卡在讀輸入，`radar restore` 結束時跟著結束。

    Raises:
        BackupFormatError: `source` 不是解密後的 radar 備份檔。
        BackupToolError: 逾時沒讀完。
    """
    outcome: list[BackupManifest | BaseException] = []

    def read() -> None:
        try:
            outcome.append(read_payload_header(source))
        except BaseException as exc:
            outcome.append(exc)

    reader = threading.Thread(target=read, name="restore-header", daemon=True)
    reader.start()
    reader.join(RESTORE_INPUT_IDLE_TIMEOUT_SECONDS)
    if not outcome:
        raise BackupToolError(
            "radar restore",
            f"超過 {RESTORE_INPUT_IDLE_TIMEOUT_SECONDS} 秒沒有讀完備份檔開頭的清單，已停止；"
            "目標資料庫沒有動",
        )
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


def run_restore(
    database: Database,
    source: IO[bytes],
    *,
    pg_restore_version: Callable[[], str],
    restore_archive: Callable[[RestoreRequest, Reader[bytes]], None],
    expect_mode: BackupMode | None = None,
    known_revisions: frozenset[str] | None = None,
) -> RestoreResult:
    """把解密後的備份（`source`：檔案或標準輸入）還原到 `database`（擁有者角色、要是空的）。

    `expect_mode` 有給時，備份的模式要相同（`radar restore --expect-mode`）；沒給時不檢查。
    `known_revisions` 是這個映像的遷移檔認得的版本：備份的 Alembic 版本有不認得的就不還原
    （`radar restore` 一定會給；沒給時不檢查）。

    Raises:
        BackupFormatError: `source` 不是解密後的 radar 備份檔（沒有寫任何東西）。
        RestoreRefusedError: 模式不符、遷移版本不認得、目標不是空的、或版本太舊（沒有寫任何東西）；
            還原的交易裡發現目標不是空的、有別的連線同時建了表、或 COMMIT 前還有別的用戶端連著
            目標（已整個退回）。
        SharedOnlyBackupHasMemberRowsError: 只有共用資料的備份裡有成員資料（已整個退回）。
        RestoreArchiveUnsafeError: 備份裡有會自己 commit 的內容（已整個退回）。
        BackupToolError: `pg_restore` 或 `psql` 失敗、輸入逾時（已整個退回）。
        RestoreCountMismatchError: 還原完成但筆數和清單不一致（還原的資料留著，不刪除）。
        RestoreVerificationError: 還原完成，但重數筆數時出錯（還原的資料留著，不刪除）。
    """
    viewer = SystemViewer()
    manifest = _read_header_within_limit(source)
    early = expect_mode_problems(manifest, expect_mode)
    if known_revisions is not None:
        early += unknown_revision_problems(manifest, known_revisions)
    if early:
        raise RestoreRefusedError(early)
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
    request = get_restore_request(
        viewer,
        mode=manifest.mode,
        member_tables=set(MEMBER_TABLES) | set(manifest.member_table_rows),
    )
    # 還原的 COMMIT 前會確認沒有別的用戶端連著目標（RR005），自己池子裡閒著的連線也算，先關掉。
    database.close_idle_connections()
    restore_archive(request, source)
    # 已經 COMMIT：之後任何錯誤都說明資料已還原、留著（審查 S6）。
    try:
        with database.read_only_snapshot_transaction(
            statement_timeout_ms=RECOUNT_STATEMENT_TIMEOUT_MS
        ) as connection:
            counts, member_rows = count_backup_rows(
                viewer, connection, manifest.member_table_rows.keys()
            )
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("還原後重數筆數失敗，資料已還原", error_type=type(exc).__name__)
        raise RestoreVerificationError(type(exc).__name__) from None
    differences = count_differences(manifest, counts, member_rows)
    if differences:
        log.error("還原後的筆數和清單不一致", differences=len(differences))
        raise RestoreCountMismatchError(differences)
    log.info("還原完成", mode=manifest.mode.value, counted_papers=counts.counted_paper)
    return RestoreResult(manifest=manifest, counts=counts, member_table_rows=member_rows)
