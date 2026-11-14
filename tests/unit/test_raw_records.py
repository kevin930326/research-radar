"""原始紀錄、API 呼叫紀錄、物件鍵與壓縮格式（AD-25、ADR-0014）。"""

import gzip
import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from radar.domain.raw_records import (
    ApiCall,
    ApiUsage,
    JournalQuery,
    PaperSourceError,
    RawRecord,
    RawSource,
    SourcePage,
    compress_raw,
    decompress_raw,
)
from radar.domain.storage_keys import RAW_CONTENT_TYPE, ObjectNotFoundError, raw_object_key

NOW = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)
CONTENT = b'{"id": "https://openalex.org/W1", "title": "\xe4\xb8\xad"}'


def _call(**changes: Any) -> ApiCall:
    values: dict[str, Any] = {
        "source": "openalex",
        "operation": "list_works",
        "http_status": 200,
        "cost_usd": Decimal("0.0001"),
        "remaining_usd": Decimal("0.9999"),
        "limit_usd": Decimal(1),
        "called_at": NOW,
    }
    values.update(changes)
    return ApiCall(**values)


# --- RawRecord ------------------------------------------------------------------------------


def test_raw_record_keeps_bytes_and_hashes_them() -> None:
    record = RawRecord(RawSource.OPENALEX, "W1", CONTENT, NOW)

    assert record.content is CONTENT
    assert record.sha256 == hashlib.sha256(CONTENT).hexdigest()
    assert record.source is RawSource.OPENALEX


def test_raw_record_accepts_source_string() -> None:
    assert RawRecord("openalex", "W1", CONTENT, NOW).source is RawSource.OPENALEX  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"source": "crossref"}, ValueError),
        ({"external_key": "w1"}, ValueError),
        ({"external_key": "https://openalex.org/W1"}, ValueError),
        ({"external_key": "A1"}, ValueError),
        ({"external_key": 1}, TypeError),
        ({"content": "text"}, TypeError),
        ({"content": bytearray(b"x")}, TypeError),
        ({"content": b""}, ValueError),
        ({"fetched_at": datetime(2026, 11, 14)}, ValueError),
        ({"fetched_at": "2026-11-14"}, TypeError),
    ],
)
def test_raw_record_rejects_bad_values(changes: dict[str, Any], error: type[Exception]) -> None:
    values: dict[str, Any] = {
        "source": RawSource.OPENALEX,
        "external_key": "W1",
        "content": CONTENT,
        "fetched_at": NOW,
    }
    values.update(changes)

    with pytest.raises(error):
        RawRecord(**values)


# --- ApiCall / SourcePage / JournalQuery ----------------------------------------------------


def test_api_call_allows_connection_error_without_status() -> None:
    call = _call(http_status=None, cost_usd=Decimal(0), remaining_usd=None, limit_usd=None)

    assert call.http_status is None
    assert call.cost_usd == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": "ListWorks"},
        {"operation": ""},
        {"http_status": 99},
        {"http_status": 600},
        {"http_status": True},
        {"cost_usd": Decimal("-0.1")},
        {"cost_usd": 0.0001},
        {"cost_usd": Decimal("NaN")},
        {"cost_usd": Decimal(1_000_000)},
        {"remaining_usd": Decimal("Infinity")},
        {"limit_usd": "1"},
        {"called_at": datetime(2026, 11, 14)},
        {"source": "s2"},
    ],
)
def test_api_call_rejects_bad_values(changes: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        _call(**changes)


def test_source_page_checks_items_and_cursor() -> None:
    record = RawRecord(RawSource.OPENALEX, "W1", CONTENT, NOW)
    page = SourcePage([record], "next", [_call()])  # type: ignore[arg-type]

    assert page.records == (record,)
    assert page.calls == (_call(),)
    with pytest.raises(ValueError, match="next_cursor"):
        SourcePage((record,), "", ())
    with pytest.raises(TypeError):
        SourcePage((CONTENT,), None, ())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SourcePage((), None, ("call",))  # type: ignore[arg-type]


def test_journal_query_wants_normalized_issn_and_year() -> None:
    assert JournalQuery("0960-085X", 2016).start_year == 2016
    with pytest.raises(ValueError, match="正規化"):
        JournalQuery("0960085x", 2016)
    with pytest.raises(ValueError, match="start_year"):
        JournalQuery("0960-085X", True)
    with pytest.raises(ValueError, match="start_year"):
        JournalQuery("0960-085X", 99)


def test_paper_source_error_keeps_calls() -> None:
    error = PaperSourceError("HTTP 401", calls=[_call(http_status=401)])

    assert str(error) == "HTTP 401"
    assert error.reason == "HTTP 401"
    assert error.calls == (_call(http_status=401),)
    assert PaperSourceError("x").calls == ()


@pytest.mark.parametrize(
    ("local", "reported", "used"),
    [
        (Decimal("0.3"), None, Decimal("0.3")),
        (Decimal("0.3"), Decimal("0.5"), Decimal("0.5")),
        (Decimal("0.7"), Decimal("0.5"), Decimal("0.7")),
    ],
)
def test_api_usage_takes_the_larger(
    local: Decimal, reported: Decimal | None, used: Decimal
) -> None:
    assert ApiUsage(local, reported).used_usd == used


# --- 物件鍵與壓縮 ---------------------------------------------------------------------------


def test_raw_object_key_shape() -> None:
    sha256 = hashlib.sha256(CONTENT).hexdigest()

    assert raw_object_key("openalex", sha256) == f"raw/openalex/{sha256}.json.gz"
    assert raw_object_key(RawSource.OPENALEX, sha256) == f"raw/openalex/{sha256}.json.gz"
    assert RAW_CONTENT_TYPE == "application/gzip"


@pytest.mark.parametrize(
    ("source", "sha256"),
    [
        ("openalex", "A" * 64),
        ("openalex", "a" * 63),
        ("openalex", "a" * 65),
        ("openalex", "../" + "a" * 61),
        ("crossref", "a" * 64),
    ],
)
def test_raw_object_key_rejects_bad_input(source: str, sha256: str) -> None:
    with pytest.raises(ValueError, match=r"sha256|crossref"):
        raw_object_key(source, sha256)


def test_compress_is_deterministic_gzip() -> None:
    first = compress_raw(CONTENT)

    assert first == compress_raw(CONTENT)
    assert first[:2] == b"\x1f\x8b"
    assert gzip.decompress(first) == CONTENT
    assert decompress_raw(first) == CONTENT
    # mtime 欄位（第 4–7 位元組）是 0：同內容不論何時壓都一樣。
    assert first[4:8] == b"\x00\x00\x00\x00"


def test_object_not_found_error_names_key() -> None:
    error = ObjectNotFoundError("raw/openalex/x.json.gz")

    assert error.key == "raw/openalex/x.json.gz"
    assert "raw/openalex/x.json.gz" in str(error)
