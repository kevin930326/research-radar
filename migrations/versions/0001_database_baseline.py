"""資料庫的起點：檢查需要的功能，把權限給應用程式角色（ADR-0003 決定第 3 點、ADR-0018）。

不建任何業務資料表，也不建擴充套件（由管理者在建資料庫時建立）。
任何一項功能不符就丟 `DatabaseRequirementsError`，整個交易退回。

版本：0001
上一版：（無）
"""

from collections.abc import Sequence

import structlog
from alembic import op

from radar.domain.database_requirements import (
    APP_ROLE,
    DatabaseRequirementsError,
    find_missing_features,
)
from radar.repo.migrations import read_database_features

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = structlog.get_logger("migrations.0001_database_baseline")

# 擁有者（執行遷移的角色）之後建的表與序列，應用程式角色自動拿到的權限。
# 事件表不給 UPDATE／DELETE（AD-10）由建那兩張表的 Story 用 REVOKE 處理。
TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"
SEQUENCE_PRIVILEGES = "USAGE, SELECT"


def upgrade() -> None:
    features = read_database_features(op.get_bind())
    log.info("資料庫功能檢查", pgvector_version=features.pgvector_version)
    problems = find_missing_features(features)
    if problems:
        raise DatabaseRequirementsError(problems)

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    # alembic_version 在這個遷移之前就由 Alembic 建好，預設權限管不到，要另外給（只能讀）。
    op.execute(f"GRANT SELECT ON alembic_version TO {APP_ROLE}")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public"
        f" GRANT {TABLE_PRIVILEGES} ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public"
        f" GRANT {SEQUENCE_PRIVILEGES} ON SEQUENCES TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public"
        f" REVOKE {SEQUENCE_PRIVILEGES} ON SEQUENCES FROM {APP_ROLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public"
        f" REVOKE {TABLE_PRIVILEGES} ON TABLES FROM {APP_ROLE}"
    )
    op.execute(f"REVOKE SELECT ON alembic_version FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
