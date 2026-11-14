"""可見條件：誰看得到哪些資料，全專案只寫在這裡（AD-4、ADR-0002）。

其他查詢把這裡回的條件接進自己的 WHERE，不自己判斷讀者身分（架構測試守著：repo 的讀取函式
要把 `viewer` 交給這裡的函式，其他模組不准用 isinstance、match 或屬性判斷 viewer）。
"""

import re

from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer, Viewer

# 別名直接組進 SQL，所以只收小寫識別字（PostgreSQL 的名稱上限 63 個字元）。
_ALIAS = re.compile(r"[a-z_][a-z0-9_]{0,62}")
# `PublicViewer` 的 EXISTS 子查詢裡 `counted_paper` 的別名；外面的別名不能和它相同。
_COUNTED_ALIAS = "visible_counted"


def visible_papers(viewer: Viewer, *, alias: str = "paper") -> str:
    """`viewer` 看得到的論文的 SQL 條件，`alias` 是查詢裡 `paper` 表的名稱或別名。

    - `MemberViewer`：`lab_visible` 為真的論文。（AD-4 的「或在這位成員的 `library_item`」
      等 `library_item` 建好時補上。）
    - `SystemViewer`：全部。
    - `PublicViewer`：看得到的論文 ⇔ 在 `counted_paper` 裡（AD-4、AD-5）：追蹤範圍內、沒有被排除、
      已算進某個已發佈的快照。用 `EXISTS` 子查詢接，所以 `alias` 是 `paper` 表或 `counted_paper`
      都可以用（兩者的 `id` 是同一個）。

    回傳的字串只由驗證過的識別字組成，不含任何值。

    Raises:
        ValueError: `alias` 不是小寫識別字，或和子查詢裡的別名 `visible_counted` 相同（內外別名
            相同時條件永遠成立）。
        TypeError: `viewer` 不是三種讀者身分之一。
    """
    if not _ALIAS.fullmatch(alias):
        raise ValueError(f"別名只能是小寫識別字：{alias!r}")
    if alias == _COUNTED_ALIAS:
        raise ValueError(f"別名不能是 {_COUNTED_ALIAS!r}（可見條件的子查詢用了這個名稱）")
    match viewer:
        case MemberViewer():
            return f'"{alias}".lab_visible'
        case SystemViewer():
            return "TRUE"
        case PublicViewer():
            return (
                f"EXISTS (SELECT 1 FROM counted_paper AS {_COUNTED_ALIAS}"
                f' WHERE {_COUNTED_ALIAS}.id = "{alias}".id)'
            )
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
