"""遷移用例：`radar migrate` 呼叫（AD-15：服務啟動時不遷移）。"""

from collections.abc import Callable

import structlog

log = structlog.get_logger(__name__)


def migrate_database(upgrade: Callable[[], frozenset[str]]) -> frozenset[str]:
    """把資料庫遷移到最新版本，回傳遷移後的版本。

    `upgrade` 由入口組裝好傳進來（在一個交易裡跑完，失敗就整個退回）；它丟出的例外原樣往上丟，
    由入口決定怎麼呈現。
    """
    log.info("開始遷移資料庫")
    revisions = upgrade()
    log.info("遷移完成", revisions=sorted(revisions))
    return revisions
