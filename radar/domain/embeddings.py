"""向量化的 domain 形狀（PRD FR-5、FR-9，AD-12、AD-29、AD-30，ADR-0005、ADR-0040，Story 2.8）。

- 設定（`config/embeddings.yaml` → `EmbeddingConfig`）：模型、維度、一批幾筆、查詢的指示文字、
  Workers AI 的呼叫與重試、每日免費額度與「用到幾成就停」、估 token 的常數。token 與 account id
  不在這裡，放環境變數（`radar/config.py`）。
- 論文的輸入固定是「標題＋空行＋目前的摘要」，`plan_for` 說只有標題時只送標題
  （`build_embedding_input`）；每一段帶著由資料列推出的來源，各自經 `clear_for_cloud` 放行後
  才接起來（`policy.join_cloud_safe_texts`）。
- 用量：Workers AI 的向量回應沒有 usage 欄位，`api_call` 記的是自己估的 token
  （`estimate_embedding_tokens`：
  每段 UTF-8 位元組 ÷ `bytes_per_token` 無條件進位，再加每段固定的 `overhead_tokens_per_text`），
  刻意偏高估；額度判斷（`quota_allows`）也用這個估計換算 neurons。
- 模型回的向量當作使用者輸入驗證（`check_embedding_vectors`）：筆數、維度、數字有限、在 halfvec 的
  範圍內；不符整批不寫。
- 錯誤型別（`EmbeddingError` 與子類別）帶 `domain/errors.py` 的原因代碼；訊息只有固定的原因
  與狀態碼，
  不含網址、account id、token 或文字內容。
"""

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import ClassVar, Final, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from radar.domain.ai_outputs import InputKind, PaperLlmSource, abstract_content_source
from radar.domain.errors import ErrorCode
from radar.domain.policy import ContentSource
from radar.domain.processing import ProcessingFacts

# 寫進 `derivation_run.task` 的工作代碼（AD-8：向量化不經閘門，代碼是 embed）。
EMBED_TASK: Final = "embed"
# `embedding.item_type`：目前只有論文（全文切塊是全文管線的事，ADR-0005 取捨）。
PAPER_ITEM_TYPE: Final = "paper"
# `api_call.source` 與兩種操作。
WORKERS_AI_SOURCE: Final = "workers_ai"
EMBED_DOCUMENTS_OPERATION: Final = "embed_documents"
EMBED_QUERY_OPERATION: Final = "embed_query"
# Workers AI 的輸入 schema：`documents`／`queries` 陣列最多 32 筆（api-reference.md 第二節）。
MAX_BATCH_SIZE: Final = 32
# pgvector halfvec 的上限：每個數字是 IEEE 半精度，絕對值最大 65504；HNSW 索引最多 4,000 維。
HALFVEC_MAX: Final = 65504.0
HALFVEC_MAX_DIMENSIONS: Final = 4000
# 標題與摘要之間的分隔（AD-12：「標題＋空行＋目前的摘要」）。
EMBED_SEPARATOR: Final = "\n\n"

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_MILLION = Decimal(1_000_000)


# --- 設定（config/embeddings.yaml） --------------------------------------------------------------


class EmbeddingRetry(BaseModel):
    """408、429、5xx、連線錯誤與逾時的重試（只在 adapter 裡，stamina）。"""

    model_config = _FROZEN

    attempts: int = Field(ge=1, le=10)
    wait_initial_seconds: float = Field(gt=0, le=60)
    wait_max_seconds: float = Field(gt=0, le=600)
    wait_jitter_seconds: float = Field(ge=0, le=60)
    retry_after_max_seconds: float = Field(ge=0, le=3600)

    @model_validator(mode="after")
    def _check_waits(self) -> Self:
        if self.wait_max_seconds < self.wait_initial_seconds:
            raise ValueError("wait_max_seconds 不能小於 wait_initial_seconds")
        return self


