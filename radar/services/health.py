"""健康檢查用例。"""

from collections.abc import Callable

import structlog

from radar.domain.schema import READY_STATES, SchemaState

log = structlog.get_logger(__name__)


def check_readiness(ping: Callable[[], bool], schema_state: Callable[[], SchemaState]) -> bool:
    """服務是否準備好接請求：資料庫連得上，而且遷移版本不落後（AD-15）。

    - 連不上（`ping` 不是 True）就不查遷移狀態。
    - 還沒遷移（`not_migrated`）或落後（`behind`）→ 未就緒；服務自己不會遷移。
    - 資料庫版本比程式新（`ahead`，退版時）→ 就緒並記警告（AD-29）。

    任何例外都視為未就緒；日誌只記例外的類別名稱，不記訊息（可能含連線字串）。
    """
    try:
        if ping() is not True:
            return False
        state = schema_state()
    except Exception as exc:
        log.warning("就緒檢查失敗", error_type=type(exc).__name__)
        return False
    if state not in READY_STATES:
        log.warning("資料庫的遷移版本不是最新，未就緒", schema_state=state.value)
        return False
    if state is SchemaState.AHEAD:
        log.warning("資料庫的遷移版本比程式新（退版中？），照常就緒", schema_state=state.value)
    return True
