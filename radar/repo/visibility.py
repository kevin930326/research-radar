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
# `visible_demo_papers` 的 EXISTS 子查詢裡 `demo_full_paper` 的別名。
_DEMO_ALIAS = "visible_demo"
_SUBQUERY_ALIASES = frozenset({_COUNTED_ALIAS, _LIBRARY_ALIAS, _DEMO_ALIAS})


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


def visible_demo_papers(viewer: Viewer, *, alias: str) -> str:
    """示範站有完整內容的論文（view `demo_full_paper`）的 SQL 條件：檢索的 `demo` 範圍（Story 4.1、
    ADR-0005 第 2 點）。`alias` 是查詢裡 `paper` 表的名稱或別名。

    **只給 `PublicViewer`**（示範站產生器）：成員與系統工作不組查詢、直接丟錯（型別檢查之外的
    執行時防線）。回的條件再接上 `visible_papers(viewer)`（`demo_full_paper` 本身就在
    `counted_paper` 裡，所以和訪客看得到的論文一致）。

    Raises:
        ValueError: `alias` 不是小寫識別字，或和可見條件子查詢裡的別名相同。
        TypeError: `viewer` 不是 `PublicViewer`。
    """
    _checked_alias(alias)
    if alias in _SUBQUERY_ALIASES:
        raise ValueError(f"別名不能是 {alias!r}（可見條件的子查詢用了這個名稱）")
    if not isinstance(viewer, PublicViewer):
        raise TypeError(f"demo 範圍只給 PublicViewer（現在是 {type(viewer).__name__}）")
    visible = visible_papers(viewer, alias=alias)
    return (
        f"(EXISTS (SELECT 1 FROM demo_full_paper AS {_DEMO_ALIAS}"
        f' WHERE {_DEMO_ALIAS}.id = "{alias}".id)'
        f" AND {visible})"
    )


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


def _reads_member_judgments(viewer: Viewer) -> bool:
    """成員的人工判斷（修正分類的歷史、回報中文重點的標記）讀不讀得到：成員與系統工作（管理指令）
    讀得到；示範站產生器（`PublicViewer`）與其他任何東西都讀不到。讀不到時要拒絕查詢還是讀成空的，
    由各個呼叫的函式決定（`visible_member_judgments`、`visible_summary_report_marks`）。"""
    return isinstance(viewer, MemberViewer | SystemViewer)


def visible_member_judgments(viewer: Viewer) -> str:
    """成員的人工判斷紀錄（修正分類的歷史：誰、什麼時候、選了什麼、理由，Story 3.11）的 SQL 條件：
    成員與系統工作（管理指令）讀得到，都回 `TRUE`（論文本身看不看得到另外接 `visible_papers`）。
    示範站產生器（`PublicViewer`）不組查詢、直接丟錯：示範站不帶修正歷史、姓名與理由（ADR-0063）。

    Raises:
        TypeError: `viewer` 不是 `MemberViewer` 或 `SystemViewer`。
    """
    if not _reads_member_judgments(viewer):
        raise TypeError(f"修正歷史只給成員或 SystemViewer（現在是 {type(viewer).__name__}）")
    return "TRUE"


def visible_summary_report_marks(viewer: Viewer) -> str:
    """「這一欄有成員回報可能有誤」的標記（`effective_zh_summary.reported_fields`，
    Story 3.12）讀不讀得到的 SQL 布林運算式：成員與系統工作是 `TRUE`；示範站產生器
    （`PublicViewer`）是 `FALSE`——它讀中文重點的內容，但標記一律讀成空的（示範站不帶回報，
    ADR-0007 白名單也擋）。標記只有欄位名稱；誰回報、說明另經 `visible_member_judgments`，
    公開身分直接丟錯。誰讀得到和修正歷史共用 `_reads_member_judgments`；差別只在公開身分讀成空的、
    不丟錯。

    Raises:
        TypeError: `viewer` 不是三種讀者身分之一。
    """
    if _reads_member_judgments(viewer):
        return "TRUE"
    if isinstance(viewer, PublicViewer):
        return "FALSE"
    raise TypeError(f"不認得的讀者身分：{type(viewer).__name__}")


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