class WorkersAiApi(BaseModel):
    """Workers AI 的 REST 端點與逾時。網址只到 `/client/v4`，account id 由 adapter 接上。"""

    model_config = _FROZEN

    base_url: str
    timeout_seconds: float = Field(gt=0, le=300)
    retry: EmbeddingRetry

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

    @property
    def worst_case_seconds(self) -> float:
        """一次 `embed_documents` 最久大約要多久：每次嘗試都等到逾時，每次重試前都等最久
        （退避上限＋抖動與 `Retry-After` 上限取大的）。逾時是 httpx 每個階段各自的上限，所以是估計。
        """
        retry = self.retry
        longest_wait = max(
            retry.wait_max_seconds + retry.wait_jitter_seconds, retry.retry_after_max_seconds
        )
        return retry.attempts * self.timeout_seconds + (retry.attempts - 1) * longest_wait


class EmbeddingQuota(BaseModel):
    """Workers AI 的每日免費額度（AD-29；Cloudflare 每天 UTC 00:00 重置）。

    數字照 Cloudflare 價格頁（`source_url`，`verified_on` 查證）；改數字時兩欄一起改。
    """

    model_config = _FROZEN

    daily_free_neurons: int = Field(ge=1, le=1_000_000_000)
    neurons_per_million_input_tokens: int = Field(ge=1, le=1_000_000_000)
    # 今天的估計用量到 `daily_free_neurons × stop_at_ratio` 就停下；0 < 比例 ≤ 1。
    stop_at_ratio: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    verified_on: date
    source_url: str = Field(min_length=1)

    @field_validator("source_url")
    @classmethod
    def _check_source_url(cls, value: str) -> str:
        if urlsplit(value).scheme != "https":
            raise ValueError("要是 https 網址")
        return value

    @model_validator(mode="after")
    def _check_usable_tokens(self) -> Self:
        # 推算天數要除以每天可用的 token；比例太小、換算率太大時會是 0。
        if self.usable_tokens_per_day < 1:
            raise ValueError("daily_free_neurons × stop_at_ratio 換算後一天可用的 token 不到 1")
        return self

    @property
    def stop_at_neurons(self) -> Decimal:
        """今天的估計用量到這麼多 neurons 就停下。"""
        return Decimal(self.daily_free_neurons) * self.stop_at_ratio

    def tokens_to_neurons(self, tokens: int) -> Decimal:
        """估計 token 數換成 neurons（價格頁的換算率）。"""
        return Decimal(tokens) * self.neurons_per_million_input_tokens / _MILLION

    @property
    def daily_free_tokens(self) -> int:
        """每日免費額度約是多少 token（自己換算，不是文件寫的數字；無條件捨去）。"""
        return int(
            Decimal(self.daily_free_neurons) * _MILLION / self.neurons_per_million_input_tokens
        )

    @property
    def usable_tokens_per_day(self) -> int:
        """到停下的比例前，一天可以用多少 token（無條件捨去）。"""
        return int(self.stop_at_neurons * _MILLION / self.neurons_per_million_input_tokens)


class EmbeddingTokenEstimate(BaseModel):
    """估 token 的常數：每段 ceil(UTF-8 位元組 ÷ bytes_per_token)＋每段固定的額外 token。"""

    model_config = _FROZEN

    bytes_per_token: Decimal = Field(gt=0, le=100, allow_inf_nan=False)
    overhead_tokens_per_text: int = Field(ge=0, le=1000)


class EmbeddingConfig(BaseModel):
    """`config/embeddings.yaml`。Cloudflare 的 account id 與 token 不在這裡（環境變數）。"""

    model_config = _FROZEN

    # 目前使用的模型（AD-12：由設定指定）；必須已經登記在 `embedding_model`（遷移建的）。
    model_id: str = Field(min_length=1, max_length=200, pattern=r"^[@A-Za-z0-9._/-]+$")
    # 要和 `embedding_model.dimensions` 一樣（服務開始前核對）。
    dimensions: int = Field(ge=1, le=HALFVEC_MAX_DIMENSIONS)
    # 一次請求送幾篇（Workers AI 上限 32）。
    batch_size: int = Field(ge=1, le=MAX_BATCH_SIZE)
    # 查詢（`embed_query`）帶的指示文字；None＝不帶，用模型的預設值。
    query_instruction: str | None = Field(default=None, min_length=1, max_length=1000)
    api: WorkersAiApi
    quota: EmbeddingQuota
    token_estimate: EmbeddingTokenEstimate


# --- 輸入 ---------------------------------------------------------------------------------------


