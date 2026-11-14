"""放行登記處（Codex 審查 PR #54 第 1 點）的反向測試：放行後用 `object.__setattr__` 硬改、
用 `object.__new__` 做空殼、物件回收後沿用號碼，Anthropic adapter 與兩個替身都一個請求都不送、
直接丟 `CloudSafeTextIntegrityError`；錯誤訊息、args、repr 不帶文字；複製與序列化照樣擋。

這裡刻意碰 `CloudSafeText` 的私有欄位，模擬有人存心繞過；`radar/` 裡不能這樣寫（架構測試守著）。
文字全是自己編的假內容。
"""

import copy
import gc
import pickle
from typing import Any

import httpx2
import pytest

from radar.adapters.anthropic import AnthropicLlmProvider
from radar.adapters.fake import FakeEmbeddingProvider, FakeLlmProvider, fake_completion
from radar.domain.errors import ErrorCode
from radar.domain.llm import (
    CloudSafeTextIntegrityError,
    LlmRequest,
    NotCloudSafeError,
    check_cloud_safe_text,
    check_cloud_safe_texts,
    check_llm_request,
    system_prompt,
)
from radar.domain.policy import (
    CloudPolicy,
    CloudSafeText,
    ContentSource,
    clear_for_cloud,
    cloud_text,
)

POLICY = CloudPolicy()
CLEARED = "（假摘要）已經放行的標題 CLEARED-MARKER-4M"
PAYWALLED = "（假）付費全文不該送出 PAYWALLED-MARKER-8Z"
UNKNOWN_TOKEN = 10**12
TOKEN = "_token"


def _safe(text: str = CLEARED, source: ContentSource = ContentSource.TITLE) -> CloudSafeText:
    return clear_for_cloud(text, source, None, POLICY)


def _request(**changes: Any) -> LlmRequest:
    values: dict[str, Any] = {
        "model": "claude-haiku-5-5",
        "system": _safe("請整理。", ContentSource.PROMPT_TEMPLATE),
        "content": (_safe(),),
        "max_tokens": 1024,
    }
    values.update(changes)
    return LlmRequest(**values)


def _tamper(safe: CloudSafeText, token: object = UNKNOWN_TOKEN) -> None:
    object.__setattr__(safe, TOKEN, token)


def _shell() -> CloudSafeText:
    # 經由變數呼叫：架構測試規定只有 policy.py 能寫 `__new__(CloudSafeText)`。
    cls: Any = CloudSafeText
    shell: CloudSafeText = object.__new__(cls)
    return shell


def _dead_token() -> int:
    """建立後回收一個 CloudSafeText，回它用過的號碼。"""
    gone = _safe("（假）用完就丟")
    token: int = getattr(gone, TOKEN)
    del gone
    gc.collect()
    return token


def _assert_clean(error: BaseException) -> None:
    shown = f"{error}{error!r}{error.args!r}"
    for secret in (CLEARED, "CLEARED-MARKER-4M", PAYWALLED, "PAYWALLED-MARKER-8Z"):
        assert secret not in shown


class Recorder:
    """MockTransport 的處理函式：只記請求、不回任何內容（測試裡不該有請求進來）。"""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(500)


def _anthropic(recorder: Recorder) -> AnthropicLlmProvider:
    return AnthropicLlmProvider(
        "sk-ant-FAKE-KEY-for-tests-only",
        timeout=30,
        attempts=1,
        retry_wait_initial=1.0,
        retry_wait_max=10.0,
        retry_wait_jitter=1.0,
        retry_after_max=60,
        transport=httpx2.MockTransport(recorder),
    )


# --- CloudSafeText 本身 ----------------------------------------------------------------------


def test_text_cannot_be_swapped_with_object_setattr() -> None:
    safe = _safe()

    # 物件上沒有存文字的欄位，Codex 舉的寫法直接失敗。
    with pytest.raises(AttributeError):
        object.__setattr__(safe, "_text", PAYWALLED)
    with pytest.raises(AttributeError):
        object.__setattr__(safe, "_source", ContentSource.UPLOADED_PDF)

    assert safe.text == CLEARED
    assert cloud_text(safe) == CLEARED


@pytest.mark.parametrize(
    "token",
    [UNKNOWN_TOKEN, -1, 0, "1", 1.0, True, None],
    ids=["沒發過的號碼", "負數", "零", "字串", "浮點數", "bool", "None"],
)
def test_swapped_token_is_rejected_without_leaking_text(token: object) -> None:
    safe = _safe()
    _tamper(safe, token)

    with pytest.raises(CloudSafeTextIntegrityError) as info:
        cloud_text(safe)
    with pytest.raises(CloudSafeTextIntegrityError):
        _ = safe.text
    with pytest.raises(CloudSafeTextIntegrityError):
        _ = safe.source

    error = info.value
    assert isinstance(error, NotCloudSafeError)
    assert isinstance(error, TypeError)
    assert error.reason is ErrorCode.CLOUD_TEXT_NOT_REGISTERED
    assert ErrorCode.CLOUD_TEXT_NOT_REGISTERED.value in str(error)
    _assert_clean(error)
    assert repr(safe) == "CloudSafeText(不在放行登記裡)"


def test_token_of_a_collected_object_is_never_valid_again() -> None:
    dead = _dead_token()
    fresh = _safe()

    assert getattr(fresh, TOKEN) != dead
    _tamper(fresh, dead)
    with pytest.raises(CloudSafeTextIntegrityError):
        cloud_text(fresh)


