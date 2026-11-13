"""組裝：唯一建立 adapter 與資料庫連線的地方（AD-1）。"""

from collections.abc import Callable

from radar.domain.settings import Settings
from radar.repo.db import Database


def build_database_ping(settings: Settings) -> Callable[[], bool]:
    """建立資料庫連線物件，回傳它的 `ping`，給就緒檢查用。"""
    database = Database(
        settings.database_url.get_secret_value(),
        connect_timeout=settings.database_connect_timeout,
    )
    return database.ping
