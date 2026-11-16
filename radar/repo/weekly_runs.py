"""`weekly_run` 與 `weekly_run_journal` 表：表的定義與唯一的寫入者（AD-14、ADR-0004、ADR-0025、
ADR-0026）。

- `weekly_run`：一次每週更新一列；同時只能有一筆 `fetching`（部分唯一索引）。`failure_count` 是
  從建立起累計失敗幾次，到上限就改成 `failed`（遷移 0009、ADR-0028）；加次數與改成 `failed`
  是兩個交易，到上限、還沒改成 `failed` 的那一筆不再抓取或套用。
- `weekly_run_journal`：每本期刊抓到哪一頁、過濾前後各幾篇；每暫存完一頁更新一次，和那一頁的
  暫存列在同一個交易（進度不會比暫存多或少）。

- 寫入一律用 `text()` SQL；函式收呼叫端開好的交易連線，不 commit。
- 讀取函式第一個參數是 `viewer`，只給系統工作（`visible_to_system_only`）。
- `fetched_by_year` 存成 JSON 物件，鍵是年份字串（`{"2016": 14, …}`）；讀出時轉回整數。
"""

import json
import uuid
from collections.abc import Mapping
from datetime import datetime

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
from sqlalchemy.dialects.postgresql import JSONB

from radar.domain.viewer import SystemViewer
from radar.domain.weekly_run import (
    JournalProgress,
    PageCounts,
    WeeklyRun,
    WeeklyRunStatus,
    check_max_failures,
)
from radar.repo.models import Base
from radar.repo.visibility import visible_to_system_only

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0006 建表、0009 加 `failure_count`） ------

_COUNTS = ("pages", "fetched", "staged", "excluded", "out_of_scope", "unparseable")

