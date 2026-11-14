"""設定的型別。值只由 `radar/config.py` 從環境變數讀進來，services 從參數收。"""

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator

from radar.domain.jobs import WORKER_HEARTBEAT_SECONDS
from radar.domain.policy import CloudPolicy
from radar.domain.weekly_run import DEFAULT_MAX_FAILURES

DATABASE_URL_SCHEMES = frozenset({"postgresql", "postgresql+psycopg"})
HEARTBEAT_URL_SCHEMES = frozenset({"https", "http"})
# 物件儲存的端點：正式是 R2（https），本機 RustFS 是 http://127.0.0.1:9000。
OBJECT_STORE_URL_SCHEMES = frozenset({"https", "http"})
# 心跳網址的設定欄位：`heartbeat_url_` 加上排程名稱（連字號換成底線），一個排程一個（AD-15）。
HEARTBEAT_URL_FIELD_PREFIX = "heartbeat_url_"


def check_database_url(value: SecretStr) -> SecretStr:
    """連線字串格式不對就在讀設定時擋下。

    錯誤訊息一律是固定句子：解析時的原始例外可能帶出整段網址（含帳密），
    所以全部攔下、改丟不含值的訊息，並用 `from None` 斷開原本的例外鏈。
    """
    try:
        parts = urlsplit(value.get_secret_value())
        scheme, hostname = parts.scheme, parts.hostname
    except Exception:
        raise ValueError("連線字串無法解析") from None
    if scheme not in DATABASE_URL_SCHEMES:
        raise ValueError("開頭必須是 postgresql:// 或 postgresql+psycopg://")
    if not hostname:
        raise ValueError("缺少主機名稱")
    try:
        _ = parts.port
    except Exception:
        raise ValueError("連接埠必須是 1–65535 的數字") from None
    return value


def check_heartbeat_url(value: SecretStr | None) -> SecretStr | None:
    """心跳網址要是 http(s) 而且有主機名稱。網址裡有監看服務給的通行碼，訊息不帶值。"""
    if value is None:
        return None
    try:
        parts = urlsplit(value.get_secret_value())
        scheme, hostname = parts.scheme, parts.hostname
    except Exception:
        raise ValueError("網址無法解析") from None
    if scheme not in HEARTBEAT_URL_SCHEMES:
        raise ValueError("開頭必須是 https:// 或 http://")
    if not hostname:
        raise ValueError("缺少主機名稱")
    return value


def heartbeat_url_field(schedule: str) -> str:
    """排程名稱對應的心跳網址設定欄位（`weekly-update` → `heartbeat_url_weekly_update`）。"""
    return HEARTBEAT_URL_FIELD_PREFIX + schedule.replace("-", "_")


