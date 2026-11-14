"""OpenAlex adapter（`httpx2.MockTransport`，不連網路）：請求參數、重試、不重試的錯誤、
壞回應、空結果、節流、額度標頭，以及原樣切片的性質測試。
"""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest
import stamina
from hypothesis import given
from hypothesis import strategies as st

from radar.adapters.openalex import (
    OpenAlexSource,
    RetryableOpenAlexError,
    slice_results,
)
from radar.domain.domain_config import OpenAlexConfig
from radar.domain.raw_records import JournalQuery, PaperSourceError, RawSource
from radar.entrypoints.logging_config import configure_logging
from radar.ports import PaperSource

FAKE_KEY = "s3cret-FAKE-KEY"
NOW = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)
QUERY = JournalQuery(issn_l="0960-085X", start_year=2016)
COST_HEADERS = {
    "x-ratelimit-cost-usd": "0.0001",
    "x-ratelimit-remaining-usd": "0.9871",
    "x-ratelimit-limit-usd": "1",
}

type Handler = Callable[[httpx2.Request], httpx2.Response]


def _config(**changes: Any) -> OpenAlexConfig:
    values: dict[str, Any] = {
        "base_url": "https://api.openalex.example",
        "per_page": 100,
        "requests_per_second": 5,
        "timeout_seconds": 30,
        "retry": {
            "attempts": 3,
            "wait_initial_seconds": 1,
            "wait_max_seconds": 30,
            "wait_jitter_seconds": 1,
            "retry_after_max_seconds": 120,
        },
        "quota": {
            "daily_budget_usd": "1.0",
            "stop_at_ratio": "0.8",
            "default_request_cost_usd": "0.0001",
        },
    }
    values.update(changes)
    return OpenAlexConfig.model_validate(values)


def _body(results: list[Any], next_cursor: Any = "next-1", **extra: Any) -> bytes:
    return json.dumps(
        {"meta": {"count": 1, "next_cursor": next_cursor}, "results": results, **extra}
    ).encode()


def _work(number: int) -> dict[str, Any]:
    return {
        "id": f"https://openalex.org/W{number}",
        "title": f"論文 {number}",
        "cited_by_count": number,
    }


def _ok(content: bytes | None = None, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        200,
        content=_body([_work(1)]) if content is None else content,
        headers=headers or COST_HEADERS,
    )


def _sequence(responses: list[httpx2.Response | Exception], seen: list[httpx2.Request]) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler


def _source(
    handler: Handler, *, config: OpenAlexConfig | None = None, **kwargs: Any
) -> OpenAlexSource:
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("clock", lambda: NOW)
    return OpenAlexSource(
        FAKE_KEY, config or _config(), transport=httpx2.MockTransport(handler), **kwargs
    )


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Iterator[None]:
    # stamina 的測試模式：重試不等待，次數照設定（cap=True 只設上限）。
    with stamina.set_testing(True, attempts=100, cap=True):
        yield


def _params(request: httpx2.Request) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(str(request.url)).query))


# --- 請求 ---------------------------------------------------------------------------------


def test_first_page_sends_exact_parameters() -> None:
    seen: list[httpx2.Request] = []

    page = _source(_sequence([_ok()], seen)).fetch_journal_page(QUERY, None)

    [request] = seen
    assert request.method == "GET"
    assert str(request.url).split("?")[0] == "https://api.openalex.example/works"
    assert _params(request) == {
        "filter": "primary_location.source.issn:0960-085X,publication_year:>2015",
        "per-page": "100",
        "cursor": "*",
        "api_key": FAKE_KEY,
    }
    assert [record.external_key for record in page.records] == ["W1"]
    assert page.records[0].source is RawSource.OPENALEX
    assert page.records[0].fetched_at == NOW
    assert page.next_cursor == "next-1"


def test_given_cursor_is_sent() -> None:
    seen: list[httpx2.Request] = []

    _source(_sequence([_ok()], seen)).fetch_journal_page(QUERY, "abc")

    assert _params(seen[0])["cursor"] == "abc"


def test_one_api_call_per_page_with_quota_headers() -> None:
    page = _source(_sequence([_ok()], [])).fetch_journal_page(QUERY, None)

    [call] = page.calls
    assert call.source is RawSource.OPENALEX
    assert call.operation == "list_works"
    assert call.http_status == 200
    assert call.cost_usd == Decimal("0.0001")
    assert call.remaining_usd == Decimal("0.9871")
    assert call.limit_usd == Decimal(1)
    assert call.called_at == NOW


