"""Workers AI 的向量化 adapter（`httpx2.MockTransport`，一個真的請求都不送）。

假回應不是真的錄的，形狀照官方 schema：輸出 schema `{"data": number[][], "shape": int[]}`，外層
信封 `{"result", "success", "errors", "messages"}` 照 REST 指南（別的模型的示範）；
見 `docs/runs/epic-2b/api-reference.md` 第二節。account id 與 token 也是測試自己編的假值。

- 文件送 `documents`、查詢送 `queries`（＋設定的 `instruction`），兩者不互換。
- 一次最多 32 段；每筆長度要等於登記的維度，不符整批丟錯。
- 信封防禦性解析（`success`、`errors`、`result.data`、`shape`）。
- 重試（429、5xx、連線錯誤、`Retry-After`），401／403／404 是設定錯，其他 4xx 是請求不合法。
- token 與 account id 不出現在 `repr`、錯誤訊息與日誌裡；網址只有在送出時才組。
"""

import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx2
import pytest
import stamina
from pydantic import SecretStr

from radar.adapters.workers_ai import WorkersAiEmbeddingProvider
from radar.domain.embeddings import (
    AttemptHooks,
    EmbeddingAttempt,
    EmbeddingConfigurationError,
    EmbeddingQuotaReachedError,
    EmbeddingRequestError,
    EmbeddingResponseError,
    EmbeddingTransientError,
    WorkersAiApi,
    current_attempt_hooks,
    report_attempts_to,
)
from radar.domain.errors import ErrorCode
from radar.domain.llm import NotCloudSafeError
from radar.domain.policy import CloudPolicy, CloudSafeText, ContentSource, clear_for_cloud
from radar.entrypoints.logging_config import configure_logging
from radar.ports import EmbeddingProvider

# 測試自己編的假值（不是真的帳號與 token）。
FAKE_ACCOUNT = "0123456789abcdef0123456789abcdef"
FAKE_TOKEN = "fake-token-for-tests"
MODEL = "@cf/qwen/qwen3-embedding-0.6b"
DIMENSIONS = 4
POLICY = CloudPolicy()
INSTRUCTION = "Given a research question, retrieve relevant paper abstracts"

type Reply = httpx2.Response | Exception


def _api(**retry: Any) -> WorkersAiApi:
    return WorkersAiApi.model_validate(
        {
            "base_url": "https://api.cloudflare.example/client/v4",
            "timeout_seconds": 30,
            "retry": {
                "attempts": 3,
                "wait_initial_seconds": 1,
                "wait_max_seconds": 30,
                "wait_jitter_seconds": 1,
                "retry_after_max_seconds": 120,
                **retry,
            },
        }
    )


def _safe(text: str) -> CloudSafeText:
    return clear_for_cloud(text, ContentSource.OPENALEX_ABSTRACT, None, POLICY)


def _vectors(count: int, dimensions: int = DIMENSIONS) -> list[list[float]]:
    return [[(row + 1) / 10 + column / 100 for column in range(dimensions)] for row in range(count)]


def _envelope(data: object, *, shape: object = "auto", **changes: Any) -> dict[str, Any]:
    """假回應（不是真的錄的，形狀照官方 schema）。"""
    result: dict[str, Any] = {"data": data}
    if shape == "auto" and isinstance(data, list):
        result["shape"] = [len(data), len(data[0]) if data and isinstance(data[0], list) else 0]
    elif shape != "auto":
        result["shape"] = shape
    envelope: dict[str, Any] = {"result": result, "success": True, "errors": [], "messages": []}
    envelope.update(changes)
    return envelope


def _ok(count: int, dimensions: int = DIMENSIONS) -> httpx2.Response:
    return httpx2.Response(200, json=_envelope(_vectors(count, dimensions)))


class Recorder:
    """依序回覆；記下每個請求。"""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def body(self, index: int = 0) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded


