"""`radar runs report` 的拒收報表（Story 2.11、ADR-0043）：範圍、每列的結果與要印的字。

- 範圍（`RunScope`）：哪幾個批次的 run（`--batch`，可重複）、哪幾篇論文（`--ids-file`，每行一個
  UUID，空行與 `#` 開頭的行略過：`parse_ids_file`）、從什麼時候以後建的 run（`--since`，ISO 8601，
  沒寫時區當台北時間：`parse_since`）；給了幾個就同時滿足幾個，至少要給一個。
- 只算三個 Batch 工作（中文重點、分類、研究模型）的平常流程 run（不含評估）；每篇每工作只看範圍內
  `created_at` 最新的一筆 run（`repo/ai_outputs.list_run_outcomes`）。
- 那筆 run 有表頭就照表頭的狀態（有效、已取代、拒收），沒有就照 run 的狀態（還在跑、暫時失敗；
  成功卻沒有表頭的算暫時失敗）。
"""

import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from radar.domain.ai_outputs import LlmTask, ResultStatus, RunStatus
from radar.domain.budget import BUDGET_TIMEZONE
from radar.domain.llm_batches import BATCH_TASKS
from radar.domain.text_table import render_table


@dataclass(frozen=True)
class RunScope:
    """報表的範圍；欄位之間是「而且」。

    Raises:
        ValueError: 三個都沒給，或 `since` 沒有時區。
    """

    batch_ids: tuple[uuid.UUID, ...] = ()
    paper_ids: tuple[uuid.UUID, ...] | None = None
    since: datetime | None = None

    def __post_init__(self) -> None:
        if not self.batch_ids and self.paper_ids is None and self.since is None:
            raise ValueError("報表範圍至少要給 --batch、--ids-file、--since 其中一個")
        if self.since is not None and (self.since.tzinfo is None or self.since.utcoffset() is None):
            raise ValueError("since 要帶時區")


def parse_ids_file(text: str) -> tuple[uuid.UUID, ...]:
    """`--ids-file` 的內容：每行一個論文 UUID，空行與 `#` 開頭（前面可以有空白）的行略過；重複的
    只算一次、照第一次出現的順序。

    Raises:
        ValueError: 有一行不是 UUID（訊息只帶行號，不帶內容）。
    """
    ids: dict[uuid.UUID, None] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            ids[uuid.UUID(value)] = None
        except ValueError:
            raise ValueError(f"第 {number} 行不是論文 ID（UUID）") from None
    return tuple(ids)


def parse_since(text: str) -> datetime:
    """`--since` 的值：ISO 8601；沒寫時區的當台北時間。

    Raises:
        ValueError: 不是 ISO 8601 的日期或時間。
    """
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(
            "--since 要是 ISO 8601 的日期或時間（例如 2026-11-16 或 2026-11-16T08:00）"
        ) from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.replace(tzinfo=BUDGET_TIMEZONE)
    return moment


class RunOutcome(StrEnum):
    """一篇論文一個工作在範圍內最新那一筆 run 的結果。"""

    ACTIVE = "active"  # 有效
    SUPERSEDED = "superseded"  # 已取代
    REJECTED = "rejected"  # 拒收
    FAILED = "failed"  # 暫時失敗（下一次 `radar batches create` 會再送，每週規劃開啟後也會）
    RUNNING = "running"  # 還在跑


_LABELS = {
    RunOutcome.ACTIVE: "有效",
    RunOutcome.SUPERSEDED: "已取代",
    RunOutcome.REJECTED: "拒收",
    RunOutcome.FAILED: "暫時失敗",
    RunOutcome.RUNNING: "還在跑",
}


@dataclass(frozen=True)
class RunOutcomeRow:
    """報表的一列：一篇論文一個工作；`reason` 是拒收或失敗的原因代碼（其他是 None）。"""

    paper_id: uuid.UUID
    task: LlmTask
    outcome: RunOutcome
    reason: str | None = None


def run_outcome(run_status: RunStatus, header_status: ResultStatus | None) -> RunOutcome:
    """有表頭照表頭（候選不算，當成沒有）；沒有就照 run 的狀態。"""
    if header_status is ResultStatus.ACTIVE:
        return RunOutcome.ACTIVE
    if header_status is ResultStatus.SUPERSEDED:
        return RunOutcome.SUPERSEDED
    if header_status is ResultStatus.REJECTED:
        return RunOutcome.REJECTED
    if run_status is RunStatus.RUNNING:
        return RunOutcome.RUNNING
    return RunOutcome.FAILED


def render_run_report(rows: Iterable[RunOutcomeRow]) -> list[str]:
    """依工作印篇數、各結果的篇數，以及拒收、暫時失敗依原因代碼的篇數。"""
    rows = list(rows)
    if not rows:
        return ["範圍內沒有 AI 工作的 run"]
    lines: list[str] = []
    header = ["工作", "篇數", *(_LABELS[outcome] for outcome in RunOutcome)]
    body: list[list[str]] = []
    for task in BATCH_TASKS:
        mine = [row for row in rows if row.task is task]
        counts = Counter(row.outcome for row in mine)
        body.append(
            [task.value, f"{len(mine):,}", *(f"{counts[outcome]:,}" for outcome in RunOutcome)]
        )
    lines += render_table(header, body, align="l" + "r" * (len(header) - 1))
    for outcome in (RunOutcome.REJECTED, RunOutcome.FAILED):
        reasons = _reasons(rows, outcome)
        if not reasons:
            continue
        lines += ["", f"{_LABELS[outcome]}依原因代碼（篇數）"]
        lines += render_table(
            ["工作", "原因代碼", "篇數"],
            [[task.value, reason, f"{count:,}"] for (task, reason), count in reasons],
            align="llr",
        )
    return lines


def _reasons(
    rows: Sequence[RunOutcomeRow], outcome: RunOutcome
) -> list[tuple[tuple[LlmTask, str], int]]:
    order = {task: index for index, task in enumerate(BATCH_TASKS)}
    counts = Counter((row.task, row.reason or "—") for row in rows if row.outcome is outcome)
    return sorted(counts.items(), key=lambda item: (order[item[0][0]], -item[1], item[0][1]))
