"""只對指定的論文跑 AI（`radar runs trial`，Story 2.10、AR-62、ADR-0042）的 domain 部分：
參數解析、抽樣、項目狀態、回應形狀的描述與要印的文字。不碰資料庫、不呼叫模型。

- **參數**：每個是論文 ID（UUID）或 DOI（任何 `normalize_doi` 看得懂的寫法）；去重後最多
  `MAX_TRIAL_PAPERS` 篇。看不懂就丟 `TrialArgumentError`（CLI 退出碼 2，不碰資料庫）。
- **名單檔**（`--save-ids`／`--ids-file`）：一行一個論文 ID；讀的時候空行與 `#` 開頭的行略過，
  每行照參數的規則解析（也收 DOI）。同一個 seed 只保證「同一時間點的候選」抽到同一批（新論文、
  摘要或排除狀態變了都會換名單），要重跑「同樣這幾篇」用存下的名單檔。
- **抽樣**：候選＝`plan_for` 三個 LLM 工作都要做（有摘要、沒被排除、不是手填書目）而且
  `split_for_dois` 是調整組；依 `trial_sample_sha256(seed, 最小的 DOI)` 排序取前 N（同分再比
  DOI）。不用 `random`：跨 Python 版本、跨資料庫都一樣，新增論文不會打亂既有論文之間的先後。
- **預估**：每項記確認時的 `RunKey`（輸入指紋、prompt 版本、模型）與單次呼叫的費用上限；最壞花費
  ＝單次上限 × adapter 每個請求最多送幾次（`attempts`，含第一次；逾時放棄的請求也可能計費）。
- **項目狀態**（`TrialItemStatus`）：計畫不做 → `not_applicable`／`no_input`；交給閘門 →
  `GatewayOutcome` 的值；另有 `not_found`（論文在跑之前被刪）、`error`（非預期例外，只記類別名）、
  `not_run`（設定錯停下後剩下的）、`input_changed`（確認後輸入變了，或確認時不會呼叫、現在會：
  沒有呼叫模型）。有 `transient_failure`／`error`／`not_run`／`input_changed` 時退出碼 1。
- **總表的「這次呼叫模型」**：只算這次有新 run、而且拿到回應的項目（`active`／`candidate`／
  `superseded`／`rejected`），拒收率的分母只算這些；`transient_failure`（送了請求、重試完還是
  沒拿到回應）另列「呼叫了但沒拿到回應」，不算測到。
- **輸出文字**：只印 id、DOI、狀態、原因代碼、token 數、金額與回應的「形狀」（鍵與 type 名稱，
  只收 `^[A-Za-z0-9_]{1,64}$`，其他寫 `<?>`），不印論文內容。只有 `--show-output` 才印模型輸出：
  `json.dumps(ensure_ascii=False)`，控制字元與格式字元一律跳脫，最多 `SHOW_OUTPUT_MAX_CHARS` 字。
"""

import json
import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

from radar.domain.ai_outputs import LlmTask, RunKey
from radar.domain.budget import BudgetStatus, format_usd
from radar.domain.eval_split import EvalSplit, split_for_dois
from radar.domain.fingerprints import trial_sample_sha256
from radar.domain.identifiers import InvalidIdentifierError, normalize_doi
from radar.domain.processing import ProcessingFacts, ProcessingTask, plan_for
from radar.domain.text_table import render_table

# 一次最多幾篇（指定或抽樣）。
MAX_TRIAL_PAPERS: Final = 100
# 試跑的三個 LLM 工作，依這個順序逐篇跑。
TRIAL_TASKS: Final = (LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL)
# `--show-output` 每項最多印幾個字（跳脫之後算）：研究模型的 max_tokens 2560 約 1 萬字，
# 要整份看得到。
SHOW_OUTPUT_MAX_CHARS: Final = 20_000
# 跳脫後的文字切成「一個跳脫序列或一個字元」：截斷只切在這些單位之間。
_ESCAPED_UNIT: Final = re.compile(r"\\u[0-9a-fA-F]{4}|\\.|.", re.DOTALL)
# 回應形狀裡的鍵與 type：只收這種名稱，其他寫 `<?>`（不讓回應的內容混進終端機與日誌）。
_SHAPE_NAME: Final = re.compile(r"[A-Za-z0-9_]{1,64}")
_UNKNOWN: Final = "<?>"
# response_id、request_id：供應商的識別碼；不像的寫 `<?>`。
_PROVIDER_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TRIAL_QUANTUM: Final = Decimal("0.000001")
_ZERO: Final = Decimal(0)
_SPLIT_LABEL: Final = {EvalSplit.TUNING: "調整組", EvalSplit.TEST: "測試組"}


