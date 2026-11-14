"""收錄過濾（`radar/domain/collection.py`）：各原因與檢查順序、出版年比今年晚照收、
`paratext`（`Editorial Board`）被類型擋下（D10：不另外加排除規則）。
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from radar.config import load_domain_config
from radar.domain.collection import COLLECTED_WORK_TYPES, CollectionSkip, collection_skip_reason
from radar.domain.exclusion import exclusion_reason
from radar.domain.papers import ParsedPaper
from radar.domain.parsers.openalex import parse_openalex_work
from tests.openalex_cassette import ISSN_L, cassette_works, work_bytes

ROOT = Path(__file__).resolve().parents[2]
START_YEAR = 2016
NOW = datetime(2026, 11, 14, tzinfo=UTC)


def _paper(**changes: Any) -> ParsedPaper:
    values: dict[str, Any] = {
        "source": "openalex",
        "fetched_at": NOW,
        "doi": "10.1016/j.infoandorg.2026.100001",
        "openalex_id": "W1",
        "title": "A study of digital work",
        "journal_issn_l": ISSN_L,
        "publication_year": 2020,
        "work_type": "article",
    }
    values.update(changes)
    return ParsedPaper.model_validate(values)


def _reason(paper: ParsedPaper) -> CollectionSkip | None:
    return collection_skip_reason(paper, journal_issn_l=ISSN_L, start_year=START_YEAR)


def test_collected_types_are_article_and_review() -> None:
    assert frozenset({"article", "review"}) == COLLECTED_WORK_TYPES
    assert _reason(_paper(work_type="article")) is None
    assert _reason(_paper(work_type="review")) is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"work_type": "book-chapter"}, CollectionSkip.WORK_TYPE),
        ({"work_type": "paratext"}, CollectionSkip.WORK_TYPE),
        ({"work_type": "editorial"}, CollectionSkip.WORK_TYPE),
        ({"work_type": "erratum"}, CollectionSkip.WORK_TYPE),
        ({"work_type": None}, CollectionSkip.WORK_TYPE),
        ({"doi": None}, CollectionSkip.NO_DOI),
        ({"journal_issn_l": "0960-085X"}, CollectionSkip.OTHER_JOURNAL),
        ({"journal_issn_l": None}, CollectionSkip.OTHER_JOURNAL),
        ({"publication_year": None}, CollectionSkip.NO_PUBLICATION_YEAR),
        ({"publication_year": 2015}, CollectionSkip.BEFORE_START_YEAR),
    ],
)
def test_each_skip_reason(changes: dict[str, Any], reason: CollectionSkip) -> None:
    assert _reason(_paper(**changes)) is reason


def test_start_year_itself_is_collected() -> None:
    assert _reason(_paper(publication_year=START_YEAR)) is None


def test_publication_year_after_this_year_is_collected_as_is() -> None:
    # OpenAlex 先登記的下一年度論文照收、不改年份。
    paper = _paper(publication_year=2027)

    assert _reason(paper) is None
    assert paper.publication_year == 2027


def test_reasons_are_checked_in_order() -> None:
    # 類型 → DOI → 期刊 → 出版年 → 起始年：全部都不符時回第一個。
    everything_wrong = _paper(
        work_type="book-chapter", doi=None, journal_issn_l="0960-085X", publication_year=None
    )
    assert _reason(everything_wrong) is CollectionSkip.WORK_TYPE
    assert _reason(_paper(doi=None, journal_issn_l=None)) is CollectionSkip.NO_DOI
    assert (
        _reason(_paper(journal_issn_l="0960-085X", publication_year=2010))
        is CollectionSkip.OTHER_JOURNAL
    )


def test_editorial_board_paratext_is_blocked_by_type_not_by_a_rule() -> None:
    # D10：`Editorial Board` 不加排除規則，靠類型過濾擋掉。
    rules = load_domain_config({"RADAR_CONFIG_DIR": str(ROOT / "config")}).exclusion_rules
    boards = [work for work in cassette_works() if work["title"] == "Editorial Board"]
    assert len(boards) == 42

    for work in boards:
        paper = parse_openalex_work(work_bytes(work), fetched_at=NOW)
        assert paper.work_type == "paratext"
        assert _reason(paper) is CollectionSkip.WORK_TYPE
        assert exclusion_reason(paper, rules) is None


def test_cassette_articles_are_all_collected() -> None:
    # I&O 的 248 篇：200 篇 article 都收；42 paratext、5 editorial、1 erratum 不收。
    reasons = [
        _reason(parse_openalex_work(work_bytes(work), fetched_at=NOW)) for work in cassette_works()
    ]

    assert reasons.count(None) == 200
    assert reasons.count(CollectionSkip.WORK_TYPE) == 48
