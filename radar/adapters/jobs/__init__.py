"""背景工作佇列（Procrastinate）的 adapter：排入、登記工作、重試卡住的工作（AD-14、ADR-0019）。

`procrastinate` 只能出現在這裡與 `radar.entrypoints.worker`（import-linter 契約）。
services 不 import 這裡，排入工作的函式由 `bootstrap.py` 組好再注入（`radar.domain.jobs.Enqueue`）。
"""

import functools
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, LiteralString

import procrastinate
import psycopg_pool
import structlog
from procrastinate import exceptions as procrastinate_exceptions
from procrastinate import jobs as procrastinate_jobs
from procrastinate import manager as procrastinate_manager
from procrastinate import sql as procrastinate_sql
from procrastinate.job_context import JobContext
from procrastinate.worker import Worker

from radar.domain.jobs import PDF_LOCK, JobSpec, Queue, check_job_ids, is_id_parameter
from radar.domain.jobs import queueing_lock as make_queueing_lock

log = structlog.get_logger(__name__)


def _json_default(value: object) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"工作參數不能放 {type(value).__name__}")


def dumps_job_args(value: object) -> str:
    """寫進佇列的 JSON：UUID 轉成字串，其他不認得的型別直接拒絕。"""
    return json.dumps(value, default=_json_default)


# 設定允許 SQLAlchemy 寫法的 `postgresql+psycopg://`，但 libpq 只認 `postgresql://`。
_SQLALCHEMY_SCHEME = "postgresql+psycopg://"


def libpq_conninfo(url: str) -> str:
    """把 `postgresql+psycopg://` 開頭換成 libpq 認得的 `postgresql://`；其他照原樣。"""
    if url.startswith(_SQLALCHEMY_SCHEME):
        return "postgresql://" + url.removeprefix(_SQLALCHEMY_SCHEME)
    return url


def _make_pool_args(url: str, max_connections: int, connect_timeout: int) -> dict[str, Any]:
    """佇列連線池（psycopg_pool）的參數：最少 1 條、最多 `max_connections` 條。"""
    return {
        "conninfo": libpq_conninfo(url),
        "min_size": 1,
        "max_size": max_connections,
        # 池子滿了時等空出連線的上限。
        "timeout": float(connect_timeout),
        "kwargs": {"connect_timeout": connect_timeout},
        "name": "radar-jobs",
    }


def _connector(pool_args: dict[str, Any]) -> procrastinate.PsycopgConnector:
    return procrastinate.PsycopgConnector(json_dumps=dumps_job_args, **pool_args)


@dataclass(frozen=True)
class JobApp:
    """Procrastinate 的 App，加上開連線池的參數。

    連線池由這裡自己開（不經 Procrastinate）：連不上時在 `connect_timeout` 秒內失敗，
    而不是 Procrastinate 預設的 30 秒。每次開都換一個新的 connector，worker 重開時不會沿用
    已經關掉的連線池。
    """

    app: procrastinate.App
    url: str = field(repr=False)
    max_connections: int
    connect_timeout: int

    def _pool_args(self) -> dict[str, Any]:
        return _make_pool_args(self.url, self.max_connections, self.connect_timeout)

    @contextmanager
    def opened(self) -> Iterator[procrastinate.App]:
        """同步開連線池（CLI 用）；離開區塊時關掉。

        Raises:
            psycopg_pool.PoolTimeout: `connect_timeout` 秒內連不上。
        """
        pool = psycopg_pool.ConnectionPool(
            **self._pool_args(), open=False, check=psycopg_pool.ConnectionPool.check_connection
        )
        try:
            pool.open(wait=True, timeout=float(self.connect_timeout))
            with self.app.replace_connector(_connector(self._pool_args())) as app:
                app.open(pool)
                try:
                    yield app
                finally:
                    app.close()
        finally:
            pool.close()

    @asynccontextmanager
    async def opened_async(self) -> AsyncIterator[procrastinate.App]:
        """非同步開連線池（worker 用）；離開區塊時關掉。

        Raises:
            psycopg_pool.PoolTimeout: `connect_timeout` 秒內連不上。
        """
        pool = psycopg_pool.AsyncConnectionPool(
            **self._pool_args(),
            open=False,
            check=psycopg_pool.AsyncConnectionPool.check_connection,
        )
        try:
            await pool.open(wait=True, timeout=float(self.connect_timeout))
            with self.app.replace_connector(_connector(self._pool_args())) as app:
                await app.open_async(pool)
                try:
                    yield app
                finally:
                    await app.close_async()
        finally:
            await pool.close()


