"""管理者的 AI 指令（`radar runs show`／`retry`／`remove-by-source`／`estimate-revision`，
Story 2.12、ADR-0044）：結果型別與要印的字（純函式）。

- 查詢（`PaperAiReport`）：三個 AI 區塊（狀態、有效值、來歷）與這篇三個工作的歷次 run
  （`AiHistoryRow`，有表頭照表頭狀態，沒有照 run 狀態）。有效值的 prompt 版本取歷次裡同工作
  `active` 那一列的 run；只有人工值時寫「人工」。
- 重跑（`RetryPlan`、`RetryReport`）：只重跑「目前這組鍵最新一列平常流程表頭是 `rejected`、沒有
  `active`」的工作；每項記下確認時的 `RunKey` 與那一列 rejected 的 id（閘門鎖內再核對）。最壞花費＝
  單次上限 × adapter 的 `attempts`。
- 依來源移除（`SourceAiCounts`、`SourceRemovalPreview`、`SourceRemovalResult`）：會刪的表頭（各表
  各狀態）與細項、還在跑的 run、目前摘要仍是這個來源的論文、來源不明的表頭；擋下條件
  （`RemovalBlock`）與下一步；稽核事件只記來源與各表計數（`source_removal_audit`）。
- 改版預估（`RevisionEstimate`）：各工作「有現行結果、改版後會重跑」篇數與 Batch 價保守上限、
  「還沒有結果」篇數、上限值。

金額一律 `format_usd`（4 位小數）。印出的文字只有 id、DOI、工作、狀態、代碼與計數，以及 `show`
的有效值（終端機用途，控制字元先跳脫；不寫進日誌）。
"""

import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from radar.domain.ai_blocks import (
    AiBlock,
    AiBlockState,
    AiProduct,
    ClassificationFieldValue,
    ClassificationValue,
    ResearchModelValue,
    ZhSummaryValue,
)
from radar.domain.ai_outputs import (
    HEADER_TABLES,
    InputKind,
    LlmTask,
    ResultStatus,
    RunKey,
    RunStatus,
)
from radar.domain.audit import AuditActorRole, NewAuditEvent
from radar.domain.budget import BUDGET_TIMEZONE, BudgetStatus, format_usd
from radar.domain.papers import AbstractSource
from radar.domain.text_table import render_table

_ZERO: Final = Decimal(0)

# 重跑、查詢的三個工作（有表頭、`plan_for` 會回答的 LLM 工作），照這個順序印。
ADMIN_TASKS: Final = (LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL)
# 依來源移除會刪的表，照刪除前印的順序（細項接在表頭後面；實際刪除先細項後表頭）。構面只刪
# 沒有其他人用的；向量是用這個來源的摘要算的（Story 2.12 審查後第 2 點）。
DETAIL_TABLES: Final = ("classification_label", "construct_mention", "hypothesis")
REMOVAL_TABLES: Final = (
    "zh_summary",
    "classification",
    "classification_label",
    "research_model",
    "construct_mention",
    "hypothesis",
    "construct",
    "embedding",
)
# 稽核事件的動作與對象種類（`audit_event.action`／`target_type`）。
SOURCE_REMOVAL_ACTION: Final = "remove_source_ai_outputs"
SOURCE_REMOVAL_TARGET: Final = "abstract_source"


def _when(moment: datetime) -> str:
    """台北時間，到分鐘。"""
    return moment.astimezone(BUDGET_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def terminal_text(value: str) -> str:
    """要印到終端機的資料文字：控制字元、格式字元（例如雙向文字控制）與行／段分隔字元寫成
    `\\uXXXX`，不讓資料改掉終端機的顯示。"""
    return "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} else char
        for char in value
    )


# --- 查詢（`radar runs show`） --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LatestAiResult:
    """這組鍵最新的一列平常流程表頭（`repo/ai_outputs.find_latest_ai_result`）。"""

    id: uuid.UUID
    status: ResultStatus
    reject_reason: str | None


