"""三個有效值 view 對真的 PostgreSQL（Story 2.7、AD-10、ADR-0035）：spec 2.7 I/O 表前六列，加上
研究模型的 jsonb 形狀、不在 view 裡的狀態，以及 `create_judgment_event` 寫處理結果的條件。

AI 產出直接用 SQL 造（`derivation_run`＋表頭＋細項）；人工判斷一律經 `repo/judgments.py` 的
`create_judgment_event` 以應用程式角色寫入。資料全是測試自己編的。
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from radar.domain.judgments import (
    ActorRole,
    NewJudgmentEvent,
    SummaryField,
    SummaryResolution,
    classification_fix,
    summary_report,
    summary_report_resolution,
)
from radar.domain.taxonomy import TaxonomyField
from radar.repo.db import Database
from radar.repo.judgments import create_judgment_event
from tests.database import TestDatabase, migrate
from tests.llm_support import create_paper, execute

MODEL = "claude-haiku-5-5"
MEMBER_A = uuid.UUID("00000000-0000-7000-8000-00000000000a")
MEMBER_B = uuid.UUID("00000000-0000-7000-8000-00000000000b")
ADMIN = uuid.UUID("00000000-0000-7000-8000-0000000000ad")
TASK_LANG = {"zh_summary": "zh-Hant", "classify": "zxx", "extract_model": "en"}
TASK_TABLE = {
    "zh_summary": "zh_summary",
    "classify": "classification",
    "extract_model": "research_model",
}


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def paper_id(database: Database) -> uuid.UUID:
    return create_paper(database)


# --- 造資料（test_ai_blocks.py 也用） ---------------------------------------------------------


def insert_run(
    db: TestDatabase,
    paper_id: uuid.UUID,
    task: str,
    *,
    input_sha256: str | None = None,
    prompt_version: int = 1,
    model: str = MODEL,
    status: str = "succeeded",
    call_mode: str = "interactive",
    eval_run_id: uuid.UUID | None = None,
    input_kind: str = "abstract",
) -> uuid.UUID:
    """一筆 `derivation_run`，回它的 id。沒給 `input_sha256` 就用一個每次都不同的值；`failed` 的原因
    固定是 `llm_run_expired`。"""
    (row,) = execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
        " input_sha256, input_kind, params, code_version, call_mode, eval_run_id, status,"
        " failure_reason, finished_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, 'test', %s, %s, %s,"
        " CASE WHEN %s = 'failed' THEN 'llm_run_expired' END,"
        " CASE WHEN %s = 'running' THEN NULL ELSE now() END) RETURNING id",
        paper_id,
        task,
        prompt_version,
        "a" * 64,
        model,
        input_sha256 or uuid.uuid4().hex * 2,
        input_kind,
        call_mode,
        eval_run_id,
        status,
        status,
        status,
    )
    return uuid.UUID(str(row[0]))


def insert_header(
    db: TestDatabase,
    paper_id: uuid.UUID,
    task: str,
    content: Mapping[str, object] | None = None,
    *,
    status: str = "active",
    run_id: uuid.UUID | None = None,
    input_kind: str = "abstract",
) -> uuid.UUID:
    """一列表頭（`active` 先把舊的 `active` 標成 `superseded`），回它的 id。`rejected` 的原因固定是
    `llm_output_invalid`；`content` 是這張表的內容欄位。"""
    content = content or {}
    table = TASK_TABLE[task]
    lang = TASK_LANG[task]
    if run_id is None:
        run_id = insert_run(db, paper_id, task, input_kind=input_kind)
    if status == "active":
        execute(
            db,
            f"UPDATE {table} SET status = 'superseded'"
            " WHERE paper_id = %s AND lang = %s AND status = 'active'",
            paper_id,
            lang,
        )
    columns = "".join(f", {name}" for name in content)
    placeholders = "".join(", %s" for _ in content)
    (row,) = execute(
        db,
        f"INSERT INTO {table} (paper_id, run_id, input_kind, input_sha256, lang, status,"
        f" reject_reason{columns})"
        f" SELECT paper_id, id, %s, input_sha256, %s, %s, %s{placeholders}"
        " FROM derivation_run WHERE id = %s RETURNING id",
        input_kind,
        lang,
        status,
        "llm_output_invalid" if status == "rejected" else None,
        *content.values(),
        run_id,
    )
    return uuid.UUID(str(row[0]))


def insert_classification(
    db: TestDatabase,
    paper_id: uuid.UUID,
    topics: Sequence[str],
    methods: Sequence[str] = (),
    *,
    status: str = "active",
    run_id: uuid.UUID | None = None,
    input_kind: str = "abstract",
) -> uuid.UUID:
    header_id = insert_header(
        db,
        paper_id,
        "classify",
        {} if status == "rejected" else {"dropped_label_count": 0},
        status=status,
        run_id=run_id,
        input_kind=input_kind,
    )
    for field, keys in (("topic", topics), ("method", methods)):
        for key in keys:
            execute(
                db,
                "INSERT INTO classification_label (classification_id, field, key)"
                " VALUES (%s, %s, %s)",
                header_id,
                field,
                key,
            )
    return header_id


def create_event(database: Database, event: NewJudgmentEvent) -> uuid.UUID | None:
    with database.transaction() as connection:
        return create_judgment_event(connection, event)


def fix(
    database: Database,
    paper_id: uuid.UUID,
    field: TaxonomyField,
    keys: Sequence[str],
    *,
    member_id: uuid.UUID = MEMBER_A,
) -> uuid.UUID:
    event_id = create_event(
        database,
        classification_fix(
            member_id=member_id,
            actor_role=ActorRole.MEMBER,
            paper_id=paper_id,
            field=field,
            keys=keys,
            reason=None,
        ),
    )
    assert event_id is not None
    return event_id


def report(database: Database, zh_summary_id: uuid.UUID, field: SummaryField) -> uuid.UUID:
    event_id = create_event(
        database,
        summary_report(
            member_id=MEMBER_A,
            actor_role=ActorRole.MEMBER,
            zh_summary_id=zh_summary_id,
            field=field,
            reason="摘要沒有提到",
        ),
    )
    assert event_id is not None
    return event_id


def resolve(
    database: Database,
    zh_summary_id: uuid.UUID,
    field: SummaryField,
    report_event_id: uuid.UUID,
    resolution: SummaryResolution = SummaryResolution.CONFIRMED_OK,
) -> uuid.UUID | None:
    return create_event(
        database,
        summary_report_resolution(
            member_id=ADMIN,
            zh_summary_id=zh_summary_id,
            field=field,
            report_event_id=report_event_id,
            resolution=resolution,
            reason=None,
        ),
    )


# --- 讀 view ------------------------------------------------------------------------------


def _classification(db: TestDatabase, paper_id: uuid.UUID) -> dict[str, tuple[Any, ...]]:
    rows = execute(
        db,
        "SELECT field, keys, human_verified, fix_member_count, members_disagree"
        " FROM effective_classification WHERE paper_id = %s",
        paper_id,
    )
    return {row[0]: tuple(row[1:]) for row in rows}


def _reported(db: TestDatabase, paper_id: uuid.UUID) -> list[tuple[uuid.UUID, list[str]]]:
    rows = execute(
        db,
        "SELECT zh_summary_id, reported_fields FROM effective_zh_summary WHERE paper_id = %s",
        paper_id,
    )
    return [(row[0], row[1]) for row in rows]


# --- effective_classification ---------------------------------------------------------------


def test_human_fix_wins_over_ai_per_field(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_classification(db, paper_id, ["erp"], ["survey_sem"])

    fix(database, paper_id, TaxonomyField.TOPIC, ["supply_chain"])

    assert _classification(db, paper_id) == {
        "topic": (["supply_chain"], True, 1, False),
        "method": (["survey_sem"], False, 0, False),
    }


def test_rerun_does_not_overwrite_the_human_value(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_classification(db, paper_id, ["erp"], ["survey_sem"])
    fix(database, paper_id, TaxonomyField.TOPIC, ["supply_chain"])

    # 摘要變了、重跑分類：新的 active 取代舊的。
    insert_classification(db, paper_id, ["erp", "it_strategy"], ["case_study"])

    assert execute(
        db,
        "SELECT count(*) FROM classification WHERE paper_id = %s AND status = 'active'",
        paper_id,
    ) == [(1,)]
    assert _classification(db, paper_id) == {
        "topic": (["supply_chain"], True, 1, False),
        "method": (["case_study"], False, 0, False),
    }


def test_same_member_fixing_twice_counts_once(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_classification(db, paper_id, ["it_strategy"])
    fix(database, paper_id, TaxonomyField.TOPIC, ["erp"])
    fix(database, paper_id, TaxonomyField.TOPIC, ["supply_chain"])

    assert _classification(db, paper_id)["topic"] == (["supply_chain"], True, 1, False)


def test_members_disagree_uses_each_members_latest_fix(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_classification(db, paper_id, ["it_strategy"])
    fix(database, paper_id, TaxonomyField.TOPIC, ["erp"], member_id=MEMBER_A)
    fix(database, paper_id, TaxonomyField.TOPIC, ["supply_chain"], member_id=MEMBER_B)

    assert _classification(db, paper_id)["topic"] == (["supply_chain"], True, 2, True)

    # A 改成和 B 一樣：仍是兩人，但意見相同。
    fix(database, paper_id, TaxonomyField.TOPIC, ["supply_chain"], member_id=MEMBER_A)

    assert _classification(db, paper_id)["topic"] == (["supply_chain"], True, 2, False)


def _raw_fix(db: TestDatabase, paper_id: uuid.UUID, member_id: uuid.UUID, keys: str) -> None:
    """繞過 domain 直接用超級使用者寫一筆分類修正（CHECK 不擋排序、重複）。"""
    execute(
        db,
        "INSERT INTO judgment_event (kind, member_id, actor_role, target_type, target_id, field,"
        " new_value) VALUES ('classification_fix', %s, 'member', 'paper', %s, 'topic',"
        " %s::jsonb)",
        member_id,
        paper_id,
        keys,
    )


def test_view_sorts_and_dedupes_human_keys_written_by_raw_sql(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_classification(db, paper_id, ["it_strategy"])
    _raw_fix(db, paper_id, MEMBER_A, '["supply_chain", "erp", "erp"]')

    assert _classification(db, paper_id)["topic"] == (["erp", "supply_chain"], True, 1, False)

    # 另一位成員選同一組 key、順序不同：不算意見不同。
    _raw_fix(db, paper_id, MEMBER_B, '["erp", "supply_chain"]')

    assert _classification(db, paper_id)["topic"] == (["erp", "supply_chain"], True, 2, False)


def test_ai_labels_sort_like_python_and_only_active_counts(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_classification(db, paper_id, ["ea", "e_x"], [])
    insert_classification(db, paper_id, ["erp"], status="rejected")
    insert_classification(db, paper_id, ["erp"], status="candidate")

    (row,) = execute(
        db,
        "SELECT keys, model, input_kind, classification_id IS NOT NULL"
        " FROM effective_classification WHERE paper_id = %s AND field = 'topic'",
        paper_id,
    )
    assert row == (sorted(["ea", "e_x"]), MODEL, "abstract", True)
    assert _classification(db, paper_id)["method"] == ([], False, 0, False)


def test_human_value_without_ai_has_no_ai_columns(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    fix(database, paper_id, TaxonomyField.TOPIC, ["erp"])

    rows = execute(
        db,
        "SELECT field, keys, human_verified, classification_id, model, input_kind,"
        " fixed_at IS NOT NULL FROM effective_classification WHERE paper_id = %s",
        paper_id,
    )
    assert rows == [("topic", ["erp"], True, None, None, None, True)]


# --- effective_zh_summary -------------------------------------------------------------------

ZH_CONTENT = {
    "research_question": "遠距團隊如何採用協作平台？",
    "method": "問卷調查",
    "theory": "科技接受模型",
    "findings": None,
}


def test_report_shows_until_resolved(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    summary_id = insert_header(db, paper_id, "zh_summary", ZH_CONTENT)
    report_id = report(database, summary_id, SummaryField.THEORY)

    assert _reported(db, paper_id) == [(summary_id, ["theory"])]

    assert resolve(database, summary_id, SummaryField.THEORY, report_id) is not None

    assert _reported(db, paper_id) == [(summary_id, [])]


def test_reported_fields_follow_the_fixed_order(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    summary_id = insert_header(db, paper_id, "zh_summary", ZH_CONTENT)
    report(database, summary_id, SummaryField.FINDINGS)
    report(database, summary_id, SummaryField.RESEARCH_QUESTION)
    second = report(database, summary_id, SummaryField.RESEARCH_QUESTION)
    resolve(database, summary_id, SummaryField.RESEARCH_QUESTION, second)

    # 同一欄有一筆回報還沒處理，標示照樣在。
    assert _reported(db, paper_id) == [(summary_id, ["research_question", "findings"])]


def test_regenerated_summary_starts_without_reports(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    old_id = insert_header(db, paper_id, "zh_summary", ZH_CONTENT)
    report(database, old_id, SummaryField.THEORY)

    new_id = insert_header(db, paper_id, "zh_summary", ZH_CONTENT)

    assert _reported(db, paper_id) == [(new_id, [])]


def test_effective_zh_summary_carries_provenance_columns(
    db: TestDatabase, paper_id: uuid.UUID
) -> None:
    insert_header(db, paper_id, "zh_summary", status="rejected")
    summary_id = insert_header(db, paper_id, "zh_summary", ZH_CONTENT)

    rows = execute(
        db,
        "SELECT zh_summary_id, model, input_kind, generated_at IS NOT NULL, research_question,"
        " findings FROM effective_zh_summary WHERE paper_id = %s",
        paper_id,
    )
    assert rows == [(summary_id, MODEL, "abstract", True, ZH_CONTENT["research_question"], None)]


def test_resolution_must_point_at_a_report_of_the_same_summary_and_field(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    summary_id = insert_header(db, paper_id, "zh_summary", ZH_CONTENT)
    other_id = insert_header(
        db, create_paper(database, doi="10.1/x", openalex_id="W9"), "zh_summary", ZH_CONTENT
    )
    report_id = report(database, summary_id, SummaryField.THEORY)
    fix_id = fix(database, paper_id, TaxonomyField.TOPIC, ["erp"])

    assert resolve(database, summary_id, SummaryField.THEORY, uuid.uuid4()) is None
    assert resolve(database, summary_id, SummaryField.THEORY, fix_id) is None
    assert resolve(database, other_id, SummaryField.THEORY, report_id) is None
    assert resolve(database, summary_id, SummaryField.METHOD, report_id) is None
    assert execute(
        db, "SELECT count(*) FROM judgment_event WHERE kind = 'summary_report_resolution'"
    ) == [(0,)]

    assert resolve(database, summary_id, SummaryField.THEORY, report_id) is not None
    # 一筆回報最多一筆處理結果。
    assert (
        resolve(database, summary_id, SummaryField.THEORY, report_id, SummaryResolution.REGENERATED)
        is None
    )
    assert execute(
        db,
        "SELECT member_id, actor_role, new_value, report_event_id FROM judgment_event"
        " WHERE kind = 'summary_report_resolution'",
    ) == [(ADMIN, "admin", "confirmed_ok", report_id)]


# --- effective_research_model ---------------------------------------------------------------


def insert_research_model(
    db: TestDatabase, paper_id: uuid.UUID, *, with_details: bool = True, **content: object
) -> uuid.UUID:
    values: dict[str, object] = {
        "is_review_or_meta_analysis": False,
        "dropped_context_value_count": 0,
        "unsupported_context_value_count": 0,
        "corrected_support_count": 0,
    } | content
    header_id = insert_header(db, paper_id, "extract_model", values)
    if not with_details:
        return header_id
    trust, loyalty = uuid.uuid4(), uuid.uuid4()
    for construct_id, name in ((trust, f"trust {header_id}"), (loyalty, f"loyalty {header_id}")):
        execute(
            db,
            "INSERT INTO construct (id, normalized_name, canonical_id) VALUES (%s, %s, %s)",
            construct_id,
            name,
            construct_id,
        )
    execute(
        db,
        "INSERT INTO construct_mention (research_model_id, construct_id, name_en, label_zh,"
        " cronbach_alpha, item_count) VALUES (%s, %s, 'Trust', '信任', 0.910, 4),"
        " (%s, %s, 'Loyalty', '忠誠度', NULL, NULL)",
        header_id,
        trust,
        header_id,
        loyalty,
    )
    execute(
        db,
        "INSERT INTO hypothesis (research_model_id, ordinal, sentence, sentence_sha256,"
        " cause_construct_id, effect_construct_id, evidence_type, support, beta, p_value,"
        " sample_size) VALUES (%s, 1, 'Trust increases loyalty.', %s, %s, %s,"
        " 'empirical_test', 'supported', -0.10000000000000001, 0.01, 200)",
        header_id,
        "b" * 64,
        trust,
        loyalty,
    )
    return header_id


def test_research_model_view_carries_details_as_jsonb_with_numbers_as_text(
    db: TestDatabase, paper_id: uuid.UUID
) -> None:
    header_id = insert_research_model(db, paper_id, country="tw", sample_size=200)

    (row,) = execute(
        db,
        "SELECT research_model_id, model, country, sample_size, constructs, hypotheses"
        " FROM effective_research_model WHERE paper_id = %s",
        paper_id,
    )
    assert row[:4] == (header_id, MODEL, "tw", 200)
    constructs, hypotheses = row[4], row[5]
    assert [
        (item["name_en"], item["cronbach_alpha"], item["item_count"]) for item in constructs
    ] == [
        ("Trust", "0.910", 4),
        ("Loyalty", None, None),
    ]
    (hypothesis,) = hypotheses
    assert hypothesis["beta"] == "-0.10000000000000001"
    assert hypothesis["p_value"] == "0.01"
    assert hypothesis["cause_construct_id"] == constructs[0]["construct_id"]
    assert (hypothesis["ordinal"], hypothesis["support"], hypothesis["sample_size"]) == (
        1,
        "supported",
        200,
    )


def test_research_model_view_has_only_the_active_one_and_empty_arrays(
    db: TestDatabase, paper_id: uuid.UUID
) -> None:
    insert_research_model(db, paper_id)
    newest = insert_research_model(db, paper_id, with_details=False)

    assert execute(
        db,
        "SELECT research_model_id, constructs, hypotheses FROM effective_research_model"
        " WHERE paper_id = %s",
        paper_id,
    ) == [(newest, [], [])]