def create_job_app(url: str, *, max_connections: int, connect_timeout: int) -> JobApp:
    """建一個還沒連線的佇列 App。連線池最少 1 條、最多 `max_connections` 條。

    沒有任何 Procrastinate 週期任務（AD-15：排程只在 `deploy/crontab`）。
    JobManager 換成 `RadarJobManager`（重試撞到排隊鎖時把那筆標成 failed）。
    """
    connector = _connector(_make_pool_args(url, max_connections, connect_timeout))
    app = procrastinate.App(connector=connector)
    # 換掉 Procrastinate 的 JobManager：工作失敗重試撞到排隊鎖時，那筆要確實結束。
    # 要在登記任何工作之前換（worker 結束工作時用的是 app.job_manager）。
    app.job_manager = RadarJobManager(connector=connector)
    return JobApp(
        app=app,
        url=url,
        max_connections=max_connections,
        connect_timeout=connect_timeout,
    )


def defer_job(app: procrastinate.App, job: JobSpec, ids: Mapping[str, uuid.UUID]) -> bool:
    """排入一個工作；回 True＝新排入，False＝同一件事已經在排隊（同一個排隊鎖），沒有再排。

    `pdf` 佇列的工作一律帶同一把鎖（同時只跑一個）。UUID 轉成字串存進 JSON。

    Raises:
        ValueError: 參數不是 `*_id`／UUID。
    """
    check_job_ids(ids)
    deferrer = app.configure_task(
        job.name,
        allow_unknown=True,
        queue=job.queue.value,
        lock=PDF_LOCK if job.queue is Queue.PDF else None,
        queueing_lock=make_queueing_lock(job, ids),
    )
    try:
        job_id = deferrer.defer(**{key: str(value) for key, value in ids.items()})
    except procrastinate_exceptions.AlreadyEnqueued:
        log.info("工作已經在排隊，沒有再排", job_name=job.name, queue=job.queue.value)
        return False
    log.info("已排入工作", job_name=job.name, queue=job.queue.value, queued_job_id=job_id)
    return True


# plpgsql 的 RAISE（SQLSTATE P0001）：Procrastinate 的函式在工作已經不是「執行中」時這樣回報。
_PLPGSQL_RAISE = "P0001"


def _is_job_state_raise(error: procrastinate_exceptions.ConnectorException) -> bool:
    """這個資料庫錯誤是不是 Procrastinate 的函式說「工作狀態已經變了」（而不是連線斷掉等）。"""
    return getattr(error.__cause__, "sqlstate", None) == _PLPGSQL_RAISE


# 「這筆工作現在還是卡住的嗎」：鎖住工作列（FOR UPDATE）後重新核對，和接下來的改動在同一個
# SQL 敘述（同一個交易）裡，核對和改動之間插不進別人的寫入。條件全部成立才算：
# - 還在執行中，attempts 和查出來時一樣（被重排或重試過的，attempts 已經加一）；
# - 還掛在查出來時那個 worker 底下，或 worker 列已經被刪掉（worker_id 變成 NULL）；
# - 掛的 worker 不是活著的（找不到 `seconds_since_heartbeat` 秒內報到過的那一列）。
# 健康的 worker 接手時 worker_id 會換成它自己、attempts 也不同，所以舊的候選清單動不到它。
# 兩輪 sweep 同時處理同一筆時，後到的會等前一個交易結束再重新核對（READ COMMITTED 下
# FOR UPDATE 的行為）：工作列自己的欄位（status、attempts、worker_id）用最新的那一版，
# 所以只有一個改得到它；但 NOT EXISTS 的心跳核對讀的仍是這個敘述開始時的
# procrastinate_workers 快照。這個時間差和「用心跳判斷失聯」本來就有的時間差同類，不另外處理。
# Procrastinate 的函式名稱（retry_job_v2、finish_job_v1）跟著 3.11.0 的凍結 SQL；
# 升版時凍結 SQL 的 arch 測試與比對函式名稱的單元測試會失敗，提醒回來核對。
_LOCK_IF_STILL_STALLED: LiteralString = """
WITH still_stalled AS (
    SELECT job.id
      FROM procrastinate_jobs job
     WHERE job.id = %(job_id)s
       AND job.status = 'doing'
       AND job.attempts = %(attempts)s
       AND (job.worker_id IS NULL OR job.worker_id = %(worker_id)s)
       AND NOT EXISTS (
           SELECT 1
             FROM procrastinate_workers worker
            WHERE worker.id = job.worker_id
              AND worker.last_heartbeat >= now() - %(seconds_since_heartbeat)s * INTERVAL '1 second'
       )
       FOR UPDATE OF job
)
"""
RETRY_IF_STILL_STALLED: LiteralString = (
    _LOCK_IF_STILL_STALLED
    + "SELECT procrastinate_retry_job_v2(id, now(), NULL, NULL, NULL) FROM still_stalled"
)
FAIL_IF_STILL_STALLED: LiteralString = (
    _LOCK_IF_STILL_STALLED
    + "SELECT procrastinate_finish_job_v1(id, 'failed'::procrastinate_job_status, false)"
    + " FROM still_stalled"
)


