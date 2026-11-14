"""排程工作的共同外殼：成功才送心跳（AD-15、ADR-0004 決定第 7 點）。"""

from collections.abc import Callable


def run_scheduled_job[T](
    check: str, work: Callable[[], T], send_heartbeat: Callable[[str], None]
) -> T:
    """執行 `work`；成功後用 `send_heartbeat` 送 `check`（這個排程的檢查名稱）一次。

    `work` 丟出的例外原樣往上丟、不送心跳：管理者因此收到「該報到沒報到」的通知。
    送心跳的函式自己處理送不出去的情況（只記錯誤），不會讓已經成功的工作變成失敗。
    """
    result = work()
    send_heartbeat(check)
    return result
