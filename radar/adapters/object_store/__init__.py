"""S3 相容物件儲存的 `ObjectStore`（R2、本機 RustFS；ADR-0014、ADR-0021）。

- boto3 client 由 `create_s3_client` 建：端點、區域、金鑰、逾時都明確傳入（不讀 AWS 的環境變數
  與設定檔裡的金鑰），botocore 自己的重試關掉（`total_max_attempts=1`）。
- 連線錯誤、逾時、5xx、429／SlowDown 由這裡的 stamina 重試，最多 `attempts` 次（含第一次）。
  錯誤回應帶 `Retry-After` 時照對方給的秒數等（ADR-0004）；要等的超過 `retry_after_max` 秒就
  不再重試，原本的錯誤直接往上丟（不提早重送）。沒帶就指數退避：從 `wait_initial` 秒開始、
  每次加倍、最多 `wait_max` 秒，再加 0～`wait_jitter` 秒的抖動。
- 交給 stamina 的例外換成不含網址的 `_RetryableStoreError`（訊息只有錯誤類別、錯誤碼、狀態碼），
  所以重試日誌不帶主機（D12(c)）。次數用完時：`ClientError` 照舊丟原本的例外（訊息本來就不含
  主機）；連線類的錯誤丟 `ObjectStoreUnavailableError`（訊息只有原本的錯誤類別）。
- `ClientError` 以外的 botocore 錯誤（`BotoCoreError`，例如 `SSLError`、`ProxyConnectionError`），
  不論可不可重試，最後一律換成不含網址的 `ObjectStoreUnavailableError`，在 `except` 外面丟，
  `__cause__`、`__context__` 都是 None：這些錯誤的訊息常帶端點網址（R2 的帳號 ID），工作失敗的
  traceback 會印出來。
- 等待與次數的預設值和 `Settings` 的 `object_store_*` 一致；正式程式由 `bootstrap.py` 從設定傳入。
- 讀不到的鍵丟 `ObjectNotFoundError`。
"""

from collections.abc import Callable
from typing import Any

import boto3
import stamina
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    IncompleteReadError,
    ReadTimeoutError,
    ResponseStreamingError,
)

from radar.adapters.heartbeat import parse_retry_after
from radar.domain.storage_keys import ObjectNotFoundError

# 這些錯誤碼不論狀態碼都值得再試一次（S3／R2 的節流與暫時性錯誤）。
RETRYABLE_ERROR_CODES = frozenset(
    {
        "SlowDown",
        "Throttling",
        "ThrottlingException",
        "RequestTimeout",
        "InternalError",
        "ServiceUnavailable",
        "TooManyRequests",
    }
)
NOT_FOUND_ERROR_CODES = frozenset({"NoSuchKey", "404", "NotFound"})
_TRANSIENT_ERRORS = (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    IncompleteReadError,
    ReadTimeoutError,
    ResponseStreamingError,
)


class ObjectStoreUnavailableError(Exception):
    """連線類的錯誤（連不上、逾時、讀到一半斷掉）重試用完，或其他不重試的 botocore 錯誤
    （例如 `SSLError`）。訊息只有原本的錯誤類別，不含網址。
    """

    def __init__(self, error_type: str, *, retried: bool = True) -> None:
        message = (
            f"物件儲存連線失敗（{error_type}），重試用完"
            if retried
            else f"物件儲存存取失敗（{error_type}），不重試"
        )
        super().__init__(message)
        self.error_type = error_type


class _RetryableStoreError(Exception):
    """交給 stamina 的可重試錯誤：訊息只有錯誤類別、錯誤碼與狀態碼，不含網址（stamina 重試時
    會把例外寫進日誌）。原本的例外放在 `original`，次數用完時再決定丟什麼。
    """

    def __init__(self, original: Exception) -> None:
        super().__init__(_describe(original))
        self.original = original


def _describe(error: Exception) -> str:
    """錯誤的固定描述：類別名稱，`ClientError` 再加錯誤碼與狀態碼。"""
    if isinstance(error, ClientError):
        response = error.response
        code = str(response.get("Error", {}).get("Code", ""))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return f"{type(error).__name__}（{code}，HTTP {status}）"
    return type(error).__name__


def create_s3_client(
    *,
    endpoint_url: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    timeout: float,
) -> Any:
    """建一個 S3 client；botocore 自己不重試（重試只在 `S3ObjectStore`）。不連線。"""
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(
            connect_timeout=timeout,
            read_timeout=timeout,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )


def is_retryable(error: Exception) -> bool:
    """連線錯誤、逾時、讀到一半斷掉、5xx、429 與 S3 的節流錯誤碼值得再試一次。"""
    if isinstance(error, _TRANSIENT_ERRORS):
        return True
    if isinstance(error, ClientError):
        response = error.response
        code = str(response.get("Error", {}).get("Code", ""))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return code in RETRYABLE_ERROR_CODES or (
            isinstance(status, int) and (status == 429 or status >= 500)
        )
    return False


class S3ObjectStore:
    """`put`／`get` 一個 bucket 裡的物件。"""

    def __init__(
        self,
        client: Any,
        bucket: str,
        *,
        attempts: int,
        wait_initial: float = 0.5,
        wait_max: float = 5.0,
        wait_jitter: float = 0.5,
        retry_after_max: float = 30.0,
    ) -> None:
        if not bucket:
            raise ValueError("bucket 不能是空的")
        self._client = client
        self._bucket = bucket
        self._attempts = attempts
        self._wait_initial = wait_initial
        self._wait_max = wait_max
        self._wait_jitter = wait_jitter
        self._retry_after_max = retry_after_max

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self._with_retry(
            lambda: self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )
        )

    def get(self, key: str) -> bytes:
        """讀出整個物件（讀到一半斷掉也重試）。

        Raises:
            ObjectNotFoundError: 沒有這個鍵。
        """

        def read() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body: bytes = response["Body"].read()
            return body

        try:
            return self._with_retry(read)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in NOT_FOUND_ERROR_CODES:
                raise ObjectNotFoundError(key) from None
            raise

    def _backoff(self, error: Exception) -> bool | float:
        """stamina 的退避判斷：不重試回 False；照一般退避回 True；照 `Retry-After` 回秒數。

        `Retry-After` 要等的超過 `retry_after_max` 秒時回 False：不等也不提早重送（那會違反
        對方的要求），原本的錯誤直接往上丟，這次存取算失敗（ADR-0021）。
        `_RetryableStoreError` 依它包著的原本的例外判斷。
        """
        if isinstance(error, _RetryableStoreError):
            error = error.original
        if not is_retryable(error):
            return False
        retry_after = parse_retry_after(_retry_after_header(error))
        if retry_after is None:
            return True
        if retry_after > self._retry_after_max:
            return False
        return retry_after

    def _with_retry[T](self, operation: Callable[[], T]) -> T:
        """重試 `operation`；可重試的錯誤換成不含網址的 `_RetryableStoreError` 交給 stamina。

        Raises:
            ClientError: 不重試的錯誤回應，或可重試的錯誤回應次數用完（原本的例外）。
            ObjectStoreUnavailableError: 連線類的錯誤次數用完，或其他不重試的 botocore 錯誤
                （`BotoCoreError`）；都不帶原本的例外（訊息可能含端點網址）。

        換成的例外都在 `except` 區塊外面丟：在裡面丟時 `from None` 只設 `__suppress_context__`，
        `__context__` 仍串著原本含網址的例外，Procrastinate 與日誌可能沿著它印出來。
        """

        def once() -> T:
            error: Exception
            try:
                return operation()
            except Exception as exc:
                if is_retryable(exc):
                    error = _RetryableStoreError(exc)
                elif isinstance(exc, BotoCoreError):
                    error = ObjectStoreUnavailableError(type(exc).__name__, retried=False)
                else:
                    raise
            raise error from None

        original: Exception | None = None
        try:
            for attempt in stamina.retry_context(
                on=self._backoff,
                attempts=self._attempts,
                timeout=None,
                wait_initial=self._wait_initial,
                wait_max=self._wait_max,
                wait_jitter=self._wait_jitter,
            ):
                with attempt:
                    return once()
        except _RetryableStoreError as exc:
            original = exc.original
        if original is None:
            raise AssertionError("stamina 用完次數時會丟出最後一個例外，不會走到這裡")
        if isinstance(original, ClientError):
            raise original from None
        raise ObjectStoreUnavailableError(type(original).__name__) from None


def _retry_after_header(error: Exception) -> str | None:
    """錯誤回應的 `Retry-After` 標頭；不是錯誤回應或沒帶時是 None。

    botocore 通常把標頭名稱轉成小寫，這裡不分大小寫比對。
    """
    if not isinstance(error, ClientError):
        return None
    headers = error.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
    if not isinstance(headers, dict):
        return None
    for name, value in headers.items():
        if str(name).lower() == "retry-after":
            return str(value)
    return None
