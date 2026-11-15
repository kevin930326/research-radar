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
    """一個背景工作。`schedule` 是 cron 用的排程名稱（也是心跳的檢查名稱），沒有排程的是 None。

    `lock`：排入時帶的佇列鎖（帶同一把鎖的工作同時只跑一個），沒有是 None。`pdf` 佇列的工作
    不能另設，一律用 `PDF_LOCK`（由 `adapters/jobs.defer_job` 帶上）。
    """

    name: str
    queue: Queue
    schedule: str | None = None
    lock: str | None = None

    def __post_init__(self) -> None:
        if not _JOB_NAME.fullmatch(self.name):
            raise ValueError(f"工作名稱只能是小寫英文、數字與底線：{self.name!r}")
        if self.schedule is not None and not _SCHEDULE_NAME.fullmatch(self.schedule):
            raise ValueError(f"排程名稱只能是小寫英文、數字與連字號：{self.schedule!r}")
        if self.lock is not None:
            if not isinstance(self.lock, str) or not self.lock:
                raise ValueError(f"鎖的名稱要是非空字串：{self.lock!r}")
            if self.queue is Queue.PDF:
                raise ValueError(f"pdf 佇列的工作一律用 {PDF_LOCK!r} 這把鎖，不能另設：{self.name}")

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

# 每週更新（ADR-0004 決定第 2、5 點、ADR-0025）：cron 排入的 `weekly_update` 寫（或找到）那筆
# `fetching` 的 `weekly_run`、交易結束後排入 `run_weekly_update(weekly_run_id)`；後者逐本、逐頁
# 推進。`run_weekly_update` 帶 `weekly_update` 這把鎖，同時只跑一個（手動再排、sweep 補排時，
# 第二個等第一個做完，接著只會看到已抓完的進度）。
WEEKLY_UPDATE = JobSpec("weekly_update", Queue.INGEST, schedule="weekly-update")
RUN_WEEKLY_UPDATE = JobSpec("run_weekly_update", Queue.INGEST, lock="weekly_update")

# 向量化（Story 2.8、AD-12、ADR-0040）：每週更新發佈快照、commit 之後排入（沒有參數、沒有排程）。
# 工作本身照 `plan_for` 找出還沒有向量或輸入變了的論文，一批一批做到完或到今天的額度比例為止；
# 帶 `embed_papers` 這把鎖，同時只跑一個（和 `radar embeddings backfill` 之間靠 `derivation_run`
# 的 running 唯一索引避免重送同一篇）。
EMBED_PAPERS = JobSpec("embed_papers", Queue.AI, lock="embed_papers")

# Batch（Story 2.9、ADR-0041）：每週更新發佈快照、commit 之後，在向量化之後排入
# `plan_llm_batches(weekly_run_id)`：照 `plan_for` 與 `should_run` 把 AI 工作排成一個個
# `batch_submission`（帶 `plan_llm_batches` 這把鎖，同時只跑一個；兩個同時跑也靠資料層擋）。
# 每建好一批排入 `submit_llm_batch(batch_submission_id)`（帶 `llm_batches` 這把鎖、不重試：
# batch create 沒有冪等鍵）；停在 `queued`／`submitting` 的由 sweep 補排。
PLAN_LLM_BATCHES = JobSpec("plan_llm_batches", Queue.AI, lock="plan_llm_batches")
SUBMIT_LLM_BATCH = JobSpec("submit_llm_batch", Queue.AI, lock="llm_batches")

JOBS: tuple[JobSpec, ...] = (
    SWEEP,
    WEEKLY_UPDATE,
    RUN_WEEKLY_UPDATE,
    EMBED_PAPERS,
    PLAN_LLM_BATCHES,
    SUBMIT_LLM_BATCH,
)

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
