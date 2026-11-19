"""`library_item`（個人文獻庫）：表的定義與唯一的寫入者（Story 3.16、AD-4、AD-26、FR-35、FR-50）。

- 對（成員, 論文）唯一；`owner_id` NOT NULL；`added_via` 三種值（`domain/library.AddedVia`）。
- 應用程式角色沒有 UPDATE（遷移 0020 收回）：一筆只會新增、刪除（移出）、用原本的值新增（復原）。
  所以也不能 `SELECT … FOR UPDATE`（PostgreSQL 鎖列要 UPDATE 權限）；列不會被改，移出時以
  `delete_library_item` 有沒有真的刪到判斷是不是被同時移出了。
- 加入是一句 `INSERT … SELECT … ON CONFLICT DO NOTHING`：論文要是 `viewer` 看得到、沒有被排除的，
  不先查存在（ADR-0002）；已經在文獻庫（任何 `added_via`）就不寫，原本那一筆不變。
- 讀取一律帶 `repo/visibility.visible_library_items` 的條件：成員只讀得到自己的。
- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。稽核與移出前的封存在
  `services/library.py` 同一個交易寫。
"""

import uuid
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    RowMapping,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from radar.domain.library import AddedVia, LibraryCursor, LibraryItem
from radar.domain.viewer import Viewer
from radar.repo.models import Base
from radar.repo.visibility import visible_library_items, visible_papers

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0020 建表） -----------------------------

library_item_table = Table(
    "library_item",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("owner_id", Uuid, ForeignKey("member.id"), nullable=False),
    Column("paper_id", Uuid, ForeignKey("paper.id"), nullable=False, index=True),
    Column("added_via", Text, nullable=False),
    Column("added_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # 同一位成員同一篇只有一筆；也當 `owner_id` 外鍵的索引。
    UniqueConstraint("owner_id", "paper_id"),
    # 清單的 keyset 分頁（`added_at DESC, id DESC`）。
    Index("ix_library_item_owner_id_added_at_id", "owner_id", "added_at", "id"),
    CheckConstraint("added_via IN ('upload', 'shared_list', 'manual')", name="added_via_values"),
)

# --- SQL ----------------------------------------------------------------------------------

_COLUMNS: Final = "id, owner_id, paper_id, added_via, added_at"
_RETURNING: Final = f" RETURNING {_COLUMNS}"
_RESTORE: Final = text(
    "INSERT INTO library_item (id, owner_id, paper_id, added_via, added_at)"
    " VALUES (:id, :owner_id, :paper_id, :added_via, :added_at)"
    f" ON CONFLICT DO NOTHING{_RETURNING}"
)
_DELETE: Final = text(f"DELETE FROM library_item WHERE id = :id{_RETURNING}")


def _item(row: RowMapping) -> LibraryItem:
    return LibraryItem(
        id=row["id"],
        owner_id=row["owner_id"],
        paper_id=row["paper_id"],
        added_via=AddedVia(row["added_via"]),
        added_at=row["added_at"],
    )


# --- 寫入 ---------------------------------------------------------------------------------


def create_library_item(
    connection: Connection,
    viewer: Viewer,
    *,
    owner_id: uuid.UUID,
    paper_id: uuid.UUID,
    added_via: AddedVia,
) -> LibraryItem | None:
    """把 `paper_id` 加進 `owner_id` 的文獻庫，回新增的那一筆。

    一句 SQL：論文要是 `viewer` 看得到（`visible_papers`）、沒有被排除的；不存在、看不到、被排除、
    已經在這位成員的文獻庫，都不寫、回 None（呼叫端再查自己的文獻庫分辨「已經在」）。在呼叫端開好的
    交易裡做，不 commit。
    """
    visible = visible_papers(viewer, alias="paper")
    row = (
        connection.execute(
            text(
                "INSERT INTO library_item (owner_id, paper_id, added_via)"
                " SELECT :owner_id, paper.id, :added_via FROM paper"
                f" WHERE {visible} AND paper.excluded_reason IS NULL AND paper.id = :paper_id"
                f" ON CONFLICT DO NOTHING{_RETURNING}"
            ),
            {
                "owner_id": owner_id,
                "paper_id": paper_id,
                "added_via": AddedVia(added_via).value,
            },
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _item(row)


def create_restored_library_item(connection: Connection, item: LibraryItem) -> LibraryItem | None:
    """用移出前的原值（`id`、`owner_id`、`paper_id`、`added_via`、`added_at`）把這一筆寫回去，回寫回
    的那一筆；這位成員的文獻庫已經又有這一篇（或同一個 id）時不寫、回 None。不 commit。"""
    row = (
        connection.execute(
            _RESTORE,
            {
                "id": item.id,
                "owner_id": item.owner_id,
                "paper_id": item.paper_id,
                "added_via": item.added_via.value,
                "added_at": item.added_at,
            },
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _item(row)


def delete_library_item(connection: Connection, item_id: uuid.UUID) -> LibraryItem | None:
    """刪掉這一筆，回刪掉的那一筆；已經不在回 None。呼叫端先把它複製到 `deleted_record`
    （`repo/deleted_records.create_deleted_record`）。不 commit。"""
    row = connection.execute(_DELETE, {"id": item_id}).mappings().one_or_none()
    return None if row is None else _item(row)


# --- 讀取 ---------------------------------------------------------------------------------


def find_library_item(
    viewer: Viewer, connection: Connection, paper_id: uuid.UUID
) -> LibraryItem | None:
    """`viewer` 自己的文獻庫裡 `paper_id` 那一筆；不在（或是別人的）回 None。

    系統身分看得到每位成員的筆，一篇可能有好幾筆（回最新的一筆），只給成員身分用。
    """
    visible = visible_library_items(viewer, alias="library_item")
    row = (
        connection.execute(
            text(
                f"SELECT {_COLUMNS} FROM library_item"
                f" WHERE {visible} AND paper_id = :paper_id"
                " ORDER BY added_at DESC, id DESC LIMIT 1"
            ),
            {"paper_id": paper_id},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _item(row)


def list_library_items(
    viewer: Viewer, connection: Connection, *, after: LibraryCursor | None, limit: int
) -> list[LibraryItem]:
    """`viewer` 看得到的文獻庫的筆，照 `added_at DESC, id DESC`，從 `after` 之後最多 `limit` 筆。

    Raises:
        ValueError: `limit` 不是正整數。
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("limit 要是正整數")
    visible = visible_library_items(viewer, alias="library_item")
    keyset = "" if after is None else " AND (added_at, id) < (:after_at, :after_id)"
    rows = (
        connection.execute(
            text(
                f"SELECT {_COLUMNS} FROM library_item"
                f" WHERE {visible}{keyset}"
                " ORDER BY added_at DESC, id DESC LIMIT :limit"
            ),
            {
                "after_at": None if after is None else after.added_at,
                "after_id": None if after is None else after.item_id,
                "limit": limit,
            },
        )
        .mappings()
        .all()
    )
    return [_item(row) for row in rows]


def count_library_items(viewer: Viewer, connection: Connection) -> int:
    """`viewer` 看得到的文獻庫的筆數（只數 `library_item`，不接論文表）。"""
    visible = visible_library_items(viewer, alias="library_item")
    value = connection.execute(
        text(f"SELECT count(*) FROM library_item WHERE {visible}")
    ).scalar_one()
    return int(value)