def _provider(
    recorder: Callable[[httpx2.Request], httpx2.Response],
    *,
    api: WorkersAiApi | None = None,
    instruction: str | None = None,
) -> WorkersAiEmbeddingProvider:
    return WorkersAiEmbeddingProvider(
        SecretStr(FAKE_ACCOUNT),
        SecretStr(FAKE_TOKEN),
        api or _api(),
        model_id=MODEL,
        dimensions=DIMENSIONS,
        query_instruction=instruction,
        transport=httpx2.MockTransport(recorder),
    )


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Iterator[None]:
    with stamina.set_testing(True, attempts=100, cap=True):
        yield


def _assert_clean(*texts: str) -> None:
    for text in texts:
        assert FAKE_TOKEN not in text
        assert FAKE_ACCOUNT not in text


# --- 請求 -------------------------------------------------------------------------------------


def test_is_an_embedding_provider() -> None:
    provider: EmbeddingProvider = _provider(Recorder(_ok(1)))
    assert provider.model_id == MODEL


def test_documents_request_url_headers_and_body() -> None:
    recorder = Recorder(_ok(2))
    with _provider(recorder, instruction=INSTRUCTION) as provider:
        vectors = provider.embed_documents([_safe("Title A\n\nAbstract A"), _safe("Title B")])

    assert vectors == _vectors(2)
    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == (
        f"https://api.cloudflare.example/client/v4/accounts/{FAKE_ACCOUNT}/ai/run/{MODEL}"
    )
    assert request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    # 文件只送 documents：不帶 queries，也不帶查詢用的指示文字。
    assert recorder.body() == {"documents": ["Title A\n\nAbstract A", "Title B"]}


def test_query_sends_queries_and_configured_instruction() -> None:
    recorder = Recorder(_ok(1))
    vector = _provider(recorder, instruction=INSTRUCTION).embed_query(_safe("how teams adopt IS"))

    assert vector == _vectors(1)[0]
    assert recorder.body() == {"queries": ["how teams adopt IS"], "instruction": INSTRUCTION}


def test_query_without_instruction_uses_model_default() -> None:
    recorder = Recorder(_ok(1))
    _provider(recorder).embed_query(_safe("how teams adopt IS"))

    assert recorder.body() == {"queries": ["how teams adopt IS"]}


def test_documents_and_queries_are_never_interchanged() -> None:
    recorder = Recorder(_ok(1))
    provider = _provider(recorder, instruction=INSTRUCTION)
    provider.embed_documents([_safe("same text")])
    provider.embed_query(_safe("same text"))

    assert set(recorder.body(0)) == {"documents"}
    assert set(recorder.body(1)) == {"queries", "instruction"}


def test_at_most_32_texts_per_request() -> None:
    recorder = Recorder(_ok(32))
    provider = _provider(recorder)
    assert len(provider.embed_documents([_safe(f"t{i}") for i in range(32)])) == 32

    with pytest.raises(ValueError, match="32"):
        provider.embed_documents([_safe(f"t{i}") for i in range(33)])
    assert len(recorder.requests) == 1


def test_empty_documents_send_nothing() -> None:
    recorder = Recorder(_ok(1))
    assert _provider(recorder).embed_documents([]) == []
    assert recorder.requests == []


@pytest.mark.parametrize("texts", ["plain string", ["plain string"]])
def test_not_cloud_safe_sends_nothing(texts: Any) -> None:
    recorder = Recorder(_ok(1))
    with pytest.raises(NotCloudSafeError):
        _provider(recorder).embed_documents(texts)
    with pytest.raises(NotCloudSafeError):
        _provider(recorder).embed_query("plain string")  # type: ignore[arg-type]
    assert recorder.requests == []


