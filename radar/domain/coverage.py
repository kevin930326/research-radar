"""覆蓋率報表：`radar coverage` 印的內容（epics Story 1.10、FR-11、FR-40、ADR-0027）。

純函式：輸入是最新已發佈的快照與 `repo/stats.py` 的計數（同一個交易讀的），輸出是要印的每一行。
內容依序：快照編號與截至日期（台北時間）、依期刊 × 年份的 `total`、`with_abstract`，以及依目前的
摘要來源分的篇數。所有數字都由同一份計數推出，所以彼此一定對得起來（「沒有摘要」不會是負數）。
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from radar.domain.contracts import CubeDim, CubeRow
from radar.domain.cube import CountedCell, build_cube_rows
from radar.domain.domain_config import JournalsConfig
from radar.domain.papers import ABSTRACT_SOURCE_PRIORITY, AbstractSource
from radar.domain.snapshot import Snapshot, data_cutoff_date
from radar.domain.text_table import render_table


@dataclass(frozen=True)
class CoverageReport:
    """一份覆蓋率報表。

    - `journal_keys`：設定裡的期刊代號，照設定的順序（沒有論文的期刊也在）。
    - `cube`：計數方塊的列（目前只有 `total`、`with_abstract`）。
    - `by_source`：期刊代號 → 目前的摘要來源 → 篇數（跨年份加總；沒有摘要的不在裡面）。
    """

    snapshot_number: int
    cutoff_date: date
    journal_keys: tuple[str, ...]
    cube: tuple[CubeRow, ...]
    by_source: Mapping[str, Mapping[AbstractSource, int]]


def build_coverage_report(
    snapshot: Snapshot, cells: Iterable[CountedCell], journals: JournalsConfig
) -> CoverageReport:
    """把快照與計數組成覆蓋率報表。

    Raises:
        ValueError: 快照沒有 `as_of`（還沒發佈）。
        UnknownJournalError: 計數裡有設定沒有的期刊（`build_cube_rows` 丟的）。
    """
    if snapshot.as_of is None:
        raise ValueError(f"快照 {snapshot.number} 沒有截至時間（還沒發佈）")
    cells = tuple(cells)
    cube = build_cube_rows(cells, journals)
    key_of = {journal.issn_l: journal.key for journal in journals.journals}
    by_source: dict[str, dict[AbstractSource, int]] = {}
    for cell in cells:
        if cell.abstract_source is None:
            continue
        sources = by_source.setdefault(key_of[cell.journal_issn_l], {})
        sources[cell.abstract_source] = sources.get(cell.abstract_source, 0) + cell.n
    return CoverageReport(
        snapshot_number=snapshot.number,
        cutoff_date=data_cutoff_date(snapshot.as_of),
        journal_keys=tuple(journal.key for journal in journals.journals),
        cube=cube,
        by_source=by_source,
    )


def render_coverage_report(report: CoverageReport) -> list[str]:
    """覆蓋率報表要印的每一行（表格依顯示寬度對齊、數字有千分位）。"""
    lines = [f"快照 {report.snapshot_number}：截至 {report.cutoff_date.isoformat()}（台北時間）"]
    lines += ["", "收錄篇數（total）", *_years_table(report, CubeDim.TOTAL)]
    lines += ["", "有摘要的篇數（with_abstract）", *_years_table(report, CubeDim.WITH_ABSTRACT)]
    lines += ["", "依摘要來源分的篇數（目前的摘要）", *_sources_table(report)]
    return lines


def _number(value: int) -> str:
    return f"{value:,}"


def _counts(report: CoverageReport, dim: CubeDim) -> dict[tuple[str, int], int]:
    return {(row.journal, row.year): row.n for row in report.cube if row.dim is dim}


def _years_table(report: CoverageReport, dim: CubeDim) -> list[str]:
    counts = _counts(report, dim)
    seen = [year for _journal, year in counts]
    years = list(range(min(seen), max(seen) + 1)) if seen else []
    body: list[list[str]] = []
    for key in report.journal_keys:
        row = [counts.get((key, year), 0) for year in years]
        body.append([key, *map(_number, row), _number(sum(row))])
    totals = [sum(counts.get((key, year), 0) for key in report.journal_keys) for year in years]
    body.append(["合計", *map(_number, totals), _number(sum(totals))])
    header = ["期刊", *map(str, years), "合計"]
    return render_table(header, body, align="l" + "r" * (len(years) + 1))


def _source_cells(total: int, sources: Mapping[AbstractSource, int]) -> list[int]:
    listed = [sources.get(source, 0) for source in ABSTRACT_SOURCE_PRIORITY]
    return [*listed, total - sum(listed), total]


def _sources_table(report: CoverageReport) -> list[str]:
    totals = _counts(report, CubeDim.TOTAL)
    body: list[list[str]] = []
    sums: list[int] = [0] * (len(ABSTRACT_SOURCE_PRIORITY) + 2)
    for key in report.journal_keys:
        total = sum(n for (journal, _year), n in totals.items() if journal == key)
        cells = _source_cells(total, report.by_source.get(key, {}))
        sums = [left + right for left, right in zip(sums, cells, strict=True)]
        body.append([key, *map(_number, cells)])
    body.append(["合計", *map(_number, sums)])
    header: Sequence[str] = [
        "期刊",
        *(source.value for source in ABSTRACT_SOURCE_PRIORITY),
        "沒有摘要",
        "合計",
    ]
    return render_table(header, body, align="l" + "r" * (len(header) - 1))