class TrialArgumentError(ValueError):
    """`radar runs trial` 的參數看不懂（CLI 退出碼 2，不碰資料庫）。訊息只帶截短的原值。"""


# --- 參數 ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrialTargets:
    """解析好的指定清單：`targets` 是去重後、照參數順序的論文 ID（`uuid.UUID`）或正規化後的
    DOI（`str`）。"""

    targets: tuple[uuid.UUID | str, ...]

    @property
    def paper_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(target for target in self.targets if isinstance(target, uuid.UUID))

    @property
    def dois(self) -> tuple[str, ...]:
        return tuple(target for target in self.targets if isinstance(target, str))


def _shown(value: str) -> str:
    return repr(value if len(value) <= 80 else value[:80] + "…")


def parse_trial_targets(values: Sequence[str]) -> TrialTargets:
    """把指令的參數解析成論文 ID 與 DOI：去頭尾空白、UUID 先試、不是 UUID 再當 DOI 正規化；
    重複的（同一個 UUID、同一個 DOI 的不同寫法）只留第一個。

    Raises:
        TrialArgumentError: 沒有參數、有參數既不是 UUID 也不是 DOI，或去重後超過
            `MAX_TRIAL_PAPERS` 個。
    """
    if not values:
        raise TrialArgumentError("沒有指定論文")
    targets: list[uuid.UUID | str] = []
    seen: set[uuid.UUID | str] = set()
    unreadable: list[str] = []
    for raw in values:
        text = raw.strip()
        target: uuid.UUID | str
        try:
            target = uuid.UUID(text)
        except ValueError:
            try:
                target = normalize_doi(text)
            except InvalidIdentifierError:
                unreadable.append(_shown(raw))
                continue
        if target not in seen:
            seen.add(target)
            targets.append(target)
    if unreadable:
        raise TrialArgumentError(f"看不懂的論文 ID 或 DOI：{'、'.join(unreadable)}")
    if len(targets) > MAX_TRIAL_PAPERS:
        raise TrialArgumentError(
            f"一次最多 {MAX_TRIAL_PAPERS} 篇（去掉重複後有 {len(targets)} 個）"
        )
    return TrialTargets(tuple(targets))


def parse_trial_ids_file(text: str) -> TrialTargets:
    """讀 `--ids-file` 的內容：一行一個論文 ID 或 DOI；空行與 `#` 開頭的行略過，其餘照
    `parse_trial_targets` 的規則（去重、最多 `MAX_TRIAL_PAPERS` 篇）。

    Raises:
        TrialArgumentError: 檔案裡沒有任何論文、有看不懂的行，或去重後超過上限。
    """
    lines = [line.strip() for line in text.splitlines()]
    values = [line for line in lines if line and not line.startswith("#")]
    if not values:
        raise TrialArgumentError("名單檔裡沒有論文")
    return parse_trial_targets(values)


def format_trial_ids(papers: Sequence[TrialPaper]) -> str:
    """`--save-ids` 寫的內容：照執行順序一行一個論文 ID（`parse_trial_ids_file` 讀得回來）。"""
    return "".join(f"{paper.paper_id}\n" for paper in papers)


def check_sample_size(size: int) -> None:
    """`--sample N` 的 N 要在 1～`MAX_TRIAL_PAPERS`。

    Raises:
        TrialArgumentError: 不在範圍內。
    """
    if type(size) is not int or not 1 <= size <= MAX_TRIAL_PAPERS:
        raise TrialArgumentError(f"--sample 要是 1～{MAX_TRIAL_PAPERS} 的整數")