@pytest.mark.parametrize(
    ("headers", "cost", "remaining"),
    [
        ({}, Decimal("0.0002"), None),
        (
            {"x-ratelimit-cost-usd": "abc", "x-ratelimit-remaining-usd": "x"},
            Decimal("0.0002"),
            None,
        ),
        (
            {"x-ratelimit-cost-usd": "-1", "x-ratelimit-remaining-usd": "NaN"},
            Decimal("0.0002"),
            None,
        ),
        (
            {"x-ratelimit-cost-usd": "0.003", "x-ratelimit-remaining-usd": "0.5"},
            Decimal("0.003"),
            Decimal("0.5"),
        ),
    ],
)
def test_missing_or_unreadable_cost_headers(
    headers: dict[str, str], cost: Decimal, remaining: Decimal | None
) -> None:
    config = _config(
        quota={"daily_budget_usd": 1, "stop_at_ratio": 0.8, "default_request_cost_usd": "0.0002"}
    )
    response = httpx2.Response(200, content=_body([_work(1)]), headers=headers)

    [call] = _source(_sequence([response], []), config=config).fetch_journal_page(QUERY, None).calls

    assert call.cost_usd == cost
    assert call.remaining_usd == remaining


def test_repr_does_not_show_key() -> None:
    assert FAKE_KEY not in repr(_source(_sequence([], [])))


def test_empty_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="api_key"):
        OpenAlexSource("", _config())


# --- 重試 ---------------------------------------------------------------------------------


def test_409_is_retried() -> None:
    seen: list[httpx2.Request] = []

    page = _source(_sequence([httpx2.Response(409), _ok()], seen)).fetch_journal_page(QUERY, None)

    assert len(seen) == 2
    assert [call.http_status for call in page.calls] == [409, 200]
    assert len(page.records) == 1


def test_429_with_retry_after_is_retried() -> None:
    seen: list[httpx2.Request] = []
    responses: list[httpx2.Response | Exception] = [
        httpx2.Response(429, headers={"Retry-After": "7", **COST_HEADERS}),
        _ok(),
    ]

    page = _source(_sequence(responses, seen)).fetch_journal_page(QUERY, None)

    assert len(seen) == 2
    assert [call.http_status for call in page.calls] == [429, 200]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_retried(status: int) -> None:
    seen: list[httpx2.Request] = []

    page = _source(_sequence([httpx2.Response(status), _ok()], seen)).fetch_journal_page(
        QUERY, None
    )

    assert [call.http_status for call in page.calls] == [status, 200]


@pytest.mark.parametrize(
    ("error", "cost"),
    [
        # 連線都沒建立：請求確定沒送出，不花額度。
        (httpx2.ConnectError("boom"), Decimal(0)),
        (httpx2.ConnectTimeout("slow connect"), Decimal(0)),
        # 請求可能已經送到對方：照預設單價算（寧可高估用量）。
        (httpx2.ReadTimeout("slow"), Decimal("0.0001")),
        (httpx2.WriteTimeout("slow write"), Decimal("0.0001")),
        (httpx2.ReadError("reset"), Decimal("0.0001")),
        (httpx2.RemoteProtocolError("bye"), Decimal("0.0001")),
    ],
)
def test_transport_errors_are_retried_and_costed(error: Exception, cost: Decimal) -> None:
    seen: list[httpx2.Request] = []

    page = _source(_sequence([error, _ok()], seen)).fetch_journal_page(QUERY, None)

    first, second = page.calls
    assert (first.http_status, first.cost_usd, first.remaining_usd, first.limit_usd) == (
        None,
        cost,
        None,
        None,
    )
    assert second.http_status == 200
    assert len(seen) == 2


def test_decoding_error_is_not_retried_and_costs_the_default() -> None:
    seen: list[httpx2.Request] = []

    with pytest.raises(PaperSourceError, match="DecodingError") as caught:
        _source(_sequence([httpx2.DecodingError("bad gzip"), _ok()], seen)).fetch_journal_page(
            QUERY, None
        )

    assert len(seen) == 1
    [call] = caught.value.calls
    assert (call.http_status, call.cost_usd) == (None, Decimal("0.0001"))


def test_fetched_at_is_the_time_of_the_successful_attempt() -> None:
    ticks = iter(NOW + timedelta(seconds=10 * n) for n in range(10))
    responses: list[httpx2.Response | Exception] = [httpx2.Response(429), _ok()]

    page = _source(_sequence(responses, []), clock=lambda: next(ticks)).fetch_journal_page(
        QUERY, None
    )

    first, second = page.calls
    assert first.called_at < second.called_at
    assert [record.fetched_at for record in page.records] == [second.called_at]


