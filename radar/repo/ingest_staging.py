"""`ingest_staging` 表：表的定義與唯一的寫入者（ADR-0014「寫入分兩段」、ADR-0022）。

解析好的結果先放這裡、不動論文資料；最後一個交易再依 `id` 一批一批讀出來套用，套用完同一個
交易刪掉這一批。同一次執行的列共用一個 `batch_id`。

- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。
- 讀取函式第一個參數是 `viewer`，只給系統工作（`visible_to_system_only`）。
- `parsed` 存 `ParsedPaper` 的 JSON；讀出時重新驗證成 `ParsedPaper`（不信任表裡的內容）。
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    Index,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from radar.domain.papers import ParsedPaper
from radar.domain.staging import StagedPaper, StagingItem, StagingKind
from radar.domain.viewer import SystemViewer
from radar.repo.models import Base
from radar.repo.visibility import visible_to_system_only

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0005 建表） ---------------------------

ingest_staging_table = Table(
    "ingest_staging",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("kind", Text, nullable=False),
    Column("batch_id", Uuid, nullable=False),
    Column("source", Text, nullable=False),
    Column("external_key", Text, nullable=False),
    Column("source_hash", Text, nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("parsed", JSONB, nullable=False),
    Column("excluded_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("batch_id", "source", "external_key"),
    Index("ix_ingest_staging_kind", "kind"),
    CheckConstraint("kind IN ('reparse')", name="kind"),
    CheckConstraint("source IN ('openalex')", name="source"),
    CheckConstraint("external_key <> ''", name="external_key_not_empty"),
    CheckConstraint("source_hash ~ '^[0-9a-f]{64}$'", name="source_hash_format"),
    CheckConstraint(
        "excluded_reason IS NULL OR excluded_reason <> ''", name="excluded_reason_not_empty"
    ),
)


# --- 讀取 ---------------------------------------------------------------------------------


def list_staged_papers(
    viewer: SystemViewer,
    connection: Connection,
    batch_id: uuid.UUID,
    *,
    after_id: uuid.UUID | None,
    limit: int,
) -> list[StagedPaper]:
    """這一批 `id` 大於 `after_id`（None＝從頭）的暫存列，依 `id` 排序、最多 `limit` 筆。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: `limit` 不是正整數。
        pydantic.ValidationError: 表裡的 `parsed` 不是合法的 `ParsedPaper`。
    """
    visible = visible_to_system_only(viewer)  # 不是 SystemViewer 就在碰連線之前丟錯
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit 要是正整數")
    parameters: dict[str, object] = {"batch_id": batch_id, "limit": limit}
    after = ""
    if after_id is not None:
        after = " AND id > :after_id"
        parameters["after_id"] = after_id
    rows = connection.execute(
        text(
            "SELECT id, external_key, parsed, excluded_reason FROM ingest_staging"
            f" WHERE {visible} AND batch_id = :batch_id{after}"
            " ORDER BY id LIMIT :limit"
        ),
        parameters,
    ).mappings()
    return [
        StagedPaper(
            id=row["id"],
            external_key=row["external_key"],
            paper=ParsedPaper.model_validate(row["parsed"]),
            excluded_reason=row["excluded_reason"],
        )
        for row in rows
    ]


# --- 寫入 ---------------------------------------------------------------------------------


def create_staged_papers(
    connection: Connection,
    *,
    batch_id: uuid.UUID,
    kind: StagingKind,
    items: Sequence[StagingItem],
) -> int:
    """把 `items` 放進這一批的暫存表；回傳新增幾列。在呼叫端開好的交易裡做，不 commit。

    Raises:
        ValueError: `kind` 不是 `StagingKind`，或 `items` 有不是 `StagingItem` 的（不碰資料庫）。
    """
    kind = StagingKind(kind)
    if not all(isinstance(item, StagingItem) for item in items):
        raise ValueError("items 只能放 StagingItem")
    if not items:
        return 0
    rows = connection.execute(
        text(
            "INSERT INTO ingest_staging"
            " (kind, batch_id, source, external_key, source_hash, fetched_at, parsed,"
            " excluded_reason)"
            " SELECT :kind, :batch_id, * FROM unnest("
            "CAST(:sources AS text[]), CAST(:external_keys AS text[]),"
            " CAST(:source_hashes AS text[]), CAST(:fetched_ats AS timestamptz[]),"
            " CAST(CAST(:parsed AS text[]) AS jsonb[]), CAST(:excluded_reasons AS text[]))"
            " RETURNING id"
        ),
        {
            "kind": kind.value,
            "batch_id": batch_id,
            "sources": [item.paper.source for item in items],
            "external_keys": [item.external_key for item in items],
            "source_hashes": [item.paper.source_hash for item in items],
            "fetched_ats": [item.paper.fetched_at for item in items],
            "parsed": [item.paper.model_dump_json() for item in items],
            "excluded_reasons": [item.excluded_reason for item in items],
        },
    )
    return len(rows.all())


def delete_staged_papers(connection: Connection, batch_id: uuid.UUID) -> int:
    """刪掉這一批的暫存列；回傳刪了幾列。在呼叫端開好的交易裡做，不 commit。"""
    rows = connection.execute(
        text("DELETE FROM ingest_staging WHERE batch_id = :batch_id RETURNING id"),
        {"batch_id": batch_id},
    )
    return len(rows.all())


def delete_staged_papers_of_kind(connection: Connection, kind: StagingKind) -> int:
    """刪掉這種工作留下的全部暫存列（例如上一次沒做完的 reparse）；回傳刪了幾列。"""
    rows = connection.execute(
        text("DELETE FROM ingest_staging WHERE kind = :kind RETURNING id"),
        {"kind": StagingKind(kind).value},
    )
    return len(rows.all())
