"""個人文獻庫：列出、加入、移出、復原（Story 3.16、FR-7、FR-35、FR-50、NFR-16、AD-4、AD-26）。

- 每個寫入在一個交易裡做完：`permissions.require` → 改資料 → 同一個交易寫一筆稽核（成員或管理者
  身分，`domain/audit.member_audit_event`）。沒有真的改到資料（已經在文獻庫）不寫稽核。
- 看不到、不存在、被排除的論文、別人的那一筆：一律回 None（api 回同一個 404，ADR-0002）；不先查
  存在，「看得到」只在 repo 的一句帶可見條件的查詢裡判斷。
- 加入：一句 `INSERT … SELECT … ON CONFLICT DO NOTHING`；沒寫入時查自己的文獻庫，有就回原本那一筆
  （`added_via`、`added_at` 不變），沒有就是看不到。
- 移出：讀自己的那一筆 → 讀論文列（刪掉之後，私人論文就看不到了）→ 整列複製到 `deleted_record`
  → 刪 → 稽核；回移出前的那一筆。
- 復原：讀自己最新的一筆封存 → 用原本的 `id`／`added_via`／`added_at` 寫回（已經又在文獻庫就不
  寫，回現在那一筆）→ 刪掉那筆封存（不管有沒有寫回都消耗）→ 稽核。
- 同時兩個請求：兩張表的應用程式角色都沒有 UPDATE，所以不能 `SELECT … FOR UPDATE` 鎖列（PostgreSQL
  鎖列要 UPDATE 權限）。列不會被改，只會被刪：移出時複製不到或刪不到、復原時封存刪不到，就是另一個
  請求先做完了；整個交易退回（不留多的封存、不寫稽核），照「不在」回 None。
- 清單：`added_at DESC, id DESC` 的 keyset 分頁，一頁 `LIBRARY_PAGE_SIZE` 篇，另給總篇數。
- 日誌只有 id，不帶論文標題這類內容。
"""

import uuid
from collections.abc import Sequence

import structlog

from radar.domain.audit import member_audit_event
from radar.domain.library import (
    LIBRARY_PAGE_SIZE,
    AddedVia,
    DeletedSource,
    LibraryCursor,
    LibraryEntry,
    LibraryItem,
    LibraryPage,
    encode_library_cursor,
)
from radar.domain.permissions import Action, require
from radar.domain.viewer import MemberViewer
from radar.repo.audit import create_audit_event
from radar.repo.db import Database, DbConnection
from radar.repo.deleted_records import (
    create_deleted_record,
    delete_deleted_record,
    find_deleted_library_item,
)
from radar.repo.library import (
    count_library_items,
    create_library_item,
    create_restored_library_item,
    delete_library_item,
    find_library_item,
    list_library_items,
)
from radar.repo.paper_rows import find_paper_rows

log = structlog.get_logger(__name__)


class _ChangedMeanwhileError(Exception):
    """交易裡要動的那一列被另一個請求先刪掉了：丟它讓整個交易退回，外面照「不在」處理。"""


# 稽核事件的動作與對象（小寫識別字，寫出去之後不改名）。
ADD_LIBRARY_ITEM_ACTION = "add_library_item"
REMOVE_LIBRARY_ITEM_ACTION = "remove_library_item"
RESTORE_LIBRARY_ITEM_ACTION = "restore_library_item"
LIBRARY_ITEM_TARGET = "library_item"


def _entries(
    viewer: MemberViewer, connection: DbConnection, items: Sequence[LibraryItem]
) -> tuple[LibraryEntry, ...]:
    """每一筆配上論文列，順序照 `items`。論文列讀不到的（同時被別的請求移出、私人論文就看不到了）
    跳過，記一筆只有 id 的警告。"""
    rows = find_paper_rows(viewer, connection, [item.paper_id for item in items])
    for item in items:
        if item.paper_id not in rows:
            log.warning(
                "文獻庫的一筆讀不到論文列，跳過",
                library_item_id=str(item.id),
                paper_id=str(item.paper_id),
            )
    return tuple(
        LibraryEntry(item=item, paper=rows[item.paper_id])
        for item in items
        if item.paper_id in rows
    )


def _entry(viewer: MemberViewer, connection: DbConnection, item: LibraryItem) -> LibraryEntry:
    """一筆配上論文列。在自己文獻庫裡的論文一定看得到；讀不到表示那一筆同時被另一個請求移出了
    （私人論文就看不到了）。

    Raises:
        _ChangedMeanwhileError: 讀不到這一篇的論文列（呼叫端讓交易退回、照「不在」處理）。
    """
    entries = _entries(viewer, connection, [item])
    if not entries:
        raise _ChangedMeanwhileError
    return entries[0]


def list_library(
    viewer: MemberViewer, database: Database, *, after: LibraryCursor | None
) -> LibraryPage:
    """`viewer` 自己的文獻庫的一頁（從 `after` 之後；None 是第一頁），加上總篇數。"""
    with database.transaction() as connection:
        items = list_library_items(viewer, connection, after=after, limit=LIBRARY_PAGE_SIZE + 1)
        page, more = items[:LIBRARY_PAGE_SIZE], len(items) > LIBRARY_PAGE_SIZE
        total = count_library_items(viewer, connection)
        entries = _entries(viewer, connection, page)
    next_cursor = encode_library_cursor(LibraryCursor.after(page[-1])) if more else None
    return LibraryPage(entries=entries, next_cursor=next_cursor, total=total)


