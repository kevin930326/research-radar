"""心跳：排程工作成功後向外部監看服務報到一次（FR-48、AD-15、ADR-0004 決定第 7 點）。

一個排程一個網址（放設定）。services 收的是 `Callable[[str], None]`（檢查名稱），
真實的函式是這裡的 `HttpHeartbeat`，測試替身是 `radar.adapters.fake.FakeHeartbeat`。
網址裡有監看服務給的通行碼，日誌只記檢查名稱，不記網址。
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx2
import stamina
import structlog

log = structlog.get_logger(__name__)


class HeartbeatError(Exception):
    """送心跳失敗。訊息只有錯誤類別或 HTTP 狀態碼：原本的例外訊息可能帶出網址（含通行碼），
    重試時 stamina 會把例外寫進日誌，所以一律換成這個再往外丟。
    """


class RetryableHeartbeatError(HeartbeatError):
    """連線錯誤、逾時、429 或 5xx：值得再試一次。

    `retry_after`：對方用 `Retry-After` 標頭說要等幾秒（沒給或看不懂是 None）。
    """

    def __init__(self, reason: str, *, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """`Retry-After` 標頭換成要等的秒數：秒數或 HTTP 日期都接受；已經過了的日期是 0。

    沒給、格式不對（負數、亂碼）回 None，交給一般的退避。上限由呼叫的一方套。
    """
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except TypeError, ValueError, IndexError:
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


class HttpHeartbeat:
    """對檢查名稱的網址送一次 HTTP GET。

    - 這個檢查沒設網址：記警告、不送。
    - 連線錯誤、逾時、429、5xx：由 stamina 重試，最多 `attempts` 次（含第一次）。
      回應帶 `Retry-After` 時照對方給的秒數等，但最多等 `retry_after_max` 秒（對方給極大值時
      不卡住排程）；沒帶就指數退避：從 `wait_initial` 秒開始、每次加倍、最多 `wait_max` 秒，
      再加 0～`wait_jitter` 秒的隨機抖動。
    - 等待與次數的預設值和 `Settings` 的 `heartbeat_*` 一致；正式程式由 `bootstrap.py` 從設定傳入。
    - 送不出去：只記錯誤，不丟例外，不讓工作因此失敗。
    """

    def __init__(
        self,
        urls: Mapping[str, str],
        *,
        timeout: float,
        attempts: int,
        wait_initial: float = 1.0,
        wait_max: float = 10.0,
        retry_after_max: float = 30.0,
        wait_jitter: float = 1.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._urls = dict(urls)
        self._timeout = timeout
        self._attempts = attempts
        self._wait_initial = wait_initial
        self._wait_max = wait_max
        self._retry_after_max = retry_after_max
        self._wait_jitter = wait_jitter
        self._transport = transport

    def __call__(self, check: str) -> None:
        url = self._urls.get(check)
        if not url:
            log.warning("這個排程沒有設定心跳網址，不送", check=check)
            return
        try:
            self._send(url)
        except HeartbeatError as exc:
            log.error("心跳送出失敗", check=check, reason=str(exc))
            return
        except Exception as exc:
            # 例外訊息可能帶出網址（含通行碼），只記類別名稱。
            log.error("心跳送出失敗", check=check, reason=type(exc).__name__)
            return
        log.info("已送出心跳", check=check)

    def _backoff(self, error: Exception) -> bool | float:
        """stamina 的退避判斷：不重試回 False；照一般退避回 True；照 `Retry-After` 回秒數。"""
        if not isinstance(error, RetryableHeartbeatError):
            return False
        if error.retry_after is None:
            return True
        return min(error.retry_after, self._retry_after_max)

    def _send(self, url: str) -> None:
        with httpx2.Client(timeout=self._timeout, transport=self._transport) as client:
            for attempt in stamina.retry_context(
                on=self._backoff,
                attempts=self._attempts,
                timeout=None,
                wait_initial=self._wait_initial,
                wait_max=self._wait_max,
                wait_jitter=self._wait_jitter,
            ):
                with attempt:
                    _get_once(client, url)


def _get_once(client: httpx2.Client, url: str) -> None:
    """送一次 GET；失敗時丟不含網址的 `HeartbeatError`。"""
    try:
        response = client.get(url)
    except httpx2.TransportError as exc:
        raise RetryableHeartbeatError(type(exc).__name__) from None
    status = response.status_code
    if status == 429 or status >= 500:
        raise RetryableHeartbeatError(
            f"HTTP {status}", retry_after=parse_retry_after(response.headers.get("Retry-After"))
        )
    # 3xx 也算失敗：httpx2 不跟著轉址，監看服務其實沒收到。
    if status >= 300:
        raise HeartbeatError(f"HTTP {status}")
