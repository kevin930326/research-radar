"""背景工作的清單：每個工作屬於哪一條佇列、有沒有排程（AD-14、AD-15、ADR-0004、ADR-0019）。

新增工作的 Story 在 `JOBS` 加一列；有排程的另外在 `deploy/crontab` 加一行
`radar enqueue <排程名稱>`，並在設定加這個排程的心跳網址（`RADAR_HEARTBEAT_URL_<排程名稱>`）。
架構測試檢查 crontab、`JOBS` 與 worker 登記的工作三者一致。
"""

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum


class Queue(StrEnum):
    """四條佇列（ADR-0004 決定第 6 點）。"""

    INGEST = "ingest"
    AI = "ai"
    PDF = "pdf"
    MAINTENANCE = "maintenance"


# `pdf` 佇列的工作排入時一律帶這把鎖：帶同一把鎖的工作同時只跑一個（解析 PDF 最吃記憶體）。
PDF_LOCK = "pdf"

# worker 每隔幾秒向資料庫報一次「我還活著」；超過 `worker_stalled_seconds` 沒報到的 worker，
# 它手上「執行中」的工作由 sweep 重試（Procrastinate 的 stalled job）。
WORKER_HEARTBEAT_SECONDS = 10

# 工作名稱：動詞_名詞（Conventions「背景工作命名」）；排程名稱：小寫加連字號（crontab 與心跳用）。
_JOB_NAME = re.compile(r"[a-z][a-z0-9_]*")
_SCHEDULE_NAME = re.compile(r"[a-z][a-z0-9-]*")
# 工作參數只有 ID：名稱一律 `*_id`。
_ID_PARAMETER = re.compile(r"[a-z][a-z0-9_]*_id")


@dataclass(frozen=True)
class JobSpec:
    """一個背景工作。`schedule` 是 cron 用的排程名稱（也是心跳的檢查名稱），沒有排程的是 None。"""

    name: str
    queue: Queue
    schedule: str | None = None

    def __post_init__(self) -> None:
        if not _JOB_NAME.fullmatch(self.name):
            raise ValueError(f"工作名稱只能是小寫英文、數字與底線：{self.name!r}")
        if self.schedule is not None and not _SCHEDULE_NAME.fullmatch(self.schedule):
            raise ValueError(f"排程名稱只能是小寫英文、數字與連字號：{self.schedule!r}")

    @property
    def check(self) -> str:
        """心跳的檢查名稱（就是排程名稱，一個排程一個心跳）。

        Raises:
            ValueError: 這個工作沒有排程，也就沒有心跳。
        """
        if self.schedule is None:
            raise ValueError(f"工作 {self.name} 沒有排程，沒有心跳的檢查名稱")
        return self.schedule


# 把卡住的狀態列與卡住的工作重新排入（ADR-0004 決定第 9 點）。
SWEEP = JobSpec("sweep", Queue.MAINTENANCE, schedule="sweep")

JOBS: tuple[JobSpec, ...] = (SWEEP,)

# 排入一個工作：（工作, 參數）→ True＝新排入；False＝同一件事已經在排隊，沒有再排一筆。
# 參數只有 ID（名稱 `*_id`、值是 UUID）。
# 真實的函式在 `radar/adapters/jobs/`，替身在 `radar/adapters/fake/`。
type Enqueue = Callable[[JobSpec, Mapping[str, uuid.UUID]], bool]


class UnknownJobError(Exception):
    """不認得的工作或排程名稱。`available` 是可以用的名稱。"""

    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = available
        super().__init__(f"不認得的工作：{name!r}（可用：{'、'.join(available)}）")


def scheduled_jobs() -> list[JobSpec]:
    """有排程的工作，依排程名稱排序。"""
    return sorted((job for job in JOBS if job.schedule is not None), key=lambda job: job.check)


def find_scheduled_job(schedule: str) -> JobSpec:
    """用排程名稱找工作（`radar enqueue <排程名稱>`）。

    Raises:
        UnknownJobError: 沒有這個排程。
    """
    jobs = scheduled_jobs()
    for job in jobs:
        if job.schedule == schedule:
            return job
    raise UnknownJobError(schedule, [job.check for job in jobs])


def find_job(name: str) -> JobSpec:
    """用工作名稱找工作。

    Raises:
        UnknownJobError: 沒有這個工作。
    """
    for job in JOBS:
        if job.name == name:
            return job
    raise UnknownJobError(name, sorted(job.name for job in JOBS))


def is_id_parameter(name: str) -> bool:
    """工作參數的名稱是不是 `*_id`（小寫英文、數字與底線）。"""
    return _ID_PARAMETER.fullmatch(name) is not None


def check_job_ids(ids: Mapping[str, object]) -> None:
    """工作參數只有 ID：名稱是 `*_id`、值是 UUID；不符就丟 `ValueError`。"""
    for key, value in ids.items():
        if not is_id_parameter(key):
            raise ValueError(f"工作參數名稱要是 *_id：{key!r}")
        if not isinstance(value, uuid.UUID):
            raise ValueError(f"工作參數 {key} 要是 UUID（現在是 {type(value).__name__}）")


def queueing_lock(job: JobSpec, ids: Mapping[str, uuid.UUID]) -> str:
    """排隊鎖：同一件事在排隊中只會有一筆。沒有參數是 `<工作>`，有參數是 `<工作>:<ID…>`
    （依參數名稱排序，和傳入順序無關）。
    """
    check_job_ids(ids)
    if not ids:
        return job.name
    return ":".join([job.name, *(str(ids[key]) for key in sorted(ids))])
