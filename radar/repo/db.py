"""資料庫連線。連線數預算（api 8、worker 6、CLI 2）留給 Story 1.3。"""

import structlog
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

log = structlog.get_logger(__name__)


class Database:
    """包一個 SQLAlchemy Engine。目前用 NullPool：每次用完就關，不留閒置連線。"""

    def __init__(self, url: str, *, connect_timeout: int) -> None:
        """`connect_timeout`（秒）是 ping 各段期限的基準，見 `ping()`。"""
        self._engine: Engine = create_engine(
            url,
            poolclass=NullPool,
            connect_args=_connect_args(url, connect_timeout),
        )

    def ping(self) -> bool:
        """連得上並能執行 `SELECT 1` 回 True；連不上或查詢逾時回 False，不丟例外。

        各段的期限（T = connect_timeout 秒）：
        - 建立連線：最多 T 秒（`connect_timeout`）。
        - 連上後伺服器有回應但查詢很慢：資料庫端在 T 秒時取消（`statement_timeout`）。
        - 連上後網路斷掉或伺服器完全不回應：送出的資料 T 秒沒被確認就斷線
          （`tcp_user_timeout`，只在 Linux 有效，容器是 Linux；macOS 的 libpq 會忽略它）；
          另有 TCP keepalive 備援：閒置 T 秒後每秒探一次、T 次沒回應就斷，約 2T 秒。
        所以整體大約在 2T～3T 秒內結束（預設 T=3 → 約 6～9 秒）。
        NullPool：離開 `with` 時連線一定關掉，逾時的那條也一樣。

        日誌只記例外的類別名稱：例外訊息可能含主機與帳號，不記。
        """
        try:
            with self._engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            log.warning("資料庫連線失敗", error_type=type(exc).__name__)
            return False
        return True


def _connect_args(url: str, connect_timeout: int) -> dict[str, object]:
    """給驅動的連線參數。網址原本帶的 `options` 保留，再加上 statement_timeout。"""
    timeout_ms = connect_timeout * 1000
    existing = make_url(url).query.get("options")
    if isinstance(existing, tuple):
        existing = " ".join(existing)
    options = f"-c statement_timeout={timeout_ms}"
    if existing:
        options = f"{existing} {options}"
    return {
        "connect_timeout": connect_timeout,
        "options": options,
        "tcp_user_timeout": timeout_ms,
        "keepalives": 1,
        "keepalives_idle": connect_timeout,
        "keepalives_interval": 1,
        "keepalives_count": connect_timeout,
    }
