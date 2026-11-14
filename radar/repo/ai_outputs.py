"""三張 AI 表頭表 `zh_summary`、`classification`、`research_model`（與掛在分類底下的
`classification_label`）：表的定義與唯一的讀寫者（AD-7、ADR-0015、ADR-0030）。

- 2.2 建表頭欄位（`paper_id`、`run_id`、`input_kind`、`input_sha256`、`abstract_id`、`lang`、
  `status`、`reject_reason`、`created_at`）；2.4 在 `zh_summary` 加四個內容欄位（遷移 0011，
  `rejected` 沒有內容、有值時不是空白，CHECK 擋）；2.5 在 `classification` 加
  `dropped_label_count`、新增 `classification_label`（遷移 0012，一列一個標籤）；`rejected` 沒有計數
  由 CHECK 擋，`rejected` 底下沒有標籤、標籤不 UPDATE 則由 `NewAiResult`／`create_ai_result` 保證
  （程式，不是資料庫）；研究模型的內容由 2.6 加。
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
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    TextClause,
    UniqueConstraint,
    Uuid,
    text,
)

from radar.domain.ai_outputs import (
    HEADER_TABLES,
    ClassificationContent,
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


def _header_table(name: str, *content: Column[Any] | CheckConstraint) -> Table:
    """一張表頭表：四張表的表頭欄位都一樣（ADR-0015 決定第 2 點）；`content` 是這張表自己的
    內容欄位與約束。"""
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
        *content,
    )


# 中文重點的四欄內容（Story 2.4、ADR-0032）：沒提到的欄位是 NULL。
ZH_SUMMARY_CONTENT_COLUMNS: Final = ("research_question", "method", "theory", "findings")

zh_summary_table = _header_table(
    "zh_summary",
    *(Column(column, Text) for column in ZH_SUMMARY_CONTENT_COLUMNS),
    CheckConstraint(
        "status <> 'rejected' OR (research_question IS NULL AND method IS NULL"
        " AND theory IS NULL AND findings IS NULL)",
        name="rejected_has_no_content",
    ),
    CheckConstraint(
        "(research_question IS NULL OR research_question ~ '[^[:space:]]')"
        " AND (method IS NULL OR method ~ '[^[:space:]]')"
        " AND (theory IS NULL OR theory ~ '[^[:space:]]')"
        " AND (findings IS NULL OR findings ~ '[^[:space:]]')",
        name="content_not_blank",
    ),
)
# 分類（Story 2.5、ADR-0033）：表頭只多一欄「丟掉幾個清單外的標籤」，標籤在 classification_label。
# 2.2 留下的表頭（沒有內容）與 rejected 的計數是 NULL。
classification_table = _header_table(
    "classification",
    Column("dropped_label_count", Integer),
    CheckConstraint(
        "dropped_label_count IS NULL OR dropped_label_count >= 0",
        name="dropped_label_count_not_negative",
    ),
    CheckConstraint(
        "status <> 'rejected' OR dropped_label_count IS NULL", name="rejected_has_no_dropped_count"
    ),
)
# 一列一個標籤；細項不帶自己的狀態，跟著表頭走（ADR-0015）。`unclassified` 只能是主題。
classification_label_table = Table(
    "classification_label",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("classification_id", Uuid, ForeignKey("classification.id"), nullable=False),
    Column("field", Text, nullable=False),
    Column("key", Text, nullable=False),
    # 同一列表頭、同一欄的同一個 key 只有一列；也當 classification_id 外鍵的索引。
    UniqueConstraint("classification_id", "field", "key"),
    CheckConstraint("field IN ('topic', 'method')", name="field"),
    CheckConstraint("key ~ '^[a-z][a-z0-9_]*$'", name="key_format"),
    CheckConstraint("field = 'topic' OR key <> 'unclassified'", name="unclassified_only_topic"),
)
research_model_table = _header_table("research_model")

# --- SQL（每張表各一份字面值） -------------------------------------------------------------

_INSERT_COLUMNS = (
    " (paper_id, run_id, input_kind, input_sha256, abstract_id, lang, status, reject_reason)"
    " VALUES (:paper_id, :run_id, :input_kind, :input_sha256, :abstract_id, :lang, :status,"
    " :reject_reason) RETURNING id"
)

_INSERT: Final[Mapping[LlmTask, TextClause]] = MappingProxyType(
    {
        LlmTask.ZH_SUMMARY: text(
            "INSERT INTO zh_summary (paper_id, run_id, input_kind, input_sha256, abstract_id,"
            " lang, status, reject_reason, research_question, method, theory, findings)"
            " VALUES (:paper_id, :run_id, :input_kind, :input_sha256, :abstract_id, :lang, :status,"
            " :reject_reason, :research_question, :method, :theory, :findings) RETURNING id"
        ),
        LlmTask.CLASSIFY: text(
            "INSERT INTO classification (paper_id, run_id, input_kind, input_sha256, abstract_id,"
            " lang, status, reject_reason, dropped_label_count)"
            " VALUES (:paper_id, :run_id, :input_kind, :input_sha256, :abstract_id, :lang, :status,"
            " :reject_reason, :dropped_label_count) RETURNING id"
        ),
        LlmTask.EXTRACT_MODEL: text("INSERT INTO research_model" + _INSERT_COLUMNS),
    }
)

_INSERT_CLASSIFICATION_LABEL: Final = text(
    "INSERT INTO classification_label (classification_id, field, key)"
    " VALUES (:classification_id, :field, :key)"
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
    """新增一列表頭（`zh_summary` 連同四欄內容；`classify` 連同計數，寫完表頭再寫標籤），回它的
    id。`active` 之前要先 `update_ai_results_superseded`（部分唯一索引擋第二筆 `active`）。在呼叫端
    開好的交易裡做，不 commit。

    Raises:
        ValueError: 這個工作沒有表頭表，或帶了不屬於這個工作的內容（不是 `zh_summary` 卻帶中文
            重點、不是 `classify` 卻帶分類）。
    """
    task = _check_task(task)
    params: dict[str, object] = {
        "paper_id": result.paper_id,
        "run_id": result.run_id,
        "input_kind": result.input_kind.value,
        "input_sha256": result.input_sha256,
        "abstract_id": result.abstract_id,
        "lang": result.lang.value,
        "status": result.status.value,
        "reject_reason": None if result.reject_reason is None else result.reject_reason.value,
    }
    if task is LlmTask.ZH_SUMMARY:
        content = result.zh_summary.fields() if result.zh_summary is not None else {}
        params.update({column: content.get(column) for column in ZH_SUMMARY_CONTENT_COLUMNS})
    elif result.zh_summary is not None:
        raise ValueError(f"{task.value} 的表頭不收中文重點的內容")
    classification = result.classification
    if task is LlmTask.CLASSIFY:
        params["dropped_label_count"] = (
            None if classification is None else classification.dropped_label_count
        )
    elif classification is not None:
        raise ValueError(f"{task.value} 的表頭不收分類的內容")
    header_id = uuid.UUID(str(connection.execute(_INSERT[task], params).scalar_one()))
    if classification is not None:
        _create_classification_labels(connection, header_id, classification)
    return header_id


def _create_classification_labels(
    connection: Connection, classification_id: uuid.UUID, content: ClassificationContent
) -> None:
    """寫這列分類表頭的標籤（一個 key 一列）。`ClassificationContent` 至少有一個主題，所以一定
    有列。"""
    connection.execute(
        _INSERT_CLASSIFICATION_LABEL,
        [
            {"classification_id": classification_id, "field": field.value, "key": key}
            for field, key in content.labels()
        ],
    )


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
