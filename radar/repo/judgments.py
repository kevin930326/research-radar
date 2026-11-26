"""`judgment_event`（人工判斷事件）：表的定義與唯一的寫入者（AD-10、ADR-0012、ADR-0015、
ADR-0035、Story 2.7）。

- 只新增：應用程式角色沒有 UPDATE、DELETE 權限（遷移 0014 收回），這裡也只有新增。
- 形狀由 `domain/judgments.NewJudgmentEvent` 在建構時擋，CHECK 再擋一次（和遷移 0014 同一份條件；
  key 排序、去重 CHECK 不擋、domain 擋；`effective_classification` 讀人工值時也會去重、排序）。
- 處理結果（`summary_report_resolution`）用 `INSERT … SELECT`：只有指到的那一筆是同一筆
  `zh_summary`、同一欄的回報時才寫；一筆回報最多一筆處理結果（UNIQUE，重複時不寫）。資料庫的
  寫入前觸發器（遷移 0014）再擋一次，防直接寫 SQL。
- 有效值不在這裡讀：讀 `effective_*` view（`repo/ai_blocks.py`）。權限檢查與同一個交易寫稽核在
  `services/judgments.py`（3.11、3.12、Epic 10）。
- 讀取：分類修正的歷史（`list_classification_fix_events`，Story 3.11）接 `visible_papers`，看不到的
  論文一筆都不回；成員只讀顯示名稱（`member.display_name`），不讀 email。只給成員與維運身分：公開
  身分（示範站產生器）在執行時就被擋（示範站不帶修正歷史，ADR-0063）。
- `created_at` 可以由呼叫端給（修正分類在拿到鎖之後取一次 `clock_timestamp()`，兩欄與稽核共用，
  歷史與有效值的新舊才會和實際寫入的順序一樣）；沒給是資料庫的 `now()`。
- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。
"""

import json
import uuid
from collections.abc import Iterable
from datetime import datetime
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
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from radar.domain.classification_fixes import ClassificationFixEvent
from radar.domain.judgments import ActorRole, JudgmentKind, JudgmentTarget, NewJudgmentEvent
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.models import Base
from radar.repo.visibility import visible_member_judgments, visible_papers

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0014 建表） -----------------------------

_KEYS_OK = (
    "NOT jsonb_path_exists({column},"
    ' \'$[*] ? (@.type() != "string" || !(@ like_regex "^[a-z][a-z0-9_]*$"))\')'
)
_SUMMARY_FIELDS = "('research_question', 'method', 'theory', 'findings')"