# --- 選到的論文與抽樣 ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrialCandidate:
    """抽樣的候選：論文 id、正規化後排好序的 DOI、`plan_for` 三個 LLM 工作是不是都要做。"""

    paper_id: uuid.UUID
    dois: tuple[str, ...]
    runs_all_tasks: bool

    @classmethod
    def from_facts(
        cls, paper_id: uuid.UUID, dois: Iterable[str], facts: ProcessingFacts
    ) -> TrialCandidate:
        """由處理事實組候選（只留判斷結果，不留摘要內容）。

        Raises:
            InvalidIdentifierError: 有看不懂的 DOI。
        """
        plan = plan_for(facts)
        runs = all(plan.for_task(ProcessingTask(task.value)).runs for task in TRIAL_TASKS)
        return cls(paper_id, tuple(sorted({normalize_doi(doi) for doi in dois})), runs)

    @property
    def eligible(self) -> bool:
        """可以被抽：三個工作都要做，而且在調整組。"""
        return self.runs_all_tasks and split_for_dois(self.dois) is EvalSplit.TUNING


@dataclass(frozen=True)
class TrialSampleResult:
    """`sample_tuning_papers` 的結果：抽到的候選（依排序鍵）與符合條件的總數。"""

    selected: tuple[TrialCandidate, ...]
    eligible: int


def sample_tuning_papers(
    candidates: Iterable[TrialCandidate], size: int, seed: int
) -> TrialSampleResult:
    """從調整組、三個工作都要做的候選裡，依 `trial_sample_sha256(seed, 最小的 DOI)` 排序取前
    `size` 篇（同分再比 DOI）；不足 `size` 篇時全部取。和候選的輸入順序無關。

    Raises:
        TrialArgumentError: `size` 不在 1～`MAX_TRIAL_PAPERS`。
        ValueError: `seed` 不是整數。
    """
    check_sample_size(size)
    if type(seed) is not int:
        raise ValueError("seed 要是整數")
    eligible = [candidate for candidate in candidates if candidate.eligible]
    # 調整組一定有 DOI（沒有 DOI 是測試組），所以 dois[0]（最小的）一定在。
    eligible.sort(
        key=lambda candidate: (trial_sample_sha256(seed, candidate.dois[0]), candidate.dois)
    )
    return TrialSampleResult(selected=tuple(eligible[:size]), eligible=len(eligible))


@dataclass(frozen=True)
class TrialPaper:
    """一篇要試跑的論文：id、正規化後的 DOI、組別。"""

    paper_id: uuid.UUID
    dois: tuple[str, ...]
    split: EvalSplit


@dataclass(frozen=True)
class TrialSample:
    """抽樣參數與結果的摘要：seed、要幾篇、調整組裡符合條件的有幾篇。"""

    seed: int
    requested: int
    eligible: int


@dataclass(frozen=True)
class TrialSelection:
    """這次要跑的論文（照參數順序或抽樣排序）；抽樣時 `sample` 有值。"""

    papers: tuple[TrialPaper, ...]
    sample: TrialSample | None = None


def trial_paper(paper_id: uuid.UUID, dois: Iterable[str]) -> TrialPaper:
    """由 id 與 DOI 組 `TrialPaper`（算組別）。

    Raises:
        InvalidIdentifierError: 有看不懂的 DOI。
    """
    normalized = tuple(sorted({normalize_doi(doi) for doi in dois}))
    return TrialPaper(paper_id, normalized, split_for_dois(normalized))


# --- 預估 ---------------------------------------------------------------------------------


def _check_attempts(attempts: int) -> None:
    if type(attempts) is not int or attempts < 1:
        raise ValueError("attempts 要是至少 1 的整數")


@dataclass(frozen=True)
class TrialPlanItem:
    """一項會交給閘門的工作的預估：單次呼叫的保守費用上限（`max_cost_usd`）、估計輸入 token、
    `max_tokens`、送出的字元數（系統提示、文件內容、JSON schema）、adapter 每個請求最多送幾次
    （`attempts`，含第一次），以及確認時的 `RunKey`（`key`；執行時只有算出同一個鍵才呼叫，沒有鍵
    的項目不呼叫）。

    Raises:
        ValueError: `attempts` 不是至少 1 的整數。
    """

    paper_id: uuid.UUID
    task: LlmTask
    max_cost_usd: Decimal
    estimated_input_tokens: int
    max_tokens: int
    system_chars: int
    content_chars: int
    schema_chars: int
    attempts: int = 1
    key: RunKey | None = None

    def __post_init__(self) -> None:
        _check_attempts(self.attempts)

    @property
    def worst_cost_usd(self) -> Decimal:
        """含重試的最壞花費：單次上限 × `attempts`（逾時放棄的請求也可能計費、不進帳本）。"""
        return self.max_cost_usd * self.attempts


