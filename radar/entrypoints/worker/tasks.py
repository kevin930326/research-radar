"""worker 登記的工作。每個工作只呼叫一個 services 函式；參數只有 `*_id: UUID`（登記時檢查）。

新增工作：在 `radar/domain/jobs.py` 的 `JOBS` 加一列，再在這裡登記同名的工作
（架構測試檢查兩邊一致）。
"""

import procrastinate

from radar.adapters.jobs import register_job
from radar.domain.jobs import SWEEP
from radar.domain.settings import Settings
from radar.domain.sweep import SWEEP_REGISTRY
from radar.entrypoints.bootstrap import WorkerParts
from radar.services.sweep import sweep


def register_tasks(app: procrastinate.App, parts: WorkerParts, settings: Settings) -> None:
    """把全部工作登記到 `app`。不註冊任何 Procrastinate 週期任務（排程只在 `deploy/crontab`）。"""
    retry = {"max_retries": settings.job_max_retries, "retry_wait": settings.job_retry_wait}

    def sweep_job() -> None:
        sweep(
            parts.database,
            SWEEP_REGISTRY,
            parts.enqueue,
            parts.retry_stalled,
            parts.send_heartbeat,
        )

    register_job(app, SWEEP, sweep_job, **retry)
