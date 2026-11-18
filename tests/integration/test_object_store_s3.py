"""S3 相容物件儲存的 adapter 對 moto（在行程裡模擬 S3，不連網路）。

put／get 往返、`ContentType`、找不到丟 `ObjectNotFoundError`；可重試的錯誤由 adapter 的
stamina 重試，而 botocore 自己不重試（每次嘗試只送一個請求）；重試前等多久（`Retry-After`、
退避設定）攔下 `time.sleep` 檢查，不真的等。
"""

import time
import traceback
from collections.abc import Iterator
from typing import Any

import pytest
import stamina
from botocore.awsrequest import AWSResponse
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ProxyConnectionError,
    SSLError,
)
from moto import mock_aws
from structlog.testing import capture_logs

from radar.adapters.object_store import (
    ObjectStoreUnavailableError,
    S3ObjectStore,
    create_s3_client,
    is_retryable,
)
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

    回應寫成（狀態碼, 錯誤碼）或（狀態碼, 錯誤碼, 回應標頭）。

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
        status, code, *rest = failure
        headers = rest[0] if rest else {}
        body = f"<Error><Code>{code}</Code><Message>test</Message></Error>".encode()
        return AWSResponse(request.url, status, headers, _Raw(body))

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


# --- 重試日誌與用完次數的訊息不帶主機（D12(c)）------------------------------------------------

HOST = "s3.us-east-1.amazonaws.com"


def test_retry_logs_do_not_carry_the_host(client: Any) -> None:
    # stamina 重試時把例外的 repr 寫進日誌；botocore 的連線錯誤訊息帶整個端點網址。
    store = S3ObjectStore(client, BUCKET, attempts=3)
    failure = EndpointConnectionError(endpoint_url=f"https://{HOST}")
    assert HOST in str(failure)
    seen = _fail_first(client, "PutObject", [failure, failure])

    with capture_logs() as logs:
        store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)

    assert len(seen) == 3
    retries = [log for log in logs if log["event"] == "stamina.retry_scheduled"]
    assert len(retries) == 2
    assert all("EndpointConnectionError" in log["caused_by"] for log in retries)
    assert HOST not in str(logs)
    assert store.get(KEY) == b"data"


def test_retry_logs_of_error_responses_keep_code_and_status(client: Any) -> None:
    store = S3ObjectStore(client, BUCKET, attempts=2)
    _fail_first(client, "PutObject", [(503, "SlowDown")])

    with capture_logs() as logs:
        store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)

    [retry] = [log for log in logs if log["event"] == "stamina.retry_scheduled"]
    assert "SlowDown" in retry["caused_by"]
    assert "503" in retry["caused_by"]
    assert HOST not in str(logs)


def test_connection_errors_out_of_attempts_raise_without_the_host(client: Any) -> None:
    store = S3ObjectStore(client, BUCKET, attempts=2)
    failure = EndpointConnectionError(endpoint_url=f"https://{HOST}")
    seen = _fail_first(client, "PutObject", [failure] * 5)

    with capture_logs() as logs, pytest.raises(ObjectStoreUnavailableError) as caught:
        store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)

    assert len(seen) == 2
    assert caught.value.error_type == "EndpointConnectionError"
    assert "EndpointConnectionError" in str(caught.value)
    assert HOST not in str(caught.value)
    # 例外鏈也斷開：印出 traceback 時不會帶出原本含網址的例外。
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert HOST not in str(logs)


def test_get_connection_errors_out_of_attempts_also_raise_without_the_host(client: Any) -> None:
    store = S3ObjectStore(client, BUCKET, attempts=2)
    store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)
    _fail_first(client, "GetObject", [EndpointConnectionError(endpoint_url=f"https://{HOST}")] * 5)

    with pytest.raises(ObjectStoreUnavailableError) as caught:
        store.get(KEY)

    assert HOST not in str(caught.value)


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


# --- 重試前等多久（Codex 審查第 4、6 點）------------------------------------------------------


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """關掉 stamina 的測試模式（它會把等待歸零），改成攔下 sleep、只記秒數。"""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    with stamina.set_testing(False):
        yield slept


def _timed_store(client: Any, *, attempts: int = 2, retry_after_max: float = 120) -> S3ObjectStore:
    return S3ObjectStore(
        client,
        BUCKET,
        attempts=attempts,
        wait_initial=2.0,
        wait_max=3.0,
        wait_jitter=0.0,
        retry_after_max=retry_after_max,
    )


def test_429_waits_the_seconds_given_by_retry_after(client: Any, waits: list[float]) -> None:
    store = _timed_store(client)
    seen = _fail_first(client, "PutObject", [(429, "SlowDown", {"Retry-After": "60"})])

    store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)

    assert len(seen) == 2
    assert waits == [60.0]


