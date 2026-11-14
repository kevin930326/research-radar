"""AI 產出與 LLM 閘門的 domain 形狀（AD-7、AD-8、ADR-0009、ADR-0015、Story 2.2）。

- 工作代碼只有六個（`LlmTask`）；要加新的工作要改架構文件的 AD-8（架構測試守著）。
- 會產生「表頭」的工作對應到哪一張表頭表（`HEADER_TABLES`）。2.2 只建表頭欄位，內容欄位由
  2.4～2.6 加；`merge_judge` 的 `merge_judgment` 在 Epic 10（它的對象表還不存在）。
- 要送給模型的文字一律由 `build_llm_input` 從資料列（`paper.title`、`paper_abstract`）推出，
  來源（`ContentSource`）也由資料列決定；呼叫端只指定論文、工作、`input_kind`，不能自己給文字
  或來源（ADR-0029：不能讓自由輸入決定來源）。
- 每段文字包成 `<document type="title|abstract">…</document>`，文字裡的 `</document` 先跳脫，
  文件內容沒辦法提早「關掉」標籤、假裝成文件外的指令。
"""

import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, assert_never

from radar.domain.errors import ErrorCode
from radar.domain.papers import AbstractSource, check_storable_text
from radar.domain.policy import CloudPolicy, ContentSource

if TYPE_CHECKING:
    from radar.domain.pricing import ModelsConfig


class LlmTask(StrEnum):
    """LLM 工作代碼（AD-8）。值會寫進 `derivation_run.task`，寫出去之後不改名。"""

    ZH_SUMMARY = "zh_summary"
    CLASSIFY = "classify"
    EXTRACT_MODEL = "extract_model"
    MERGE_JUDGE = "merge_judge"
    QA_ANSWER = "qa_answer"
    EVAL_JUDGE = "eval_judge"


class InputKind(StrEnum):
    """模型看的是什麼（表頭的 `input_kind`）。2.2 只做得出 `title` 與 `abstract`。"""

    TITLE = "title"
    ABSTRACT = "abstract"
    FULLTEXT = "fulltext"


class OutputLang(StrEnum):
    """產出的語言（BCP 47）：`zxx` 是「沒有語言內容」（例如分類的代號）。"""

    ZH_HANT = "zh-Hant"
    EN = "en"
    ZXX = "zxx"


