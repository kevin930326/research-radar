"""設定的型別。值只由 `radar/config.py` 從環境變數讀進來，services 從參數收。"""

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

DATABASE_URL_SCHEMES = frozenset({"postgresql", "postgresql+psycopg"})


class Settings(BaseModel):
    """api 與 worker 啟動時需要的設定。

    環境變數名稱：`database_url` 對應 `DATABASE_URL`，其餘是 `RADAR_` 加上欄位名稱的大寫。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 秘密用 SecretStr：印出設定物件或寫進日誌時只會顯示星號。
    database_url: SecretStr = Field(min_length=1)
    # libpq 的連線逾時（秒），最小有效值是 2。
    database_connect_timeout: int = Field(default=3, ge=2, le=60)
    api_host: str = Field(default="0.0.0.0", min_length=1)
    api_port: int = Field(default=8000, ge=1, le=65535)
    # 收到 SIGTERM 後等手上的請求做完的上限（秒）；compose 的 stop_grace_period 要比它長。
    api_shutdown_graceful_timeout: int = Field(default=10, ge=1, le=300)
    # worker 收到 SIGTERM 後等手上的工作做完的上限（秒）；compose 的 stop_grace_period 要比它長。
    worker_shutdown_graceful_timeout: int = Field(default=30, ge=1, le=600)

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        """格式不對就在讀設定時擋下；錯誤訊息不含連線字串本身。"""
        parts = urlsplit(value.get_secret_value())
        if parts.scheme not in DATABASE_URL_SCHEMES:
            raise ValueError("開頭必須是 postgresql:// 或 postgresql+psycopg://")
        if not parts.hostname:
            raise ValueError("缺少主機名稱")
        try:
            _ = parts.port
        except ValueError:
            raise ValueError("連接埠必須是 1–65535 的數字") from None
        return value
