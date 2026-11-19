"""個人文獻庫的層間資料（Story 3.16、AD-4、AD-26、FR-35、FR-50）。

- `library_item`：成員（`owner_id`）的文獻庫裡的一篇論文，對（成員, 論文）唯一；`added_via` 三種值
  （`AddedVia`）。表在遷移 0020（`repo/library.py`）。
- 移出前先把那一列整列複製到 `deleted_record`（`repo/deleted_records.py`），之後可以復原成原本的
  那一筆（`id`、`added_via`、`added_at` 都不變）。復原沒有期限（前端決定什麼時候不再提供）。
- 清單照 `added_at DESC, id DESC` 排，keyset 分頁（`LibraryCursor`）：一頁 `LIBRARY_PAGE_SIZE` 篇，
  下一頁的位置是這一頁最後一筆的（`added_at`, `id`），編成不透明的字串給前端原樣傳回。
- 加入、移出、復原在交易一開始拿（成員, 論文）的交易級 advisory lock（`library_item_lock_key`），
  同一位成員對同一篇的寫入排隊做完。
"""

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from radar.domain.fingerprints import named_lock_key
from radar.domain.paper_rows import PaperRowFacts

# 文獻庫清單一頁幾篇（前端的 `client-config` 也給這個值）。
LIBRARY_PAGE_SIZE: Final = 50

# 下一頁位置字串的長度上限（正常的長度約 90 個字元；太長的直接當格式錯）。
_CURSOR_MAX_LENGTH: Final = 200
_CURSOR_SEPARATOR: Final = ","


class AddedVia(StrEnum):
    """這一篇怎麼進了文獻庫（`library_item.added_via`）。"""

    UPLOAD = "upload"  # 上傳 PDF（Epic 6）
    SHARED_LIST = "shared_list"  # 從分享的清單加入
    MANUAL = "manual"  # 在論文頁按「加入我的文獻庫」


class DeletedSource(StrEnum):
    """`deleted_record.source_table` 可以是哪些表（`repo/deleted_records.py` 的白名單）。"""

    LIBRARY_ITEM = "library_item"


def library_item_lock_key(owner_id: uuid.UUID, paper_id: uuid.UUID) -> int:
    """（成員, 論文）的交易級 advisory lock 鍵（Story 3.16 審查後）：加入、移出、復原都在交易一開始
    拿這把獨佔鎖（`repo/db.Database.transaction_lock`），同一位成員對同一篇的寫入排隊做完。

    名稱帶 `library_item:` 前綴再經 `fingerprints.named_lock_key` 雜湊，和其他鎖（固定名稱、
    `run_lock_key`、摘要來源）分在不同的命名空間；撞到的機率約 2^-64，撞到只是多等一下。
    """
    return named_lock_key(f"library_item:{owner_id}:{paper_id}")


def _check_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 要是帶時區的時間")


@dataclass(frozen=True, slots=True)
class LibraryItem:
    """文獻庫的一筆（`library_item` 的一列）。

    Raises:
        ValueError: `added_via` 不是三種值之一、`added_at` 沒有時區。
    """

    id: uuid.UUID
    owner_id: uuid.UUID
    paper_id: uuid.UUID
    added_via: AddedVia
    added_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "added_via", AddedVia(self.added_via))
        _check_aware(self.added_at, "added_at")


@dataclass(frozen=True, slots=True)
class DeletedLibraryItem:
    """移出時封存的那一筆（`deleted_record` 的 id 與還原出來的 `LibraryItem`）。"""

    record_id: uuid.UUID
    item: LibraryItem


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    """文獻庫的一筆，加上論文列要的欄位。

    Raises:
        ValueError: 論文列不是這一筆的那篇論文。
    """

    item: LibraryItem
    paper: PaperRowFacts

    def __post_init__(self) -> None:
        if self.paper.paper_id != self.item.paper_id:
            raise ValueError("論文列要是這一筆的那篇論文")


@dataclass(frozen=True, slots=True)
class LibraryCursor:
    """清單的下一頁從哪裡開始：上一頁最後一筆的（`added_at`, `id`）。

    Raises:
        ValueError: `added_at` 沒有時區。
    """

    added_at: datetime
    item_id: uuid.UUID

    def __post_init__(self) -> None:
        _check_aware(self.added_at, "added_at")

    @classmethod
    def after(cls, item: LibraryItem) -> LibraryCursor:
        """接在 `item` 之後的位置。"""
        return cls(added_at=item.added_at, item_id=item.id)


@dataclass(frozen=True, slots=True)
class LibraryPage:
    """清單的一頁：這一頁的每一筆（照 `added_at DESC, id DESC`）、下一頁的位置（沒有下一頁是
    None）、文獻庫的總篇數。

    總數和這一頁是分開的兩句查詢讀的（中間可能有別的請求加入或移出），所以不檢查兩者對不對得上。

    Raises:
        ValueError: 篇數超過一頁、總數是負的。
    """

    entries: tuple[LibraryEntry, ...]
    next_cursor: str | None
    total: int

    def __post_init__(self) -> None:
        if len(self.entries) > LIBRARY_PAGE_SIZE:
            raise ValueError(f"一頁最多 {LIBRARY_PAGE_SIZE} 篇")
        if type(self.total) is not int or self.total < 0:
            raise ValueError("total 要是非負整數")


def encode_library_cursor(cursor: LibraryCursor) -> str:
    """位置 → 不透明的字串（URL 安全的 base64、沒有 `=`）。時間一律先轉成 UTC。"""
    plain = (
        f"{cursor.added_at.astimezone(UTC).isoformat(timespec='microseconds')}"
        f"{_CURSOR_SEPARATOR}{cursor.item_id}"
    )
    return base64.urlsafe_b64encode(plain.encode("ascii")).decode("ascii").rstrip("=")


def decode_library_cursor(value: str) -> LibraryCursor:
    """`encode_library_cursor` 的反向；只收它產生的寫法（重新編碼要一模一樣）。

    Raises:
        ValueError: 不是 `encode_library_cursor` 產生的字串（太長、不是 base64、時間或 ID 不對）。
    """
    if not isinstance(value, str) or not 0 < len(value) <= _CURSOR_MAX_LENGTH:
        raise ValueError("下一頁的位置格式不對")
    try:
        padded = value + "=" * (-len(value) % 4)
        plain = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
        added_at_text, item_id_text = plain.split(_CURSOR_SEPARATOR)
        cursor = LibraryCursor(
            added_at=datetime.fromisoformat(added_at_text), item_id=uuid.UUID(item_id_text)
        )
        # 重新編碼會轉成 UTC：年份 1 或 9999 帶極端時差時超出範圍（`OverflowError`），也算格式錯。
        same = encode_library_cursor(cursor) == value
    except (UnicodeError, binascii.Error, ValueError, OverflowError) as exc:
        raise ValueError("下一頁的位置格式不對") from exc
    if not same:
        raise ValueError("下一頁的位置格式不對")
    return cursor
