"""worker 登記的工作。每個工作只呼叫一個 services 函式；參數只有 `*_id: UUID`（登記時檢查）。

新增工作：在 `radar/domain/jobs.py` 的 `JOBS` 加一列，再在這裡登記同名的工作
（架構測試檢查兩邊一致）。
"""

import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

import procrastinate

from radar.adapters.jobs import register_job
from radar.config import ConfigError, load_domain_config
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


class AdapterSetupError(Exception):
    """建 adapter 或讀領域設定失敗（`ConfigError` 以外）。訊息只有原本的錯誤類別。

    原例外的訊息可能帶設定值或網址（例如 botocore 的 `Invalid endpoint: <整個端點網址>`），
    Procrastinate 記失敗時會印出例外與它的 `__cause__`／`__context__`，所以不串原例外。
    """

    def __init__(self, error_type: str) -> None:
        self.error_type = error_type
        super().__init__(f"建立 adapter 失敗（{error_type}）")


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
        # 每失敗一次記一次，累計到上限就標 failed、不再補排（ADR-0028）。
        advance_weekly_update(
            SystemViewer(),
            parts.database,
            open_setup,
            load_scope,
            parts.send_heartbeat,
            weekly_run_id,
            max_failures=settings.weekly_update_max_failures,
        )

    def load_scope() -> ScopeSettings:
        # 執行時才讀 config/journals.yaml：抓完後才改設定也照新的追蹤範圍算 in_scope。
        return load_domain_config().journals.scope_settings()

    def enter_setup(stack: ExitStack) -> WeeklyFetchSetup:
        # 執行時才讀領域設定、建 OpenAlex 與物件儲存的 adapter：改 config/journals.yaml 不用重開
        # worker，沒有抓取工作時也不佔記憶體。
        domain_config = load_domain_config()
        store = build_object_store(settings)
        source = stack.enter_context(build_paper_source(settings, domain_config.openalex))
        return WeeklyFetchSetup(
            source=source,
            store=store,
            journals=domain_config.journals,
            exclusion_rules=domain_config.exclusion_rules,
            quota=domain_config.openalex.quota,
        )

    @contextmanager
    def open_setup() -> Iterator[WeeklyFetchSetup]:
        """`ConfigError`（訊息只列變數名稱與檔名）原樣往上丟；其他錯誤換成 `AdapterSetupError`。"""
        with ExitStack() as stack:
            setup: WeeklyFetchSetup | None = None
            error_type = ""
            try:
                setup = enter_setup(stack)
            except ConfigError:
                raise
            except Exception as exc:
                error_type = type(exc).__name__
            if setup is None:
                # 在 except 外面丟：`from None` 仍會留下 __context__，Procrastinate 照樣會印。
                raise AdapterSetupError(error_type)
            # 抓取時的錯誤不經過上面的轉換：由 service 處理、原樣往上丟。
            yield setup

    register_job(app, SWEEP, sweep_job, **retry)
    register_job(app, WEEKLY_UPDATE, weekly_update_job, **retry)
    register_job(app, RUN_WEEKLY_UPDATE, run_weekly_update_job, **retry)
