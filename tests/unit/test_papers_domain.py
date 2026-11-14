"""`ParsedPaper` 的驗證，以及 `upsert_by_external_id` 在碰資料庫之前就擋下的錯誤。"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from radar.domain.identifiers import ExternalId, IdentifierScheme
from radar.domain.papers import (
    PaperOrigin,
    ParsedPaper,
    UpsertOutcome,
    UpsertResult,
    check_storable_text,
)
from radar.domain.scope import ScopeSettings
from radar.repo.papers import upsert_by_external_id

FETCHED_AT = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)
SCOPE = ScopeSettings(frozenset({"0276-7783"}))


def _fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "source": "openalex",
        "fetched_at": FETCHED_AT,
        "doi": "https://doi.org/10.1/ABC",
        "openalex_id": "https://openalex.org/w123",
        "title": "  A study  ",
        "journal_issn_l": "02767783",
        "publication_year": 2020,
    }
    fields.update(overrides)
    return fields


def _paper(**overrides: object) -> ParsedPaper:
    return ParsedPaper.model_validate(_fields(**overrides))


def test_identifiers_are_normalized_on_construction() -> None:
    paper = _paper()

    assert paper.doi == "10.1/abc"
    assert paper.openalex_id == "W123"
    assert paper.journal_issn_l == "0276-7783"
    assert paper.title == "A study"


def test_external_ids_in_priority_order() -> None:
    assert _paper().external_ids() == (
        ExternalId(IdentifierScheme.DOI, "10.1/abc"),
        ExternalId(IdentifierScheme.OPENALEX, "W123"),
    )
    assert _paper(doi=None).external_ids() == (ExternalId(IdentifierScheme.OPENALEX, "W123"),)
    assert _paper(openalex_id=None).external_ids() == (
        ExternalId(IdentifierScheme.DOI, "10.1/abc"),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"doi": None, "openalex_id": None},
        {"doi": "not a doi"},
        {"openalex_id": "A123"},  # 作者的 ID，不是論文
        {"openalex_id": "123"},
        {"journal_issn_l": "1234"},
        {"title": "   "},
        {"fetched_at": datetime(2026, 11, 14, 3, 0)},  # 沒有時區
        {"source": "crossref"},
        {"unexpected": 1},
        {"doi": 10.1},
        {"publication_year": 0},
        {"publication_year": -2020},
        {"publication_year": 999},
        {"publication_year": 10000},
        {"publication_year": 2**31},
        {"publication_year": True},
        {"publication_year": "2020"},
        {"title": "A\x00study"},
        # 單獨的代理字元：Python 收得下、PostgreSQL 存不下（psycopg 寫入時才丟錯）。
        {"title": "A study \ud800"},
        {"title": "\udfff"},
        {"doi": "10.1/\ud800"},
        {"doi": "https://doi.org/10.1/\ud800"},
        {"openalex_id": "W1\ud800"},
        {"journal_issn_l": "0276-778\ud800"},
    ],
)
def test_invalid_input_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _paper(**overrides)


def test_every_field_must_be_given() -> None:
    fields = _fields()
    del fields["journal_issn_l"]

    with pytest.raises(ValidationError):
        ParsedPaper.model_validate(fields)


def test_parsed_paper_is_frozen() -> None:
    paper = _paper()

    with pytest.raises(ValidationError):
        paper.title = "changed"  # type: ignore[misc]


def test_upsert_result_has_no_conflicts_by_default() -> None:
    result = UpsertResult(paper_id=uuid.uuid4(), outcome=UpsertOutcome.STALE)

    assert result.conflicts == ()


def test_upsert_result_source_changed_defaults_false_and_is_not_compared() -> None:
    # Story 1.11：結果相等仍只看（論文, 結果, 衝突），`source_changed` 只是附帶的資訊。
    paper_id = uuid.uuid4()
    changed = UpsertResult(paper_id, UpsertOutcome.UPDATED, source_changed=True)

    assert UpsertResult(paper_id, UpsertOutcome.UPDATED).source_changed is False
    assert changed.source_changed is True
    assert changed == UpsertResult(paper_id, UpsertOutcome.UPDATED)


# --- upsert_by_external_id：不碰資料庫就擋下 ------------------------------------------------


class UntouchableConnection:
    """任何操作都讓測試失敗：證明錯誤在碰資料庫之前就丟出。"""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"不該碰連線：{name}")


def test_upsert_rejects_dict_without_touching_database() -> None:
    with pytest.raises(TypeError):
        upsert_by_external_id(
            UntouchableConnection(),  # type: ignore[arg-type]
            _fields(),  # type: ignore[arg-type]
            origin=PaperOrigin.TRACKED,
            excluded_reason=None,
            scope=SCOPE,
        )


@pytest.mark.parametrize("origin", [PaperOrigin.MANUAL, "manual", "x"])
def test_upsert_rejects_manual_or_unknown_origin(origin: Any) -> None:
    with pytest.raises(ValueError, match=r"origin|PaperOrigin"):
        upsert_by_external_id(
            UntouchableConnection(),  # type: ignore[arg-type]
            _paper(),
            origin=origin,
            excluded_reason=None,
            scope=SCOPE,
        )


def test_upsert_rejects_empty_excluded_reason() -> None:
    with pytest.raises(ValueError, match="excluded_reason"):
        upsert_by_external_id(
            UntouchableConnection(),  # type: ignore[arg-type]
            _paper(),
            origin=PaperOrigin.TRACKED,
            excluded_reason="",
            scope=SCOPE,
        )


@pytest.mark.parametrize("reason", ["edit\x00orial", "editorial \ud800"])
def test_upsert_rejects_unstorable_excluded_reason(reason: str) -> None:
    with pytest.raises(ValueError, match="excluded_reason"):
        upsert_by_external_id(
            UntouchableConnection(),  # type: ignore[arg-type]
            _paper(),
            origin=PaperOrigin.TRACKED,
            excluded_reason=reason,
            scope=SCOPE,
        )


@pytest.mark.parametrize(
    "value",
    ["A study", "論文標題", "emoji \U0001f4da", "\ufffd"],
)
def test_storable_text_accepts_normal_unicode(value: str) -> None:
    check_storable_text(value, "標題")


@pytest.mark.parametrize("value", ["\x00", "a\ud800b", "\udc00"])
def test_storable_text_rejects_nul_and_surrogates(value: str) -> None:
    with pytest.raises(ValueError, match="標題"):
        check_storable_text(value, "標題")
