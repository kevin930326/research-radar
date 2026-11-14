"""第一次匯入（以及之後每一次每週更新）的匯入報表：純函式（epics Story 1.9、FR-8、ADR-0026）。

`radar weekly-run report` 印的內容，依序：

1. 這一次每週更新的 id、狀態、抓完時間；有快照時印快照編號、截至時間與套用花的時間，
   超過 `APPLY_SLOW_THRESHOLD`（AD-19 的預設門檻）加註。
2. 每本期刊每年的過濾前篇數（`weekly_run_journal.fetched_by_year`）。
3. 依期刊的排除篇數（`excluded`）、收錄範圍外（`out_of_scope`）、解析失敗（`unparseable`）。
4. 和 OQ-02 基準（`config/import_baseline.yaml`）比對 `year_from`–`year_to` 的過濾前篇數，
   差異超過 `EXCEED_PERCENT`% 的期刊標出來。

收錄篇數與有摘要的篇數不在這裡印（AD-5：論文的篇數只由之後的 `repo/stats.py` 算，Story 1.10）。
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from radar.domain.domain_config import JournalsConfig
from radar.domain.snapshot import Snapshot, SnapshotStatus
from radar.domain.text_table import display_width as display_width
from radar.domain.text_table import render_table
from radar.domain.weekly_run import JournalProgress, WeeklyRun, WeeklyRunStatus

# 套用超過這個時間，第一次匯入可以改成一本期刊一個交易（AD-19 的預設門檻；這裡只用來加註）。
APPLY_SLOW_THRESHOLD = timedelta(minutes=10)
# 和基準的差異超過幾 %（不含剛好）要標出來（epics Story 1.9）。
EXCEED_PERCENT = 5

_JOURNAL_KEY = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
_COUNT = Annotated[int, Field(ge=0)]


class ImportBaseline(BaseModel):
    """`config/import_baseline.yaml`：OQ-02 量到的各期刊各年篇數（期刊代號 → 年 → 篇數）。

    只有報表指令讀；不放進 `DomainConfig`，api、worker 啟動時不讀。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    year_from: int = Field(ge=1900, le=9999)
    year_to: int = Field(ge=1900, le=9999)
    journals: dict[_JOURNAL_KEY, dict[int, _COUNT]] = Field(min_length=1)

    @model_validator(mode="after")
    def _years_in_range(self) -> Self:
        if self.year_from > self.year_to:
            raise ValueError(f"year_from（{self.year_from}）不能比 year_to（{self.year_to}）晚")
        problems = [
            f"journals.{key}.{year}：年份不在 {self.year_from}–{self.year_to} 裡"
            for key, years in self.journals.items()
            for year in years
            if not self.year_from <= year <= self.year_to
        ]
        if problems:
            raise ValueError("；".join(problems))
        return self

    def journal_total(self, key: str) -> int | None:
        """這本期刊 `year_from`–`year_to` 的基準篇數；沒有這本的基準是 None。"""
        years = self.journals.get(key)
        return None if years is None else sum(years.values())

    def total(self) -> int:
        """全部期刊的基準篇數合計。"""
        return sum(sum(years.values()) for years in self.journals.values())


class JournalFetchState(StrEnum):
    """一本期刊在這一次每週更新裡抓到哪裡。"""

    NOT_STARTED = "not_started"
    FETCHING = "fetching"
    FINISHED = "finished"


class ComparisonOutcome(StrEnum):
    """和基準比對的結果。"""

    NOT_FINISHED = "not_finished"  # 還沒抓完，不比
    NO_BASELINE = "no_baseline"  # 沒有基準或基準是 0，不比
    COMPARED = "compared"


@dataclass(frozen=True)
class Comparison:
    """本次的過濾前篇數和基準比的結果。

    `percent`：（本次 − 基準）÷ 基準 × 100，四捨五入到一位小數（只有 `COMPARED` 有）。
    `exceeds`：`|本次 − 基準| × 100 > EXCEED_PERCENT × 基準`（整數運算；剛好 5% 不算）。
    """

    current: int | None
    baseline: int | None
    outcome: ComparisonOutcome
    percent: Decimal | None = None
    exceeds: bool = False


