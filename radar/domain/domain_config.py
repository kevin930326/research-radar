"""領域設定的型別（AD-18）：`config/` 下的五個檔。

- `journals.yaml`：追蹤的期刊（`JournalsConfig`）。
- `openalex.yaml`：OpenAlex 的呼叫設定（`OpenAlexConfig`）。
- `exclusion_rules.yaml`：標記非研究文章的排除規則（`ExclusionRulesConfig`，PRD FR-8）。
- `keyword_sources.yaml`：目前用哪個關鍵字來源（`KeywordSourcesConfig`，ADR-0016）。
- `demo_publishers.yaml`：示範站不放完整內容的出版社（`DemoPublishersConfig`，ADR-0052）。

檔案只由 `radar/config.py` 讀（YAML safe load、重複的鍵也算錯），讀進來後用這裡的模型驗證；
api、worker 啟動時與 CLI 每個指令執行前都驗證一次，錯了就不啟動。
"""

import dataclasses
from decimal import Decimal
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from radar.domain.identifiers import InvalidIdentifierError, normalize_issn_l, normalize_openalex_id
from radar.domain.papers import KeywordSource
from radar.domain.policy import PublisherExclusions, normalize_publisher_name
from radar.domain.scope import ScopeSettings

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

    def scope_settings(self) -> ScopeSettings:
        """追蹤範圍的設定：這些期刊的 ISSN-L 與起始年（`domain/scope.py` 的 `is_in_scope` 用）。

        **不帶示範站的排除清單**（`demo_publishers` 是 None）：拿它寫入的論文一律不算示範站可用。
        正式的寫入路徑（每週更新、reparse）要用 `DomainConfig.scope_settings()`；這個只留給只看
        追蹤範圍的地方與既有測試。
        """
        return ScopeSettings(
            frozenset(journal.issn_l for journal in self.journals), start_year=self.start_year
        )


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


class ExclusionRule(BaseModel):
    """一條排除規則：標題符合 `title_equals` 或 `title_starts_with` 其中一個字串就標成 `reason`。

    比對前標題與規則字串都做同一個正規化（`domain/exclusion.py` 的 `normalize_title`）。
    """

    model_config = _FROZEN

    # 寫進 `paper.excluded_reason` 的代碼：小寫英文開頭、只用小寫英文、數字、底線。
    reason: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    title_equals: tuple[str, ...] = ()
    title_starts_with: tuple[str, ...] = ()

    @field_validator("title_equals", "title_starts_with")
    @classmethod
    def _no_blank_strings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for index, value in enumerate(values):
            if not value.strip():
                raise ValueError(f"第 {index} 個字串是空白")
        return values

    @model_validator(mode="after")
    def _require_a_pattern(self) -> Self:
        if not self.title_equals and not self.title_starts_with:
            raise ValueError("title_equals 與 title_starts_with 至少要有一個非空白字串")
        return self


class ExclusionRulesConfig(BaseModel):
    """`config/exclusion_rules.yaml`：依檔案順序比對，第一條符合的勝出；可以是空清單。"""

    model_config = _FROZEN

    rules: tuple[ExclusionRule, ...]


class KeywordSourcesConfig(BaseModel):
    """`config/keyword_sources.yaml`：目前畫面與統計用哪個關鍵字來源（ADR-0016）。

    寫入不看這個值：OpenAlex 的關鍵字一律以 `openalex` 寫入；換來源時先寫新來源、再改 `active`。
    """

    model_config = _FROZEN

    active: KeywordSource


class DemoExcludedPublisher(BaseModel):
    """一家示範站不放完整內容的出版社：OpenAlex 的出版社 ID 或名稱，任一個對上就排除。

    `id` 可以是 None（還沒查證）；`name` 用 OpenAlex `host_organization_name` 的完整寫法，比對前
    做 `normalize_publisher_name`（只比字母與數字；兩筆名稱得到同一個鍵不算錯，合併成一筆）。
    `note` 寫來源或為什麼列（只給人看）。
    """

    model_config = _FROZEN

    id: str | None
    name: str = Field(min_length=1, max_length=300)
    note: str = Field(min_length=1, max_length=500)

    @field_validator("id", mode="before")
    @classmethod
    def _normalize_id(cls, value: object) -> object:
        # 只處理字串；其他型別交給 pydantic 回報。
        if not isinstance(value, str):
            return value
        try:
            normalized = normalize_openalex_id(value)
        except InvalidIdentifierError:
            raise ValueError("要是 OpenAlex 的出版社 ID（P 開頭）") from None
        if not normalized.startswith("P"):
            raise ValueError("要是 OpenAlex 的出版社 ID（P 開頭）")
        return normalized

    @field_validator("name", "note")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能是空白")
        return value

    @field_validator("name")
    @classmethod
    def _has_letters_or_digits(cls, value: str) -> str:
        # 比對只看字母與數字（ADR-0052 第 5 點）；一個都沒有的名稱什麼都對不上，當成寫錯。
        if not normalize_publisher_name(value):
            raise ValueError("至少要有一個字母或數字")
        return value


class DemoPublishersConfig(BaseModel):
    """`config/demo_publishers.yaml`：示範站不放完整內容的出版社（ADR-0007 第 5 點、ADR-0052）。

    清單至少一筆（Kevin 2026-11-14 決定先放 IEEE；整個清單不見等於全部放行，所以不收空清單）。
    """

    model_config = _FROZEN

    excluded: tuple[DemoExcludedPublisher, ...] = Field(min_length=1)

    def exclusions(self) -> PublisherExclusions:
        """`is_demo_eligible` 用的排除清單（ID 與正規化過的名稱）。"""
        return PublisherExclusions(
            ids=frozenset(entry.id for entry in self.excluded if entry.id is not None),
            names=frozenset(normalize_publisher_name(entry.name) for entry in self.excluded),
        )


class DomainConfig(BaseModel):
    """五個領域設定檔合在一起。"""

    model_config = _FROZEN

    journals: JournalsConfig
    openalex: OpenAlexConfig
    exclusion_rules: ExclusionRulesConfig
    keyword_sources: KeywordSourcesConfig
    demo_publishers: DemoPublishersConfig

    def scope_settings(self) -> ScopeSettings:
        """寫入論文用的設定：追蹤範圍（`journals.yaml`）＋示範站排除的出版社。

        正式的寫入路徑（每週更新、reparse）一律用這個，不用 `JournalsConfig.scope_settings()`
        （那個不帶排除清單，寫入的論文一律不算示範站可用）。
        """
        return dataclasses.replace(
            self.journals.scope_settings(), demo_publishers=self.demo_publishers.exclusions()
        )
