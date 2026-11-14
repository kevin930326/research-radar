"""`sweep`：把卡住的狀態列與卡住的工作重新排入（AD-14、ADR-0004 決定第 9 點）。

由 cron 每 15 分鐘 `radar enqueue sweep` 觸發，在 worker 裡執行；成功才送 `sweep` 心跳。
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import structlog

from radar.domain.jobs import SWEEP, Enqueue
from radar.domain.sweep import SWEEP_BATCH_LIMIT, SweepEntry
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.sweep import find_stuck_ids
from radar.services.scheduled import run_scheduled_job

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SweepResult:
    """這一輪找到幾列卡住的狀態列、其中新排入幾個（其餘已經在排隊），以及重試了幾個卡住的工作。"""

    stuck_rows: int
    requeued: int
    retried_jobs: int


def sweep(
    database: Database,
    registry: Sequence[SweepEntry],
    enqueue: Enqueue,
    retry_stalled: Callable[[], int],
    send_heartbeat: Callable[[str], None],
) -> SweepResult:
    """掃一輪：

    1. 在一個交易裡讀出每張登記表卡住的列，結束交易（不在開著的交易裡排工作）。
    2. 逐列排入登記的工作，參數是 `<表名>_id`；已經在排隊的不會多一筆。
    3. 重試 worker 已經不在、卻還停在執行中的工作（`retry_stalled`）。

    全部成功才送 `sweep` 的心跳；任何一步丟出例外就原樣往上丟、不送。
    """
    return run_scheduled_job(
        SWEEP.check,
        lambda: _sweep_once(database, registry, enqueue, retry_stalled),
        send_heartbeat,
    )


def _sweep_once(
    database: Database,
    registry: Sequence[SweepEntry],
    enqueue: Enqueue,
    retry_stalled: Callable[[], int],
) -> SweepResult:
    stuck: list[tuple[SweepEntry, list[uuid.UUID]]] = []
    if registry:
        with database.transaction() as connection:
            viewer = SystemViewer()
            stuck = [
                (entry, find_stuck_ids(viewer, connection, entry, SWEEP_BATCH_LIMIT))
                for entry in registry
            ]

    stuck_rows = requeued = 0
    for entry, ids in stuck:
        for row_id in ids:
            stuck_rows += 1
            if enqueue(entry.job, {entry.id_parameter: row_id}):
                requeued += 1
        if ids:
            log.warning(
                "狀態列卡住，重新排入", table=entry.table, job_name=entry.job.name, rows=len(ids)
            )

    retried = retry_stalled()
    result = SweepResult(stuck_rows=stuck_rows, requeued=requeued, retried_jobs=retried)
    log.info(
        "sweep 完成",
        stuck_rows=result.stuck_rows,
        requeued=result.requeued,
        retried_jobs=result.retried_jobs,
    )
    return result
