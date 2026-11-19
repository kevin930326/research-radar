"""找論文清單的 domain（Story 3.13）：不連資料庫。

- 條件的解析：期刊代號換成 ISSN-L；多值欄位去重、排序；不認得的期刊、主題、方法代碼，年份小於追蹤
  起始年或大於 9999，任一個多值參數超過 50 個值（重複的也算），都丟 `InvalidPaperListQueryError`；
  `unclassified` 只能當主題、不能當方法。
- `PaperListFilters` 只收排序去重過的值。
- 下一頁的位置：編碼後解得回來（含出版年是 None）；不是它產生的寫法（亂碼、改過、補了 `=`、前置
  零、加號、太長、年份超出範圍）一律 `ValueError`。
- `PaperListPage`：一頁最多 `PAPER_LIST_PAGE_SIZE` 篇、總數非負整數、快照要已發佈；`client-config`
  給的是同一個一頁篇數。
資料全是測試自己編的。
"""

import base64
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from radar.domain.contracts import build_client_config
from radar.domain.domain_config import Journal, JournalsConfig
from radar.domain.paper_list import (
    MAX_FILTER_VALUES,
    PAPER_LIST_PAGE_SIZE,
    InvalidPaperListQueryError,
    PaperListCursor,
    PaperListFilters,
    PaperListPage,
    PaperListQuery,
    PaperListScope,
    decode_paper_list_cursor,
    encode_paper_list_cursor,
    parse_paper_list_filters,
)
from radar.domain.paper_rows import PaperRowFacts
from radar.domain.snapshot import Snapshot, SnapshotStatus
from radar.domain.taxonomy import UNCLASSIFIED, Taxonomy, TaxonomyItem

MISQ_ISSN = "0276-7783"
ISR_ISSN = "1047-7047"
JOURNALS = JournalsConfig(
    start_year=2016,
    journals=(
        Journal(key="misq", name="MIS Quarterly", issn_l=MISQ_ISSN),
        Journal(key="isr", name="Information Systems Research", issn_l=ISR_ISSN),
    ),
)
TAXONOMY = Taxonomy(
    version=1,
    topics=(
        TaxonomyItem(key="erp", name="ERP", definition="Enterprise systems."),
        TaxonomyItem(key="health_it", name="Health IT", definition="Health information systems."),
    ),
    methods=(
        TaxonomyItem(key="experiment", name="Experiment", definition="Controlled experiments."),
        TaxonomyItem(key="survey_sem", name="Survey", definition="Surveys and SEM."),
    ),
)
PAPER_ID = uuid.UUID("018f0000-0000-7000-8000-0000000000c3")
AS_OF = datetime(2026, 11, 16, 2, 30, tzinfo=UTC)


def _parse(**fields: Any) -> PaperListFilters:
    return parse_paper_list_filters(PaperListQuery(**fields), JOURNALS, TAXONOMY)


# --- 條件的解析 ------------------------------------------------------------------------------


def test_empty_query_is_no_filter_in_tracked() -> None:
    assert _parse() == PaperListFilters(scope=PaperListScope.TRACKED)


def test_values_are_mapped_deduplicated_and_sorted() -> None:
    filters = _parse(
        journals=("misq", "isr", "misq"),
        years=(2021, 2016, 2021, 9999),
        topics=("health_it", UNCLASSIFIED, "erp", "erp"),
        methods=("survey_sem", "experiment", "survey_sem"),
        has_abstract=False,
        this_week=True,
        scope=PaperListScope.LAB,
    )

    assert filters == PaperListFilters(
        journal_issn_ls=(MISQ_ISSN, ISR_ISSN),  # ISSN-L 的排序（"0276…" < "1047…"）
        years=(2016, 2021, 9999),
        topics=("erp", "health_it", UNCLASSIFIED),
        methods=("experiment", "survey_sem"),
        has_abstract=False,
        this_week=True,
        scope=PaperListScope.LAB,
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"journals": ("nope",)},
        {"journals": ("misq", "MISQ")},
        {"journals": (MISQ_ISSN,)},  # 要用代號，不是 ISSN-L
        {"topics": ("nope",)},
        {"topics": ("survey_sem",)},  # 方法的代碼不能當主題
        {"methods": ("nope",)},
        {"methods": ("erp",)},  # 主題的代碼不能當方法
        {"methods": (UNCLASSIFIED,)},  # 方法沒有 unclassified
        {"years": (2015,)},
        {"years": (2020, 10000)},
        {"years": (-1,)},
        {"years": (True,)},  # 布林不是年份
    ],
)
def test_invalid_values_are_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(InvalidPaperListQueryError) as raised:
        _parse(**fields)

    # 訊息不帶使用者送來的值。
    assert "nope" not in str(raised.value)
    assert "MISQ" not in str(raised.value)


def test_year_bounds_follow_the_tracking_start_year() -> None:
    later = JournalsConfig(start_year=2018, journals=JOURNALS.journals)

    assert parse_paper_list_filters(PaperListQuery(years=(2018,)), later, TAXONOMY).years == (2018,)
    with pytest.raises(InvalidPaperListQueryError):
        parse_paper_list_filters(PaperListQuery(years=(2017,)), later, TAXONOMY)