def _if_still_stalled(connector: Any, query: LiteralString, guard: Mapping[str, Any]) -> bool:
    """核對後才改；回 True＝核對通過、已經改了，False＝已經不是卡住的狀態，什麼都沒動。"""
    return bool(connector.execute_query_all(query=query, **guard))


def retry_stalled_jobs(
    app: procrastinate.App, *, seconds_since_heartbeat: int, max_retries: int
) -> int:
    """把「執行中」、但 worker 已經超過 `seconds_since_heartbeat` 秒沒報到的工作改回排隊
    （attempts 加一），回傳重試了幾個。

    - 查出來的清單只是候選：每一筆在改之前都鎖住重新核對（`_LOCK_IF_STILL_STALLED`），
      已經被重排、被健康的 worker 接手或已經做完的，跳過、不動它。
    - 卡住的重試和工作失敗的重試算同一個上限：attempts 已經到 `max_retries` 的，改成 failed，
      不再排（每次都把 worker 弄掛的工作不會每 15 分鐘重來一次、一直佔著 `pdf` 鎖）。
    - 同一件事已經有一筆在排隊（排隊鎖相同）時，卡住的那筆改成 failed，不重複排。
    - Procrastinate 的函式回報工作狀態已經變了（plpgsql RAISE）：跳過這筆，繼續下一筆。
      其他資料庫錯誤（例如連線斷了）照樣往上丟。
    """
    connector = app.connector.get_sync_connector()
    rows = connector.execute_query_all(
        query=procrastinate_sql.queries["select_stalled_jobs_by_heartbeat"],
        queue=None,
        task_name=None,
        seconds_since_heartbeat=seconds_since_heartbeat,
    )
    retried = 0
    for row in rows:
        stalled = {
            "stalled_job_id": row["id"],
            "job_name": row["task_name"],
            "attempts": row["attempts"],
        }
        guard = {
            "job_id": row["id"],
            "attempts": row["attempts"],
            "worker_id": row["worker_id"],
            "seconds_since_heartbeat": seconds_since_heartbeat,
        }
        try:
            if row["attempts"] >= max_retries:
                if _if_still_stalled(connector, FAIL_IF_STILL_STALLED, guard):
                    log.warning("卡住的工作已經用完重試次數，標成失敗", **stalled)
                else:
                    log.warning("卡住的工作已經不是卡住的狀態，跳過", **stalled)
                continue
            try:
                done = _if_still_stalled(connector, RETRY_IF_STILL_STALLED, guard)
            except procrastinate_exceptions.UniqueViolation:
                if _if_still_stalled(connector, FAIL_IF_STILL_STALLED, guard):
                    log.warning("卡住的工作已經有同一件事在排隊，這筆標成失敗", **stalled)
                else:
                    log.warning("卡住的工作已經不是卡住的狀態，跳過", **stalled)
                continue
        except procrastinate_exceptions.ConnectorException as exc:
            if not _is_job_state_raise(exc):
                raise
            log.warning(
                "卡住的工作狀態已經變了，跳過",
                stalled_job_id=row["id"],
                error_type=type(exc.__cause__).__name__,
            )
            continue
        if not done:
            # 例如：上一輪 sweep 已經重排、健康的 worker 接手了，或原本的 worker 晚一步做完。
            log.warning("卡住的工作已經不是卡住的狀態，跳過", **stalled)
            continue
        retried += 1
        log.warning("重試卡住的工作（worker 已經不在）", **stalled)
    return retried


