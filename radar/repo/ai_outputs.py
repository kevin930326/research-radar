"""三張 AI 表頭表 `zh_summary`、`classification`、`research_model`（與掛在底下的細項
`classification_label`、`construct_mention`、`hypothesis`）：表的定義與唯一的讀寫者（AD-7、
ADR-0015、ADR-0030）。

- 2.2 建表頭欄位（`paper_id`、`run_id`、`input_kind`、`input_sha256`、`abstract_id`、`lang`、
  `status`、`reject_reason`、`created_at`）；2.4 在 `zh_summary` 加四個內容欄位（遷移 0011，
  `rejected` 沒有內容、有值時不是空白，CHECK 擋）；2.5 在 `classification` 加
  `dropped_label_count`、新增 `classification_label`（遷移 0012，一列一個標籤）；`rejected` 沒有計數
  由 CHECK 擋，`rejected` 底下沒有標籤、標籤不 UPDATE 則由 `NewAiResult`／`create_ai_result` 保證
  （程式，不是資料庫）；2.6 在 `research_model` 加論文層級的內容欄位與四個計數、新增
  `construct_mention`、`hypothesis`（遷移 0013；構面本身在 `repo/constructs.py`，寫研究模型時經
  `upsert_constructs` 新增）；`rejected` 沒有內容與計數由 CHECK 擋。
- 同一篇論文、同一種語言最多一筆 `active`（部分唯一索引）。內容不 UPDATE：重跑就新增一列，
  舊的 `active` 標成 `superseded`（只改 `status`）。
- 結果只經 `services/ai_results.py` 寫入；`repo` 裡除了這個模組，其他查詢不直接讀表頭與細項表
  （`tests/arch/test_ai_table_reads.py`）。有效值讀遷移 0014 的 `effective_*` view
  （`repo/ai_blocks.py`）。
- 寫入 SQL 每張表各寫一份字面值（架構測試掃字串確認一張表一個寫入者，表名不能是變數）。
- 唯一的刪除路徑是 `delete_ai_results_by_abstract_source`（`radar runs remove-by-source`，
  Story 2.12、FR-11、AD-26）：摘要來源授權改變時，依 `derivation_run.abstract_source` 直接刪表頭與
  細項，不複製被刪的內容。
- `abstract_id` 不設外鍵（ADR-0030）：`paper_abstract` 在來源拿掉摘要時會被刪掉，外鍵會讓每週
  更新失敗；表頭是歷史，留著原本的 id。
"""

import uuid
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final

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
    Table,
    Text,
    TextClause,
    UniqueConstraint,
    Uuid,
    text,
)

