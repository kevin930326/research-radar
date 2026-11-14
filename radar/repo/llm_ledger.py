"""`derivation_run`（來歷）與 `llm_call`（帳）兩張表：表的定義與唯一的寫入者（AD-8、ADR-0009、
ADR-0030）。

- `derivation_run`：一次模型呼叫一筆，狀態 `running` → `succeeded`（含被拒收）／`failed`（暫時性
  失敗、設定錯、sweep 判定逾時）。（對象, 工作, prompt 版本, 模型, input_sha256）有只算
  `running` 而且不是評估的部分唯一索引：同一份輸入同時只有一個在跑。
- `llm_call`：一次呼叫的 tokens 與費用（`numeric(12,6)`，美元）；`run_id` 唯一。應用程式角色對它
  沒有 UPDATE、DELETE（遷移 0010 收回）：帳只能新增。
- 兩張表都由 services 用自己的短交易寫、立刻 commit（Conventions 的例外：主交易回滾時帳照記）。
- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。
- 讀取函式第一個參數是 `viewer`，只給系統工作（`visible_to_system_only`）。
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
)
from radar.domain.errors import ErrorCode
from radar.domain.viewer import SystemViewer
from radar.repo.models import Base
from radar.repo.visibility import visible_to_system_only

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0010 建表） -----------------------------

_KEY_COLUMNS = ("paper_id", "task", "prompt_version", "model", "input_sha256")
_SHA256 = "'^[0-9a-f]{64}$'"

derivation_run_table = Table(
    "derivation_run",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("paper_id", Uuid, ForeignKey("paper.id"), nullable=False),
    Column("task", Text, nullable=False),
    Column("prompt_version", Integer, nullable=False),
    Column("prompt_sha256", Text, nullable=False),
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
    # sweep 找逾時的互動 running。
    Index(
        "ix_derivation_run_running_created_at",
        "created_at",
        postgresql_where=text("status = 'running'"),
    ),
    CheckConstraint(
        "task IN ('zh_summary', 'classify', 'extract_model', 'merge_judge', 'qa_answer',"
        " 'eval_judge')",
        name="task",
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
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("run_id"),
    CheckConstraint(
        "input_tokens >= 0 AND output_tokens >= 0 AND cache_creation_input_tokens >= 0"
        " AND cache_read_input_tokens >= 0",
        name="tokens_not_negative",
    ),
    CheckConstraint("cost_usd >= 0", name="cost_usd_not_negative"),
    CheckConstraint("price_tier IN ('standard', 'long_context')", name="price_tier"),
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
    viewer: SystemViewer, connection: Connection, key: RunKey
) -> uuid.UUID | None:
    """這組鍵有沒有平常流程（不是評估）的 `running`；有就回它的 id。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    value = connection.execute(
        text(
            f"SELECT id FROM derivation_run WHERE {visible}"
            " AND paper_id = :paper_id AND task = :task AND prompt_version = :prompt_version"
            " AND model = :model AND input_sha256 = :input_sha256"
            " AND status = 'running' AND eval_run_id IS NULL"
            " LIMIT 1"
        ),
        _key_params(key),
    ).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


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


def update_expired_derivation_runs(connection: Connection, older_than: timedelta) -> int:
    """把建立超過 `older_than` 還是 `running` 的互動呼叫改成 `failed`（`llm_run_expired`），
    回改了幾筆。Batch 的不看時限（跟著批次的結果走，ADR-0009）。
    """
    result = connection.execute(
        text(
            "UPDATE derivation_run SET status = 'failed', failure_reason = :reason,"
            " finished_at = clock_timestamp(), updated_at = clock_timestamp()"
            " WHERE status = 'running' AND call_mode = 'interactive'"
            " AND created_at < clock_timestamp() - :older_than"
        ),
        {"reason": ErrorCode.LLM_RUN_EXPIRED.value, "older_than": older_than},
    )
    return int(result.rowcount)


def create_llm_call(connection: Connection, call: NewLlmCall) -> uuid.UUID:
    """新增一筆 `llm_call`，回它的 id。在呼叫端開好的交易裡做，不 commit。

    `raw_response` 要存得進 jsonb（呼叫端先用 `is_storable_json` 檢查，存不下就傳 None）。
    """
    raw = None if call.raw_response is None else json.dumps(dict(call.raw_response))
    value = connection.execute(
        text(
            "INSERT INTO llm_call"
            " (run_id, input_tokens, output_tokens, cache_creation_input_tokens,"
            " cache_read_input_tokens, cost_usd, price_tier, is_batch, response_id, request_id,"
            " stop_reason, raw_response)"
            " VALUES (:run_id, :input_tokens, :output_tokens, :cache_creation_input_tokens,"
            " :cache_read_input_tokens, :cost_usd, :price_tier, :is_batch, :response_id,"
            " :request_id, :stop_reason, CAST(:raw_response AS jsonb))"
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
        },
    ).scalar_one()
    return uuid.UUID(str(value))
