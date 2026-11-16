"""組裝：唯一建立 adapter 與資料庫連線的地方（AD-1）。"""

import importlib.metadata
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from radar.adapters.heartbeat import HttpHeartbeat
from radar.adapters.jobs import JobApp, create_job_app, defer_job, retry_stalled_jobs
from radar.config import ConfigError, env_name
from radar.domain.ai_outputs import CallMode, GatewayConfig
from radar.domain.domain_config import OpenAlexConfig
from radar.domain.embeddings import EmbeddingConfig
from radar.domain.jobs import Enqueue
from radar.domain.pricing import ModelsConfig
from radar.domain.schema import SchemaState
from radar.domain.settings import MigrationSettings, Settings
from radar.repo.db import Database
from radar.repo.migrations import MigrationScripts, read_schema_state, upgrade_to_head

if TYPE_CHECKING:
    # 只給型別註記用：OpenAlex、物件儲存（boto3 約 11 MB）與 Anthropic 的 adapter 在 build_*
    # 函式裡才 import，api 行程為了 build_readiness_probes import 這個模組時不會載入它們
    # （mem_limit 很緊）。
    from radar.adapters.anthropic import AnthropicLlmProvider
    from radar.adapters.object_store import S3ObjectStore
    from radar.adapters.openalex import OpenAlexSource
    from radar.adapters.workers_ai import WorkersAiEmbeddingProvider

# 入口不能直接 import radar.repo，這個例外從這裡轉出去給入口攔。
from radar.repo.migrations import MigrationsNotFoundError as MigrationsNotFoundError

# 遷移檔的位置：工作目錄下的 migrations/（映像的 WORKDIR 是 /app，開發時是 repo 根目錄）。
DEFAULT_MIGRATIONS_DIR = Path("migrations")
# prompt 檔的位置：工作目錄下的 prompts/（映像裡由 Dockerfile 複製進去）。
DEFAULT_PROMPTS_DIR = Path("prompts")


@dataclass(frozen=True)
class ReadinessProbes:
    """就緒檢查用的兩個探測函式；共用同一個連線池。"""

    ping: Callable[[], bool]
    schema_state: Callable[[], SchemaState]


def build_readiness_probes(
    settings: Settings, *, pool_size: int, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
) -> ReadinessProbes:
    """建立資料庫連線池，回傳 ping 與遷移狀態兩個探測函式。

    Raises:
        MigrationsNotFoundError: 找不到遷移檔（比對版本要用）。
    """
    scripts = MigrationScripts.load(migrations_dir)
    database = Database(
        settings.database_url.get_secret_value(),
        connect_timeout=settings.database_connect_timeout,
        pool_size=pool_size,
    )
    return ReadinessProbes(
        ping=database.ping, schema_state=partial(read_schema_state, database, scripts)
    )


def build_database(settings: Settings, *, pool_size: int) -> Database:
    """應用程式角色（`DATABASE_URL`）的資料庫連線池，最多 `pool_size` 條。不連線。"""
    return Database(
        settings.database_url.get_secret_value(),
        connect_timeout=settings.database_connect_timeout,
        pool_size=pool_size,
    )


def build_migration_runner(
    settings: MigrationSettings, *, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
) -> Callable[[], frozenset[str]]:
    """回傳「用擁有者角色遷移到最新版本」的函式。

    Raises:
        MigrationsNotFoundError: 找不到遷移檔。
    """
    scripts = MigrationScripts.load(migrations_dir)
    return partial(
        upgrade_to_head,
        settings.migration_database_url.get_secret_value(),
        scripts,
        connect_timeout=settings.database_connect_timeout,
    )


@contextmanager
def build_job_enqueuer(settings: Settings, *, max_connections: int) -> Iterator[Enqueue]:
    """開佇列的連線池（最多 `max_connections` 條），交出排入工作的函式；離開區塊時關掉。

    Raises:
        Exception: `connect_timeout` 秒內連不上（psycopg_pool 的 PoolTimeout）。
    """
    job_app = create_job_app(
        settings.database_url.get_secret_value(),
        max_connections=max_connections,
        connect_timeout=settings.database_connect_timeout,
    )
    with job_app.opened() as app:
        yield partial(defer_job, app)


