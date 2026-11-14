"""worker 登記的工作。每個工作只呼叫一個 services 函式；參數只有 `*_id: UUID`（登記時檢查）。

新增工作：在 `radar/domain/jobs.py` 的 `JOBS` 加一列，再在這裡登記同名的工作
（架構測試檢查兩邊一致）。
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import procrastinate

from radar.adapters.jobs import register_job
from radar.config import load_domain_config
from radar.domain.jobs import RUN_WEEKLY_UPDATE, SWEEP, WEEKLY_UPDATE
from radar.domain.scope import ScopeSettings
from radar.domain.settings import Settings
from radar.domain.sweep import SWEEP_REGISTRY
from radar.domain.viewer import SystemViewer
from radar.entrypoints.bootstrap import WorkerParts, build_object_store, build_paper_source
from radar.services.sweep import sweep
from radar.services.weekly_update import (
    WeeklyFetchSetup,
    advance_weekly_update,
    weekly_update,
)


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

    def weekly_update_job() -> None:
        # 心跳在 run_weekly_update 發佈快照、commit 之後才送（ADR-0026），這裡不送。
        weekly_update(SystemViewer(), parts.database, parts.enqueue)

    def run_weekly_update_job(weekly_run_id: uuid.UUID) -> None:
        # service 先讀狀態，確認要抓才呼叫 open_setup：已經抓完、不是 fetching 時不讀設定、不建
        # adapter；建不起來（缺金鑰、設定壞掉）時 service 也會更新 updated_at（ADR-0025）。
        # 抓完了才呼叫 load_scope 讀追蹤範圍，接著套用、發佈快照，commit 後送心跳（ADR-0026）。
        advance_weekly_update(
            SystemViewer(),
            parts.database,
            open_setup,
            load_scope,
            parts.send_heartbeat,
            weekly_run_id,
        )

    def load_scope() -> ScopeSettings:
        # 執行時才讀 config/journals.yaml：抓完後才改設定也照新的追蹤範圍算 in_scope。
        return load_domain_config().journals.scope_settings()

    @contextmanager
    def open_setup() -> Iterator[WeeklyFetchSetup]:
        # 執行時才讀領域設定、建 OpenAlex 與物件儲存的 adapter：改 config/journals.yaml 不用重開
        # worker，沒有抓取工作時也不佔記憶體。
        domain_config = load_domain_config()
        store = build_object_store(settings)
        with build_paper_source(settings, domain_config.openalex) as source:
            yield WeeklyFetchSetup(
                source=source,
                store=store,
                journals=domain_config.journals,
                exclusion_rules=domain_config.exclusion_rules,
                quota=domain_config.openalex.quota,
            )

    register_job(app, SWEEP, sweep_job, **retry)
    register_job(app, WEEKLY_UPDATE, weekly_update_job, **retry)
    register_job(app, RUN_WEEKLY_UPDATE, run_weekly_update_job, **retry)