def test_deleted_token_is_rejected() -> None:
    safe = _safe()
    object.__delattr__(safe, TOKEN)

    with pytest.raises(CloudSafeTextIntegrityError):
        cloud_text(safe)


def test_empty_shell_from_object_new_is_rejected() -> None:
    shell = _shell()

    with pytest.raises(CloudSafeTextIntegrityError):
        cloud_text(shell)
    with pytest.raises(CloudSafeTextIntegrityError):
        _request(content=(shell,))
    assert repr(shell) == "CloudSafeText(不在放行登記裡)"


def test_cloud_text_rejects_anything_but_cloud_safe_text() -> None:
    for wrong in (PAYWALLED, PAYWALLED.encode(), None, [_safe()]):
        with pytest.raises(NotCloudSafeError) as info:
            cloud_text(wrong)
        assert not isinstance(info.value, CloudSafeTextIntegrityError)
        _assert_clean(info.value)


def test_registry_keeps_each_object_independent() -> None:
    first = _safe("（假）第一段")
    second = _safe("（假）第二段")
    del first
    gc.collect()

    assert cloud_text(second) == "（假）第二段"


@pytest.mark.parametrize("tampered", [False, True], ids=["正常", "號碼被改過"])
def test_copy_replace_and_pickle_are_still_blocked(tampered: bool) -> None:
    safe = _safe()
    if tampered:
        _tamper(safe)
    target: Any = safe  # copy.replace 的型別本來就不收它；這裡要證明執行期也擋

    with pytest.raises(TypeError):
        copy.copy(safe)
    with pytest.raises(TypeError):
        copy.deepcopy(safe)
    with pytest.raises(TypeError):
        copy.replace(target)
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        with pytest.raises(TypeError):
            pickle.dumps(safe, protocol=protocol)


# --- domain/llm.py 的入口檢查 ----------------------------------------------------------------


def test_request_cannot_be_built_from_tampered_text() -> None:
    content = _safe()
    system = _safe("請整理。", ContentSource.PROMPT_TEMPLATE)
    _tamper(content)
    _tamper(system)

    with pytest.raises(CloudSafeTextIntegrityError):
        _request(content=(content,))
    with pytest.raises(CloudSafeTextIntegrityError):
        _request(system=system)


def test_check_functions_catch_text_tampered_after_the_request_was_built() -> None:
    request = _request()
    _tamper(request.content[0])

    with pytest.raises(CloudSafeTextIntegrityError):
        check_llm_request(request)

    other = _request()
    _tamper(other.system)
    with pytest.raises(CloudSafeTextIntegrityError):
        check_llm_request(other)
    with pytest.raises(CloudSafeTextIntegrityError):
        system_prompt(other)

    tampered = _safe()
    _tamper(tampered)
    with pytest.raises(CloudSafeTextIntegrityError):
        check_cloud_safe_text(tampered)
    with pytest.raises(CloudSafeTextIntegrityError):
        check_cloud_safe_texts([_safe(), tampered])


# --- Anthropic adapter：一個請求都不送 ------------------------------------------------------


def test_anthropic_sends_nothing_when_content_is_tampered() -> None:
    recorder = Recorder()
    request = _request()
    _tamper(request.content[0])

    with pytest.raises(CloudSafeTextIntegrityError) as info:
        _anthropic(recorder).complete(request)

    assert recorder.requests == []
    _assert_clean(info.value)


def test_anthropic_sends_nothing_when_system_is_tampered() -> None:
    recorder = Recorder()
    request = _request()
    _tamper(request.system)

    with pytest.raises(CloudSafeTextIntegrityError):
        _anthropic(recorder).complete(request)

    assert recorder.requests == []


def test_anthropic_sends_nothing_when_content_is_replaced_by_a_shell() -> None:
    recorder = Recorder()
    request = _request()
    object.__setattr__(request, "content", (_shell(),))

    with pytest.raises(CloudSafeTextIntegrityError):
        _anthropic(recorder).complete(request)

    assert recorder.requests == []


def test_anthropic_sends_nothing_with_a_dead_token() -> None:
    recorder = Recorder()
    request = _request()
    _tamper(request.content[0], _dead_token())

    with pytest.raises(CloudSafeTextIntegrityError):
        _anthropic(recorder).complete(request)

    assert recorder.requests == []


# --- 替身：同樣擋下、不記錄、不消耗回覆 -------------------------------------------------------


def test_fake_llm_rejects_tampered_request_before_recording_or_consuming() -> None:
    fake = FakeLlmProvider([fake_completion("x")])
    request = _request()
    _tamper(request.content[0])

    with pytest.raises(CloudSafeTextIntegrityError):
        fake.complete(request)

    assert fake.requests == []
    assert fake.remaining == 1


def test_fake_embedding_rejects_tampered_texts_before_recording() -> None:
    fake = FakeEmbeddingProvider()
    tampered = _safe()
    _tamper(tampered)

    with pytest.raises(CloudSafeTextIntegrityError):
        fake.embed_documents([_safe(), tampered])
    with pytest.raises(CloudSafeTextIntegrityError):
        fake.embed_query(tampered)
    with pytest.raises(CloudSafeTextIntegrityError):
        fake.embed_query(_shell())

    assert fake.document_calls == []
    assert fake.query_calls == []