# --- 回應 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (_vectors(1, DIMENSIONS - 1), "維度"),
        (_vectors(1, DIMENSIONS + 1), "維度"),
        ([*_vectors(1), [0.1, 0.2]], "筆數"),
        ([], "筆數"),
    ],
)
def test_wrong_dimensions_or_count_reject_the_whole_batch(data: object, reason: str) -> None:
    rows = data if isinstance(data, list) else []
    count = 1
    recorder = Recorder(httpx2.Response(200, json=_envelope(data, shape=[len(rows), DIMENSIONS])))
    with pytest.raises(EmbeddingResponseError) as caught:
        _provider(recorder).embed_documents([_safe(f"t{i}") for i in range(count)])

    assert caught.value.code is ErrorCode.EMBED_RESPONSE_INVALID
    assert caught.value.http_status == 200
    assert reason in str(caught.value)


def test_second_vector_with_wrong_length_rejects_both() -> None:
    data = [[0.1] * DIMENSIONS, [0.1] * (DIMENSIONS - 1)]
    recorder = Recorder(httpx2.Response(200, json=_envelope(data, shape=None)))
    with pytest.raises(EmbeddingResponseError):
        _provider(recorder).embed_documents([_safe("a"), _safe("b")])


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        json.dumps({"result": {"data": _vectors(1)}, "errors": []}).encode(),  # 沒有 success
        json.dumps(_envelope(_vectors(1), success=False)).encode(),
        json.dumps(_envelope(_vectors(1), success="true")).encode(),
        json.dumps(_envelope(_vectors(1), errors=[{"code": 1, "message": "x"}])).encode(),
        json.dumps(_envelope(_vectors(1), errors="none")).encode(),
        json.dumps({"result": None, "success": True, "errors": []}).encode(),
        json.dumps({"result": [_vectors(1)], "success": True, "errors": []}).encode(),
        json.dumps(_envelope(None)).encode(),
        json.dumps(_envelope("1,2,3,4")).encode(),
        json.dumps(_envelope([[0.1, 0.2, "0.3", 0.4]])).encode(),
        json.dumps(_envelope([[0.1, 0.2, True, 0.4]])).encode(),
        json.dumps(_envelope([[0.1, 0.2, 1e10, 0.4]])).encode(),  # 超過 halfvec 範圍
        b'{"result": {"data": [[0.1, 0.2, NaN, 0.4]]}, "success": true, "errors": []}',
        json.dumps(_envelope(_vectors(1), shape=[1, DIMENSIONS + 1])).encode(),
        json.dumps(_envelope(_vectors(1), shape=[2, DIMENSIONS])).encode(),
    ],
)
def test_bad_envelopes_are_response_errors(body: bytes) -> None:
    recorder = Recorder(httpx2.Response(200, content=body))
    with pytest.raises(EmbeddingResponseError) as caught:
        _provider(recorder).embed_documents([_safe("a")])
    assert caught.value.http_status == 200
    assert len(recorder.requests) == 1  # 回應不對不重試


def test_envelope_without_shape_or_messages_is_accepted() -> None:
    body = {"result": {"data": _vectors(1)}, "success": True, "errors": []}
    recorder = Recorder(httpx2.Response(200, json=body))
    assert _provider(recorder).embed_documents([_safe("a")]) == _vectors(1)


# --- 錯誤與重試 -------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 404])
def test_configuration_errors_are_not_retried(status: int) -> None:
    recorder = Recorder(httpx2.Response(status, json={"success": False, "errors": []}))
    with pytest.raises(EmbeddingConfigurationError) as caught:
        _provider(recorder).embed_documents([_safe("a")])
    assert caught.value.http_status == status
    assert caught.value.code is ErrorCode.EMBED_CONFIGURATION
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("status", [400, 413, 422, 302])
def test_other_statuses_are_request_errors(status: int) -> None:
    recorder = Recorder(httpx2.Response(status))
    with pytest.raises(EmbeddingRequestError) as caught:
        _provider(recorder).embed_documents([_safe("a")])
    assert caught.value.http_status == status
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    "first",
    [
        httpx2.Response(429),
        httpx2.Response(500),
        httpx2.Response(503),
        httpx2.Response(408),
        httpx2.ConnectError("boom"),
        httpx2.ReadTimeout("slow"),
    ],
)
def test_transient_failures_are_retried(first: Reply) -> None:
    recorder = Recorder(first, _ok(1))
    assert _provider(recorder).embed_documents([_safe("a")]) == _vectors(1)
    assert len(recorder.requests) == 2