class NoEmbeddingInputError(Exception):
    """這篇論文沒有這種 `input_kind` 要的內容（例如標題只有空白）。"""

    def __init__(self, input_kind: InputKind) -> None:
        self.input_kind = input_kind
        super().__init__(f"沒有可以向量化的內容（input_kind={input_kind.value}）")


@dataclass(frozen=True, slots=True)
class EmbeddingInput:
    """一篇論文要送去向量化的內容：依序的（文字, 來源），以及用到哪一筆摘要。

    `text` 是實際送出的文字（各段以 `EMBED_SEPARATOR` 接起來），指紋也算它。
    各段還不是 `CloudSafeText`：服務照 `source` 呼叫 `clear_for_cloud` 放行後才接起來送出。
    """

    input_kind: InputKind
    parts: tuple[tuple[str, ContentSource], ...]
    abstract_id: uuid.UUID | None

    @property
    def text(self) -> str:
        return EMBED_SEPARATOR.join(part for part, _ in self.parts)


def build_embedding_input(source: PaperLlmSource, input_kind: InputKind) -> EmbeddingInput:
    """從資料列組出向量的輸入（AD-12）：`title`＝只有標題；`abstract`＝標題＋空行＋目前的摘要。

    標題與摘要去掉頭尾的空白；來源由資料列推出（摘要的來源看 `paper_abstract.source`）。

    Raises:
        NoEmbeddingInputError: 要的內容不存在或只有空白；`fulltext`（還沒有全文）。
    """
    input_kind = InputKind(input_kind)
    title = source.title.strip()
    if not title:
        raise NoEmbeddingInputError(input_kind)
    title_part = (title, ContentSource.TITLE)
    if input_kind is InputKind.TITLE:
        return EmbeddingInput(input_kind, (title_part,), None)
    if input_kind is InputKind.ABSTRACT:
        abstract = source.abstract
        content = "" if abstract is None else abstract.content.strip()
        if abstract is None or not content:
            raise NoEmbeddingInputError(input_kind)
        return EmbeddingInput(
            input_kind,
            (title_part, (content, abstract_content_source(abstract.source))),
            abstract.id,
        )
    raise NoEmbeddingInputError(input_kind)


# --- 用量與額度 ---------------------------------------------------------------------------------


def estimate_embedding_tokens(texts: Sequence[str], estimate: EmbeddingTokenEstimate) -> int:
    """這些文字送去向量化大約是幾個 token：保守估計（偏高）。

    每段 ceil(UTF-8 位元組 ÷ `bytes_per_token`) ＋ `overhead_tokens_per_text`，加總。
    英文平均約 4 個位元組一個 token，設 3 是刻意高估；中文一個字 3 個位元組，約一個 token。
    """
    total = 0
    for text in texts:
        size = Decimal(len(text.encode("utf-8")))
        total += int((size / estimate.bytes_per_token).to_integral_value(rounding=ROUND_CEILING))
        total += estimate.overhead_tokens_per_text
    return total


def quota_allows(used_tokens: int, next_tokens: int, quota: EmbeddingQuota) -> bool:
    """今天（UTC）已經估計用掉 `used_tokens`，再送 `next_tokens` 之後還在停下的比例內嗎。"""
    if used_tokens < 0 or next_tokens < 0:
        raise ValueError("token 數不能是負的")
    return quota.tokens_to_neurons(used_tokens + next_tokens) <= quota.stop_at_neurons


@dataclass(frozen=True, slots=True)
class EmbeddingApiCall:
    """一次向量化請求的紀錄（`api_call`，`source`＝`workers_ai`）。

    - `http_status`：最後一次嘗試的狀態碼；沒有回應（連線錯誤、逾時）是 None。
    - `estimated_input_tokens`：自己估的 token（Workers AI 不回報用量）；失敗也照記（保守）。
    - `cost_usd` 一律記 0：在免費額度內，用量到設定的比例就停（ADR-0040）。
    """

    operation: str
    http_status: int | None
    estimated_input_tokens: int
    called_at: datetime

    def __post_init__(self) -> None:
        if self.operation not in (EMBED_DOCUMENTS_OPERATION, EMBED_QUERY_OPERATION):
            raise ValueError(f"不認得的操作：{self.operation!r}")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("http_status 要是 None 或 100–599")
        if type(self.estimated_input_tokens) is not int or self.estimated_input_tokens < 0:
            raise ValueError("estimated_input_tokens 要是 ≥ 0 的整數")
        if self.called_at.tzinfo is None or self.called_at.utcoffset() is None:
            raise ValueError("called_at 要帶時區")


