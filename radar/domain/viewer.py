"""讀者身分（AD-3、ADR-0002）：`repo` 的讀取函式第一個參數都是其中一種。

會改資料的 services 函式用同一個物件當「是誰做的」（參數叫 `actor`，AD-27）。

誰可以建立哪一種：
- `MemberViewer`：api 的登入相依項（由 session）；worker 替某位成員做事時（由狀態列的擁有者）；
  CLI 指令帶 `--as <管理者 email>` 時。
- `PublicViewer`：只有示範站產生器與展示問答。
- `SystemViewer`：只用在沒有特定使用者的工作（匯入、每週更新、Batch、維運）。
"""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class MemberViewer:
    """某一位成員。管理者透過網站看到的和一般成員一樣。"""

    member_id: uuid.UUID
    is_admin: bool


@dataclass(frozen=True)
class PublicViewer:
    """示範站產生器與展示問答：只看得到可以公開的資料。"""


@dataclass(frozen=True)
class SystemViewer:
    """沒有特定使用者的工作：看得到全部。"""


type Viewer = MemberViewer | PublicViewer | SystemViewer
