"""排入工作的替身：記下排了什麼；同一個排隊鎖還在排隊時回 False（和真的佇列一樣）。"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from radar.domain.jobs import JobSpec, queueing_lock


@dataclass(frozen=True)
class QueuedJob:
    job: JobSpec
    ids: Mapping[str, uuid.UUID]


@dataclass
class FakeJobQueue:
    """`enqueue` 符合 `radar.domain.jobs.Enqueue`；`retry_stalled` 回傳 `stalled` 並清空它。"""

    queued: list[QueuedJob] = field(default_factory=list)
    stalled: int = 0
    retry_calls: int = 0

    def enqueue(self, job: JobSpec, ids: Mapping[str, uuid.UUID]) -> bool:
        lock = queueing_lock(job, ids)
        if any(queueing_lock(item.job, item.ids) == lock for item in self.queued):
            return False
        self.queued.append(QueuedJob(job, dict(ids)))
        return True

    def retry_stalled(self) -> int:
        self.retry_calls += 1
        retried, self.stalled = self.stalled, 0
        return retried