@dataclass(frozen=True, slots=True)
class NewEmbedRun:
    """要新增的一筆向量化 `derivation_run`（task＝embed、狀態 `running`、沒有 prompt）。

    `call_mode` 固定是互動：卡住的 running 由 sweep 照互動呼叫的時限改成 failed。
    `params`：送出的設定（維度、一批幾筆），寫成 jsonb。
    """

    paper_id: uuid.UUID
    model: str
    input_sha256: str
    input_kind: InputKind
    params: Mapping[str, object]
    code_version: str


@dataclass(frozen=True, slots=True)
class EmbeddingCandidate:
    """要不要向量化時看的一篇論文：處理事實（`plan_for` 用）、輸入用的資料列、這個模型現有向量的
    `input_sha256`（沒有向量是 None）。由 `repo/embeddings.list_embedding_candidates` 讀。"""

    facts: ProcessingFacts
    source: PaperLlmSource
    current_sha256: str | None


@dataclass(frozen=True, slots=True)
class NewEmbedding:
    """要寫進 `embedding` 的一筆（同一個主鍵直接覆寫，AD-12）。`vector` 已經驗證過。"""

    item_id: uuid.UUID
    model_id: str
    dimensions: int
    vector: tuple[float, ...]
    input_sha256: str
    run_id: uuid.UUID

    def __post_init__(self) -> None:
        if len(self.vector) != self.dimensions:
            raise ValueError("vector 的長度要等於 dimensions")


@dataclass(frozen=True, slots=True)
class StoredEmbedding:
    """讀出來的一筆 `embedding`。"""

    item_id: uuid.UUID
    model_id: str
    vector: tuple[float, ...]
    input_sha256: str
    run_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class VectorMatch:
    """向量查詢的一筆結果：論文 ID 與餘弦距離（0＝方向相同，越大越不像）。"""

    paper_id: uuid.UUID
    distance: float


def halfvec_literal(vector: Sequence[float]) -> str:
    """pgvector 的文字格式 `[x1,x2,...]`（寫入時 `CAST(:v AS halfvec)`）。

    Raises:
        ValueError: 是空的，或有非有限、超出 halfvec 範圍的數字。
    """
    if not vector:
        raise ValueError("向量不能是空的")
    parts: list[str] = []
    for value in vector:
        number = float(value)
        if not math.isfinite(number) or abs(number) > HALFVEC_MAX:
            raise ValueError("向量有非有限或超出 halfvec 範圍的數字")
        parts.append(repr(number))
    return "[" + ",".join(parts) + "]"


def parse_halfvec(text: str) -> tuple[float, ...]:
    """`halfvec` 的文字格式換回數字。

    Raises:
        ValueError: 不是 `[...]` 的格式。
    """
    stripped = text.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        raise ValueError("不是 halfvec 的文字格式")
    inner = stripped[1:-1]
    return tuple(float(part) for part in inner.split(",")) if inner else ()


# --- 錯誤 ---------------------------------------------------------------------------------------


class EmbeddingError(Exception):
    """向量化失敗。`code` 是原因代碼；`http_status` 是最後一次嘗試的狀態碼（沒有回應是 None）。

    訊息只有固定的原因（狀態碼、錯誤類別、哪一個欄位不對），不含網址、account id、token 或文字。
    """

    code: ClassVar[ErrorCode] = ErrorCode.EMBED_FAILED

    def __init__(self, reason: str, *, http_status: int | None = None) -> None:
        super().__init__(f"向量化失敗（{self.code.value}）：{reason}")
        self.reason = reason
        self.http_status = http_status


class EmbeddingTransientError(EmbeddingError):
    """重試用完仍是逾時、限流、服務端錯誤或連線錯誤：下一次可以再跑。"""

    code = ErrorCode.EMBED_TRANSIENT


class EmbeddingConfigurationError(EmbeddingError):
    """401、403、404：token、帳號或模型設定不對，要人處理。"""

    code = ErrorCode.EMBED_CONFIGURATION