class Settings(BaseModel):
    """api、worker 與一般 CLI 指令需要的設定。

    環境變數名稱：`database_url` 對應 `DATABASE_URL`，其餘是 `RADAR_` 加上欄位名稱的大寫。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 秘密用 SecretStr：印出設定物件或寫進日誌時只會顯示星號。
    # 應用程式角色（radar_app）的連線字串（AD-17）。
    database_url: SecretStr = Field(min_length=1)
    # libpq 的連線逾時（秒），最小有效值是 2。
    database_connect_timeout: int = Field(default=3, ge=2, le=60)
    # 每個行程最多同時佔用幾條連線；沒設定時用 `radar/config.py` 的連線數預算
    # （ADR-0003 決定第 4 點）。
    api_database_pool_size: int | None = Field(default=None, ge=1, le=35)
    worker_database_pool_size: int | None = Field(default=None, ge=1, le=35)
    cli_database_pool_size: int | None = Field(default=None, ge=1, le=35)
    api_host: str = Field(default="0.0.0.0", min_length=1)
    api_port: int = Field(default=8000, ge=1, le=65535)
    # 收到 SIGTERM 後等手上的請求做完的上限（秒）；compose 的 stop_grace_period 要比它長。
    api_shutdown_graceful_timeout: int = Field(default=10, ge=1, le=300)
    # worker 收到 SIGTERM 後等手上的工作做完的上限（秒）；compose 的 stop_grace_period 要比它長。
    worker_shutdown_graceful_timeout: int = Field(default=30, ge=1, le=600)
    # worker 同時跑幾個工作（ADR-0019）；pdf 佇列另有同一把鎖，同時只跑一個。
    worker_concurrency: int = Field(default=2, ge=1, le=8)
    # worker 連不上資料庫、資料庫還沒遷移或佇列中斷時，隔幾秒重開。
    worker_restart_interval: int = Field(default=10, ge=1, le=600)
    # worker 超過幾秒沒有向資料庫報到（每 WORKER_HEARTBEAT_SECONDS 秒報一次）就算已經不在，
    # 它手上「執行中」的工作由 sweep 重試。至少是報到間隔的 3 倍，避免誤判。
    worker_stalled_seconds: int = Field(default=60, ge=3 * WORKER_HEARTBEAT_SECONDS, le=3600)
    # 工作層的重試（ADR-0004 決定第 7 點）：整個工作失敗後最多再試幾次；
    # 第 n 次重試前等 n × job_retry_wait 秒。連線層的重試在各 adapter，不在這裡。
    job_max_retries: int = Field(default=3, ge=0, le=20)
    job_retry_wait: int = Field(default=30, ge=1, le=3600)
    # 一筆每週更新從建立起累計失敗幾次就標成 failed（ADR-0028）：跨工作重試、sweep 補排、下一週
    # cron 接著同一筆都一起算；額度停下、reparse 正在跑而先不做不算失敗。
    weekly_update_max_failures: int = Field(default=DEFAULT_MAX_FAILURES, ge=1, le=100)
    # 心跳（AD-15）：一個排程一個網址，沒設的排程不送心跳、只記警告。
    # 欄位名稱是 heartbeat_url_<排程名稱>，和 radar/domain/jobs.py 的排程一一對應（有測試）。
    heartbeat_url_sweep: SecretStr | None = None
    # 每週更新：發佈快照後才送（Story 1.9）；1.8 只先登記欄位（架構測試要求每個排程一個）。
    heartbeat_url_weekly_update: SecretStr | None = None
    # 送一次心跳的 HTTP 逾時（秒）與最多嘗試幾次（含第一次；失敗只記錯誤，不讓工作失敗）。
    heartbeat_timeout: int = Field(default=5, ge=1, le=60)
    heartbeat_attempts: int = Field(default=3, ge=1, le=10)
    # 心跳重試的等待（秒，Conventions「重試、逾時、速率」）：一般是指數退避，從 initial 開始
    # 每次加倍、最多 max；對方回 429／5xx 並帶 Retry-After 時，不超過 retry_after_max 就照它等；
    # 超過就這一輪不再重試、記成心跳失敗（不提早重送，也不卡住 worker；ADR-0019 決定第 8 點）。
    heartbeat_retry_wait_initial: float = Field(default=1.0, gt=0, le=60)
    heartbeat_retry_wait_max: float = Field(default=10.0, gt=0, le=300)
    heartbeat_retry_after_max: int = Field(default=30, ge=0, le=600)
    # OpenAlex 的 API key（FR-10）。只有抓取的指令與工作需要；其餘的呼叫、速率、額度設定在
    # config/openalex.yaml（AD-18）。
    openalex_api_key: SecretStr | None = Field(default=None, min_length=1)
    # 物件儲存（R2／本機 RustFS，S3 相容）：端點網址、區域（R2 是 auto）、bucket、金鑰。
    # 只有寫原始資料的指令與工作需要；缺了由 bootstrap 在組裝時回報。
    object_store_endpoint_url: str | None = None
    object_store_region: str | None = Field(default=None, min_length=1)
    object_store_bucket: str | None = Field(default=None, min_length=1)
    object_store_access_key_id: SecretStr | None = Field(default=None, min_length=1)
    object_store_secret_access_key: SecretStr | None = Field(default=None, min_length=1)
    # 物件儲存的連線與讀取逾時（秒）與最多嘗試幾次（含第一次；adapter 裡的 stamina 重試，
    # botocore 自己的重試關掉）。
    object_store_timeout: int = Field(default=10, ge=1, le=300)
    object_store_attempts: int = Field(default=3, ge=1, le=10)
    # 物件儲存重試的等待（秒，Conventions「重試、逾時、速率」）：一般是指數退避，從 initial 開始
    # 每次加倍、最多 max，再加 0～jitter 秒的抖動；對方回 429／5xx 並帶 Retry-After 時照它等，
    # 要等的超過 retry_after_max 就不再重試、這次存取算失敗（不提早重送；對方給極大值時也不卡住
    # 工作，ADR-0021）。
    object_store_retry_wait_initial: float = Field(default=0.5, gt=0, le=60)
    object_store_retry_wait_max: float = Field(default=5.0, gt=0, le=300)
    object_store_retry_wait_jitter: float = Field(default=0.5, ge=0, le=60)
    object_store_retry_after_max: int = Field(default=30, ge=0, le=600)
    # 送雲端的放行（AD-9、ADR-0029）：付費全文與上傳的 PDF 內容可不可以送 Anthropic／Cloudflare。
    # 預設關；圖書館回覆允許時只改這個設定，不改程式。
    paywalled_fulltext_llm: bool = False
    # Anthropic 的 API key。只有要呼叫 LLM 的指令與工作需要；缺了由 bootstrap 在組裝時回報。
    anthropic_api_key: SecretStr | None = Field(default=None, min_length=1)
    # 一次呼叫的逾時（秒）與最多嘗試幾次（含第一次；adapter 裡的 stamina 重試，
    # SDK 自己的重試關掉）。
    anthropic_timeout: int = Field(default=60, ge=1, le=600)
    anthropic_attempts: int = Field(default=3, ge=1, le=10)
    # Anthropic 重試的等待（秒，Conventions「重試、逾時、速率」）：只重試逾時、限流、服務端錯誤與
    # 連線錯誤；一般是指數退避，從 initial 開始每次加倍、最多 max，再加 0～jitter 秒的抖動；
    # 對方回 Retry-After 時照它等，要等的超過 retry_after_max 就不再重試（不提早重送）。
    anthropic_retry_wait_initial: float = Field(default=1.0, gt=0, le=60)
    anthropic_retry_wait_max: float = Field(default=10.0, gt=0, le=300)
    anthropic_retry_wait_jitter: float = Field(default=1.0, ge=0, le=60)
    anthropic_retry_after_max: int = Field(default=60, ge=0, le=600)
    # 一次互動呼叫含所有重試的總時限（秒，AD-8 的硬性時間上限）：超過就不再重試。正在進行的那一次
    # 嘗試不會被打斷，所以最壞是 deadline + timeout 秒。
    anthropic_deadline: int = Field(default=180, ge=1, le=3600)
    # 互動呼叫的 `derivation_run` 建立超過幾秒還是 running，sweep 就改成 failed
    # （`llm_run_expired`）。
    # 要比 anthropic_deadline + anthropic_timeout 長，否則還在跑的呼叫會被誤判。
    llm_run_stale_after: int = Field(default=900, ge=60, le=86_400)

    @property
    def cloud_policy(self) -> CloudPolicy:
        """送雲端的放行要看的設定（交給 `clear_for_cloud`）。"""
        return CloudPolicy(paywalled_fulltext_llm=self.paywalled_fulltext_llm)

    @field_validator("anthropic_api_key")
    @classmethod
    def _check_anthropic_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        # 帶空白或換行的 key 會變成不合法的 HTTP 標頭，送出時被當成連線錯誤一直重試；
        # 在讀設定時擋下。訊息是固定句子，不帶值。
        if value is not None and any(ch.isspace() for ch in value.get_secret_value()):
            raise ValueError("不能含空白或換行")
        return value

    @field_validator("llm_run_stale_after")
    @classmethod
    def _check_llm_run_stale_after(cls, value: int, info: ValidationInfo) -> int:
        # 欄位依定義順序驗證：deadline、timeout 在前面；它們本身不合法時另有錯誤，這裡不重複報。
        deadline = info.data.get("anthropic_deadline")
        timeout = info.data.get("anthropic_timeout")
        if isinstance(deadline, int) and isinstance(timeout, int) and value <= deadline + timeout:
            raise ValueError(
                "要大於 RADAR_ANTHROPIC_DEADLINE + RADAR_ANTHROPIC_TIMEOUT"
                "（還在跑的呼叫會被誤判逾時）"
            )
        return value

    @field_validator("object_store_endpoint_url")
    @classmethod
    def _check_object_store_endpoint_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parts = urlsplit(value)
            scheme, hostname = parts.scheme, parts.hostname
            has_userinfo = parts.username is not None or parts.password is not None
        except Exception:
            raise ValueError("網址無法解析") from None
        if scheme not in OBJECT_STORE_URL_SCHEMES:
            raise ValueError("開頭必須是 https:// 或 http://")
        if not hostname:
            raise ValueError("缺少主機名稱")
        if has_userinfo:
            # 帳密走 RADAR_OBJECT_STORE_ACCESS_KEY_ID／SECRET_ACCESS_KEY（SecretStr），
            # 放在網址裡會跟著網址出現在錯誤訊息與日誌。
            raise ValueError("網址不能含帳號或密碼")
        try:
            _ = parts.port
        except Exception:
            raise ValueError("連接埠必須是 1–65535 的數字") from None
        if parts.query or parts.fragment:
            # S3 端點用不到查詢參數與 #片段；這兩段可能帶簽章之類的值，訊息不帶出來。
            raise ValueError("網址不能含查詢參數（?）或片段（#）")
        return value

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        return check_database_url(value)

    @field_validator("*")
    @classmethod
    def _check_heartbeat_url(cls, value: object, info: ValidationInfo) -> object:
        # 之後的排程新增 heartbeat_url_* 欄位時，不用再回來改這裡。
        name = info.field_name or ""
        if name.startswith(HEARTBEAT_URL_FIELD_PREFIX) and isinstance(value, SecretStr):
            return check_heartbeat_url(value)
        return value


class MigrationSettings(BaseModel):
    """`radar migrate` 需要的設定。api、worker 不讀這一組（擁有者的連線字串不給它們）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 擁有者角色（radar_owner）的連線字串，環境變數 `RADAR_MIGRATION_DATABASE_URL`（AD-17）。
    migration_database_url: SecretStr = Field(min_length=1)
    database_connect_timeout: int = Field(default=3, ge=2, le=60)

    @field_validator("migration_database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        return check_database_url(value)
