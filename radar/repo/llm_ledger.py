"""`derivation_run`（來歷）與 `llm_call`（帳）兩張表：表的定義與唯一的寫入者（AD-8、ADR-0009、
ADR-0030）。

- `derivation_run`：一次模型呼叫一筆，狀態 `running` → `succeeded`（含被拒收）／`failed`（暫時性
  失敗、設定錯、sweep 判定逾時）。（對象, 工作, prompt 版本, 模型, input_sha256）有只算
  `running` 而且不是評估的部分唯一索引：同一份輸入同時只有一個在跑。已經存下輸出卻沒寫成結果的
  run 標 `failed`（`llm_result_pending`），下一次同一組鍵把它改回 `running` 接續（ADR-0030）。
- 向量化（task＝embed，Story 2.8、ADR-0040）也記在 `derivation_run`：沒有 prompt（兩個 prompt 欄
  是 NULL）、沒有 `llm_call`；同一篇、同一個模型、同一份輸入同時只有一個 running
  （`uq_derivation_run_embed_running`）。依鍵查的讀取函式都帶 LLM 的工作代碼，讀不到 embed 的列；
  依 id 讀的 `find_derivation_run` 只給閘門讀自己建的 run。
- `llm_call`：一次呼叫的 tokens、費用（`numeric(12,6)`，美元）與存下的輸出；`run_id` 唯一。應用
  程式角色對它沒有 UPDATE、DELETE（遷移 0010 收回）：帳只能新增。Batch 結果的帳用
  `create_batch_llm_call`（`ON CONFLICT ... DO NOTHING`，遷移 0017 的部分唯一索引）：同一筆 run
  匯入幾次都只記一次（Story 2.11）。
- 兩張表都由 services 用自己的短交易寫、立刻 commit（Conventions 的例外：主交易回滾時帳照記）。
- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。
- 讀取函式第一個參數是 `viewer`，只給系統工作（`visible_to_system_only`）；AI 區塊用的
  `find_running_call_mode` 給任何讀者（接 `visible_papers`）。
"""

import json
import uuid
from datetime import timedelta

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    RowMapping,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from radar.domain.ai_outputs import (
    CallMode,
    DerivationRun,
    InputKind,
    LlmTask,
    NewDerivationRun,
    NewLlmCall,
    RunKey,
    RunStatus,
    SavedLlmOutput,
)
from radar.domain.embeddings import EMBED_TASK, NewEmbedRun
from radar.domain.errors import ErrorCode
from radar.domain.trial_runs import LlmCallDiagnostics
from radar.domain.viewer import SystemViewer, Viewer
from radar.repo.models import Base
from radar.repo.visibility import visible_papers, visible_to_system_only

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0010 建表） -----------------------------

_KEY_COLUMNS = ("paper_id", "task", "prompt_version", "model", "input_sha256")
_SHA256 = "'^[0-9a-f]{64}$'"

