"""心跳：排程工作成功後向外部監看服務報到一次（FR-48、AD-15、ADR-0004 決定第 7 點）。

一個排程一個網址（放設定）。services 收的是 `Callable[[str], None]`（檢查名稱），
真實的函式是這裡的 `HttpHeartbeat`，測試替身是 `radar.adapters.fake.FakeHeartbeat`。
網址裡有監看服務給的通行碼，日誌只記檢查名稱，不記網址。
"""

from collections.abc import Mapping

import httpx2
import stamina
import structlog

log = structlog.get_logger(__name__)


class HeartbeatError(Exception):
    """送心跳失敗。訊息只有錯誤類別或 HTTP 狀態碼：原本的例外訊息可能帶出網址（含通行碼），
    重試時 stamina 會把例外寫進日誌，所以一律換成這個再往外丟。
    """


class RetryableHeartbeatError(HeartbeatError):
    """連線錯誤、逾時、429 或 5xx：值得再試一次。"""


class HttpHeartbeat:
    """對檢查名稱的網址送一次 HTTP GET。

    - 這個檢查沒設網址：記警告、不送。
    - 連線錯誤、逾時、429、5xx：由 stamina 重試，最多 `attempts` 次（含第一次）。
    - 送不出去：只記錯誤，不丟例外，不讓工作因此失敗。
    """

    def __init__(
        self,
        urls: Mapping[str, str],
        *,
        timeout: float,
        attempts: int,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._urls = dict(urls)
        self._timeout = timeout
        self._attempts = attempts
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

    def _send(self, url: str) -> None:
        with httpx2.Client(timeout=self._timeout, transport=self._transport) as client:
            for attempt in stamina.retry_context(
                on=RetryableHeartbeatError, attempts=self._attempts, timeout=None
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
        raise RetryableHeartbeatError(f"HTTP {status}")
    # 3xx 也算失敗：httpx2 不跟著轉址，監看服務其實沒收到。
    if status >= 300:
        raise HeartbeatError(f"HTTP {status}")
