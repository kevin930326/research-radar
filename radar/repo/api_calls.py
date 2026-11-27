"""`api_call` 表：表的定義與唯一的寫入者（FR-10、AD-29、ADR-0021）。

外部 API 的每次 HTTP 嘗試（含重試）記一列：花了多少（`cost_usd`）、對方回報的剩餘與上限。
抓取時用來算今天的用量，到設定的比例就停下。
向量化（`workers_ai`，Story 2.8、ADR-0040）每次 HTTP 嘗試（含 adapter 內部的重試）記一列：
Workers AI 不回報用量，記自己估的輸入 token（`estimated_input_tokens`），`cost_usd` 記 0
（免費額度內，到設定的比例就停）。

向量化的額度占用（`embedding_quota_hold`）：每次嘗試前在短交易裡拿額度鎖
（`repo/db.EMBEDDING_QUOTA_LOCK_KEY`），把過期的占用轉成用量（`delete_expired_embedding_quota_holds`），讀
「今天已用＋占用中」（`get_embedding_quota_usage`，同一句、同一個快照），放得下才新增占用
（`create_embedding_quota_hold`）；嘗試後把占用換成那次嘗試的 `api_call`
（`delete_embedding_quota_hold`，同一個交易）。所以另一個 worker 還沒回應的請求也算在帳上。

- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit（Conventions 的例外：
  services 用自己的短交易立刻 commit，抓取失敗也留得下紀錄）。
- 讀取函式第一個參數是 `viewer`，只給系統工作（`visible_to_system_only`）。
"""

import uuid
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

from radar.domain.embeddings import EMBED_DOCUMENTS_OPERATION, WORKERS_AI_SOURCE, EmbeddingApiCall
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
    # 向量化（`workers_ai`）自己估的輸入 token：Workers AI 的回應沒有用量欄位
    # （遷移 0015、ADR-0040）。
    Column("estimated_input_tokens", Integer),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # 查詢向量（`embed_query`）每次嘗試的結果代碼（遷移 0023、Story 4.1）：分得出「額度用完」
    # （沒有送出）和「連不上」。背景向量化與 OpenAlex 的列是 NULL。
    Column("outcome", Text),
    Index("ix_api_call_source_called_at", "source", "called_at"),
    CheckConstraint("source IN ('openalex', 'workers_ai')", name="source"),
    CheckConstraint("operation ~ '^[a-z][a-z0-9_]*$'", name="operation_format"),
    CheckConstraint(
        "http_status IS NULL OR http_status BETWEEN 100 AND 599", name="http_status_range"
    ),
    CheckConstraint("cost_usd >= 0", name="cost_usd_not_negative"),
    CheckConstraint(
        "estimated_input_tokens IS NULL OR estimated_input_tokens >= 0",
        name="estimated_input_tokens_not_negative",
    ),
    CheckConstraint(
        "(source = 'workers_ai') = (estimated_input_tokens IS NOT NULL)",
        name="estimated_input_tokens_iff_workers_ai",
    ),
    CheckConstraint(
        "outcome IS NULL OR outcome IN ('succeeded', 'unreachable', 'rejected', 'quota_exhausted')",
        name="outcome",
    ),
)


