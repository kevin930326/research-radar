"""人工判斷事件、稽核事件與三個有效值 view（Story 2.7、AD-10、ADR-0015、ADR-0035）。

- 新增 `judgment_event`：人工判斷只新增，`kind` 固定四個值；各 `kind` 的形狀由 CHECK 擋
  （和 `radar/domain/judgments.py` 同一份規則；key 排序、去重 CHECK 不擋，domain 擋；直接寫 SQL
  存進沒排序、有重複的值時，`effective_classification` 讀人工值也會去重、用 "C" 定序排序）。
  對象不設外鍵（論文刪除、AI 產出依摘要來源移除時，應用程式角色刪不了事件，外鍵會卡住刪除）；
  `member_id` 暫不設外鍵（`member` 在 3.3）。`report_event_id` 指回同一張表，UNIQUE（一筆回報
  最多一筆處理結果，也當外鍵的索引）。
- 處理結果寫入前的觸發器 `tg_judgment_event_resolution_matches_report`：`report_event_id` 指到的
  事件要是 `summary_report`，而且對象（`target_type`、`target_id`）與 `field` 相同，不是就拒絕
  （`check_violation`）。外鍵與 CHECK 擋不了跨列的比對；錯的處理結果寫進去會占住 UNIQUE 又刪不掉，
  所以資料庫也擋一道（`repo/judgments.create_judgment_event` 的 `INSERT … SELECT` 是第一道）。
  觸發器函式不用 SECURITY DEFINER（以寫入者的權限讀這張表），`search_path` 固定、表名寫全。
- 新增 `audit_event`：稽核事件只新增（寫入者留給做寫入的 Story）。
- 應用程式角色對兩張事件表收回 UPDATE、DELETE（0001 的預設權限給了四種）。
- 新增 view `effective_zh_summary`、`effective_classification`、`effective_research_model`：有效值
  只由它們決定（AD-10）。view 收回 INSERT、UPDATE、DELETE（照 0008）。view 不在 `Base.metadata`
  裡，`alembic check` 不比對；之後改到 view 用到的欄位時，要在同一個遷移裡先 `DROP VIEW`、改完重建，
  重建後再收一次寫入權限。

和 `radar/repo/judgments.py`、`radar/repo/audit.py` 的表定義一致（`alembic check` 比對）；CHECK 約束
autogenerate 不比，這裡照抄同一份條件。可降版（降版會丟掉兩張事件表、處理結果的觸發器函式
與三個 view）。

版本：0014
上一版：0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from radar.domain.database_requirements import APP_ROLE

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEYS_OK = (
    "NOT jsonb_path_exists({column},"
    ' \'$[*] ? (@.type() != "string" || !(@ like_regex "^[a-z][a-z0-9_]*$"))\')'
)
_SUMMARY_FIELDS = "('research_question', 'method', 'theory', 'findings')"

_JUDGMENT_CHECKS = (
    (
        "ck_judgment_event_kind",
        "kind IN ('classification_fix', 'merge_decision', 'summary_report',"
        " 'summary_report_resolution')",
    ),
    ("ck_judgment_event_actor_role", "actor_role IN ('member', 'admin')"),
    ("ck_judgment_event_target_type", "target_type IN ('paper', 'construct_pair', 'zh_summary')"),
    ("ck_judgment_event_reason_not_blank", "reason IS NULL OR reason ~ '[^[:space:]]'"),
    (
        "ck_judgment_event_kind_target",
        "(kind = 'classification_fix' AND target_type = 'paper')"
        " OR (kind = 'merge_decision' AND target_type = 'construct_pair')"
        " OR (kind IN ('summary_report', 'summary_report_resolution')"
        " AND target_type = 'zh_summary')",
    ),
    (
        "ck_judgment_event_related_id_iff_merge",
        "(kind = 'merge_decision') = (related_id IS NOT NULL)",
    ),
    (
        "ck_judgment_event_ai_result_id_only_fix",
        "ai_result_id IS NULL OR kind = 'classification_fix'",
    ),
    (
        "ck_judgment_event_report_event_id_iff_resolution",
        "(kind = 'summary_report_resolution') = (report_event_id IS NOT NULL)",
    ),
    ("ck_judgment_event_old_value_only_fix", "old_value IS NULL OR kind = 'classification_fix'"),
    (
        "ck_judgment_event_classification_fix_shape",
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
    ),
    (
        "ck_judgment_event_merge_decision_shape",
        "kind <> 'merge_decision' OR COALESCE("
        "field IS NULL AND target_id < related_id"
        " AND jsonb_typeof(new_value) = 'object'"
        " AND new_value - 'decision' - 'evidence_level' = '{}'::jsonb"
        " AND new_value ->> 'decision' IN ('accept', 'reject')"
        " AND new_value ->> 'evidence_level' IN ('name', 'definition', 'items')"
        ", FALSE)",
    ),
    (
        "ck_judgment_event_summary_report_shape",
        "kind <> 'summary_report' OR COALESCE("
        f"field IN {_SUMMARY_FIELDS} AND new_value IS NULL AND reason IS NOT NULL"
        ", FALSE)",
    ),
    (
        "ck_judgment_event_summary_report_resolution_shape",
        "kind <> 'summary_report_resolution' OR COALESCE("
        f"field IN {_SUMMARY_FIELDS} AND actor_role = 'admin'"
        " AND new_value IN ('\"confirmed_ok\"'::jsonb, '\"regenerated\"'::jsonb)"
        ", FALSE)",
    ),
)

_AUDIT_CHECKS = (
    ("ck_audit_event_actor_role", "actor_role IN ('member', 'admin', 'system')"),
    (
        "ck_audit_event_system_iff_no_member",
        "(actor_role = 'system') = (actor_member_id IS NULL)",
    ),
    ("ck_audit_event_action_format", "action ~ '^[a-z][a-z0-9_]*$'"),
    ("ck_audit_event_target_type_format", "target_type ~ '^[a-z][a-z0-9_]*$'"),
    ("ck_audit_event_detail_object", "jsonb_typeof(detail) = 'object'"),
)

# 有效中文重點：`active`＋`zh-Hant`；`reported_fields`＝這一筆有回報、還沒有處理結果的欄位（照四欄
# 的固定順序）。回報只對它指到的那一筆有效：重新產生的新一筆沒有回報。
_EFFECTIVE_ZH_SUMMARY = """
CREATE VIEW effective_zh_summary AS
SELECT h.id AS zh_summary_id, h.paper_id, h.input_kind, h.created_at AS generated_at, r.model,
  h.research_question, h.method, h.theory, h.findings,
  ARRAY(
    SELECT f.field
    FROM (VALUES (1, 'research_question'), (2, 'method'), (3, 'theory'), (4, 'findings'))
      AS f(ordinal, field)
    WHERE EXISTS (
      SELECT 1 FROM judgment_event AS report
      WHERE report.kind = 'summary_report' AND report.target_type = 'zh_summary'
        AND report.target_id = h.id AND report.field = f.field
        AND NOT EXISTS (
          SELECT 1 FROM judgment_event AS resolution
          WHERE resolution.kind = 'summary_report_resolution'
            AND resolution.report_event_id = report.id))
    ORDER BY f.ordinal
  ) AS reported_fields
