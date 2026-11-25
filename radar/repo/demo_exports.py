"""`demo_export` 表：示範站匯出的狀態列，表的定義與唯一的寫入者（Story 3.7、AD-14、AD-20、AR-17）。

- 一列＝一次匯出：屬於哪個已發佈的快照、這個快照的第幾次（`sequence`，匯出編號＝
  `<快照編號>-<sequence>`）、狀態（`queued` → `running` → `succeeded`／`failed`）。
- 同一個快照同時只能有一筆 `queued` 或 `running`：部分唯一索引擋下第二筆（AD-14 第 267 行）。
- 停在 `queued`、`running` 太久的由 sweep 重新排入（`domain/sweep.SWEEP_REGISTRY`）；匯出工作最後
  一定把列改成 `succeeded` 或 `failed`（移出起始狀態）。
- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。讀取只給系統工作。
"""

import uuid
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    RowMapping,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from radar.domain.demo_export import (
    DemoExportBusyError,
    DemoExportFailure,
    DemoExportRun,
    DemoExportStatus,
)
from radar.domain.snapshot import SnapshotStatus
from radar.domain.viewer import SystemViewer
from radar.repo.models import Base
from radar.repo.visibility import visible_to_system_only

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0022 建表） ---------------------------

demo_export_table = Table(
    "demo_export",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("snapshot_id", Uuid, ForeignKey("snapshot.id"), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("failure_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    # 同一個快照的序號不重複（也當 `snapshot_id` 外鍵的索引）。
    UniqueConstraint("snapshot_id", "sequence"),
    Index(
        "uq_demo_export_snapshot_id_in_progress",
        "snapshot_id",
        unique=True,
        postgresql_where=text("status IN ('queued', 'running')"),
    ),
    CheckConstraint("sequence >= 1", name="sequence_positive"),
    CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="status"),
    CheckConstraint(
        "(status = 'failed') = (failure_reason IS NOT NULL)", name="failure_reason_iff_failed"
    ),
    CheckConstraint(
        "status NOT IN ('succeeded', 'failed') OR finished_at IS NOT NULL",
        name="finished_at_once_finished",
    ),
)

_RETURNING: Final = "id, snapshot_id, sequence, status"


def _run(row: RowMapping, snapshot_number: int) -> DemoExportRun:
    return DemoExportRun(
        id=row["id"],
        snapshot_id=row["snapshot_id"],
        snapshot_number=snapshot_number,
        sequence=row["sequence"],
        status=DemoExportStatus(row["status"]),
    )


# --- 讀取 ---------------------------------------------------------------------------------


def find_demo_export(
    viewer: SystemViewer, connection: Connection, demo_export_id: uuid.UUID
) -> DemoExportRun | None:
    """這一筆匯出（含快照編號）；沒有就回 None。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)  # 不是 SystemViewer 就在碰連線之前丟錯
    row = (
        connection.execute(
            text(
                "SELECT e.id, e.snapshot_id, e.sequence, e.status, s.number AS snapshot_number"
                " FROM demo_export AS e JOIN snapshot AS s ON s.id = e.snapshot_id"
                f" WHERE {visible} AND e.id = :id"
            ),
            {"id": demo_export_id},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _run(row, row["snapshot_number"])


# --- 寫入 ---------------------------------------------------------------------------------


def create_demo_export(connection: Connection) -> DemoExportRun | None:
    """為最新已發佈的快照新增一筆 `queued` 的匯出（`sequence`＝這個快照目前最大值＋1）；還沒有已發佈
    的快照時不新增、回 None。在呼叫端開好的交易裡做，不 commit。

    Raises:
        DemoExportBusyError: 這個快照已經有 `queued`／`running` 的一筆（部分唯一索引），或並行的
            另一個交易剛好拿到同一個序號（`ON CONFLICT DO NOTHING`，沒有新增任何一列）。
    """
    row = (
        connection.execute(
            text(
                "WITH latest AS (SELECT id, number FROM snapshot WHERE status = :published"
                " ORDER BY number DESC LIMIT 1),"
                " inserted AS (INSERT INTO demo_export (snapshot_id, sequence, status)"
                " SELECT latest.id, COALESCE((SELECT max(e.sequence) FROM demo_export AS e"
                " WHERE e.snapshot_id = latest.id), 0) + 1, :queued FROM latest"
                f" ON CONFLICT DO NOTHING RETURNING {_RETURNING})"
                " SELECT inserted.id, inserted.snapshot_id, inserted.sequence, inserted.status,"
                " latest.number AS snapshot_number"
                " FROM latest LEFT JOIN inserted ON inserted.snapshot_id = latest.id"
            ),
            {
                "published": SnapshotStatus.PUBLISHED.value,
                "queued": DemoExportStatus.QUEUED.value,
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if row["id"] is None:
        raise DemoExportBusyError("最新的快照已經有一筆排隊中或執行中的匯出")
    return _run(row, row["snapshot_number"])


def update_demo_export_running(
    connection: Connection, demo_export_id: uuid.UUID
) -> DemoExportRun | None:
    """把 `queued`（或卡在 `running`、由 sweep 補排的）那一筆改成 `running`，記開始時間；已經結束或
    不存在時不改、回 None。在呼叫端開好的交易裡做，不 commit。"""
    row = (
        connection.execute(
            text(
                "UPDATE demo_export AS e SET status = :running, started_at = now(),"
                " updated_at = now()"
                " FROM snapshot AS s"
                " WHERE e.id = :id AND s.id = e.snapshot_id AND e.status IN (:queued, :running)"
                " RETURNING e.id, e.snapshot_id, e.sequence, e.status, s.number AS snapshot_number"
            ),
            {
                "id": demo_export_id,
                "queued": DemoExportStatus.QUEUED.value,
                "running": DemoExportStatus.RUNNING.value,
            },
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _run(row, row["snapshot_number"])


def update_demo_export_finished(
    connection: Connection,
    demo_export_id: uuid.UUID,
    *,
    failure: DemoExportFailure | None,
) -> bool:
    """把還沒結束（`queued`／`running`）的那一筆改成 `succeeded`（`failure` 是 None）或 `failed`
    （記原因代碼），記結束時間；回傳有沒有改到。在呼叫端開好的交易裡做，不 commit。"""
    status = DemoExportStatus.SUCCEEDED if failure is None else DemoExportStatus.FAILED
    result = connection.execute(
        text(
            "UPDATE demo_export SET status = :status, failure_reason = :reason,"
            " finished_at = now(), updated_at = now()"
            " WHERE id = :id AND status IN (:queued, :running)"
        ),
        {
            "id": demo_export_id,
            "status": status.value,
            "reason": None if failure is None else failure.value,
            "queued": DemoExportStatus.QUEUED.value,
            "running": DemoExportStatus.RUNNING.value,
        },
    )
    return result.rowcount == 1
