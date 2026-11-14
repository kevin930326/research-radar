"""OpenAlex adapter 對錄好的真實回應（`tests/cassettes/openalex/list_works_issn_1471-7727.yaml`）。

I&O（ISSN-L 1471-7727）2016 年起三頁：100＋100＋48＝248 篇。不連網路：請求由 vcrpy 回放，
對不上錄好的請求就失敗（record mode none）。
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest
import yaml

from radar.adapters.openalex import OpenAlexSource
from radar.config import load_domain_config
from radar.domain.raw_records import JournalQuery, RawSource
from tests.conftest import CASSETTES_DIR

CASSETTE = "openalex/list_works_issn_1471-7727.yaml"
CASSETTE_PATH = CASSETTES_DIR / CASSETTE
FAKE_KEY = "test-key-not-real"
QUERY = JournalQuery(issn_l="1471-7727", start_year=2016)
FIXED_NOW = datetime(2026, 11, 14, 2, 43, 48, tzinfo=UTC)

pytestmark = pytest.mark.vcr(CASSETTE)


class RecordingTransport(httpx2.BaseTransport):
    """包住真的 `HTTPTransport`（vcrpy 會攔下它），記下送出的每個請求。"""

    def __init__(self) -> None:
        self.inner = httpx2.HTTPTransport()
        self.requests: list[httpx2.Request] = []

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


def _source(transport: RecordingTransport) -> OpenAlexSource:
    config = load_domain_config({"RADAR_CONFIG_DIR": str(Path(__file__).parents[2] / "config")})
    return OpenAlexSource(
        FAKE_KEY,
        config.openalex,
        transport=transport,
        sleep=lambda _seconds: None,
        clock=lambda: FIXED_NOW,
    )


def _recorded_bodies() -> list[bytes]:
    with CASSETTE_PATH.open(encoding="utf-8") as file:
        cassette = yaml.safe_load(file)
    return [
        interaction["response"]["body"]["string"].encode("utf-8")
        for interaction in cassette["interactions"]
    ]


def _assert_raw_slices(contents: list[bytes], body: bytes) -> None:
    """每一篇的位元組就是錄好的本文裡那一段原文：

    - 依序接起來（OpenAlex 的分隔是 `, `）正好是本文裡 `"results": [...]` 的內容；
    - 每一段單獨解析，等於整份本文解析後的對應元素。
    """
    assert b'"results": [' + b", ".join(contents) + b"]" in body
    assert [json.loads(content) for content in contents] == json.loads(body)["results"]


def test_first_page_request_and_raw_slices() -> None:
    transport = RecordingTransport()
    with _source(transport) as source:
        page = source.fetch_journal_page(QUERY, None)

    [request] = transport.requests
    url = urlsplit(str(request.url))
    assert (request.method, url.scheme, url.netloc, url.path) == (
        "GET",
        "https",
        "api.openalex.org",
        "/works",
    )
    assert dict(parse_qsl(url.query)) == {
        "filter": "primary_location.source.issn:1471-7727,publication_year:>2015",
        "per-page": "100",
        "cursor": "*",
        "api_key": FAKE_KEY,
    }
    assert "search" not in url.query

    assert len(page.records) == 100
    _assert_raw_slices([record.content for record in page.records], _recorded_bodies()[0])
    for record in page.records:
        assert record.source is RawSource.OPENALEX
        assert record.external_key.startswith("W")
        assert record.fetched_at == FIXED_NOW
    assert page.next_cursor is not None


def test_follows_cursor_until_done() -> None:
    transport = RecordingTransport()
    pages = []
    cursor: str | None = None
    with _source(transport) as source:
        while True:
            page = source.fetch_journal_page(QUERY, cursor)
            pages.append(page)
            cursor = page.next_cursor
            if cursor is None:
                break

    assert [len(page.records) for page in pages] == [100, 100, 48]
    assert pages[-1].next_cursor is None
    sent_cursors = [
        dict(parse_qsl(urlsplit(str(r.url)).query))["cursor"] for r in transport.requests
    ]
    assert sent_cursors == ["*", pages[0].next_cursor, pages[1].next_cursor]

    bodies = _recorded_bodies()
    for page, body in zip(pages, bodies, strict=True):
        _assert_raw_slices([record.content for record in page.records], body)
        [call] = page.calls
        assert call.http_status == 200
        assert call.operation == "list_works"
        assert call.cost_usd == Decimal("0.0001")
        assert call.remaining_usd is not None
        assert call.limit_usd == Decimal(1)

    keys = [record.external_key for page in pages for record in page.records]
    assert len(set(keys)) == 248
