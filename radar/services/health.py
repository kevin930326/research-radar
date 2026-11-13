"""健康檢查用例。"""

from collections.abc import Callable

import structlog

log = structlog.get_logger(__name__)


def check_readiness(ping: Callable[[], bool]) -> bool:
    """服務是否準備好接請求：目前只看資料庫連不連得上。

    `ping` 丟出的任何例外都視為未就緒；日誌只記例外的類別名稱，不記訊息（可能含連線字串）。
    """
    try:
        return ping() is True
    except Exception as exc:
        log.warning("就緒檢查失敗", error_type=type(exc).__name__)
        return False
