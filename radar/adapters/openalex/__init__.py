"""OpenAlex 的 `PaperSource`（FR-10、AD-25、ADR-0014、ADR-0021）。

- 只用清單端點 `GET {base_url}/works`：依期刊 ISSN-L 與起始年篩、cursor 分頁、帶 API key；
  不用 search 端點，也不加 type／DOI 篩選（那是解析後的過濾）。
- 回傳逐篇的原始紀錄：回應原文裡那一篇的原樣位元組切片，不解析、不重新序列化。
- 節流：每秒最多 `requests_per_second` 個請求（含重試）。
- 409、429、5xx、連線錯誤與逾時由 stamina 指數退避重試；帶 `Retry-After` 時照對方給的秒數等
  （ADR-0004）。要等的超過 `retry_after_max_seconds` 就不再重試、丟 `PaperSourceError`（不提早
  重送）。其他 4xx 不重試。
- 每次可重試的失敗後先看對方回報的額度：「上限 − 剩餘」到 `quota.stop_at_usd` 或剩 0 就不再
  重試，丟 `QuotaReachedError`（帶著每次嘗試），由 services 照「額度停下」處理、不算失敗。
- 每次 HTTP 嘗試都記一筆 `ApiCall`；失敗時 `PaperSourceError` 帶著它們。確定沒送出的
  （ConnectError、ConnectTimeout）記 0 元，其他沒有回應標頭的錯誤照預設單價算。
- 錯誤訊息只有固定的原因（狀態碼、錯誤類別），不含網址與 key：網址裡有 key，重試時 stamina
  會把例外寫進日誌。
"""

import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import Any, Self

import httpx2
import stamina

from radar.adapters.heartbeat import parse_retry_after
from radar.domain.domain_config import OpenAlexConfig
from radar.domain.identifiers import InvalidIdentifierError, normalize_openalex_id
from radar.domain.raw_records import (
    MAX_USD,
    ApiCall,
    JournalQuery,
    PaperSourceError,
    QuotaReachedError,
    RawRecord,
    RawSource,
    SourcePage,
)

LIST_WORKS = "list_works"
FIRST_CURSOR = "*"
COST_HEADER = "x-ratelimit-cost-usd"
REMAINING_HEADER = "x-ratelimit-remaining-usd"
LIMIT_HEADER = "x-ratelimit-limit-usd"
RETRY_STATUSES = frozenset({409, 429})

# JSON 的空白只有這四種（RFC 8259）。
_JSON_WHITESPACE = re.compile(r"[ \t\n\r]*")