def test_backoff_follows_retry_after_with_cap() -> None:
    source = _source(_sequence([], []))

    assert source._backoff(RetryableOpenAlexError("HTTP 429", retry_after=7)) == 7
    assert source._backoff(RetryableOpenAlexError("HTTP 429", retry_after=500)) == 120
    assert source._backoff(RetryableOpenAlexError("HTTP 503")) is True
    assert source._backoff(PaperSourceError("HTTP 401")) is False
    assert source._backoff(ValueError("x")) is False


def test_retry_after_from_response_reaches_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(_sequence([httpx2.Response(429, headers={"Retry-After": "7"}), _ok()], []))
    decisions: list[bool | float] = []
    original = source._backoff

    def spy(error: Exception) -> bool | float:
        decisions.append(original(error))
        return decisions[-1]

    monkeypatch.setattr(source, "_backoff", spy)

    source.fetch_journal_page(QUERY, None)

    assert decisions == [7.0]


def test_retries_exhausted_raise_with_every_call(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    seen: list[httpx2.Request] = []
    responses: list[httpx2.Response | Exception] = [httpx2.Response(429) for _ in range(3)]

    with pytest.raises(PaperSourceError) as caught:
        _source(_sequence(responses, seen)).fetch_journal_page(QUERY, None)

    assert len(seen) == 3
    assert [call.http_status for call in caught.value.calls] == [429, 429, 429]
    assert "HTTP 429" in str(caught.value)
    out = capsys.readouterr()
    assert FAKE_KEY not in str(caught.value)
    assert FAKE_KEY not in repr(caught.value)
    assert FAKE_KEY not in out.out + out.err
    assert "api.openalex.example" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("status", [400, 401, 403, 404, 302])
def test_other_statuses_are_not_retried(status: int) -> None:
    seen: list[httpx2.Request] = []

    with pytest.raises(PaperSourceError) as caught:
        _source(_sequence([httpx2.Response(status), _ok()], seen)).fetch_journal_page(QUERY, None)

    assert len(seen) == 1
    assert str(caught.value) == f"HTTP {status}"
    assert [call.http_status for call in caught.value.calls] == [status]
    assert FAKE_KEY not in str(caught.value)


# --- 壞回應 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"<html>oops</html>", "不是 JSON"),
        (b'{"results": ["\xff"]}', "不是 UTF-8"),
        (b"[1, 2]", "不是 JSON 物件"),
        (b'{"meta": {}}', "沒有 results"),
        (b'{"results": {"id": "W1"}}', "沒有 results"),
        (_body([{"title": "沒有 id"}]), "results.0 沒有 id"),
        (_body([_work(1), {"id": 5}]), "results.1 沒有 id"),
        (_body([{"id": "not an id"}]), "看不懂"),
        (_body([{"id": "https://openalex.org/A123"}]), "不是論文"),
        (_body(["W1"]), "results.0 沒有 id"),
        (_body([_work(1)], next_cursor=5), "next_cursor"),
        (_body([_work(1)], next_cursor=""), "next_cursor"),
    ],
)
def test_bad_responses_are_errors_without_retry(content: bytes, reason: str) -> None:
    seen: list[httpx2.Request] = []

    with pytest.raises(PaperSourceError, match=reason) as caught:
        _source(_sequence([_ok(content), _ok()], seen)).fetch_journal_page(QUERY, None)

    assert len(seen) == 1
    assert [call.http_status for call in caught.value.calls] == [200]


def test_cursor_that_does_not_advance_is_an_error() -> None:
    with pytest.raises(PaperSourceError, match="沒有前進"):
        _source(_sequence([_ok(_body([_work(1)], next_cursor="abc"))], [])).fetch_journal_page(
            QUERY, "abc"
        )


def test_first_page_cursor_star_returned_again_is_an_error() -> None:
    with pytest.raises(PaperSourceError, match="沒有前進"):
        _source(_sequence([_ok(_body([_work(1)], next_cursor="*"))], [])).fetch_journal_page(
            QUERY, None
        )


def test_empty_results_always_end_the_journal() -> None:
    page = _source(_sequence([_ok(_body([], next_cursor="still-more"))], [])).fetch_journal_page(
        QUERY, "abc"
    )

    assert page.records == ()
    assert page.next_cursor is None
    assert len(page.calls) == 1


def test_last_page_has_null_cursor() -> None:
    page = _source(_sequence([_ok(_body([_work(1)], next_cursor=None))], [])).fetch_journal_page(
        QUERY, "abc"
    )

    assert page.next_cursor is None


# --- 節流 ---------------------------------------------------------------------------------


