"""schema 體檢（NFR-7、NFR-8、Conventions「主鍵」「時間」）：遷移後的資料庫每張表都守規則。

- 主鍵：單一欄位、型別 `uuid`、預設值 `uuidv7()`。例外只能列在 `PRIMARY_KEY_EXCEPTIONS`，
  而且清單裡的每一張表都要真的存在（表改名或拿掉時，例外不會默默留著）。
- 時間：不能有 `timestamp`（不帶時區）的欄位；名稱是 `*_at` 的欄位一定是 `timestamptz`。
- 外鍵：每個外鍵的欄位都要有索引（索引的開頭幾欄剛好是外鍵的欄位，順序不拘；
  部分索引（帶 WHERE）不算）。
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

# 只有部分索引（或沒有索引）、但不能改的外鍵，寫明理由；清單裡的每一項都要真的是沒有完整索引的外鍵。
FOREIGN_KEY_INDEX_EXCEPTIONS: dict[str, str] = {
    "procrastinate_jobs.worker_id": (
        "Procrastinate 凍結的 SQL（ADR-0019）只建了部分索引"
        "（idx_procrastinate_jobs_worker_not_null，WHERE status = 'doing'），不改套件原檔"
    ),
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


# 沒有索引的外鍵：索引開頭的 n 欄（n＝外鍵的欄數）和外鍵的欄位是同一組。
UNINDEXED_FOREIGN_KEYS_SQL = """
SELECT (
    SELECT string_agg(a.attname, ', ' ORDER BY k.ord)
    FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
    JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
)
FROM pg_constraint con
WHERE con.conrelid = %s AND con.contype = 'f'
  AND NOT EXISTS (
    SELECT 1
    FROM pg_index i,
         LATERAL (
             SELECT (string_to_array(i.indkey::text, ' ')::int2[])
                    [1:cardinality(con.conkey)] AS lead
         ) l
    WHERE i.indrelid = con.conrelid AND i.indpred IS NULL
      AND l.lead @> con.conkey AND l.lead <@ con.conkey
  )
ORDER BY con.conname
"""


def _short(qualified: str) -> str:
    return qualified.removeprefix("public.")


def schema_problems(
    connection: psycopg.Connection,
    exceptions: dict[str, str],
    foreign_key_exceptions: dict[str, str] = FOREIGN_KEY_INDEX_EXCEPTIONS,
) -> list[str]:
    """回傳每一條違規；沒有違規時是空清單。"""
    tables = connection.execute(TABLES_SQL).fetchall()
    names = {_short(name) for _oid, name in tables}
    problems = [
        f"{missing}：在例外清單裡，但資料庫沒有這張表"
        for missing in exceptions
        if missing not in names
    ]

    unindexed_exceptions: set[str] = set()
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
        for (columns,) in connection.execute(UNINDEXED_FOREIGN_KEYS_SQL, (oid,)).fetchall():
            foreign_key = f"{table}.{columns}"
            if foreign_key in foreign_key_exceptions:
                unindexed_exceptions.add(foreign_key)
            else:
                problems.append(f"{foreign_key}：外鍵沒有索引")
    problems += [
        f"{foreign_key}：在外鍵索引的例外清單裡，但它不是沒有完整索引的外鍵"
        for foreign_key in foreign_key_exceptions
        if foreign_key not in unindexed_exceptions
    ]
    return problems


def test_migrated_schema_follows_conventions(migrated_database: TestDatabase) -> None:
    with migrated_database.connect(OWNER_ROLE) as connection:
        assert schema_problems(connection, PRIMARY_KEY_EXCEPTIONS) == []


def test_every_exception_has_a_reason() -> None:
    assert all(reason.strip() for reason in PRIMARY_KEY_EXCEPTIONS.values())
    assert all(reason.strip() for reason in FOREIGN_KEY_INDEX_EXCEPTIONS.values())


def test_stale_foreign_key_exception_is_reported(migrated_database: TestDatabase) -> None:
    exceptions = {**FOREIGN_KEY_INDEX_EXCEPTIONS, "external_id.paper_id": "測試用"}
    with migrated_database.connect(OWNER_ROLE) as connection:
        problems = schema_problems(connection, PRIMARY_KEY_EXCEPTIONS, exceptions)

    assert problems == ["external_id.paper_id：在外鍵索引的例外清單裡，但它不是沒有完整索引的外鍵"]


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
    "bad_unindexed_fk": (
        "CREATE TABLE bad_unindexed_fk (id uuid PRIMARY KEY DEFAULT uuidv7(),"
        " good_table_id uuid NOT NULL REFERENCES good_table (id))"
    ),
    # 只有部分索引（帶 WHERE）的外鍵也算沒有索引。
    "bad_partial_fk": (
        "CREATE TABLE bad_partial_fk (id uuid PRIMARY KEY DEFAULT uuidv7(),"
        " good_table_id uuid REFERENCES good_table (id));"
        " CREATE INDEX ON bad_partial_fk (good_table_id) WHERE good_table_id IS NOT NULL"
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
            # 有索引的外鍵（含複合索引的開頭欄）不算違規。
            connection.execute(
                "CREATE TABLE good_child (id uuid PRIMARY KEY DEFAULT uuidv7(),"
                " good_table_id uuid NOT NULL REFERENCES good_table (id), n int)"
            )
            connection.execute("CREATE INDEX ON good_child (good_table_id, n)")
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
