"""排入排程工作：`radar enqueue <排程名稱>` 呼叫（AD-15：排程只由外部 cron 觸發）。"""

import structlog

from radar.domain.jobs import Enqueue, find_scheduled_job

log = structlog.get_logger(__name__)


def enqueue_scheduled_job(schedule: str, enqueue: Enqueue) -> bool:
    """把排程名稱對應的工作排進它的佇列；回 True＝新排入，False＝已經在排隊。

    Raises:
        UnknownJobError: 沒有這個排程（不會呼叫 `enqueue`）。
    """
    job = find_scheduled_job(schedule)
    queued = enqueue(job, {})
    log.info(
        "排程工作已排入" if queued else "排程工作已經在排隊",
        schedule=schedule,
        job_name=job.name,
        queue=job.queue.value,
    )
    return queued
