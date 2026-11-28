"""遷移 0014（人工判斷事件、稽核事件、三個有效值 view）對真的 PostgreSQL：降版、升版、權限、CHECK。

- 降到 0013 後兩張事件表與三個 view 都不在，再升回來都在。
- 應用程式角色對 `judgment_event`、`audit_event` 只能新增與讀取（UPDATE、DELETE 被拒）；對三個
  view 只能讀取。
- `judgment_event` 各 `kind` 的形狀由 CHECK 擋（domain 是第一道，見 `tests/unit/test_judgments.py`；
  key 排序、去重只在 domain）；`report_event_id` 要指到存在的事件、一筆回報最多一筆處理結果。
- 處理結果的觸發器：直接寫 SQL 時，指到的事件要是同一個對象、同一欄的 `summary_report`；觸發器
  函式不是 SECURITY DEFINER、`search_path` 固定。
- `audit_event`：`system` ⇔ 沒有成員、`action` 的寫法、`detail` 是物件。
"""

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from tests.database import APP_ROLE, ROOT, TestDatabase, connect_admin, migrate

BIN = Path(sys.executable).parent
EVENT_TABLES = ("judgment_event", "audit_event")
VIEWS = ("effective_zh_summary", "effective_classification", "effective_research_model")
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
MEMBER = uuid.UUID("00000000-0000-7000-8000-000000000001")
LOW = uuid.UUID("00000000-0000-7000-8000-000000000010")
HIGH = uuid.UUID("00000000-0000-7000-8000-000000000020")


