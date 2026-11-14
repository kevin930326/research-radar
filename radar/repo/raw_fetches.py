"""`raw_fetch` 表：表的定義與唯一的寫入者（AD-25、ADR-0014、ADR-0021）。

每抓到一篇記一列（來源、外部鍵、內容的 SHA-256、抓取時間）；原始內容本身在物件儲存的
`raw/<來源>/<sha256>.json.gz`。內容沒變時物件不重存，只多一列抓取時間。

- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。
- 讀取函式第一個參數是 `viewer`，只給系統工作（`visible_to_system_only`）。
"""

from collections.abc import Iterable, Sequence

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

from radar.domain.raw_records import RawFetchRef, RawRecord, RawSource
from radar.domain.viewer import SystemViewer
from radar.repo.models import Base
from radar.repo.visibility import visible_to_system_only

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0004 建表） ---------------------------

raw_fetch_table = Table(
    "raw_fetch",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("source", Text, nullable=False),
    Column("external_key", Text, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("source", "external_key", "fetched_at"),
    Index("ix_raw_fetch_source_sha256", "source", "sha256"),
    CheckConstraint("source IN ('openalex')", name="source"),
    CheckConstraint("external_key <> ''", name="external_key_not_empty"),
    CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
)


# --- 讀取 ---------------------------------------------------------------------------------


def find_stored_sha256s(
    viewer: SystemViewer, connection: Connection, source: RawSource, sha256s: Iterable[str]
) -> frozenset[str]:
    """`sha256s` 裡已經有 `raw_fetch` 列的那些（代表物件已經存過）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)  # 不是 SystemViewer 就在碰連線之前丟錯
    wanted = sorted(set(sha256s))
    if not wanted:
        return frozenset()
    rows = connection.execute(
        text(
            f"SELECT DISTINCT sha256 FROM raw_fetch"
            f" WHERE {visible} AND source = :source AND sha256 = ANY(:sha256s)"
        ),
        {"source": RawSource(source).value, "sha256s": wanted},
    )
    return frozenset(str(value) for value in rows.scalars())


def list_latest_raw_fetches(
    viewer: SystemViewer,
    connection: Connection,
    source: RawSource,
    *,
    after_external_key: str | None,
    limit: int,
) -> list[RawFetchRef]:
    """每個外部鍵最新（抓取時間最晚）的那一列，依外部鍵排序；只取外部鍵大於
    `after_external_key`（None＝從頭）的，最多 `limit` 個外部鍵（keyset 分頁）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: `limit` 不是正整數。
    """
    visible = visible_to_system_only(viewer)  # 不是 SystemViewer 就在碰連線之前丟錯
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit 要是正整數")
    parameters: dict[str, object] = {"source": RawSource(source).value, "limit": limit}
    after = ""
    if after_external_key is not None:
        after = " AND external_key > :after"
        parameters["after"] = after_external_key
    rows = connection.execute(
        text(
            "SELECT DISTINCT ON (external_key) external_key, sha256, fetched_at FROM raw_fetch"
            f" WHERE {visible} AND source = :source{after}"
            " ORDER BY external_key, fetched_at DESC"
            " LIMIT :limit"
        ),
        parameters,
    )
    return [
        RawFetchRef(external_key=external_key, sha256=sha256, fetched_at=fetched_at)
        for external_key, sha256, fetched_at in rows
    ]


# --- 寫入 ---------------------------------------------------------------------------------


def create_raw_fetches(connection: Connection, records: Sequence[RawRecord]) -> int:
    """每筆原始紀錄記一列；同一個（來源, 外部鍵, 抓取時間）已經有了就略過。回傳新增幾列。

    在呼叫端開好的交易裡做，不 commit。
    """
    if not records:
        return 0
    rows = connection.execute(
        text(
            "INSERT INTO raw_fetch (source, external_key, sha256, fetched_at)"
            " SELECT * FROM unnest("
            "CAST(:sources AS text[]), CAST(:external_keys AS text[]),"
            " CAST(:sha256s AS text[]), CAST(:fetched_ats AS timestamptz[]))"
            " ON CONFLICT (source, external_key, fetched_at) DO NOTHING"
            " RETURNING id"
        ),
        {
            "sources": [record.source.value for record in records],
            "external_keys": [record.external_key for record in records],
            "sha256s": [record.sha256 for record in records],
            "fetched_ats": [record.fetched_at for record in records],
        },
    )
    return len(rows.all())
