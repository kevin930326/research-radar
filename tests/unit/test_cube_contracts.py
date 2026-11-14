"""計數方塊的列（`domain/contracts.py`）與 `build_cube_rows`（`domain/cube.py`）
（Story 1.10、ADR-0011）。
"""

from typing import Any

import pytest
from pydantic import ValidationError

from radar.domain.contracts import CubeDim, CubeRow
from radar.domain.cube import CountedCell, UnknownJournalError, build_cube_rows
from radar.domain.domain_config import Journal, JournalsConfig
from radar.domain.papers import AbstractSource

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


def _row(**overrides: Any) -> CubeRow:
    fields: dict[str, Any] = {"journal": "misq", "year": 2020, "dim": "total", "n": 3}
    fields.update(overrides)
    return CubeRow.model_validate(fields)


# --- CubeDim、CubeRow ---------------------------------------------------------------------


def test_cube_dim_has_exactly_the_six_kinds() -> None:
    assert [dim.value for dim in CubeDim] == [
        "total",
        "with_abstract",
        "pending_classification",
        "topic",
        "topic_pair",
        "method",
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"dim": "total"},
        {"dim": "with_abstract"},
        {"dim": "pending_classification"},
        {"dim": "topic", "key_a": "erp"},
        {"dim": "method", "key_a": "survey"},
        {"dim": "topic_pair", "key_a": "erp", "key_b": "platform"},
        {"dim": "topic", "key_a": "unclassified"},
        {"n": 0},
        {"journal": "j2_x"},
    ],
)
def test_valid_rows(fields: dict[str, Any]) -> None:
    row = _row(**fields)

    assert row.dim is CubeDim(fields.get("dim", "total"))


@pytest.mark.parametrize(
    "fields",
    [
        # 不帶 key 的三種帶了 key
        {"dim": "total", "key_a": "erp"},
        {"dim": "with_abstract", "key_b": "erp"},
        {"dim": "pending_classification", "key_a": "a", "key_b": "b"},
        # topic、method 沒有 key_a 或多了 key_b
        {"dim": "topic"},
        {"dim": "method", "key_a": "survey", "key_b": "x"},
        {"dim": "topic", "key_b": "erp"},
        # topic_pair：少一個、順序不對、相同
        {"dim": "topic_pair", "key_a": "erp"},
        {"dim": "topic_pair", "key_a": "platform", "key_b": "erp"},
        {"dim": "topic_pair", "key_a": "erp", "key_b": "erp"},
        # 空字串的 key
        {"dim": "topic", "key_a": ""},
        # n < 0、不認得的 dim、多餘的欄位
        {"n": -1},
        {"dim": "share"},
        {"ratio": 0.5},
        # 期刊代號的格式與長度
        {"journal": "MISQ"},
        {"journal": "1misq"},
        {"journal": "0276-7783"},
        {"journal": "a" * 65},
    ],
)
def test_invalid_rows(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _row(**fields)


def test_row_is_frozen() -> None:
    row = _row()

    with pytest.raises(ValidationError):
        row.n = 4  # type: ignore[misc]


# --- build_cube_rows ----------------------------------------------------------------------


def test_build_cube_rows_sums_total_and_with_abstract() -> None:
    cells = [
        CountedCell(MISQ_ISSN, 2020, OPENALEX, 3),
        CountedCell(MISQ_ISSN, 2020, AbstractSource.SEMANTIC_SCHOLAR, 1),
        CountedCell(MISQ_ISSN, 2020, None, 2),
        CountedCell(MISQ_ISSN, 2021, OPENALEX, 1),
    ]

    assert build_cube_rows(cells, JOURNALS) == (
        CubeRow(journal="misq", year=2020, dim=CubeDim.TOTAL, n=6),
        CubeRow(journal="misq", year=2020, dim=CubeDim.WITH_ABSTRACT, n=4),
        CubeRow(journal="misq", year=2021, dim=CubeDim.TOTAL, n=1),
        CubeRow(journal="misq", year=2021, dim=CubeDim.WITH_ABSTRACT, n=1),
    )


def test_with_abstract_zero_is_still_produced() -> None:
    rows = build_cube_rows([CountedCell(ISR_ISSN, 2019, None, 2)], JOURNALS)

    assert rows == (
        CubeRow(journal="isr", year=2019, dim=CubeDim.TOTAL, n=2),
        CubeRow(journal="isr", year=2019, dim=CubeDim.WITH_ABSTRACT, n=0),
    )


def test_rows_follow_config_order_then_year_then_total_first() -> None:
    # 輸入照 ISSN-L 排（repo 的排序），輸出照設定的順序：misq、isr、io。
    cells = [
        CountedCell(ISR_ISSN, 2019, OPENALEX, 1),
        CountedCell(IO_ISSN, 2018, None, 1),
        CountedCell(MISQ_ISSN, 2021, OPENALEX, 1),
        CountedCell(MISQ_ISSN, 2019, None, 1),
    ]

    rows = build_cube_rows(cells, JOURNALS)

    assert [(row.journal, row.year, row.dim.value) for row in rows] == [
        ("misq", 2019, "total"),
        ("misq", 2019, "with_abstract"),
        ("misq", 2021, "total"),
        ("misq", 2021, "with_abstract"),
        ("isr", 2019, "total"),
        ("isr", 2019, "with_abstract"),
        ("io", 2018, "total"),
        ("io", 2018, "with_abstract"),
    ]


def test_no_cells_no_rows() -> None:
    assert build_cube_rows([], JOURNALS) == ()


def test_unknown_issn_ls_are_all_listed() -> None:
    cells = [
        CountedCell("2222-2222", 2020, None, 1),
        CountedCell(MISQ_ISSN, 2020, OPENALEX, 1),
        CountedCell("1111-1111", 2020, OPENALEX, 1),
        CountedCell("2222-2222", 2021, OPENALEX, 1),
    ]

    with pytest.raises(UnknownJournalError) as caught:
        build_cube_rows(cells, JOURNALS)

    assert caught.value.issn_ls == ("1111-1111", "2222-2222")
    assert "1111-1111、2222-2222" in str(caught.value)