from radar.domain.admin_runs import (
    REMOVAL_TABLES,
    AiHistoryRow,
    LatestAiResult,
    SourceAiCounts,
)
from radar.domain.ai_outputs import (
    HEADER_TABLES,
    ClassificationContent,
    InputKind,
    LlmTask,
    NewAiResult,
    OutputLang,
    ResearchModelContent,
    ResultStatus,
    RunKey,
    RunStatus,
)
from radar.domain.llm_batches import BATCH_TASKS
from radar.domain.papers import AbstractSource
from radar.domain.run_reports import RunOutcomeRow, RunScope, run_outcome
from radar.domain.viewer import SystemViewer, Viewer
from radar.repo.constructs import delete_constructs, upsert_constructs
from radar.repo.db import Database
from radar.repo.models import Base
from radar.repo.visibility import visible_papers, visible_to_system_only

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
# 研究模型（Story 2.6、ADR-0034）：論文層級的擷取結果放在表頭（ADR-0012 第 5 點、ADR-0015
# 決定第 5 點），構面與假設在 construct_mention、hypothesis。2.2 留下的表頭（沒有內容）與 rejected
# 的內容、計數都是 NULL。情境欄位是情境值清單的 key。
RESEARCH_MODEL_CONTEXT_COLUMNS: Final = (
    "country",
    "industry",
    "research_design",
    "unit_of_analysis",
)
RESEARCH_MODEL_TEXT_COLUMNS: Final = ("limitations", "future_research")
RESEARCH_MODEL_COUNT_COLUMNS: Final = (
    "dropped_context_value_count",
    "unsupported_context_value_count",
    "corrected_support_count",
    "cleared_value_count",
)
_RESEARCH_MODEL_CONTENT_COLUMNS: Final = (
    "is_review_or_meta_analysis",
    *RESEARCH_MODEL_CONTEXT_COLUMNS,
    "sample_size",
    *RESEARCH_MODEL_TEXT_COLUMNS,
    *RESEARCH_MODEL_COUNT_COLUMNS,
)
research_model_table = _header_table(
    "research_model",
    Column("is_review_or_meta_analysis", Boolean),
    *(Column(column, Text) for column in RESEARCH_MODEL_CONTEXT_COLUMNS),
    Column("sample_size", Integer),
    *(Column(column, Text) for column in RESEARCH_MODEL_TEXT_COLUMNS),
    *(Column(column, Integer) for column in RESEARCH_MODEL_COUNT_COLUMNS),
    CheckConstraint(
        " AND ".join(
            f"({column} IS NULL OR {column} ~ '^[a-z][a-z0-9_]*$')"
            for column in RESEARCH_MODEL_CONTEXT_COLUMNS
        ),
        name="context_key_format",
    ),
    CheckConstraint("sample_size IS NULL OR sample_size >= 1", name="sample_size_positive"),
    CheckConstraint(
        "(limitations IS NULL OR limitations ~ '[^[:space:]]')"
        " AND (future_research IS NULL OR future_research ~ '[^[:space:]]')",
        name="content_not_blank",
    ),
    CheckConstraint(
        " AND ".join(
            f"({column} IS NULL OR {column} >= 0)" for column in RESEARCH_MODEL_COUNT_COLUMNS
        ),
        name="counts_not_negative",
    ),
    CheckConstraint(
        "status <> 'rejected' OR ("
        + " AND ".join(f"{column} IS NULL" for column in _RESEARCH_MODEL_CONTENT_COLUMNS)
        + ")",
        name="rejected_has_no_content",
    ),
)
# 一篇論文提到的一個構面；指向原本的構面（不因合併而改，ADR-0012 第 5 點）。細項不帶自己的狀態。
construct_mention_table = Table(
    "construct_mention",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("research_model_id", Uuid, ForeignKey("research_model.id"), nullable=False),
    Column("construct_id", Uuid, ForeignKey("construct.id"), nullable=False),
    Column("name_en", Text, nullable=False),
    Column("label_zh", Text, nullable=False),
    Column("definition", Text),
    Column("scale_source", Text),
    Column("item_location", Text),
    Column("item_count", Integer),
    Column("cronbach_alpha", Numeric),
    Column("composite_reliability", Numeric),
    Column("ave", Numeric),
    # 同一次擷取、同一個構面只有一列；也當 research_model_id 外鍵的索引。
    UniqueConstraint("research_model_id", "construct_id"),
    Index("ix_construct_mention_construct_id", "construct_id"),
    CheckConstraint(
        "name_en ~ '[^[:space:]]' AND label_zh ~ '[^[:space:]]'"
        " AND (definition IS NULL OR definition ~ '[^[:space:]]')"
        " AND (scale_source IS NULL OR scale_source ~ '[^[:space:]]')"
        " AND (item_location IS NULL OR item_location ~ '[^[:space:]]')",
        name="text_not_blank",
    ),
    CheckConstraint("item_count IS NULL OR item_count >= 1", name="item_count_positive"),
    CheckConstraint(
        "(cronbach_alpha IS NULL OR cronbach_alpha BETWEEN 0 AND 1)"
        " AND (composite_reliability IS NULL OR composite_reliability BETWEEN 0 AND 1)"
        " AND (ave IS NULL OR ave BETWEEN 0 AND 1)",
        name="reliability_range",
    ),
)
# 一條關係；因與果指向原本的構面。`sentence_sha256` 是來源句子（原句在輸入裡所在的完整句子）的
# 雜湊（「不同原句」用；片段和完整版算同一句）。
hypothesis_table = Table(
    "hypothesis",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("research_model_id", Uuid, ForeignKey("research_model.id"), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("sentence", Text, nullable=False),
    Column("sentence_sha256", Text, nullable=False),
    Column("cause_construct_id", Uuid, ForeignKey("construct.id"), nullable=False),
    Column("effect_construct_id", Uuid, ForeignKey("construct.id"), nullable=False),
    Column("evidence_type", Text, nullable=False),
    Column("support", Text, nullable=False),
    Column("beta", Numeric),
    Column("p_value", Numeric),
    Column("sample_size", Integer),
    Column("effect_type", Text),
    Column("result_quote", Text),
    # 也當 research_model_id 外鍵的索引。
    UniqueConstraint("research_model_id", "ordinal"),
    # 同一次擷取、同一句、同一條關係只有一列（FR-3 擷取規則 3）；自動產生的名字超過 63 字，給短名。
    UniqueConstraint(
        "research_model_id",
        "sentence_sha256",
        "cause_construct_id",
        "effect_construct_id",
        name="uq_hypothesis_sentence_relation",
    ),
    Index("ix_hypothesis_cause_construct_id", "cause_construct_id"),
    Index("ix_hypothesis_effect_construct_id", "effect_construct_id"),
    CheckConstraint("ordinal >= 1", name="ordinal_positive"),
    CheckConstraint(
        "sentence ~ '[^[:space:]]' AND (effect_type IS NULL OR effect_type ~ '[^[:space:]]')"
        " AND (result_quote IS NULL OR result_quote ~ '[^[:space:]]')",
        name="text_not_blank",
    ),
    CheckConstraint("sentence_sha256 ~ '^[0-9a-f]{64}$'", name="sentence_sha256_format"),
    # FR-3 擷取規則 1：判定了支持與否就一定有寫出結果的原文，沒判定就沒有。
    CheckConstraint(
        "(support IN ('supported', 'not_supported', 'partially_supported'))"
        " = (result_quote IS NOT NULL)",
        name="result_quote_matches_support",
    ),
    CheckConstraint(
        "evidence_type IN ('empirical_test', 'analytical', 'simulation', 'qualitative',"
        " 'conceptual')",
        name="evidence_type",
    ),
    CheckConstraint(
        "support IN ('supported', 'not_supported', 'partially_supported', 'not_stated',"
        " 'not_determined')",
        name="support",
    ),
    # FR-3 擷取規則 1、2：只有實證檢定判定支持與否，其他證據類型一律 not_determined。
    CheckConstraint(
        "(evidence_type = 'empirical_test') = (support <> 'not_determined')",
        name="support_matches_evidence_type",
    ),
    CheckConstraint("cause_construct_id <> effect_construct_id", name="cause_differs_from_effect"),
    # beta 是有限數（numeric 收得下 NaN 與正負無限大；NaN 比任何數都大）。
    CheckConstraint(
        "beta IS NULL OR (beta > '-Infinity'::numeric AND beta < 'Infinity'::numeric)",
        name="beta_finite",
    ),
    CheckConstraint("p_value IS NULL OR p_value BETWEEN 0 AND 1", name="p_value_range"),
    CheckConstraint("sample_size IS NULL OR sample_size >= 1", name="sample_size_positive"),
)

# --- SQL（每張表各一份字面值） -------------------------------------------------------------

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
        LlmTask.EXTRACT_MODEL: text(
            "INSERT INTO research_model (paper_id, run_id, input_kind, input_sha256, abstract_id,"
            " lang, status, reject_reason, is_review_or_meta_analysis, country, industry,"
            " research_design, unit_of_analysis, sample_size, limitations, future_research,"
            " dropped_context_value_count, unsupported_context_value_count,"
            " corrected_support_count, cleared_value_count)"
            " VALUES (:paper_id, :run_id, :input_kind, :input_sha256, :abstract_id, :lang, :status,"
            " :reject_reason, :is_review_or_meta_analysis, :country, :industry, :research_design,"
            " :unit_of_analysis, :sample_size, :limitations, :future_research,"
            " :dropped_context_value_count, :unsupported_context_value_count,"
            " :corrected_support_count, :cleared_value_count) RETURNING id"
        ),
    }
)