class EmbeddingRequestError(EmbeddingError):
    """400、413、422 等：請求本身不合法（重送也一樣）。"""

    code = ErrorCode.EMBED_REQUEST_INVALID


class EmbeddingResponseError(EmbeddingError):
    """回應的信封、筆數、維度或數字不對：整批不寫。"""

    code = ErrorCode.EMBED_RESPONSE_INVALID


def check_embedding_vectors(raw: object, *, count: int, dimensions: int) -> list[list[float]]:
    """模型回的向量當作使用者輸入驗證，回轉成 float 的向量。

    要是 `count` 筆、每筆剛好 `dimensions` 個數字；數字是 int 或 float（不收 bool）、有限、
    絕對值不超過 halfvec 的上限 65504。

    Raises:
        EmbeddingResponseError: 任何一項不符（訊息只說哪一項，不含數值）。
    """
    if not isinstance(raw, list):
        raise EmbeddingResponseError("向量不是陣列")
    if len(raw) != count:
        raise EmbeddingResponseError(f"向量筆數不對（要 {count} 筆，收到 {len(raw)} 筆）")
    vectors: list[list[float]] = []
    for index, row in enumerate(raw):
        if not isinstance(row, list):
            raise EmbeddingResponseError(f"第 {index} 筆向量不是陣列")
        if len(row) != dimensions:
            raise EmbeddingResponseError(
                f"第 {index} 筆向量的維度不對（要 {dimensions}，收到 {len(row)}）"
            )
        vector: list[float] = []
        for value in row:
            if type(value) is not int and type(value) is not float:
                raise EmbeddingResponseError(f"第 {index} 筆向量有不是數字的值")
            number = float(value)
            if not math.isfinite(number) or abs(number) > HALFVEC_MAX:
                raise EmbeddingResponseError(f"第 {index} 筆向量有非有限或超出範圍的數字")
            vector.append(number)
        vectors.append(vector)
    return vectors


# --- 一次執行的結果與報表 -----------------------------------------------------------------------


class EmbedStop(StrEnum):
    """`embed_papers` 停下的原因。只有 `failed` 算失敗。"""

    COMPLETED = "completed"  # 要做的論文都看完了
    LIMIT_REACHED = "limit_reached"  # 到了這次指定的篇數
    QUOTA_REACHED = "quota_reached"  # 今天的估計用量到了設定的比例（不算失敗，下一次續跑）
    FAILED = "failed"  # 呼叫或回應失敗（原因在 `failure_reason`）


@dataclass(frozen=True, slots=True)
class EmbedRunResult:
    """一次 `embed_papers` 的結果。

    - `embedded`：這次寫入（新增或覆寫）向量的論文篇數。
    - `estimated_tokens`：這次記進 `api_call` 的估計 token（含失敗的請求）。
    - `requests`：這次送了幾次請求（`embed_documents` 呼叫次數）。
    - `not_applicable`：`plan_for` 說不做；`unchanged`：`input_sha256` 沒變；`no_input`：沒有可送的
      內容；`blocked`：放行擋下；`busy`：同一份輸入另一個工作正在跑；`input_changed`：寫入前核對
      發現輸入又變了（這次不寫，下一次重算）；`failed`：這次標成失敗的篇數。
    """

    stop: EmbedStop
    embedded: int = 0
    estimated_tokens: int = 0
    requests: int = 0
    not_applicable: int = 0
    unchanged: int = 0
    no_input: int = 0
    blocked: int = 0
    busy: int = 0
    input_changed: int = 0
    failed: int = 0
    failure_reason: ErrorCode | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingBacklog:
    """還要建（或重算）向量的論文，只用本地資料估（不送任何請求）。

    - `papers`：`plan_for` 要做、還沒有這個模型的向量或 `input_sha256` 變了、而且放行得了的論文。
    - `estimated_tokens`：這些論文的輸入估計共幾個 token。
    - `blocked`：要做但放行擋下（不會送出）的論文。
    """

    papers: int
    estimated_tokens: int
    blocked: int


def days_needed(tokens: int, quota: EmbeddingQuota) -> Decimal:
    """`tokens` 照每天可用的量（到停下的比例前）要幾天，取到小數一位（無條件進位）。"""
    if tokens <= 0:
        return Decimal(0)
    days = Decimal(tokens) / Decimal(quota.usable_tokens_per_day)
    return days.quantize(Decimal("0.1"), rounding=ROUND_CEILING)


