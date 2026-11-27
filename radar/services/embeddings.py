"""向量化：全專案唯一寫 `embedding` 的服務（PRD FR-5、FR-9，AD-12、AD-29、AD-30，ADR-0005、
ADR-0040，Story 2.8）。向量化不經 LLM 閘門。

`embed_papers` 依論文 id 順序一頁一頁看全部論文，每篇照固定順序判斷，前一步擋下就不做後面的：

1. `plan_for(論文).for_task(embed)`：不做 → `not_applicable`（被排除的非研究文章）。
2. 組輸入（`build_embedding_input`：標題＋空行＋目前的摘要；計畫說只有標題時只送標題）；
   沒有內容 → `no_input`。
3. 算 `input_sha256`（`domain/fingerprints.py`）；和這個模型現有向量的一樣 → `unchanged`，不送。
4. 每一段照資料列推出的來源經 `clear_for_cloud` 放行，再用 `join_cloud_safe_texts` 接起來；
   擋下 → `blocked`（付費全文、還沒授權的來源不送雲端）。

湊滿一批（`batch_size`，最多 32）或看完時送一批：

5. 額度占用（短交易，拿額度鎖）：今天（UTC 00:00 起）`api_call` 記的估計 token、其他 worker
   占用中（還沒回應）的估計 token，加上這一批的估計，換成 neurons 超過
   `daily_free_neurons × stop_at_ratio` 就停下（`quota_reached`，記警告、不算失敗、不寫任何列），
   留到下一次續跑（下一次每週更新排入的工作，或 Kevin 再跑一次回填指令）；放得下就新增一筆占用
   （第一次嘗試用）。日期在拿到額度鎖之後才讀時鐘決定。等額度鎖逾時 → 這一批算失敗
   （`embed_quota_lock_timeout`，還沒有 run）、不送，其他論文照做。
6. 取得執行資格（每篇一個短交易，立刻 commit，等鎖最多 `lock_timeout_seconds`）：先鎖住論文那一列
   再重讀；別的工作已經寫好同一份輸入 → `unchanged`，輸入又變了 → `input_changed`，都不是才新增
   一筆 `running` 的 `derivation_run`（task＝embed）；同一份輸入另一個工作正在跑（部分唯一索引）
   或等鎖逾時 → `busy`。這幾種都不送，也不算進 `limit`（重送時略過的不再扣，見 `_split`）。
7. 呼叫 `provider.embed_documents`（不開著交易）。adapter 每次 HTTP 嘗試（含內部重試）前後回報
   （`report_attempts_to`）：第一次嘗試前在額度鎖內確認日期，和第 5 步同一個 UTC 日期就用第 5 步的
   占用；跨過 UTC 午夜了就在同一個交易裡放掉舊占用、照新的一天重查並占用。之後每次重試前再占一次。
   放不下 → `EmbeddingQuotaReachedError`，這批的 run 改成 `failed`（`embed_quota_reached`）、停下
   `quota_reached`；等額度鎖逾時 → 這批的 run 改成 `failed`（`embed_quota_lock_timeout`），其他
   論文照做。每次嘗試後在同一個短交易裡刪掉占用、記一筆 `api_call`（`workers_ai`、那次嘗試當下的
   時間與 UTC 日期、估計 token；失敗也照記，設定錯記 0）；adapter 回報「沒送出」時只放掉占用。
   不回報嘗試的 provider（替身）整次呼叫當成一次嘗試記帳。
8. 驗證向量（筆數、維度＝登記的維度、數字有限且在 halfvec 範圍內）；不符整批不寫。轉成 halfvec 後
   全是 0 的那一篇單獨標成失敗（`embed_response_invalid`）、不寫，其他篇照寫。
9. 寫入（每篇一個短交易，等鎖最多 `lock_timeout_seconds`）：看摘要的先拿摘要來源的共用鎖
   （依來源移除拿獨佔，Story 2.12 審查後），再鎖住論文那一列，重讀、重算 `input_sha256` 並核對
   摘要來源；變了就把 run 改成 `failed`（`embed_input_changed`），run 已經被 sweep 收掉也
   不寫，等鎖逾時把 run 改成 `failed`（`embed_lock_timeout`），都對才把 run 改成 `succeeded` 並覆寫
   `embedding`（主鍵（論文, 模型），一篇一個模型只有一列；連同用到的摘要來源）。

兩個工作同時跑（例如 Procrastinate 把還活著的 worker 誤判失聯、工作被另一個 worker 領走）也只有
一份有效的結果：取得資格與寫入都先鎖同一篇論文，每週更新改論文時也先鎖它，所以「核對指紋 → 寫入」
之間不會被插隊；同一份輸入只有一個工作拿得到資格，舊輸入算的向量蓋不掉新的。

失敗：暫時性錯誤（含總時限到了）、回應不對 → 這一批的 run 改成 `failed`（帶原因代碼），整批不寫、
停下（`failed`）。請求不合法（4xx）→ 多篇的一批改成一篇一篇重送，單獨一篇還是被拒就標成失敗、這次
略過，其他照做（不停下）。設定錯（401、403、404）→ 同樣收尾後原樣丟出。寫入或其他非預期的錯 →
盡量把還在跑的 run 改成 `failed`（`embed_failed`）後原樣丟出。還停在 `running` 的 run 由 sweep
照互動呼叫的時限收掉。有任何一篇標成失敗時，其他論文照做，但結果的 `has_failures` 是 True
（CLI 結束碼 1、背景工作記成失敗）。

`estimate_embedding_backlog` 走同樣的 1～4 步，只用本地資料估還要做幾篇、幾個 token，不送任何請求。
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog

from radar.domain.embeddings import (
    EMBED_DOCUMENTS_OPERATION,
    EMBED_QUERY_OPERATION,
    EMBED_SEPARATOR,
    AttemptHooks,
    EmbeddingApiCall,
    EmbeddingAttempt,
    EmbeddingBacklog,
    EmbeddingCallOutcome,
    EmbeddingCandidate,
    EmbeddingConfig,
    EmbeddingConfigurationError,
    EmbeddingError,
    EmbeddingInput,
    EmbeddingQuotaReachedError,
    EmbeddingRequestError,
    EmbedRunResult,
    EmbedStop,
    NewEmbedding,
    NewEmbedRun,
    NoEmbeddingInputError,
    build_embedding_input,
    check_embedding_vectors,
    embedding_call_outcome,
    estimate_embedding_tokens,
    is_zero_at_halfvec,
    quota_allows,
    report_attempts_to,
)
from radar.domain.errors import ErrorCode, LockWaitTimeoutError
from radar.domain.fingerprints import abstract_source_lock_key, embedding_input_sha256
from radar.domain.papers import AbstractSource
from radar.domain.permissions import Action, require
from radar.domain.policy import (
    CloudClearanceError,
    CloudPolicy,
    CloudSafeText,
    clear_for_cloud,
    cloud_text,
    join_cloud_safe_texts,
)
from radar.domain.processing import ProcessingTask, plan_for
from radar.domain.retrieval import SearchEmbeddingUnavailableError, SearchQuotaExhaustedError
from radar.domain.viewer import SystemViewer, Viewer
from radar.ports import EmbeddingProvider
from radar.repo.api_calls import (
    create_embedding_api_call,
    create_embedding_quota_hold,
    delete_embedding_quota_hold,
    delete_expired_embedding_quota_holds,
    get_embedding_quota_usage,
)
from radar.repo.db import EMBEDDING_QUOTA_LOCK_KEY, Database
from radar.repo.embeddings import (
    find_embedding_candidate_for_update,
    find_embedding_model_dimensions,
    list_embedding_candidates,
    upsert_embeddings,
)
from radar.repo.llm_ledger import (
    create_embed_derivation_run,
    update_derivation_run_failed,
    update_derivation_run_succeeded,
)

log = structlog.get_logger(__name__)

DEFAULT_PAGE_SIZE = 500


class EmbeddingModelMismatchError(Exception):
    """設定、provider 與 `embedding_model` 的模型或維度對不上（還沒送任何請求、沒寫任何列）。"""


@dataclass(frozen=True, slots=True)
class _Planned:
    """一篇要向量化的論文：輸入與指紋（放行之前）。"""

    paper_id: uuid.UUID
    embedding_input: EmbeddingInput
    sha256: str


@dataclass(frozen=True, slots=True)
class _Item:
    """放行過、等著送出的一篇。"""

    planned: _Planned
    text: CloudSafeText


@dataclass
class _Tally:
    embedded: int = 0
    estimated_tokens: int = 0
    requests: int = 0
    selected: int = 0
    not_applicable: int = 0
    unchanged: int = 0
    no_input: int = 0
    blocked: int = 0
    busy: int = 0
    input_changed: int = 0
    failed: int = 0
    failure_reason: ErrorCode | None = None
    # 挑中後在第一次取得執行資格時才略過的（別的工作做好了、輸入又變了、正在跑）：不算進 `limit`。
    # 重送（`_split`）時才略過的不算在這裡：那些已經送出過一次，仍算這次送的篇數。
    skipped_at_claim: int = 0

    def reached(self, limit: int | None) -> bool:
        """這次真的要送的篇數到 `limit` 了嗎（None＝不限）。"""
        return limit is not None and self.selected - self.skipped_at_claim >= limit

    def result(self, stop: EmbedStop) -> EmbedRunResult:
        return EmbedRunResult(
            stop=stop,
            embedded=self.embedded,
            estimated_tokens=self.estimated_tokens,
            requests=self.requests,
            not_applicable=self.not_applicable,
            unchanged=self.unchanged,
            no_input=self.no_input,
            blocked=self.blocked,
            busy=self.busy,
            input_changed=self.input_changed,
            failed=self.failed,
            failure_reason=self.failure_reason,
        )


def embed_papers(
    actor: Viewer,
    database: Database,
    provider: EmbeddingProvider,
    config: EmbeddingConfig,
    cloud_policy: CloudPolicy,
    *,
    code_version: str,
    clock: Callable[[], datetime],
    limit: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> EmbedRunResult:
    """把還沒有向量（或輸入變了）的論文向量化，直到做完、到 `limit` 篇或今天的額度比例
    （見模組說明）。

    `limit`：這次最多送幾篇（None＝不限）；`clock` 給額度算 UTC 日期與記 `api_call` 的時間用。

    Raises:
        PermissionDeniedError: `actor` 不是系統工作（不碰資料庫）。
        ValueError: `limit`、`page_size` 不是正整數，`code_version` 是空的，或時鐘回了沒有時區的
            時間。
        EmbeddingModelMismatchError: provider 的模型和設定不同、模型沒有登記在 `embedding_model`，
            或登記的維度和設定不同（沒送任何請求）。
        EmbeddingConfigurationError: token、帳號或模型設定不對（這一批的 run 已改成 `failed`）。
        Exception: 寫入或其他非預期的錯（run 盡量改成 `failed`），原樣往上丟。
    """
    require(actor, Action.RUN_AI_JOBS, None)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit 要是正整數或 None")
    if type(page_size) is not int or page_size < 1:
        raise ValueError("page_size 要是正整數")
    if not isinstance(code_version, str) or not code_version:
        raise ValueError("code_version 不能是空的")
    viewer = SystemViewer()
    dimensions = _check_model(database, provider, config)
    tally = _Tally()
    pending: list[_Item] = []
    stop: EmbedStop | None = None
    after_id: uuid.UUID | None = None
    run = _BatchRunner(
        database, provider, config, dimensions, code_version=code_version, clock=clock, tally=tally
    )
    while stop is None:
        with database.transaction() as connection:
            page = list_embedding_candidates(
                viewer, connection, config.model_id, after_id=after_id, limit=page_size
            )
        if not page:
            break
        after_id = page[-1].source.paper_id
        for candidate in page:
            planned = _plan(candidate, tally)
            if planned is None:
                continue
            text = _clear(planned, cloud_policy, tally)
            if text is None:
                continue
            pending.append(_Item(planned, text))
            tally.selected += 1
            if len(pending) >= config.batch_size or tally.reached(limit):
                stop = run(pending)
                pending = []
                if stop is None and tally.reached(limit):
                    stop = EmbedStop.LIMIT_REACHED
            if stop is not None:
                break
    if stop is None and pending:
        stop = run(pending)
    result = tally.result(stop or EmbedStop.COMPLETED)
    log.info(
        "向量化結束",
        has_failures=result.has_failures,
        model_id=config.model_id,
        stop=result.stop.value,
        embedded=result.embedded,
        estimated_tokens=result.estimated_tokens,
        requests=result.requests,
        unchanged=result.unchanged,
        not_applicable=result.not_applicable,
        no_input=result.no_input,
        blocked=result.blocked,
        busy=result.busy,
        input_changed=result.input_changed,
        failed=result.failed,
        failure_reason=None if result.failure_reason is None else result.failure_reason.value,
    )
    return result


def estimate_embedding_backlog(
    viewer: SystemViewer,
    database: Database,
    config: EmbeddingConfig,
    cloud_policy: CloudPolicy,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> EmbeddingBacklog:
    """還要建（或重算）向量的論文篇數與估計 token：只讀本地資料，不送任何請求、不寫任何列。

    判斷照 `embed_papers` 的 1～4 步（`plan_for`、有沒有內容、指紋有沒有變、放不放行）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: `page_size` 不是正整數。
    """
    if not isinstance(viewer, SystemViewer):
        raise TypeError("只給 SystemViewer")
    if type(page_size) is not int or page_size < 1:
        raise ValueError("page_size 要是正整數")
    tally = _Tally()
    papers = 0
    tokens = 0
    after_id: uuid.UUID | None = None
    while True:
        with database.transaction() as connection:
            page = list_embedding_candidates(
                viewer, connection, config.model_id, after_id=after_id, limit=page_size
            )
        if not page:
            break
        after_id = page[-1].source.paper_id
        for candidate in page:
            planned = _plan(candidate, tally)
            if planned is None:
                continue
            try:
                for part, source in planned.embedding_input.parts:
                    clear_for_cloud(part, source, None, cloud_policy)
            except CloudClearanceError:
                tally.blocked += 1
                continue
            papers += 1
            tokens += estimate_embedding_tokens(
                [planned.embedding_input.text], config.token_estimate
            )
    return EmbeddingBacklog(papers=papers, estimated_tokens=tokens, blocked=tally.blocked)


# --- 各步驟 ---------------------------------------------------------------------------------


def check_embedding_model(database: Database, config: EmbeddingConfig) -> int:
    """設定檔的模型有登記在 `embedding_model`、維度一致；回登記的維度。只讀，不送任何請求。

    Raises:
        EmbeddingModelMismatchError: 模型沒有登記，或登記的維度和設定不同。
    """
    with database.transaction() as connection:
        dimensions = find_embedding_model_dimensions(SystemViewer(), connection, config.model_id)
    if dimensions is None:
        raise EmbeddingModelMismatchError(
            "config/embeddings.yaml 的 model_id 沒有登記在 embedding_model（要先跑 radar migrate）"
        )
    if dimensions != config.dimensions:
        raise EmbeddingModelMismatchError(
            f"embedding_model 登記的維度是 {dimensions}，config/embeddings.yaml 寫的是"
            f" {config.dimensions}"
        )
    return dimensions


def _check_model(database: Database, provider: EmbeddingProvider, config: EmbeddingConfig) -> int:
    """provider、設定與 `embedding_model` 的模型與維度一致；回登記的維度。"""
    if provider.model_id != config.model_id:
        raise EmbeddingModelMismatchError(
            "provider 的模型和 config/embeddings.yaml 的 model_id 不同"
        )
    return check_embedding_model(database, config)


def _planned_input(candidate: EmbeddingCandidate) -> _Planned | ErrorCode | str:
    """1～3 步共用：要做就回 `_Planned`；不做回略過的理由（`not_applicable`／`no_input`）。"""
    plan = plan_for(candidate.facts).for_task(ProcessingTask.EMBED)
    if plan.input_kind is None:
        return "not_applicable"
    try:
        embedding_input = build_embedding_input(candidate.source, plan.input_kind)
    except NoEmbeddingInputError:
        return "no_input"
    sha = embedding_input_sha256(embedding_input.text, embedding_input.input_kind)
    return _Planned(candidate.source.paper_id, embedding_input, sha)


def _plan(candidate: EmbeddingCandidate, tally: _Tally) -> _Planned | None:
    """1～3 步：不用送就計數、回 None。"""
    planned = _planned_input(candidate)
    if planned == "not_applicable":
        tally.not_applicable += 1
        return None
    if planned == "no_input":
        tally.no_input += 1
        return None
    assert isinstance(planned, _Planned)
    if planned.sha256 == candidate.current_sha256:
        tally.unchanged += 1
        return None
    return planned


def _current_sha256(candidate: EmbeddingCandidate) -> str | None:
    """這篇論文現在該用的輸入指紋；現在不該做或沒有內容時是 None。"""
    planned = _planned_input(candidate)
    return planned.sha256 if isinstance(planned, _Planned) else None


def _current_abstract_source(candidate: EmbeddingCandidate) -> AbstractSource | None:
    """這篇論文現在該用的輸入用到的摘要來源（只看標題、或現在不該做時是 None）。指紋只算文字，
    同一段文字換了來源也要看得出來（Story 2.12 審查後）。"""
    planned = _planned_input(candidate)
    return planned.embedding_input.abstract_source if isinstance(planned, _Planned) else None


def _clear(planned: _Planned, cloud_policy: CloudPolicy, tally: _Tally) -> CloudSafeText | None:
    """4：每一段照來源放行再接起來；擋下就計數、回 None（不送）。"""
    try:
        parts = [
            clear_for_cloud(part, source, None, cloud_policy)
            for part, source in planned.embedding_input.parts
        ]
    except CloudClearanceError as exc:
        tally.blocked += 1
        log.info("向量化的輸入放行擋下", source=exc.source.value, reason=exc.reason.value)
        return None
    return join_cloud_safe_texts(parts, EMBED_SEPARATOR)


@dataclass(frozen=True, slots=True)
class _Hold:
    """一筆額度占用（`embedding_quota_hold`）。

    `checked_at`：最近一次在額度鎖內確認這筆占用的時間（新增時、第一次嘗試前確認日期時）；它的
    UTC 日期就是這筆占用算進的那一天，也是記帳時那次嘗試的時間。
    """

    id: uuid.UUID
    checked_at: datetime


def _utc_day_start(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


class _AttemptLedger:
    """一次 `embed_documents` 呼叫的額度占用與記帳（7）。

    - 開始前已經有第 5 步的占用（第一次嘗試用）。
    - adapter 每次嘗試前呼叫 `hooks.before`：第一次在額度鎖內確認日期，同一個 UTC 日期就用現成的
      占用，跨過午夜就放掉它、照新的一天重查並占用（Codex 複查 C1）；之後每次重試前再占一次。
      放不下丟 `EmbeddingQuotaReachedError`（adapter 不送、不再重試）。
    - adapter 每次嘗試後呼叫 `hooks.after`：刪掉占用、記一筆那次嘗試的 `api_call`（同一個交易），
      時間是那次嘗試在額度鎖內確認的時間（跨過 UTC 午夜的重試記在各自的日期）；`sent=False`
      （嘗試前就停下）只放掉占用、不記用量（Codex 複查 C2）。
    - provider 回來或丟錯之後呼叫 `close`：provider 從沒回報過嘗試（替身）就把整次呼叫當成一次嘗試
      記帳；回報過卻還留著占用，代表那筆占用的嘗試沒有送出（例如確認日期時等額度鎖逾時），只放掉。
      `release` 是一篇都沒取得資格、什麼都沒送時放掉占用。
    """

    def __init__(self, runner: _BatchRunner, hold: _Hold) -> None:
        self._runner = runner
        self._hold: _Hold | None = hold
        self._reported = False
        self.tokens = 0
        self.run_ids: list[uuid.UUID] = []
        self.hooks = AttemptHooks(before=self._before, after=self._after)

    def _before(self) -> None:
        first = not self._reported
        self._reported = True
        current = self._hold if first else None
        hold = self._runner.reserve(self.tokens, current=current)
        if hold is None:
            # 跨日重查放不下時，舊占用已經在同一個交易裡放掉。
            self._hold = None
            raise EmbeddingQuotaReachedError()
        self._hold = hold

    def _after(self, attempt: EmbeddingAttempt) -> None:
        self._reported = True
        hold, self._hold = self._hold, None
        if hold is not None:
            self._runner.settle(hold, attempt, self.tokens)

    def close(self, http_status: int | None, *, billable: bool) -> None:
        """provider 回來或丟錯之後：沒結清的占用照整次呼叫的結果記帳（見類別說明）。記不進去時盡量
        把這批的 run 收掉，再原樣丟出（占用留著，過期後轉成用量）。"""
        if self._hold is None:
            return
        if self._reported:
            attempt = EmbeddingAttempt(sent=False, http_status=None, billable=False)
        else:
            attempt = EmbeddingAttempt(sent=True, http_status=http_status, billable=billable)
        try:
            self._after(attempt)
        except BaseException:
            self._runner.fail_runs(self.run_ids, ErrorCode.EMBED_FAILED)
            raise

    def release(self) -> None:
        """什麼都沒送：放掉占用，不記用量。"""
        if self._hold is not None:
            self._after(EmbeddingAttempt(sent=False, http_status=None, billable=False))


class _BatchRunner:
    """5～9：送一批。回停下的原因（額度、失敗），可以繼續時回 None。"""

    def __init__(
        self,
        database: Database,
        provider: EmbeddingProvider,
        config: EmbeddingConfig,
        dimensions: int,
        *,
        code_version: str,
        clock: Callable[[], datetime],
        tally: _Tally,
    ) -> None:
        self._database = database
        self._provider = provider
        self._config = config
        self._dimensions = dimensions
        self._code_version = code_version
        self._clock = clock
        self._tally = tally

    def __call__(self, items: Sequence[_Item], *, resend: bool = False) -> EmbedStop | None:
        config = self._config
        # 5. 額度：占住這一批（取得資格前的篇數估計，不會比真的送出的少）。
        estimated = estimate_embedding_tokens(
            [item.planned.embedding_input.text for item in items], config.token_estimate
        )
        try:
            hold = self.reserve(estimated)
        except LockWaitTimeoutError:
            # 還沒有 run：這批算失敗、不送，也不扣這次送的篇數；其他論文照做。
            self._quota_lock_timeout([], len(items))
            for _ in items:
                self._skipped(resend)
            return None
        if hold is None:
            return EmbedStop.QUOTA_REACHED
        ledger = _AttemptLedger(self, hold)

        # 6. 取得執行資格：每篇自己一個短交易，先鎖論文、重讀、核對，再新增 running 的 run。
        claimed: list[tuple[_Item, uuid.UUID]] = []
        try:
            for item in items:
                run_id = self._claim(item, resend=resend)
                if run_id is not None:
                    claimed.append((item, run_id))
        except BaseException:
            self.fail_runs([run_id for _, run_id in claimed], ErrorCode.EMBED_FAILED)
            ledger.release()
            raise
        if not claimed:
            ledger.release()
            return None
        run_ids = [run_id for _, run_id in claimed]
        ledger.run_ids = run_ids
        ledger.tokens = estimate_embedding_tokens(
            [item.planned.embedding_input.text for item, _ in claimed], config.token_estimate
        )

        # 7、8. 呼叫（不開著交易；每次嘗試前後記帳）、驗證。
        try:
            with report_attempts_to(ledger.hooks):
                raw = self._provider.embed_documents([item.text for item, _ in claimed])
        except EmbeddingQuotaReachedError:
            ledger.close(None, billable=False)
            self.fail_runs(run_ids, ErrorCode.EMBED_QUOTA_REACHED)
            log.warning(
                "送出前今天的估計用量到了停下的比例，這一批不寫，留到下一次續跑",
                papers=len(run_ids),
            )
            return EmbedStop.QUOTA_REACHED
        except LockWaitTimeoutError:
            # 嘗試前等額度鎖逾時（adapter 原樣丟出、沒有送出這一次）。
            ledger.close(None, billable=False)
            self._quota_lock_timeout(run_ids, len(run_ids))
            return None
        except EmbeddingRequestError as exc:
            ledger.close(exc.http_status, billable=True)
            if len(claimed) > 1:
                return self._split(exc, claimed)
            self._skip_one(exc, run_ids)
            return None
        except EmbeddingError as exc:
            configuration = isinstance(exc, EmbeddingConfigurationError)
            ledger.close(exc.http_status, billable=not configuration)
            return self._failed(exc, exc.http_status, run_ids)
        except BaseException:
            ledger.close(None, billable=True)
            self.fail_runs(run_ids, ErrorCode.EMBED_FAILED)
            raise
        # provider 回來了就是 200；向量不對也照記這一次請求的用量。
        ledger.close(200, billable=True)
        try:
            vectors = check_embedding_vectors(
                raw, count=len(claimed), dimensions=self._dimensions, allow_zero=True
            )
        except EmbeddingError as exc:
            return self._failed(exc, 200, run_ids)

        # 9. 寫入：每篇自己一個短交易，先鎖論文、核對輸入沒變、run 還是 running 才寫。
        #    轉成 halfvec 後全是 0 的那一篇單獨標成失敗，其他照寫（不讓它卡住後面的論文）。
        try:
            for (item, run_id), vector in zip(claimed, vectors, strict=True):
                if is_zero_at_halfvec(vector):
                    self._zero_vector(run_id)
                    continue
                self._write_one(item, run_id, vector)
        except BaseException as exc:
            log.error("寫入向量失敗", error_type=type(exc).__name__)
            self.fail_runs(run_ids, ErrorCode.EMBED_FAILED)
            raise
        return None

    # --- 額度占用與記帳 ---------------------------------------------------------------------

    def reserve(self, tokens: int, *, current: _Hold | None = None) -> _Hold | None:
        """5／每次嘗試前：短交易裡拿額度鎖，**拿到鎖之後**才讀時鐘決定是哪一天，把過期的占用轉成
        用量，「今天已用＋占用中＋這次」放得下就新增一筆占用；放不下回 None（記警告）。

        `current`：第一次嘗試沿用的第 5 步占用（Codex 複查 C1）。和現在同一個 UTC 日期就照用
        （回同一筆、確認時間換成現在）；跨過午夜了就在同一個交易裡刪掉它（還沒送出，不記用量），
        再照新的一天重查、放得下才新增。放不下時舊占用也已經刪掉。

        占用的有效期限是 `config.run_limit_seconds`：過了這麼久那次嘗試（對方正常回標頭時）一定已經
        結束，還在就是行程中斷沒回來記帳，由下一次占用照估計轉成那天的用量（保守，當作已計費）；
        原本的行程之後才回來結清時只刪不記（`settle`）。

        Raises:
            LockWaitTimeoutError: 等額度鎖超過 `lock_timeout_seconds`（交易退回，`current` 還在）。
        """
        config = self._config
        with self._database.transaction_with_lock_timeout(config.lock_timeout_ms) as connection:
            Database.transaction_lock(connection, EMBEDDING_QUOTA_LOCK_KEY)
            now = self.now()
            day_start = _utc_day_start(now)
            if current is not None:
                if _utc_day_start(current.checked_at) == day_start:
                    return _Hold(current.id, now)
                log.info("占用額度後、第一次送出前跨過 UTC 午夜，照新的一天重查額度")
                delete_embedding_quota_hold(connection, current.id, None)
            delete_expired_embedding_quota_holds(connection, now)
            used, held = get_embedding_quota_usage(SystemViewer(), connection, day_start)
            if not quota_allows(used + held, tokens, config.quota):
                log.warning(
                    "Workers AI 今天的估計用量到了停下的比例，向量化先停下，留到下一次續跑",
                    used_tokens=used,
                    held_tokens=held,
                    next_tokens=tokens,
                    stop_at_neurons=str(config.quota.stop_at_neurons),
                )
                return None
            hold_id = create_embedding_quota_hold(
                connection,
                tokens,
                operation=EMBED_DOCUMENTS_OPERATION,
                reserved_at=now,
                expires_at=now + timedelta(seconds=config.run_limit_seconds),
            )
        return _Hold(hold_id, now)

    def settle(self, hold: _Hold, attempt: EmbeddingAttempt, tokens: int) -> None:
        """每次嘗試後：同一個短交易裡刪掉占用、記一筆 `api_call`（沒送出就只刪占用）。

        占用已經不在（過期後被別的占用轉成用量了）時什麼都不寫，這次的報表也不計入（那筆用量已經照
        占用的估計記過，報表和帳對得上）。
        """
        call = None
        if attempt.sent:
            call = EmbeddingApiCall(
                operation=EMBED_DOCUMENTS_OPERATION,
                http_status=attempt.http_status,
                estimated_input_tokens=tokens if attempt.billable else 0,
                called_at=hold.checked_at,
            )
        with self._database.transaction() as connection:
            settled = delete_embedding_quota_hold(connection, hold.id, call)
        if call is not None and settled:
            self._tally.requests += 1
            self._tally.estimated_tokens += call.estimated_input_tokens

    # --- 各步驟 -----------------------------------------------------------------------------

    def _claim(self, item: _Item, *, resend: bool) -> uuid.UUID | None:
        """6：鎖住論文、重讀；別的工作已經寫好同一份輸入 → `unchanged`，輸入又變了 →
        `input_changed`（下一次照新的輸入做），同一份輸入正在跑或等鎖逾時 → `busy`；都不是才回
        新的 run id。

        鎖論文讓這一步和別的工作的寫入（也先鎖論文）排隊：等到鎖時，對方要嘛還在跑（running 擋下
        → busy），要嘛已經寫好（重讀看得到 → unchanged），不會兩個工作都送同一份輸入。
        `resend`：這篇已經送出過一次（`_split` 重送）；略過時不扣這次送的篇數（`limit`）。
        """
        config = self._config
        planned = item.planned
        try:
            with self._database.transaction_with_lock_timeout(config.lock_timeout_ms) as connection:
                candidate = find_embedding_candidate_for_update(
                    SystemViewer(), connection, config.model_id, planned.paper_id
                )
                if candidate is not None and candidate.current_sha256 == planned.sha256:
                    self._tally.unchanged += 1
                    self._skipped(resend)
                    return None
                if candidate is None or _current_sha256(candidate) != planned.sha256:
                    self._tally.input_changed += 1
                    self._skipped(resend)
                    return None
                run_id = create_embed_derivation_run(
                    connection,
                    NewEmbedRun(
                        paper_id=planned.paper_id,
                        model=config.model_id,
                        input_sha256=planned.sha256,
                        input_kind=planned.embedding_input.input_kind,
                        params={"dimensions": self._dimensions, "batch_size": config.batch_size},
                        code_version=self._code_version,
                    ),
                )
        except LockWaitTimeoutError:
            log.info("取得執行資格時等論文的鎖逾時，這篇這次不送")
            run_id = None
        if run_id is None:
            self._tally.busy += 1
            self._skipped(resend)
        return run_id

    def _skipped(self, resend: bool) -> None:
        if not resend:
            self._tally.skipped_at_claim += 1

    def _split(
        self, exc: EmbeddingRequestError, claimed: list[tuple[_Item, uuid.UUID]]
    ) -> EmbedStop | None:
        """一批被拒（請求不合法）：可能只是其中一篇的問題。這批的 run 收掉，改成一篇一篇重送，
        讓有問題的那一篇不會擋住後面所有論文。重送時才略過的（別的工作做好了等）不扣這次送的篇數。
        """
        self.fail_runs([run_id for _, run_id in claimed], exc.code)
        log.warning(
            "向量化請求不合法，這一批改成一篇一篇重送",
            http_status=exc.http_status,
            papers=len(claimed),
        )
        for item, _ in claimed:
            stop = self([item], resend=True)
            if stop is not None:
                return stop
        return None

    def _skip_one(self, exc: EmbeddingRequestError, run_ids: list[uuid.UUID]) -> None:
        """單獨一篇被拒（請求不合法）：這篇標成失敗、這次略過，其他論文照做（不停下）。"""
        self.fail_runs(run_ids, exc.code)
        self._tally.failed += len(run_ids)
        self._tally.failure_reason = exc.code
        log.error(
            "這一篇的向量化請求不合法，這次略過", reason=exc.code.value, http_status=exc.http_status
        )

    def _quota_lock_timeout(self, run_ids: list[uuid.UUID], papers: int) -> None:
        """等額度鎖逾時：這批不送、算失敗（有 run 就改成 `failed`），其他論文照做（不停下）。"""
        self.fail_runs(run_ids, ErrorCode.EMBED_QUOTA_LOCK_TIMEOUT)
        self._tally.failed += papers
        self._tally.failure_reason = ErrorCode.EMBED_QUOTA_LOCK_TIMEOUT
        log.error("占用額度時等額度鎖逾時，這一批這次不送", papers=papers)

    def _zero_vector(self, run_id: uuid.UUID) -> None:
        """這一篇的向量轉成 halfvec 後全是 0：標成失敗、不寫，其他篇照寫。"""
        self.fail_runs([run_id], ErrorCode.EMBED_RESPONSE_INVALID)
        self._tally.failed += 1
        self._tally.failure_reason = ErrorCode.EMBED_RESPONSE_INVALID
        log.error("這一篇的向量轉成 halfvec 後全是 0，這次不寫")

    def _failed(
        self, exc: EmbeddingError, http_status: int | None, run_ids: list[uuid.UUID]
    ) -> EmbedStop:
        """這一批失敗：run 改成 `failed`、整批不寫；設定錯原樣丟出，其他停下這次。

        用量已經在每次嘗試後記過（設定錯是驗證或路徑就被拒、沒有跑模型，記 0 個估計 token）。
        """
        self.fail_runs(run_ids, exc.code)
        self._tally.failed += len(run_ids)
        self._tally.failure_reason = exc.code
        log.error(
            "向量化請求失敗，這一批不寫",
            reason=exc.code.value,
            http_status=http_status,
            papers=len(run_ids),
        )
        if isinstance(exc, EmbeddingConfigurationError):
            raise exc
        return EmbedStop.FAILED

    def now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("時鐘要回帶時區的時間")
        return now

    def _write_one(self, item: _Item, run_id: uuid.UUID, vector: list[float]) -> None:
        """9：一篇一個短交易。先鎖論文再重讀、重算指紋：

        - 變了 → run 改成 `failed`（`embed_input_changed`），不寫，下一次照新的輸入重算；
        - run 已經不是 `running`（sweep 判定逾時收掉了）→ 不寫、算失敗，下一次重算；
        - 等鎖逾時 → run 改成 `failed`（`embed_lock_timeout`）、算失敗，下一次重算；
        - 都對 → run 改成 `succeeded`、覆寫向量（主鍵是（論文, 模型），一篇一個模型只會有一列）。

        鎖論文讓核對到寫入之間不會被每週更新或另一個向量化工作插隊：舊輸入算的向量不會蓋掉新的。
        """
        config = self._config
        planned = item.planned
        abstract_source = planned.embedding_input.abstract_source
        try:
            with self._database.transaction_with_lock_timeout(config.lock_timeout_ms) as connection:
                if abstract_source is not None:
                    # 鎖論文之前先拿摘要來源的共用鎖（依來源移除拿獨佔，Story 2.12 審查後）：
                    # 移除做完之後，下面的核對看得到這個來源的摘要已經不在，就不會再寫進向量。
                    Database.transaction_lock_shared(
                        connection, abstract_source_lock_key(abstract_source)
                    )
                candidate = find_embedding_candidate_for_update(
                    SystemViewer(), connection, config.model_id, planned.paper_id
                )
                if (
                    candidate is None
                    or _current_sha256(candidate) != planned.sha256
                    or _current_abstract_source(candidate) is not abstract_source
                ):
                    update_derivation_run_failed(connection, run_id, ErrorCode.EMBED_INPUT_CHANGED)
                    self._tally.input_changed += 1
                    return
                if not update_derivation_run_succeeded(connection, run_id):
                    log.warning("向量化的 run 已經被收掉（逾時），這一篇這次不寫")
                    self._tally.failed += 1
                    self._tally.failure_reason = ErrorCode.LLM_RUN_EXPIRED
                    return
                upsert_embeddings(
                    connection,
                    [
                        NewEmbedding(
                            item_id=planned.paper_id,
                            model_id=config.model_id,
                            dimensions=self._dimensions,
                            vector=tuple(vector),
                            input_sha256=planned.sha256,
                            run_id=run_id,
                            abstract_source=abstract_source,
                        )
                    ],
                )
        except LockWaitTimeoutError:
            log.warning("寫入向量時等論文的鎖逾時，這一篇這次不寫")
            self.fail_runs([run_id], ErrorCode.EMBED_LOCK_TIMEOUT)
            self._tally.failed += 1
            self._tally.failure_reason = ErrorCode.EMBED_LOCK_TIMEOUT
            return
        self._tally.embedded += 1

    def fail_runs(self, run_ids: list[uuid.UUID], reason: ErrorCode) -> None:
        """盡量把這批 `running` 的 run 改成 `failed`；這一步也失敗只記日誌（sweep 之後收）。"""
        if not run_ids:
            return
        try:
            with self._database.transaction() as connection:
                for run_id in run_ids:
                    update_derivation_run_failed(connection, run_id, reason)
        except Exception as cleanup:
            log.warning(
                "把向量化的 derivation_run 改成 failed 也失敗", error_type=type(cleanup).__name__
            )


# --- 搜尋的查詢向量（Story 4.1、ADR-0005 的「2026-11 實作更新」） -------------------------------


def embed_search_query(
    actor: Viewer,
    database: Database,
    provider: EmbeddingProvider,
    config: EmbeddingConfig,
    text: CloudSafeText,
    *,
    clock: Callable[[], datetime],
) -> list[float]:
    """把搜尋的查詢轉成向量（`provider.embed_query`），和背景向量化記在同一本帳（`api_call`，
    operation `embed_query`、每次嘗試一列，帶結果代碼 `outcome`）、共用同一套額度占用。

    - 額度：搜尋可以用到每日免費額度的 100%（背景向量化停在 `stop_at_ratio`，留給搜尋）。送出前
      在額度鎖內占住這次的估計 token（查詢文字＋設定的指示文字），放不下就不送：記一列
      `quota_exhausted`（估計 token 0、沒有狀態碼）、寫一筆警告日誌、丟
      `SearchQuotaExhaustedError`。兩個請求同時搶最後的額度時，額度鎖讓它們排隊，只有放得下的那個
      送出（占用在帳上，另一個看得到）。
    - 每次嘗試後把占用換成一列 `api_call`：`succeeded`、`unreachable`（沒有回應，或 408、429、
      5xx）、`rejected`（其他 4xx）。替身（不回報嘗試）整次呼叫當成一次嘗試記。
    - 沒有送出的失敗也記一列 `not_sent`（估計 token 0、沒有狀態碼；審查 H2）：等額度鎖逾時（占用
      前、第一次送出前確認日期、重試前都一樣），或 adapter 在送出前就停下（總時限已到）。所以每次
      搜尋至少有一列。
    - 回來的向量當作使用者輸入驗（筆數、維度＝設定的維度、數字有限、不是全 0）。
    - 任何一種失敗都不降級成只用關鍵字：額度用完丟 `SearchQuotaExhaustedError`，其他（連不上、
      設定錯、回應不對、等額度鎖逾時）丟 `SearchEmbeddingUnavailableError`。日誌不記查詢內容。

    `actor` 是發出搜尋的讀者身分（只用來表明這次寫帳是誰的請求；寫的是共用的用量帳，不看權限）。

    Raises:
        EmbeddingModelMismatchError: provider 的模型和設定不同（沒送任何請求）。
        SearchQuotaExhaustedError: 今天的額度放不下這次查詢。
        SearchEmbeddingUnavailableError: 其他原因沒能轉成向量。
    """
    del actor  # 用量帳是全站共用的，不分讀者（AD-25）。
    if provider.model_id != config.model_id:
        raise EmbeddingModelMismatchError(
            "provider 的模型和 config/embeddings.yaml 的 model_id 不同"
        )
    texts = [cloud_text(text)]
    if config.query_instruction is not None:
        texts.append(config.query_instruction)
    ledger = _QueryLedger(
        database, config, clock, estimate_embedding_tokens(texts, config.token_estimate)
    )
    try:
        ledger.start()
        with report_attempts_to(ledger.hooks):
            raw = provider.embed_query(text)
    except EmbeddingQuotaReachedError:
        ledger.close(None, billable=False)
        raise SearchQuotaExhaustedError() from None
    except SearchQuotaExhaustedError:
        raise
    except LockWaitTimeoutError:
        ledger.close_unsent()
        log.warning("查詢向量等額度鎖逾時，這次搜尋不送")
        raise SearchEmbeddingUnavailableError() from None
    except EmbeddingError as exc:
        ledger.close(exc.http_status, billable=not isinstance(exc, EmbeddingConfigurationError))
        log.warning(
            "查詢向量沒有轉成功，搜尋回固定的錯誤（不降級成只用關鍵字）",
            error_code=exc.code.value,
            http_status=exc.http_status,
        )
        raise SearchEmbeddingUnavailableError() from None
    except BaseException:
        ledger.close(None, billable=True)
        raise
    ledger.close(200, billable=True)
    try:
        (vector,) = check_embedding_vectors(
            [raw], count=1, dimensions=config.dimensions, allow_zero=False
        )
    except EmbeddingError as exc:
        log.warning("查詢向量的回應不對，搜尋回固定的錯誤", error_code=exc.code.value)
        raise SearchEmbeddingUnavailableError() from None
    return vector


class _QueryLedger:
    """一次 `embed_query` 的額度占用與記帳（做法同 `_AttemptLedger`，額度用到 100%）。

    - `start`：送出前在額度鎖內占住（放不下就記一列 `quota_exhausted` 並丟
      `SearchQuotaExhaustedError`）。
    - adapter 每次嘗試前 `hooks.before`：第一次沿用 `start` 的占用（跨過 UTC 午夜就照新的一天
      重查），之後每次重試再占一次；放不下記一列 `quota_exhausted`、丟
      `EmbeddingQuotaReachedError`。
    - adapter 每次嘗試後 `hooks.after`：刪掉占用、記一列這次嘗試的 `api_call`（帶結果代碼）；
      沒送出（`sent=False`）記一列 `not_sent`（估計 token 0）。
    - provider 回來或丟錯之後 `close`：從沒回報過嘗試（替身）就把整次呼叫當成一次嘗試記帳；回報過
      卻還留著占用（那次嘗試沒送出）就照 `not_sent` 結清。
    - 等額度鎖逾時 `close_unsent`：還有占用就照 `not_sent` 結清；沒有占用（占用前、重試前等不到）
      就直接記一列 `not_sent`。

    **已送出、還沒結清就中斷**（審查 H3 核對）：真的 adapter 在 `before()` 之後的送出包在
    `try/finally` 裡，`finally` 一定呼叫 `after(sent=True)`（`BaseException` 也一樣），所以不會有
    「送了卻被當成沒送」的占用；結清的交易本身失敗（資料庫斷線、行程被砍）時占用留在資料庫，過期後
    由下一次占用照 `embed_query` 轉成用量（`self._hold` 已經是 None，`close` 不會把它刪掉）。
    """

    def __init__(
        self,
        database: Database,
        config: EmbeddingConfig,
        clock: Callable[[], datetime],
        tokens: int,
    ) -> None:
        self._database = database
        self._config = config
        # 搜尋可以用到 100%：只改停下的比例，其他數字照設定。
        self._quota = config.quota.model_copy(update={"stop_at_ratio": Decimal(1)})
        self._clock = clock
        self._tokens = tokens
        self._hold: _Hold | None = None
        self._reported = False
        self.hooks = AttemptHooks(before=self._before, after=self._after)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("時鐘回的時間要帶時區")
        return now

    def start(self) -> None:
        """送出前占住這次的額度。

        Raises:
            SearchQuotaExhaustedError: 放不下（已記一列 `quota_exhausted`）。
            LockWaitTimeoutError: 等額度鎖逾時。
        """
        hold = self._reserve(None)
        if hold is None:
            raise SearchQuotaExhaustedError()
        self._hold = hold

    def _reserve(self, current: _Hold | None) -> _Hold | None:
        config = self._config
        with self._database.transaction_with_lock_timeout(config.lock_timeout_ms) as connection:
            Database.transaction_lock(connection, EMBEDDING_QUOTA_LOCK_KEY)
            now = self._now()
            day_start = _utc_day_start(now)
            if current is not None:
                if _utc_day_start(current.checked_at) == day_start:
                    return _Hold(current.id, now)
                delete_embedding_quota_hold(connection, current.id, None)
            delete_expired_embedding_quota_holds(connection, now)
            used, held = get_embedding_quota_usage(SystemViewer(), connection, day_start)
            if not quota_allows(used + held, self._tokens, self._quota):
                create_embedding_api_call(
                    connection,
                    EmbeddingApiCall(
                        operation=EMBED_QUERY_OPERATION,
                        http_status=None,
                        estimated_input_tokens=0,
                        called_at=now,
                        outcome=EmbeddingCallOutcome.QUOTA_EXHAUSTED,
                    ),
                )
                log.warning(
                    "Workers AI 今天的額度用完了，搜尋回固定的錯誤（不降級成只用關鍵字）",
                    used_tokens=used,
                    held_tokens=held,
                    next_tokens=self._tokens,
                )
                return None
            hold_id = create_embedding_quota_hold(
                connection,
                self._tokens,
                operation=EMBED_QUERY_OPERATION,
                reserved_at=now,
                expires_at=now + timedelta(seconds=config.run_limit_seconds),
            )
        return _Hold(hold_id, now)

    def _before(self) -> None:
        first = not self._reported
        self._reported = True
        hold = self._reserve(self._hold if first else None)
        if hold is None:
            self._hold = None
            raise EmbeddingQuotaReachedError()
        self._hold = hold

    def _after(self, attempt: EmbeddingAttempt) -> None:
        self._reported = True
        hold, self._hold = self._hold, None
        if hold is None:
            return
        if attempt.sent:
            call = EmbeddingApiCall(
                operation=EMBED_QUERY_OPERATION,
                http_status=attempt.http_status,
                estimated_input_tokens=self._tokens if attempt.billable else 0,
                called_at=hold.checked_at,
                outcome=embedding_call_outcome(attempt.http_status),
            )
        else:
            call = self._unsent(hold.checked_at)
        with self._database.transaction() as connection:
            delete_embedding_quota_hold(connection, hold.id, call)

    @staticmethod
    def _unsent(at: datetime) -> EmbeddingApiCall:
        return EmbeddingApiCall(
            operation=EMBED_QUERY_OPERATION,
            http_status=None,
            estimated_input_tokens=0,
            called_at=at,
            outcome=EmbeddingCallOutcome.NOT_SENT,
        )

    def close_unsent(self) -> None:
        """等額度鎖逾時、這次搜尋不送：記一列 `not_sent`（見類別說明）。"""
        if self._hold is not None:
            self._after(EmbeddingAttempt(sent=False, http_status=None, billable=False))
            return
        with self._database.transaction() as connection:
            create_embedding_api_call(connection, self._unsent(self._now()))

    def close(self, http_status: int | None, *, billable: bool) -> None:
        """provider 回來或丟錯之後：沒結清的占用照整次呼叫的結果記帳（見類別說明）。"""
        if self._hold is None:
            return
        if self._reported:
            self._after(EmbeddingAttempt(sent=False, http_status=None, billable=False))
        else:
            self._after(EmbeddingAttempt(sent=True, http_status=http_status, billable=billable))