@dataclass(frozen=True)
class TrialEstimate:
    """執行前的預估：`plan_for` 要做、放行與大小都過了的項目（已有結果的也算，保守）。
    `attempts` 是 adapter 每個請求最多送幾次（含第一次），每一項的 `attempts` 都要一樣。

    Raises:
        ValueError: `attempts` 不是至少 1 的整數，或有項目的 `attempts` 不一樣。
    """

    items: tuple[TrialPlanItem, ...]
    attempts: int = 1

    def __post_init__(self) -> None:
        _check_attempts(self.attempts)
        if any(item.attempts != self.attempts for item in self.items):
            raise ValueError("每一項的 attempts 要和預估的一樣")

    @property
    def max_cost_usd(self) -> Decimal:
        """含重試的最壞花費合計（每項單次上限 × `attempts`）。"""
        return sum((item.worst_cost_usd for item in self.items), _ZERO)

    def for_item(self, paper_id: uuid.UUID, task: LlmTask) -> TrialPlanItem | None:
        for item in self.items:
            if item.paper_id == paper_id and item.task is LlmTask(task):
                return item
        return None


# --- 執行結果 -----------------------------------------------------------------------------


class TrialItemStatus(StrEnum):
    """一篇論文一個工作的結果。中間一段的值和 `services/llm_gateway.GatewayOutcome` 相同
    （`tests/unit/test_trial_runs.py` 核對兩邊一致）。"""

    NOT_APPLICABLE = "not_applicable"  # plan_for：這種論文不做這項工作
    NO_INPUT = "no_input"  # plan_for 或閘門：沒有這項工作要的內容
    ACTIVE = "active"
    CANDIDATE = "candidate"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    DEFERRED_BUDGET = "deferred_budget"
    TOO_LARGE = "too_large"
    TRANSIENT_FAILURE = "transient_failure"
    NOT_FOUND = "not_found"  # 論文在跑之前被刪了
    ERROR = "error"  # 非預期例外（只記類別名）
    NOT_RUN = "not_run"  # 金鑰或設定錯停下後，剩下沒跑的
    INPUT_CHANGED = "input_changed"  # 確認後輸入變了（或確認時不呼叫、現在會）：沒有呼叫模型


# 有這些狀態時退出碼 1。
FAILING_STATUSES: Final = frozenset(
    {
        TrialItemStatus.TRANSIENT_FAILURE,
        TrialItemStatus.ERROR,
        TrialItemStatus.NOT_RUN,
        TrialItemStatus.INPUT_CHANGED,
    }
)


# 這次呼叫模型而且拿到回應的狀態（總表「這次呼叫模型」與拒收率的分母只算這些）。
RESPONDED_STATUSES: Final = frozenset(
    {
        TrialItemStatus.ACTIVE,
        TrialItemStatus.CANDIDATE,
        TrialItemStatus.SUPERSEDED,
        TrialItemStatus.REJECTED,
    }
)


@dataclass(frozen=True)
class LlmCallDiagnostics:
    """一筆 `llm_call` 的診斷欄位（`--verbose`、`--show-output` 用）。`raw_response` 只拿來描述
    形狀；`output_text` 只在 `--show-output` 印（被拒收時的模型原文）。"""

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    stop_reason: str | None
    price_tier: str
    response_id: str | None
    request_id: str | None
    raw_response: Mapping[str, object] | None
    output_text: str | None
    output_reject_reason: str | None