def test_retries_exhausted_raise_transient_error(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    recorder = Recorder(httpx2.Response(503))
    with pytest.raises(EmbeddingTransientError) as caught:
        _provider(recorder).embed_documents([_safe("a")])

    assert len(recorder.requests) == 3
    assert caught.value.http_status == 503
    assert caught.value.code is ErrorCode.EMBED_TRANSIENT
    assert caught.value.__cause__ is None
    out = capsys.readouterr()
    _assert_clean(str(caught.value), repr(caught.value), out.out, out.err)


def test_retry_after_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    # 關掉 stamina 的測試模式（它會把等待歸零），攔下 sleep、只記秒數，不真的等。
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    recorder = Recorder(httpx2.Response(429, headers={"Retry-After": "7"}), _ok(1))
    with stamina.set_testing(False):
        _provider(recorder).embed_documents([_safe("a")])
    assert waits == [7.0]
    assert len(recorder.requests) == 2


def test_retry_after_too_long_is_not_retried() -> None:
    recorder = Recorder(httpx2.Response(429, headers={"Retry-After": "600"}), _ok(1))
    with pytest.raises(EmbeddingTransientError) as caught:
        _provider(recorder).embed_documents([_safe("a")])
    assert len(recorder.requests) == 1
    assert caught.value.http_status == 429
    assert "600" in str(caught.value)


def test_decoding_error_after_response_is_response_error() -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Content-Encoding": "gzip"}, content=b"not gzip")

    with pytest.raises(EmbeddingResponseError):
        _provider(broken).embed_documents([_safe("a")])


# --- 秘密不外洩 -------------------------------------------------------------------------------


def test_repr_has_no_token_or_account() -> None:
    provider = _provider(Recorder(_ok(1)))
    _assert_clean(repr(provider), str(provider), repr(vars(provider)))
    assert MODEL in repr(provider)


@pytest.mark.parametrize(
    "reply",
    [
        httpx2.Response(401),
        httpx2.Response(400),
        httpx2.Response(200, content=b"oops"),
        httpx2.ConnectError(f"cannot reach /accounts/{FAKE_ACCOUNT}/ai/run Bearer {FAKE_TOKEN}"),
    ],
)
def test_errors_and_logs_never_carry_token_or_account(
    reply: Reply, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    recorder = Recorder(reply)
    with pytest.raises(
        (
            EmbeddingConfigurationError,
            EmbeddingRequestError,
            EmbeddingResponseError,
            EmbeddingTransientError,
        )
    ) as caught:
        _provider(recorder).embed_documents([_safe("a")])

    error = caught.value
    out = capsys.readouterr()
    _assert_clean(str(error), repr(error), out.out, out.err)
    assert error.__cause__ is None
    assert error.__suppress_context__ or error.__context__ is None


@pytest.mark.parametrize(
    ("account", "token"),
    [("", FAKE_TOKEN), (FAKE_ACCOUNT, "")],
)
def test_empty_secrets_are_rejected(account: str, token: str) -> None:
    with pytest.raises(ValueError, match="SecretStr") as caught:
        WorkersAiEmbeddingProvider(
            SecretStr(account), SecretStr(token), _api(), model_id=MODEL, dimensions=DIMENSIONS
        )
    _assert_clean(str(caught.value))


def test_plain_strings_are_rejected_as_secrets() -> None:
    with pytest.raises(ValueError, match="SecretStr"):
        WorkersAiEmbeddingProvider(
            FAKE_ACCOUNT,  # type: ignore[arg-type]
            SecretStr(FAKE_TOKEN),
            _api(),
            model_id=MODEL,
            dimensions=DIMENSIONS,
        )


# --- 審查後的修正：每次嘗試回報、總時限（Codex 高 1、中 3） ---------------------------------


class _Hooks:
    """記下 adapter 每次嘗試前後的回報；`deny_from` 起（從 1 起算）嘗試前丟額度不夠。"""

    def __init__(self, *, deny_from: int | None = None) -> None:
        self.deny_from = deny_from
        self.before_calls = 0
        self.attempts: list[EmbeddingAttempt] = []

    def before(self) -> None:
        self.before_calls += 1
        if self.deny_from is not None and self.before_calls >= self.deny_from:
            raise EmbeddingQuotaReachedError()

    def after(self, attempt: EmbeddingAttempt) -> None:
        self.attempts.append(attempt)

    def hooks(self) -> AttemptHooks:
        return AttemptHooks(before=self.before, after=self.after)


class _FakeClock:
    """假的單調時鐘（秒）；transport 裡往前撥，模擬每次請求花掉的時間。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _timed_provider(
    handler: Callable[[httpx2.Request], httpx2.Response],
    clock: _FakeClock,
    **api: Any,
) -> WorkersAiEmbeddingProvider:
    values: dict[str, Any] = {
        "base_url": "https://api.cloudflare.example/client/v4",
        "timeout_seconds": 30,
        "total_timeout_seconds": 100,
        "retry": {
            "attempts": 5,
            "wait_initial_seconds": 1,
            "wait_max_seconds": 30,
            "wait_jitter_seconds": 1,
            "retry_after_max_seconds": 120,
        },
    }
    values.update(api)
    return WorkersAiEmbeddingProvider(
        SecretStr(FAKE_ACCOUNT),
        SecretStr(FAKE_TOKEN),
        WorkersAiApi.model_validate(values),
        model_id=MODEL,
        dimensions=DIMENSIONS,
        transport=httpx2.MockTransport(handler),
        monotonic=clock,
    )


def test_every_attempt_is_reported_including_internal_retries() -> None:
    # 第一次讀取逾時（對方可能已經算了 neurons）、第二次成功：兩次都回報、都可能計費。
    recorder = Recorder(httpx2.ReadTimeout("slow"), _ok(1))
    hooks = _Hooks()

    with report_attempts_to(hooks.hooks()):
        assert _provider(recorder).embed_documents([_safe("a")]) == _vectors(1)

    assert hooks.before_calls == 2
    assert hooks.attempts == [
        EmbeddingAttempt(sent=True, http_status=None, billable=True),
        EmbeddingAttempt(sent=True, http_status=200, billable=True),
    ]


def test_configuration_error_attempt_is_not_billable() -> None:
    hooks = _Hooks()
    with report_attempts_to(hooks.hooks()), pytest.raises(EmbeddingConfigurationError):
        _provider(Recorder(httpx2.Response(401))).embed_documents([_safe("a")])
    assert hooks.attempts == [EmbeddingAttempt(sent=True, http_status=401, billable=False)]


def test_retry_server_error_attempts_are_reported_with_their_status() -> None:
    hooks = _Hooks()
    recorder = Recorder(httpx2.Response(503), httpx2.Response(429), _ok(1))
    with report_attempts_to(hooks.hooks()):
        _provider(recorder).embed_documents([_safe("a")])
    assert [attempt.http_status for attempt in hooks.attempts] == [503, 429, 200]
    assert all(attempt.billable and attempt.sent for attempt in hooks.attempts)


def test_quota_refused_before_a_retry_sends_nothing_more() -> None:
    # 第二次嘗試前額度放不下：原樣丟出，不送、不再重試；第一次嘗試照樣回報。
    recorder = Recorder(httpx2.Response(503), _ok(1))
    hooks = _Hooks(deny_from=2)

    with report_attempts_to(hooks.hooks()), pytest.raises(EmbeddingQuotaReachedError):
        _provider(recorder).embed_documents([_safe("a")])

    assert len(recorder.requests) == 1
    assert hooks.attempts == [EmbeddingAttempt(sent=True, http_status=503, billable=True)]


def test_without_hooks_the_adapter_still_works() -> None:
    assert current_attempt_hooks() is None
    assert _provider(Recorder(_ok(1))).embed_documents([_safe("a")]) == _vectors(1)


def test_total_timeout_stops_retrying_before_the_next_wait_would_pass_it() -> None:
    # 總時限 100 秒：每次請求花 45 秒、都回 503。第 2 次結束在 90 秒，再等（≥ 1 秒）就到 91 秒以上；
    # 第 3 次只剩不到 10 秒——還是會試，但各階段逾時縮成剩下的秒數；第 3 次結束時已過總時限，不再等。
    clock = _FakeClock()
    timeouts: list[float] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        clock.now += 45
        return httpx2.Response(503)

    with pytest.raises(EmbeddingTransientError) as caught:
        _timed_provider(handler, clock).embed_documents([_safe("a")])

    assert timeouts == [30, 30, 10]
    assert clock.now == 135
    assert "總時限 100 秒" in str(caught.value)
    assert caught.value.http_status == 503


def test_retry_after_beyond_the_total_timeout_is_not_waited_for() -> None:
    clock = _FakeClock()
    recorder = Recorder(httpx2.Response(429, headers={"Retry-After": "110"}), _ok(1))

    with pytest.raises(EmbeddingTransientError) as caught:
        _timed_provider(recorder, clock).embed_documents([_safe("a")])

    assert len(recorder.requests) == 1
    assert "總時限 100 秒" in str(caught.value)


def test_slow_trickling_response_is_cut_at_the_total_timeout() -> None:
    # 對方一直慢慢送少量資料（每一塊都在單次讀取逾時內到）：收到每一塊後看總時限，過了就中斷。
    clock = _FakeClock()
    body = json.dumps(_envelope(_vectors(1))).encode()

    def trickle() -> Iterator[bytes]:
        for start in range(0, len(body), 8):
            clock.now += 20
            yield body[start : start + 8]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=trickle())

    hooks = _Hooks()
    with report_attempts_to(hooks.hooks()), pytest.raises(EmbeddingTransientError) as caught:
        _timed_provider(handler, clock).embed_documents([_safe("a")])

    assert "總時限" in str(caught.value)
    assert clock.now == 100  # 第 5 塊（100 秒）到時就停，不再讀下去
    assert hooks.attempts == [EmbeddingAttempt(sent=True, http_status=200, billable=True)]


def test_no_attempt_starts_after_the_total_timeout() -> None:
    # before() 等了很久（例如等額度鎖）：回來時總時限已過，不送，回報「沒送出」。
    clock = _FakeClock()
    recorder = Recorder(_ok(1))

    def slow_before() -> None:
        clock.now += 200

    attempts: list[EmbeddingAttempt] = []
    hooks = AttemptHooks(before=slow_before, after=attempts.append)
    with report_attempts_to(hooks), pytest.raises(EmbeddingTransientError):
        _timed_provider(recorder, clock).embed_documents([_safe("a")])

    assert recorder.requests == []
    assert attempts == [EmbeddingAttempt(sent=False, http_status=None, billable=False)]


def test_request_limit_bounds_a_full_run_of_the_slowest_attempts() -> None:
    # 每次請求都花滿逾時（30 秒）並失敗：結束時間不超過 request_limit_seconds（100＋2 × 30）。
    clock = _FakeClock()

    def handler(request: httpx2.Request) -> httpx2.Response:
        clock.now += request.extensions["timeout"]["read"]
        raise httpx2.ReadTimeout("slow")

    provider = _timed_provider(handler, clock)
    with pytest.raises(EmbeddingTransientError):
        provider.embed_documents([_safe("a")])
    assert clock.now <= provider._api.request_limit_seconds
