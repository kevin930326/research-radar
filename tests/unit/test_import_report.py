"""`domain/import_report.py`：匯入報表的組成與輸出（純函式，Story 1.9、ADR-0026）。

年份欄、合計列與合計欄、還沒開始的 `-`、差異與正負號、5% 邊界（剛好 5% 不標、多一篇就標）、
沒有基準、基準 0、還沒抓完、設定外的期刊排後面、全形字對齊、套用時間與 10 分鐘加註、狀態。
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from radar.domain.domain_config import Journal, JournalsConfig
from radar.domain.import_report import (
    APPLY_SLOW_THRESHOLD,
    ComparisonOutcome,
    ImportBaseline,
    build_import_report,
    compare_with_baseline,
    display_width,
    render_import_report,
)
from radar.domain.snapshot import Snapshot, SnapshotStatus
from radar.domain.weekly_run import JournalProgress, WeeklyRun, WeeklyRunStatus

RUN_ID = uuid.UUID("019a0000-0000-7000-8000-000000000001")
SNAPSHOT_ID = uuid.UUID("019a0000-0000-7000-8000-000000000002")
CREATED = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
FINISHED = datetime(2026, 11, 16, 3, 20, 5, tzinfo=UTC)
APPLY_START = datetime(2026, 11, 16, 3, 20, 6, tzinfo=UTC)

MISQ = Journal(key="misq", name="MIS Quarterly", issn_l="0276-7783")
ISR = Journal(key="isr", name="Information Systems Research", issn_l="1047-7047")
IO = Journal(key="io", name="Information and Organization", issn_l="1471-7727")
JOURNALS = JournalsConfig(start_year=2016, journals=(MISQ, ISR, IO))
BASELINE = ImportBaseline(
    year_from=2016,
    year_to=2025,
    journals={"misq": {2016: 50, 2017: 50}, "isr": {2018: 50}},
)


def _run(
    status: WeeklyRunStatus = WeeklyRunStatus.PUBLISHED, finished_at: datetime | None = FINISHED
) -> WeeklyRun:
    return WeeklyRun(
        id=RUN_ID,
        status=status,
        fetch_finished_at=finished_at,
        created_at=CREATED,
        updated_at=CREATED,
    )


def _progress(
    key: str,
    by_year: Mapping[int, int],
    *,
    finished: bool = True,
    excluded: int = 0,
    out_of_scope: int = 0,
    unparseable: int = 0,
) -> JournalProgress:
    return JournalProgress(
        journal_key=key,
        next_cursor=None if finished else "cursor-2",
        finished=finished,
        pages=1,
        fetched=sum(by_year.values()) + unparseable,
        fetched_by_year=by_year,
        staged=sum(by_year.values()) - out_of_scope,
        excluded=excluded,
        out_of_scope=out_of_scope,
        unparseable=unparseable,
    )


def _snapshot(seconds: float, *, number: int = 1) -> Snapshot:
    return Snapshot(
        id=SNAPSHOT_ID,
        number=number,
        weekly_run_id=RUN_ID,
        status=SnapshotStatus.PUBLISHED,
        as_of=APPLY_START + timedelta(seconds=seconds),
        created_at=APPLY_START,
    )


def _render(
    progress: Mapping[str, JournalProgress],
    *,
    run: WeeklyRun | None = None,
    snapshot: Snapshot | None = None,
    journals: JournalsConfig = JOURNALS,
    baseline: ImportBaseline = BASELINE,
) -> list[str]:
    report = build_import_report(run or _run(), progress, snapshot, journals, baseline)
    return render_import_report(report)


# --- 整份輸出 ---------------------------------------------------------------------------------


def test_published_report_lines() -> None:
    # 一本差 +2%、一本差 +40%、一本沒有基準（另有 2015 年的論文，不在比對範圍）。
    progress = {
        "misq": _progress("misq", {2016: 50, 2017: 52}, excluded=3, out_of_scope=4),
        "isr": _progress("isr", {2018: 70}, unparseable=1),
        "io": _progress("io", {2015: 3, 2017: 1234}, excluded=1, out_of_scope=2),
    }

    lines = _render(progress, snapshot=_snapshot(12.34))

    assert lines == [
        f"每週更新 {RUN_ID}",
        "狀態：已發佈",
        "抓完時間：2026-11-16 03:20:05（UTC）",
        "快照 1：截至 2026-11-16 03:20:18（UTC）；套用 12.3 秒",
        "",
        "每本期刊每年的過濾前篇數",
        "期刊  2015  2016   2017  2018   合計",
        "misq     0    50     52     0    102",
        "isr      0     0      0    70     70",
        "io       3     0  1,234     0  1,237",
        "合計     3    50  1,286    70  1,409",
        "",
        "依期刊的排除篇數",
        "期刊  排除  收錄範圍外  解析失敗",
        "misq     3           4         0",
        "isr      0           0         1",
        "io       1           2         0",
        "合計     4           6         1",
        "",
        "和 OQ-02 基準比對（2016–2025 過濾前篇數）",
        "期刊           本次  基準      差異",
        "misq            102   100     +2.0%",
        "isr              70    50    +40.0%  超過 5%",
        "io            1,234     -  沒有基準",
        "有基準的合計    172   150    +14.7%  超過 5%",
        "超過 5% 的期刊：isr",
    ]


def test_fetching_report_marks_unfinished_and_not_started_journals() -> None:
    # 一本抓完、一本抓到一半、一本還沒開始；設定外的期刊排在最後。
    progress = {
        "misq": _progress("misq", {2016: 49, 2017: 49}),
        "isr": _progress("isr", {2018: 10}, finished=False),
        "zzz_extra": _progress("zzz_extra", {2016: 1}),
        "aaa_extra": _progress("aaa_extra", {2016: 2}),
    }

    lines = _render(progress, run=_run(WeeklyRunStatus.FETCHING, None))

    assert lines == [
        f"每週更新 {RUN_ID}",
        "狀態：抓取中 3／5 本抓完",
        "抓完時間：還沒抓完",
        "",
        "每本期刊每年的過濾前篇數",
        "期刊       2016  2017  2018  合計",
        "misq         49    49     0    98",
        "isr           0     0    10    10",
        "io            -     -     -     -",
        "aaa_extra     2     0     0     2",
        "zzz_extra     1     0     0     1",
        "合計         52    49    10   111",
        "",
        "依期刊的排除篇數",
        "期刊       排除  收錄範圍外  解析失敗",
        "misq          0           0         0",
        "isr           0           0         0",
        "io            -           -         -",
        "aaa_extra     0           0         0",
        "zzz_extra     0           0         0",
        "合計          0           0         0",
        "",
        "和 OQ-02 基準比對（2016–2025 過濾前篇數）",
        "期刊          本次  基準      差異",
        "misq            98   100     -2.0%",
        "isr              -    50  還沒抓完",
        "io               -     -  還沒抓完",
        "aaa_extra        2     -  沒有基準",
        "zzz_extra        1     -  沒有基準",
        "有基準的合計     -   150  還沒抓完",
        "超過 5% 的期刊：沒有",
    ]


def test_nothing_fetched_yet_has_no_year_columns() -> None:
    lines = _render({}, run=_run(WeeklyRunStatus.FETCHING, None))

    assert lines[1] == "狀態：抓取中 0／3 本抓完"
    years = lines[lines.index("每本期刊每年的過濾前篇數") + 1 :][:5]
    assert years == ["期刊  合計", "misq     -", "isr      -", "io       -", "合計     0"]
    assert lines[-1] == "超過 5% 的期刊：沒有"


def test_finished_but_not_applied_status() -> None:
    progress = {key: _progress(key, {2016: 1}) for key in ("misq", "isr", "io")}

    lines = _render(progress, run=_run(WeeklyRunStatus.FETCHING, FINISHED))

    assert lines[1] == "狀態：已抓完還沒套用"
    assert lines[2] == "抓完時間：2026-11-16 03:20:05（UTC）"
    assert not any(line.startswith("快照") for line in lines)


# --- 5% 邊界、沒有基準、基準 0 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "percent", "exceeds"),
    [
        (100, "0.0", False),
        (105, "5.0", False),  # 剛好 5% 不標
        (106, "6.0", True),  # 多一篇就標
        (95, "-5.0", False),
        (94, "-6.0", True),
    ],
)
def test_five_percent_boundary(current: int, percent: str, exceeds: bool) -> None:
    comparison = compare_with_baseline(current, 100, finished=True)

    assert comparison.outcome is ComparisonOutcome.COMPARED
    assert comparison.percent == Decimal(percent)
    assert comparison.exceeds is exceeds


def test_five_percent_boundary_uses_integers_not_rounded_percent() -> None:
    # 基準 3001：本次 3151 是 +4.998%（印 +5.0%）不標；3152 是 +5.03% 要標。
    below = compare_with_baseline(3151, 3001, finished=True)
    above = compare_with_baseline(3152, 3001, finished=True)

    assert (below.percent, below.exceeds) == (Decimal("5.0"), False)
    assert (above.percent, above.exceeds) == (Decimal("5.0"), True)


def test_rendered_signs_and_boundary() -> None:
    baseline = ImportBaseline(
        year_from=2016,
        year_to=2025,
        journals={"misq": {2016: 100}, "isr": {2016: 100}, "io": {2016: 100}},
    )
    progress = {
        "misq": _progress("misq", {2016: 105}),
        "isr": _progress("isr", {2016: 94}),
        "io": _progress("io", {2016: 100}),
    }

    lines = _render(progress, baseline=baseline)

    start = lines.index("和 OQ-02 基準比對（2016–2025 過濾前篇數）")
    assert lines[start + 1 :] == [
        "期刊          本次  基準   差異",
        "misq           105   100  +5.0%",
        "isr             94   100  -6.0%  超過 5%",
        "io             100   100   0.0%",
        "有基準的合計   299   300  -0.3%",
        "超過 5% 的期刊：isr",
    ]


def test_zero_or_missing_baseline_is_not_compared() -> None:
    assert compare_with_baseline(10, 0, finished=True).outcome is ComparisonOutcome.NO_BASELINE
    assert compare_with_baseline(10, None, finished=True).outcome is ComparisonOutcome.NO_BASELINE
    unfinished = compare_with_baseline(10, 0, finished=False)
    assert (unfinished.outcome, unfinished.exceeds) == (ComparisonOutcome.NOT_FINISHED, False)

    baseline = ImportBaseline(year_from=2016, year_to=2025, journals={"misq": {2016: 0}})
    lines = _render(
        {"misq": _progress("misq", {2016: 7})},
        run=_run(WeeklyRunStatus.FETCHING, finished_at=None),
        baseline=baseline,
    )

    # 唯一一本有列的基準是 0：合計也沒有基準；抓取中、還沒開始的兩本印「還沒抓完」。
    assert lines[-6:] == [
        "期刊          本次  基準      差異",
        "misq             7     0  沒有基準",
        "isr              -     -  還沒抓完",
        "io               -     -  還沒抓完",
        "有基準的合計     -     -  沒有基準",
        "超過 5% 的期刊：沒有",
    ]


def test_finished_run_omits_journals_added_to_config_later() -> None:
    # 抓完之後才在 journals.yaml 加一本（NFR-9）：那本沒有進度，不列、不算「還沒抓完」，
    # 合計照常比對，狀態列不出現「抓取中」。
    later = Journal(key="later", name="Added Later", issn_l="2345-6780")
    journals = JournalsConfig(start_year=2016, journals=(MISQ, later, ISR))
    progress = {
        "misq": _progress("misq", {2016: 50, 2017: 50}),
        "isr": _progress("isr", {2018: 50}),
    }

    report = build_import_report(_run(), progress, _snapshot(1), journals, BASELINE)

    assert [row.key for row in report.rows] == ["misq", "isr"]
    assert (report.total.outcome, report.total.current, report.total.baseline) == (
        ComparisonOutcome.COMPARED,
        150,
        150,
    )
    lines = render_import_report(report)
    assert lines[1] == "狀態：已發佈"
    assert not any("還沒抓完" in line or "later" in line for line in lines)


def test_comparison_counts_only_baseline_years() -> None:
    progress = {
        "misq": _progress("misq", {2015: 40, 2016: 50, 2017: 50, 2026: 9}),
        "isr": _progress("isr", {2018: 50}),
        "io": _progress("io", {}),
    }

    report = build_import_report(_run(), progress, None, JOURNALS, BASELINE)

    misq = report.rows[0].comparison
    assert (misq.current, misq.baseline, misq.percent) == (100, 100, Decimal("0.0"))
    assert (report.total.current, report.total.baseline) == (150, 150)
    assert report.exceeded == ()


# --- 套用時間 ---------------------------------------------------------------------------------


def test_apply_time_over_ten_minutes_is_annotated() -> None:
    progress = {key: _progress(key, {2016: 1}) for key in ("misq", "isr", "io")}
    limit = APPLY_SLOW_THRESHOLD.total_seconds()

    exactly = _render(progress, snapshot=_snapshot(limit))[3]
    over = _render(progress, snapshot=_snapshot(limit + 0.5, number=2))[3]

    assert exactly == "快照 1：截至 2026-11-16 03:30:06（UTC）；套用 600.0 秒"
    assert over == (
        "快照 2：截至 2026-11-16 03:30:06（UTC）；套用 600.5 秒，超過 10 分鐘（AD-19 的門檻）"
    )


def test_snapshot_apply_duration() -> None:
    assert _snapshot(3.5).apply_duration == timedelta(seconds=3.5)
    building = Snapshot(
        id=SNAPSHOT_ID,
        number=1,
        weekly_run_id=RUN_ID,
        status=SnapshotStatus.BUILDING,
        as_of=None,
        created_at=APPLY_START,
    )
    assert building.apply_duration is None
    with pytest.raises(ValueError, match="number"):
        Snapshot(
            id=SNAPSHOT_ID,
            number=0,
            weekly_run_id=RUN_ID,
            status=SnapshotStatus.BUILDING,
            as_of=None,
            created_at=APPLY_START,
        )


def test_failed_snapshot_line() -> None:
    failed = Snapshot(
        id=SNAPSHOT_ID,
        number=3,
        weekly_run_id=RUN_ID,
        status=SnapshotStatus.FAILED,
        as_of=None,
        created_at=APPLY_START,
    )
    progress = {key: _progress(key, {2016: 1}) for key in ("misq", "isr", "io")}

    lines = _render(progress, run=_run(WeeklyRunStatus.FAILED), snapshot=failed)

    assert lines[1] == "狀態：失敗"
    assert lines[3] == "快照 3：失敗"


# --- 對齊 -------------------------------------------------------------------------------------


def test_display_width_counts_full_width_as_two() -> None:
    assert display_width("io") == 2
    assert display_width("期刊") == 4
    assert display_width("超過 5%") == 7
    assert display_width("－") == 2


def test_columns_line_up_by_display_width() -> None:
    progress = {
        "misq": _progress("misq", {2016: 50, 2017: 52}, excluded=3, out_of_scope=4),
        "isr": _progress("isr", {2018: 70}, unparseable=1),
        "io": _progress("io", {2017: 1234}),
    }
    lines = _render(progress)
    start = lines.index("依期刊的排除篇數") + 1
    table = lines[start : start + 5]

    # 右對齊的欄：每一行的結尾在同一個顯示寬度（全形字算 2）。
    assert len({display_width(line) for line in table}) == 1
    assert len({len(line) for line in table}) > 1  # 用字元數算就對不齊


# --- 基準檔的模型 ---------------------------------------------------------------------------


def test_baseline_totals() -> None:
    assert BASELINE.total() == 150
    assert BASELINE.journal_total("misq") == 100
    assert BASELINE.journal_total("io") is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"year_from": 2016, "year_to": 2025, "journals": {"misq": {2015: 1}}}, "2015"),
        ({"year_from": 2016, "year_to": 2025, "journals": {"misq": {2016: -1}}}, "greater"),
        ({"year_from": 2016, "year_to": 2025, "journals": {"MISQ": {2016: 1}}}, "pattern"),
        ({"year_from": 2026, "year_to": 2025, "journals": {"misq": {2026: 1}}}, "year_from"),
        ({"year_from": 2016, "year_to": 2025, "journals": {}}, "at least 1"),
    ],
)
def test_baseline_validation(data: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ImportBaseline.model_validate(data)
