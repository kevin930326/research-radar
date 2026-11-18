"""備份與還原要讀的資料庫狀態（Story 3.18、ADR-0051）：版本、快照、筆數、是不是空的。

只有維運的 `radar backup`／`radar restore` 用，`viewer` 一律是 `SystemViewer`（整個資料庫都要讀）。
全部只讀；收呼叫端開好的交易連線，不自己 commit。表名只來自 `radar/domain/backup.py` 的固定清單
（小寫識別字），組進 SQL 時一律加雙引號。
"""

from collections.abc import Iterable

from sqlalchemy import Connection, text

from radar.domain.ai_outputs import LlmTask
from radar.domain.backup import BackupCounts, DatabaseVersions, version_text
from radar.domain.viewer import SystemViewer
from radar.repo.ai_outputs import count_active_ai_results
from radar.repo.stats import list_cube_counts
from radar.repo.visibility import visible_to_system_only

# 「空的資料庫」：public 與其他非系統 schema 裡，沒有任何不屬於擴充套件的 relation（資料表、
# 分割表、view、物化 view、序列、外部表）。擴充套件自己帶的物件（vector、pg_trgm）不算。
# 只有函式或型別、沒有 relation 的資料庫算空的；那種情況還原會在同一個交易裡撞名而整個退回
# （ADR-0051「已知限制」）。
_USER_RELATIONS_WHERE = (
    " c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')"
    " AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND n.nspname NOT LIKE 'pg\\_%'"
    " AND NOT EXISTS (SELECT 1 FROM pg_depend d"
    " WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"
)


def find_user_relations(viewer: SystemViewer, connection: Connection) -> list[str]:
    """不屬於擴充套件的 relation（`schema.名稱`）；空的資料庫回空清單。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    sql = (
        "SELECT n.nspname || '.' || c.relname"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        f" WHERE {visible} AND{_USER_RELATIONS_WHERE} ORDER BY 1"
    )
    return [str(row[0]) for row in connection.execute(text(sql))]


def get_database_versions(viewer: SystemViewer, connection: Connection) -> DatabaseVersions:
    """PostgreSQL、pgvector（沒有就是 None）、有沒有 pg_trgm 與 Alembic（沒有版本表就是空的）的
    版本。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    row = (
        connection.execute(
            text(
                "SELECT current_setting('server_version') AS server_version,"
                " current_setting('server_version_num')::int AS server_version_num,"
                " (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS pgvector,"
                " EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') AS has_pg_trgm,"
                " to_regclass('public.alembic_version') IS NOT NULL AS has_alembic"
                f" WHERE {visible}"
            )
        )
        .mappings()
        .one()
    )
    revisions: tuple[str, ...] = ()
    if row["has_alembic"]:
        revisions = tuple(
            sorted(
                str(r[0])
                for r in connection.execute(
                    text(f"SELECT version_num FROM public.alembic_version WHERE {visible}")
                )
            )
        )
    pgvector = row["pgvector"]
    return DatabaseVersions(
        postgres_version=version_text(str(row["server_version"])),
        postgres_version_num=int(row["server_version_num"]),
        pgvector_version=None if pgvector is None else version_text(str(pgvector)),
        has_pg_trgm=bool(row["has_pg_trgm"]),
        alembic_revisions=revisions,
    )


def get_exported_snapshot(viewer: SystemViewer, connection: Connection) -> str:
    """匯出這個交易的快照，給 `pg_dump --snapshot` 用（交易要是 REPEATABLE READ）。

    快照只在這個交易還開著時能被匯入；`pg_dump` 匯入之後就各自持有，這個交易可以結束。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    return str(
        connection.execute(text(f"SELECT pg_export_snapshot() WHERE {visible}")).scalar_one()
    )


def count_backup_rows(
    viewer: SystemViewer, connection: Connection, member_tables: Iterable[str]
) -> tuple[BackupCounts, dict[str, int]]:
    """算數的論文、三種 AI 產出（`active`）、向量的筆數，加上 `member_tables` 每張表的列數。

    算數的論文數是 `repo/stats.list_cube_counts` 各格篇數的加總（AD-5：要分母時呼叫它，不自己數
    `paper`）；追蹤範圍外、被排除、還沒進已發佈快照的論文不在這個數字裡（ADR-0051「要 Kevin
    決定」第 1 項）。`active` 的定義由 `repo/ai_outputs.py` 決定。資料庫裡沒有的成員資料表不放進
    回傳的 dict（比對時算不一致）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    papers = sum(cell.n for cell in list_cube_counts(viewer, connection))
    vectors = connection.execute(
        text(f"SELECT count(*) FROM embedding WHERE {visible}")
    ).scalar_one()
    active = count_active_ai_results(viewer, connection)
    counts = BackupCounts(
        counted_paper=papers,
        zh_summary_active=active[LlmTask.ZH_SUMMARY],
        classification_active=active[LlmTask.CLASSIFY],
        research_model_active=active[LlmTask.EXTRACT_MODEL],
        embedding=int(vectors),
    )
    rows: dict[str, int] = {}
    for table in sorted(set(member_tables)):
        exists = connection.execute(
            text(f"SELECT to_regclass(:name) IS NOT NULL WHERE {visible}"),
            {"name": f'public."{table}"'},
        ).scalar_one()
        if exists:
            rows[table] = int(
                connection.execute(
                    text(f'SELECT count(*) FROM public."{table}" WHERE {visible}')
                ).scalar_one()
            )
    return counts, rows