def test_requests_are_throttled() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    responses: list[httpx2.Response | Exception] = [httpx2.Response(503), _ok(), _ok()]
    source = _source(_sequence(responses, []), sleep=sleep, monotonic=lambda: now[0])

    source.fetch_journal_page(QUERY, None)  # 503 → 重試：第二個請求要等 0.2 秒
    assert sleeps == [pytest.approx(0.2)]

    now[0] += 5  # 隔很久才抓下一頁：不用等
    source.fetch_journal_page(QUERY, "page-2")
    assert sleeps == [pytest.approx(0.2)]


# --- 原樣切片 -----------------------------------------------------------------------------


def test_slices_are_the_original_bytes() -> None:
    body = (
        b'{\n  "meta" : {"next_cursor": null},\n  "results"  :  [ \n'
        b'    {"id":"https://openalex.org/W1","n":1.50,"t":"\\u4e2d\xe6\x96\x87"} ,\n'
        b'\t{"id": "https://openalex.org/W2", "x": [1, {"y": "]"}]}\n  ] , "z": 1}\n'
    )

    slices, whole = slice_results(body)

    assert slices == [
        b'{"id":"https://openalex.org/W1","n":1.50,"t":"\\u4e2d\xe6\x96\x87"}',
        b'{"id": "https://openalex.org/W2", "x": [1, {"y": "]"}]}',
    ]
    assert whole["z"] == 1


def test_duplicate_results_key_uses_the_last_like_json_loads() -> None:
    body = b'{"results": [{"id": "W1"}], "results": [{"id": "W2"}, {"id": "W3"}]}'

    slices, _ = slice_results(body)

    assert slices == [b'{"id": "W2"}', b'{"id": "W3"}']


def test_deeply_nested_body_is_a_paper_source_error() -> None:
    deep = b"[" * 100_000 + b"]" * 100_000
    calls_seen: list[httpx2.Request] = []
    body = b'{"meta": {"next_cursor": null}, "results": [{"id": "W1", "x": ' + deep + b"}]}"

    with pytest.raises(PaperSourceError, match="巢狀太深") as caught:
        _source(_sequence([_ok(body)], calls_seen)).fetch_journal_page(QUERY, None)

    assert len(calls_seen) == 1
    assert [call.http_status for call in caught.value.calls] == [200]


@pytest.mark.parametrize(
    "extra",
    [{}, {"meta": "x"}, {"meta": {"count": 1}}],
    ids=["no-meta", "meta-not-object", "no-next-cursor-key"],
)
def test_results_without_next_cursor_are_an_error(extra: dict[str, Any]) -> None:
    body = json.dumps({"results": [_work(1)], **extra}).encode()

    with pytest.raises(PaperSourceError, match="next_cursor") as caught:
        _source(_sequence([_ok(body)], [])).fetch_journal_page(QUERY, None)

    assert len(caught.value.calls) == 1


def test_empty_results_slice() -> None:
    assert slice_results(b'{"results": []}')[0] == []


_JSON_SCALARS = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(st.characters(exclude_categories=["Cs"]))
)
_JSON_VALUES = st.recursive(
    _JSON_SCALARS,
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(st.characters(exclude_categories=["Cs"])), children, max_size=4)
    ),
    max_leaves=12,
)
_OBJECTS = st.lists(
    st.dictionaries(st.text(st.characters(exclude_categories=["Cs"])), _JSON_VALUES, max_size=5),
    max_size=6,
)


@given(
    results=_OBJECTS,
    other=st.dictionaries(st.sampled_from(["meta", "group_by", "a", "zz"]), _JSON_VALUES),
    indent=st.sampled_from([None, 0, 1, 2, "\t"]),
    separators=st.sampled_from([None, (",", ":"), (", ", ": "), (" ,  ", " :\t")]),
    ensure_ascii=st.booleans(),
    results_first=st.booleans(),
)
def test_slices_property(
    results: list[dict[str, Any]],
    other: dict[str, Any],
    indent: int | str | None,
    separators: tuple[str, str] | None,
    ensure_ascii: bool,
    results_first: bool,
) -> None:
    document = {"results": results, **other} if results_first else {**other, "results": results}
    text = json.dumps(document, indent=indent, separators=separators, ensure_ascii=ensure_ascii)
    body = text.encode("utf-8")

    slices, whole = slice_results(body)

    assert whole == json.loads(text)
    assert len(slices) == len(results)
    position = 0
    for piece, expected in zip(slices, results, strict=True):
        assert json.loads(piece) == expected
        found = body.find(piece, position)
        assert found >= 0  # 是原文的子字串，而且依序出現
        position = found + len(piece)


def test_satisfies_the_paper_source_port() -> None:
    source: PaperSource = _source(_sequence([], []))

    assert source.name is RawSource.OPENALEX
