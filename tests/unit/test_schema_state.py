"""資料庫遷移狀態的判斷（`/readyz` 用）。"""

import pytest

from radar.domain.schema import READY_STATES, SchemaState, classify_schema

KNOWN = frozenset({"0001", "0002", "0003"})
HEADS = frozenset({"0003"})


@pytest.mark.parametrize(
    ("revisions", "expected"),
    [
        (None, SchemaState.NOT_MIGRATED),
        (frozenset(), SchemaState.NOT_MIGRATED),
        (frozenset({"0003"}), SchemaState.CURRENT),
        (frozenset({"0001"}), SchemaState.BEHIND),
        (frozenset({"0002"}), SchemaState.BEHIND),
        (frozenset({"0004"}), SchemaState.AHEAD),
        # 認得的和不認得的混在一起：資料庫有程式不知道的版本，算比程式新。
        (frozenset({"0003", "9999"}), SchemaState.AHEAD),
    ],
)
def test_classify(revisions: frozenset[str] | None, expected: SchemaState) -> None:
    assert classify_schema(revisions, heads=HEADS, known=KNOWN) is expected


def test_only_current_and_ahead_are_ready() -> None:
    assert {SchemaState.CURRENT, SchemaState.AHEAD} == READY_STATES


def test_state_values_are_lowercase_names() -> None:
    assert [state.value for state in SchemaState] == ["not_migrated", "behind", "current", "ahead"]