@dataclass(frozen=True)
class WorkerParts:
    """worker 需要的東西：佇列 App、應用程式的資料庫、排入工作、重試卡住的工作、送心跳。

    `enqueue` 與 `retry_stalled` 用的是佇列 App 目前的連線（worker 開著連線池時才能用）。
    """

    job_app: JobApp
    database: Database
    enqueue: Enqueue
    retry_stalled: Callable[[], int]
    send_heartbeat: Callable[[str], None]


def build_worker(
    settings: Settings,
    *,
    queue_pool_size: int,
    app_pool_size: int,
    heartbeat_urls: Mapping[str, str],
) -> WorkerParts:
    """組 worker 的零件，不連線（連線池由 worker 的監督迴圈開與關）。

    連線數：佇列的連線池 `queue_pool_size`＋佇列監聽 1 條＋應用程式的連線池 `app_pool_size`。
    """
    url = settings.database_url.get_secret_value()
    job_app = create_job_app(
        url, max_connections=queue_pool_size, connect_timeout=settings.database_connect_timeout
    )
    return WorkerParts(
        job_app=job_app,
        database=Database(
            url, connect_timeout=settings.database_connect_timeout, pool_size=app_pool_size
        ),
        enqueue=partial(defer_job, job_app.app),
        retry_stalled=partial(
            retry_stalled_jobs,
            job_app.app,
            seconds_since_heartbeat=settings.worker_stalled_seconds,
            max_retries=settings.job_max_retries,
        ),
        send_heartbeat=HttpHeartbeat(
            heartbeat_urls,
            timeout=settings.heartbeat_timeout,
            attempts=settings.heartbeat_attempts,
            wait_initial=settings.heartbeat_retry_wait_initial,
            wait_max=settings.heartbeat_retry_wait_max,
            retry_after_max=settings.heartbeat_retry_after_max,
        ),
    )


def build_paper_source(settings: Settings, openalex_config: OpenAlexConfig) -> OpenAlexSource:
    """OpenAlex 的 `PaperSource`（呼叫、速率、額度設定來自 `config/openalex.yaml`）。

    用完要 `close()`（或用 `with`）。不連線。

    Raises:
        ConfigError: 沒有設定 `RADAR_OPENALEX_API_KEY`。
    """
    if settings.openalex_api_key is None:
        raise ConfigError([f"缺少環境變數 {env_name('openalex_api_key')}"])
    from radar.adapters.openalex import OpenAlexSource

    return OpenAlexSource(settings.openalex_api_key.get_secret_value(), openalex_config)


def build_embedding_provider(
    settings: Settings, embedding_config: EmbeddingConfig
) -> WorkersAiEmbeddingProvider:
    """Cloudflare Workers AI 的 `EmbeddingProvider`（模型、維度、端點來自 embeddings.yaml）。

    用完要 `close()`（或用 `with`）。不連線。

    Raises:
        ConfigError: 缺少 `RADAR_CLOUDFLARE_ACCOUNT_ID` 或 `RADAR_CLOUDFLARE_API_TOKEN`（訊息只列
            環境變數名稱）；或一筆向量化的 run 最久會跑多久（`EmbeddingConfig.run_limit_seconds`：
            請求的總時限＋2 次逾時＋鎖等待）不小於 `RADAR_LLM_RUN_STALE_AFTER`（還在等回應的 run
            會被 sweep 誤判逾時收掉）。
    """
    run_limit = embedding_config.run_limit_seconds
    if run_limit >= settings.llm_run_stale_after:
        raise ConfigError(
            [
                "config/embeddings.yaml 的一筆向量化最久約"
                f" {run_limit:g} 秒（api.total_timeout_seconds＋2 × api.timeout_seconds＋"
                "(2 × batch_size＋2) × lock_timeout_seconds），要小於"
                f" {env_name('llm_run_stale_after')}（{settings.llm_run_stale_after} 秒）"
            ]
        )
    account_id = settings.cloudflare_account_id
    api_token = settings.cloudflare_api_token
    if account_id is None or api_token is None:
        required = {"cloudflare_account_id": account_id, "cloudflare_api_token": api_token}
        raise ConfigError(
            [
                f"缺少環境變數 {env_name(field)}"
                for field, value in required.items()
                if value is None
            ]
        )
    from radar.adapters.workers_ai import WorkersAiEmbeddingProvider

    return WorkersAiEmbeddingProvider(
        account_id,
        api_token,
        embedding_config.api,
        model_id=embedding_config.model_id,
        dimensions=embedding_config.dimensions,
        query_instruction=embedding_config.query_instruction,
    )