derivation_run_table = Table(
    "derivation_run",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("paper_id", Uuid, ForeignKey("paper.id"), nullable=False),
    Column("task", Text, nullable=False),
    # 向量化（task＝embed）沒有 prompt，這兩欄是 NULL；其他工作一定有（遷移 0015、ADR-0040）。
    Column("prompt_version", Integer),
    Column("prompt_sha256", Text),
    Column("model", Text, nullable=False),
    Column("input_sha256", Text, nullable=False),
    Column("input_kind", Text, nullable=False),
    Column("params", JSONB, nullable=False),
    Column("taxonomy_version", Text),
    Column("context_values_version", Text),
    Column("code_version", Text, nullable=False),
    Column("call_mode", Text, nullable=False),
    # 評估（Epic 8 的 `eval_run`）的編號；那張表還不存在，暫時不設外鍵（ADR-0030）。
    Column("eval_run_id", Uuid),
    Column("status", Text, nullable=False),
    Column("failure_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("finished_at", DateTime(timezone=True)),
    # 開頭是 paper_id：同時當外鍵的索引；`should_run` 也照這組欄位查。
    Index("ix_derivation_run_key", *_KEY_COLUMNS),
    # 同一份輸入同時只有一個平常流程的 running（評估不受限制，ADR-0009 第 4 點）。
    Index(
        "uq_derivation_run_running",
        *_KEY_COLUMNS,
        unique=True,
        postgresql_where=text("status = 'running' AND eval_run_id IS NULL"),
    ),
    # 向量化：同一篇論文、同一個模型、同一份輸入同時只有一個 running（prompt 欄是 NULL，上面那個
    # 索引擋不到；遷移 0015、ADR-0040）。
    Index(
        "uq_derivation_run_embed_running",
        "paper_id",
        "model",
        "input_sha256",
        unique=True,
        postgresql_where=text("task = 'embed' AND status = 'running'"),
    ),
    # sweep 找逾時的互動 running（看 `updated_at`：接續時會重設）。
    Index(
        "ix_derivation_run_running_updated_at",
        "updated_at",
        postgresql_where=text("status = 'running'"),
    ),
    CheckConstraint(
        "task IN ('zh_summary', 'classify', 'extract_model', 'merge_judge', 'qa_answer',"
        " 'eval_judge', 'embed')",
        name="task",
    ),
    CheckConstraint(
        "(task = 'embed') = (prompt_version IS NULL)", name="prompt_version_iff_not_embed"
    ),
    CheckConstraint(
        "(prompt_version IS NULL) = (prompt_sha256 IS NULL)",
        name="prompt_sha256_iff_prompt_version",
    ),
    CheckConstraint("prompt_version >= 1", name="prompt_version_positive"),
    CheckConstraint(f"prompt_sha256 ~ {_SHA256}", name="prompt_sha256_format"),
    CheckConstraint(f"input_sha256 ~ {_SHA256}", name="input_sha256_format"),
    CheckConstraint("model <> ''", name="model_not_empty"),
    CheckConstraint("input_kind IN ('title', 'abstract', 'fulltext')", name="input_kind"),
    CheckConstraint("jsonb_typeof(params) = 'object'", name="params_object"),
    CheckConstraint(
        "taxonomy_version IS NULL OR taxonomy_version <> ''", name="taxonomy_version_not_empty"
    ),
    CheckConstraint(
        "context_values_version IS NULL OR context_values_version <> ''",
        name="context_values_version_not_empty",
    ),
    CheckConstraint("code_version <> ''", name="code_version_not_empty"),
    CheckConstraint("call_mode IN ('interactive', 'batch')", name="call_mode"),
    CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status"),
    CheckConstraint(
        "(status = 'failed') = (failure_reason IS NOT NULL)", name="failure_reason_iff_failed"
    ),
    CheckConstraint(
        "failure_reason IS NULL OR failure_reason ~ '^[a-z][a-z0-9_]*$'",
        name="failure_reason_format",
    ),
    CheckConstraint(
        "(status = 'running') = (finished_at IS NULL)", name="finished_at_iff_not_running"
    ),
)

# 外鍵的索引由 run_id 的唯一約束提供。
llm_call_table = Table(
    "llm_call",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("run_id", Uuid, ForeignKey("derivation_run.id"), nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cache_creation_input_tokens", Integer, nullable=False),
    Column("cache_read_input_tokens", Integer, nullable=False),
    Column("cost_usd", Numeric(12, 6), nullable=False),
    Column("price_tier", Text, nullable=False),
    Column("is_batch", Boolean, nullable=False),
    Column("response_id", Text),
    Column("request_id", Text),
    Column("stop_reason", Text),
    Column("raw_response", JSONB),
    # 存下的模型輸出（最多一個有值）：寫結果失敗後從這裡接續，不再呼叫模型（ADR-0030）。
    Column("output_text", Text),
    Column("output_reject_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("run_id"),
    # Batch 一筆 run 一筆帳（遷移 0017、ADR-0043）：匯入用 `ON CONFLICT (run_id) WHERE is_batch`
    # 記帳，兩個 poller 同時匯入、重跑、中斷再跑都只記一次。
    Index(
        "uq_llm_call_batch_run_id",
        "run_id",
        unique=True,
        postgresql_where=text("is_batch"),
    ),
    CheckConstraint(
        "input_tokens >= 0 AND output_tokens >= 0 AND cache_creation_input_tokens >= 0"
        " AND cache_read_input_tokens >= 0",
        name="tokens_not_negative",
    ),
    CheckConstraint("cost_usd >= 0", name="cost_usd_not_negative"),
    CheckConstraint("price_tier IN ('standard', 'long_context')", name="price_tier"),
    CheckConstraint(
        "output_text IS NULL OR output_reject_reason IS NULL", name="output_at_most_one"
    ),
    CheckConstraint(
        "output_reject_reason IS NULL OR output_reject_reason ~ '^[a-z][a-z0-9_]*$'",
        name="output_reject_reason_format",
    ),
)

# 這筆 run 有存下輸出的 `llm_call`（可以接續）；用在 `UPDATE derivation_run` 裡。
_HAS_SAVED_OUTPUT = (
    "EXISTS (SELECT 1 FROM llm_call AS c WHERE c.run_id = derivation_run.id"
    " AND (c.output_text IS NOT NULL OR c.output_reject_reason IS NOT NULL))"
)

_RUN_COLUMNS = (
    "id, paper_id, task, prompt_version, model, input_sha256, input_kind, taxonomy_version,"
    " context_values_version, call_mode, eval_run_id, status, failure_reason"
)


def _derivation_run(row: RowMapping) -> DerivationRun:
    return DerivationRun(
        id=row["id"],
        key=RunKey(
            paper_id=row["paper_id"],
            task=LlmTask(row["task"]),
            prompt_version=row["prompt_version"],
            model=row["model"],
            input_sha256=row["input_sha256"],
        ),
        input_kind=InputKind(row["input_kind"]),
        taxonomy_version=row["taxonomy_version"],
        context_values_version=row["context_values_version"],
        call_mode=CallMode(row["call_mode"]),
        eval_run_id=row["eval_run_id"],
        status=RunStatus(row["status"]),
        failure_reason=row["failure_reason"],
    )


def _key_params(key: RunKey) -> dict[str, object]:
    return {
        "paper_id": key.paper_id,
        "task": LlmTask(key.task).value,
        "prompt_version": key.prompt_version,
        "model": key.model,
        "input_sha256": key.input_sha256,
    }


# --- 讀取 ---------------------------------------------------------------------------------


def find_derivation_run(
    viewer: SystemViewer, connection: Connection, run_id: uuid.UUID, *, for_update: bool
) -> DerivationRun | None:
    """一筆 `derivation_run`；`for_update` 為真時鎖住那一列（`ai_results` 核對狀態用）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    lock = " FOR UPDATE" if for_update else ""
    row = (
        connection.execute(
            text(f"SELECT {_RUN_COLUMNS} FROM derivation_run WHERE {visible} AND id = :id{lock}"),
            {"id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _derivation_run(row)


def find_running_derivation_run_id(
    viewer: SystemViewer, connection: Connection, key: RunKey, eval_run_id: uuid.UUID | None
) -> uuid.UUID | None:
    """這組鍵有沒有同一個流程的 `running`；有就回它的 id。

    `eval_run_id` 是 None 時只看平常流程（不是評估）的 run；有值時只看同一個評估的 run（別的評估、
    平常流程都不算）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    value = connection.execute(
        text(
            f"SELECT id FROM derivation_run WHERE {visible}"
            " AND paper_id = :paper_id AND task = :task AND prompt_version = :prompt_version"
            " AND model = :model AND input_sha256 = :input_sha256"
            " AND status = 'running'"
            " AND eval_run_id IS NOT DISTINCT FROM CAST(:eval_run_id AS uuid)"
            " LIMIT 1"
        ),
        {**_key_params(key), "eval_run_id": eval_run_id},
    ).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


def find_running_call_mode(viewer: Viewer, connection: Connection, key: RunKey) -> CallMode | None:
    """這組鍵有平常流程（不是評估）的 `running` 時回它的呼叫方式（互動或 Batch）；沒有、論文不存在或
    `viewer` 看不到時回 None。AI 區塊分 `pending_interactive`／`pending_batch` 用。
    """
    visible = visible_papers(viewer, alias="paper")
    value = connection.execute(
        text(
            "SELECT r.call_mode FROM derivation_run AS r JOIN paper ON paper.id = r.paper_id"
            f" WHERE {visible}"
            " AND r.paper_id = :paper_id AND r.task = :task AND r.prompt_version = :prompt_version"
            " AND r.model = :model AND r.input_sha256 = :input_sha256"
            " AND r.status = 'running' AND r.eval_run_id IS NULL"
            " ORDER BY r.created_at, r.id LIMIT 1"
        ),
        _key_params(key),
    ).scalar_one_or_none()
    return None if value is None else CallMode(value)


def find_resumable_derivation_run_id(
    viewer: SystemViewer, connection: Connection, key: RunKey, eval_run_id: uuid.UUID | None
) -> uuid.UUID | None:
    """這組鍵（評估時還要同一個 `eval_run_id`）最早一筆可接續的 run：`llm_result_pending` 而且
    `llm_call` 存下了輸出（和 `update_derivation_run_resumed` 同一個條件，只讀不改）；沒有回 None。
    預算不准時閘門用它判斷還能不能接續（ADR-0031）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    value = connection.execute(
        text(
            f"SELECT r.id FROM derivation_run AS r WHERE {visible}"
            " AND r.paper_id = :paper_id AND r.task = :task"
            " AND r.prompt_version = :prompt_version AND r.model = :model"
            " AND r.input_sha256 = :input_sha256"
            " AND r.status = 'failed' AND r.failure_reason = :pending"
            " AND r.eval_run_id IS NOT DISTINCT FROM CAST(:eval_run_id AS uuid)"
            " AND EXISTS (SELECT 1 FROM llm_call AS c WHERE c.run_id = r.id"
            " AND (c.output_text IS NOT NULL OR c.output_reject_reason IS NOT NULL))"
            " ORDER BY r.created_at, r.id LIMIT 1"
        ),
        {
            **_key_params(key),
            "pending": ErrorCode.LLM_RESULT_PENDING.value,
            "eval_run_id": eval_run_id,
        },
    ).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


def find_llm_call_output(
    viewer: SystemViewer, connection: Connection, run_id: uuid.UUID
) -> SavedLlmOutput | None:
    """這筆 run 的 `llm_call` 存下的模型輸出；沒有帳或沒有存輸出時回 None。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    row = (
        connection.execute(
            text(
                f"SELECT output_text, output_reject_reason FROM llm_call WHERE {visible}"
                " AND run_id = :run_id"
            ),
            {"run_id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None or (row["output_text"] is None and row["output_reject_reason"] is None):
        return None
    reason = row["output_reject_reason"]
    return SavedLlmOutput(
        text=row["output_text"], reject_reason=None if reason is None else ErrorCode(reason)
    )


def find_llm_call_diagnostics(
    viewer: SystemViewer, connection: Connection, run_id: uuid.UUID
) -> LlmCallDiagnostics | None:
    """這筆 run 的 `llm_call` 診斷欄位（tokens、`stop_reason`、`price_tier`、識別碼、原始回應、
    存下的輸出）；同一個 run 有多筆時取最新的，沒有帳回 None。`radar runs trial --verbose`／
    `--show-output` 用（Story 2.10）；呼叫端不得把 `output_text` 寫進日誌。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    row = (
        connection.execute(
            text(
                "SELECT input_tokens, output_tokens, cache_creation_input_tokens,"
                " cache_read_input_tokens, stop_reason, price_tier, response_id, request_id,"
                " raw_response, output_text, output_reject_reason"
                f" FROM llm_call WHERE {visible} AND run_id = :run_id"
                " ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"run_id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    raw = row["raw_response"]
    return LlmCallDiagnostics(
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_creation_input_tokens=row["cache_creation_input_tokens"],
        cache_read_input_tokens=row["cache_read_input_tokens"],
        stop_reason=row["stop_reason"],
        price_tier=row["price_tier"],
        response_id=row["response_id"],
        request_id=row["request_id"],
        raw_response=raw if isinstance(raw, dict) else None,
        output_text=row["output_text"],
        output_reject_reason=row["output_reject_reason"],
    )


# --- 寫入 ---------------------------------------------------------------------------------


def create_derivation_run(connection: Connection, run: NewDerivationRun) -> uuid.UUID | None:
    """新增一筆 `running` 的 `derivation_run`，回它的 id。

    同一組鍵已經有平常流程的 `running`（部分唯一索引）時什麼都不寫、回 None（`ON CONFLICT DO
    NOTHING`）；評估（`eval_run_id` 有值）不受這個索引限制。在呼叫端開好的交易裡做，不 commit。
    """
    value = connection.execute(
        text(
            "INSERT INTO derivation_run"
            " (paper_id, task, prompt_version, prompt_sha256, model, input_sha256, input_kind,"
            " params, taxonomy_version, context_values_version, code_version, call_mode,"
            " eval_run_id, status)"
            " VALUES (:paper_id, :task, :prompt_version, :prompt_sha256, :model, :input_sha256,"
            " :input_kind, CAST(:params AS jsonb), :taxonomy_version, :context_values_version,"
            " :code_version, :call_mode, :eval_run_id, 'running')"
            " ON CONFLICT (paper_id, task, prompt_version, model, input_sha256)"
            " WHERE status = 'running' AND eval_run_id IS NULL DO NOTHING"
            " RETURNING id"
        ),
        {
            **_key_params(run.key),
            "prompt_sha256": run.prompt_sha256,
            "input_kind": InputKind(run.input_kind).value,
            "params": json.dumps(dict(run.params), sort_keys=True, ensure_ascii=False),
            "taxonomy_version": run.taxonomy_version,
            "context_values_version": run.context_values_version,
            "code_version": run.code_version,
            "call_mode": CallMode(run.call_mode).value,
            "eval_run_id": run.eval_run_id,
        },
    ).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


def create_embed_derivation_run(connection: Connection, run: NewEmbedRun) -> uuid.UUID | None:
    """新增一筆向量化（task＝embed）的 `running` `derivation_run`，回它的 id
    （Story 2.8、ADR-0040）。

    沒有 prompt（`prompt_version`、`prompt_sha256` 是 NULL）；呼叫方式記互動，卡住時由 sweep 收。
    同一篇論文、同一個模型、同一份輸入已經有 `running`（`uq_derivation_run_embed_running`）時
    什麼都不寫、回 None。在呼叫端開好的交易裡做，不 commit。
    """
    value = connection.execute(
        text(
            "INSERT INTO derivation_run"
            " (paper_id, task, prompt_version, prompt_sha256, model, input_sha256, input_kind,"
            " params, code_version, call_mode, status)"
            " VALUES (:paper_id, :task, NULL, NULL, :model, :input_sha256, :input_kind,"
            " CAST(:params AS jsonb), :code_version, :call_mode, 'running')"
            " ON CONFLICT (paper_id, model, input_sha256)"
            " WHERE task = 'embed' AND status = 'running' DO NOTHING"
            " RETURNING id"
        ),
        {
            "paper_id": run.paper_id,
            "task": EMBED_TASK,
            "model": run.model,
            "input_sha256": run.input_sha256,
            "input_kind": InputKind(run.input_kind).value,
            "params": json.dumps(dict(run.params), sort_keys=True, ensure_ascii=False),
            "code_version": run.code_version,
            "call_mode": CallMode.INTERACTIVE.value,
        },
    ).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


def update_derivation_run_succeeded(connection: Connection, run_id: uuid.UUID) -> bool:
    """`running` → `succeeded`（含結果被拒收）；已經不是 `running` 就不改、回 False。"""
    result = connection.execute(
        text(
            "UPDATE derivation_run SET status = 'succeeded', finished_at = clock_timestamp(),"
            " updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'running'"
        ),
        {"id": run_id},
    )
    return result.rowcount == 1


def update_derivation_run_failed(
    connection: Connection, run_id: uuid.UUID, reason: ErrorCode
) -> bool:
    """`running` → `failed`，記原因代碼；已經不是 `running` 就不改、回 False。"""
    result = connection.execute(
        text(
            "UPDATE derivation_run SET status = 'failed', failure_reason = :reason,"
            " finished_at = clock_timestamp(), updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'running'"
        ),
        {"id": run_id, "reason": ErrorCode(reason).value},
    )
    return result.rowcount == 1


def update_derivation_run_failed_or_pending(
    connection: Connection, run_id: uuid.UUID, reason: ErrorCode
) -> bool:
    """`running` → `failed`：已經有存下輸出的 `llm_call` 時記 `llm_result_pending`（下一次接續、
    不再呼叫模型），否則記 `reason`。已經不是 `running` 就不改、回 False。
    """
    result = connection.execute(
        text(
            "UPDATE derivation_run SET status = 'failed',"
            f" failure_reason = CASE WHEN {_HAS_SAVED_OUTPUT} THEN :pending ELSE :reason END,"
            " finished_at = clock_timestamp(), updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'running'"
        ),
        {
            "id": run_id,
            "reason": ErrorCode(reason).value,
            "pending": ErrorCode.LLM_RESULT_PENDING.value,
        },
    )
    return result.rowcount == 1


def update_derivation_run_resumed(
    connection: Connection, key: RunKey, eval_run_id: uuid.UUID | None
) -> uuid.UUID | None:
    """把這組鍵（評估時還要同一個 `eval_run_id`）最早一筆 `llm_result_pending` 而且有存下輸出的
    run 改回 `running`（重設 `updated_at`，sweep 從現在起算），回它的 id；沒有就回 None。

    呼叫端要先持有這組鍵的 advisory lock，並確認沒有平常流程的 `running`（部分唯一索引）。
    在呼叫端開好的交易裡做，不 commit。
    """
    value = connection.execute(
        text(
            "UPDATE derivation_run SET status = 'running', failure_reason = NULL,"
            " finished_at = NULL, updated_at = clock_timestamp()"
            " WHERE status = 'failed' AND id = ("
            " SELECT r.id FROM derivation_run AS r"
            " WHERE r.paper_id = :paper_id AND r.task = :task"
            " AND r.prompt_version = :prompt_version AND r.model = :model"
            " AND r.input_sha256 = :input_sha256"
            " AND r.status = 'failed' AND r.failure_reason = :pending"
            " AND r.eval_run_id IS NOT DISTINCT FROM CAST(:eval_run_id AS uuid)"
            " AND EXISTS (SELECT 1 FROM llm_call AS c WHERE c.run_id = r.id"
            " AND (c.output_text IS NOT NULL OR c.output_reject_reason IS NOT NULL))"
            " ORDER BY r.created_at, r.id LIMIT 1)"
            " RETURNING id"
        ),
        {
            **_key_params(key),
            "pending": ErrorCode.LLM_RESULT_PENDING.value,
            "eval_run_id": eval_run_id,
        },
    ).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


def update_derivation_run_pending_settled(connection: Connection, run_id: uuid.UUID) -> bool:
    """`llm_result_pending` 的 run 已經寫了表頭（例如 sweep 判定逾時後原本的呼叫才寫成
    `superseded`）：原因改成 `llm_run_expired`，不再接續。不是 pending 就不改、回 False。
    """
    result = connection.execute(
        text(
            "UPDATE derivation_run SET failure_reason = :expired, updated_at = clock_timestamp()"
            " WHERE id = :id AND status = 'failed' AND failure_reason = :pending"
        ),
        {
            "id": run_id,
            "expired": ErrorCode.LLM_RUN_EXPIRED.value,
            "pending": ErrorCode.LLM_RESULT_PENDING.value,
        },
    )
    return result.rowcount == 1


def update_expired_derivation_runs(connection: Connection, older_than: timedelta) -> int:
    """把超過 `older_than` 沒有進展（`updated_at`；接續時會重設）還是 `running` 的互動呼叫改成
    `failed`，回改了幾筆：已經有存下輸出的 `llm_call` 記 `llm_result_pending`（下一次接續），
    否則記 `llm_run_expired`。Batch 的不看時限（跟著批次的結果走，ADR-0009）。
    """
    result = connection.execute(
        text(
            "UPDATE derivation_run SET status = 'failed',"
            f" failure_reason = CASE WHEN {_HAS_SAVED_OUTPUT} THEN :pending ELSE :reason END,"
            " finished_at = clock_timestamp(), updated_at = clock_timestamp()"
            " WHERE status = 'running' AND call_mode = 'interactive'"
            " AND updated_at < clock_timestamp() - :older_than"
        ),
        {
            "reason": ErrorCode.LLM_RUN_EXPIRED.value,
            "pending": ErrorCode.LLM_RESULT_PENDING.value,
            "older_than": older_than,
        },
    )
    return int(result.rowcount)


def update_derivation_runs_failed_for_batch(
    connection: Connection, batch_submission_id: uuid.UUID, reason: ErrorCode
) -> int:
    """這個批次裡還是 `running` 的 run 全部改 `failed`，回改了幾筆（Story 2.11：結果拿不到、結果
    裡缺了、人工標失敗）。已經存下輸出的 `llm_call`（之前匯入到一半中斷）記 `llm_result_pending`，
    下一次接續、不用再付一次錢；否則記 `reason`。已經不是 `running` 的不改。"""
    result = connection.execute(
        text(
            "UPDATE derivation_run SET status = 'failed',"
            f" failure_reason = CASE WHEN {_HAS_SAVED_OUTPUT} THEN :pending ELSE :reason END,"
            " finished_at = clock_timestamp(), updated_at = clock_timestamp()"
            " WHERE status = 'running' AND id IN ("
            " SELECT run_id FROM batch_item WHERE batch_submission_id = :batch_submission_id)"
        ),
        {
            "batch_submission_id": batch_submission_id,
            "reason": ErrorCode(reason).value,
            "pending": ErrorCode.LLM_RESULT_PENDING.value,
        },
    )
    return int(result.rowcount)


def create_batch_llm_call(connection: Connection, call: NewLlmCall) -> bool:
    """記一筆 Batch 結果的帳（Story 2.11、ADR-0043），回有沒有新增。

    這筆 run 已經有帳（之前匯入過、別的 poller 剛記了）就什麼都不寫、回 False
    （`ON CONFLICT (run_id) WHERE is_batch DO NOTHING`，遷移 0017 的部分唯一索引）。在呼叫端開好的
    交易裡做，不 commit。

    Raises:
        ValueError: `call.is_batch` 不是 True。
    """
    if call.is_batch is not True:
        raise ValueError("create_batch_llm_call 只記 Batch 的帳（is_batch=True）")
    output = call.output
    raw = (
        None if call.raw_response is None else json.dumps(dict(call.raw_response), allow_nan=False)
    )
    value = connection.execute(
        text(
            "INSERT INTO llm_call"
            " (run_id, input_tokens, output_tokens, cache_creation_input_tokens,"
            " cache_read_input_tokens, cost_usd, price_tier, is_batch, response_id, request_id,"
            " stop_reason, raw_response, output_text, output_reject_reason)"
            " VALUES (:run_id, :input_tokens, :output_tokens, :cache_creation_input_tokens,"
            " :cache_read_input_tokens, :cost_usd, :price_tier, true, :response_id,"
            " :request_id, :stop_reason, CAST(:raw_response AS jsonb), :output_text,"
            " :output_reject_reason)"
            " ON CONFLICT (run_id) WHERE is_batch DO NOTHING"
            " RETURNING id"
        ),
        {
            "run_id": call.run_id,
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
            "cache_creation_input_tokens": call.cache_creation_input_tokens,
            "cache_read_input_tokens": call.cache_read_input_tokens,
            "cost_usd": call.cost_usd,
            "price_tier": call.price_tier,
            "response_id": call.response_id,
            "request_id": call.request_id,
            "stop_reason": call.stop_reason,
            "raw_response": raw,
            "output_text": None if output is None else output.text,
            "output_reject_reason": (
                None
                if output is None or output.reject_reason is None
                else ErrorCode(output.reject_reason).value
            ),
        },
    ).scalar_one_or_none()
    return value is not None


def create_llm_call(connection: Connection, call: NewLlmCall) -> uuid.UUID:
    """新增一筆 `llm_call`，回它的 id。在呼叫端開好的交易裡做，不 commit。

    `raw_response` 要存得進 jsonb（呼叫端先用 `is_storable_json` 檢查，存不下就傳 None）；
    `output` 的文字已由 `SavedLlmOutput` 確認存得進 text。
    """
    output = call.output
    raw = (
        None if call.raw_response is None else json.dumps(dict(call.raw_response), allow_nan=False)
    )
    value = connection.execute(
        text(
            "INSERT INTO llm_call"
            " (run_id, input_tokens, output_tokens, cache_creation_input_tokens,"
            " cache_read_input_tokens, cost_usd, price_tier, is_batch, response_id, request_id,"
            " stop_reason, raw_response, output_text, output_reject_reason)"
            " VALUES (:run_id, :input_tokens, :output_tokens, :cache_creation_input_tokens,"
            " :cache_read_input_tokens, :cost_usd, :price_tier, :is_batch, :response_id,"
            " :request_id, :stop_reason, CAST(:raw_response AS jsonb), :output_text,"
            " :output_reject_reason)"
            " RETURNING id"
        ),
        {
            "run_id": call.run_id,
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
            "cache_creation_input_tokens": call.cache_creation_input_tokens,
            "cache_read_input_tokens": call.cache_read_input_tokens,
            "cost_usd": call.cost_usd,
            "price_tier": call.price_tier,
            "is_batch": call.is_batch,
            "response_id": call.response_id,
            "request_id": call.request_id,
            "stop_reason": call.stop_reason,
            "raw_response": raw,
            "output_text": None if output is None else output.text,
            "output_reject_reason": (
                None
                if output is None or output.reject_reason is None
                else ErrorCode(output.reject_reason).value
            ),
        },
    ).scalar_one()
    return uuid.UUID(str(value))