@dataclass(frozen=True)
class TrialItem:
    """一篇論文一個工作的結果。

    - `reason`：原因代碼（擋下、拒收、暫時性失敗）或例外的類別名（`error`）。
    - `http_status`：金鑰或設定錯時的 HTTP 狀態碼（有的話）。
    - `cost_usd`：這次新記的帳（接續、略過、沒呼叫是 None）。
    - `elapsed_seconds`：這一項花了多久（`time.monotonic`，含 adapter 重試）；沒跑是 None。
    - `plan`：執行前的預估（`--verbose` 對照用）。
    - `diagnostics`：有 run 而且要求診斷時讀的 `llm_call`。
    - `output`：`--show-output` 要印的東西：驗證過的輸出（JSON 值），或被拒收時的模型原文
      （`output_is_raw` 為真）。
    """

    paper_id: uuid.UUID
    task: LlmTask
    status: TrialItemStatus
    reason: str | None = None
    http_status: int | None = None
    cost_usd: Decimal | None = None
    run_id: uuid.UUID | None = None
    resumed: bool = False
    elapsed_seconds: float | None = None
    plan: TrialPlanItem | None = None
    diagnostics: LlmCallDiagnostics | None = None
    output: object | None = None
    output_is_raw: bool = False

    @property
    def called_model(self) -> bool:
        """這一次有呼叫模型（有新的 run、不是接續）。略過、預算不准、擋下、沒跑、接續都不算。
        `transient_failure` 也算（送了請求，只是沒拿到回應），所以要再看 `got_response`。"""
        return self.run_id is not None and not self.resumed

    @property
    def got_response(self) -> bool:
        """這一次呼叫模型而且拿到回應（`active`／`candidate`／`superseded`／`rejected`）。
        總表「這次呼叫模型」與拒收率的分母只算這些；`transient_failure` 不算。"""
        return self.called_model and self.status in RESPONDED_STATUSES


@dataclass(frozen=True)
class TrialReport:
    """整次試跑的結果：論文（照執行順序）與每篇每種工作的項目。

    `stopped_by_configuration`：金鑰或設定錯而停下（其餘標 `not_run`）。
    """

    papers: tuple[TrialPaper, ...]
    items: tuple[TrialItem, ...]
    stopped_by_configuration: bool = False

    @property
    def cost_usd(self) -> Decimal:
        """這次新記的帳合計（接續與略過是 0）。"""
        return sum((item.cost_usd or _ZERO for item in self.items), _ZERO)

    @property
    def exit_code(self) -> int:
        return 1 if any(item.status in FAILING_STATUSES for item in self.items) else 0

    def rejected_by_reason(self) -> dict[str, int]:
        """被拒收的項目數，依原因代碼。"""
        counts = Counter(
            item.reason or "unknown"
            for item in self.items
            if item.status is TrialItemStatus.REJECTED
        )
        return dict(sorted(counts.items()))

    def called_items(self) -> tuple[TrialItem, ...]:
        """這一次有呼叫模型的項目（含沒拿到回應的 `transient_failure`）。"""
        return tuple(item for item in self.items if item.called_model)

    def responded_items(self) -> tuple[TrialItem, ...]:
        """這一次呼叫模型而且拿到回應的項目（總表「這次呼叫模型」與拒收率的分母只算這些）。"""
        return tuple(item for item in self.items if item.got_response)

    def no_response_by_status(self) -> dict[str, int]:
        """這一次呼叫了模型、卻沒拿到回應的項目數，依狀態（例如 `transient_failure`）。"""
        counts = Counter(
            item.status.value for item in self.items if item.called_model and not item.got_response
        )
        return dict(sorted(counts.items()))

    def not_called_by_status(self) -> dict[str, int]:
        """這一次沒有呼叫模型的項目數，依狀態（接續的另記 `resumed`）。"""
        counts = Counter(
            "resumed" if item.resumed else item.status.value
            for item in self.items
            if not item.called_model
        )
        return dict(sorted(counts.items()))


# --- 回應形狀 -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ResponseShape:
    """原始回應的形狀（只有名稱，沒有內容）。"""

    top_keys: tuple[str, ...]
    content_types: tuple[str, ...]
    usage_keys: tuple[str, ...]
    stop_details_keys: tuple[str, ...] | None
    stop_details_type: str | None
    stop_details_category: str | None


def _name(value: object) -> str:
    return value if isinstance(value, str) and _SHAPE_NAME.fullmatch(value) else _UNKNOWN


def _keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(sorted(_name(key) for key in value))