def build_object_store(settings: Settings) -> S3ObjectStore:
    """S3 相容物件儲存（R2、本機 RustFS）的 `ObjectStore`。不連線。

    Raises:
        ConfigError: 缺少 `RADAR_OBJECT_STORE_*` 裡必要的設定；訊息只列環境變數名稱。
    """
    endpoint_url = settings.object_store_endpoint_url
    region = settings.object_store_region
    bucket = settings.object_store_bucket
    access_key_id = settings.object_store_access_key_id
    secret_access_key = settings.object_store_secret_access_key
    if (
        endpoint_url is None
        or region is None
        or bucket is None
        or access_key_id is None
        or secret_access_key is None
    ):
        required = {
            "object_store_endpoint_url": endpoint_url,
            "object_store_region": region,
            "object_store_bucket": bucket,
            "object_store_access_key_id": access_key_id,
            "object_store_secret_access_key": secret_access_key,
        }
        raise ConfigError(
            [
                f"缺少環境變數 {env_name(field)}"
                for field, value in required.items()
                if value is None
            ]
        )
    from radar.adapters.object_store import S3ObjectStore, create_s3_client

    client = create_s3_client(
        endpoint_url=endpoint_url,
        region=region,
        access_key_id=access_key_id.get_secret_value(),
        secret_access_key=secret_access_key.get_secret_value(),
        timeout=settings.object_store_timeout,
    )
    return S3ObjectStore(
        client,
        bucket,
        attempts=settings.object_store_attempts,
        wait_initial=settings.object_store_retry_wait_initial,
        wait_max=settings.object_store_retry_wait_max,
        wait_jitter=settings.object_store_retry_wait_jitter,
        retry_after_max=settings.object_store_retry_after_max,
    )


def build_llm_provider(settings: Settings) -> AnthropicLlmProvider:
    """Anthropic 的 `LlmProvider`（逾時、重試設定來自 `RADAR_ANTHROPIC_*`）。不連線。

    用完要 `close()`（或用 `with`）。

    Raises:
        ConfigError: 沒有設定 `RADAR_ANTHROPIC_API_KEY`；訊息只列環境變數名稱。
    """
    if settings.anthropic_api_key is None:
        raise ConfigError([f"缺少環境變數 {env_name('anthropic_api_key')}"])
    from radar.adapters.anthropic import AnthropicLlmProvider

    return AnthropicLlmProvider(
        settings.anthropic_api_key.get_secret_value(),
        timeout=settings.anthropic_timeout,
        attempts=settings.anthropic_attempts,
        retry_wait_initial=settings.anthropic_retry_wait_initial,
        retry_wait_max=settings.anthropic_retry_wait_max,
        retry_wait_jitter=settings.anthropic_retry_wait_jitter,
        retry_after_max=settings.anthropic_retry_after_max,
        deadline=settings.anthropic_deadline,
    )


def build_gateway_config(
    settings: Settings,
    models: ModelsConfig,
    *,
    prompts_root: Path = DEFAULT_PROMPTS_DIR,
    call_mode: CallMode = CallMode.INTERACTIVE,
) -> GatewayConfig:
    """LLM 閘門的設定：模型與價格表（`config/models.yaml`，由呼叫端用 `load_models_config` 讀）、
    prompt 資料夾、放行設定（`RADAR_PAYWALLED_FULLTEXT_LLM`）、程式版本。不讀檔、不連線。
    """
    return GatewayConfig(
        models=models,
        prompts_root=prompts_root,
        cloud_policy=settings.cloud_policy,
        code_version=code_version(),
        call_mode=call_mode,
    )


def code_version() -> str:
    """記進 `derivation_run.code_version` 的程式版本：安裝的 `radar` 套件版本。"""
    try:
        return importlib.metadata.version("radar")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"
