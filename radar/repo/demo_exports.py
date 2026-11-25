"""`demo_export` 表：示範站匯出的狀態列，表的定義與唯一的寫入者（Story 3.7、AD-14、AD-20、AR-17）。

- 一列＝一次匯出：屬於哪個已發佈的快照、這個快照的第幾次（`sequence`，匯出編號＝
  `<快照編號>-<sequence>`）、狀態（`queued` → `running` → `succeeded`／`failed`）。
- 同一個快照同時只能有一筆 `queued` 或 `running`：部分唯一索引擋下第二筆（AD-14 第 267 行）。
- 停在 `queued`、`running` 太久的由 sweep 重新排入（`domain/sweep.SWEEP_REGISTRY`）；匯出工作最後
  一定把列改成 `succeeded` 或 `failed`（移出起始狀態）。
- **接手要有擁有者**（`run_token`）：`update_demo_export_claimed` 只接手 `queued`，或卡在
  `running` 超過門檻的那一筆，每次接手換一個新的代號；寫檔前（`update_demo_export_if_owned`）與
  結束時（`update_demo_export_finished`）都要拿著同一個代號，被別的行程接手後舊的那個什麼都不能寫。
- **發佈鎖**（`DEMO_PUBLISH_LOCK_KEY`）：CLI 與 worker 寫 `preview/` 與換 manifest 前都在同一個
  短交易裡拿這把交易級 advisory lock（`Database.transaction_lock`），一次只有一個在換。
- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。
"""

import uuid
from datetime import timedelta
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
from radar.domain.fingerprints import named_lock_key
from radar.domain.snapshot import SnapshotStatus
from radar.repo.models import Base

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
    # 接手執行的那一次的代號（`running` 一定有；結束後留著，看得出最後是誰做的）。
    Column("run_token", Uuid),
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
    CheckConstraint("status <> 'running' OR run_token IS NOT NULL", name="run_token_when_running"),
)

_RETURNING: Final = "e.id, e.snapshot_id, e.sequence, e.status, e.run_token"

# 發佈鎖（F3、ADR-0061）：寫 `preview/<匯出編號>/` 與換 `manifest.json` 的那一段，CLI 與 worker 都在
# 一個短交易裡先拿這把獨佔交易級 advisory lock（`Database.transaction_lock`，會等；交易結束
# 自動放）。
DEMO_PUBLISH_LOCK_KEY: Final = named_lock_key("demo_publish")


def _run(row: RowMapping) -> DemoExportRun:
    return DemoExportRun(
        id=row["id"],
        snapshot_id=row["snapshot_id"],
        snapshot_number=row["snapshot_number"],
        sequence=row["sequence"],
        status=DemoExportStatus(row["status"]),
        run_token=row["run_token"],
    )


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
                " inserted AS (INSERT INTO demo_export AS e (snapshot_id, sequence, status)"
                " SELECT latest.id, COALESCE((SELECT max(x.sequence) FROM demo_export AS x"
                " WHERE x.snapshot_id = latest.id), 0) + 1, :queued FROM latest"
                f" ON CONFLICT DO NOTHING RETURNING {_RETURNING})"
                " SELECT inserted.id, inserted.snapshot_id, inserted.sequence, inserted.status,"
                " inserted.run_token, latest.number AS snapshot_number"
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
    return _run(row)


def update_demo_export_claimed(
    connection: Connection, demo_export_id: uuid.UUID, *, running_stale_after: timedelta
) -> DemoExportRun | None:
    """接手那一筆匯出：`queued` 的，或 `running` 但 `updated_at` 已經超過 `running_stale_after`
    （做的行程不在了，由 sweep 補排）的，改成 `running`、換一個新的 `run_token`、記開始時間。
    已經結束、不存在，或正由別的行程在做（`running` 還沒超過門檻）時不改、回 None。在呼叫端開好的
    交易裡做，不 commit。

    Raises:
        ValueError: `running_stale_after` 不是正的時間長度。
    """
    if not isinstance(running_stale_after, timedelta) or running_stale_after <= timedelta(0):
        raise ValueError("running_stale_after 要是正的時間長度")
    row = (
        connection.execute(
            text(
                "UPDATE demo_export AS e SET status = :running, run_token = :token,"
                " started_at = now(), updated_at = now()"
                " FROM snapshot AS s"
                " WHERE e.id = :id AND s.id = e.snapshot_id AND (e.status = :queued"
                " OR (e.status = :running"
                " AND e.updated_at < now() - make_interval(secs => :stale)))"
                f" RETURNING {_RETURNING}, s.number AS snapshot_number"
            ),
            {
                "id": demo_export_id,
                "token": uuid.uuid4(),
                "queued": DemoExportStatus.QUEUED.value,
                "running": DemoExportStatus.RUNNING.value,
                "stale": running_stale_after.total_seconds(),
            },
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _run(row)


def update_demo_export_if_owned(
    connection: Connection, demo_export_id: uuid.UUID, run_token: uuid.UUID
) -> bool:
    """那一筆還是 `running`、而且是 `run_token` 這一次接手的：更新 `updated_at`（同時鎖住那一列，
    交易結束前別人不能接手或結束它）、回 True；已經被別的行程接手或已經結束時不改、回 False。
    不 commit。"""
    result = connection.execute(
        text(
            "UPDATE demo_export SET updated_at = now()"
            " WHERE id = :id AND status = :running AND run_token = :token"
        ),
        {"id": demo_export_id, "running": DemoExportStatus.RUNNING.value, "token": run_token},
    )
    return result.rowcount == 1


def update_demo_export_finished(
    connection: Connection,
    demo_export_id: uuid.UUID,
    *,
    run_token: uuid.UUID,
    failure: DemoExportFailure | None,
) -> bool:
    """把 `run_token` 這一次接手、還在 `running` 的那一筆改成 `succeeded`（`failure` 是 None）或
    `failed`（記原因代碼），記結束時間；回傳有沒有改到（被別的行程接手或已經結束時改不到）。在
    呼叫端開好的交易裡做，不 commit。"""
    status = DemoExportStatus.SUCCEEDED if failure is None else DemoExportStatus.FAILED
    result = connection.execute(
        text(
            "UPDATE demo_export SET status = :status, failure_reason = :reason,"
            " finished_at = now(), updated_at = now()"
            " WHERE id = :id AND status = :running AND run_token = :token"
        ),
        {
            "id": demo_export_id,
            "status": status.value,
            "reason": None if failure is None else failure.value,
            "running": DemoExportStatus.RUNNING.value,
            "token": run_token,
        },
    )
    return result.rowcount == 1