class ResultStatus(StrEnum):
    """表頭的狀態（ADR-0015）。"""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class RunStatus(StrEnum):
    """`derivation_run` 的狀態（ADR-0009）：`succeeded` 包含結果被拒收；`failed` 是暫時性失敗。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CallMode(StrEnum):
    """呼叫方式：互動（有硬性時間上限、由 sweep 收尾）或 Batch（跟著批次的結果走）。"""

    INTERACTIVE = "interactive"
    BATCH = "batch"


# 會產生表頭的工作 → 表頭表（ADR-0015）。`merge_judge` 的 `merge_judgment` 在 Epic 10。
HEADER_TABLES: Final[Mapping[LlmTask, str]] = MappingProxyType(
    {
        LlmTask.ZH_SUMMARY: "zh_summary",
        LlmTask.CLASSIFY: "classification",
        LlmTask.EXTRACT_MODEL: "research_model",
    }
)


@dataclass(frozen=True, slots=True)
class RunKey:
    """`should_run` 與 advisory lock 用的鍵：（對象, 工作, prompt 版本, 模型, `input_sha256`）。"""

    paper_id: uuid.UUID
    task: LlmTask
    prompt_version: int
    model: str
    input_sha256: str


@dataclass(frozen=True, slots=True)
class NewDerivationRun:
    """要新增的一筆 `derivation_run`（狀態一定是 `running`）。

    `params`：送出的參數（effort、max_tokens、disable_thinking、output_schema 名稱與雜湊），
    寫成 jsonb；`eval_run_id` 有值代表評估（Epic 8 補外鍵）。
    """

    key: RunKey
    prompt_sha256: str
    input_kind: InputKind
    params: Mapping[str, object]
    taxonomy_version: str | None
    context_values_version: str | None
    code_version: str
    call_mode: CallMode
    eval_run_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class DerivationRun:
    """讀出來的一筆 `derivation_run`（`ai_results` 核對用的欄位）。"""

    id: uuid.UUID
    key: RunKey
    input_kind: InputKind
    taxonomy_version: str | None
    context_values_version: str | None
    call_mode: CallMode
    eval_run_id: uuid.UUID | None
    status: RunStatus
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class NewLlmCall:
    """要新增的一筆 `llm_call`（一次模型呼叫的帳）。"""

    run_id: uuid.UUID
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cost_usd: Decimal
    price_tier: str
    is_batch: bool
    response_id: str | None
    request_id: str | None
    stop_reason: str | None
    raw_response: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class NewAiResult:
    """要新增的一列表頭（2.2 只有表頭欄位；內容欄位由 2.4～2.6 加）。"""

    paper_id: uuid.UUID
    run_id: uuid.UUID
    input_kind: InputKind
    input_sha256: str
    abstract_id: uuid.UUID | None
    lang: OutputLang
    status: ResultStatus
    reject_reason: ErrorCode | None

    def __post_init__(self) -> None:
        if (self.status is ResultStatus.REJECTED) != (self.reject_reason is not None):
            raise ValueError("只有 rejected 帶原因代碼，而且 rejected 一定要帶")


@dataclass(frozen=True, slots=True)
class CurrentAbstract:
    """論文目前的摘要（`paper_abstract` 裡 `is_current` 的那一筆）。"""

    id: uuid.UUID
    source: AbstractSource
    content: str


@dataclass(frozen=True, slots=True)
class PaperLlmSource:
    """組 LLM 輸入要用的資料列：論文標題與目前的摘要（沒有時是 None）。"""

    paper_id: uuid.UUID
    title: str
    abstract: CurrentAbstract | None


class DocumentType(StrEnum):
    """`<document type="…">` 的值。"""

    TITLE = "title"
    ABSTRACT = "abstract"


@dataclass(frozen=True, slots=True)
class LlmInputBlock:
    """一段要送給模型的文件：已經包好 `<document>` 的文字，以及由資料列推出的來源。

    `text` 還不是 `CloudSafeText`；閘門用 `source` 呼叫 `clear_for_cloud` 放行後才送得出去。
    """

    document_type: DocumentType
    source: ContentSource
    text: str


@dataclass(frozen=True, slots=True)
class LlmInput:
    """一次呼叫的使用者訊息（依序的文件區塊）與它用到哪一筆摘要。"""

    input_kind: InputKind
    blocks: tuple[LlmInputBlock, ...]
    abstract_id: uuid.UUID | None


class NoLlmInputError(Exception):
    """這篇論文沒有這種 `input_kind` 要的內容（例如要摘要但沒有目前的摘要）。"""

    def __init__(self, input_kind: InputKind) -> None:
        self.input_kind = input_kind
        super().__init__(f"沒有可以送給模型的內容（input_kind={input_kind.value}）")


class OutputRejectedError(Exception):
    """模型輸出沒通過驗證：整筆拒收、寫 `rejected`，原因代碼在 `code`。訊息不含輸出內容。"""

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"輸出被拒收（{code.value}）" + (f"：{detail}" if detail else ""))


_CLOSING_TAG: Final = re.compile(r"<(\s*/\s*document)", re.IGNORECASE)


def escape_document_text(text: str) -> str:
    """文字裡的 `</document`（大小寫、空白都算）把 `<` 換成 `&lt;`，不讓文件提早關掉標籤。"""
    return _CLOSING_TAG.sub(r"&lt;\1", text)


def wrap_document(document_type: DocumentType, text: str) -> str:
    """包成 `<document type="…">…</document>`（先跳脫）。"""
    return f'<document type="{document_type.value}">{escape_document_text(text)}</document>'


def abstract_content_source(source: AbstractSource) -> ContentSource:
    """`paper_abstract.source` → 放行用的 `ContentSource`。新增摘要來源時不寫對應就過不了 mypy。"""
    match source:
        case AbstractSource.OPENALEX:
            return ContentSource.OPENALEX_ABSTRACT
        case AbstractSource.SEMANTIC_SCHOLAR:
            return ContentSource.SEMANTIC_SCHOLAR_ABSTRACT
        case AbstractSource.UPLOADED_PDF:
            return ContentSource.UPLOADED_PDF
        case _:
            assert_never(source)


def build_llm_input(source: PaperLlmSource, input_kind: InputKind) -> LlmInput:
    """從資料列組出要送給模型的文件區塊。

    - `title`：只有標題（來源 `title`）。
    - `abstract`：標題＋目前的摘要（摘要的來源由 `paper_abstract.source` 推出）。
    - `fulltext`：還沒有全文的資料列，一律當成沒有輸入。

    Raises:
        NoLlmInputError: 要的內容不存在，或只有空白。
    """
    input_kind = InputKind(input_kind)
    if not source.title.strip():
        raise NoLlmInputError(input_kind)
    title = LlmInputBlock(
        DocumentType.TITLE, ContentSource.TITLE, wrap_document(DocumentType.TITLE, source.title)
    )
    match input_kind:
        case InputKind.TITLE:
            return LlmInput(input_kind, (title,), None)
        case InputKind.ABSTRACT:
            abstract = source.abstract
            if abstract is None or not abstract.content.strip():
                raise NoLlmInputError(input_kind)
            block = LlmInputBlock(
                DocumentType.ABSTRACT,
                abstract_content_source(abstract.source),
                wrap_document(DocumentType.ABSTRACT, abstract.content),
            )
            return LlmInput(input_kind, (title, block), abstract.id)
        case InputKind.FULLTEXT:
            raise NoLlmInputError(input_kind)
        case _:
            assert_never(input_kind)


def check_output_storable(value: object) -> None:
    """模型輸出（解析好的 JSON 值或文字）裡每個字串（含物件的鍵）都存得進 PostgreSQL。

    `jsonb` 與 `text` 都收不下 NUL 字元與單獨的代理字元，`jsonb` 也收不下 NaN、Infinity（原始回應
    用 `json.loads` 讀進來時會變成非有限的 float）；寫入時才失敗會讓整個交易退回，所以先擋。
    用堆疊走訪，不會遞迴過深。

    Raises:
        OutputRejectedError: `llm_output_unstorable`。
    """
    stack: list[object] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            try:
                check_storable_text(item, "模型輸出")
            except ValueError:
                raise OutputRejectedError(ErrorCode.LLM_OUTPUT_UNSTORABLE) from None
        elif isinstance(item, float) and not math.isfinite(item):
            raise OutputRejectedError(ErrorCode.LLM_OUTPUT_UNSTORABLE)
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)


def is_storable_json(value: object) -> bool:
    """`check_output_storable` 的布林版（`llm_call.raw_response` 存不下時改存 NULL）。"""
    try:
        check_output_storable(value)
    except OutputRejectedError:
        return False
    return True


@dataclass(frozen=True)
class GatewayConfig:
    """閘門要用的設定：模型與價格表、prompt 資料夾、放行設定、程式版本、呼叫方式。

    由 `entrypoints/bootstrap.build_gateway_config` 組（bootstrap 不能 import services，所以
    放在 domain；`services/llm_gateway.py` 照樣匯出這個名字）。
    """

    models: ModelsConfig
    prompts_root: Path
    cloud_policy: CloudPolicy
    code_version: str
    call_mode: CallMode = CallMode.INTERACTIVE

    def __post_init__(self) -> None:
        if not self.code_version.strip():
            raise ValueError("code_version 不能是空的")
