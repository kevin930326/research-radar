"""可見條件：誰看得到哪些資料，全專案只寫在這裡（AD-4、ADR-0002）。

其他查詢把這裡回的條件接進自己的 WHERE，不自己判斷讀者身分（架構測試守著：repo 的讀取函式
要把 `viewer` 交給這裡的函式，其他模組不准用 isinstance、match 或屬性判斷 viewer）。
"""

import re
import uuid

from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer, Viewer

# 別名直接組進 SQL，所以只收小寫識別字（PostgreSQL 的名稱上限 63 個字元）。
_ALIAS = re.compile(r"[a-z_][a-z0-9_]{0,62}")
# `PublicViewer` 的 EXISTS 子查詢裡 `counted_paper` 的別名；外面的別名不能和它相同。
_COUNTED_ALIAS = "visible_counted"
# `MemberViewer` 的 EXISTS 子查詢裡 `library_item` 的別名；外面的別名同樣不能和它相同。
_LIBRARY_ALIAS = "visible_library"
_SUBQUERY_ALIASES = frozenset({_COUNTED_ALIAS, _LIBRARY_ALIAS})


def _member_literal(member_id: uuid.UUID) -> str:
    """成員 ID 組進 SQL 的常值：先轉成標準寫法的 UUID（擋注入），再加上型別。"""
    return f"'{uuid.UUID(str(member_id))}'::uuid"


def _checked_alias(alias: str) -> str:
    if not _ALIAS.fullmatch(alias):
        raise ValueError(f"別名只能是小寫識別字：{alias!r}")
    return alias


def visible_papers(viewer: Viewer, *, alias: str = "paper") -> str:
    """`viewer` 看得到的論文的 SQL 條件，`alias` 是查詢裡 `paper` 表的名稱或別名。

    - `MemberViewer`：`lab_visible` 為真的論文，或在這位成員的文獻庫（`library_item`，Story 3.16）
      裡的論文（AD-4）。成員 ID 以標準寫法的 UUID 常值組進去。
    - `SystemViewer`：全部。
    - `PublicViewer`：看得到的論文 ⇔ 在 `counted_paper` 裡（AD-4、AD-5）：追蹤範圍內、沒有被排除、
      已算進某個已發佈的快照。用 `EXISTS` 子查詢接，所以 `alias` 是 `paper` 表或 `counted_paper`
      都可以用（兩者的 `id` 是同一個）。

    回傳的字串只由驗證過的識別字與重新組過的 UUID 常值組成，不含其他值。

    Raises:
        ValueError: `alias` 不是小寫識別字，或和子查詢裡的別名（`visible_counted`、
            `visible_library`）相同（內外別名相同時條件永遠成立）。
        TypeError: `viewer` 不是三種讀者身分之一。
    """
    _checked_alias(alias)
    if alias in _SUBQUERY_ALIASES:
        raise ValueError(f"別名不能是 {alias!r}（可見條件的子查詢用了這個名稱）")
    match viewer:
        case MemberViewer(member_id=member_id):
            return (
                f'("{alias}".lab_visible OR EXISTS (SELECT 1 FROM library_item AS {_LIBRARY_ALIAS}'
                f' WHERE {_LIBRARY_ALIAS}.paper_id = "{alias}".id'
                f" AND {_LIBRARY_ALIAS}.owner_id = {_member_literal(member_id)}))"
            )
        case SystemViewer():
            return "TRUE"
        case PublicViewer():
            return (
                f"EXISTS (SELECT 1 FROM counted_paper AS {_COUNTED_ALIAS}"
                f' WHERE {_COUNTED_ALIAS}.id = "{alias}".id)'
            )
        case _:
            raise TypeError(f"不認得的讀者身分：{type(viewer).__name__}")


def visible_lab_papers(viewer: Viewer, *, alias: str) -> str:
    """`viewer` 看得到、而且全實驗室都看得到（`lab_visible`）的論文的 SQL 條件（找論文清單的 `lab`
    範圍，Story 3.13）。`alias` 是查詢裡 `paper` 表的名稱或別名。

    ＝`lab_visible` 且 `visible_papers(viewer, alias=alias)`：成員自己文獻庫裡的私人論文不算；訪客
    只剩 `counted_paper` 裡的；系統是全部 `lab_visible` 的論文。

    Raises:
        ValueError: `alias` 不是小寫識別字，或和可見條件子查詢裡的別名相同（同 `visible_papers`）。
        TypeError: `viewer` 不是三種讀者身分之一。
    """
    visible = visible_papers(viewer, alias=alias)  # 別名與讀者身分在這裡檢查
    return f'("{alias}".lab_visible AND {visible})'