@dataclass(frozen=True, slots=True)
class AiHistoryRow:
    """這篇論文一個工作的一筆 run（`repo/ai_outputs.list_paper_ai_history`）：有表頭時帶表頭的
    狀態與拒收原因。`is_eval`：評估（`eval_run_id` 有值）的 run。"""

    run_id: uuid.UUID
    task: LlmTask
    run_status: RunStatus
    failure_reason: str | None
    header_status: ResultStatus | None
    reject_reason: str | None
    model: str
    prompt_version: int
    input_kind: InputKind
    abstract_source: AbstractSource | None
    created_at: datetime
    is_eval: bool

    @property
    def outcome(self) -> str:
        """有表頭照表頭狀態，沒有照 run 狀態。"""
        return (self.header_status or self.run_status).value

    @property
    def reason(self) -> str | None:
        """拒收是表頭的原因代碼，失敗是 run 的原因代碼。"""
        if self.header_status is ResultStatus.REJECTED:
            return self.reject_reason
        return self.failure_reason if self.header_status is None else None


@dataclass(frozen=True)
class PaperAiReport:
    """`radar runs show` 的內容：三個 AI 區塊與歷次 run（依建立時間）。"""

    paper_id: uuid.UUID
    dois: tuple[str, ...]
    blocks: Mapping[AiProduct, AiBlock]
    history: tuple[AiHistoryRow, ...]

    def active_prompt_version(self, task: LlmTask) -> int | None:
        """這個工作現在 `active` 那一列的 prompt 版本（評估不算）；沒有回 None。"""
        for row in reversed(self.history):
            if row.task is task and row.header_status is ResultStatus.ACTIVE and not row.is_eval:
                return row.prompt_version
        return None


_STATE_LABELS: Final[Mapping[AiBlockState, str]] = {
    AiBlockState.READY: "有結果",
    AiBlockState.EMPTY: "跑過了，沒有可擷取的內容",
    AiBlockState.PENDING_INTERACTIVE: "互動執行中",
    AiBlockState.PENDING_BATCH: "排在 Batch（或等下一次排入）",
    AiBlockState.DEFERRED_BUDGET: "這個月的預算用完，延後",
    AiBlockState.NO_INPUT: "沒有摘要，不做",
    AiBlockState.FAILED: (
        "沒有產生成功（被拒收：radar runs retry 重跑；Batch 卡住：radar batches resolve）"
    ),
    AiBlockState.NOT_APPLICABLE: "這種論文不做",
}
_PRODUCT_LABELS: Final[Mapping[AiProduct, str]] = {
    AiProduct.ZH_SUMMARY: "中文重點",
    AiProduct.CLASSIFICATION: "分類",
    AiProduct.RESEARCH_MODEL: "研究模型",
}
_ZH_FIELDS: Final = (
    ("research_question", "研究問題"),
    ("method", "方法"),
    ("theory", "理論"),
    ("findings", "發現"),
)


def _value_lines(value: object) -> list[str]:
    if isinstance(value, ZhSummaryValue):
        return _zh_lines(value)
    if isinstance(value, ClassificationValue):
        return [
            _classification_line("主題", value.topic),
            _classification_line("方法", value.method),
        ]
    if isinstance(value, ResearchModelValue):
        return _research_model_lines(value)
    return []


def _zh_lines(value: ZhSummaryValue) -> list[str]:
    lines: list[str] = []
    reported = {field.value for field in value.reported_fields}
    for name, label in _ZH_FIELDS:
        text = getattr(value, name)
        shown = "—" if text is None else terminal_text(text)
        mark = "（有成員回報可能有誤）" if name in reported else ""
        lines.append(f"    {label}：{shown}{mark}")
    return lines


def _classification_line(label: str, value: ClassificationFieldValue | None) -> str:
    if value is None:
        return f"    {label}：—"
    keys = "、".join(value.keys) if value.keys else "—"
    extra = []
    if value.human_verified:
        extra.append("人工值")
    if value.members_disagree:
        extra.append("成員意見不同")
    suffix = f"（{'，'.join(extra)}）" if extra else ""
    return f"    {label}：{keys}{suffix}"


