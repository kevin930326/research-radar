"""AI 表頭表的規則（AD-7、ADR-0015、ADR-0030），對遷移後的資料庫檢查：

- 登記在 `HEADER_TABLES` 的每張表都有表頭欄位：`paper_id`、`run_id`、`input_kind`、
  `input_sha256`、`lang`、`status` 是 NOT NULL；`abstract_id`、`reject_reason` 可以是 NULL。
- 每張表都有 `(paper_id, lang) WHERE status = 'active'` 的部分唯一索引（同一篇、同一種語言只有一筆
  有效的結果）。
- 資料庫裡同時有 `run_id` 與 `status` 欄位的表＝登記清單：新加 AI 結果表忘了登記會被抓到。
"""

import psycopg

from radar.domain.ai_outputs import HEADER_TABLES
from tests.database import OWNER_ROLE, TestDatabase

NOT_NULL = ("paper_id", "run_id", "input_kind", "input_sha256", "lang", "status")
NULLABLE = ("abstract_id", "reject_reason")

COLUMNS_SQL = """
SELECT column_name, is_nullable = 'YES' FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = %s
"""

ACTIVE_INDEX_SQL = """
SELECT pg_get_indexdef(i.indexrelid), pg_get_expr(i.indpred, i.indrelid)
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE i.indrelid = %s::regclass AND i.indisunique AND c.relname = %s
"""

RUN_STATUS_TABLES_SQL = """
SELECT table_name FROM information_schema.columns
WHERE table_schema = 'public' AND column_name IN ('run_id', 'status')
GROUP BY table_name HAVING count(*) = 2
"""


def header_table_problems(connection: psycopg.Connection) -> list[str]:
    problems: list[str] = []
    registered = set(HEADER_TABLES.values())
    for table in sorted(registered):
        columns: dict[str, bool] = dict(connection.execute(COLUMNS_SQL, (table,)).fetchall())
        if not columns:
            problems.append(f"{table}：登記了但資料庫沒有這張表")
            continue
        for column in NOT_NULL:
            if columns.get(column) is not False:
                problems.append(f"{table}.{column}：要有這欄而且 NOT NULL")
        for column in NULLABLE:
            if columns.get(column) is not True:
                problems.append(f"{table}.{column}：要有這欄而且可以是 NULL")
        index = connection.execute(ACTIVE_INDEX_SQL, (table, f"uq_{table}_active_lang")).fetchone()
        if (
            index is None
            or "(paper_id, lang)" not in index[0]
            or index[1] != "(status = 'active'::text)"
        ):
            problems.append(f"{table}：缺 (paper_id, lang) WHERE status = 'active' 的部分唯一索引")
    found = {row[0] for row in connection.execute(RUN_STATUS_TABLES_SQL).fetchall()}
    for table in sorted(found - registered):
        problems.append(f"{table}：有 run_id 與 status，但沒有登記在 HEADER_TABLES")
    return problems


def test_header_tables_follow_rules(migrated_database: TestDatabase) -> None:
    with migrated_database.connect(OWNER_ROLE) as connection:
        assert header_table_problems(connection) == []
    assert set(HEADER_TABLES.values()) == {"zh_summary", "classification", "research_model"}


def test_check_catches_unregistered_table_and_missing_index(
    migrated_database: TestDatabase,
) -> None:
    with migrated_database.connect(OWNER_ROLE, autocommit=False) as connection:
        try:
            connection.execute("CREATE TABLE fake_ai_result (id uuid, run_id uuid, status text)")
            connection.execute("DROP INDEX uq_zh_summary_active_lang")
            connection.execute("ALTER TABLE classification ALTER COLUMN lang DROP NOT NULL")

            problems = header_table_problems(connection)
        finally:
            connection.rollback()

    assert problems == [
        "classification.lang：要有這欄而且 NOT NULL",
        "zh_summary：缺 (paper_id, lang) WHERE status = 'active' 的部分唯一索引",
        "fake_ai_result：有 run_id 與 status，但沒有登記在 HEADER_TABLES",
    ]
