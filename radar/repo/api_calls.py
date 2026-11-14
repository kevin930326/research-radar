"""`api_call` 表：表的定義與唯一的寫入者（FR-10、AD-29、ADR-0021）。

外部 API 的每次 HTTP 嘗試（含重試）記一列：花了多少（`cost_usd`）、對方回報的剩餘與上限。
抓取時用來算今天的用量，到設定的比例就停下。

- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit（Conventions 的例外：
  services 用自己的短交易立刻 commit，抓取失敗也留得下紀錄）。
- 讀取函式第一個參數是 `viewer`，只給系統工作（`visible_to_system_only`）。
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    Index,
    Integer,
    Numeric,
    Table,
    Text,
    Uuid,
    text,
)

from radar.domain.raw_records import ApiCall, ApiUsage, RawSource
from radar.domain.viewer import SystemViewer
from radar.repo.models import Base
from radar.repo.visibility import visible_to_system_only

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0004 建表） ---------------------------

api_call_table = Table(
    "api_call",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("source", Text, nullable=False),
    Column("operation", Text, nullable=False),
    Column("http_status", Integer),
    Column("cost_usd", Numeric(12, 6), nullable=False),
    Column("remaining_usd", Numeric(12, 6)),
    Column("limit_usd", Numeric(12, 6)),
    Column("called_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Index("ix_api_call_source_called_at", "source", "called_at"),
    CheckConstraint("source IN ('openalex')", name="source"),
    CheckConstraint("operation ~ '^[a-z][a-z0-9_]*$'", name="operation_format"),
    CheckConstraint(
        "http_status IS NULL OR http_status BETWEEN 100 AND 599", name="http_status_range"
    ),
    CheckConstraint("cost_usd >= 0", name="cost_usd_not_negative"),
)


# --- 讀取 ---------------------------------------------------------------------------------


def get_api_usage_since(
    viewer: SystemViewer, connection: Connection, source: RawSource, since: datetime
) -> ApiUsage:
    """`since`（含）之後這個來源的用量（ADR-0021）：

    - `local_cost_usd`：本地 `cost_usd` 的總和。
    - `reported_used_usd`：最新一筆有回報的「上限 − 剩餘」，加上排在它之後（`called_at`、`id`
      的順序）每一筆的 `cost_usd`：之後的呼叫可能沒帶額度標頭，花費要接著累計，不能停在最後
      一次回報的數字。那一筆自己的花費已經算在對方回報的數字裡，不重複加。沒有回報時是 None。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)  # 不是 SystemViewer 就在碰連線之前丟錯
    row = (
        connection.execute(
            text(
                "WITH latest AS ("
                "   SELECT id, called_at, limit_usd - remaining_usd AS used FROM api_call"
                f"   WHERE {visible} AND source = :source AND called_at >= :since"
                "     AND limit_usd IS NOT NULL AND remaining_usd IS NOT NULL"
                "   ORDER BY called_at DESC, id DESC LIMIT 1"
                " )"
                " SELECT"
                " (SELECT COALESCE(sum(cost_usd), 0) FROM api_call"
                f"   WHERE {visible} AND source = :source AND called_at >= :since) AS local_cost,"
                " (SELECT latest.used + COALESCE(("
                "     SELECT sum(later.cost_usd) FROM api_call AS later"
                f"     WHERE {visible} AND later.source = :source"
                "       AND (later.called_at, later.id) > (latest.called_at, latest.id)"
                "   ), 0) FROM latest) AS reported_used"
            ),
            {"source": RawSource(source).value, "since": since},
        )
        .mappings()
        .one()
    )
    reported = row["reported_used"]
    return ApiUsage(
        local_cost_usd=Decimal(row["local_cost"]),
        reported_used_usd=None if reported is None else Decimal(reported),
    )


# --- 寫入 ---------------------------------------------------------------------------------


def create_api_calls(connection: Connection, calls: Sequence[ApiCall]) -> int:
    """每次 HTTP 嘗試記一列，回傳新增幾列。在呼叫端開好的交易裡做，不 commit。"""
    if not calls:
        return 0
    connection.execute(
        text(
            "INSERT INTO api_call"
            " (source, operation, http_status, cost_usd, remaining_usd, limit_usd, called_at)"
            " VALUES (:source, :operation, :http_status, :cost_usd, :remaining_usd, :limit_usd,"
            " :called_at)"
        ),
        [
            {
                "source": call.source.value,
                "operation": call.operation,
                "http_status": call.http_status,
                "cost_usd": call.cost_usd,
                "remaining_usd": call.remaining_usd,
                "limit_usd": call.limit_usd,
                "called_at": call.called_at,
            }
            for call in calls
        ],
    )
    return len(calls)