def visible_in_own_library(viewer: Viewer, *, alias: str = "paper") -> str:
    """「這篇論文在 `viewer` 自己的文獻庫裡」的 SQL 布林運算式（Story 3.17、ADR-0053），`alias` 是
    查詢裡 `paper` 表的名稱或別名。放在 SELECT 裡當一欄，和外面的查詢一起算（一句算整頁，不是每篇
    一句）。

    - `MemberViewer`：`EXISTS` 子查詢，只看 `owner_id` 是自己的那一筆（管理者也一樣）。
    - `SystemViewer`、`PublicViewer`：`FALSE`。系統與訪客沒有「自己的文獻庫」；不能沿用
      `visible_library_items` 的「系統看得到全部」，否則「任何人加過」會被當成「在我的文獻庫」，
      經示範站匯出等系統身分的路徑把別人的文獻庫狀態帶出去（ADR-0050 第 100 行）。

    Raises:
        ValueError: `alias` 不是小寫識別字，或和子查詢裡的別名相同。
        TypeError: `viewer` 不是三種讀者身分之一。
    """
    _checked_alias(alias)
    if alias in _SUBQUERY_ALIASES:
        raise ValueError(f"別名不能是 {alias!r}（可見條件的子查詢用了這個名稱）")
    match viewer:
        case MemberViewer(member_id=member_id):
            return (
                f"EXISTS (SELECT 1 FROM library_item AS {_LIBRARY_ALIAS}"
                f' WHERE {_LIBRARY_ALIAS}.paper_id = "{alias}".id'
                f" AND {_LIBRARY_ALIAS}.owner_id = {_member_literal(member_id)})"
            )
        case SystemViewer() | PublicViewer():
            return "FALSE"
        case _:
            raise TypeError(f"不認得的讀者身分：{type(viewer).__name__}")


def visible_library_items(viewer: Viewer, *, alias: str = "library_item") -> str:
    """`viewer` 看得到的文獻庫的筆（`library_item`）的 SQL 條件，`alias` 是查詢裡這張表的名稱
    或別名。

    - `MemberViewer`：只有自己的（`owner_id` 是自己）。管理者也一樣：文獻庫是私人的。
    - `SystemViewer`：全部。
    - `PublicViewer`：沒有（`FALSE`）。

    Raises:
        ValueError: `alias` 不是小寫識別字。
        TypeError: `viewer` 不是三種讀者身分之一。
    """
    return _owned_rows(viewer, _checked_alias(alias))


def visible_deleted_records(viewer: Viewer, *, alias: str = "deleted_record") -> str:
    """`viewer` 看得到的刪除封存（`deleted_record`）的 SQL 條件；規則和 `visible_library_items`
    一樣（成員只看 `owner_id` 是自己的、系統全部、訪客沒有）。

    Raises:
        ValueError: `alias` 不是小寫識別字。
        TypeError: `viewer` 不是三種讀者身分之一。
    """
    return _owned_rows(viewer, _checked_alias(alias))


def _owned_rows(viewer: Viewer, alias: str) -> str:
    match viewer:
        case MemberViewer(member_id=member_id):
            return f'"{alias}".owner_id = {_member_literal(member_id)}'
        case SystemViewer():
            return "TRUE"
        case PublicViewer():
            return "FALSE"
        case _:
            raise TypeError(f"不認得的讀者身分：{type(viewer).__name__}")


def visible_to_system_only(viewer: Viewer) -> str:
    """只給沒有特定使用者的工作讀的資料（例如 sweep 讀的狀態表）的 SQL 條件。

    `SystemViewer` 看得到全部（回 `TRUE`）；其他身分不組查詢、直接丟錯。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    if not isinstance(viewer, SystemViewer):
        raise TypeError(f"只給 SystemViewer（現在是 {type(viewer).__name__}）")
    return "TRUE"


def visible_snapshot_facts(viewer: Viewer) -> str:
    """快照編號與截止時間（`snapshot` 表）的 SQL 條件：系統工作與示範站匯出（`PublicViewer`，
    Story 3.7）讀得到，都回 `TRUE`。已發佈的快照編號與截止時間本來就印在示範站與每個統計回應上，
    不是私人資料；建立中、失敗的快照由呼叫的查詢自己用 `status` 篩掉。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer` 或 `PublicViewer`（成員的畫面用系統身分讀快照）。
    """
    if not isinstance(viewer, SystemViewer | PublicViewer):
        raise TypeError(f"只給 SystemViewer 或 PublicViewer（現在是 {type(viewer).__name__}）")
    return "TRUE"
