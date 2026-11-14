"""`sweep` 讀狀態表：找出停在起始狀態太久的列（AD-14、ADR-0004 決定第 9 點）。只讀，不 commit。"""

import uuid

from sqlalchemy import Connection, text

from radar.domain.sweep import SweepEntry


def find_stuck_ids(connection: Connection, entry: SweepEntry, limit: int) -> list[uuid.UUID]:
    """`entry.table` 裡狀態是起始狀態、而且時間欄早於「現在減時限」的列的 `id`，
    依時間由舊到新、最多 `limit` 筆。

    表名與欄名在 `SweepEntry` 建立時已經檢查過只含小寫識別字，這裡再用資料庫的規則加引號。
    """
    quote = connection.dialect.identifier_preparer.quote
    table, status, time = (
        quote(entry.table),
        quote(entry.status_column),
        quote(entry.time_column),
    )
    # 只有識別字組進字串（已驗證並加引號）；值一律走參數。
    rows = connection.execute(
        text(
            f"SELECT id FROM {table}"
            f" WHERE {status} = :start_status AND {time} < now() - :stuck_after"
            f" ORDER BY {time}, id LIMIT :limit"
        ),
        {"start_status": entry.start_status, "stuck_after": entry.stuck_after, "limit": limit},
    )
    return [uuid.UUID(str(value)) for value in rows.scalars()]