def test_retry_after_over_the_cap_fails_without_waiting_or_resending(
    client: Any, waits: list[float]
) -> None:
    # 對方要等一天、超過 retry_after_max：不等、不提早重送，原本的錯誤直接往上丟
    # （Codex 補複查、Kevin 2026-11-14 決定；以前是截短成 30 秒就重送）。
    store = _timed_store(client, attempts=3, retry_after_max=30)
    seen = _fail_first(client, "GetObject", [(503, "SlowDown", {"Retry-After": "86400"})])
    client.put_object(Bucket=BUCKET, Key=KEY, Body=b"data")

    with pytest.raises(ClientError) as caught:
        store.get(KEY)

    assert caught.value.response["Error"]["Code"] == "SlowDown"
    assert len(seen) == 1
    assert waits == []
    message = str(caught.value)
    for secret in ("s3.us-east-1.amazonaws.com", "test-access-key", "test-secret-key"):
        assert secret not in message


def test_retry_after_equal_to_the_cap_is_waited_in_full(client: Any, waits: list[float]) -> None:
    store = _timed_store(client, attempts=3, retry_after_max=30)
    seen = _fail_first(client, "PutObject", [(429, "SlowDown", {"Retry-After": "30"})])

    store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)

    assert len(seen) == 2
    assert waits == [30.0]
    assert store.get(KEY) == b"data"


def test_without_retry_after_uses_configured_backoff(client: Any, waits: list[float]) -> None:
    # 從 wait_initial（2）開始每次加倍、最多 wait_max（3）；抖動 0；次數照 attempts。
    store = _timed_store(client, attempts=4)
    failures: list[Any] = [(503, "SlowDown"), (429, "TooManyRequests"), (500, "InternalError")]
    seen = _fail_first(client, "PutObject", failures)

    store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)

    assert len(seen) == 4
    assert waits == [2.0, 3.0, 3.0]


def test_unreadable_retry_after_falls_back_to_backoff(client: Any, waits: list[float]) -> None:
    store = _timed_store(client)
    seen = _fail_first(client, "PutObject", [(429, "SlowDown", {"Retry-After": "soon"})])

    store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)

    assert len(seen) == 2
    assert waits == [2.0]


def test_backoff_reads_retry_after_from_the_error_response() -> None:
    store = S3ObjectStore(object(), BUCKET, attempts=3, retry_after_max=120)

    def client_error(status: int, headers: dict[str, str]) -> ClientError:
        metadata = {"HTTPStatusCode": status, "HTTPHeaders": headers}
        return ClientError(
            {"Error": {"Code": "SlowDown"}, "ResponseMetadata": metadata}, "PutObject"
        )

    assert store._backoff(client_error(429, {"retry-after": "60"})) == 60.0
    # 等於上限照等；超過上限不重試（不提早重送）。
    assert store._backoff(client_error(503, {"Retry-After": "120"})) == 120
    assert store._backoff(client_error(503, {"Retry-After": "500"})) is False
    assert store._backoff(client_error(503, {})) is True
    assert store._backoff(ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")) is False
    assert store._backoff(ValueError("x")) is False


def test_satisfies_the_object_store_port(client: Any) -> None:
    store: ObjectStore = S3ObjectStore(client, BUCKET, attempts=1)

    store.put(KEY, b"x", content_type=RAW_CONTENT_TYPE)
    assert store.get(KEY) == b"x"


# --- 其他 botocore 錯誤（對抗式審查第 2 條）--------------------------------------------------


def _access(store: S3ObjectStore, operation: str) -> None:
    if operation == "GetObject":
        store.get(KEY)
    else:
        store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)


@pytest.mark.parametrize(
    "failure",
    [
        SSLError(endpoint_url=f"https://{HOST}", error="certificate verify failed"),
        ProxyConnectionError(proxy_url=f"http://proxy.{HOST}:3128", error="refused"),
    ],
    ids=["SSLError", "ProxyConnectionError"],
)
@pytest.mark.parametrize("operation", ["PutObject", "GetObject"])
def test_other_botocore_errors_raise_without_the_host_and_are_not_retried(
    client: Any, failure: Exception, operation: str
) -> None:
    # 不在可重試清單裡的 botocore 錯誤：不重試，換成不含網址的例外，例外鏈也斷開。
    store = S3ObjectStore(client, BUCKET, attempts=3)
    store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)
    seen = _fail_first(client, operation, [failure] * 5)

    with capture_logs() as logs, pytest.raises(ObjectStoreUnavailableError) as caught:
        _access(store, operation)

    assert len(seen) == 1
    assert HOST in str(failure)
    assert caught.value.error_type == type(failure).__name__
    assert type(failure).__name__ in str(caught.value)
    assert "不重試" in str(caught.value)
    assert HOST not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert HOST not in str(logs)