weekly_run_table = Table(
    "weekly_run",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("status", Text, nullable=False),
    Column("fetch_finished_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # 由遷移 0009 加（Story 1.11）：從建立起累計失敗幾次（ADR-0028）。
    Column("failure_count", Integer, nullable=False, server_default=text("0")),
    # 同時只能有一筆 `fetching`（ADR-0004 決定第 2 點）。
    Index(
        "uq_weekly_run_fetching",
        "status",
        unique=True,
        postgresql_where=text("status = 'fetching'"),
    ),
    CheckConstraint(
        "status IN ('fetching', 'published', 'ai_done', 'exported', 'failed')", name="status"
    ),
    CheckConstraint("failure_count >= 0", name="failure_count_not_negative"),
)

# 外鍵的索引由唯一約束（開頭是 weekly_run_id）提供。
weekly_run_journal_table = Table(
    "weekly_run_journal",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("weekly_run_id", Uuid, ForeignKey("weekly_run.id"), nullable=False),
    Column("journal_key", Text, nullable=False),
    Column("next_cursor", Text),
    Column("finished_at", DateTime(timezone=True)),
    *(Column(name, Integer, nullable=False, server_default=text("0")) for name in _COUNTS),
    Column("fetched_by_year", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("weekly_run_id", "journal_key"),
    CheckConstraint(
        "journal_key ~ '^[a-z][a-z0-9_]*$' AND char_length(journal_key) <= 64",
        name="journal_key_format",
    ),
    CheckConstraint("next_cursor IS NULL OR next_cursor <> ''", name="next_cursor_not_empty"),
    CheckConstraint(" AND ".join(f"{name} >= 0" for name in _COUNTS), name="counts_not_negative"),
    CheckConstraint("jsonb_typeof(fetched_by_year) = 'object'", name="fetched_by_year_object"),
)

_RUN_COLUMNS = "id, status, fetch_finished_at, created_at, updated_at, failure_count"


def _weekly_run(row: RowMapping) -> WeeklyRun:
    return WeeklyRun(
        id=row["id"],
        status=WeeklyRunStatus(row["status"]),
        fetch_finished_at=row["fetch_finished_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        failure_count=row["failure_count"],
    )


def _years_from_json(value: object) -> dict[int, int]:
    """`{"2016": 14}` → `{2016: 14}`；不是 JSON 物件或值不是整數就丟 `ValueError`。"""
    if not isinstance(value, dict):
        raise ValueError("fetched_by_year 要是 JSON 物件")
    years: dict[int, int] = {}
    for key, count in value.items():
        if not isinstance(count, int) or isinstance(count, bool):
            raise ValueError("fetched_by_year 的值要是整數")
        years[int(key)] = count
    return years


def _years_to_json(years: Mapping[int, int]) -> str:
    return json.dumps({str(year): count for year, count in sorted(years.items())})


# --- 讀取 ---------------------------------------------------------------------------------


def get_weekly_run(
    viewer: SystemViewer, connection: Connection, weekly_run_id: uuid.UUID
) -> WeeklyRun | None:
    """這一筆 `weekly_run`；沒有就回 None。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)  # 不是 SystemViewer 就在碰連線之前丟錯
    row = (
        connection.execute(
            text(f"SELECT {_RUN_COLUMNS} FROM weekly_run WHERE {visible} AND id = :id"),
            {"id": weekly_run_id},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _weekly_run(row)


def find_fetching_weekly_run(viewer: SystemViewer, connection: Connection) -> WeeklyRun | None:
    """狀態是 `fetching` 的那一筆（同時最多一筆）；沒有就回 None。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)  # 不是 SystemViewer 就在碰連線之前丟錯
    row = (
        connection.execute(
            text(f"SELECT {_RUN_COLUMNS} FROM weekly_run WHERE {visible} AND status = :status"),
            {"status": WeeklyRunStatus.FETCHING.value},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _weekly_run(row)


def find_latest_weekly_run(viewer: SystemViewer, connection: Connection) -> WeeklyRun | None:
    """最新的一筆 `weekly_run`（`created_at`、再 `id` 最新的）；一筆都沒有就回 None。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)  # 不是 SystemViewer 就在碰連線之前丟錯
    row = (
        connection.execute(
            text(
                f"SELECT {_RUN_COLUMNS} FROM weekly_run WHERE {visible}"
                " ORDER BY created_at DESC, id DESC LIMIT 1"
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _weekly_run(row)


def list_weekly_run_journals(
    viewer: SystemViewer, connection: Connection, weekly_run_id: uuid.UUID
) -> dict[str, JournalProgress]:
    """這一次每週更新每本期刊的進度（期刊代號 → 進度）；還沒開始的期刊不在裡面。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: 表裡的 `fetched_by_year` 不是「年份 → 整數」的 JSON 物件。
    """
    visible = visible_to_system_only(viewer)  # 不是 SystemViewer 就在碰連線之前丟錯
    rows = connection.execute(
        text(
            "SELECT journal_key, next_cursor, finished_at, fetched_by_year, "
            + ", ".join(_COUNTS)
            + f" FROM weekly_run_journal WHERE {visible} AND weekly_run_id = :weekly_run_id"
            " ORDER BY journal_key"
        ),
        {"weekly_run_id": weekly_run_id},
    ).mappings()
    return {
        row["journal_key"]: JournalProgress(
            journal_key=row["journal_key"],
            next_cursor=row["next_cursor"],
            finished=row["finished_at"] is not None,
            fetched_by_year=_years_from_json(row["fetched_by_year"]),
            **{name: row[name] for name in _COUNTS},
        )
        for row in rows
    }


# --- 寫入 ---------------------------------------------------------------------------------


def create_weekly_run(connection: Connection) -> uuid.UUID | None:
    """新增一筆 `fetching` 的 `weekly_run`，回傳它的 `id`；已經有一筆 `fetching` 時不新增、回 None。

    在呼叫端開好的交易裡做，不 commit。
    """
    row = connection.execute(
        text(
            "INSERT INTO weekly_run (status) VALUES (:status)"
            " ON CONFLICT (status) WHERE status = 'fetching' DO NOTHING"
            " RETURNING id"
        ),
        {"status": WeeklyRunStatus.FETCHING.value},
    ).one_or_none()
    return None if row is None else uuid.UUID(str(row[0]))


def upsert_weekly_run_journal_page(
    connection: Connection,
    weekly_run_id: uuid.UUID,
    journal_key: str,
    *,
    first_page: bool,
    next_cursor: str | None,
    counts: PageCounts,
) -> None:
    """記下這本期刊又暫存好一頁：頁數加一、各篇數加上這一頁的、`next_cursor` 換成下一頁的。

    - `first_page=True`（這一頁送的是第一頁）：先把頁數與篇數歸零再加這一頁的（從頭重抓時重算，
      不重複加）。
    - `next_cursor is None`：這本抓完，寫 `finished_at`；否則 `finished_at` 清成 NULL。
    - 同時更新那筆 `weekly_run.updated_at`（sweep 不會把還在推進的那筆當成卡住）。

    在呼叫端開好的交易裡做，不 commit。

    Raises:
        TypeError: `counts` 不是 `PageCounts`（不碰資料庫）。
    """
    if not isinstance(counts, PageCounts):
        raise TypeError("counts 要是 PageCounts")
    # 第一頁時舊值當作 0；之後的頁接著加。
    base = "CASE WHEN :first_page THEN 0 ELSE weekly_run_journal.{0} END + EXCLUDED.{0}"
    connection.execute(
        text(
            "INSERT INTO weekly_run_journal"
            " (weekly_run_id, journal_key, next_cursor, finished_at, pages, fetched, staged,"
            " excluded, out_of_scope, unparseable, fetched_by_year)"
            " VALUES (:weekly_run_id, :journal_key, :next_cursor,"
            " CASE WHEN :finished THEN now() END, 1, :fetched, :staged, :excluded,"
            " :out_of_scope, :unparseable, CAST(:fetched_by_year AS jsonb))"
            " ON CONFLICT (weekly_run_id, journal_key) DO UPDATE SET"
            " next_cursor = EXCLUDED.next_cursor,"
            " finished_at = EXCLUDED.finished_at,"
            + ",".join(f" {name} = {base.format(name)}" for name in _COUNTS)
            + ", fetched_by_year = ("
            "   SELECT COALESCE(jsonb_object_agg(year, total), '{}'::jsonb) FROM ("
            "     SELECT year, sum(n) AS total FROM ("
            "       SELECT key AS year, value::int AS n FROM jsonb_each_text("
            "         CASE WHEN :first_page THEN '{}'::jsonb"
            "         ELSE weekly_run_journal.fetched_by_year END)"
            "       UNION ALL"
            "       SELECT key, value::int FROM jsonb_each_text(EXCLUDED.fetched_by_year)"
            "     ) AS parts GROUP BY year"
            "   ) AS sums"
            " ),"
            " updated_at = now()"
        ),
        {
            "weekly_run_id": weekly_run_id,
            "journal_key": journal_key,
            "next_cursor": next_cursor,
            "finished": next_cursor is None,
            "first_page": first_page,
            "fetched": counts.fetched,
            "staged": counts.staged,
            "excluded": counts.excluded,
            "out_of_scope": counts.out_of_scope,
            "unparseable": counts.unparseable,
            "fetched_by_year": _years_to_json(counts.fetched_by_year),
        },
    )
    update_weekly_run_touched(connection, weekly_run_id)


def update_weekly_run_touched(connection: Connection, weekly_run_id: uuid.UUID) -> None:
    """把這筆 `weekly_run.updated_at` 改成現在（還在推進、額度停下、失敗、已抓完又被排入時用）。

    不 commit。
    """
    connection.execute(
        text("UPDATE weekly_run SET updated_at = now() WHERE id = :id"), {"id": weekly_run_id}
    )


def update_weekly_run_fetch_finished(connection: Connection, weekly_run_id: uuid.UUID) -> bool:
    """這筆還是 `fetching` 時寫 `fetch_finished_at`（已經寫過就保留原本的時間）；回傳有沒有寫到。

    在呼叫端開好的交易裡做，不 commit。
    """
    rows = connection.execute(
        text(
            "UPDATE weekly_run"
            " SET fetch_finished_at = COALESCE(fetch_finished_at, now()), updated_at = now()"
            " WHERE id = :id AND status = :status"
            " RETURNING id"
        ),
        {"id": weekly_run_id, "status": WeeklyRunStatus.FETCHING.value},
    )
    return len(rows.all()) == 1


def update_weekly_run_touched_while_fetching(
    connection: Connection, weekly_run_id: uuid.UUID, *, max_failures: int
) -> bool:
    """這筆還是 `fetching`、而且 `failure_count` 還沒到 `max_failures` 時，把 `updated_at` 改成現在
    並拿到這一列的鎖；回傳有沒有改到（沒改到＝不存在、已經不是 `fetching`，或已到失敗上限）。

    抓取每頁寫原始資料、暫存列與進度的交易第一句（Story 1.11 Codex C1、ADR-0028）：改到了，這一列
    就鎖到交易結束，另一個工作要把它標成 `failed` 得等這個交易結束；沒改到，呼叫端整個交易都不寫。
    另一個交易正改著這一列時先等它結束，再照改完的值判斷（PostgreSQL 的 READ COMMITTED 會重新
    檢查 WHERE）。在呼叫端開好的交易裡做，不 commit。

    Raises:
        ValueError: `max_failures` 不是正整數（不碰資料庫）。
    """
    check_max_failures(max_failures)
    rows = connection.execute(
        text(
            "UPDATE weekly_run SET updated_at = now()"
            " WHERE id = :id AND status = :fetching AND failure_count < :max_failures"
            " RETURNING id"
        ),
        {
            "id": weekly_run_id,
            "fetching": WeeklyRunStatus.FETCHING.value,
            "max_failures": max_failures,
        },
    )
    return len(rows.all()) == 1


def update_weekly_run_published(
    connection: Connection, weekly_run_id: uuid.UUID, *, max_failures: int | None = None
) -> bool:
    """這筆是 `fetching` 而且已經全部抓完時改成 `published`；回傳有沒有改到。

    有給 `max_failures` 時，`failure_count` 已到上限（等著收尾成 `failed`）的也不改（Story 1.11
    Codex C2、ADR-0028）。

    套用交易的第一句：它拿到這一列的鎖，並行的第二個套用會等第一個交易結束，再判斷時已經不是
    `fetching`，就不會改到（互斥，ADR-0026）。和其他寫入在同一個交易，中途失敗時一起退回。
    在呼叫端開好的交易裡做，不 commit。

    Raises:
        ValueError: 有給 `max_failures` 但不是正整數（不碰資料庫）。
    """
    limit = ""
    params: dict[str, object] = {
        "id": weekly_run_id,
        "published": WeeklyRunStatus.PUBLISHED.value,
        "fetching": WeeklyRunStatus.FETCHING.value,
    }
    if max_failures is not None:
        params["max_failures"] = check_max_failures(max_failures)
        limit = " AND failure_count < :max_failures"
    rows = connection.execute(
        text(
            "UPDATE weekly_run SET status = :published, updated_at = now()"
            " WHERE id = :id AND status = :fetching AND fetch_finished_at IS NOT NULL"
            + limit
            + " RETURNING id"
        ),
        params,
    )
    return len(rows.all()) == 1


def update_weekly_run_failure(connection: Connection, weekly_run_id: uuid.UUID) -> int | None:
    """記這筆每週更新又失敗一次：只在 `fetching` 時把 `failure_count` 加一，回傳加完的次數；
    不存在或不是 `fetching`（已經 `failed`、已發佈）時什麼都不改、回 None。

    只加次數、不改狀態：到上限時改成 `failed` 是另一個交易的事（`update_weekly_run_failed`），
    收尾失敗時次數也不會跟著退回（Story 1.11 Codex C2、ADR-0028）。不改 `updated_at`（那是
    `update_weekly_run_touched` 的事）。在呼叫端開好的交易裡做，不 commit。
    """
    row = connection.execute(
        text(
            "UPDATE weekly_run SET failure_count = failure_count + 1"
            " WHERE id = :id AND status = :fetching"
            " RETURNING failure_count"
        ),
        {"id": weekly_run_id, "fetching": WeeklyRunStatus.FETCHING.value},
    ).one_or_none()
    return None if row is None else int(row[0])


def update_weekly_run_failed(
    connection: Connection, weekly_run_id: uuid.UUID, *, max_failures: int
) -> int | None:
    """這筆還是 `fetching`、而且 `failure_count` 已到 `max_failures` 時改成 `failed`（同時更新
    `updated_at`）；回傳它的累計失敗次數。沒改到（不存在、不是 `fetching`、還沒到上限）回 None。

    收尾的交易用：和刪暫存列在同一個交易（ADR-0028）。在呼叫端開好的交易裡做，不 commit。

    Raises:
        ValueError: `max_failures` 不是正整數（不碰資料庫）。
    """
    check_max_failures(max_failures)
    row = connection.execute(
        text(
            "UPDATE weekly_run SET status = :failed, updated_at = now()"
            " WHERE id = :id AND status = :fetching AND failure_count >= :max_failures"
            " RETURNING failure_count"
        ),
        {
            "id": weekly_run_id,
            "max_failures": max_failures,
            "failed": WeeklyRunStatus.FAILED.value,
            "fetching": WeeklyRunStatus.FETCHING.value,
        },
    ).one_or_none()
    return None if row is None else int(row[0])


def update_weekly_runs_ai_done(connection: Connection, cutoff: datetime) -> list[uuid.UUID]:
    """`published` 的每週更新，這一週的 Batch 都結束了（有批次、全部 `imported`／`failed`），或
    快照的發布時間（`snapshot.as_of`，不是開始建立的 `created_at`）早於 `cutoff`（呼叫端給
    「現在 − 24 小時」）時改成 `ai_done`；回傳改到的 id（AD-14、Story 2.11）。

    判斷和改寫在同一句：並行的第二個呼叫等第一個交易結束，再判斷時已經不是 `published`，不會再改。
    在呼叫端開好的交易裡做，不 commit。

    Raises:
        ValueError: `cutoff` 沒有時區（不碰資料庫）。
    """
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff 要帶時區")
    rows = connection.execute(
        text(
            "UPDATE weekly_run SET status = :ai_done, updated_at = now()"
            " WHERE status = :published AND ("
            " (EXISTS (SELECT 1 FROM batch_submission AS b WHERE b.weekly_run_id = weekly_run.id)"
            " AND NOT EXISTS (SELECT 1 FROM batch_submission AS b"
            " WHERE b.weekly_run_id = weekly_run.id AND b.status NOT IN ('imported', 'failed')))"
            " OR EXISTS (SELECT 1 FROM snapshot AS s"
            " WHERE s.weekly_run_id = weekly_run.id AND s.as_of < :cutoff))"
            " RETURNING id"
        ),
        {
            "ai_done": WeeklyRunStatus.AI_DONE.value,
            "published": WeeklyRunStatus.PUBLISHED.value,
            "cutoff": cutoff,
        },
    )
    return [row[0] for row in rows.all()]
