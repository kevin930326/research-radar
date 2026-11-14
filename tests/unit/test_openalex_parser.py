"""`parse_openalex_work`：錄好的 OpenAlex 回應全部解析得了、欄位對應、壞內容一律 `ParseError`。

測試資料只取自 `tests/cassettes/openalex/list_works_issn_1471-7727.yaml`（OpenAlex 資料是 CC0）。
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from radar.domain.papers import AbstractSource, ParsedKeyword
from radar.domain.parsers import ParseError
from radar.domain.parsers.openalex import parse_openalex_work
from tests.openalex_cassette import SAMPLE_ID, cassette_work, cassette_works, work_bytes

FETCHED_AT = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)
HEX64 = re.compile(r"[0-9a-f]{64}")


def _parse(work: object) -> Any:
    return parse_openalex_work(json.dumps(work).encode(), fetched_at=FETCHED_AT)


def _restore(index: dict[str, list[int]]) -> str:
    positions = {position: word for word, spots in index.items() for position in spots}
    return " ".join(positions[position] for position in sorted(positions))


# --- 錄好的回應 -------------------------------------------------------------------------------


def test_every_cassette_work_parses_with_its_own_id() -> None:
    works = cassette_works()
    assert len(works) == 248

    for work in works:
        paper = parse_openalex_work(work_bytes(work), fetched_at=FETCHED_AT)
        assert paper.openalex_id == work["id"].rsplit("/", 1)[-1]
        assert paper.fetched_at == FETCHED_AT
        assert HEX64.fullmatch(paper.source_hash)
        assert (paper.abstract is None) == (not work["abstract_inverted_index"])
        assert paper.cited_by_count == work["cited_by_count"]


def test_sample_work_fields() -> None:
    work = cassette_work(SAMPLE_ID)

    paper = parse_openalex_work(work_bytes(work), fetched_at=FETCHED_AT)

    assert paper.source == "openalex"
    assert paper.openalex_id == SAMPLE_ID
    assert paper.doi == "10.1016/j.infoandorg.2025.100560"
    assert paper.title == "Human-AI agency in the age of generative AI"
    assert paper.journal_issn_l == "1471-7727"
    assert paper.publication_year == 2025
    assert (paper.volume, paper.issue, paper.first_page, paper.last_page) == (
        "35",
        "1",
        "100560",
        "100560",
    )
    assert paper.work_type == "article"
    assert paper.is_retracted is False
    assert paper.authors == tuple(a["author"]["display_name"] for a in work["authorships"])
    assert paper.authors[0] == "Sebastian Maximillian Krakowski"
    # 摘要：倒排索引依位置還原成原文順序。
    assert paper.abstract is not None
    assert paper.abstract.startswith("The rapid emergence of generative artificial intelligence")
    assert paper.abstract == _restore(work["abstract_inverted_index"]).strip()
    assert paper.abstract_source is AbstractSource.OPENALEX
    assert paper.referenced_works == tuple(
        dict.fromkeys(ref.rsplit("/", 1)[-1] for ref in work["referenced_works"])
    )
    assert paper.referenced_works[0] == "W1521738132"
    assert paper.cited_by_count == 223
    assert paper.keywords[0] == ParsedKeyword(key="generative-ai", display_name="generative AI")
    assert [k.key for k in paper.keywords] == [k["id"].rsplit("/", 1)[-1] for k in work["keywords"]]
    assert HEX64.fullmatch(paper.source_hash)


# --- 欄位對應的細節 ---------------------------------------------------------------------------


@pytest.mark.parametrize("empty", [None, {}])
def test_no_abstract_is_none(empty: object) -> None:
    work = cassette_work()
    work["abstract_inverted_index"] = empty

    paper = _parse(work)

    assert paper.abstract is None
    assert paper.abstract_source is None


def test_missing_abstract_key_is_none() -> None:
    work = cassette_work()
    del work["abstract_inverted_index"]

    assert _parse(work).abstract is None


def test_abstract_positions_may_have_gaps() -> None:
    work = cassette_work()
    work["abstract_inverted_index"] = {"world": [7], "Hello": [2]}

    assert _parse(work).abstract == "Hello world"


def test_later_publication_year_is_kept() -> None:
    work = cassette_work()
    work["publication_year"] = 2027

    assert _parse(work).publication_year == 2027  # fetched_at 是 2026 年，照收不改


def test_title_falls_back_to_display_name() -> None:
    work = cassette_work()
    work["title"] = None
    work["display_name"] = "Display title"

    assert _parse(work).title == "Display title"


def test_blank_title_falls_back_to_display_name() -> None:
    work = cassette_work()
    work["title"] = "  "
    work["display_name"] = "Display title"

    assert _parse(work).title == "Display title"


def test_blank_author_names_fall_back_or_are_skipped() -> None:
    work = cassette_work()
    work["authorships"] = [
        {"author": {"display_name": ""}, "raw_author_name": "Raw Name"},
        {"author": {"display_name": "  "}, "raw_author_name": " "},
        {"author": {"display_name": "Third"}},
    ]

    assert _parse(work).authors == ("Raw Name", "Third")


def test_author_falls_back_to_raw_name_and_skips_nameless() -> None:
    work = cassette_work()
    work["authorships"] = [
        {"author": {"display_name": None}, "raw_author_name": "Raw Name"},
        {"author": None, "raw_author_name": None},
        {"author": {"display_name": "Second"}},
        {},
    ]

    assert _parse(work).authors == ("Raw Name", "Second")


def test_biblio_strips_and_blank_is_missing() -> None:
    work = cassette_work()
    work["biblio"] = {"volume": " 12 ", "issue": "", "first_page": "   ", "last_page": None}

    paper = _parse(work)

    assert (paper.volume, paper.issue, paper.first_page, paper.last_page) == (
        "12",
        None,
        None,
        None,
    )


def test_optional_values_missing_or_null() -> None:
    work = {"id": "https://openalex.org/W1", "title": "Only the basics"}

    paper = _parse(work)

    assert paper.openalex_id == "W1"
    assert paper.doi is None
    assert paper.journal_issn_l is None
    assert paper.publication_year is None
    assert paper.authors == ()
    assert paper.work_type is None
    assert paper.is_retracted is False
    assert paper.referenced_works == ()
    assert paper.cited_by_count is None
    assert paper.keywords == ()

    nulls = {key: None for key in ("doi", "primary_location", "biblio", "type", "is_retracted")}
    assert _parse({**work, **nulls}).is_retracted is False
    located = {**work, "primary_location": {"source": None}}
    assert _parse(located).journal_issn_l is None


def test_referenced_works_are_normalized_and_deduplicated() -> None:
    work = cassette_work()
    work["referenced_works"] = [
        "https://openalex.org/W3",
        "https://openalex.org/W1",
        "https://openalex.org/w3",
        "W2",
    ]

    assert _parse(work).referenced_works == ("W3", "W1", "W2")


def test_duplicate_keyword_keeps_first() -> None:
    work = cassette_work()
    work["keywords"] = [
        {"id": "https://openalex.org/keywords/a-b", "display_name": "first"},
        {"id": "https://openalex.org/keywords/c", "display_name": "other"},
        {"id": "https://openalex.org/keywords/a-b", "display_name": "second"},
    ]

    assert _parse(work).keywords == (
        ParsedKeyword(key="a-b", display_name="first"),
        ParsedKeyword(key="c", display_name="other"),
    )


def test_duplicate_keyword_with_null_name_is_ignored() -> None:
    work = cassette_work()
    work["keywords"] = [
        {"id": "https://openalex.org/keywords/a", "display_name": "A"},
        {"id": "https://openalex.org/keywords/a", "display_name": None},
    ]

    assert _parse(work).keywords == (ParsedKeyword(key="a", display_name="A"),)


def test_retracted_flag() -> None:
    work = cassette_work()
    work["is_retracted"] = True

    assert _parse(work).is_retracted is True


# --- 壞內容 ---------------------------------------------------------------------------------

SECRET = "TOPSECRET-VALUE-9f2"


def _broken(**changes: object) -> dict[str, Any]:
    work = cassette_work()
    for key, value in changes.items():
        if value is ...:
            del work[key]
        else:
            work[key] = value
    return work


BROKEN_WORKS: dict[str, tuple[dict[str, Any], str]] = {
    "缺 id": (_broken(id=...), "id"),
    "id 看不懂": (_broken(id=SECRET), "id"),
    "id 不是論文": (_broken(id="https://openalex.org/A123"), "openalex_id"),
    "title 與 display_name 都空白": (_broken(title="   ", display_name=""), "title"),
    "title 與 display_name 都沒有": (_broken(title=None, display_name=None), "title"),
    "doi 是數字": (_broken(doi=123), "doi"),
    "doi 看不懂": (_broken(doi=SECRET), "doi"),
    "is_retracted 是字串": (_broken(is_retracted="no"), "is_retracted"),
    "cited_by_count 是布林": (_broken(cited_by_count=True), "cited_by_count"),
    "cited_by_count 是負數": (_broken(cited_by_count=-1), "cited_by_count"),
    "publication_year 是字串": (_broken(publication_year="2020"), "publication_year"),
    "authorships 不是陣列": (_broken(authorships={"a": 1}), "authorships"),
    "author 名字是數字": (
        _broken(authorships=[{"author": {"display_name": 5}}]),
        "authorships.0.author.display_name",
    ),
    "issn_l 看不懂": (
        _broken(primary_location={"source": {"issn_l": SECRET}}),
        "primary_location.source.issn_l",
    ),
    "biblio 不是物件": (_broken(biblio=["35"]), "biblio"),
    "倒排索引位置重複": (
        _broken(abstract_inverted_index={SECRET: [0], "b": [0]}),
        "abstract_inverted_index",
    ),
    "倒排索引位置是負數": (
        _broken(abstract_inverted_index={SECRET: [-1]}),
        "abstract_inverted_index",
    ),
    "倒排索引位置是布林": (
        _broken(abstract_inverted_index={SECRET: [True]}),
        "abstract_inverted_index",
    ),
    "倒排索引位置是小數": (
        _broken(abstract_inverted_index={SECRET: [1.0]}),
        "abstract_inverted_index",
    ),
    "referenced_works 有看不懂的": (
        _broken(referenced_works=["https://openalex.org/W1", SECRET]),
        "referenced_works.1",
    ),
    "referenced_works 有不是論文的": (
        _broken(referenced_works=["https://openalex.org/A1"]),
        "referenced_works.0",
    ),
    "關鍵字 id 不對": (
        _broken(keywords=[{"id": f"https://example.org/{SECRET}", "display_name": "x"}]),
        "keywords.0.id",
    ),
    "關鍵字 key 有大寫": (
        _broken(keywords=[{"id": "https://openalex.org/keywords/AI", "display_name": "x"}]),
        "keywords.0.id",
    ),
    "關鍵字名稱空白": (
        _broken(keywords=[{"id": "https://openalex.org/keywords/ai", "display_name": " "}]),
        "keywords.0.display_name",
    ),
    "type 格式不對": (_broken(type="Journal Article"), "work_type"),
}


@pytest.mark.parametrize("name", list(BROKEN_WORKS))
def test_broken_work_raises_parse_error_without_values(name: str) -> None:
    work, path = BROKEN_WORKS[name]

    with pytest.raises(ParseError) as caught:
        _parse(work)

    message = str(caught.value)
    assert caught.value.path == path, message
    assert message.startswith(f"{path}：")
    assert SECRET not in message
    assert isinstance(caught.value, ValueError)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"\xff\xfe not utf-8", "不是 UTF-8"),
        (b"{not json", "不是合法的 JSON"),
        (b"[1, 2]", "要是 JSON 物件"),
        (b'"just a string"', "要是 JSON 物件"),
        (b"[" * 100_000 + b"]" * 100_000, None),
    ],
)
def test_undecodable_content(content: bytes, reason: str | None) -> None:
    with pytest.raises(ParseError) as caught:
        parse_openalex_work(content, fetched_at=FETCHED_AT)

    if reason is not None:
        assert caught.value.reason == reason


def test_deeply_nested_object_is_parse_error() -> None:
    depth = 100_000
    content = (
        b'{"id": "W1", "title": "x", "biblio": ' + b'{"a": ' * depth + b"1" + b"}" * depth + b"}"
    )

    with pytest.raises(ParseError):
        parse_openalex_work(content, fetched_at=FETCHED_AT)


# --- 倒排索引還原的性質 -----------------------------------------------------------------------

_WORD = st.text(
    alphabet=st.characters(exclude_categories=("Z", "Cc", "Cs"), exclude_characters="\x00"),
    min_size=1,
    max_size=8,
)


@given(st.lists(_WORD, min_size=1, max_size=40))
def test_inverted_index_round_trip(words: list[str]) -> None:
    index: dict[str, list[int]] = {}
    for position, word in enumerate(words):
        index.setdefault(word, []).append(position)
    work = {"id": "https://openalex.org/W1", "title": "t", "abstract_inverted_index": index}

    assert _parse(work).abstract == " ".join(words)
