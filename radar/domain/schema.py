"""資料庫的遷移狀態（`/readyz` 用，AD-15、AD-29）。"""

from enum import StrEnum


class SchemaState(StrEnum):
    """資料庫的遷移版本和程式比起來的狀態。"""

    # 沒有 alembic_version，或裡面沒有版本：還沒遷移。
    NOT_MIGRATED = "not_migrated"
    # 版本是程式認得的，但不是最新：程式需要的遷移還沒跑。
    BEHIND = "behind"
    CURRENT = "current"
    # 版本是程式不認得的（比程式新）：退版時會遇到，上一版程式照樣要能跑（AD-29）。
    AHEAD = "ahead"


READY_STATES = frozenset({SchemaState.CURRENT, SchemaState.AHEAD})


def classify_schema(
    revisions: frozenset[str] | None, *, heads: frozenset[str], known: frozenset[str]
) -> SchemaState:
    """`revisions` 是資料庫裡的版本（沒有版本表時是 None）；`heads` 是程式的最新版本，
    `known` 是程式認得的全部版本。
    """
    if not revisions:
        return SchemaState.NOT_MIGRATED
    if not revisions <= known:
        return SchemaState.AHEAD
    if revisions == heads:
        return SchemaState.CURRENT
    return SchemaState.BEHIND