@pytest.mark.parametrize(
    ("name", "value"),
    [("journals", "misq"), ("years", 2020), ("topics", "erp"), ("methods", "experiment")],
)
def test_more_than_fifty_values_are_rejected_even_if_repeated(name: str, value: object) -> None:
    assert MAX_FILTER_VALUES == 50
    allowed = _parse(**{name: (value,) * MAX_FILTER_VALUES})
    assert len(getattr(allowed, name if name != "journals" else "journal_issn_ls")) == 1

    with pytest.raises(InvalidPaperListQueryError):
        _parse(**{name: (value,) * (MAX_FILTER_VALUES + 1)})


def test_unclassified_is_only_a_topic() -> None:
    assert _parse(topics=(UNCLASSIFIED,)).topics == (UNCLASSIFIED,)
    with pytest.raises(InvalidPaperListQueryError):
        _parse(methods=(UNCLASSIFIED,))


@pytest.mark.parametrize(
    "fields",
    [
        {"journal_issn_ls": (ISR_ISSN, MISQ_ISSN)},  # 沒排序
        {"years": (2020, 2020)},  # 重複
        {"topics": ["erp"]},  # 不是 tuple
        {"years": ("2020",)},
        {"has_abstract": 1},
        {"this_week": None},
        {"scope": "all"},
    ],
)
def test_filters_only_take_normalized_values(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 — 每一種都是 ValueError
        PaperListFilters(**fields)


# --- 下一頁的位置 ------------------------------------------------------------------------------


@pytest.mark.parametrize("year", [2025, None, 0, -5, 2**31 - 1])
def test_cursor_round_trips(year: int | None) -> None:
    cursor = PaperListCursor(year=year, paper_id=PAPER_ID)

    encoded = encode_paper_list_cursor(cursor)

    assert "=" not in encoded
    assert decode_paper_list_cursor(encoded) == cursor


def _encoded(plain: str) -> str:
    return base64.urlsafe_b64encode(plain.encode("ascii")).decode("ascii").rstrip("=")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-cursor",
        "A" * 200,
        "%%%",
        _encoded(f"2025,{PAPER_ID}") + "=",  # 補了 =
        _encoded(f"2025,{PAPER_ID}") + "x",
        _encoded(f"02025,{PAPER_ID}"),  # 前置零
        _encoded(f"+2025,{PAPER_ID}"),
        _encoded(f" 2025,{PAPER_ID}"),
        _encoded(f"-0,{PAPER_ID}"),
        _encoded(f"2025,{str(PAPER_ID).upper()}"),  # UUID 不是標準寫法
        _encoded(f"2025,{PAPER_ID.hex}"),
        _encoded(f"2025,{PAPER_ID},x"),
        _encoded(f"abc,{PAPER_ID}"),
        _encoded(f"{2**31},{PAPER_ID}"),  # 超出 integer
        _encoded("2025,not-a-uuid"),
        _encoded(f"null,{PAPER_ID}"),
    ],
)
def test_malformed_cursor_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="下一頁的位置"):
        decode_paper_list_cursor(value)


def test_cursor_rejects_bad_fields() -> None:
    with pytest.raises(ValueError, match="year"):
        PaperListCursor(year=True, paper_id=PAPER_ID)
    with pytest.raises(ValueError, match="year"):
        PaperListCursor(year=-(2**31) - 1, paper_id=PAPER_ID)
    with pytest.raises(ValueError, match="paper_id"):
        PaperListCursor(year=2020, paper_id=str(PAPER_ID))  # type: ignore[arg-type]


# --- 一頁 ----------------------------------------------------------------------------------


def _facts(index: int) -> PaperRowFacts:
    return PaperRowFacts(
        paper_id=uuid.UUID(int=index + 1),
        title=f"Made-up study {index}",
        authors=("Chen, A.",),
        author_count=1,
        publication_year=2020,
        journal_issn_l=MISQ_ISSN,
        doi=None,
        has_abstract=True,
        in_scope=True,
        is_retracted=False,
    )


def _snapshot(status: SnapshotStatus, as_of: datetime | None) -> Snapshot:
    return Snapshot(
        id=uuid.UUID(int=99),
        number=3,
        weekly_run_id=uuid.UUID(int=98),
        status=status,
        as_of=as_of,
        created_at=AS_OF,
    )


def test_page_holds_at_most_one_page() -> None:
    rows = tuple(_facts(index) for index in range(PAPER_LIST_PAGE_SIZE))
    published = _snapshot(SnapshotStatus.PUBLISHED, AS_OF)

    page = PaperListPage(rows=rows, next_cursor="x", total=45, snapshot=published)

    assert page.total == 45
    assert PaperListPage(rows=(), next_cursor=None, total=0, snapshot=None).snapshot is None
    with pytest.raises(ValueError, match="一頁最多"):
        PaperListPage(rows=(*rows, _facts(99)), next_cursor=None, total=21, snapshot=None)


@pytest.mark.parametrize("total", [-1, True, 1.0])
def test_page_total_is_a_non_negative_int(total: object) -> None:
    with pytest.raises(ValueError, match="total"):
        PaperListPage(rows=(), next_cursor=None, total=total, snapshot=None)  # type: ignore[arg-type]


def test_page_snapshot_must_be_published() -> None:
    with pytest.raises(ValueError, match="已發佈"):
        PaperListPage(
            rows=(),
            next_cursor=None,
            total=0,
            snapshot=_snapshot(SnapshotStatus.BUILDING, None),
        )


def test_client_config_gives_the_paper_list_page_size() -> None:
    assert PAPER_LIST_PAGE_SIZE == 20
    assert build_client_config().paper_list_page_size == PAPER_LIST_PAGE_SIZE