# --- 例外鏈完全斷開（Codex 補審 C1）---------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "expected_requests"),
    [
        (EndpointConnectionError(endpoint_url=f"https://{HOST}"), 2),
        (SSLError(endpoint_url=f"https://{HOST}", error="certificate verify failed"), 1),
    ],
    ids=["retryable-out-of-attempts", "not-retryable"],
)
@pytest.mark.parametrize("operation", ["PutObject", "GetObject"])
def test_unavailable_error_carries_no_exception_chain_at_all(
    client: Any, failure: Exception, expected_requests: int, operation: str
) -> None:
    # `from None` 在 except 裡丟時 `__context__` 仍串著原例外；Procrastinate 與日誌可能沿著它印出
    # 主機，所以 `__cause__`、`__context__` 都要是 None，完整的 traceback 也不能有主機。
    store = S3ObjectStore(client, BUCKET, attempts=2)
    store.put(KEY, b"data", content_type=RAW_CONTENT_TYPE)
    seen = _fail_first(client, operation, [failure] * 5)

    with pytest.raises(ObjectStoreUnavailableError) as caught:
        _access(store, operation)

    assert len(seen) == expected_requests
    assert HOST in str(failure)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    printed = "".join(traceback.format_exception(caught.value))
    assert "ObjectStoreUnavailableError" in printed
    assert HOST not in printed


# --- 串流上傳（Story 3.18 的備份）---------------------------------------------------------------


class _Chunks:
    """`read(size)` 一段一段吐出 `total` 個位元組；`fail_after` 有給時讀到那裡就丟例外。"""

    def __init__(self, total: int, *, fail_after: int | None = None) -> None:
        self._left = total
        self._sent = 0
        self._fail_after = fail_after

    def read(self, size: int = -1, /) -> bytes:
        if self._fail_after is not None and self._sent >= self._fail_after:
            raise RuntimeError("backup tool failed")
        n = self._left if size < 0 else min(size, self._left)
        self._left -= n
        self._sent += n
        return b"x" * n


def test_put_stream_uploads_in_parts_with_content_type(client: Any) -> None:
    store = S3ObjectStore(client, BUCKET, attempts=3)
    total = 9 * 1024 * 1024  # 超過一段（8 MiB），走分段上傳

    store.put_stream("backup/full.age", _Chunks(total), content_type="application/octet-stream")

    head = client.head_object(Bucket=BUCKET, Key="backup/full.age")
    assert head["ContentLength"] == total
    assert head["ContentType"] == "application/octet-stream"


def test_put_stream_failing_mid_way_leaves_no_key_and_no_open_upload(client: Any) -> None:
    store = S3ObjectStore(client, BUCKET, attempts=3)
    stream = _Chunks(20 * 1024 * 1024, fail_after=8 * 1024 * 1024)

    with pytest.raises(RuntimeError, match="backup tool failed"):
        store.put_stream("backup/half.age", stream, content_type="application/octet-stream")

    assert client.list_objects_v2(Bucket=BUCKET, Prefix="backup/").get("KeyCount") == 0
    assert client.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []


def test_put_stream_s3_error_is_not_retried_and_hides_the_host(client: Any) -> None:
    store = S3ObjectStore(client, BUCKET, attempts=3)
    seen = _fail_first(client, "PutObject", [(503, "SlowDown")])

    with pytest.raises(ObjectStoreUnavailableError) as caught:
        store.put_stream("backup/small.age", _Chunks(10), content_type="application/octet-stream")

    assert seen == ["PUT"]
    assert "amazonaws" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_put_stream_failing_at_the_end_of_a_small_stream_leaves_no_key(client: Any) -> None:
    """Story 3.18 審查 T3：小於一段（單次 PutObject）、讀到結尾才丟例外（`pg_dump` 一開始就失敗
    時多半走這條）：例外原樣往上丟，`backup/` 沒有鍵。"""
    store = S3ObjectStore(client, BUCKET, attempts=3)
    stream = _Chunks(10, fail_after=10)

    with pytest.raises(RuntimeError, match="backup tool failed"):
        store.put_stream("backup/small.age", stream, content_type="application/octet-stream")

    assert client.list_objects_v2(Bucket=BUCKET, Prefix="backup/").get("KeyCount") == 0
    assert client.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []
