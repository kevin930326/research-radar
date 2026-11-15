"""三張 AI 表頭表 `zh_summary`、`classification`、`research_model`：表的定義與唯一的讀寫者
（AD-7、ADR-0015、ADR-0030）。

- 2.2 只建表頭欄位（`paper_id`、`run_id`、`input_kind`、`input_sha256`、`abstract_id`、`lang`、
  `status`、`reject_reason`、`created_at`）；內容欄位由 2.4～2.6 加。
- 同一篇論文、同一種語言最多一筆 `active`（部分唯一索引）。內容不 UPDATE：重跑就新增一列，
  舊的 `active` 標成 `superseded`（只改 `status`）。
- 結果只經 `services/ai_results.py` 寫入；`repo` 裡除了這個模組，其他查詢不直接讀這三張表
  （`tests/arch/test_ai_table_reads.py`；之後的有效值 view 在遷移裡）。
- 寫入 SQL 每張表各寫一份字面值（架構測試掃字串確認一張表一個寫入者，表名不能是變數）。
- `abstract_id` 不設外鍵（ADR-0030）：`paper_abstract` 在來源拿掉摘要時會被刪掉，外鍵會讓每週
  更新失敗；表頭是歷史，留著原本的 id。
"""

import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    Table,
    Text,
    TextClause,
    UniqueConstraint,
    Uuid,
    text,
)

from radar.domain.ai_outputs import (
    HEADER_TABLES,
    LlmTask,
    NewAiResult,
    OutputLang,
    ResultStatus,
    RunKey,
)
from radar.domain.viewer import SystemViewer
from radar.repo.models import Base
from radar.repo.visibility import visible_to_system_only

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0010 建表） -----------------------------


def _header_table(name: str) -> Table:
    """一張表頭表：四張表的表頭欄位都一樣（ADR-0015 決定第 2 點）。"""
    return Table(
        name,
        Base.metadata,
        Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
        Column("paper_id", Uuid, ForeignKey("paper.id"), nullable=False),
        Column("run_id", Uuid, ForeignKey("derivation_run.id"), nullable=False),
        Column("input_kind", Text, nullable=False),
        Column("input_sha256", Text, nullable=False),
        Column("abstract_id", Uuid),
        Column("lang", Text, nullable=False),
        Column("status", Text, nullable=False),
        Column("reject_reason", Text),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
        # 一次模型呼叫最多一列表頭；同時當 run_id 外鍵的索引。
        UniqueConstraint("run_id"),
        # paper_id 外鍵的索引（下面的部分唯一索引不算）。
        Index(f"ix_{name}_paper_id", "paper_id"),
        # 依摘要找衍生產出（FR-11）。
        Index(f"ix_{name}_abstract_id", "abstract_id"),
        Index(
            f"uq_{name}_active_lang",
            "paper_id",
            "lang",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        CheckConstraint("input_kind IN ('title', 'abstract', 'fulltext')", name="input_kind"),
        CheckConstraint("input_sha256 ~ '^[0-9a-f]{64}$'", name="input_sha256_format"),
        CheckConstraint("lang IN ('zh-Hant', 'en', 'zxx')", name="lang"),
        CheckConstraint(
            "status IN ('candidate', 'active', 'superseded', 'rejected')", name="status"
        ),
        CheckConstraint(
            "(status = 'rejected') = (reject_reason IS NOT NULL)", name="reject_reason_iff_rejected"
        ),
        CheckConstraint(
            "reject_reason IS NULL OR reject_reason ~ '^[a-z][a-z0-9_]*$'",
            name="reject_reason_format",
        ),
    )


zh_summary_table = _header_table("zh_summary")
classification_table = _header_table("classification")
research_model_table = _header_table("research_model")

# --- SQL（每張表各一份字面值） -------------------------------------------------------------

_INSERT_COLUMNS = (
    " (paper_id, run_id, input_kind, input_sha256, abstract_id, lang, status, reject_reason)"
    " VALUES (:paper_id, :run_id, :input_kind, :input_sha256, :abstract_id, :lang, :status,"
    " :reject_reason) RETURNING id"
)

_INSERT: Final[Mapping[LlmTask, TextClause]] = MappingProxyType(
    {
        LlmTask.ZH_SUMMARY: text("INSERT INTO zh_summary" + _INSERT_COLUMNS),
        LlmTask.CLASSIFY: text("INSERT INTO classification" + _INSERT_COLUMNS),
        LlmTask.EXTRACT_MODEL: text("INSERT INTO research_model" + _INSERT_COLUMNS),
    }
)

_SUPERSEDE_WHERE = (
    " SET status = 'superseded' WHERE paper_id = :paper_id AND lang = :lang AND status = 'active'"
)

_SUPERSEDE: Final[Mapping[LlmTask, TextClause]] = MappingProxyType(
    {
        LlmTask.ZH_SUMMARY: text("UPDATE zh_summary" + _SUPERSEDE_WHERE),
        LlmTask.CLASSIFY: text("UPDATE classification" + _SUPERSEDE_WHERE),
        LlmTask.EXTRACT_MODEL: text("UPDATE research_model" + _SUPERSEDE_WHERE),
    }
)


def _check_task(task: LlmTask) -> LlmTask:
    task = LlmTask(task)
    if task not in HEADER_TABLES:
        raise ValueError(f"{task.value} 沒有表頭表")
    return task


# --- 讀取 ---------------------------------------------------------------------------------


def find_blocking_ai_result(
    viewer: SystemViewer, connection: Connection, key: RunKey
) -> ResultStatus | None:
    """這組鍵已經有 `active`，或有平常流程留下的 `rejected` 時回那個狀態；都沒有回 None。

    評估留下的 `candidate` 與 `rejected` 不算（ADR-0009 第 4 點）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: 這個工作沒有表頭表。
    """
    visible = visible_to_system_only(viewer)
    task = _check_task(key.task)
    params = {
        "paper_id": key.paper_id,
        "task": task.value,
        "prompt_version": key.prompt_version,
        "model": key.model,
        "input_sha256": key.input_sha256,
    }
    match task:
        case LlmTask.ZH_SUMMARY:
            sql = text(
                "SELECT h.status FROM zh_summary AS h JOIN derivation_run AS r ON r.id = h.run_id"
                f" WHERE {visible} AND r.paper_id = :paper_id AND r.task = :task"
                " AND r.prompt_version = :prompt_version AND r.model = :model"
                " AND r.input_sha256 = :input_sha256"
                " AND (h.status = 'active' OR (h.status = 'rejected' AND r.eval_run_id IS NULL))"
                " ORDER BY h.status LIMIT 1"
            )
        case LlmTask.CLASSIFY:
            sql = text(
                "SELECT h.status FROM classification AS h"
                " JOIN derivation_run AS r ON r.id = h.run_id"
                f" WHERE {visible} AND r.paper_id = :paper_id AND r.task = :task"
                " AND r.prompt_version = :prompt_version AND r.model = :model"
                " AND r.input_sha256 = :input_sha256"
                " AND (h.status = 'active' OR (h.status = 'rejected' AND r.eval_run_id IS NULL))"
                " ORDER BY h.status LIMIT 1"
            )
        case _:
            sql = text(
                "SELECT h.status FROM research_model AS h"
                " JOIN derivation_run AS r ON r.id = h.run_id"
                f" WHERE {visible} AND r.paper_id = :paper_id AND r.task = :task"
                " AND r.prompt_version = :prompt_version AND r.model = :model"
                " AND r.input_sha256 = :input_sha256"
                " AND (h.status = 'active' OR (h.status = 'rejected' AND r.eval_run_id IS NULL))"
                " ORDER BY h.status LIMIT 1"
            )
    value = connection.execute(sql, params).scalar_one_or_none()
    return None if value is None else ResultStatus(value)


# --- 寫入 ---------------------------------------------------------------------------------


def create_ai_result(connection: Connection, task: LlmTask, result: NewAiResult) -> uuid.UUID:
    """新增一列表頭，回它的 id。`active` 之前要先 `update_ai_results_superseded`
    （部分唯一索引擋第二筆 `active`）。在呼叫端開好的交易裡做，不 commit。

    Raises:
        ValueError: 這個工作沒有表頭表。
    """
    task = _check_task(task)
    value = connection.execute(
        _INSERT[task],
        {
            "paper_id": result.paper_id,
            "run_id": result.run_id,
            "input_kind": result.input_kind.value,
            "input_sha256": result.input_sha256,
            "abstract_id": result.abstract_id,
            "lang": result.lang.value,
            "status": result.status.value,
            "reject_reason": None if result.reject_reason is None else result.reject_reason.value,
        },
    ).scalar_one()
    return uuid.UUID(str(value))


def update_ai_results_superseded(
    connection: Connection, task: LlmTask, paper_id: uuid.UUID, lang: OutputLang
) -> int:
    """把這篇、這種語言現在的 `active` 標成 `superseded`，回改了幾列（0 或 1）。

    Raises:
        ValueError: 這個工作沒有表頭表。
    """
    task = _check_task(task)
    result = connection.execute(
        _SUPERSEDE[task], {"paper_id": paper_id, "lang": OutputLang(lang).value}
    )
    return int(result.rowcount)