class RetryableOpenAlexError(Exception):
    """值得再試一次的失敗（409、429、5xx、連線錯誤、逾時）。訊息不含網址與 key。

    `retry_after`：對方用 `Retry-After` 說要等幾秒（沒給或看不懂是 None）。
    """

    def __init__(self, reason: str, *, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OpenAlexSource:
    """依期刊 cursor 分頁抓 OpenAlex 的 works。用完呼叫 `close()`（或用 `with`）。"""

    name = RawSource.OPENALEX

    def __init__(
        self,
        api_key: str,
        config: OpenAlexConfig,
        *,
        transport: httpx2.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("api_key 不能是空的")
        self._api_key = api_key
        self._config = config
        self._sleep = sleep
        self._monotonic = monotonic
        self._clock = clock
        self._interval = 1.0 / config.requests_per_second
        self._last_request: float | None = None
        self._client = httpx2.Client(timeout=config.timeout_seconds, transport=transport)

    def __repr__(self) -> str:
        # 不印 key。
        return f"OpenAlexSource(base_url={self._config.base_url!r})"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def fetch_journal_page(self, query: JournalQuery, cursor: str | None) -> SourcePage:
        """抓這本期刊的一頁；`cursor=None` 是第一頁（送 `cursor=*`）。

        Raises:
            QuotaReachedError: 重試途中對方回報額度已到停下的比例；`calls` 帶著每次嘗試。
            PaperSourceError: 重試用完、不重試的狀態碼或回應看不懂；`calls` 帶著每次嘗試。
        """
        sent_cursor = FIRST_CURSOR if cursor is None else cursor
        params = {
            "filter": (
                f"primary_location.source.issn:{query.issn_l},"
                f"publication_year:>{query.start_year - 1}"
            ),
            "per-page": str(self._config.per_page),
            "cursor": sent_cursor,
            "api_key": self._api_key,
        }
        calls: list[ApiCall] = []
        retry = self._config.retry
        try:
            for attempt in stamina.retry_context(
                on=self._backoff,
                attempts=retry.attempts,
                timeout=None,
                wait_initial=retry.wait_initial_seconds,
                wait_max=retry.wait_max_seconds,
                wait_jitter=retry.wait_jitter_seconds,
            ):
                with attempt:
                    response = self._get_once(params, calls)
        except RetryableOpenAlexError as exc:
            if self._retry_after_too_long(exc):
                raise PaperSourceError(
                    f"嘗試 {len(calls)} 次後停止（{exc.reason}；對方要求等 {exc.retry_after:g} 秒，"
                    f"超過上限 {retry.retry_after_max_seconds:g} 秒）",
                    calls=calls,
                ) from None
            raise PaperSourceError(
                f"嘗試 {len(calls)} 次仍失敗（{exc.reason}）", calls=calls
            ) from None
        except QuotaReachedError as exc:
            raise QuotaReachedError(exc.reason, calls=calls) from None
        except PaperSourceError as exc:
            raise PaperSourceError(exc.reason, calls=calls) from None

        fetched_at = calls[-1].called_at
        try:
            return _parse_page(response.content, sent_cursor, fetched_at, calls)
        except PaperSourceError as exc:
            raise PaperSourceError(exc.reason, calls=calls) from None

    def _backoff(self, error: Exception) -> bool | float:
        """stamina 的退避判斷：不重試回 False；照一般退避回 True；照 `Retry-After` 回秒數。

        `Retry-After` 要等的超過 `retry_after_max_seconds` 時回 False：不等也不提早重送（那會
        違反對方的要求），由 `fetch_journal_page` 轉成 `PaperSourceError`，這一頁算失敗。
        """
        if not isinstance(error, RetryableOpenAlexError):
            return False
        if error.retry_after is None:
            return True
        if self._retry_after_too_long(error):
            return False
        return error.retry_after

    def _retry_after_too_long(self, error: RetryableOpenAlexError) -> bool:
        """對方用 `Retry-After` 要求等的秒數超過 `retry_after_max_seconds`（等於上限還是照等）。"""
        return (
            error.retry_after is not None
            and error.retry_after > self._config.retry.retry_after_max_seconds
        )

    def _throttle(self) -> None:
        """離上一個請求不到 1 / requests_per_second 秒就先等。"""
        if self._last_request is not None:
            wait = self._last_request + self._interval - self._monotonic()
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._monotonic()

    def _get_once(self, params: Mapping[str, str], calls: list[ApiCall]) -> httpx2.Response:
        """送一次請求並記一筆 `ApiCall`；可重試的失敗丟 `RetryableOpenAlexError`。"""
        self._throttle()
        try:
            response = self._client.get(f"{self._config.base_url}/works", params=params)
        except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
            # 連線都沒建立：請求確定沒送出，不花額度。
            calls.append(self._call(None, {}, cost=Decimal(0)))
            raise RetryableOpenAlexError(type(exc).__name__) from None
        except httpx2.TransportError as exc:
            # 讀寫逾時、連線中斷等：請求可能已經送到對方、被計費，照預設單價算（寧可高估）。
            calls.append(self._call(None, {}))
            raise RetryableOpenAlexError(type(exc).__name__) from None
        except httpx2.HTTPError as exc:
            # 例如回應的壓縮格式解不開（DecodingError）：對方已經回應，照預設單價算，不重試。
            calls.append(self._call(None, {}))
            raise PaperSourceError(type(exc).__name__) from None

        status = response.status_code
        call = self._call(status, response.headers)
        calls.append(call)
        if status in RETRY_STATUSES or status >= 500:
            if self._quota_reached(call):
                # 額度已經到了，再試只會繼續被擋（還可能再扣錢）：停下，交給 services。
                raise QuotaReachedError(f"HTTP {status}，對方回報的額度已到停下的比例")
            raise RetryableOpenAlexError(
                f"HTTP {status}",
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if status != 200:
            raise PaperSourceError(f"HTTP {status}")
        return response

    def _quota_reached(self, call: ApiCall) -> bool:
        """這次回應回報的額度到了沒：剩 0，或「上限 − 剩餘」到 `quota.stop_at_usd`。

        門檻和 services 讀用量時用的是同一條線（`daily_budget_usd × stop_at_ratio`）。
        """
        remaining, limit = call.remaining_usd, call.limit_usd
        if remaining is None:
            return False
        if remaining <= 0:
            return True
        return limit is not None and limit - remaining >= self._config.quota.stop_at_usd

    def _call(
        self, status: int | None, headers: Mapping[str, str], *, cost: Decimal | None = None
    ) -> ApiCall:
        if cost is None:
            cost = _usd_header(headers, COST_HEADER)
            if cost is None:
                cost = self._config.quota.default_request_cost_usd
        return ApiCall(
            source=RawSource.OPENALEX,
            operation=LIST_WORKS,
            http_status=status,
            cost_usd=cost,
            remaining_usd=_usd_header(headers, REMAINING_HEADER),
            limit_usd=_usd_header(headers, LIMIT_HEADER),
            called_at=self._clock(),
        )


def _usd_header(headers: Mapping[str, str], name: str) -> Decimal | None:
    """金額標頭換成 `Decimal`；沒有、看不懂、負數或太大都回 None。"""
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        return None
    if not value.is_finite() or not Decimal(0) <= value < MAX_USD:
        return None
    return value


def _parse_page(
    body: bytes, sent_cursor: str, fetched_at: datetime, calls: list[ApiCall]
) -> SourcePage:
    """把一頁回應拆成逐篇的原始紀錄。看不懂就丟 `PaperSourceError`（不重試）。"""
    slices, whole = slice_results(body)
    records: list[RawRecord] = []
    for index, (content, item) in enumerate(zip(slices, whole["results"], strict=True)):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise PaperSourceError(f"results.{index} 沒有 id")
        try:
            external_key = normalize_openalex_id(item["id"])
        except InvalidIdentifierError:
            raise PaperSourceError(f"results.{index} 的 id 看不懂") from None
        if not external_key.startswith("W"):
            raise PaperSourceError(f"results.{index} 的 id 不是論文（W…）")
        records.append(RawRecord(RawSource.OPENALEX, external_key, content, fetched_at))

    meta = whole.get("meta")
    if not records:
        # 結果是空的就當作抓完，不管對方給不給下一頁的 cursor。
        return SourcePage((), None, tuple(calls))
    if not isinstance(meta, dict) or "next_cursor" not in meta:
        # 有結果卻不知道下一頁：當成抓完會默默漏掉後面的頁，所以丟錯。
        raise PaperSourceError("回應沒有 meta.next_cursor")
    next_cursor = meta["next_cursor"]
    if next_cursor is not None:
        if not isinstance(next_cursor, str) or not next_cursor:
            raise PaperSourceError("meta.next_cursor 要是字串或 null")
        if next_cursor == sent_cursor:
            raise PaperSourceError("next_cursor 沒有前進")
    return SourcePage(tuple(records), next_cursor, tuple(calls))


def slice_results(body: bytes) -> tuple[list[bytes], dict[str, Any]]:
    """回應原文裡 `results` 陣列每個元素的原樣位元組切片，以及整份解析的結果。

    UTF-8 解碼後用 `json.JSONDecoder().raw_decode` 走過頂層物件，記下 `results` 每個元素在原文的
    起訖位置切出來、再編回 UTF-8：合法的 UTF-8 解碼再編碼是原樣，所以切片就是對方給的位元組。
    切完每一段再解析一次，和整份解析的對應元素比對，不相等就丟錯（切錯要大聲失敗）。

    Raises:
        PaperSourceError: 不是 UTF-8、不是 JSON 物件、沒有 `results` 陣列，或切片對不上。
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise PaperSourceError("回應不是 UTF-8") from None
    try:
        whole = json.loads(text)
    except ValueError:
        raise PaperSourceError("回應不是 JSON") from None
    except RecursionError:
        raise PaperSourceError("回應的 JSON 巢狀太深") from None
    if not isinstance(whole, dict):
        raise PaperSourceError("回應不是 JSON 物件")
    results = whole.get("results")
    if not isinstance(results, list):
        raise PaperSourceError("回應沒有 results 陣列")

    try:
        spans = _results_spans(text)
    except ValueError, IndexError, RecursionError:
        raise PaperSourceError("回應的 results 切不出來") from None
    if spans is None or len(spans) != len(results):
        raise PaperSourceError("回應的 results 切不出來")

    slices: list[bytes] = []
    for (start, end), expected in zip(spans, results, strict=True):
        piece = text[start:end]
        try:
            same = json.loads(piece) == expected
        except RecursionError:
            raise PaperSourceError("回應的 JSON 巢狀太深") from None
        if not same:
            raise PaperSourceError("切片和整份解析的結果不一樣")
        slices.append(piece.encode("utf-8"))
    return slices, whole


def _skip_whitespace(text: str, position: int) -> int:
    match = _JSON_WHITESPACE.match(text, position)
    # 這個樣式可以配到空字串，所以一定配得到；None 的分支只是給型別檢查看的。
    return position if match is None else match.end()


def _expect(text: str, position: int, character: str) -> int:
    if text[position] != character:
        raise ValueError(f"第 {position} 個字元應該是 {character!r}")
    return position + 1


def _results_spans(text: str) -> list[tuple[int, int]] | None:
    """頂層物件裡 `results` 陣列每個元素的（起, 訖）位置；沒有 `results` 回 None。

    同一個鍵出現兩次時取最後一個（和 `json.loads` 一樣）。
    """
    decoder = json.JSONDecoder()
    position = _expect(text, _skip_whitespace(text, 0), "{")
    position = _skip_whitespace(text, position)
    spans: list[tuple[int, int]] | None = None
    if text[position] == "}":
        return None
    while True:
        key, position = decoder.raw_decode(text, position)
        if not isinstance(key, str):
            raise ValueError("物件的鍵要是字串")
        position = _skip_whitespace(text, _expect(text, _skip_whitespace(text, position), ":"))
        if key == "results" and text[position] == "[":
            spans, position = _array_spans(decoder, text, position)
        else:
            _, position = decoder.raw_decode(text, position)
        position = _skip_whitespace(text, position)
        if text[position] == ",":
            position = _skip_whitespace(text, position + 1)
            continue
        _expect(text, position, "}")
        return spans


def _array_spans(
    decoder: json.JSONDecoder, text: str, position: int
) -> tuple[list[tuple[int, int]], int]:
    """從 `[` 開始的陣列，每個元素的（起, 訖）位置，以及 `]` 後面的位置。"""
    position = _skip_whitespace(text, _expect(text, position, "["))
    spans: list[tuple[int, int]] = []
    if text[position] == "]":
        return spans, position + 1
    while True:
        _, end = decoder.raw_decode(text, position)
        spans.append((position, end))
        position = _skip_whitespace(text, end)
        if text[position] == ",":
            position = _skip_whitespace(text, position + 1)
            continue
        return spans, _expect(text, position, "]")
