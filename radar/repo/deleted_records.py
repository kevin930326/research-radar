"""`deleted_record`（刪除前的封存）：表的定義與唯一的寫入者（Story 3.16、AD-26）。

- 刪除私人資料前，在同一個交易把來源列整列複製成 jsonb（`to_jsonb`），記來源表、來源列的 id、
  擁有者、刪的人（空的＝系統）。之後可以從這裡復原。
- 來源表只能是白名單（`domain/library.DeletedSource`）裡的；每一種一句寫死的 SQL，表名不是變數。
- 不設外鍵到來源表（來源列已經刪掉）；應用程式角色沒有 UPDATE（遷移 0020 收回），所以也不能
  `SELECT … FOR UPDATE`（PostgreSQL 鎖列要 UPDATE 權限）。封存不會被改，復原時以
  `delete_deleted_record` 有沒有真的刪到判斷是不是被同時用掉了。
- 不清理、不設期限（之後要清理另開 Story）。
- 讀取一律帶 `repo/visibility.visible_deleted_records` 的條件：成員只讀得到自己的。
- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。
"""

import uuid
from collections.abc import Mapping
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    Table,
    Text,
    TextClause,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from radar.domain.library import AddedVia, DeletedLibraryItem, DeletedSource, LibraryItem
from radar.domain.viewer import Viewer
from radar.repo.models import Base
from radar.repo.visibility import visible_deleted_records

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0020 建表） -----------------------------

deleted_record_table = Table(
    "deleted_record",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("source_table", Text, nullable=False),
    Column("source_id", Uuid, nullable=False),
    Column("content", JSONB, nullable=False),
    Column("owner_id", Uuid, ForeignKey("member.id")),
    # 空的＝系統刪的。
    Column("deleted_by_member_id", Uuid, ForeignKey("member.id"), index=True),
    Column("deleted_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # 找自己最新的紀錄；也當 `owner_id` 外鍵的索引。
    Index(
        "ix_deleted_record_owner_id_source_table_deleted_at",
        "owner_id",
        "source_table",
        "deleted_at",
    ),
    CheckConstraint("source_table ~ '^[a-z][a-z0-9_]*$'", name="source_table_format"),
    CheckConstraint("jsonb_typeof(content) = 'object'", name="content_object"),
)

# --- SQL ----------------------------------------------------------------------------------

# 白名單：每個來源表一句寫死的複製 SQL（`to_jsonb` 整列、擁有者照來源列的 `owner_id`）。
_COPY: Final[Mapping[DeletedSource, TextClause]] = {
    DeletedSource.LIBRARY_ITEM: text(
        "INSERT INTO deleted_record"
        " (source_table, source_id, content, owner_id, deleted_by_member_id)"
        " SELECT 'library_item', s.id, to_jsonb(s), s.owner_id, :deleted_by_member_id"
        " FROM library_item AS s WHERE s.id = :source_id"
        " RETURNING id"
    ),
}
_DELETE: Final = text("DELETE FROM deleted_record WHERE id = :id RETURNING id")


# --- 寫入 ---------------------------------------------------------------------------------


def create_deleted_record(
    connection: Connection,
    *,
    source: DeletedSource,
    source_id: uuid.UUID,
    deleted_by_member_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """把 `source` 表裡 `source_id` 那一列整列複製到 `deleted_record`，回封存的 id；來源列已經不在
    （例如被同時刪掉）回 None。在刪掉來源列之前、同一個交易裡呼叫，不 commit。

    Raises:
        ValueError: `source` 不在白名單。
    """
    statement = _COPY.get(DeletedSource(source))
    if statement is None:
        raise ValueError(f"不能封存這張表：{source!r}")
    value = connection.execute(
        statement, {"source_id": source_id, "deleted_by_member_id": deleted_by_member_id}
    ).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


def delete_deleted_record(connection: Connection, record_id: uuid.UUID) -> bool:
    """刪掉這筆封存（復原時消耗）；有刪到回 True。不 commit。"""
    return connection.execute(_DELETE, {"id": record_id}).scalar_one_or_none() is not None


# --- 讀取 ---------------------------------------------------------------------------------


def find_deleted_library_item(
    viewer: Viewer, connection: Connection, paper_id: uuid.UUID
) -> DeletedLibraryItem | None:
    """`viewer` 自己移出過的、`paper_id` 那一篇最新的一筆封存；沒有回 None。

    封存的內容在 SQL 裡轉回欄位的型別（時間不經過字串解析）。
    """
    visible = visible_deleted_records(viewer, alias="deleted_record")
    row = (
        connection.execute(
            text(
                "SELECT deleted_record.id AS record_id,"
                " (content->>'id')::uuid AS id, (content->>'owner_id')::uuid AS owner_id,"
                " (content->>'paper_id')::uuid AS paper_id, content->>'added_via' AS added_via,"
                " (content->>'added_at')::timestamptz AS added_at"
                f" FROM deleted_record WHERE {visible}"
                # 比文字、不轉型：別的來源表的內容不一定有能轉成 UUID 的 `paper_id`。
                " AND source_table = :source AND content->>'paper_id' = :paper_id"
                " ORDER BY deleted_record.deleted_at DESC, deleted_record.id DESC LIMIT 1"
            ),
            {"source": DeletedSource.LIBRARY_ITEM.value, "paper_id": str(paper_id)},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return DeletedLibraryItem(
        record_id=row["record_id"],
        item=LibraryItem(
            id=row["id"],
            owner_id=row["owner_id"],
            paper_id=row["paper_id"],
            added_via=AddedVia(row["added_via"]),
            added_at=row["added_at"],
        ),
    )
