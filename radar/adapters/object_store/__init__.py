"""S3 相容物件儲存的 `ObjectStore`（R2、本機 RustFS；ADR-0014、ADR-0021）。

- boto3 client 由 `create_s3_client` 建：端點、區域、金鑰、逾時都明確傳入（不讀 AWS 的環境變數
  與設定檔裡的金鑰），botocore 自己的重試關掉（`total_max_attempts=1`）。
- 連線錯誤、逾時、5xx、429／SlowDown 由這裡的 stamina 重試，最多 `attempts` 次（含第一次）。
- 讀不到的鍵丟 `ObjectNotFoundError`。
"""

from collections.abc import Callable
from typing import Any

import boto3
import stamina
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    IncompleteReadError,
    ReadTimeoutError,
    ResponseStreamingError,
)

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
    ) -> None:
        if not bucket:
            raise ValueError("bucket 不能是空的")
        self._client = client
        self._bucket = bucket
        self._attempts = attempts
        self._wait_initial = wait_initial
        self._wait_max = wait_max
        self._wait_jitter = wait_jitter

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

    def _with_retry[T](self, operation: Callable[[], T]) -> T:
        for attempt in stamina.retry_context(
            on=is_retryable,
            attempts=self._attempts,
            timeout=None,
            wait_initial=self._wait_initial,
            wait_max=self._wait_max,
            wait_jitter=self._wait_jitter,
        ):
            with attempt:
                return operation()
        raise AssertionError("stamina 用完次數時會丟出最後一個例外，不會走到這裡")
