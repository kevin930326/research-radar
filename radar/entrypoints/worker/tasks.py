"""worker 登記的工作。每個工作只呼叫一個 services 函式；參數只有 `*_id: UUID`（登記時檢查）。

新增工作：在 `radar/domain/jobs.py` 的 `JOBS` 加一列，再在這裡登記同名的工作
（架構測試檢查兩邊一致）。
"""

import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial

import procrastinate

from radar.adapters.jobs import register_job
from radar.config import (
    ConfigError,
    load_budget_config,
    load_context_values,
    load_domain_config,
    load_embedding_config,
    load_llm_batch_config,
    load_models_config,
    load_taxonomy,
    load_zh_summary_rules,
)
from radar.domain.ai_outputs import CallMode
from radar.domain.jobs import (
    EMBED_PAPERS,
    PLAN_LLM_BATCHES,
    POLL_BATCHES,
    RUN_WEEKLY_UPDATE,
    SUBMIT_LLM_BATCH,
    SWEEP,
    WEEKLY_UPDATE,
)
from radar.domain.scope import ScopeSettings
from radar.domain.settings import Settings
from radar.domain.sweep import SWEEP_REGISTRY
from radar.domain.viewer import SystemViewer
from radar.entrypoints.bootstrap import (
    WorkerParts,
    build_embedding_provider,
    build_gateway_config,
    build_llm_provider,
    build_object_store,
    build_paper_source,
    code_version,
)
from radar.ports import LlmProvider
from radar.services.embeddings import embed_papers
from radar.services.llm_batches import (
    LlmBatchSetup,
    plan_llm_batches,
    poll_llm_batches,
    submit_llm_batch,
)
from radar.services.llm_gateway import expire_stale_runs
from radar.services.scheduled import run_checked_scheduled_job
from radar.services.sweep import sweep
from radar.services.weekly_update import (
    WeeklyFetchSetup,
    advance_weekly_update,
    weekly_update,
)


class EmbeddingJobFailedError(Exception):
    """向量化這次有一批失敗或有論文標成失敗（原因代碼已記在 `derivation_run` 與日誌）。
    讓工作層記成失敗（不重試）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"向量化失敗（{reason}）")


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
        # 互動 LLM 呼叫超過 RADAR_LLM_RUN_STALE_AFTER 秒還是 running 的，改成 failed（ADR-0030）。
        sweep(
            parts.database,
            SWEEP_REGISTRY,
            parts.enqueue,
            parts.retry_stalled,
            parts.send_heartbeat,
            expire_runs=partial(
                expire_stale_runs,
                SystemViewer(),
                parts.database,
                timedelta(seconds=settings.llm_run_stale_after),
            ),
        )

    def weekly_update_job() -> None:
        # 心跳在 run_weekly_update 發佈快照、commit 之後才送（ADR-0026），這裡不送。
        # 找到的 fetching 已到失敗上限（上次收尾沒做成）時先收尾再建新的一筆（ADR-0028）。
        weekly_update(
            SystemViewer(),
            parts.database,
            parts.enqueue,
            max_failures=settings.weekly_update_max_failures,
        )

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
            enqueue=parts.enqueue,
        )

    def embed_papers_job() -> None:
        # 每週更新發佈快照後排入（Story 2.8）。執行時才讀 config/embeddings.yaml、建 Workers AI 的
        # adapter：沒有向量化工作時不佔記憶體。到今天的額度比例就停下、正常結束（不算失敗），
        # 下一次排入或 `radar embeddings backfill` 接著做。失敗時工作記成失敗、不重試（見登記處）。
        config = load_embedding_config()
        error_type: str | None = None
        try:
            provider = build_embedding_provider(settings, config)
        except ConfigError:
            raise
        except Exception as exc:
            error_type = type(exc).__name__
        if error_type is not None:
            # 在 except 外面丟，不串原例外（見 AdapterSetupError）。
            raise AdapterSetupError(error_type)
        with provider:
            result = embed_papers(
                SystemViewer(),
                parts.database,
                provider,
                config,
                settings.cloud_policy,
                code_version=code_version(),
                clock=lambda: datetime.now(UTC),
            )
        if result.has_failures:
            # 單篇被拒時其他論文照做、停下的原因可能是 completed，仍記成失敗（ADR-0040）。
            reason = "" if result.failure_reason is None else result.failure_reason.value
            raise EmbeddingJobFailedError(reason)

    def load_batch_setup() -> LlmBatchSetup:
        # 執行時才讀 AI 設定（AD-18；啟動時 check_ai_config 已檢查過一次）：改設定不用重開 worker。
        return LlmBatchSetup(
            gateway=build_gateway_config(settings, load_models_config(), call_mode=CallMode.BATCH),
            budget=load_budget_config(),
            batches=load_llm_batch_config(),
            zh_summary_rules=load_zh_summary_rules(),
            taxonomy=load_taxonomy(),
            context_values=load_context_values(),
        )

    def plan_llm_batches_job(weekly_run_id: uuid.UUID) -> None:
        # 每週更新發佈快照後排入（Story 2.9、ADR-0041）。只排批次、不呼叫供應商（不建 provider）；
        # 每建好一批排入 submit_llm_batch。設定沒開、月初窗口、熔斷都正常結束（不算失敗）。
        plan_llm_batches(
            SystemViewer(),
            parts.database,
            load_batch_setup(),
            weekly_run_id=weekly_run_id,
            clock=lambda: datetime.now(UTC),
            enqueue=parts.enqueue,
        )

    def submit_llm_batch_job(batch_submission_id: uuid.UUID) -> None:
        # 送出一個 queued 的批次（Story 2.9、ADR-0041）。provider 由 service 在確定要送出時才建
        # （open_provider）：過期的 submitting 標 needs_attention、已結束、延後這些只碰資料庫的路
        # 不建，所以缺金鑰時也做得完。真的要送才建：缺金鑰的 ConfigError 原樣丟出，列維持 queued，
        # sweep 1 小時後補排；不確定有沒有建好時丟 BatchSubmitUncertainError。送完關掉 provider。
        setup = load_batch_setup()
        with ExitStack() as stack:

            def open_provider() -> LlmProvider:
                error_type: str | None = None
                try:
                    provider = build_llm_provider(settings)
                except ConfigError:
                    raise
                except Exception as exc:
                    error_type = type(exc).__name__
                if error_type is not None:
                    # 在 except 外面丟，不串原例外（見 AdapterSetupError）。
                    raise AdapterSetupError(error_type)
                return stack.enter_context(provider)

            submit_llm_batch(
                SystemViewer(),
                parts.database,
                open_provider,
                setup,
                batch_submission_id,
                clock=lambda: datetime.now(UTC),
            )

    def open_llm_provider(stack: ExitStack) -> LlmProvider:
        """建 Anthropic 的 provider 並交給 `stack` 關。`ConfigError`（缺金鑰；訊息只列變數名稱）
        原樣往上丟；其他錯誤換成 `AdapterSetupError`（不串原例外）。"""
        error_type: str | None = None
        try:
            provider = build_llm_provider(settings)
        except ConfigError:
            raise
        except Exception as exc:
            error_type = type(exc).__name__
        if error_type is not None:
            raise AdapterSetupError(error_type)
        return stack.enter_context(provider)

    def poll_batches_job() -> None:
        # cron 每 15 分鐘排入（Story 2.11、ADR-0043）。查 submitted／ended 的批次、匯入已結束的；
        # provider 只在有要查的批次時才建（open_provider），沒有批次時缺金鑰也做得完。沒有錯誤、
        # 也沒有 needs_attention 才送 poll-batches 心跳（不健康時 service 已記錯誤日誌）。不做工作層
        # 的重試：供應商的暫時性錯誤 adapter 已重試過，下一次 cron 會接著做（匯入是冪等的）。
        setup = load_batch_setup()
        with ExitStack() as stack:
            run_checked_scheduled_job(
                POLL_BATCHES.check,
                partial(
                    poll_llm_batches,
                    SystemViewer(),
                    parts.database,
                    partial(open_llm_provider, stack),
                    setup,
                    clock=lambda: datetime.now(UTC),
                ),
                lambda result: result.healthy,
                parts.send_heartbeat,
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
    # 向量化不做工作層的重試：暫時性錯誤 adapter 已經重試過，設定錯（缺 token、401）重跑也一樣；
    # 停下的部分由下一次排入或 `radar embeddings backfill` 接著做（用輸入指紋挑，不會漏）。
    register_job(
        app, EMBED_PAPERS, embed_papers_job, max_retries=0, retry_wait=settings.job_retry_wait
    )
    register_job(app, PLAN_LLM_BATCHES, plan_llm_batches_job, **retry)
    # 送出不做工作層的重試：batch create 沒有冪等鍵，不確定有沒有建好時重送可能付兩次錢
    # （ADR-0041）；429 退回 queued、設定錯留在 queued，都由 sweep 補排。
    register_job(
        app,
        SUBMIT_LLM_BATCH,
        submit_llm_batch_job,
        max_retries=0,
        retry_wait=settings.job_retry_wait,
    )
    # 查結果不做工作層的重試：下一次 cron（15 分鐘後）接著做，匯入是冪等的。
    register_job(
        app, POLL_BATCHES, poll_batches_job, max_retries=0, retry_wait=settings.job_retry_wait
    )