def _optional(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    return terminal_text(str(value))


def _research_model_lines(value: ResearchModelValue) -> list[str]:
    lines = [
        f"    文獻回顧或後設分析：{_optional(value.is_review_or_meta_analysis)}",
        f"    國家：{_optional(value.country)}；產業：{_optional(value.industry)}；"
        f"研究設計：{_optional(value.research_design)}；"
        f"分析單位：{_optional(value.unit_of_analysis)}；樣本數：{_optional(value.sample_size)}",
        f"    限制：{_optional(value.limitations)}",
        f"    未來研究：{_optional(value.future_research)}",
    ]
    names = {construct.construct_id: construct.name_en for construct in value.constructs}
    lines.append(f"    構面（{len(value.constructs)}）：")
    for construct in value.constructs:
        lines.append(
            f"      - {terminal_text(construct.name_en)}（{terminal_text(construct.label_zh)}）"
        )
    lines.append(f"    假設（{len(value.hypotheses)}）：")
    for hypothesis in value.hypotheses:
        cause = terminal_text(names.get(hypothesis.cause_construct_id, "?"))
        effect = terminal_text(names.get(hypothesis.effect_construct_id, "?"))
        lines.append(
            f"      - H{hypothesis.ordinal}：{cause} → {effect}"
            f"（{hypothesis.evidence_type.value}，{hypothesis.support.value}）"
        )
    return lines


def render_paper_ai_report(report: PaperAiReport) -> list[str]:
    """`radar runs show` 要印的字：三個區塊（狀態、有效值、來歷）與歷次 run。"""
    dois = "、".join(report.dois) if report.dois else "（沒有 DOI）"
    lines = [f"論文 {report.paper_id}（{dois}）"]
    for product in AiProduct:
        block = report.blocks[product]
        lines += [
            "",
            f"{_PRODUCT_LABELS[product]}：{block.state.value}（{_STATE_LABELS[block.state]}）",
        ]
        provenance = block.provenance
        if provenance is not None:
            if provenance.model is None:
                source = "人工（沒有 AI 值）"
            else:
                version = report.active_prompt_version(product.llm_task)
                shown = "—" if version is None else f"v{version}"
                input_kind = "—" if provenance.input_kind is None else provenance.input_kind.value
                source = f"模型 {provenance.model}、prompt {shown}、看 {input_kind}"
            verified = "是" if provenance.human_verified else "否"
            lines.append(
                f"  來歷：{source}、產生於 {_when(provenance.generated_at)}、人工核對：{verified}"
            )
        lines += _value_lines(block.value)
    lines += ["", "歷次 run（舊到新；台北時間）"]
    if not report.history:
        lines.append("  （沒有）")
        return lines
    body = [
        [
            _when(row.created_at),
            row.task.value,
            row.outcome + ("（評估）" if row.is_eval else ""),
            row.reason or "—",
            row.model,
            f"v{row.prompt_version}",
            row.input_kind.value,
            "—" if row.abstract_source is None else row.abstract_source.value,
        ]
        for row in report.history
    ]
    lines += render_table(
        ["時間", "工作", "結果", "原因代碼", "模型", "prompt", "輸入", "摘要來源"],
        body,
        align="llllllll",
    )
    return lines


# --- 重跑（`radar runs retry`） -------------------------------------------------------------


class RetryBlock(StrEnum):
    """這個工作為什麼不能重跑。"""

    NOT_APPLICABLE = "not_applicable"  # plan_for：這種論文不做
    NO_INPUT = "no_input"  # plan_for 或閘門：沒有要的內容
    BLOCKED = "blocked"  # 放行擋下
    TOO_LARGE = "too_large"  # 輸入超過上限
    HAS_ACTIVE = "has_active"  # 目前這組鍵已經有有效結果
    RUNNING = "running"  # 目前這組鍵正在跑
    NO_REJECTED = "no_rejected"  # 目前這組鍵最新的表頭不是 rejected（或還沒有結果、輸入已變）
    NOT_FOUND = "not_found"  # 論文不見了


_BLOCK_LABELS: Final[Mapping[RetryBlock, str]] = {
    RetryBlock.NOT_APPLICABLE: "這種論文不做這個工作",
    RetryBlock.NO_INPUT: "沒有這個工作要的內容（例如沒有摘要）",
    RetryBlock.BLOCKED: "輸入被放行規則擋下",
    RetryBlock.TOO_LARGE: "輸入超過長度上限",
    RetryBlock.HAS_ACTIVE: "目前的輸入已經有有效結果",
    RetryBlock.RUNNING: "目前的輸入正在跑",
    RetryBlock.NO_REJECTED: "目前的輸入沒有被拒收的結果（輸入或 prompt 變了會照平常流程重跑）",
    RetryBlock.NOT_FOUND: "找不到論文",
}


@dataclass(frozen=True)
class RetryPlanItem:
    """一個工作的重跑計畫。可以重跑時 `key`、`input_kind`、`rejected_id`、`max_cost_usd`
    （單次上限）都有值、`block` 是 None；不能重跑時只有 `block`（與 `detail` 原因代碼）。

    Raises:
        ValueError: 可以重跑卻缺欄位，或不能重跑卻帶了鍵。
    """

    task: LlmTask
    block: RetryBlock | None = None
    detail: str | None = None
    key: RunKey | None = None
    input_kind: InputKind | None = None
    rejected_id: uuid.UUID | None = None
    reject_reason: str | None = None
    max_cost_usd: Decimal | None = None

    def __post_init__(self) -> None:
        filled = (self.key, self.input_kind, self.rejected_id, self.max_cost_usd)
        if self.block is None and any(value is None for value in filled):
            raise ValueError("可以重跑的項目要有鍵、輸入種類、rejected id 與單次上限")
        if self.block is not None and any(value is not None for value in filled):
            raise ValueError("不能重跑的項目不帶鍵")

    @property
    def retryable(self) -> bool:
        return self.block is None


@dataclass(frozen=True)
class RetryPlan:
    """一篇論文的重跑計畫：`attempts` 是 adapter 每個請求最多送幾次（含第一次）。"""

    paper_id: uuid.UUID
    items: tuple[RetryPlanItem, ...]
    attempts: int

    @property
    def retryable(self) -> tuple[RetryPlanItem, ...]:
        return tuple(item for item in self.items if item.retryable)

    @property
    def max_cost_usd(self) -> Decimal:
        """最壞花費：可以重跑的項目的單次上限合計 × `attempts`。"""
        return sum((item.max_cost_usd or _ZERO for item in self.retryable), _ZERO) * self.attempts


def render_retry_plan(plan: RetryPlan, status: BudgetStatus) -> list[str]:
    """重跑前印的計畫、預估與本月預算。"""
    lines = [f"論文 {plan.paper_id}"]
    for item in plan.items:
        if item.retryable:
            lines.append(
                f"  {item.task.value}：會重跑（被拒收：{item.reject_reason or '—'}；"
                f"單次上限 {format_usd(item.max_cost_usd or _ZERO)} 美元）"
            )
        else:
            assert item.block is not None
            detail = f"（{item.detail}）" if item.detail else ""
            lines.append(f"  {item.task.value}：不能重跑：{_BLOCK_LABELS[item.block]}{detail}")
    if not plan.retryable:
        return lines
    lines += [
        f"預估：重跑 {len(plan.retryable)} 項，費用上限 {format_usd(plan.max_cost_usd)} 美元"
        f"（含重試的最壞情況：每項最多送 {plan.attempts} 次；互動價）",
        f"本月（{status.window.label}，台北時間）：已花 {format_usd(status.total_usd)}、"
        f"Batch 預留 {format_usd(status.reserved_usd)}、離熔斷線"
        f" {format_usd(status.remaining_usd)}（熔斷線 {format_usd(status.breaker_usd)}）",
    ]
    if status.breaker_tripped:
        lines.append("已熔斷：重跑都會是 deferred_budget（不呼叫、不寫列）")
    return lines


class RetryItemStatus(StrEnum):
    """一項重跑的結果。中間一段的值和 `services/llm_gateway.GatewayOutcome` 相同。"""

    ACTIVE = "active"
    CANDIDATE = "candidate"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    SKIPPED = "skipped"  # 鎖內再核對：已經有更新的表頭、有 active 或正在跑（不呼叫）
    BLOCKED = "blocked"
    NO_INPUT = "no_input"
    DEFERRED_BUDGET = "deferred_budget"
    TOO_LARGE = "too_large"
    TRANSIENT_FAILURE = "transient_failure"
    INPUT_CHANGED = "input_changed"  # 確認後輸入、prompt 或模型變了：沒有呼叫模型
    NOT_FOUND = "not_found"  # 論文在跑之前被刪了
    ERROR = "error"  # 非預期例外（只記類別名）


# 有這些結果時退出碼 1（沒有照確認的做完，或出錯）。
RETRY_FAILING: Final = frozenset(
    {
        RetryItemStatus.TRANSIENT_FAILURE,
        RetryItemStatus.INPUT_CHANGED,
        RetryItemStatus.DEFERRED_BUDGET,
        RetryItemStatus.BLOCKED,
        RetryItemStatus.NO_INPUT,
        RetryItemStatus.TOO_LARGE,
        RetryItemStatus.NOT_FOUND,
        RetryItemStatus.ERROR,
    }
)


@dataclass(frozen=True)
class RetryItem:
    """一項重跑的結果：`reason` 是拒收／失敗的原因代碼或錯誤類別；`cost_usd` 是這次新記的帳。"""

    task: LlmTask
    status: RetryItemStatus
    reason: str | None = None
    run_id: uuid.UUID | None = None
    cost_usd: Decimal | None = None
    http_status: int | None = None


@dataclass(frozen=True)
class RetryReport:
    """整次重跑的結果。"""

    paper_id: uuid.UUID
    items: tuple[RetryItem, ...]

    @property
    def cost_usd(self) -> Decimal:
        return sum((item.cost_usd or _ZERO for item in self.items), _ZERO)

    @property
    def exit_code(self) -> int:
        return 1 if any(item.status in RETRY_FAILING for item in self.items) else 0


def render_retry_report(report: RetryReport) -> list[str]:
    """重跑完印的結果：每項一行，最後是這次新記的帳。"""
    lines: list[str] = []
    for item in report.items:
        line = f"  {item.task.value}：{item.status.value}"
        if item.reason is not None:
            line += f"（{item.reason}）"
        if item.http_status is not None:
            line += f" HTTP {item.http_status}"
        if item.run_id is not None:
            line += f"  run {item.run_id}"
        lines.append(line)
    lines.append(f"這次新記的費用：{format_usd(report.cost_usd)} 美元")
    if any(item.status is RetryItemStatus.SKIPPED for item in report.items):
        lines.append("skipped：鎖內再核對時已經有更新的結果或正在跑（別的行程做了），沒有呼叫模型")
    return lines


# --- 依來源移除（`radar runs remove-by-source`） --------------------------------------------


@dataclass(frozen=True)
class SourceAiCounts:
    """送出時用了某個來源的摘要的 AI 產出列數
    （`repo/ai_outputs.count_ai_results_by_abstract_source`）。

    - `headers`：三個工作 × 四個狀態的表頭數（沒有的是 0）。
    - `details`：掛在那些表頭底下的細項數（`DETAIL_TABLES` 三張）。
    - `unknown_source`：看摘要（`input_kind='abstract'`）、但 run 沒記來源的表頭數（遷移 0018 前、
      摘要列已刪的）：不會被刪。
    - `constructs`：刪完這些表頭之後沒有其他人用的構面數（只被這個來源的結果用到、沒有其他來源
      的結果、代表或人工合併判斷用到；審查後第 2 點）。
    - `embeddings`：用這個來源的摘要算的向量數（`embedding.abstract_source`），加上看摘要卻沒記
      來源的向量（遷移 0018 前就有的，一律沒記來源，保守一起刪；審查後第二輪 R2、第三輪）。
    """

    headers: Mapping[LlmTask, Mapping[ResultStatus, int]]
    details: Mapping[str, int]
    unknown_source: int
    constructs: int = 0
    embeddings: int = 0

    @property
    def total_headers(self) -> int:
        return sum(sum(by_status.values()) for by_status in self.headers.values())

    def by_table(self) -> dict[str, int]:
        """各表會刪幾列（`REMOVAL_TABLES` 的順序）。"""
        counts = {
            HEADER_TABLES[task]: sum(by_status.values()) for task, by_status in self.headers.items()
        }
        counts.update(self.details)
        counts["construct"] = self.constructs
        counts["embedding"] = self.embeddings
        return {table: counts.get(table, 0) for table in REMOVAL_TABLES}


class RemovalBlock(StrEnum):
    """`--execute` 時不刪的原因（鎖內重查）。"""

    RUNNING_RUNS = "running_runs"  # 還有用這個來源的 running run：它回來會再寫一列
    CURRENT_ABSTRACTS = "current_abstracts"  # 有論文的目前摘要仍是這個來源：下次規劃會重新產生


def removal_blocks(running_runs: int, current_papers: int) -> tuple[RemovalBlock, ...]:
    """擋下的條件（沒有就是空的）。"""
    blocks: list[RemovalBlock] = []
    if running_runs:
        blocks.append(RemovalBlock.RUNNING_RUNS)
    if current_papers:
        blocks.append(RemovalBlock.CURRENT_ABSTRACTS)
    return tuple(blocks)


@dataclass(frozen=True)
class SourceRemovalPreview:
    """不加 `--execute` 時印的內容（只讀）。"""

    source: AbstractSource
    counts: SourceAiCounts
    running_runs: int
    current_papers: int

    @property
    def blocks(self) -> tuple[RemovalBlock, ...]:
        return removal_blocks(self.running_runs, self.current_papers)


@dataclass(frozen=True)
class SourceRemovalResult:
    """`--execute` 的結果：被擋下時 `blocks` 不是空的、什麼都沒刪；否則 `deleted` 是各表刪了幾列，
    有刪才有 `audit_event_id`。"""

    source: AbstractSource
    blocks: tuple[RemovalBlock, ...]
    running_runs: int
    current_papers: int
    deleted: Mapping[str, int] = field(default_factory=dict)
    audit_event_id: uuid.UUID | None = None

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())


