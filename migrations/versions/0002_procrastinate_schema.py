"""背景工作佇列（Procrastinate 3.11.0）的資料表、型別與函式（ADR-0004、ADR-0019）。

執行凍結在 `migrations/procrastinate/schema-3.11.0.sql` 的 SQL，不 import procrastinate：
套件升版後 schema.sql 會變，舊的遷移若讀套件的檔，在新資料庫上會建出新版結構。
不建擴充套件：檔頭的 `CREATE EXTENSION IF NOT EXISTS plpgsql` 遇到已經存在的 plpgsql
（PostgreSQL 內建）只發 NOTICE。新建的表與序列由 0001 設的預設權限交給 `radar_app`；
函式與型別預設就開放給所有角色使用。

版本：0002
上一版：0001
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "procrastinate" / "schema-3.11.0.sql"

# 套件的 schema.sql 建的四張表與三個型別（函式用名稱前綴整批刪）。
TABLES = (
    "procrastinate_events",
    "procrastinate_periodic_defers",
    "procrastinate_jobs",
    "procrastinate_workers",
)
TYPES = (
    "procrastinate_job_to_defer_v1",
    "procrastinate_job_event_type",
    "procrastinate_job_status",
)

# 刪掉 public 裡名稱以 procrastinate_ 開頭、符合 {condition} 的函式與程序。
DROP_ROUTINES = r"""
DO $$
DECLARE
    routine regprocedure;
BEGIN
    FOR routine IN
        SELECT p.oid::regprocedure
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname LIKE 'procrastinate\_%' AND {condition}
    LOOP
        EXECUTE format('DROP ROUTINE %s', routine);
    END LOOP;
END;
$$
"""


def _run_script(sql: str) -> None:
    """原樣送出一段（可能有多個敘述的）SQL。

    `no_parameters`：不經過參數代換，SQL 裡的 `%`（例如 RAISE 的訊息格式）照原樣送出，
    驅動也才會用可以一次送多個敘述的方式執行。
    """
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})


def upgrade() -> None:
    _run_script(SCHEMA_FILE.read_text(encoding="utf-8"))


def downgrade() -> None:
    # 不用 CASCADE，照依賴順序刪：一般函式（有的回傳表的列型別）→ 表（觸發器跟著刪）
    # → 觸發器函式 → 型別（表與函式都用到它們）。
    trigger = "p.prorettype = 'trigger'::regtype"
    _run_script(DROP_ROUTINES.format(condition=f"NOT ({trigger})"))
    _run_script(f"DROP TABLE {', '.join(TABLES)}")
    _run_script(DROP_ROUTINES.format(condition=trigger))
    _run_script(f"DROP TYPE {', '.join(TYPES)}")