_INSERT_CONSTRUCT_MENTION: Final = text(
    "INSERT INTO construct_mention (research_model_id, construct_id, name_en, label_zh,"
    " definition, scale_source, item_location, item_count, cronbach_alpha,"
    " composite_reliability, ave)"
    " VALUES (:research_model_id, :construct_id, :name_en, :label_zh, :definition,"
    " :scale_source, :item_location, :item_count, :cronbach_alpha, :composite_reliability,"
    " :ave)"
)

_INSERT_HYPOTHESIS: Final = text(
    "INSERT INTO hypothesis (research_model_id, ordinal, sentence, sentence_sha256,"
    " cause_construct_id, effect_construct_id, evidence_type, support, beta, p_value,"
    " sample_size, effect_type, result_quote)"
    " VALUES (:research_model_id, :ordinal, :sentence, :sentence_sha256, :cause_construct_id,"
    " :effect_construct_id, :evidence_type, :support, :beta, :p_value, :sample_size,"
    " :effect_type, :result_quote)"
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


def _key_params(key: RunKey, task: LlmTask) -> dict[str, object]:
    return {
        "paper_id": key.paper_id,
        "task": task.value,
        "prompt_version": key.prompt_version,
        "model": key.model,
        "input_sha256": key.input_sha256,
    }


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


def find_rejected_ai_result_id(
    viewer: Viewer, connection: Connection, key: RunKey
) -> uuid.UUID | None:
    """這組鍵有平常流程留下的 `rejected` 時回那列表頭的 id（有好幾列時回最早的）；沒有、論文不存在
    或 `viewer` 看不到時回 None。AI 區塊判定 `failed` 用（ADR-0015 判定順序第 4 步）。

    條件和 `find_blocking_ai_result` 的 `rejected` 分支一樣：評估留下的 `rejected` 不算。

    Raises:
        ValueError: 這個工作沒有表頭表。
    """
    visible = visible_papers(viewer, alias="paper")
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
                "SELECT h.id FROM zh_summary AS h JOIN derivation_run AS r ON r.id = h.run_id"
                " JOIN paper ON paper.id = h.paper_id"
                f" WHERE {visible} AND r.paper_id = :paper_id AND r.task = :task"
                " AND r.prompt_version = :prompt_version AND r.model = :model"
                " AND r.input_sha256 = :input_sha256"
                " AND h.status = 'rejected' AND r.eval_run_id IS NULL"
                " ORDER BY h.created_at, h.id LIMIT 1"
            )
        case LlmTask.CLASSIFY:
            sql = text(
                "SELECT h.id FROM classification AS h JOIN derivation_run AS r ON r.id = h.run_id"
                " JOIN paper ON paper.id = h.paper_id"
                f" WHERE {visible} AND r.paper_id = :paper_id AND r.task = :task"
                " AND r.prompt_version = :prompt_version AND r.model = :model"
                " AND r.input_sha256 = :input_sha256"
                " AND h.status = 'rejected' AND r.eval_run_id IS NULL"
                " ORDER BY h.created_at, h.id LIMIT 1"
            )
        case _:
            sql = text(
                "SELECT h.id FROM research_model AS h JOIN derivation_run AS r ON r.id = h.run_id"
                " JOIN paper ON paper.id = h.paper_id"
                f" WHERE {visible} AND r.paper_id = :paper_id AND r.task = :task"
                " AND r.prompt_version = :prompt_version AND r.model = :model"
                " AND r.input_sha256 = :input_sha256"
                " AND h.status = 'rejected' AND r.eval_run_id IS NULL"
                " ORDER BY h.created_at, h.id LIMIT 1"
            )
    value = connection.execute(sql, params).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