def render_source_removal_preview(preview: SourceRemovalPreview) -> list[str]:
    """會刪幾筆（各表各狀態、細項）、還在跑的 run、目前摘要仍是這個來源的論文、來源不明的表頭。"""
    statuses = tuple(ResultStatus)
    body = [
        [
            HEADER_TABLES[task],
            *(f"{preview.counts.headers.get(task, {}).get(status, 0):,}" for status in statuses),
        ]
        for task in ADMIN_TASKS
    ]
    lines = [f"摘要來源 {preview.source.value} 衍生的 AI 產出（送出時用了這個來源的摘要）", ""]
    lines += render_table(
        ["表頭", *(status.value for status in statuses)], body, align="l" + "r" * len(statuses)
    )
    details = "、".join(
        f"{table} {preview.counts.details.get(table, 0):,}" for table in DETAIL_TABLES
    )
    lines += [
        "",
        f"會刪：表頭 {preview.counts.total_headers:,} 列；細項 {details}",
        f"另外會刪：只被這個來源用到的構面 {preview.counts.constructs:,}、"
        f"用這個來源（或來源不明）的摘要算的向量 {preview.counts.embeddings:,}（之後重算）",
        f"還在跑、用這個來源的 run：{preview.running_runs:,}",
        f"目前摘要仍是這個來源的論文：{preview.current_papers:,}",
        f"來源不明的表頭（遷移前或摘要列已刪，不會刪）：{preview.counts.unknown_source:,}",
    ]
    return lines