# 向量化的額度占用（遷移 0015、ADR-0040）：一次 Workers AI 嘗試送出前占住的估計 token。嘗試結束
# 就刪掉並換成那次嘗試的 `api_call`；行程中斷留下的占用過了 `expires_at` 由下一次占用轉成用量
# （當作已計費，記在占用當天）。
embedding_quota_hold_table = Table(
    "embedding_quota_hold",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("estimated_input_tokens", Integer, nullable=False),
    Column("reserved_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Index("ix_embedding_quota_hold_expires_at", "expires_at"),
    CheckConstraint("estimated_input_tokens >= 0", name="estimated_input_tokens_not_negative"),
    CheckConstraint("expires_at > reserved_at", name="expires_after_reserved"),
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


def get_embedding_quota_usage(
    viewer: SystemViewer, connection: Connection, since: datetime
) -> tuple[int, int]:
    """向量化的額度用量（Story 2.8、ADR-0040）：回（已用, 占用中）。

    - 已用：`since`（含）之後 `workers_ai` 的 `api_call` 記的估計輸入 token 總和（失敗的嘗試也算）。
    - 占用中：`embedding_quota_hold` 全部的估計 token（還沒回應的嘗試；呼叫前先轉掉過期的）。

    兩個數字在同一句 SQL 裡讀（同一個快照）：另一個 worker 在中間把占用換成 `api_call` 時，不會
    兩邊都沒算到。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    row = connection.execute(
        text(
            "SELECT"
            " (SELECT COALESCE(sum(estimated_input_tokens), 0) FROM api_call"
            f"   WHERE {visible} AND source = :source AND called_at >= :since) AS used,"
            " (SELECT COALESCE(sum(estimated_input_tokens), 0) FROM embedding_quota_hold"
            f"   WHERE {visible}) AS held"
        ),
        {"source": WORKERS_AI_SOURCE, "since": since},
    ).one()
    return int(row.used), int(row.held)


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


def create_embedding_api_call(connection: Connection, call: EmbeddingApiCall) -> None:
    """記一次向量化請求（`source`＝`workers_ai`、`cost_usd`＝0、估計的輸入 token；查詢向量另記
    結果代碼 `outcome`）。

    在呼叫端開好的交易裡做，不 commit（services 用自己的短交易立刻 commit）。
    """
    connection.execute(
        text(
            "INSERT INTO api_call"
            " (source, operation, http_status, cost_usd, estimated_input_tokens, called_at,"
            " outcome)"
            " VALUES (:source, :operation, :http_status, 0, :estimated_input_tokens, :called_at,"
            " :outcome)"
        ),
        {
            "source": WORKERS_AI_SOURCE,
            "operation": call.operation,
            "http_status": call.http_status,
            "estimated_input_tokens": call.estimated_input_tokens,
            "called_at": call.called_at,
            "outcome": None if call.outcome is None else call.outcome.value,
        },
    )


def delete_expired_embedding_quota_holds(connection: Connection, now: datetime) -> int:
    """把 `expires_at` 已到（≤ `now`）的占用刪掉、各換成一列 `api_call`，回換了幾列。

    占用過期代表那次嘗試的行程沒有回來記帳（被砍、資料庫斷線）：不知道對方有沒有算 neurons，
    照占用的估計 token 記成用量（`http_status` 是 NULL、`called_at` 是占用的時間），保守。
    在呼叫端開好的交易裡做（要先拿額度鎖 `repo/db.EMBEDDING_QUOTA_LOCK_KEY`），不 commit。
    """
    result = connection.execute(
        text(
            "WITH expired AS ("
            "   DELETE FROM embedding_quota_hold WHERE expires_at <= :now"
            "   RETURNING estimated_input_tokens, reserved_at"
            " )"
            " INSERT INTO api_call"
            " (source, operation, http_status, cost_usd, estimated_input_tokens, called_at)"
            " SELECT :source, :operation, NULL, 0, estimated_input_tokens, reserved_at FROM expired"
        ),
        {"now": now, "source": WORKERS_AI_SOURCE, "operation": EMBED_DOCUMENTS_OPERATION},
    )
    return int(result.rowcount)


def create_embedding_quota_hold(
    connection: Connection, tokens: int, *, reserved_at: datetime, expires_at: datetime
) -> uuid.UUID:
    """新增一筆占用（`tokens` 個估計 token），回它的 id。在呼叫端開好的交易裡做，不 commit。"""
    value = connection.execute(
        text(
            "INSERT INTO embedding_quota_hold (estimated_input_tokens, reserved_at, expires_at)"
            " VALUES (:tokens, :reserved_at, :expires_at) RETURNING id"
        ),
        {"tokens": tokens, "reserved_at": reserved_at, "expires_at": expires_at},
    ).scalar_one()
    return uuid.UUID(str(value))


def delete_embedding_quota_hold(
    connection: Connection, hold_id: uuid.UUID, call: EmbeddingApiCall | None
) -> bool:
    """結清一筆占用：刪掉它，`call` 不是 None 時在同一個交易裡記成 `api_call`。

    占用已經不在（過期、被別的 worker 換成用量了）時什麼都不寫、回 False：那筆用量已經照占用的
    估計記過，不重複記。在呼叫端開好的交易裡做，不 commit。
    """
    deleted = connection.execute(
        text("DELETE FROM embedding_quota_hold WHERE id = :id RETURNING id"), {"id": hold_id}
    ).one_or_none()
    if deleted is None:
        return False
    if call is not None:
        create_embedding_api_call(connection, call)
    return True
