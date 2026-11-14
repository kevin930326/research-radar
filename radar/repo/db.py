"""資料庫連線。連線數預算（api 8、worker 6、CLI 2）在 `radar/config.py`，由入口傳進 `pool_size`。"""

from collections.abc import Iterator
from contextlib import contextmanager

import structlog
from sqlalchemy import Connection, Engine, create_engine, make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool

log = structlog.get_logger(__name__)


class Database:
    """包一個 SQLAlchemy Engine，連線池最多 `pool_size` 條、不另外多開（`max_overflow=0`）。"""

    def __init__(self, url: str, *, connect_timeout: int, pool_size: int = 1) -> None:
        """`connect_timeout`（秒）是 ping 各段期限的基準，見 `ping()`；
        也是池子滿了時等空出連線的上限（`pool_timeout`）。

        `pool_size` 預設 1 條；入口一律依 `radar/config.py` 的連線數預算傳入。
        """
        self._engine: Engine = create_engine(
            url,
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=0,
            pool_timeout=connect_timeout,
            # 借出前先確認連線還活著；資料庫重啟過也不會拿到斷掉的連線。
            pool_pre_ping=True,
            connect_args=_connect_args(url, connect_timeout),
        )

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """開一個交易：區塊正常結束就 commit，丟出例外就退回（Conventions「交易」）。

        只由入口直接呼叫的那個 services 函式開；`repo` 的函式收這個連線、不自己 commit。
        """
        with self._engine.begin() as connection:
            yield connection

    @staticmethod
    @contextmanager
    def savepoint(connection: Connection) -> Iterator[None]:
        """在呼叫端開好的交易裡開一個保存點（`SAVEPOINT`）。

        區塊正常結束就釋放保存點（改動留在交易裡）；丟出例外就退回到保存點、例外照樣往上丟，
        外面的交易繼續可用。退回時，保存點裡新增的列連同它們佔住的唯一值一起放掉：正在等這些值的
        其他交易會立刻繼續，不必等整個交易結束（`repo/papers.py` 靠這點固定鎖的順序，ADR-0020）。

        不開新交易、不 commit；交易仍由 `transaction()` 開關。
        """
        with connection.begin_nested():
            yield

    def ping(self) -> bool:
        """連得上並能執行 `SELECT 1` 回 True；連不上或查詢逾時回 False，不丟例外。

        各段的期限（T = connect_timeout 秒）：
        - 建立連線：最多 T 秒（`connect_timeout`）。
        - 連上後伺服器有回應但查詢很慢：資料庫端在 T 秒時取消（`statement_timeout`）。
        - 連上後網路斷掉或伺服器完全不回應：送出的資料 T 秒沒被確認就斷線
          （`tcp_user_timeout`，只在 Linux 有效，容器是 Linux；macOS 的 libpq 會忽略它）；
          另有 TCP keepalive 備援：閒置 T 秒後每秒探一次、T 次沒回應就斷，約 2T 秒。
        所以整體大約在 2T～3T 秒內結束（預設 T=3 → 約 6～9 秒）。
        斷線的連線會被連線池丟掉、不再借出；查詢被取消的連線退回交易後放回池子。

        日誌只記例外的類別名稱：例外訊息可能含主機與帳號，不記。
        """
        try:
            with self._engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            log.warning("資料庫連線失敗", error_type=type(exc).__name__)
            return False
        return True

    def schema_revisions(self) -> frozenset[str] | None:
        """資料庫目前的遷移版本；沒有 `alembic_version`（還沒遷移）時回 None。只讀，不建表。

        Raises:
            SQLAlchemyError: 連不上或查詢失敗。
        """
        with self._engine.connect() as connection:
            exists = connection.execute(
                text("SELECT to_regclass('alembic_version') IS NOT NULL")
            ).scalar_one()
            if not exists:
                return None
            rows = connection.execute(text("SELECT version_num FROM alembic_version"))
            return frozenset(str(value) for value in rows.scalars())


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