judgment_event_table = Table(
    "judgment_event",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("kind", Text, nullable=False),
    # `member` 在 3.3，暫不設外鍵。
    Column("member_id", Uuid, nullable=False),
    Column("actor_role", Text, nullable=False),
    # 對象不設外鍵：論文刪除、AI 產出依摘要來源移除時，事件刪不掉會卡住刪除（ADR-0035）。
    Column("target_type", Text, nullable=False),
    Column("target_id", Uuid, nullable=False),
    Column("related_id", Uuid),
    Column("field", Text),
    Column("old_value", JSONB),
    Column("new_value", JSONB),
    Column("reason", Text),
    Column("ai_result_id", Uuid),
    Column("report_event_id", Uuid, ForeignKey("judgment_event.id")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # 一筆回報最多一筆處理結果；也當 report_event_id 外鍵的索引。
    UniqueConstraint("report_event_id"),
    # 有效值的 view 依對象與欄位找事件。
    Index("ix_judgment_event_target", "target_type", "target_id", "field"),
    CheckConstraint(
        "kind IN ('classification_fix', 'merge_decision', 'summary_report',"
        " 'summary_report_resolution')",
        name="kind",
    ),
    CheckConstraint("actor_role IN ('member', 'admin')", name="actor_role"),
    CheckConstraint("target_type IN ('paper', 'construct_pair', 'zh_summary')", name="target_type"),
    CheckConstraint("reason IS NULL OR reason ~ '[^[:space:]]'", name="reason_not_blank"),
    CheckConstraint(
        "(kind = 'classification_fix' AND target_type = 'paper')"
        " OR (kind = 'merge_decision' AND target_type = 'construct_pair')"
        " OR (kind IN ('summary_report', 'summary_report_resolution')"
        " AND target_type = 'zh_summary')",
        name="kind_target",
    ),
    CheckConstraint(
        "(kind = 'merge_decision') = (related_id IS NOT NULL)", name="related_id_iff_merge"
    ),
    CheckConstraint(
        "ai_result_id IS NULL OR kind = 'classification_fix'", name="ai_result_id_only_fix"
    ),
    CheckConstraint(
        "(kind = 'summary_report_resolution') = (report_event_id IS NOT NULL)",
        name="report_event_id_iff_resolution",
    ),
    CheckConstraint("old_value IS NULL OR kind = 'classification_fix'", name="old_value_only_fix"),
    CheckConstraint(
        "kind <> 'classification_fix' OR COALESCE("
        "field IN ('topic', 'method')"
        " AND jsonb_typeof(new_value) = 'array'"
        f" AND {_KEYS_OK.format(column='new_value')}"
        " AND (field <> 'topic' OR jsonb_array_length(new_value) >= 1)"
        " AND (NOT jsonb_path_exists(new_value, '$[*] ? (@ == \"unclassified\")')"
        " OR (field = 'topic' AND jsonb_array_length(new_value) = 1))"
        " AND (old_value IS NULL OR (jsonb_typeof(old_value) = 'array'"
        f" AND {_KEYS_OK.format(column='old_value')}"
        " AND (field <> 'topic' OR jsonb_array_length(old_value) >= 1)"
        " AND (NOT jsonb_path_exists(old_value, '$[*] ? (@ == \"unclassified\")')"
        " OR (field = 'topic' AND jsonb_array_length(old_value) = 1))))"
        ", FALSE)",
        name="classification_fix_shape",
    ),
    CheckConstraint(
        "kind <> 'merge_decision' OR COALESCE("
        "field IS NULL AND target_id < related_id"
        " AND jsonb_typeof(new_value) = 'object'"
        " AND new_value - 'decision' - 'evidence_level' = '{}'::jsonb"
        " AND new_value ->> 'decision' IN ('accept', 'reject')"
        " AND new_value ->> 'evidence_level' IN ('name', 'definition', 'items')"
        ", FALSE)",
        name="merge_decision_shape",
    ),
    CheckConstraint(
        "kind <> 'summary_report' OR COALESCE("
        f"field IN {_SUMMARY_FIELDS} AND new_value IS NULL AND reason IS NOT NULL"
        ", FALSE)",
        name="summary_report_shape",
    ),
    CheckConstraint(
        "kind <> 'summary_report_resolution' OR COALESCE("
        f"field IN {_SUMMARY_FIELDS} AND actor_role = 'admin'"
        " AND new_value IN ('\"confirmed_ok\"'::jsonb, '\"regenerated\"'::jsonb)"
        ", FALSE)",
        name="summary_report_resolution_shape",
    ),
)

# --- SQL ----------------------------------------------------------------------------------

_INSERT: Final = text(
    "INSERT INTO judgment_event (kind, member_id, actor_role, target_type, target_id, related_id,"
    " field, old_value, new_value, reason, ai_result_id, report_event_id, created_at)"
    " VALUES (:kind, :member_id, :actor_role, :target_type, :target_id, :related_id, :field,"
    " CAST(:old_value AS jsonb), CAST(:new_value AS jsonb), :reason, :ai_result_id,"
    " :report_event_id, COALESCE(CAST(:created_at AS timestamptz), now()))"
    " RETURNING id"
)

# 處理結果：原本那筆要是同一筆 zh_summary、同一欄的回報；已經有處理結果就不寫（UNIQUE）。
_INSERT_RESOLUTION: Final = text(
    "INSERT INTO judgment_event (kind, member_id, actor_role, target_type, target_id, related_id,"
    " field, old_value, new_value, reason, ai_result_id, report_event_id, created_at)"
    " SELECT CAST(:kind AS text), CAST(:member_id AS uuid), CAST(:actor_role AS text),"
    " CAST(:target_type AS text), CAST(:target_id AS uuid), CAST(:related_id AS uuid),"
    " CAST(:field AS text), CAST(:old_value AS jsonb), CAST(:new_value AS jsonb),"
    " CAST(:reason AS text), CAST(:ai_result_id AS uuid), report.id,"
    " COALESCE(CAST(:created_at AS timestamptz), now())"
    " FROM judgment_event AS report"
    " WHERE report.id = :report_event_id AND report.kind = 'summary_report'"
    " AND report.target_type = :target_type AND report.target_id = :target_id"
    " AND report.field = :field"
    " ON CONFLICT (report_event_id) DO NOTHING"
    " RETURNING id"
)


def _json(value: object) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, allow_nan=False)