def _alembic(database: TestDatabase, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BIN / "alembic"), *args],
        env={"PYTHONUNBUFFERED": "1", "RADAR_MIGRATION_DATABASE_URL": database.owner_url},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _app(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    """用應用程式角色執行一句 SQL，回所有列。"""
    with db.connect(APP_ROLE) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description is not None else []


def _relations(db: TestDatabase) -> set[str]:
    with connect_admin(db.name) as admin:
        rows = admin.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    return {row[0] for row in rows}


def _event(db: TestDatabase, **changes: object) -> uuid.UUID:
    """用應用程式角色新增一筆 `judgment_event`（預設是合格的分類修正），回它的 id。"""
    values: dict[str, object] = {
        "kind": "classification_fix",
        "member_id": MEMBER,
        "actor_role": "member",
        "target_type": "paper",
        "target_id": uuid.uuid4(),
        "field": "topic",
        "new_value": json.dumps(["supply_chain"]),
    } | changes
    columns = ", ".join(values)
    placeholders = ", ".join("%s" for _ in values)
    (row,) = _app(
        db,
        f"INSERT INTO judgment_event ({columns}) VALUES ({placeholders}) RETURNING id",
        *values.values(),
    )
    return uuid.UUID(str(row[0]))


def _audit(db: TestDatabase, **changes: object) -> uuid.UUID:
    values: dict[str, object] = {
        "actor_member_id": MEMBER,
        "actor_role": "admin",
        "action": "classification_fix",
        "target_type": "paper",
        "target_id": uuid.uuid4(),
        "detail": json.dumps({"field": "topic"}),
    } | changes
    columns = ", ".join(values)
    placeholders = ", ".join("%s" for _ in values)
    (row,) = _app(
        db,
        f"INSERT INTO audit_event ({columns}) VALUES ({placeholders}) RETURNING id",
        *values.values(),
    )
    return uuid.UUID(str(row[0]))


def _report(db: TestDatabase, target_id: uuid.UUID) -> uuid.UUID:
    return _event(
        db,
        kind="summary_report",
        target_type="zh_summary",
        target_id=target_id,
        field="theory",
        new_value=None,
        reason="摘要沒提到",
    )


# --- 降版、升版 ---------------------------------------------------------------------------------


def test_downgrade_to_0013_drops_tables_and_views_and_upgrade_restores_them(
    db: TestDatabase,
) -> None:
    assert _relations(db) >= {*EVENT_TABLES, *VIEWS}
    _event(db)
    _audit(db)

    down = _alembic(db, "downgrade", "0013")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0013"}
    assert not {*EVENT_TABLES, *VIEWS} & _relations(db)
    assert db.has_table("research_model")

    migrate(db)

    assert db.revisions() == {"0023"}
    assert _relations(db) >= {*EVENT_TABLES, *VIEWS}
    for table in EVENT_TABLES:
        assert _app(db, f"SELECT count(*) FROM {table}") == [(0,)]


# --- 權限 ---------------------------------------------------------------------------------------


def _granted(db: TestDatabase, relation: str) -> set[str]:
    with connect_admin(db.name) as admin:
        return {
            privilege
            for privilege in TABLE_PRIVILEGES
            if admin.execute(
                "SELECT has_table_privilege(%s, %s, %s)", (APP_ROLE, relation, privilege)
            ).fetchone()
            == (True,)
        }


@pytest.mark.parametrize("table", EVENT_TABLES)
def test_app_role_can_only_insert_and_read_event_tables(db: TestDatabase, table: str) -> None:
    assert _granted(db, table) == {"SELECT", "INSERT"}


@pytest.mark.parametrize("view", VIEWS)
def test_app_role_can_only_read_views(db: TestDatabase, view: str) -> None:
    assert _granted(db, view) == {"SELECT"}
    assert _app(db, f"SELECT count(*) FROM {view}") == [(0,)]
    for statement in (
        f"DELETE FROM {view}",
        f"UPDATE {view} SET paper_id = paper_id",
        f"INSERT INTO {view} (paper_id) VALUES (uuidv7())",
    ):
        # 權限上面直接查過；這裡的 view 本來就不能自動更新，兩種錯誤都算擋下。
        with pytest.raises(
            (psycopg.errors.InsufficientPrivilege, psycopg.errors.ObjectNotInPrerequisiteState)
        ):
            _app(db, statement)


def test_app_role_cannot_update_or_delete_judgment_events(db: TestDatabase) -> None:
    event_id = _event(db)

    assert _app(db, "SELECT kind, field FROM judgment_event WHERE id = %s", event_id) == [
        ("classification_fix", "topic")
    ]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(db, "UPDATE judgment_event SET reason = 'x' WHERE id = %s", event_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(db, "DELETE FROM judgment_event WHERE id = %s", event_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(db, "TRUNCATE judgment_event")
    assert _app(db, "SELECT count(*) FROM judgment_event") == [(1,)]


def test_app_role_cannot_update_or_delete_audit_events(db: TestDatabase) -> None:
    audit_id = _audit(db)
    _audit(db, actor_member_id=None, actor_role="system", target_id=None, detail="{}")

    assert _app(db, "SELECT count(*) FROM audit_event") == [(2,)]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(db, "UPDATE audit_event SET action = 'x' WHERE id = %s", audit_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _app(db, "DELETE FROM audit_event WHERE id = %s", audit_id)
    assert _app(db, "SELECT count(*) FROM audit_event") == [(2,)]


def test_event_stores_who_when_target_field_values_reason_and_role(db: TestDatabase) -> None:
    target = uuid.uuid4()
    ai_result = uuid.uuid4()
    event_id = _event(
        db,
        actor_role="admin",
        target_id=target,
        old_value=json.dumps(["erp"]),
        reason="主要談供應鏈",
        ai_result_id=ai_result,
    )

    (row,) = _app(
        db,
        "SELECT member_id, actor_role, target_type, target_id, field, old_value, new_value,"
        " reason, ai_result_id, created_at IS NOT NULL FROM judgment_event WHERE id = %s",
        event_id,
    )
    assert row == (
        MEMBER,
        "admin",
        "paper",
        target,
        "topic",
        ["erp"],
        ["supply_chain"],
        "主要談供應鏈",
        ai_result,
        True,
    )


# --- judgment_event 的 CHECK ----------------------------------------------------------------


def test_every_kind_accepts_its_shape(db: TestDatabase) -> None:
    _event(db, field="method", new_value=json.dumps([]))
    _event(db, new_value=json.dumps(["unclassified"]))
    _event(
        db,
        kind="merge_decision",
        target_type="construct_pair",
        target_id=LOW,
        related_id=HIGH,
        field=None,
        new_value=json.dumps({"decision": "reject", "evidence_level": "items"}),
    )
    summary_id = uuid.uuid4()
    report_id = _report(db, summary_id)
    _event(
        db,
        kind="summary_report_resolution",
        actor_role="admin",
        target_type="zh_summary",
        target_id=summary_id,
        field="theory",
        new_value=json.dumps("confirmed_ok"),
        report_event_id=report_id,
    )


MERGE = {
    "kind": "merge_decision",
    "target_type": "construct_pair",
    "target_id": LOW,
    "related_id": HIGH,
    "field": None,
    "new_value": json.dumps({"decision": "accept", "evidence_level": "name"}),
}
REPORT = {
    "kind": "summary_report",
    "target_type": "zh_summary",
    "field": "method",
    "new_value": None,
    "reason": "方法寫錯",
}


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "classification_note"},
        {"actor_role": "system"},
        {"target_type": "zh_summary"},
        {"reason": "  "},
        {"field": "theory"},
        {"field": None},
        {"new_value": None},
        {"new_value": json.dumps("supply_chain")},
        {"new_value": json.dumps([])},
        {"new_value": json.dumps(["Supply Chain"])},
        {"new_value": json.dumps([1])},
        {"new_value": json.dumps(["unclassified", "erp"])},
        {"field": "method", "new_value": json.dumps(["unclassified"])},
        {"old_value": json.dumps(["Bad"])},
        {"old_value": json.dumps({"keys": []})},
        {"old_value": json.dumps([])},
        {"old_value": json.dumps(["unclassified", "erp"])},
        {"related_id": HIGH},
        MERGE | {"target_id": HIGH, "related_id": LOW},
        MERGE | {"related_id": None},
        MERGE | {"field": "topic"},
        MERGE | {"new_value": json.dumps({"decision": "maybe", "evidence_level": "name"})},
        MERGE | {"new_value": json.dumps({"decision": "accept", "evidence_level": "vibes"})},
        MERGE | {"new_value": json.dumps({"decision": "accept", "evidence_level": "name", "x": 1})},
        MERGE | {"new_value": json.dumps({"decision": "accept"})},
        MERGE | {"old_value": json.dumps({"decision": "reject", "evidence_level": "name"})},
        MERGE | {"ai_result_id": LOW},
        REPORT | {"reason": None},
        REPORT | {"field": "topic"},
        REPORT | {"new_value": json.dumps("confirmed_ok")},
        REPORT | {"target_type": "paper"},
    ],
)
def test_judgment_event_checks(db: TestDatabase, changes: dict[str, object]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _event(db, **changes)


def _resolution(db: TestDatabase, report_id: uuid.UUID | None, **changes: object) -> uuid.UUID:
    values: dict[str, object] = {
        "kind": "summary_report_resolution",
        "actor_role": "admin",
        "target_type": "zh_summary",
        "field": "theory",
        "new_value": json.dumps("regenerated"),
        "report_event_id": report_id,
    } | changes
    return _event(db, **values)


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_role": "member"},
        {"new_value": json.dumps("fixed")},
        {"new_value": None},
        {"field": None},
        {"report_event_id": None},
    ],
)
def test_resolution_checks(db: TestDatabase, changes: dict[str, object]) -> None:
    summary_id = uuid.uuid4()
    report_id = _report(db, summary_id)
    with pytest.raises(psycopg.errors.CheckViolation) as raised:
        _resolution(db, report_id, target_id=summary_id, **changes)
    # 是 CHECK 擋下的，不是處理結果的觸發器（對象、欄位都對得上）。
    assert (raised.value.diag.constraint_name or "").startswith("ck_judgment_event_")


