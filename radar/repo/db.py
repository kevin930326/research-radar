"""資料庫連線。連線數預算（api 8、worker 6、CLI 2）在 `radar/config.py`，由入口傳進 `pool_size`。"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

import structlog
from sqlalchemy import Connection, Engine, create_engine, make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool

log = structlog.get_logger(__name__)

# --- advisory lock 的鍵 -----------------------------------------------------------------------
# advisory lock：PostgreSQL 提供、用途由程式自己約定的鎖，用一個 bigint 當名字。整個資料庫
# 共用同一個鍵空間（Procrastinate 不用 advisory lock），新增的鎖不能和這裡的重複；
# 一律用單一 bigint 的版本，不用兩個 int 的版本。

# 原始資料與論文的寫入鎖（ADR-0026 取捨「reparse 與每週更新互斥」）：
# - `radar reparse` 整段持有獨佔的 session 級鎖（`Database.session_lock`）；拿不到就拒絕。
# - 每週更新在「開始抓取或套用前讀狀態」與「套用」的交易裡試拿共用的交易級鎖
#   （`Database.try_shared_transaction_lock`）；拿不到代表 reparse 正在跑，這次先不做。
# 共用鎖彼此不衝突（兩個套用照舊靠 `weekly_run` 那一列互斥），只和 reparse 的獨佔鎖衝突。
# 值是 ASCII「RRAW」（Research Radar raw）。
INGEST_WRITE_LOCK_KEY: Final = 0x5252_4157


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

    @contextmanager
    def session_lock(self, key: int) -> Iterator[bool]:
        """借一條專用連線，試拿 `key` 的獨佔 session 級 advisory lock（`pg_try_advisory_lock`，
        不等）；交出有沒有拿到。

        拿到時整個區塊都持有。離開區塊（正常或例外）時先在同一條連線上 `pg_advisory_unlock`，
        才把連線還給連線池；解鎖丟錯或回 false 時把這條連線作廢（關掉、不放回池子）：
        PostgreSQL 在連線結束時一定放掉它持有的 session 級鎖，所以鎖不會跟著連線留在池子裡。
        行程被殺掉時連線也會斷，鎖一樣放掉。解鎖失敗只記一筆警告，不蓋掉區塊裡的例外。

        專用連線用 AUTOCOMMIT：持有鎖的期間這條連線沒有開著的交易。它佔連線池一條，
        區塊裡再開 `transaction()` 要另一條（CLI 的預算是 2 條）。
        """
        with self._engine.connect() as connection:
            connection.execution_options(isolation_level="AUTOCOMMIT")
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                ).scalar_one()
            )
            if not acquired:
                yield False
                return
            try:
                yield True
            finally:
                _unlock_or_discard(connection, key)

    @staticmethod
    def try_shared_transaction_lock(connection: Connection, key: int) -> bool:
        """在呼叫端開好的交易裡試拿 `key` 的共用交易級 advisory lock
        （`pg_try_advisory_xact_lock_shared`，不等）；回有沒有拿到。

        交易結束（commit 或退回）時 PostgreSQL 自動放掉，沒有要手動解鎖的。共用鎖彼此不衝突，
        只和別人持有的獨佔鎖（`session_lock`）衝突。不開新交易、不 commit。
        """
        return bool(
            connection.execute(
                text("SELECT pg_try_advisory_xact_lock_shared(:key)"), {"key": key}
            ).scalar_one()
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


def _unlock_or_discard(connection: Connection, key: int) -> None:
    """解開 `session_lock` 拿的鎖；解不開（丟錯或回 false）就作廢這條連線，讓連線結束時放掉鎖。"""
    try:
        released = bool(
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key}).scalar_one()
        )
    except SQLAlchemyError as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.warning("advisory lock 解鎖失敗，關掉這條連線", error_type=type(exc).__name__)
        released = False
    else:
        if not released:
            log.warning("advisory lock 已經不在這條連線上，關掉這條連線")
    if not released:
        connection.invalidate()


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
