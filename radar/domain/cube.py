"""計數方塊：把 `repo/stats.py` 數出來的最細計數換成計數方塊的列（AD-5、AD-19、
ADR-0011「計數方塊」）。

- `paper` 只存 ISSN-L；期刊代號在設定裡，`repo` 不讀設定（AD-1），所以在這裡用 services 傳進來的
  `JournalsConfig` 換。設定裡沒有的 ISSN-L 直接報錯，不默默少算。
- 產生 `total`、`with_abstract`（`CountedCell`），以及 `topic`、`method`（`CountedKeyCell`，Story
  3.13）；`pending_classification`、`topic_pair` 由之後的 Story 補。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from radar.domain.contracts import CubeDim, CubeRow
from radar.domain.domain_config import JournalsConfig
from radar.domain.papers import AbstractSource


@dataclass(frozen=True)
class CountedCell:
    """`repo/stats.py` 回傳的最細計數：某本期刊、某一年、目前的摘要來自哪裡，有幾篇。

    `abstract_source` 是 None 表示沒有目前的摘要（`paper_abstract` 沒有 `is_current` 那一筆）。
    """

    journal_issn_l: str
    year: int
    abstract_source: AbstractSource | None
    n: int


_KEY_DIMS = frozenset({CubeDim.TOPIC, CubeDim.METHOD})


@dataclass(frozen=True)
class CountedKeyCell:
    """`repo/stats.list_cube_key_counts` 回傳的計數：某本期刊、某一年，有效分類有這個主題（或方法）
    的篇數（Story 3.13）。`dim` 只能是 `topic` 或 `method`；`key` 是分類表的代號，主題可以是
    `unclassified`。

    Raises:
        ValueError: `dim` 不是 `topic` 或 `method`。
    """

    journal_issn_l: str
    year: int
    dim: CubeDim
    key: str
    n: int

    def __post_init__(self) -> None:
        dim = CubeDim(self.dim)
        if dim not in _KEY_DIMS:
            raise ValueError(f"CountedKeyCell 的 dim 只能是 topic 或 method（現在是 {dim}）")
        object.__setattr__(self, "dim", dim)


class UnknownJournalError(Exception):
    """計數裡有設定（`config/journals.yaml`）沒有的期刊。"""

    def __init__(self, issn_ls: Iterable[str]) -> None:
        self.issn_ls: tuple[str, ...] = tuple(sorted(set(issn_ls)))
        super().__init__(f"計數裡有設定沒有的期刊（ISSN-L：{'、'.join(self.issn_ls)}）")


def build_cube_rows(
    cells: Iterable[CountedCell],
    journals: JournalsConfig,
    *,
    key_cells: Iterable[CountedKeyCell] = (),
) -> tuple[CubeRow, ...]:
    """每個出現過的（期刊, 年份）產生 `total`（全部加總）與 `with_abstract`（有目前的摘要的加總，
    0 也產生）兩列；有 `key_cells` 時，同一個（期刊, 年份）接著是每個主題一列（`topic`）、再每個方法
    一列（`method`），各依 key 的 "C" 定序（Python 的字串排序）。沒有論文的 key 不產生列。

    排序：期刊照設定的順序、年份遞增、同一格依 `total`、`with_abstract`、`topic`、`method`。

    Raises:
        UnknownJournalError: `cells` 或 `key_cells` 有設定裡沒有的 ISSN-L（列出全部）。
    """
    key_of = {journal.issn_l: journal.key for journal in journals.journals}
    order = {journal.key: index for index, journal in enumerate(journals.journals)}
    totals: dict[tuple[str, int], int] = {}
    with_abstract: dict[tuple[str, int], int] = {}
    keyed: dict[tuple[str, int], dict[tuple[CubeDim, str], int]] = {}
    unknown: set[str] = set()
    for cell in cells:
        key = key_of.get(cell.journal_issn_l)
        if key is None:
            unknown.add(cell.journal_issn_l)
            continue
        slot = (key, cell.year)
        totals[slot] = totals.get(slot, 0) + cell.n
        with_abstract[slot] = with_abstract.get(slot, 0)
        if cell.abstract_source is not None:
            with_abstract[slot] += cell.n
    for key_cell in key_cells:
        key = key_of.get(key_cell.journal_issn_l)
        if key is None:
            unknown.add(key_cell.journal_issn_l)
            continue
        counts = keyed.setdefault((key, key_cell.year), {})
        cell_key = (key_cell.dim, key_cell.key)
        counts[cell_key] = counts.get(cell_key, 0) + key_cell.n
    if unknown:
        raise UnknownJournalError(unknown)
    rows: list[CubeRow] = []
    for journal, year in sorted(
        totals.keys() | keyed.keys(), key=lambda slot: (order[slot[0]], slot[1])
    ):
        if (journal, year) in totals:
            rows.append(
                CubeRow(journal=journal, year=year, dim=CubeDim.TOTAL, n=totals[journal, year])
            )
            rows.append(
                CubeRow(
                    journal=journal,
                    year=year,
                    dim=CubeDim.WITH_ABSTRACT,
                    n=with_abstract[journal, year],
                )
            )
        counts = keyed.get((journal, year), {})
        for dim in (CubeDim.TOPIC, CubeDim.METHOD):
            rows += [
                CubeRow(journal=journal, year=year, dim=dim, key_a=key, n=counts[dim, key])
                for key in sorted(key for cell_dim, key in counts if cell_dim is dim)
            ]
    return tuple(rows)
