"""預設時鐘：`radar costs` 與 `BudgetGuard` 沒注入時鐘時用的 `_utc_now` 一定帶時區
（不帶時區的話 `month_window` 會丟 `ValueError`，指令與每個預算檢查都壞掉）。"""

from collections.abc import Callable
from datetime import datetime

import pytest

import radar.entrypoints.cli as cli
from radar.domain.budget import month_window
from radar.services import budget


@pytest.mark.parametrize("clock", [cli._utc_now, budget._utc_now])
def test_default_clock_is_timezone_aware(clock: Callable[[], datetime]) -> None:
    now = clock()

    assert now.utcoffset() is not None
    month_window(now)  # 不丟例外