class RadarJobManager(procrastinate_manager.JobManager):
    """Procrastinate 的 JobManager，只多處理一件事：工作失敗要重試、卻撞到排隊鎖。

    工作失敗、要照重試策略改回排隊時，同一件事可能已經有一筆在排隊（同一個排隊鎖，例如並行數 1
    時 sweep 還在跑、下一個 cron 的 sweep 已經排入）。Procrastinate 這時丟 `UniqueViolation`，
    只結束那個 task，工作留在「執行中」；它的 worker 還活著、有報到，sweep 也不會回收它。
    這裡改成把這筆標成 failed 並記警告：同一件事已經在排隊，由排隊的那筆去做。
    其他唯一鍵衝突照樣往上丟。
    """

    async def retry_job(
        self,
        job: procrastinate_jobs.Job,
        retry_at: datetime | None = None,
        priority: int | None = None,
        queue: str | None = None,
        lock: str | None = None,
    ) -> None:
        try:
            await super().retry_job(
                job, retry_at=retry_at, priority=priority, queue=queue, lock=lock
            )
        except procrastinate_exceptions.UniqueViolation as exc:
            if exc.queueing_lock is None:
                raise
            await self.finish_job(job, status=procrastinate_jobs.Status.FAILED, delete_job=False)
            log.warning(
                "工作失敗要重試，但同一件事已經在排隊；這筆標成失敗，不再重試",
                failed_job_id=job.id,
                job_name=job.task_name,
                queueing_lock=exc.queueing_lock,
            )


class InvalidJobSignatureError(TypeError):
    """工作函式的參數不是「只有 ID」。"""


def check_job_signature(func: Callable[..., object]) -> tuple[str, ...]:
    """工作函式的參數只能是 `*_id: uuid.UUID`（沒有預設值、沒有 *args／**kwargs），回傳參數名稱。

    Raises:
        InvalidJobSignatureError: 有不符的參數。
    """
    names: list[str] = []
    for parameter in inspect.signature(func, eval_str=True).parameters.values():
        where = f"{getattr(func, '__name__', func)!r} 的參數 {parameter.name}"
        if parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
            raise InvalidJobSignatureError(f"{where}：只能是一般的具名參數")
        if not is_id_parameter(parameter.name):
            raise InvalidJobSignatureError(f"{where}：名稱要是 *_id")
        if parameter.annotation is not uuid.UUID:
            raise InvalidJobSignatureError(f"{where}：型別要是 uuid.UUID")
        if parameter.default is not parameter.empty:
            raise InvalidJobSignatureError(f"{where}：不能有預設值")
        names.append(parameter.name)
    return tuple(names)


def register_job(
    app: procrastinate.App,
    job: JobSpec,
    func: Callable[..., None],
    *,
    max_retries: int,
    retry_wait: int,
) -> None:
    """把工作函式登記到 `job` 的佇列。登記時檢查參數只有 `*_id: UUID`，不符就丟錯。

    執行前把佇列裡的字串轉回 UUID；轉不了（格式不對）就讓這次執行失敗。
    工作層的重試：最多 `max_retries` 次，第 n 次前等 n × `retry_wait` 秒（ADR-0004 決定第 7 點）。

    Raises:
        InvalidJobSignatureError: 參數不是只有 `*_id: uuid.UUID`。
    """
    parameters = check_job_signature(func)

    @functools.wraps(func)
    def run(**kwargs: object) -> None:
        if set(kwargs) != set(parameters):
            raise TypeError(f"工作 {job.name} 的參數不對：{sorted(kwargs)}")
        func(**{name: uuid.UUID(str(kwargs[name])) for name in parameters})

    retry: procrastinate.RetryStrategy | bool = (
        procrastinate.RetryStrategy(
            max_attempts=max_retries, wait=retry_wait, linear_wait=retry_wait
        )
        if max_retries > 0
        else False
    )
    app.task(name=job.name, queue=job.queue.value, retry=retry)(run)


async def job_id_middleware(
    call_next: Callable[[], Awaitable[Any]], context: JobContext, _worker: Worker
) -> Any:
    """工作執行期間的每筆日誌都帶 `job_id`（Conventions「日誌」）。

    同步的工作在另一個執行緒跑；Procrastinate 會把這裡設的 contextvars 一起帶過去。
    """
    with structlog.contextvars.bound_contextvars(job_id=context.job.id):
        return await call_next()