def render_removal_blocks(source: AbstractSource, blocks: Sequence[RemovalBlock]) -> list[str]:
    """被擋下時的原因與下一步。"""
    lines = ["沒有刪任何一列："]
    if RemovalBlock.RUNNING_RUNS in blocks:
        lines.append("- 還有用這個來源的 run 在跑：等它們結束（或 sweep 收掉）再跑一次")
    if RemovalBlock.CURRENT_ABSTRACTS in blocks:
        lines.append(
            f"- 還有論文的目前摘要是 {source.value}：先停用這個來源、移除它的摘要，再跑一次"
            "（不然下次規劃會用同一份摘要重新產生）"
        )
    return lines


def render_source_removal_result(result: SourceRemovalResult) -> list[str]:
    """刪完印的各表列數。"""
    if result.total_deleted == 0:
        return [f"摘要來源 {result.source.value} 沒有可刪的 AI 產出（沒有寫稽核）"]
    parts = "、".join(f"{table} {result.deleted.get(table, 0):,}" for table in REMOVAL_TABLES)
    return [
        f"已刪除摘要來源 {result.source.value} 衍生的 AI 產出：{parts}",
        f"稽核事件：{result.audit_event_id}",
    ]


def source_removal_audit(source: AbstractSource, deleted: Mapping[str, int]) -> NewAuditEvent:
    """依來源移除的稽核事件：系統身分（CLI 以 `SystemViewer` 執行，`member` 表在 3.3），`detail` 只
    有來源與各表刪了幾列。

    Raises:
        ValueError: 計數有不認得的表或不是 ≥ 0 的整數。
    """
    unknown = set(deleted) - set(REMOVAL_TABLES)
    if unknown:
        raise ValueError("計數裡有不認得的表")
    counts: dict[str, int] = {}
    for table in REMOVAL_TABLES:
        count = deleted.get(table, 0)
        if type(count) is not int or count < 0:
            raise ValueError(f"{table} 的計數要是 ≥ 0 的整數")
        counts[table] = count
    return NewAuditEvent(
        actor_role=AuditActorRole.SYSTEM,
        actor_member_id=None,
        action=SOURCE_REMOVAL_ACTION,
        target_type=SOURCE_REMOVAL_TARGET,
        target_id=None,
        detail={"source": AbstractSource(source).value, "counts": counts},
    )


