"""sweep 多一步：把逾時的互動 LLM 呼叫（`derivation_run` 的 `running`）改成 `failed`（ADR-0030）。

`derivation_run` 不進 sweep 的登記清單（它卡住時不是重新排入）；worker 傳 `expire_stale_runs`。
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import pytest

from radar.adapters.fake import FakeHeartbeat, FakeJobQueue
from radar.domain.permissions import PermissionDeniedError
from radar.domain.sweep import SWEEP_REGISTRY
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer
from radar.services.llm_gateway import expire_stale_runs
from radar.services.sweep import SweepResult, sweep


class FakeDatabase:
    """只記交易開了幾次。"""

    def __init__(self) -> None:
        self.transactions = 0

    @contextmanager
    def transaction(self) -> Iterator[str]:
        self.transactions += 1
        yield "connection"


def test_sweep_reports_expired_runs_and_sends_heartbeat() -> None:
    queue, heartbeat = FakeJobQueue(stalled=1), FakeHeartbeat()

    result = sweep(
        FakeDatabase(),  # type: ignore[arg-type]
        (),
        queue.enqueue,
        queue.retry_stalled,
        heartbeat,
        expire_runs=lambda: 3,
    )

    assert result == SweepResult(stuck_rows=0, requeued=0, retried_jobs=1, expired_runs=3)
    assert heartbeat.sent == ["sweep"]


def test_sweep_without_expire_runs_reports_zero() -> None:
    queue, heartbeat = FakeJobQueue(), FakeHeartbeat()

    result = sweep(FakeDatabase(), (), queue.enqueue, queue.retry_stalled, heartbeat)  # type: ignore[arg-type]

    assert result.expired_runs == 0


def test_expire_failure_sends_no_heartbeat() -> None:
    queue, heartbeat = FakeJobQueue(), FakeHeartbeat()

    def broken() -> int:
        raise ConnectionError("db down")

    with pytest.raises(ConnectionError):
        sweep(
            FakeDatabase(),  # type: ignore[arg-type]
            (),
            queue.enqueue,
            queue.retry_stalled,
            heartbeat,
            expire_runs=broken,
        )
    assert heartbeat.sent == []


def test_derivation_run_is_not_in_the_sweep_registry() -> None:
    assert "derivation_run" not in {entry.table for entry in SWEEP_REGISTRY}


@pytest.mark.parametrize(
    "actor",
    [PublicViewer(), MemberViewer(member_id=uuid.UUID(int=1), is_admin=True)],
)
def test_expire_stale_runs_is_system_only(actor: MemberViewer | PublicViewer) -> None:
    database = FakeDatabase()

    with pytest.raises(PermissionDeniedError):
        expire_stale_runs(actor, database, timedelta(minutes=15))  # type: ignore[arg-type]
    assert database.transactions == 0


@pytest.mark.parametrize("older_than", [timedelta(0), timedelta(seconds=-1)])
def test_expire_stale_runs_needs_positive_duration(older_than: timedelta) -> None:
    database = FakeDatabase()

    with pytest.raises(ValueError, match="older_than"):
        expire_stale_runs(SystemViewer(), database, older_than)  # type: ignore[arg-type]
    assert database.transactions == 0