# --- 寫入 ---------------------------------------------------------------------------------


def create_judgment_event(
    connection: Connection, event: NewJudgmentEvent, *, created_at: datetime | None = None
) -> uuid.UUID | None:
    """新增一筆人工判斷事件，回它的 id。在呼叫端開好的交易裡做，不 commit。`created_at` 沒給是
    資料庫的 `now()`（交易開始的時間）。

    處理結果（`summary_report_resolution`）指到的不是同一筆 `zh_summary`、同一欄的回報（不存在、
    是別種事件、對象或欄位不同），或那筆回報已經有處理結果時，什麼都不寫、回 None；其他種類
    一定寫入。

    Raises:
        TypeError: `event` 不是 `NewJudgmentEvent`（形狀在它建構時已經驗過）。
        ValueError: `created_at` 沒有時區。
    """
    if not isinstance(event, NewJudgmentEvent):
        raise TypeError("event 要是 NewJudgmentEvent")
    if created_at is not None and (created_at.tzinfo is None or created_at.utcoffset() is None):
        raise ValueError("created_at 要是帶時區的時間")
    kind = JudgmentKind(event.kind)
    old_value, new_value = event.json_values()
    params: dict[str, object] = {
        "kind": kind.value,
        "member_id": event.member_id,
        "actor_role": ActorRole(event.actor_role).value,
        "target_type": JudgmentTarget(event.target_type).value,
        "target_id": event.target_id,
        "related_id": event.related_id,
        "field": event.field,
        "old_value": _json(old_value),
        "new_value": _json(new_value),
        "reason": event.reason,
        "ai_result_id": event.ai_result_id,
        "report_event_id": event.report_event_id,
        "created_at": created_at,
    }
    sql = _INSERT_RESOLUTION if kind is JudgmentKind.SUMMARY_REPORT_RESOLUTION else _INSERT
    value = connection.execute(sql, params).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


# --- 讀取 ---------------------------------------------------------------------------------


def list_classification_fix_events(
    viewer: MemberViewer | SystemViewer, connection: Connection, paper_ids: Iterable[uuid.UUID]
) -> list[ClassificationFixEvent]:
    """這些論文（`viewer` 看得到的）的分類修正事件，新的在前（`created_at DESC, id DESC`）。看不到、
    不存在的論文沒有任何一筆。每筆帶修正的成員的顯示名稱（不帶 email）；`new_value` 照 view 的做法
    去重、排序（"C" 定序）。

    Raises:
        TypeError: `viewer` 不是成員或維運身分（公開身分讀不到修正歷史；
            `visible_member_judgments`）。
    """
    # 先判身分（paper_ids 是空的也一樣擋公開身分）。
    judgments_visible = visible_member_judgments(viewer)
    wanted = sorted(set(paper_ids))
    if not wanted:
        return []
    visible = visible_papers(viewer, alias="paper")
    rows = connection.execute(
        text(
            "SELECT j.id, j.target_id, j.member_id, m.display_name, j.created_at, j.field,"
            " j.reason,"
            " ARRAY(SELECT k.key FROM jsonb_array_elements_text(j.new_value) AS k(key)"
            ' GROUP BY k.key ORDER BY k.key COLLATE "C") AS keys'
            " FROM judgment_event AS j"
            " JOIN paper ON paper.id = j.target_id"
            " JOIN member AS m ON m.id = j.member_id"
            f" WHERE {visible} AND {judgments_visible} AND j.kind = :kind"
            " AND j.target_type = :target"
            " AND j.target_id = ANY(CAST(:ids AS uuid[]))"
            " ORDER BY j.created_at DESC, j.id DESC"
        ),
        {
            "kind": JudgmentKind.CLASSIFICATION_FIX.value,
            "target": JudgmentTarget.PAPER.value,
            "ids": wanted,
        },
    )
    events: list[ClassificationFixEvent] = []
    for event_id, paper_id, member_id, name, created_at, field, reason, keys in rows:
        fixed_at: datetime = created_at
        events.append(
            ClassificationFixEvent(
                event_id=uuid.UUID(str(event_id)),
                paper_id=uuid.UUID(str(paper_id)),
                member_id=uuid.UUID(str(member_id)),
                member_name=str(name),
                fixed_at=fixed_at,
                field=TaxonomyField(field),
                keys=tuple(keys),
                reason=reason,
            )
        )
    return events