# --- 改版預估（`radar runs estimate-revision`） ---------------------------------------------


@dataclass
class RevisionTaskEstimate:
    """一個工作：有現行結果、改版後會重跑的篇數與 Batch 價保守上限合計；還沒有結果的篇數。"""

    rerun: int = 0
    max_cost_usd: Decimal = _ZERO
    no_result: int = 0


@dataclass
class RevisionEstimate:
    """改版會重跑多少（`services/llm_batches.estimate_revision_reruns`）。`cap_usd` 是
    `config/llm_batches.yaml` 的 `revision_rerun_max_usd`。"""

    tasks: tuple[LlmTask, ...]
    cap_usd: Decimal
    by_task: dict[LlmTask, RevisionTaskEstimate] = field(default_factory=dict)
    # 算進「會重跑」的每一筆（這次的鍵 → Batch 價保守上限）。Batch 規劃只能排這份清單裡的改版
    # 重跑（Story 2.12 審查後第 4 點）。
    approved: dict[RunKey, Decimal] = field(default_factory=dict)

    def for_task(self, task: LlmTask) -> RevisionTaskEstimate:
        return self.by_task.setdefault(LlmTask(task), RevisionTaskEstimate())

    @property
    def max_cost_usd(self) -> Decimal:
        return sum((estimate.max_cost_usd for estimate in self.by_task.values()), _ZERO)

    @property
    def reruns(self) -> int:
        return sum(estimate.rerun for estimate in self.by_task.values())

    @property
    def exceeds_cap(self) -> bool:
        return self.max_cost_usd > self.cap_usd