def compare_with_baseline(
    current: int | None, baseline: int | None, *, finished: bool
) -> Comparison:
    """比對本次與基準。還沒抓完的不比、不標；基準是 0 或沒有基準的不比、不標。"""
    if not finished or current is None:
        return Comparison(current, baseline, ComparisonOutcome.NOT_FINISHED)
    if baseline is None or baseline == 0:
        return Comparison(current, baseline, ComparisonOutcome.NO_BASELINE)
    difference = current - baseline
    percent = (Decimal(difference) * 100 / Decimal(baseline)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return Comparison(
        current,
        baseline,
        ComparisonOutcome.COMPARED,
        percent=percent,
        exceeds=abs(difference) * 100 > EXCEED_PERCENT * baseline,
    )


@dataclass(frozen=True)
class JournalReportRow:
    """報表裡的一本期刊。還沒開始抓的期刊，篇數都是 0、`fetched_by_year` 是空的。"""

    key: str
    state: JournalFetchState
    fetched_by_year: Mapping[int, int]
    excluded: int
    out_of_scope: int
    unparseable: int
    comparison: Comparison


@dataclass(frozen=True)
class ImportReport:
    """一次每週更新的匯入報表（`render_import_report` 把它印成文字）。"""

    weekly_run_id: uuid.UUID
    status: WeeklyRunStatus
    fetch_finished_at: datetime | None
    snapshot: Snapshot | None
    year_from: int
    year_to: int
    rows: tuple[JournalReportRow, ...]
    total: Comparison

    @property
    def journals_finished(self) -> int:
        return sum(1 for row in self.rows if row.state is JournalFetchState.FINISHED)

    @property
    def exceeded(self) -> tuple[str, ...]:
        """差異超過 5% 的期刊代號（照報表的順序）。"""
        return tuple(row.key for row in self.rows if row.comparison.exceeds)


def build_import_report(
    run: WeeklyRun,
    progress: Mapping[str, JournalProgress],
    snapshot: Snapshot | None,
    journals: JournalsConfig,
    baseline: ImportBaseline,
) -> ImportReport:
    """組出報表。期刊順序照 `journals` 的設定；進度裡有、設定裡沒有的排在後面（依代號）。

    這次已經抓完（`fetch_finished_at` 有值）時，只列有進度的期刊：之後才加進設定的期刊
    （NFR-9）不屬於這一次，不列成「還沒抓完」。

    合計列只算有基準（且基準不是 0）的期刊：本次＝那些期刊的過濾前篇數加總，基準＝它們的基準
    加總；其中任何一本還沒抓完，合計就是「還沒抓完」。
    """
    configured = [journal.key for journal in journals.journals]
    if run.fetch_finished_at is not None:
        configured = [key for key in configured if key in progress]
    extra = sorted(key for key in progress if key not in set(configured))
    rows: list[JournalReportRow] = []
    for key in (*configured, *extra):
        current = progress.get(key)
        expected = baseline.journal_total(key)
        if current is None:
            rows.append(
                JournalReportRow(
                    key=key,
                    state=JournalFetchState.NOT_STARTED,
                    fetched_by_year={},
                    excluded=0,
                    out_of_scope=0,
                    unparseable=0,
                    comparison=compare_with_baseline(None, expected, finished=False),
                )
            )
            continue
        in_range = sum(
            count
            for year, count in current.fetched_by_year.items()
            if baseline.year_from <= year <= baseline.year_to
        )
        state = JournalFetchState.FINISHED if current.finished else JournalFetchState.FETCHING
        rows.append(
            JournalReportRow(
                key=key,
                state=state,
                fetched_by_year=current.fetched_by_year,
                excluded=current.excluded,
                out_of_scope=current.out_of_scope,
                unparseable=current.unparseable,
                comparison=compare_with_baseline(in_range, expected, finished=current.finished),
            )
        )

    with_baseline = [row for row in rows if row.comparison.baseline]
    if not with_baseline:
        total = Comparison(None, None, ComparisonOutcome.NO_BASELINE)
    else:
        finished = all(row.state is JournalFetchState.FINISHED for row in with_baseline)
        total = compare_with_baseline(
            sum(row.comparison.current or 0 for row in with_baseline),
            sum(row.comparison.baseline or 0 for row in with_baseline),
            finished=finished,
        )
    return ImportReport(
        weekly_run_id=run.id,
        status=run.status,
        fetch_finished_at=run.fetch_finished_at,
        snapshot=snapshot,
        year_from=baseline.year_from,
        year_to=baseline.year_to,
        rows=tuple(rows),
        total=total,
    )


# --- 印成文字 ---------------------------------------------------------------------------------

_NOTHING = "-"
_EXCEEDS_MARK = f"超過 {EXCEED_PERCENT}%"
_RUN_STATUS = {
    WeeklyRunStatus.PUBLISHED: "已發佈",
    WeeklyRunStatus.AI_DONE: "已發佈，AI 處理完成",
    WeeklyRunStatus.EXPORTED: "已發佈，示範站已匯出",
    WeeklyRunStatus.FAILED: "失敗",
}
_SNAPSHOT_STATUS = {SnapshotStatus.BUILDING: "建立中", SnapshotStatus.FAILED: "失敗"}


def render_import_report(report: ImportReport) -> list[str]:
    """把報表印成一行一行的文字（不含換行字元）。時間一律印 UTC。"""
    lines = [f"每週更新 {report.weekly_run_id}", f"狀態：{_status_label(report)}"]
    if report.fetch_finished_at is None:
        lines.append("抓完時間：還沒抓完")
    else:
        lines.append(f"抓完時間：{_utc(report.fetch_finished_at)}")
    if report.snapshot is not None:
        lines.append(_snapshot_line(report.snapshot))

    lines += ["", "每本期刊每年的過濾前篇數", *_years_table(report.rows)]
    lines += ["", "依期刊的排除篇數", *_skips_table(report.rows)]
    lines += [
        "",
        f"和 OQ-02 基準比對（{report.year_from}–{report.year_to} 過濾前篇數）",
        *_comparison_table(report),
        f"超過 {EXCEED_PERCENT}% 的期刊：{'、'.join(report.exceeded) or '沒有'}",
    ]
    return lines


def _status_label(report: ImportReport) -> str:
    if report.status is WeeklyRunStatus.FETCHING:
        if report.fetch_finished_at is None:
            return f"抓取中 {report.journals_finished}／{len(report.rows)} 本抓完"
        return "已抓完還沒套用"
    return _RUN_STATUS[report.status]


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") + "（UTC）"


def _snapshot_line(snapshot: Snapshot) -> str:
    duration = snapshot.apply_duration
    if duration is None or snapshot.as_of is None:
        return f"快照 {snapshot.number}：{_SNAPSHOT_STATUS[snapshot.status]}"
    line = (
        f"快照 {snapshot.number}：截至 {_utc(snapshot.as_of)}；"
        f"套用 {duration.total_seconds():.1f} 秒"
    )
    if duration > APPLY_SLOW_THRESHOLD:
        minutes = int(APPLY_SLOW_THRESHOLD.total_seconds() // 60)
        line += f"，超過 {minutes} 分鐘（AD-19 的門檻）"
    return line


def _number(value: int) -> str:
    return f"{value:,}"


def _years_table(rows: Sequence[JournalReportRow]) -> list[str]:
    seen = [year for row in rows for year in row.fetched_by_year]
    years = list(range(min(seen), max(seen) + 1)) if seen else []
    started = [row for row in rows if row.state is not JournalFetchState.NOT_STARTED]
    body: list[list[str]] = []
    for row in rows:
        if row.state is JournalFetchState.NOT_STARTED:
            body.append([row.key, *([_NOTHING] * (len(years) + 1))])
            continue
        counts = [row.fetched_by_year.get(year, 0) for year in years]
        body.append([row.key, *map(_number, counts), _number(sum(row.fetched_by_year.values()))])
    totals = [sum(row.fetched_by_year.get(year, 0) for row in started) for year in years]
    grand = sum(sum(row.fetched_by_year.values()) for row in started)
    body.append(["合計", *map(_number, totals), _number(grand)])
    header = ["期刊", *map(str, years), "合計"]
    return render_table(header, body, align="l" + "r" * (len(years) + 1))


def _skips_table(rows: Sequence[JournalReportRow]) -> list[str]:
    body: list[list[str]] = []
    for row in rows:
        if row.state is JournalFetchState.NOT_STARTED:
            body.append([row.key, _NOTHING, _NOTHING, _NOTHING])
        else:
            body.append(
                [
                    row.key,
                    _number(row.excluded),
                    _number(row.out_of_scope),
                    _number(row.unparseable),
                ]
            )
    started = [row for row in rows if row.state is not JournalFetchState.NOT_STARTED]
    body.append(
        [
            "合計",
            _number(sum(row.excluded for row in started)),
            _number(sum(row.out_of_scope for row in started)),
            _number(sum(row.unparseable for row in started)),
        ]
    )
    return render_table(["期刊", "排除", "收錄範圍外", "解析失敗"], body, align="lrrr")


def _comparison_cells(label: str, comparison: Comparison) -> list[str]:
    current = _NOTHING
    if comparison.outcome is not ComparisonOutcome.NOT_FINISHED and comparison.current is not None:
        current = _number(comparison.current)
    baseline = _NOTHING if comparison.baseline is None else _number(comparison.baseline)
    match comparison.outcome:
        case ComparisonOutcome.NOT_FINISHED:
            difference = "還沒抓完"
        case ComparisonOutcome.NO_BASELINE:
            difference = "沒有基準"
        case ComparisonOutcome.COMPARED:
            assert comparison.percent is not None
            difference = "0.0%" if comparison.percent == 0 else f"{comparison.percent:+}%"
    return [label, current, baseline, difference, _EXCEEDS_MARK if comparison.exceeds else ""]


def _comparison_table(report: ImportReport) -> list[str]:
    body = [_comparison_cells(row.key, row.comparison) for row in report.rows]
    body.append(_comparison_cells("有基準的合計", report.total))
    return render_table(["期刊", "本次", "基準", "差異", ""], body, align="lrrrl")
