"""`services/coverage.get_coverage`：快照前後各讀一次的一致性做法（Story 1.10、ADR-0027）。

假的 `Database`＋把 `radar.services.coverage` 裡的 repo 函式換掉；不連資料庫。
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from radar.domain.cube import CountedCell
from radar.domain.domain_config import Journal, JournalsConfig
from radar.domain.papers import AbstractSource
from radar.domain.snapshot import Snapshot, SnapshotStatus
from radar.domain.viewer import SystemViewer
from radar.services import coverage as coverage_module
from radar.services.coverage import MAX_ATTEMPTS, get_coverage

MISQ_ISSN = "0276-7783"
JOURNALS = JournalsConfig(
    start_year=2016, journals=(Journal(key="misq", name="MIS Quarterly", issn_l=MISQ_ISSN),)
)
AS_OF = datetime(2026, 11, 16, 2, 30, tzinfo=UTC)


def _snapshot(number: int) -> Snapshot:
    return Snapshot(
        id=uuid.UUID(int=number),
        number=number,
        weekly_run_id=uuid.UUID(int=1000 + number),
        status=SnapshotStatus.PUBLISHED,
        as_of=AS_OF,
        created_at=AS_OF,
    )


class FakeDatabase:
    """只記交易開了幾次、目前是不是在交易裡。"""

    def __init__(self) -> None:
        self.transactions = 0
        self.in_transaction = False

    @contextmanager
    def transaction(self) -> Iterator[str]:
        self.transactions += 1
        self.in_transaction = True
        try:
            yield "connection"
        finally:
            self.in_transaction = False


class FakeRepo:
    """依序回 `snapshots` 裡的快照；每次計數回 `counts` 的下一份。記下呼叫順序。"""

    def __init__(
        self,
        database: FakeDatabase,
        snapshots: list[Snapshot | None],
        counts: list[list[CountedCell]],
    ) -> None:
        self.database = database
        self.snapshots = list(snapshots)
        self.counts = list(counts)
        self.calls: list[str] = []

    def find_latest_published_snapshot(self, viewer: Any, connection: Any) -> Snapshot | None:
        assert isinstance(viewer, SystemViewer)
        assert connection == "connection"
        assert self.database.in_transaction
        self.calls.append("snapshot")
        return self.snapshots.pop(0)

    def list_cube_counts(self, viewer: Any, connection: Any) -> list[CountedCell]:
        assert isinstance(viewer, SystemViewer)
        assert connection == "connection"
        assert self.database.in_transaction
        self.calls.append("counts")
        return self.counts.pop(0)


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    database: FakeDatabase,
    snapshots: list[Snapshot | None],
    counts: list[list[CountedCell]] | None = None,
) -> FakeRepo:
    repo = FakeRepo(database, snapshots, counts or [[] for _ in range(MAX_ATTEMPTS)])
    monkeypatch.setattr(
        coverage_module, "find_latest_published_snapshot", repo.find_latest_published_snapshot
    )
    monkeypatch.setattr(coverage_module, "list_cube_counts", repo.list_cube_counts)
    return repo


def _get(database: FakeDatabase) -> Any:
    return get_coverage(SystemViewer(), database, journals=JOURNALS)  # type: ignore[arg-type]


def test_no_published_snapshot_returns_none(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase
) -> None:
    repo = _install(monkeypatch, database, [None])

    assert _get(database) is None
    assert repo.calls == ["snapshot"]
    assert database.transactions == 1


def test_same_snapshot_before_and_after_reads_once(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase
) -> None:
    cells = [CountedCell(MISQ_ISSN, 2020, AbstractSource.OPENALEX, 2)]
    repo = _install(monkeypatch, database, [_snapshot(1), _snapshot(1)], [cells])

    report = _get(database)

    assert report.snapshot_number == 1
    assert report.by_source == {"misq": {AbstractSource.OPENALEX: 2}}
    assert repo.calls == ["snapshot", "counts", "snapshot"]
    assert database.transactions == 1


def test_new_snapshot_in_between_reads_again_and_uses_second_round(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase
) -> None:
    first = [CountedCell(MISQ_ISSN, 2020, None, 1)]
    second = [CountedCell(MISQ_ISSN, 2020, None, 1), CountedCell(MISQ_ISSN, 2021, None, 1)]
    repo = _install(
        monkeypatch,
        database,
        [_snapshot(1), _snapshot(2), _snapshot(2), _snapshot(2)],
        [first, second],
    )

    report = _get(database)

    assert report.snapshot_number == 2
    assert [(row.year, row.dim.value, row.n) for row in report.cube] == [
        (2020, "total", 1),
        (2020, "with_abstract", 0),
        (2021, "total", 1),
        (2021, "with_abstract", 0),
    ]
    assert repo.calls == ["snapshot", "counts", "snapshot"] * 2
    assert database.transactions == 1


def test_different_snapshots_three_rounds_raise(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase
) -> None:
    snapshots: list[Snapshot | None] = [_snapshot(n) for n in range(1, 2 * MAX_ATTEMPTS + 1)]
    repo = _install(monkeypatch, database, snapshots)

    with pytest.raises(RuntimeError, match="快照都不同"):
        _get(database)

    assert MAX_ATTEMPTS == 3
    assert repo.calls == ["snapshot", "counts", "snapshot"] * 3
    assert database.transactions == 1