_REVISION_LABELS: Final[Mapping[LlmTask, str]] = {
    LlmTask.CLASSIFY: "分類表改版 → classify",
    LlmTask.EXTRACT_MODEL: "情境值清單改版 → extract_model",
}


def render_revision_estimate(estimate: RevisionEstimate) -> list[str]:
    """各工作會重跑幾篇、Batch 價保守上限、還沒有結果的篇數、上限值；超過上限時加提示。"""
    body = []
    for task in estimate.tasks:
        found = estimate.for_task(task)
        body.append(
            [
                _REVISION_LABELS.get(task, task.value),
                f"{found.rerun:,}",
                format_usd(found.max_cost_usd),
                f"{found.no_result:,}",
            ]
        )
    lines = ["改版後下一次 Batch 規劃會重跑的篇數（有現行 AI 結果的；人工值不受影響）", ""]
    lines += render_table(
        ["改版", "會重跑（篇）", "保守上限（Batch 價，美元）", "還沒有結果（篇）"],
        body,
        align="lrrr",
    )
    lines += [
        "",
        f"合計保守上限：{format_usd(estimate.max_cost_usd)} 美元；"
        f"上限 revision_rerun_max_usd：{format_usd(estimate.cap_usd)} 美元",
    ]
    if estimate.exceeds_cap:
        lines.append(
            "超過上限：改版後 Batch 規劃會整次不排（PlanStop rerun_cap_exceeded）。先決定要不要調高"
            " config/llm_batches.yaml 的 revision_rerun_max_usd（0～50），再改設定檔"
        )
    return lines