def list_run_outcomes(
    viewer: SystemViewer, connection: Connection, scope: RunScope
) -> list[RunOutcomeRow]:
    """`radar runs report` 的列：範圍內三個 Batch 工作的平常流程 run（不含評估），每篇每工作只取
    `created_at` 最新的一筆，連同它的表頭狀態（有的話）與原因代碼（拒收是表頭的 `reject_reason`，
    失敗是 run 的 `failure_reason`）。依論文 id、工作排序。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    filters = ""
    params: dict[str, object] = {"tasks": [task.value for task in BATCH_TASKS]}
    if scope.batch_ids:
        filters += (
            " AND r.id IN (SELECT run_id FROM batch_item"
            " WHERE batch_submission_id = ANY(:batch_ids))"
        )
        params["batch_ids"] = list(scope.batch_ids)
    if scope.paper_ids is not None:
        filters += " AND r.paper_id = ANY(:paper_ids)"
        params["paper_ids"] = list(scope.paper_ids)
    if scope.since is not None:
        filters += " AND r.created_at >= :since"
        params["since"] = scope.since
    rows = connection.execute(
        text(
            "SELECT DISTINCT ON (r.paper_id, r.task) r.paper_id, r.task, r.status,"
            " r.failure_reason, h.status AS header_status, h.reject_reason"
            " FROM derivation_run AS r LEFT JOIN ("
            " SELECT run_id, status, reject_reason FROM zh_summary"
            " UNION ALL SELECT run_id, status, reject_reason FROM classification"
            " UNION ALL SELECT run_id, status, reject_reason FROM research_model"
            ") AS h ON h.run_id = r.id"
            f" WHERE {visible} AND r.eval_run_id IS NULL AND r.task = ANY(:tasks){filters}"
            " ORDER BY r.paper_id, r.task, r.created_at DESC, r.id DESC"
        ),
        params,
    ).mappings()
    result: list[RunOutcomeRow] = []
    for row in rows:
        header = None if row["header_status"] is None else ResultStatus(row["header_status"])
        outcome = run_outcome(RunStatus(row["status"]), header)
        reason = row["reject_reason"] if header is ResultStatus.REJECTED else row["failure_reason"]
        result.append(RunOutcomeRow(row["paper_id"], LlmTask(row["task"]), outcome, reason))
    return result


def find_latest_ai_result(
    viewer: SystemViewer, connection: Connection, key: RunKey
) -> LatestAiResult | None:
    """這組鍵最新的一列平常流程（不是評估）表頭（`created_at, id` 最新），連同狀態與拒收原因；
    沒有回 None。`radar runs retry` 與閘門鎖內的 `retry_of` 核對用（Story 2.12）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: 這個工作沒有表頭表。
    """
    visible = visible_to_system_only(viewer)
    task = _check_task(key.task)
    where = (
        f" WHERE {visible} AND r.paper_id = :paper_id AND r.task = :task"
        " AND r.prompt_version = :prompt_version AND r.model = :model"
        " AND r.input_sha256 = :input_sha256 AND r.eval_run_id IS NULL"
        " ORDER BY h.created_at DESC, h.id DESC LIMIT 1"
    )
    match task:
        case LlmTask.ZH_SUMMARY:
            sql = text(
                "SELECT h.id, h.status, h.reject_reason FROM zh_summary AS h"
                " JOIN derivation_run AS r ON r.id = h.run_id" + where
            )
        case LlmTask.CLASSIFY:
            sql = text(
                "SELECT h.id, h.status, h.reject_reason FROM classification AS h"
                " JOIN derivation_run AS r ON r.id = h.run_id" + where
            )
        case _:
            sql = text(
                "SELECT h.id, h.status, h.reject_reason FROM research_model AS h"
                " JOIN derivation_run AS r ON r.id = h.run_id" + where
            )
    row = connection.execute(sql, _key_params(key, task)).mappings().one_or_none()
    if row is None:
        return None
    return LatestAiResult(
        id=uuid.UUID(str(row["id"])),
        status=ResultStatus(row["status"]),
        reject_reason=row["reject_reason"],
    )


def list_paper_ai_history(
    viewer: SystemViewer, connection: Connection, paper_id: uuid.UUID
) -> list[AiHistoryRow]:
    """這篇論文三個工作的全部 run（含評估，`is_eval` 標出來），有表頭時連同表頭的狀態與拒收原因；
    依 run 的建立時間（舊到新）。`radar runs show` 用（Story 2.12）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    rows = connection.execute(
        text(
            "SELECT r.id, r.task, r.status, r.failure_reason, h.status AS header_status,"
            " h.reject_reason, r.model, r.prompt_version, r.input_kind, r.abstract_source,"
            " r.created_at, r.eval_run_id IS NOT NULL AS is_eval"
            " FROM derivation_run AS r LEFT JOIN ("
            " SELECT run_id, status, reject_reason FROM zh_summary"
            " UNION ALL SELECT run_id, status, reject_reason FROM classification"
            " UNION ALL SELECT run_id, status, reject_reason FROM research_model"
            ") AS h ON h.run_id = r.id"
            f" WHERE {visible} AND r.paper_id = :paper_id AND r.task = ANY(:tasks)"
            " ORDER BY r.created_at, r.id"
        ),
        {"paper_id": paper_id, "tasks": [task.value for task in HEADER_TABLES]},
    ).mappings()
    return [
        AiHistoryRow(
            run_id=uuid.UUID(str(row["id"])),
            task=LlmTask(row["task"]),
            run_status=RunStatus(row["status"]),
            failure_reason=row["failure_reason"],
            header_status=(
                None if row["header_status"] is None else ResultStatus(row["header_status"])
            ),
            reject_reason=row["reject_reason"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            input_kind=InputKind(row["input_kind"]),
            abstract_source=(
                None if row["abstract_source"] is None else AbstractSource(row["abstract_source"])
            ),
            created_at=row["created_at"],
            is_eval=bool(row["is_eval"]),
        )
        for row in rows
    ]


def count_ai_results_by_abstract_source(
    viewer: SystemViewer, connection: Connection, source: AbstractSource
) -> SourceAiCounts:
    """送出時用了 `source` 的摘要（`derivation_run.abstract_source`）的表頭：各表各狀態幾列、底下
    的細項幾列；另外數看摘要但 run 沒記來源的表頭（來源不明，不會被刪）。`radar runs
    remove-by-source` 的預覽（Story 2.12）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    params = {"source": AbstractSource(source).value}
    rows = connection.execute(
        text(
            "SELECT 'zh_summary' AS task, h.status, count(*) AS n FROM zh_summary AS h"
            " JOIN derivation_run AS r ON r.id = h.run_id"
            f" WHERE {visible} AND r.abstract_source = :source GROUP BY h.status"
            " UNION ALL SELECT 'classify', h.status, count(*) FROM classification AS h"
            " JOIN derivation_run AS r ON r.id = h.run_id"
            f" WHERE {visible} AND r.abstract_source = :source GROUP BY h.status"
            " UNION ALL SELECT 'extract_model', h.status, count(*) FROM research_model AS h"
            " JOIN derivation_run AS r ON r.id = h.run_id"
            f" WHERE {visible} AND r.abstract_source = :source GROUP BY h.status"
        ),
        params,
    ).all()
    headers: dict[LlmTask, dict[ResultStatus, int]] = {
        task: dict.fromkeys(ResultStatus, 0) for task in HEADER_TABLES
    }
    for task, status, count in rows:
        headers[LlmTask(task)][ResultStatus(status)] = int(count)
    detail_row = connection.execute(
        text(
            "SELECT"
            " (SELECT count(*) FROM classification_label AS d"
            " JOIN classification AS h ON h.id = d.classification_id"
            " JOIN derivation_run AS r ON r.id = h.run_id"
            f" WHERE {visible} AND r.abstract_source = :source),"
            " (SELECT count(*) FROM construct_mention AS d"
            " JOIN research_model AS h ON h.id = d.research_model_id"
            " JOIN derivation_run AS r ON r.id = h.run_id"
            f" WHERE {visible} AND r.abstract_source = :source),"
            " (SELECT count(*) FROM hypothesis AS d"
            " JOIN research_model AS h ON h.id = d.research_model_id"
            " JOIN derivation_run AS r ON r.id = h.run_id"
            f" WHERE {visible} AND r.abstract_source = :source),"
            " (SELECT count(*) FROM ("
            " SELECT run_id, input_kind FROM zh_summary"
            " UNION ALL SELECT run_id, input_kind FROM classification"
            " UNION ALL SELECT run_id, input_kind FROM research_model"
            ") AS h JOIN derivation_run AS r ON r.id = h.run_id"
            f" WHERE {visible} AND h.input_kind = 'abstract' AND r.abstract_source IS NULL)"
        ),
        params,
    ).one()
    labels, mentions, hypotheses, unknown = (int(value) for value in detail_row)
    # 刪完這些表頭之後沒有其他人用的構面（「其他」＝這個來源以外的表頭的提及與假設）。
    unused = _construct_unused("m.research_model_id NOT IN (SELECT id FROM targets)")
    constructs = connection.execute(
        text(
            f"WITH {_SOURCE_RESEARCH_MODELS}, {_SOURCE_CONSTRUCTS}"
            " SELECT count(*) FROM construct AS c WHERE c.id IN (SELECT id FROM cand)"
            f" AND {unused}"
        ),
        params,
    ).scalar_one()
    return SourceAiCounts(
        headers=headers,
        details={
            "classification_label": labels,
            "construct_mention": mentions,
            "hypothesis": hypotheses,
        },
        unknown_source=unknown,
        constructs=int(constructs),
    )


def _construct_unused(other: str) -> str:
    """構面 `c` 沒有其他人用的條件（`other` 篩出「其他」的構面、假設：預覽時是這個來源以外的表頭，
    刪完細項後是全部）：沒有其他構面提及、假設，沒有別的構面以它為代表（`canonical_id`），也沒有
    人工的合併判斷（`judgment_event` 的 `construct_pair`）提到它。"""
    hypothesis_other = other.replace("m.", "h.")
    return (
        "NOT EXISTS (SELECT 1 FROM construct_mention AS m"
        f" WHERE m.construct_id = c.id AND {other})"
        " AND NOT EXISTS (SELECT 1 FROM hypothesis AS h"
        " WHERE (h.cause_construct_id = c.id OR h.effect_construct_id = c.id)"
        f" AND {hypothesis_other})"
        " AND NOT EXISTS (SELECT 1 FROM construct AS o"
        " WHERE o.canonical_id = c.id AND o.id <> c.id)"
        " AND NOT EXISTS (SELECT 1 FROM judgment_event AS j WHERE j.target_type = 'construct_pair'"
        " AND (j.target_id = c.id OR j.related_id = c.id))"
    )


# 這個來源衍生的研究模型表頭（`targets`）與它們的構面、假設用到的構面（`cand`）。
_SOURCE_RESEARCH_MODELS: Final = (
    "targets AS (SELECT h.id FROM research_model AS h JOIN derivation_run AS r ON r.id = h.run_id"
    " WHERE r.abstract_source = :source)"
)
_SOURCE_CONSTRUCTS: Final = (
    "cand AS (SELECT construct_id AS id FROM construct_mention"
    " WHERE research_model_id IN (SELECT id FROM targets)"
    " UNION SELECT cause_construct_id FROM hypothesis"
    " WHERE research_model_id IN (SELECT id FROM targets)"
    " UNION SELECT effect_construct_id FROM hypothesis"
    " WHERE research_model_id IN (SELECT id FROM targets))"
)


def find_active_config_versions(
    viewer: SystemViewer, connection: Connection, task: LlmTask, paper_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str | None]:
    """這幾篇論文 `task` 的 `active` 表頭是用哪一版設定檔跑的：`classify` 回分類表版本、
    `extract_model` 回情境值清單版本（run 沒記時是 None）；沒有 `active` 的論文不在結果裡。改版
    預估與 Batch 規劃的重跑上限用（Story 2.12）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: `task` 不是 `classify` 或 `extract_model`（其他工作的輸入不含設定檔版本）。
    """
    visible = visible_to_system_only(viewer)
    task = LlmTask(task)
    params = {"paper_ids": list(paper_ids)}
    match task:
        case LlmTask.CLASSIFY:
            sql = text(
                "SELECT h.paper_id, r.taxonomy_version AS version FROM classification AS h"
                " JOIN derivation_run AS r ON r.id = h.run_id"
                f" WHERE {visible} AND h.status = 'active' AND h.paper_id = ANY(:paper_ids)"
            )
        case LlmTask.EXTRACT_MODEL:
            sql = text(
                "SELECT h.paper_id, r.context_values_version AS version FROM research_model AS h"
                " JOIN derivation_run AS r ON r.id = h.run_id"
                f" WHERE {visible} AND h.status = 'active' AND h.paper_id = ANY(:paper_ids)"
            )
        case _:
            raise ValueError(f"{task.value} 的輸入不含設定檔版本")
    if not paper_ids:
        return {}
    return {
        uuid.UUID(str(row["paper_id"])): row["version"]
        for row in connection.execute(sql, params).mappings()
    }


# --- 寫入 ---------------------------------------------------------------------------------


def create_ai_result(connection: Connection, task: LlmTask, result: NewAiResult) -> uuid.UUID:
    """新增一列表頭（`zh_summary` 連同四欄內容；`classify` 連同計數，寫完表頭再寫標籤；
    `extract_model` 連同論文層級的欄位與計數，寫完表頭再經 `upsert_constructs` 確保構面存在、寫
    `construct_mention` 與 `hypothesis`），回它的 id。`active` 之前要先
    `update_ai_results_superseded`（部分唯一索引擋第二筆 `active`）。在呼叫端開好的交易裡做，
    不 commit。

    Raises:
        ValueError: 這個工作沒有表頭表，或帶了不屬於這個工作的內容（不是 `zh_summary` 卻帶中文
            重點、不是 `classify` 卻帶分類、不是 `extract_model` 卻帶研究模型）。
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
    research_model = result.research_model
    if task is LlmTask.EXTRACT_MODEL:
        params.update(_research_model_header_params(research_model))
    elif research_model is not None:
        raise ValueError(f"{task.value} 的表頭不收研究模型的內容")
    header_id = uuid.UUID(str(connection.execute(_INSERT[task], params).scalar_one()))
    if classification is not None:
        _create_classification_labels(connection, header_id, classification)
    if research_model is not None:
        _create_research_model_details(connection, header_id, research_model)
    return header_id


def _research_model_header_params(content: ResearchModelContent | None) -> dict[str, object]:
    """研究模型表頭的內容欄位（沒有內容時全是 NULL）。"""
    if content is None:
        return dict.fromkeys(_RESEARCH_MODEL_CONTENT_COLUMNS)
    return {column: getattr(content, column) for column in _RESEARCH_MODEL_CONTENT_COLUMNS}


def _create_research_model_details(
    connection: Connection, research_model_id: uuid.UUID, content: ResearchModelContent
) -> None:
    """確保構面存在（新構面的 canonical 指向自己），再寫這列表頭的構面與假設。"""
    construct_ids = upsert_constructs(connection, content.normalized_names())
    if content.constructs:
        connection.execute(
            _INSERT_CONSTRUCT_MENTION,
            [
                {
                    "research_model_id": research_model_id,
                    "construct_id": construct_ids[mention.normalized_name],
                    "name_en": mention.name_en,
                    "label_zh": mention.label_zh,
                    "definition": mention.definition,
                    "scale_source": mention.scale_source,
                    "item_location": mention.item_location,
                    "item_count": mention.item_count,
                    "cronbach_alpha": mention.cronbach_alpha,
                    "composite_reliability": mention.composite_reliability,
                    "ave": mention.ave,
                }
                for mention in content.constructs
            ],
        )
    if content.hypotheses:
        connection.execute(
            _INSERT_HYPOTHESIS,
            [
                {
                    "research_model_id": research_model_id,
                    "ordinal": hypothesis.ordinal,
                    "sentence": hypothesis.sentence,
                    "sentence_sha256": hypothesis.sentence_sha256,
                    "cause_construct_id": construct_ids[hypothesis.cause],
                    "effect_construct_id": construct_ids[hypothesis.effect],
                    "evidence_type": hypothesis.evidence_type.value,
                    "support": hypothesis.support.value,
                    "beta": hypothesis.beta,
                    "p_value": hypothesis.p_value,
                    "sample_size": hypothesis.sample_size,
                    "effect_type": hypothesis.effect_type,
                    "result_quote": hypothesis.result_quote,
                }
                for hypothesis in content.hypotheses
            ],
        )


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


# 依來源移除鎖構面候選時最多等多久（毫秒）：比連線的 statement_timeout（`connect_timeout` 秒，至少
# 3 秒）短，等不到就整次移除退回、請管理者重跑（Story 2.12 審查後第二輪 R3）。
CONSTRUCT_LOCK_TIMEOUT_MS: Final = 2000


def delete_ai_results_by_abstract_source(
    connection: Connection,
    source: AbstractSource,
    *,
    construct_lock_timeout_ms: int = CONSTRUCT_LOCK_TIMEOUT_MS,
) -> dict[str, int]:
    """刪掉送出時用了 `source` 的摘要（`derivation_run.abstract_source`）的表頭（四種狀態）與底下的
    細項、只被它們用到的構面，回各表刪了幾列（`REMOVAL_TABLES` 的順序，不含 `embedding`：向量由
    `repo/embeddings.delete_embeddings_by_abstract_source` 刪）。先刪細項再刪表頭（外鍵沒有
    ON DELETE）。

    構面（審查後第 2 點）：只刪這些表頭的構面提及、假設用到，而且刪完之後沒有其他構面提及、假設、
    代表（`canonical_id`）或人工合併判斷用到的（交給 `repo/constructs.delete_constructs`）。候選在
    刪任何提及、假設之前先全部 `FOR UPDATE` 鎖住（不跳過、最多等 `construct_lock_timeout_ms`；審查後
    第二輪 R3）：別的交易正在新增引用它的列（外鍵檢查會拿 `FOR KEY SHARE`）時等它結束，拿到鎖之後
    再判斷有沒有人用，不會因為跳過而永遠失去清理線索。等不到就丟 `LockWaitTimeoutError`，呼叫端
    讓整個交易退回（一列都沒少），請管理者重跑。

    `derivation_run`、`llm_call`、人工判斷事件都不動；不複製被刪的內容。`radar runs
    remove-by-source --execute` 用（Story 2.12、FR-11、AD-26）。在呼叫端開好的交易裡做，不 commit。

    Raises:
        LockWaitTimeoutError: 構面候選等不到鎖（交易已經不能用，要整個退回）。
    """
    params = {"source": AbstractSource(source).value}
    deleted: dict[str, int] = {}
    candidates = sorted(
        {
            str(row[0])
            for row in connection.execute(
                text(f"WITH {_SOURCE_RESEARCH_MODELS}, {_SOURCE_CONSTRUCTS} SELECT id FROM cand"),
                params,
            ).all()
        }
    )
    if candidates:
        # 先鎖住全部候選（照 id 排序），才刪提及與假設：等不到就整次退回，候選線索不會遺失。
        with Database.local_lock_timeout(connection, construct_lock_timeout_ms):
            connection.execute(
                text(
                    "SELECT id FROM construct WHERE id = ANY(CAST(:ids AS uuid[]))"
                    " ORDER BY id FOR UPDATE"
                ),
                {"ids": candidates},
            ).all()
    deleted["classification_label"] = connection.execute(
        text(
            "DELETE FROM classification_label WHERE classification_id IN ("
            " SELECT h.id FROM classification AS h JOIN derivation_run AS r ON r.id = h.run_id"
            " WHERE r.abstract_source = :source)"
        ),
        params,
    ).rowcount
    deleted["construct_mention"] = connection.execute(
        text(
            "DELETE FROM construct_mention WHERE research_model_id IN ("
            " SELECT h.id FROM research_model AS h JOIN derivation_run AS r ON r.id = h.run_id"
            " WHERE r.abstract_source = :source)"
        ),
        params,
    ).rowcount
    deleted["hypothesis"] = connection.execute(
        text(
            "DELETE FROM hypothesis WHERE research_model_id IN ("
            " SELECT h.id FROM research_model AS h JOIN derivation_run AS r ON r.id = h.run_id"
            " WHERE r.abstract_source = :source)"
        ),
        params,
    ).rowcount
    deleted["zh_summary"] = connection.execute(
        text(
            "DELETE FROM zh_summary AS h USING derivation_run AS r"
            " WHERE r.id = h.run_id AND r.abstract_source = :source"
        ),
        params,
    ).rowcount
    deleted["classification"] = connection.execute(
        text(
            "DELETE FROM classification AS h USING derivation_run AS r"
            " WHERE r.id = h.run_id AND r.abstract_source = :source"
        ),
        params,
    ).rowcount
    deleted["research_model"] = connection.execute(
        text(
            "DELETE FROM research_model AS h USING derivation_run AS r"
            " WHERE r.id = h.run_id AND r.abstract_source = :source"
        ),
        params,
    ).rowcount
    # 刪完提及與假設之後沒有任何人用的構面（候選已經鎖住，別的交易不能再新增引用它們的列），
    # 交給構面表的寫入者刪。
    unused: list[uuid.UUID] = []
    if candidates:
        unused = [
            uuid.UUID(str(row[0]))
            for row in connection.execute(
                text(
                    "SELECT c.id FROM construct AS c WHERE c.id = ANY(CAST(:ids AS uuid[]))"
                    f" AND {_construct_unused('TRUE')} ORDER BY c.id"
                ),
                {"ids": candidates},
            ).all()
        ]
    deleted["construct"] = delete_constructs(connection, unused)
    return {table: int(deleted[table]) for table in REMOVAL_TABLES if table in deleted}