def test_resolution_must_point_at_an_existing_event_and_only_once(db: TestDatabase) -> None:
    summary_id = uuid.uuid4()
    report_id = _report(db, summary_id)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _resolution(db, uuid.uuid4(), target_id=summary_id)
    _resolution(db, report_id, target_id=summary_id)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _resolution(db, report_id, target_id=summary_id, new_value=json.dumps("confirmed_ok"))


RESOLUTION_TRIGGER = "tg_judgment_event_resolution_matches_report"


def test_resolution_trigger_rejects_wrong_target_field_or_kind_written_by_raw_sql(
    db: TestDatabase,
) -> None:
    summary_id = uuid.uuid4()
    report_id = _report(db, summary_id)
    other_report_id = _report(db, summary_id)
    resolved_report_id = _report(db, summary_id)
    resolution_id = _resolution(db, resolved_report_id, target_id=summary_id)

    wrong: list[tuple[uuid.UUID, dict[str, object]]] = [
        (report_id, {"target_id": uuid.uuid4()}),  # 別筆中文重點
        (report_id, {"target_id": summary_id, "field": "method"}),  # 別欄
        (resolution_id, {"target_id": summary_id}),  # 指到處理結果，不是回報
    ]
    for pointed_at, changes in wrong:
        with pytest.raises(psycopg.errors.CheckViolation) as raised:
            _resolution(db, pointed_at, **changes)
        assert raised.value.diag.constraint_name == RESOLUTION_TRIGGER

    # 錯的都沒寫進去，UNIQUE 沒被占住：正確的處理結果照樣寫得進去。
    assert _app(
        db, "SELECT count(*) FROM judgment_event WHERE report_event_id = %s", report_id
    ) == [(0,)]
    _resolution(db, report_id, target_id=summary_id)
    _resolution(db, other_report_id, target_id=summary_id, new_value=json.dumps("confirmed_ok"))


def test_resolution_trigger_function_is_not_security_definer_and_pins_search_path(
    db: TestDatabase,
) -> None:
    with connect_admin(db.name) as admin:
        rows = admin.execute(
            "SELECT p.prosecdef, p.proconfig FROM pg_trigger AS t"
            " JOIN pg_proc AS p ON p.oid = t.tgfoid"
            " WHERE t.tgrelid = 'judgment_event'::regclass AND t.tgname = %s",
            (RESOLUTION_TRIGGER,),
        ).fetchall()

    assert rows == [(False, ["search_path=pg_catalog, pg_temp"])]


# --- audit_event 的 CHECK -------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_role": "robot"},
        {"actor_role": "system"},
        {"actor_member_id": None},
        {"action": "Fix Classification"},
        {"target_type": "Paper"},
        {"detail": json.dumps(["x"])},
        {"detail": None},
    ],
)
def test_audit_event_checks(db: TestDatabase, changes: dict[str, object]) -> None:
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation)):
        _audit(db, **changes)