def describe_response_shape(raw: Mapping[str, object] | None) -> ResponseShape | None:
    """原始回應的形狀：頂層鍵、content 區塊的 type 序列、usage 的鍵、`stop_details` 的鍵與
    type／category。名稱不符合 `^[A-Za-z0-9_]{1,64}$` 的一律寫 `<?>`；沒有原始回應回 None。"""
    if raw is None:
        return None
    content = raw.get("content")
    content_types = (
        tuple(
            _name(block.get("type")) if isinstance(block, Mapping) else _UNKNOWN
            for block in content
        )
        if isinstance(content, list)
        else ()
    )
    details = raw.get("stop_details")
    if isinstance(details, Mapping):
        category = details.get("category")
        return ResponseShape(
            top_keys=_keys(raw),
            content_types=content_types,
            usage_keys=_keys(raw.get("usage")),
            stop_details_keys=_keys(details),
            stop_details_type=_name(details.get("type")),
            stop_details_category=None if category is None else _name(category),
        )
    return ResponseShape(
        top_keys=_keys(raw),
        content_types=content_types,
        usage_keys=_keys(raw.get("usage")),
        stop_details_keys=None,
        stop_details_type=None,
        stop_details_category=None,
    )


# --- 要印的文字 ---------------------------------------------------------------------------


def format_trial_usd(amount: Decimal) -> str:
    """試跑的金額：6 位小數（和 `llm_call.cost_usd` 一樣，單次呼叫常常不到 0.0001）。"""
    return f"{amount.quantize(_TRIAL_QUANTUM, rounding=ROUND_HALF_UP):f}"


def escape_for_terminal(value: object) -> str:
    """`json.dumps(ensure_ascii=False)`，再把剩下的控制字元、格式字元（例如雙向文字控制）與
    行／段分隔字元寫成 `\\uXXXX`，最多 `SHOW_OUTPUT_MAX_CHARS` 字（超過接 `…（截斷）`）。"""
    text = json.dumps(value, ensure_ascii=False)
    escaped = "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} else char
        for char in text
    )
    if len(escaped) <= SHOW_OUTPUT_MAX_CHARS:
        return escaped
    # 只切在跳脫序列之間（`\uXXXX`、`\"` 不會被切一半）。
    kept = 0
    for unit in _ESCAPED_UNIT.finditer(escaped):
        if unit.end() > SHOW_OUTPUT_MAX_CHARS:
            break
        kept = unit.end()
    return escaped[:kept] + "…（截斷）"


def _dois_text(dois: Sequence[str]) -> str:
    return ", ".join(dois) if dois else "（沒有 DOI）"


def render_trial_selection(selection: TrialSelection) -> list[str]:
    """選到的論文：抽樣參數（有的話）、每篇的 id、DOI、組別；測試組的每篇一行警告。"""
    lines: list[str] = []
    sample = selection.sample
    if sample is not None:
        lines.append(
            f"抽樣：seed {sample.seed}，從調整組符合條件的 {sample.eligible} 篇抽"
            f" {sample.requested} 篇"
        )
        if sample.eligible < sample.requested:
            lines.append(
                f"調整組符合條件的只有 {sample.eligible} 篇，少於 {sample.requested} 篇，全部抽出"
            )
    lines.append(f"選到的論文（{len(selection.papers)} 篇）：")
    body = [
        [str(index), str(paper.paper_id), _dois_text(paper.dois), _SPLIT_LABEL[paper.split]]
        for index, paper in enumerate(selection.papers, start=1)
    ]
    lines += render_table(["#", "論文 ID", "DOI", "組別"], body, align="rlll")
    for paper in selection.papers:
        if paper.split is EvalSplit.TEST:
            lines.append(f"警告：{paper.paper_id} 是測試組，結果不要拿來改提示詞")
    return lines


def render_trial_estimate(estimate: TrialEstimate, status: BudgetStatus) -> list[str]:
    """執行前的預估與本月預算狀態。"""
    lines = [
        f"預估：會交給閘門 {len(estimate.items)} 項，費用上限"
        f" {format_trial_usd(estimate.max_cost_usd)} 美元"
        f"（含重試的最壞情況：每項最多送 {estimate.attempts} 次；已經有結果、會略過的也算進去）",
        f"本月（{status.window.label}，台北時間）：已花 {format_usd(status.total_usd)}、"
        f"Batch 預留 {format_usd(status.reserved_usd)}、離熔斷線"
        f" {format_usd(status.remaining_usd)}（熔斷線 {format_usd(status.breaker_usd)}）",
    ]
    if status.breaker_tripped:
        lines.append("已熔斷：要呼叫模型的項目都會是 deferred_budget（不呼叫、不寫列）")
        return lines
    if estimate.max_cost_usd > status.remaining_usd:
        lines.append("預估上限超過離熔斷線的金額：後面的項目可能變成 deferred_budget")
    for task in TRIAL_TASKS:
        if status.task_exhausted(task):
            lines.append(f"{task.value} 的功能預算已用完：這項會是 deferred_budget")
    return lines


def _provider_id(value: str | None) -> str:
    if value is None:
        return "—"
    return value if _PROVIDER_ID.fullmatch(value) else _UNKNOWN


def _names(values: Sequence[str]) -> str:
    return "[" + ", ".join(values) + "]"


def _diagnostic_lines(item: TrialItem) -> list[str]:
    plan = item.plan
    lines: list[str] = []
    elapsed = "—" if item.elapsed_seconds is None else f"{item.elapsed_seconds:.2f} 秒"
    diagnostics = item.diagnostics
    if diagnostics is None:
        lines.append(f"    耗時 {elapsed}；沒有這次的 llm_call")
    else:
        estimated = "—" if plan is None else str(plan.estimated_input_tokens)
        max_tokens = "—" if plan is None else str(plan.max_tokens)
        lines.append(
            f"    耗時 {elapsed}；輸入 {diagnostics.input_tokens}／估計上限 {estimated} tokens；"
            f"輸出 {diagnostics.output_tokens}／max_tokens {max_tokens}；"
            f"快取寫入 {diagnostics.cache_creation_input_tokens}、"
            f"讀取 {diagnostics.cache_read_input_tokens}"
        )
        stop_reason = "—" if diagnostics.stop_reason is None else _name(diagnostics.stop_reason)
        lines.append(
            f"    stop_reason {stop_reason}；price_tier {_name(diagnostics.price_tier)}；"
            f"response_id {_provider_id(diagnostics.response_id)}；"
            f"request_id {_provider_id(diagnostics.request_id)}"
        )
        shape = describe_response_shape(diagnostics.raw_response)
        if shape is None:
            lines.append("    回應形狀：沒有存原始回應")
        else:
            if shape.stop_details_keys is None:
                details = "無"
            else:
                category = shape.stop_details_category or "null"
                details = (
                    f"鍵 {_names(shape.stop_details_keys)}、type {shape.stop_details_type}、"
                    f"category {category}"
                )
            lines.append(
                f"    回應形狀：頂層鍵 {_names(shape.top_keys)}；content 區塊"
                f" {_names(shape.content_types)}；usage 鍵 {_names(shape.usage_keys)}；"
                f"stop_details {details}"
            )
    if plan is not None:
        lines.append(
            f"    送出字元：系統提示 {plan.system_chars}、內容 {plan.content_chars}、"
            f"schema {plan.schema_chars}"
        )
        lines.append(
            f"    費用上限：單次 {format_trial_usd(plan.max_cost_usd)} 美元；含重試的最壞情況"
            f" {format_trial_usd(plan.worst_cost_usd)} 美元（最多送 {plan.attempts} 次）"
        )
    return lines


def render_trial_item(
    index: int, total: int, item: TrialItem, *, verbose: bool, show_output: bool
) -> list[str]:
    """做完一項印的行：編號、論文、工作、狀態（原因）、這次的費用；`verbose` 加診斷，
    `show_output` 加輸出。"""
    parts = [f"[{index}/{total}]", str(item.paper_id), item.task.value, item.status.value]
    if item.reason is not None:
        parts.append(f"（{item.reason}）")
    if item.http_status is not None:
        parts.append(f"HTTP {item.http_status}")
    if item.resumed:
        parts.append("接續")
    if item.cost_usd is not None:
        parts.append(f"{format_trial_usd(item.cost_usd)} 美元")
    lines = [" ".join(parts)]
    if verbose:
        lines += _diagnostic_lines(item)
    if show_output:
        if item.output is not None:
            label = "模型原文（拒收）" if item.output_is_raw else "輸出"
            lines.append(f"    {label}：{escape_for_terminal(item.output)}")
        elif item.status is TrialItemStatus.SKIPPED:
            lines.append(
                "    這一版 prompt 已經有結果，這次沒有新的輸出（要並排比較得先升 prompt 版本）"
            )
        elif item.status is TrialItemStatus.REJECTED:
            lines.append("    沒有存下模型原文（拒答、截斷或存不進資料庫）")
    return lines


def render_trial_summary(report: TrialReport, *, verbose: bool) -> list[str]:
    """結束時的總表：每篇每種工作的狀態、拒收統計（依原因代碼）、這次的費用與提醒；`verbose`
    加每個工作的最長耗時與最大 output／max_tokens 比例。"""
    status_of = {(item.paper_id, item.task): item.status.value for item in report.items}
    body = [
        [str(paper.paper_id), _SPLIT_LABEL[paper.split]]
        + [status_of.get((paper.paper_id, task), "—") for task in TRIAL_TASKS]
        for paper in report.papers
    ]
    lines = [f"結果（{len(report.papers)} 篇 × {len(TRIAL_TASKS)} 種工作）："]
    lines += render_table(
        ["論文 ID", "組別", *(task.value for task in TRIAL_TASKS)],
        body,
        align="l" * (2 + len(TRIAL_TASKS)),
    )
    rejected = report.rejected_by_reason()
    if rejected:
        lines.append(
            "拒收：" + "、".join(f"{reason} {count} 項" for reason, count in rejected.items())
        )
    else:
        lines.append("拒收：沒有")
    responded = report.responded_items()
    responded_rejected = sum(1 for item in responded if item.status is TrialItemStatus.REJECTED)
    no_response = report.no_response_by_status()
    not_called = report.not_called_by_status()
    line = f"這次呼叫模型：{len(responded)} 項（其中拒收 {responded_rejected} 項）"
    if no_response:
        line += f"；呼叫了但沒拿到回應：{sum(no_response.values())} 項（{_breakdown(no_response)}）"
    line += f"；沒有呼叫模型：{sum(not_called.values())} 項"
    if not_called:
        line += f"（{_breakdown(not_called)}）"
    lines.append(line)
    lines.append(f"這次費用：{format_trial_usd(report.cost_usd)} 美元（接續與略過是 0）")
    if verbose:
        lines += _verbose_summary(report)
    statuses = {item.status for item in report.items}
    if TrialItemStatus.ERROR in statuses:
        lines.append("有項目出錯：這次費用可能少算，以 radar costs 為準")
    if TrialItemStatus.TRANSIENT_FAILURE in statuses:
        lines.append("有暫時性失敗：直接重跑同一個指令，做完的不會重付")
    if report.stopped_by_configuration:
        lines.append("金鑰或設定錯誤，已停下；剩下的標 not_run")
    changed = [item for item in report.items if item.status is TrialItemStatus.INPUT_CHANGED]
    if changed:
        lines.append(
            f"確認後輸入變了、沒有呼叫模型：{len(changed)} 項（重跑同一個指令會重新預估、確認）"
        )
        lines += [f"  {item.paper_id} {item.task.value}" for item in changed]
    return lines


def _breakdown(counts: Mapping[str, int]) -> str:
    return "、".join(f"{status} {count}" for status, count in counts.items())


def _verbose_summary(report: TrialReport) -> list[str]:
    lines: list[str] = []
    for task in TRIAL_TASKS:
        items = [item for item in report.items if item.task is task]
        elapsed = [item.elapsed_seconds for item in items if item.elapsed_seconds is not None]
        ratios = [
            item.diagnostics.output_tokens / item.plan.max_tokens
            for item in items
            if item.diagnostics is not None and item.plan is not None and item.plan.max_tokens
        ]
        longest = f"{max(elapsed):.2f} 秒" if elapsed else "—"
        ratio = f"{max(ratios):.0%}" if ratios else "—"
        lines.append(f"{task.value}：最長耗時 {longest}；最大 output／max_tokens {ratio}")
    return lines
