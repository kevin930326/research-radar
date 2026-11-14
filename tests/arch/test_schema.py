"""schema 體檢（NFR-7、NFR-8、Conventions「主鍵」「時間」）：遷移後的資料庫每張表都守規則。

- 主鍵：單一欄位、型別 `uuid`、預設值 `uuidv7()`。例外只能列在 `PRIMARY_KEY_EXCEPTIONS`，
  而且清單裡的每一張表都要真的存在（表改名或拿掉時，例外不會默默留著）。
- 時間：不能有 `timestamp`（不帶時區）的欄位；名稱是 `*_at` 的欄位一定是 `timestamptz`。
"""

import psycopg

from tests.database import OWNER_ROLE, TestDatabase

# 之後的 Story 加例外時寫在這裡並註明理由（例如 snapshot.number、複合主鍵的 embedding、qa_usage）。
PROCRASTINATE_REASON = (
    "Procrastinate 自己的表（遷移 0002 執行凍結的 SQL，ADR-0019），主鍵是它定的 bigint 流水號；"
    "Conventions「主鍵」明列為例外"
)
PRIMARY_KEY_EXCEPTIONS: dict[str, str] = {
    "alembic_version": "Alembic 自己的版本表，主鍵是版本號字串",
    "procrastinate_jobs": PROCRASTINATE_REASON,
    "procrastinate_workers": PROCRASTINATE_REASON,
    "procrastinate_events": PROCRASTINATE_REASON,
    "procrastinate_periodic_defers": PROCRASTINATE_REASON,
}

TABLES_SQL = """
SELECT c.oid, n.nspname || '.' || c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_%'
ORDER BY 2
"""

PRIMARY_KEY_SQL = """
SELECT a.attname, format_type(a.atttypid, a.atttypmod), pg_get_expr(d.adbin, d.adrelid)
FROM pg_index i
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE i.indrelid = %s AND i.indisprimary
"""

COLUMNS_SQL = """
SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.atttypid::regtype::text
FROM pg_attribute a
WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""


def _short(qualified: str) -> str:
    return qualified.removeprefix("public.")


def schema_problems(connection: psycopg.Connection, exceptions: dict[str, str]) -> list[str]:
    """回傳每一條違規；沒有違規時是空清單。"""
    tables = connection.execute(TABLES_SQL).fetchall()
    names = {_short(name) for _oid, name in tables}
    problems = [
        f"{missing}：在例外清單裡，但資料庫沒有這張表"
        for missing in exceptions
        if missing not in names
    ]

    for oid, qualified in tables:
        table = _short(qualified)
        if table not in exceptions:
            key = connection.execute(PRIMARY_KEY_SQL, (oid,)).fetchall()
            if not key:
                problems.append(f"{table}：沒有主鍵")
            elif len(key) > 1:
                problems.append(f"{table}：主鍵是複合主鍵（{', '.join(c for c, _t, _d in key)}）")
            else:
                column, type_name, default = key[0]
                if type_name != "uuid" or default != "uuidv7()":
                    problems.append(
                        f"{table}：主鍵 {column} 要是 uuid、預設值 uuidv7()"
                        f"（現在是 {type_name}、預設值 {default}）"
                    )
        for column, type_name, base_type in connection.execute(COLUMNS_SQL, (oid,)).fetchall():
            where = f"{table}.{column}"
            if base_type == "timestamp without time zone":
                problems.append(f"{where}：時間欄位要用 timestamptz（現在是 {type_name}）")
            elif column.endswith("_at") and base_type != "timestamp with time zone":
                problems.append(f"{where}：*_at 欄位要用 timestamptz（現在是 {type_name}）")
    return problems


def test_migrated_schema_follows_conventions(migrated_database: TestDatabase) -> None:
    with migrated_database.connect(OWNER_ROLE) as connection:
        assert schema_problems(connection, PRIMARY_KEY_EXCEPTIONS) == []


def test_every_exception_has_a_reason() -> None:
    assert all(reason.strip() for reason in PRIMARY_KEY_EXCEPTIONS.values())


VIOLATIONS = {
    "bad_serial": "CREATE TABLE bad_serial (id bigserial PRIMARY KEY)",
    "bad_random_uuid": (
        "CREATE TABLE bad_random_uuid (id uuid PRIMARY KEY DEFAULT gen_random_uuid())"
    ),
    "bad_composite": (
        "CREATE TABLE bad_composite (a uuid DEFAULT uuidv7(), b uuid, PRIMARY KEY (a, b))"
    ),
    "bad_no_pk": "CREATE TABLE bad_no_pk (id uuid DEFAULT uuidv7())",
    "bad_timestamp": (
        "CREATE TABLE bad_timestamp (id uuid PRIMARY KEY DEFAULT uuidv7(), seen timestamp)"
    ),
    "bad_at_column": (
        "CREATE TABLE bad_at_column (id uuid PRIMARY KEY DEFAULT uuidv7(), created_at date)"
    ),
}


def test_schema_check_catches_each_violation(migrated_database: TestDatabase) -> None:
    # 證明體檢真的抓得到：在交易裡建違規的表，檢查完整個退回，不留在共用的測試資料庫。
    with migrated_database.connect(OWNER_ROLE, autocommit=False) as connection:
        try:
            connection.execute(
                "CREATE TABLE good_table"
                " (id uuid PRIMARY KEY DEFAULT uuidv7(), created_at timestamptz NOT NULL)"
            )
            for statement in VIOLATIONS.values():
                connection.execute(statement)

            problems = schema_problems(connection, PRIMARY_KEY_EXCEPTIONS)
        finally:
            connection.rollback()

    by_table: dict[str, list[str]] = {}
    for problem in problems:
        by_table.setdefault(problem.split("：")[0].split(".")[0], []).append(problem)
    assert set(by_table) == set(VIOLATIONS), problems
    assert all(len(found) == 1 for found in by_table.values()), problems


def test_exception_for_missing_table_is_reported(migrated_database: TestDatabase) -> None:
    exceptions = {**PRIMARY_KEY_EXCEPTIONS, "no_such_table": "測試用"}
    with migrated_database.connect(OWNER_ROLE) as connection:
        problems = schema_problems(connection, exceptions)

    assert problems == ["no_such_table：在例外清單裡，但資料庫沒有這張表"]
