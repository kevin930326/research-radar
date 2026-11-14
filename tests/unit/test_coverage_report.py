"""覆蓋率報表的純函式（`domain/coverage.py`）與資料截止日（`domain/snapshot.py`）（Story 1.10）。"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from radar.domain.contracts import CubeDim, CubeRow
from radar.domain.coverage import build_coverage_report, render_coverage_report
from radar.domain.cube import CountedCell, UnknownJournalError
from radar.domain.domain_config import Journal, JournalsConfig
from radar.domain.papers import AbstractSource
from radar.domain.snapshot import Snapshot, SnapshotStatus, data_cutoff_date
from radar.domain.text_table import display_width

MISQ_ISSN = "0276-7783"
ISR_ISSN = "1047-7047"
IO_ISSN = "1471-7727"
JOURNALS = JournalsConfig(
    start_year=2016,
    journals=(
        Journal(key="misq", name="MIS Quarterly", issn_l=MISQ_ISSN),
        Journal(key="isr", name="Information Systems Research", issn_l=ISR_ISSN),
        Journal(key="io", name="Information and Organization", issn_l=IO_ISSN),
    ),
)
OPENALEX = AbstractSource.OPENALEX
AS_OF = datetime(2026, 11, 16, 2, 30, tzinfo=UTC)


def _snapshot(number: int = 1, as_of: datetime | None = AS_OF) -> Snapshot:
    published = as_of is not None
    return Snapshot(
        id=uuid.UUID("019a0000-0000-7000-8000-000000000002"),
        number=number,
        weekly_run_id=uuid.UUID("019a0000-0000-7000-8000-000000000001"),
        status=SnapshotStatus.PUBLISHED if published else SnapshotStatus.BUILDING,
        as_of=as_of,
        created_at=AS_OF - timedelta(minutes=5),
    )


# Design Notes 的例子：misq 2020 有 4 篇（3 篇有摘要）、2021 有 1 篇；
# isr 2019 有 2 篇（1 篇有摘要）。
EXAMPLE_CELLS = [
    CountedCell(MISQ_ISSN, 2020, OPENALEX, 3),
    CountedCell(MISQ_ISSN, 2020, None, 1),
    CountedCell(MISQ_ISSN, 2021, OPENALEX, 1),
    CountedCell(ISR_ISSN, 2019, OPENALEX, 1),
    CountedCell(ISR_ISSN, 2019, None, 1),
]
EXAMPLE_LINES = [
    "快照 1：截至 2026-11-16（台北時間）",
    "",
    "收錄篇數（total）",
    "期刊  2019  2020  2021  合計",
    "misq     0     4     1     5",
    "isr      2     0     0     2",
    "io       0     0     0     0",
    "合計     2     4     1     7",
    "",
    "有摘要的篇數（with_abstract）",
    "期刊  2019  2020  2021  合計",
    "misq     0     3     1     4",
    "isr      1     0     0     1",
    "io       0     0     0     0",
    "合計     1     3     1     5",
    "",
    "依摘要來源分的篇數（目前的摘要）",
    "期刊  openalex  semantic_scholar  沒有摘要  合計",
    "misq         4                 0         1     5",
    "isr          1                 0         1     2",
    "io           0                 0         0     0",
    "合計         5                 0         2     7",
]


def test_render_matches_the_design_notes_example() -> None:
    report = build_coverage_report(_snapshot(), EXAMPLE_CELLS, JOURNALS)

    assert render_coverage_report(report) == EXAMPLE_LINES


def test_report_fields() -> None:
    report = build_coverage_report(_snapshot(number=3), EXAMPLE_CELLS, JOURNALS)

    assert report.snapshot_number == 3
    assert report.cutoff_date == date(2026, 11, 16)
    assert report.journal_keys == ("misq", "isr", "io")
    assert report.cube[:2] == (
        CubeRow(journal="misq", year=2020, dim=CubeDim.TOTAL, n=4),
        CubeRow(journal="misq", year=2020, dim=CubeDim.WITH_ABSTRACT, n=3),
    )
    assert report.by_source == {"misq": {OPENALEX: 4}, "isr": {OPENALEX: 1}}


def test_no_counted_papers_has_only_the_total_column() -> None:
    report = build_coverage_report(_snapshot(), [], JOURNALS)

    assert render_coverage_report(report) == [
        "快照 1：截至 2026-11-16（台北時間）",
        "",
        "收錄篇數（total）",
        "期刊  合計",
        "misq     0",
        "isr      0",
        "io       0",
        "合計     0",
        "",
        "有摘要的篇數（with_abstract）",
        "期刊  合計",
        "misq     0",
        "isr      0",
        "io       0",
        "合計     0",
        "",
        "依摘要來源分的篇數（目前的摘要）",
        "期刊  openalex  semantic_scholar  沒有摘要  合計",
        "misq         0                 0         0     0",
        "isr          0                 0         0     0",
        "io           0                 0         0     0",
        "合計         0                 0         0     0",
    ]


def test_journal_without_papers_prints_zero_and_years_fill_gaps() -> None:
    cells = [CountedCell(IO_ISSN, 2016, None, 1), CountedCell(IO_ISSN, 2019, OPENALEX, 2)]

    lines = render_coverage_report(build_coverage_report(_snapshot(), cells, JOURNALS))

    assert lines[3:8] == [
        "期刊  2016  2017  2018  2019  合計",
        "misq     0     0     0     0     0",
        "isr      0     0     0     0     0",
        "io       1     0     0     2     3",
        "合計     1     0     0     2     3",
    ]


def test_numbers_have_thousands_separators_and_line_up_by_display_width() -> None:
    cells = [
        CountedCell(MISQ_ISSN, 2020, OPENALEX, 1234),
        CountedCell(MISQ_ISSN, 2020, None, 5),
        CountedCell(ISR_ISSN, 2020, AbstractSource.SEMANTIC_SCHOLAR, 7),
    ]

    lines = render_coverage_report(build_coverage_report(_snapshot(), cells, JOURNALS))

    assert lines[3:8] == [
        "期刊   2020   合計",
        "misq  1,239  1,239",
        "isr       7      7",
        "io        0      0",
        "合計  1,246  1,246",
    ]
    sources = lines[lines.index("依摘要來源分的篇數（目前的摘要）") + 1 :]
    assert sources[1] == "misq     1,234                 0         5  1,239"
    assert sources[2] == "isr          0                 7         0      7"
    # 每張表靠右對齊：每一行結尾的顯示寬度相同（全形字算 2）；用字元數算就對不齊。
    for table in (lines[3:8], lines[10:15], sources):
        assert len({display_width(line) for line in table}) == 1, table
        assert len({len(line) for line in table}) > 1, table


def test_unknown_journal_is_raised() -> None:
    with pytest.raises(UnknownJournalError) as caught:
        build_coverage_report(_snapshot(), [CountedCell("0960-085X", 2020, None, 1)], JOURNALS)

    assert caught.value.issn_ls == ("0960-085X",)


def test_snapshot_without_as_of_is_rejected() -> None:
    with pytest.raises(ValueError, match="截至時間"):
        build_coverage_report(_snapshot(as_of=None), EXAMPLE_CELLS, JOURNALS)


# --- 資料截止日 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        # UTC 還在 6 月 30 日，台北已經是 7 月 1 日。
        (datetime(2026, 6, 30, 16, 0, tzinfo=UTC), date(2026, 7, 1)),
        (datetime(2026, 6, 30, 15, 59, 59, tzinfo=UTC), date(2026, 6, 30)),
        (datetime(2026, 12, 31, 16, 0, tzinfo=UTC), date(2027, 1, 1)),
        (AS_OF, date(2026, 11, 16)),
    ],
)
def test_data_cutoff_date_uses_taipei_time(as_of: datetime, expected: date) -> None:
    assert data_cutoff_date(as_of) == expected


def test_report_cutoff_date_is_the_taipei_date_of_as_of() -> None:
    # UTC 還是 6 月 30 日、台北已經是 7 月 1 日：報表第一行要印台北的日期。
    as_of = datetime(2026, 6, 30, 16, 0, tzinfo=UTC)

    report = build_coverage_report(_snapshot(as_of=as_of), EXAMPLE_CELLS, JOURNALS)

    assert report.cutoff_date == date(2026, 7, 1)
    assert render_coverage_report(report)[0] == "快照 1：截至 2026-07-01（台北時間）"


def test_data_cutoff_date_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="時區"):
        data_cutoff_date(datetime(2026, 6, 30, 16, 0))
