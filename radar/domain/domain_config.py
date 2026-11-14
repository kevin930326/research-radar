"""領域設定的型別（AD-18）：`config/journals.yaml` 與 `config/openalex.yaml`。

檔案只由 `radar/config.py` 讀（YAML safe load、重複的鍵也算錯），讀進來後用這裡的模型驗證；
api、worker 啟動時與 CLI 每個指令執行前都驗證一次，錯了就不啟動。
"""

from decimal import Decimal
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from radar.domain.identifiers import normalize_issn_l

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Journal(BaseModel):
    """一本追蹤的期刊。"""

    model_config = _FROZEN

    # 期刊代號：小寫英文開頭、只用小寫英文、數字、底線。
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    name: str = Field(min_length=1, max_length=300)
    # OpenAlex 的 ISSN-L；經 `normalize_issn_l`（只檢查格式，ADR-0020）。
    issn_l: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能是空白")
        return value

    @field_validator("issn_l", mode="before")
    @classmethod
    def _normalize_issn_l(cls, value: object) -> object:
        # 只處理字串；其他型別交給 pydantic 回報「要是字串」。
        return normalize_issn_l(value) if isinstance(value, str) else value


class JournalsConfig(BaseModel):
    """`config/journals.yaml`：從哪一年起抓、追蹤哪些期刊。"""

    model_config = _FROZEN

    start_year: int = Field(ge=1900, le=9999)
    journals: tuple[Journal, ...] = Field(min_length=1)

    @field_validator("journals")
    @classmethod
    def _no_duplicates(cls, journals: tuple[Journal, ...]) -> tuple[Journal, ...]:
        problems: list[str] = []
        for field in ("key", "issn_l"):
            first_index: dict[str, int] = {}
            for index, journal in enumerate(journals):
                value = getattr(journal, field)
                if value in first_index:
                    problems.append(
                        f"journals.{index}.{field} 和 journals.{first_index[value]}.{field}"
                        f" 重複（{value!r}）"
                    )
                else:
                    first_index[value] = index
        if problems:
            raise ValueError("；".join(problems))
        return journals


class OpenAlexRetry(BaseModel):
    """409、429、5xx、連線錯誤與逾時的重試（只在 adapter 裡，stamina）。"""

    model_config = _FROZEN

    attempts: int = Field(ge=1, le=20)
    wait_initial_seconds: float = Field(gt=0, le=60)
    wait_max_seconds: float = Field(gt=0, le=600)
    wait_jitter_seconds: float = Field(ge=0, le=60)
    retry_after_max_seconds: float = Field(ge=0, le=3600)

    @model_validator(mode="after")
    def _check_waits(self) -> Self:
        if self.wait_max_seconds < self.wait_initial_seconds:
            raise ValueError("wait_max_seconds 不能小於 wait_initial_seconds")
        return self


class OpenAlexQuota(BaseModel):
    """每日額度（AD-29）。金額一律 `Decimal`。"""

    model_config = _FROZEN

    daily_budget_usd: Decimal = Field(gt=0, lt=1_000_000, allow_inf_nan=False)
    # 今天的用量到 `daily_budget_usd × stop_at_ratio` 就停下；0 < 比例 ≤ 1。
    stop_at_ratio: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    # 回應沒有 `x-ratelimit-cost-usd` 標頭時，一次請求算多少錢。
    default_request_cost_usd: Decimal = Field(ge=0, lt=1_000_000, allow_inf_nan=False)

    @property
    def stop_at_usd(self) -> Decimal:
        """今天的用量到這個金額就停下。"""
        return self.daily_budget_usd * self.stop_at_ratio


class OpenAlexConfig(BaseModel):
    """`config/openalex.yaml`。API key 不在這裡，放環境變數 `RADAR_OPENALEX_API_KEY`。"""

    model_config = _FROZEN

    base_url: str
    per_page: int = Field(ge=1, le=200)
    # OpenAlex 的上限是每秒 10 個請求（spikes/oq-02/check_coverage.py 的註解記載的官方限制）。
    requests_per_second: float = Field(gt=0, le=10)
    timeout_seconds: float = Field(gt=0, le=300)
    retry: OpenAlexRetry
    quota: OpenAlexQuota

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https":
            raise ValueError("開頭必須是 https://")
        if not parts.hostname:
            raise ValueError("缺少主機名稱")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("不能帶查詢字串、錨點或帳號密碼")
        return value.rstrip("/")


class DomainConfig(BaseModel):
    """兩個領域設定檔合在一起。"""

    model_config = _FROZEN

    journals: JournalsConfig
    openalex: OpenAlexConfig
