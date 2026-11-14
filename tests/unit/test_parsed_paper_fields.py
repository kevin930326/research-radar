"""`ParsedPaper` 在 Story 1.7 加的欄位：驗證規則與指紋 `source_hash` 算進／不算進哪些欄位。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from radar.domain.fingerprints import source_hash
from radar.domain.papers import (
    ABSTRACT_SOURCE_PRIORITY,
    AbstractSource,
    KeywordSource,
    ParsedKeyword,
    ParsedPaper,
)

FETCHED_AT = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)


def _fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "source": "openalex",
        "fetched_at": FETCHED_AT,
        "doi": "10.1016/x",
        "openalex_id": "W1",
        "title": "A study",
        "journal_issn_l": "1471-7727",
        "publication_year": 2020,
        "authors": ("A", "B"),
        "volume": "35",
        "issue": "1",
        "first_page": "1",
        "last_page": "20",
        "work_type": "article",
        "is_retracted": False,
        "abstract": "An abstract.",
        "abstract_source": "openalex",
        "referenced_works": ("W2", "W3"),
        "cited_by_count": 5,
        "keywords": ({"key": "ai", "display_name": "AI"},),
    }
    fields.update(overrides)
    return fields


def _paper(**overrides: object) -> ParsedPaper:
    return ParsedPaper.model_validate(_fields(**overrides))


def test_new_fields_have_defaults() -> None:
    paper = ParsedPaper(
        source="openalex",
        fetched_at=FETCHED_AT,
        doi="10.1/a",
        openalex_id=None,
        title="t",
        journal_issn_l=None,
        publication_year=None,
    )

    assert paper.authors == ()
    assert (paper.volume, paper.issue, paper.first_page, paper.last_page) == (None,) * 4
    assert paper.work_type is None
    assert paper.is_retracted is False
    assert paper.abstract is None
    assert paper.abstract_source is None
    assert paper.referenced_works == ()
    assert paper.cited_by_count is None
    assert paper.keywords == ()


def test_text_fields_are_stripped() -> None:
    paper = _paper(authors=(" A ", "B"), volume=" 35 ", abstract="  text  ")

    assert paper.authors == ("A", "B")
    assert paper.volume == "35"
    assert paper.abstract == "text"


@pytest.mark.parametrize(
    "overrides",
    [
        {"abstract": "  "},  # 沒有摘要要用 None，不是空白
        {"abstract": ""},
        {"abstract": "x\x00y"},
        {"abstract": "\ud800"},
        {"abstract": None},  # 有來源卻沒有摘要
        {"abstract_source": None},  # 有摘要卻沒有來源
        {"abstract_source": "crossref"},
        {"authors": ("A", " ")},
        {"volume": ""},
        {"issue": "   "},
        {"first_page": "\x00"},
        {"work_type": "Journal Article"},
        {"work_type": "1article"},
        {"is_retracted": "no"},
        {"is_retracted": 1},
        {"cited_by_count": True},
        {"cited_by_count": -1},
        {"cited_by_count": 2_147_483_648},
        {"cited_by_count": "5"},
        {"referenced_works": ("A1",)},
        {"referenced_works": ("not an id",)},
        {"keywords": ({"key": "ai", "display_name": "AI"}, {"key": "ai", "display_name": "x"})},
        {"keywords": ({"key": "AI", "display_name": "AI"},)},
        {"keywords": ({"key": "a--b", "display_name": "x"},)},
        {"keywords": ({"key": "-a", "display_name": "x"},)},
        {"keywords": ({"key": "a" * 201, "display_name": "x"},)},
        {"keywords": ({"key": "ai", "display_name": " "},)},
    ],
)
def test_invalid_values_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _paper(**overrides)


def test_no_abstract_is_none_and_none() -> None:
    paper = _paper(abstract=None, abstract_source=None)

    assert paper.abstract is None
    assert paper.abstract_source is None


def test_referenced_works_are_normalized_and_deduplicated() -> None:
    paper = _paper(referenced_works=("https://openalex.org/w3", "W1", "W3"))

    assert paper.referenced_works == ("W3", "W1")


def test_keyword_key_limits() -> None:
    assert ParsedKeyword(key="a" * 200, display_name="x").key == "a" * 200
    assert ParsedKeyword(key="a1-b2-c3", display_name=" Name ").display_name == "Name"


def test_enums() -> None:
    assert [source.value for source in AbstractSource] == [
        "openalex",
        "semantic_scholar",
        "uploaded_pdf",
    ]
    assert [source.value for source in KeywordSource] == ["openalex"]
    assert ABSTRACT_SOURCE_PRIORITY == ("openalex", "semantic_scholar")
    assert AbstractSource.UPLOADED_PDF not in ABSTRACT_SOURCE_PRIORITY


def test_round_trip_through_json() -> None:
    paper = _paper()

    assert ParsedPaper.model_validate(json.loads(paper.model_dump_json())) == paper


# --- source_hash ----------------------------------------------------------------------------


def test_source_hash_matches_documented_algorithm() -> None:
    paper = _paper()
    payload = {
        "abstract": "An abstract.",
        "authors": ["A", "B"],
        "doi": "10.1016/x",
        "first_page": "1",
        "is_retracted": False,
        "issue": "1",
        "journal_issn_l": "1471-7727",
        "last_page": "20",
        "publication_year": 2020,
        "title": "A study",
        "v": 1,
        "volume": "35",
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    assert paper.source_hash == hashlib.sha256(canonical.encode()).hexdigest()
    assert paper.source_hash == source_hash(paper)
    assert len(paper.source_hash) == 64


def test_source_hash_keeps_non_ascii_unescaped() -> None:
    paper = _paper(title="資訊管理", authors=(), abstract=None, abstract_source=None)
    canonical = (
        '{"abstract":null,"authors":[],"doi":"10.1016/x","first_page":"1","is_retracted":false,'
        '"issue":"1","journal_issn_l":"1471-7727","last_page":"20","publication_year":2020,'
        '"title":"資訊管理","v":1,"volume":"35"}'
    )

    assert paper.source_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "overrides",
    [
        {"cited_by_count": 999},
        {"cited_by_count": None},
        {"keywords": ()},
        {"keywords": ({"key": "other", "display_name": "Other"},)},
        {"referenced_works": ()},
        {"referenced_works": ("W9",)},
        {"work_type": "review"},
        {"work_type": None},
        {"fetched_at": FETCHED_AT + timedelta(days=7)},
        {"openalex_id": "W999"},
        {"abstract_source": "semantic_scholar"},
    ],
)
def test_source_hash_ignores_nightly_and_identity_fields(overrides: dict[str, object]) -> None:
    assert _paper(**overrides).source_hash == _paper().source_hash


@pytest.mark.parametrize(
    "overrides",
    [
        {"abstract": "Another abstract."},
        {"abstract": None, "abstract_source": None},
        {"is_retracted": True},
        {"title": "Another title"},
        {"authors": ("A",)},
        {"authors": ("B", "A")},
        {"journal_issn_l": "0276-7783"},
        {"journal_issn_l": None},
        {"publication_year": 2021},
        {"volume": "36"},
        {"issue": "2"},
        {"first_page": "2"},
        {"last_page": "21"},
        {"doi": "10.1016/y"},
    ],
)
def test_source_hash_changes_with_bibliographic_fields(overrides: dict[str, object]) -> None:
    assert _paper(**overrides).source_hash != _paper().source_hash
