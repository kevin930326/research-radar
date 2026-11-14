"""`FakePaperSource` 與 `FakeObjectStore`：之後的 Story 都用它們，行為要和 port 說的一樣。"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

import radar.adapters.fake as fake
from radar.adapters.fake import FakeObjectStore, FakePaperSource
from radar.domain.raw_records import JournalQuery, PaperSourceError, RawRecord, RawSource
from radar.domain.storage_keys import ObjectNotFoundError
from radar.ports import ObjectStore, PaperSource

NOW = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)
ISSN = "1471-7727"
QUERY = JournalQuery(ISSN, 2016)


def _record(number: int) -> RawRecord:
    return RawRecord(RawSource.OPENALEX, f"W{number}", f'{{"id": "W{number}"}}'.encode(), NOW)


def test_exported_from_fake_package() -> None:
    assert {"FakePaperSource", "FakeObjectStore"} <= set(fake.__all__)


def test_fakes_satisfy_the_ports() -> None:
    source: PaperSource = FakePaperSource()
    store: ObjectStore = FakeObjectStore()

    assert source.name is RawSource.OPENALEX
    assert store.get.__name__ == "get"


def test_pages_follow_own_cursor_and_requests_are_recorded() -> None:
    source = FakePaperSource({ISSN: [[_record(1), _record(2)], [_record(3)]]}, clock=lambda: NOW)

    first = source.fetch_journal_page(QUERY, None)
    second = source.fetch_journal_page(QUERY, first.next_cursor)

    assert [r.external_key for r in first.records] == ["W1", "W2"]
    assert first.next_cursor is not None
    assert [r.external_key for r in second.records] == ["W3"]
    assert second.next_cursor is None
    assert [(r.query, r.cursor) for r in source.requests] == [
        (QUERY, None),
        (QUERY, first.next_cursor),
    ]
    for page in (first, second):
        [call] = page.calls
        assert (call.operation, call.http_status, call.cost_usd, call.called_at) == (
            "list_works",
            200,
            Decimal("0.0001"),
            NOW,
        )


def test_unknown_journal_returns_one_empty_page() -> None:
    page = FakePaperSource().fetch_journal_page(QUERY, None)

    assert page.records == ()
    assert page.next_cursor is None
    assert len(page.calls) == 1


def test_fail_once_on_page_n_then_recovers() -> None:
    source = FakePaperSource({ISSN: [[_record(1)], [_record(2)], [_record(3)]]})
    source.fail_once(ISSN, 2)

    first = source.fetch_journal_page(QUERY, None)
    with pytest.raises(PaperSourceError) as caught:
        source.fetch_journal_page(QUERY, first.next_cursor)
    retried = source.fetch_journal_page(QUERY, first.next_cursor)

    assert len(caught.value.calls) == 1
    assert caught.value.calls[0].http_status == 503
    assert [r.external_key for r in retried.records] == ["W2"]
    assert len(source.requests) == 3


def test_fail_once_page_number_starts_at_one() -> None:
    with pytest.raises(ValueError, match="從 1 起算"):
        FakePaperSource().fail_once(ISSN, 0)


def test_cursor_from_another_journal_is_rejected() -> None:
    source = FakePaperSource({ISSN: [[_record(1)], [_record(2)]]})
    cursor = source.fetch_journal_page(QUERY, None).next_cursor

    with pytest.raises(ValueError, match="cursor"):
        source.fetch_journal_page(JournalQuery("0960-085X", 2016), cursor)


def test_quota_values_are_configurable() -> None:
    source = FakePaperSource(
        cost_usd=Decimal("0.5"), remaining_usd=Decimal("0.2"), limit_usd=Decimal(1)
    )

    [call] = source.fetch_journal_page(QUERY, None).calls

    assert (call.cost_usd, call.remaining_usd, call.limit_usd) == (
        Decimal("0.5"),
        Decimal("0.2"),
        Decimal(1),
    )


def test_object_store_records_puts_and_round_trips() -> None:
    store = FakeObjectStore()

    store.put("raw/openalex/a.json.gz", b"one", content_type="application/gzip")
    store.put("raw/openalex/a.json.gz", b"two", content_type="application/gzip")

    assert store.get("raw/openalex/a.json.gz") == b"two"
    assert [(p.key, p.data, p.content_type) for p in store.puts] == [
        ("raw/openalex/a.json.gz", b"one", "application/gzip"),
        ("raw/openalex/a.json.gz", b"two", "application/gzip"),
    ]
    with pytest.raises(ObjectNotFoundError):
        store.get("raw/openalex/missing.json.gz")
