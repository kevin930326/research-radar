"""S3 相容物件儲存的 adapter 對 moto（在行程裡模擬 S3，不連網路）。

put／get 往返、`ContentType`、找不到丟 `ObjectNotFoundError`；可重試的錯誤由 adapter 的
stamina 重試，而 botocore 自己不重試（每次嘗試只送一個請求）。
"""

from collections.abc import Iterator
from typing import Any

import pytest
import stamina
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from radar.adapters.object_store import S3ObjectStore, create_s3_client, is_retryable
from radar.domain.raw_records import compress_raw, decompress_raw
from radar.domain.storage_keys import RAW_CONTENT_TYPE, ObjectNotFoundError, raw_object_key
from radar.ports import ObjectStore

BUCKET = "radar-test"
KEY = raw_object_key("openalex", "a" * 64)


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Iterator[None]:
    with stamina.set_testing(True, attempts=100, cap=True):
        yield


@pytest.fixture
def client() -> Iterator[Any]:
    with mock_aws():
        s3 = create_s3_client(
            endpoint_url="https://s3.us-east-1.amazonaws.com",
            region="us-east-1",
            access_key_id="test-access-key",
            secret_access_key="test-secret-key",
            timeout=5,
        )
        s3.create_bucket(Bucket=BUCKET)
        yield s3


class _Raw:
    """`AWSResponse` 要的最小 raw：只會吐一次本文。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, **_kwargs: Any) -> Iterator[bytes]:
        yield self._body


def _fail_first(client: Any, operation: str, failures: list[Any]) -> list[str]:
    """`operation` 的前幾次請求在送出前換成 `failures` 裡的結果（回應或例外）。

    回傳每次請求的紀錄。
    """
    seen: list[str] = []

    def handler(request: Any, **_kwargs: Any) -> AWSResponse | None:
        seen.append(request.method)
        if not failures:
            return None
        failure = failures.pop(0)
        if isinstance(failure, Exception):
            raise failure
        status, code = failure
        body = f"<Error><Code>{code}</Code><Message>test</Message></Error>".encode()
        return AWSResponse(request.url, status, {}, _Raw(body))

    client.meta.events.register_first(f"before-send.s3.{operation}", handler)
    return seen


def test_put_get_round_trip_with_content_type(client: Any) -> None:
    store = S3ObjectStore(client, BUCKET, attempts=3)
    data = compress_raw(b'{"id": "https://openalex.org/W1"}')

    store.put(KEY, data, content_type=RAW_CONTENT_TYPE)

    assert store.get(KEY) == data
    assert decompress_raw(store.get(KEY)) == b'{"id": "https://openalex.org/W1"}'
    head = client.head_object(Bucket=BUCKET, Key=KEY)
    assert head["ContentType"] == "application/gzip"


def test_missing_key_raises_object_not_found(client: Any) -> None:
    store = S3ObjectStore(client, BUCKET, attempts=3)

    with pytest.raises(ObjectNotFoundError) as caught:
        store.get(raw_object_key("openalex", "b" * 64))

    assert caught.value.key == raw_object_key("openalex", "b" * 64)


@pytest.mark.parametrize(
    "failure",
    [
        (503, "SlowDown"),
        (500, "InternalError"),
        (429, "TooManyRequests"),
        EndpointConnectionError(endpoint_url="https://s3.us-east-1.amazonaws.com"),
    ],
)
def test_retryable_errors_are_retried_by_the_adapter_only(client: Any, failure: Any) -> None:
    store = S3ObjectStore(client, BUCKET, attempts=3)
    seen = _fail_first(client, "PutObject", [failure, failure])

    store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)

    # 兩次失敗＋一次成功：三個請求，代表 botocore 自己沒有多送。
    assert len(seen) == 3
    assert store.get(KEY) == b"data"


def test_gives_up_after_attempts(client: Any) -> None:
    store = S3ObjectStore(client, BUCKET, attempts=2)
    seen = _fail_first(client, "PutObject", [(503, "SlowDown")] * 5)

    with pytest.raises(ClientError) as caught:
        store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)

    assert caught.value.response["Error"]["Code"] == "SlowDown"
    assert len(seen) == 2


def test_get_is_retried_too(client: Any) -> None:
    store = S3ObjectStore(client, BUCKET, attempts=3)
    store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)
    seen = _fail_first(client, "GetObject", [(503, "SlowDown")])

    assert store.get(KEY) == b"data"
    assert len(seen) == 2


def test_other_client_errors_are_not_retried(client: Any) -> None:
    store = S3ObjectStore(client, BUCKET, attempts=3)
    seen = _fail_first(client, "PutObject", [(403, "AccessDenied")])

    with pytest.raises(ClientError):
        store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)

    assert len(seen) == 1


def test_is_retryable_classification() -> None:
    def client_error(code: str, status: int) -> ClientError:
        return ClientError(
            {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "PutObject"
        )

    assert is_retryable(client_error("SlowDown", 503))
    assert is_retryable(client_error("Whatever", 502))
    assert is_retryable(client_error("Whatever", 429))
    assert is_retryable(client_error("SlowDown", 400))
    assert not is_retryable(client_error("NoSuchKey", 404))
    assert not is_retryable(client_error("AccessDenied", 403))
    # 時鐘偏差等幾秒也不會好，不重試。
    assert not is_retryable(client_error("RequestTimeTooSkewed", 403))
    assert not is_retryable(ValueError("x"))


def test_satisfies_the_object_store_port(client: Any) -> None:
    store: ObjectStore = S3ObjectStore(client, BUCKET, attempts=1)

    store.put(KEY, b"x", content_type=RAW_CONTENT_TYPE)
    assert store.get(KEY) == b"x"
