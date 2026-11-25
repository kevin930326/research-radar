"""`snapshot` 表：表的定義與唯一的寫入者（AD-19、ADR-0011「快照」、ADR-0026）。

- 快照只由 `services/weekly_update.py` 的套用交易建立與發佈（架構測試守著：
  `create_snapshot`、`update_snapshot_published` 只出現在這裡與那個 service）。
- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。
- 讀取函式第一個參數是 `viewer`，只給系統工作（`visible_to_system_only`）；最新已發佈的快照另外給
  示範站匯出（`PublicViewer`，`visible_snapshot_facts`）。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Integer,
    RowMapping,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from radar.domain.snapshot import Snapshot, SnapshotStatus
from radar.domain.viewer import PublicViewer, SystemViewer
from radar.repo.models import Base
from radar.repo.visibility import visible_snapshot_facts, visible_to_system_only

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0007 建表） ---------------------------

# 外鍵 `weekly_run_id` 的索引由它的唯一約束提供。
snapshot_table = Table(
    "snapshot",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("number", Integer, nullable=False),
    Column("weekly_run_id", Uuid, ForeignKey("weekly_run.id"), nullable=False),
    Column("status", Text, nullable=False),
    Column("as_of", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("number"),
    UniqueConstraint("weekly_run_id"),
    CheckConstraint("number >= 1", name="number_positive"),
    CheckConstraint("status IN ('building', 'published', 'failed')", name="status"),
    CheckConstraint("status <> 'published' OR as_of IS NOT NULL", name="published_has_as_of"),
)

_COLUMNS = "id, number, weekly_run_id, status, as_of, created_at"


# --- 讀取 ---------------------------------------------------------------------------------


def get_snapshot_of_weekly_run(
    viewer: SystemViewer, connection: Connection, weekly_run_id: uuid.UUID
) -> Snapshot | None:
    """這一次每週更新的快照；還沒有（還沒套用，或套用失敗整個退回）就回 None。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)  # 不是 SystemViewer 就在碰連線之前丟錯
    row = (
        connection.execute(
            text(
                f"SELECT {_COLUMNS} FROM snapshot"
                f" WHERE {visible} AND weekly_run_id = :weekly_run_id"
            ),
            {"weekly_run_id": weekly_run_id},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _snapshot(row)


def find_latest_published_snapshot(
    viewer: SystemViewer | PublicViewer, connection: Connection
) -> Snapshot | None:
    """目前已發佈的快照（`published` 裡 `number` 最大的一筆）；一個都還沒發佈就回 None。

    示範站匯出用 `PublicViewer` 讀（Story 3.7，AD-20「只用 PublicViewer 經 repo 讀」）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer` 或 `PublicViewer`。
    """
    visible = visible_snapshot_facts(viewer)  # 身分不對就在碰連線之前丟錯
    row = (
        connection.execute(
            text(
                f"SELECT {_COLUMNS} FROM snapshot"
                f" WHERE {visible} AND status = :published"
                " ORDER BY number DESC LIMIT 1"
            ),
            {"published": SnapshotStatus.PUBLISHED.value},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _snapshot(row)


def _snapshot(row: RowMapping) -> Snapshot:
    return Snapshot(
        id=row["id"],
        number=row["number"],
        weekly_run_id=row["weekly_run_id"],
        status=SnapshotStatus(row["status"]),
        as_of=row["as_of"],
        created_at=row["created_at"],
    )


# --- 寫入 ---------------------------------------------------------------------------------


def create_snapshot(connection: Connection, weekly_run_id: uuid.UUID) -> tuple[uuid.UUID, int]:
    """新增這一次每週更新的快照（`building`，`number`＝目前最大值＋1），回傳（id, number）。

    在呼叫端開好的交易裡做，不 commit。同一筆 `weekly_run` 已經有快照、或並行的另一個交易剛好
    拿到同一個編號時，唯一約束擋下（丟 `IntegrityError`），整個交易退回。
    """
    row = connection.execute(
        text(
            "INSERT INTO snapshot (number, weekly_run_id, status)"
            " SELECT COALESCE(max(number), 0) + 1, :weekly_run_id, :status FROM snapshot"
            " RETURNING id, number"
        ),
        {"weekly_run_id": weekly_run_id, "status": SnapshotStatus.BUILDING.value},
    ).one()
    return uuid.UUID(str(row[0])), int(row[1])


def update_snapshot_published(connection: Connection, snapshot_id: uuid.UUID) -> datetime:
    """把建立中的快照改成 `published`，`as_of` 是現在（`clock_timestamp()`，不是交易開始的時間）；
    回傳 `as_of`。

    在呼叫端開好的交易裡做，不 commit。

    Raises:
        RuntimeError: 沒有這個快照，或它不是建立中。
    """
    as_of: datetime | None = connection.execute(
        text(
            "UPDATE snapshot SET status = :published, as_of = clock_timestamp(), updated_at = now()"
            " WHERE id = :id AND status = :building"
            " RETURNING as_of"
        ),
        {
            "id": snapshot_id,
            "published": SnapshotStatus.PUBLISHED.value,
            "building": SnapshotStatus.BUILDING.value,
        },
    ).scalar_one_or_none()
    if as_of is None:
        raise RuntimeError("找不到建立中的這個快照，沒有發佈")
    return as_of
