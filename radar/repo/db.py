"""資料庫連線。連線數預算（api 8、worker 6、CLI 2）留給 Story 1.3。"""

import structlog
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

log = structlog.get_logger(__name__)


class Database:
    """包一個 SQLAlchemy Engine。目前用 NullPool：每次用完就關，不留閒置連線。"""

    def __init__(self, url: str, *, connect_timeout: int) -> None:
        self._engine: Engine = create_engine(
            url,
            poolclass=NullPool,
            connect_args={"connect_timeout": connect_timeout},
        )

    def ping(self) -> bool:
        """連得上並能執行 `SELECT 1` 回 True；連不上回 False，不丟例外。

        日誌只記例外的類別名稱：例外訊息可能含主機與帳號，不記。
        """
        try:
            with self._engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            log.warning("資料庫連線失敗", error_type=type(exc).__name__)
            return False
        return True