_STOP_TEXT: Final = {
    EmbedStop.COMPLETED: "要做的論文都做完了",
    EmbedStop.LIMIT_REACHED: "到了這次指定的篇數（--limit）",
    EmbedStop.QUOTA_REACHED: "今天的額度到了設定的比例，明天 UTC 00:00（台北 08:00）之後再跑",
    EmbedStop.FAILED: "失敗",
}


def _quota_lines(quota: EmbeddingQuota) -> list[str]:
    ratio = f"{format((quota.stop_at_ratio * 100).normalize(), 'f')}%"
    return [
        f"每日免費額度：{quota.daily_free_neurons:,} neurons，約 {quota.daily_free_tokens:,} token"
        f"（到 {ratio} 就停，一天可用約 {quota.usable_tokens_per_day:,} token；"
        f"{quota.verified_on.isoformat()} 查證）",
    ]


def render_backlog_estimate(
    backlog: EmbeddingBacklog, quota: EmbeddingQuota, model_id: str
) -> list[str]:
    """`radar embeddings backfill --estimate` 的輸出（只用本地資料，沒有送任何請求）。"""
    lines = [
        f"向量化估算（模型 {model_id}；只用本地資料，沒有送任何請求）",
        f"還要建或重算向量的論文：{backlog.papers:,} 篇",
        f"估計共 {backlog.estimated_tokens:,} token（估計值，偏高）",
    ]
    if backlog.papers:
        average = math.ceil(backlog.estimated_tokens / backlog.papers)
        lines.append(f"平均每篇約 {average:,} token")
    lines += _quota_lines(quota)
    lines.append(f"推算要 {days_needed(backlog.estimated_tokens, quota)} 天")
    if backlog.blocked:
        lines.append(f"放行擋下、不會送出的論文：{backlog.blocked:,} 篇")
    return lines


def render_backfill_report(
    result: EmbedRunResult, backlog: EmbeddingBacklog, quota: EmbeddingQuota, model_id: str
) -> list[str]:
    """`radar embeddings backfill` 跑完的輸出：這次做了多少、照這次的平均推算全庫還要多少。

    推算用這次的平均（`estimated_tokens ÷ embedded`）乘以還沒做的篇數；這次一篇都沒寫時改用
    本地估計。token 都是估計值（Workers AI 不回報用量），實際用量看 Cloudflare 後台。
    """
    stop = _STOP_TEXT[result.stop]
    if result.stop is EmbedStop.FAILED and result.failure_reason is not None:
        stop = f"{stop}（{result.failure_reason.value}）"
    lines = [
        f"向量化結束（模型 {model_id}）：{stop}",
        f"這次寫入向量：{result.embedded:,} 篇（送出 {result.requests:,} 次請求）",
        f"這次估計用了：{result.estimated_tokens:,} token"
        "（估計值，偏高；Workers AI 不回報用量，實際用量看 Cloudflare 後台的 neurons）",
    ]
    if result.embedded:
        average = Decimal(result.estimated_tokens) / result.embedded
        projected = int((average * backlog.papers).to_integral_value(rounding=ROUND_CEILING))
        lines.append(f"平均每篇約 {math.ceil(average):,} token")
        basis = "照這次的平均推算"
    else:
        projected = backlog.estimated_tokens
        basis = "照本地估計"
    lines.append(f"還沒有向量（或要重算）的論文：{backlog.papers:,} 篇")
    lines.append(f"{basis}還要：{projected:,} token")
    lines += _quota_lines(quota)
    lines.append(f"推算還要 {days_needed(projected, quota)} 天")
    lines.append(
        "略過："
        f"不做（plan_for）{result.not_applicable:,} 篇、"
        f"沒變 {result.unchanged:,} 篇、"
        f"沒有內容 {result.no_input:,} 篇、"
        f"放行擋下 {result.blocked:,} 篇、"
        f"別的工作正在跑 {result.busy:,} 篇、"
        f"送出後輸入又變了 {result.input_changed:,} 篇、"
        f"失敗 {result.failed:,} 篇"
    )
    return lines