def add_library_item(
    actor: MemberViewer, database: Database, paper_id: uuid.UUID
) -> LibraryEntry | None:
    """把 `actor` 看得到的論文加進自己的文獻庫（`added_via = manual`），寫稽核，回新增的那一筆。

    已經在文獻庫（任何 `added_via`）時不變更、不寫稽核，回原本那一筆。看不到、不存在、被排除的
    非研究文章（或同時被另一個請求移出、讀不到論文列）回 None。

    Raises:
        PermissionDeniedError: `actor` 不能加入（權限表）。
    """
    require(actor, Action.ADD_LIBRARY_ITEM, actor.member_id)
    try:
        with database.transaction() as connection:
            created = create_library_item(
                connection,
                actor,
                owner_id=actor.member_id,
                paper_id=paper_id,
                added_via=AddedVia.MANUAL,
            )
            if created is None:
                existing = find_library_item(actor, connection, paper_id)
                return None if existing is None else _entry(actor, connection, existing)
            create_audit_event(
                connection,
                member_audit_event(
                    actor,
                    action=ADD_LIBRARY_ITEM_ACTION,
                    target_type=LIBRARY_ITEM_TARGET,
                    target_id=created.id,
                    detail={"paper_id": str(paper_id), "added_via": created.added_via.value},
                ),
            )
            entry = _entry(actor, connection, created)
    except _ChangedMeanwhileError:
        log.info("要加入的那一篇同時被另一個請求移出", paper_id=str(paper_id))
        return None
    log.info("已加入文獻庫", library_item_id=str(created.id), paper_id=str(paper_id))
    return entry


def remove_library_item(
    actor: MemberViewer, database: Database, paper_id: uuid.UUID
) -> LibraryEntry | None:
    """把 `paper_id` 移出 `actor` 自己的文獻庫：先複製到 `deleted_record` 再刪，寫稽核，回移出前的
    那一筆。不在自己的文獻庫（或同時被另一個請求移出了）回 None；別人的那一筆不動。

    Raises:
        PermissionDeniedError: `actor` 不是這一筆的擁有者（權限表）。
    """
    try:
        with database.transaction() as connection:
            item = find_library_item(actor, connection, paper_id)
            if item is None:
                return None
            require(actor, Action.REMOVE_LIBRARY_ITEM, item.owner_id)
            # 刪掉之前讀論文列：私人論文移出之後就看不到了。
            entry = _entry(actor, connection, item)
            record_id = create_deleted_record(
                connection,
                source=DeletedSource.LIBRARY_ITEM,
                source_id=item.id,
                deleted_by_member_id=actor.member_id,
            )
            if record_id is None or delete_library_item(connection, item.id) is None:
                raise _ChangedMeanwhileError
            create_audit_event(
                connection,
                member_audit_event(
                    actor,
                    action=REMOVE_LIBRARY_ITEM_ACTION,
                    target_type=LIBRARY_ITEM_TARGET,
                    target_id=item.id,
                    detail={
                        "paper_id": str(paper_id),
                        "added_via": item.added_via.value,
                        "deleted_record_id": str(record_id),
                    },
                ),
            )
    except _ChangedMeanwhileError:
        log.info("要移出的那一筆已經被另一個請求移出", paper_id=str(paper_id))
        return None
    log.info("已移出文獻庫", library_item_id=str(item.id), deleted_record_id=str(record_id))
    return entry


def restore_library_item(
    actor: MemberViewer, database: Database, paper_id: uuid.UUID
) -> LibraryEntry | None:
    """把 `actor` 最近一次移出的 `paper_id` 復原成原本那一筆（`id`、`added_via`、`added_at` 不變），
    刪掉那筆封存、寫稽核，回現在文獻庫裡的那一筆。

    那篇已經又在文獻庫時不再寫入，回現在那一筆（冪等），封存照樣消耗。自己沒有這一篇的封存（或是
    別人的、或同時被另一個請求用掉了）回 None。

    Raises:
        PermissionDeniedError: `actor` 不是這筆封存的擁有者（權限表）。
    """
    try:
        with database.transaction() as connection:
            record = find_deleted_library_item(actor, connection, paper_id)
            if record is None:
                return None
            require(actor, Action.ADD_LIBRARY_ITEM, record.item.owner_id)
            restored = create_restored_library_item(connection, record.item)
            current = restored or find_library_item(actor, connection, paper_id)
            if current is None or not delete_deleted_record(connection, record.record_id):
                raise _ChangedMeanwhileError
            create_audit_event(
                connection,
                member_audit_event(
                    actor,
                    action=RESTORE_LIBRARY_ITEM_ACTION,
                    target_type=LIBRARY_ITEM_TARGET,
                    # 已經又在文獻庫時是現在那一筆（不是封存裡的舊 id）。
                    target_id=current.id,
                    detail={
                        "paper_id": str(paper_id),
                        "added_via": record.item.added_via.value,
                        "deleted_record_id": str(record.record_id),
                        "restored": restored is not None,
                    },
                ),
            )
            entry = _entry(actor, connection, current)
    except _ChangedMeanwhileError:
        log.info("要復原的封存已經被另一個請求用掉", paper_id=str(paper_id))
        return None
    log.info(
        "已復原文獻庫的一篇",
        library_item_id=str(current.id),
        deleted_record_id=str(record.record_id),
        restored=restored is not None,
    )
    return entry