FROM zh_summary AS h
JOIN derivation_run AS r ON r.id = h.run_id
WHERE h.status = 'active' AND h.lang = 'zh-Hant'
"""

# 有效分類，單位是（論文, `field`）：最新一筆人工修正（`created_at`、`id` 由新到舊）勝過 `active`
# 的 AI 值。`human_verified`＝這一欄是人工值；`fix_member_count`＝修正過這一欄的成員數；
# `members_disagree`＝每位成員各取最新一筆、兩人以上而且整組 key 不同。AI 的標籤與人工值都用 "C"
# 定序排序（和 Python 的 `sorted` 一樣）；人工值另外去重（防直接寫 SQL 存進沒排序、有重複的值），
# 比較成員意見時也用去重、排序後的整組 key（同一組不同順序不算意見不同）。
_EFFECTIVE_CLASSIFICATION = """
CREATE VIEW effective_classification AS
SELECT t.paper_id, t.field,
  COALESCE(latest.keys, ai.keys) AS keys,
  latest.keys IS NOT NULL AS human_verified,
  members.fix_member_count,
  members.members_disagree,
  ai.classification_id, ai.model, ai.input_kind, ai.generated_at,
  latest.created_at AS fixed_at
FROM (
  SELECT h.paper_id, f.field
  FROM classification AS h CROSS JOIN (VALUES ('topic'), ('method')) AS f(field)
  WHERE h.status = 'active' AND h.lang = 'zxx'
  UNION
  SELECT e.target_id, e.field
  FROM judgment_event AS e
  WHERE e.kind = 'classification_fix' AND e.target_type = 'paper'
) AS t
LEFT JOIN LATERAL (
  SELECT h.id AS classification_id, h.input_kind, h.created_at AS generated_at, r.model,
    ARRAY(
      SELECT l.key FROM classification_label AS l
      WHERE l.classification_id = h.id AND l.field = t.field
      ORDER BY l.key COLLATE "C"
    ) AS keys
  FROM classification AS h
  JOIN derivation_run AS r ON r.id = h.run_id
  WHERE h.paper_id = t.paper_id AND h.status = 'active' AND h.lang = 'zxx'
) AS ai ON TRUE
LEFT JOIN LATERAL (
  SELECT e.created_at,
    ARRAY(
      SELECT k.key FROM jsonb_array_elements_text(e.new_value) AS k(key)
      GROUP BY k.key ORDER BY k.key COLLATE "C"
    ) AS keys
  FROM judgment_event AS e
  WHERE e.kind = 'classification_fix' AND e.target_type = 'paper'
    AND e.target_id = t.paper_id AND e.field = t.field
  ORDER BY e.created_at DESC, e.id DESC
  LIMIT 1
) AS latest ON TRUE
LEFT JOIN LATERAL (
  SELECT count(*)::integer AS fix_member_count,
    count(DISTINCT newest.keys) > 1 AS members_disagree
  FROM (
    SELECT DISTINCT ON (e.member_id)
      ARRAY(
        SELECT k.key FROM jsonb_array_elements_text(e.new_value) AS k(key)
        GROUP BY k.key ORDER BY k.key COLLATE "C"
      ) AS keys
    FROM judgment_event AS e
    WHERE e.kind = 'classification_fix' AND e.target_type = 'paper'
      AND e.target_id = t.paper_id AND e.field = t.field
    ORDER BY e.member_id, e.created_at DESC, e.id DESC
  ) AS newest
) AS members ON TRUE
"""

# 有效研究模型：`active`＋`en`。構面（照寫入順序）與假設（照 `ordinal`）以 jsonb 陣列帶出；
# numeric 先轉成文字（讀的人轉成 Decimal，不經浮點）。
_EFFECTIVE_RESEARCH_MODEL = """
CREATE VIEW effective_research_model AS
SELECT h.id AS research_model_id, h.paper_id, h.input_kind, h.created_at AS generated_at, r.model,
  h.is_review_or_meta_analysis, h.country, h.industry, h.research_design, h.unit_of_analysis,
  h.sample_size, h.limitations, h.future_research,
  COALESCE((
    SELECT jsonb_agg(jsonb_build_object(
      'construct_id', m.construct_id, 'name_en', m.name_en, 'label_zh', m.label_zh,
      'definition', m.definition, 'scale_source', m.scale_source,
      'item_location', m.item_location, 'item_count', m.item_count,
      'cronbach_alpha', m.cronbach_alpha::text,
      'composite_reliability', m.composite_reliability::text,
      'ave', m.ave::text) ORDER BY m.id)
    FROM construct_mention AS m WHERE m.research_model_id = h.id
  ), '[]'::jsonb) AS constructs,
  COALESCE((
    SELECT jsonb_agg(jsonb_build_object(
      'ordinal', y.ordinal, 'sentence', y.sentence,
      'cause_construct_id', y.cause_construct_id, 'effect_construct_id', y.effect_construct_id,
      'evidence_type', y.evidence_type, 'support', y.support,
      'beta', y.beta::text, 'p_value', y.p_value::text,
      'sample_size', y.sample_size, 'effect_type', y.effect_type,
      'result_quote', y.result_quote) ORDER BY y.ordinal)
    FROM hypothesis AS y WHERE y.research_model_id = h.id
  ), '[]'::jsonb) AS hypotheses
FROM research_model AS h
JOIN derivation_run AS r ON r.id = h.run_id
WHERE h.status = 'active' AND h.lang = 'en'
"""

_VIEWS = (
    ("effective_zh_summary", _EFFECTIVE_ZH_SUMMARY),
    ("effective_classification", _EFFECTIVE_CLASSIFICATION),
    ("effective_research_model", _EFFECTIVE_RESEARCH_MODEL),
)


# 處理結果要對得上它指到的回報。指到不存在的事件時不在這裡擋，留給外鍵回 foreign_key_violation；
# `field` 是 NULL 時也不在這裡擋，留給 CHECK（處理結果一定要有欄位）。
_RESOLUTION_TRIGGER = "tg_judgment_event_resolution_matches_report"
_RESOLUTION_FUNCTION = "judgment_event_resolution_matches_report"
_CREATE_RESOLUTION_FUNCTION = f"""
CREATE FUNCTION public.{_RESOLUTION_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
  report record;
BEGIN
  IF NEW.kind <> 'summary_report_resolution' OR NEW.report_event_id IS NULL
      OR NEW.field IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT kind, target_type, target_id, field INTO report
  FROM public.judgment_event WHERE id = NEW.report_event_id;
  IF NOT FOUND THEN
    RETURN NEW;
  END IF;
  IF report.kind <> 'summary_report' OR report.target_type <> NEW.target_type
      OR report.target_id <> NEW.target_id OR report.field IS DISTINCT FROM NEW.field THEN
    RAISE EXCEPTION '處理結果要指到同一個對象、同一欄的 summary_report'
      USING ERRCODE = 'check_violation', CONSTRAINT = '{_RESOLUTION_TRIGGER}',
        TABLE = 'judgment_event';
  END IF;
  RETURN NEW;
END;
$$
"""
_CREATE_RESOLUTION_TRIGGER = f"""
CREATE TRIGGER {_RESOLUTION_TRIGGER}
BEFORE INSERT ON public.judgment_event
FOR EACH ROW EXECUTE FUNCTION public.{_RESOLUTION_FUNCTION}()
"""


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "judgment_event",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("actor_role", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("related_id", sa.Uuid(), nullable=True),
        sa.Column("field", sa.Text(), nullable=True),
        sa.Column("old_value", JSONB(), nullable=True),
        sa.Column("new_value", JSONB(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("ai_result_id", sa.Uuid(), nullable=True),
        sa.Column("report_event_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_judgment_event"),
        sa.ForeignKeyConstraint(
            ["report_event_id"],
            ["judgment_event.id"],
            name="fk_judgment_event_report_event_id_judgment_event",
        ),
        sa.UniqueConstraint("report_event_id", name="uq_judgment_event_report_event_id"),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _JUDGMENT_CHECKS),
    )
    op.create_index(
        "ix_judgment_event_target", "judgment_event", ["target_type", "target_id", "field"]
    )
    op.execute(_CREATE_RESOLUTION_FUNCTION)
    op.execute(_CREATE_RESOLUTION_TRIGGER)

    op.create_table(
        "audit_event",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("actor_member_id", sa.Uuid(), nullable=True),
        sa.Column("actor_role", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("detail", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_event"),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _AUDIT_CHECKS),
    )
    op.create_index("ix_audit_event_created_at", "audit_event", ["created_at"])
    op.create_index("ix_audit_event_target", "audit_event", ["target_type", "target_id"])

    # 事件只能新增（AD-10）。
    op.execute(f"REVOKE UPDATE, DELETE ON judgment_event, audit_event FROM {APP_ROLE}")

    for name, sql in _VIEWS:
        op.execute(sql)
        op.execute(f"REVOKE INSERT, UPDATE, DELETE ON {name} FROM {APP_ROLE}")


def downgrade() -> None:
    for name, _ in reversed(_VIEWS):
        op.execute(f"DROP VIEW {name}")
    op.drop_table("audit_event")
    op.drop_table("judgment_event")
    op.execute(f"DROP FUNCTION public.{_RESOLUTION_FUNCTION}()")
