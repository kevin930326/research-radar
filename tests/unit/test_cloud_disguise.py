"""偽裝成 `CloudSafeText` 的物件（Codex 複查 PR #54）：一般物件讓 `__class__` 回 `CloudSafeText`
就能騙過 `isinstance`，再用自己的 `.text` 回付費全文。核對點改看真實型別、直接查放行登記處之後，
這種物件、子類別、帶 `_token` 屬性的冒牌物件，進 `cloud_text`、`LlmRequest`、Anthropic adapter、
兩個替身都會被擋下，一個請求都不送，錯誤訊息不帶文字。

文字全是自己編的假內容。
"""

from typing import Any

import httpx2
import pytest

from radar.adapters.anthropic import AnthropicLlmProvider
from radar.adapters.fake import FakeEmbeddingProvider, FakeLlmProvider, fake_completion
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
    cloud_source,
    cloud_text,
    is_cloud_safe_text,
)
from tests.prompt_support import template_text

POLICY = CloudPolicy()
CLEARED = "（假摘要）已經放行的標題 CLEARED-MARKER-6P"
PAYWALLED = "（假）付費全文不該送出 PAYWALLED-MARKER-2W"
TOKEN = "_token"
UNKNOWN_TOKEN = 10**12


def _safe(text: str = CLEARED, source: ContentSource = ContentSource.TITLE) -> CloudSafeText:
    return clear_for_cloud(text, source, None, POLICY)


def _request(**changes: Any) -> LlmRequest:
    values: dict[str, Any] = {
        "model": "claude-haiku-5-5",
        "system": template_text("請整理。"),
        "content": (_safe(),),
        "max_tokens": 1024,
    }
    values.update(changes)
    return LlmRequest(**values)


class Disguised:
    """讓 `__class__` 回 `CloudSafeText`，騙過 `isinstance`；`.text` 回付費全文。"""

    def __init__(self, token: int) -> None:
        self._token = token

    @property  # type: ignore[misc]
    def __class__(self) -> type:
        return CloudSafeText

    @property
    def text(self) -> str:
        return PAYWALLED

    @property
    def source(self) -> ContentSource:
        return ContentSource.PROMPT_TEMPLATE

    def __repr__(self) -> str:
        return "Disguised()"


class HasToken:
    """不是 `CloudSafeText`，但帶著一個真的、還在登記裡的號碼。"""

    def __init__(self, token: int) -> None:
        self._token = token
        self.text = PAYWALLED
        self.source = ContentSource.PROMPT_TEMPLATE


class Twin:
    """和 `CloudSafeText` 一樣的 slots，可以把 `__class__` 改成 `CloudSafeText`。"""

    __slots__ = ("__weakref__", "_token")


def _live_token(safe: CloudSafeText) -> int:
    token: int = getattr(safe, TOKEN)
    return token


def _impostors(anchor: CloudSafeText) -> list[object]:
    """各種冒牌物件；`anchor` 要在呼叫端保持存活，號碼才是「還在登記裡」的。"""
    token = _live_token(anchor)
    return [Disguised(token), Disguised(UNKNOWN_TOKEN), HasToken(token)]


IMPOSTOR_IDS = ["偽裝 __class__（真號碼）", "偽裝 __class__（假號碼）", "帶 _token 的物件"]


def _assert_clean(error: BaseException) -> None:
    shown = f"{error}{error!r}{error.args!r}"
    for secret in (CLEARED, "CLEARED-MARKER-6P", PAYWALLED, "PAYWALLED-MARKER-2W"):
        assert secret not in shown


class Recorder:
    """MockTransport 的處理函式：只記請求（測試裡不該有請求進來）。"""

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


# --- 攻擊前提與 policy 的核對 ----------------------------------------------------------------


def test_disguised_object_fools_isinstance_but_not_is_cloud_safe_text() -> None:
    anchor = _safe()
    disguised = Disguised(_live_token(anchor))

    # 這就是 Codex 指出的前提：isinstance 被騙過。
    assert isinstance(disguised, CloudSafeText)
    assert not is_cloud_safe_text(disguised)
    assert is_cloud_safe_text(anchor)


@pytest.mark.parametrize("index", range(3), ids=IMPOSTOR_IDS)
def test_cloud_text_and_source_reject_impostors(index: int) -> None:
    anchor = _safe()
    impostor = _impostors(anchor)[index]

    with pytest.raises(NotCloudSafeError) as info:
        cloud_text(impostor)
    with pytest.raises(NotCloudSafeError):
        cloud_source(impostor)

    assert not isinstance(info.value, CloudSafeTextIntegrityError)
    _assert_clean(info.value)


def test_subclass_cannot_be_created() -> None:
    base: Any = CloudSafeText

    with pytest.raises(TypeError, match="繼承"):
        type("Sub", (base,), {"text": PAYWALLED})


def test_twin_with_class_reassigned_only_reaches_cleared_text() -> None:
    anchor = _safe()
    unknown = Twin()
    object.__setattr__(unknown, TOKEN, UNKNOWN_TOKEN)
    object.__setattr__(unknown, "__class__", CloudSafeText)
    borrowed = Twin()
    object.__setattr__(borrowed, TOKEN, _live_token(anchor))
    object.__setattr__(borrowed, "__class__", CloudSafeText)

    # 號碼不在登記裡：擋下。
    with pytest.raises(CloudSafeTextIntegrityError):
        cloud_text(unknown)
    # 借用還活著的號碼：只拿得到那筆已經放行的內容（ADR-0029 取捨），拿不到沒放行的文字。
    assert cloud_text(borrowed) == CLEARED


# --- domain/llm.py 的入口檢查 ----------------------------------------------------------------


@pytest.mark.parametrize("index", range(3), ids=IMPOSTOR_IDS)
def test_request_cannot_be_built_from_impostors(index: int) -> None:
    anchor = _safe()
    impostor = _impostors(anchor)[index]

    with pytest.raises(NotCloudSafeError) as content_info:
        _request(content=(impostor,))
    with pytest.raises(NotCloudSafeError) as system_info:
        _request(system=impostor)

    _assert_clean(content_info.value)
    _assert_clean(system_info.value)


@pytest.mark.parametrize("index", range(3), ids=IMPOSTOR_IDS)
def test_check_functions_reject_impostors(index: int) -> None:
    anchor = _safe()
    impostor = _impostors(anchor)[index]

    content_swapped = _request()
    object.__setattr__(content_swapped, "content", (impostor,))
    system_swapped = _request()
    object.__setattr__(system_swapped, "system", impostor)

    with pytest.raises(NotCloudSafeError):
        check_llm_request(content_swapped)
    with pytest.raises(NotCloudSafeError):
        check_llm_request(system_swapped)
    with pytest.raises(NotCloudSafeError):
        system_prompt(system_swapped)
    with pytest.raises(NotCloudSafeError):
        check_cloud_safe_text(impostor)
    with pytest.raises(NotCloudSafeError):
        check_cloud_safe_texts([_safe(), impostor])


# --- Anthropic adapter：一個請求都不送 ------------------------------------------------------


@pytest.mark.parametrize("field", ["content", "system"])
@pytest.mark.parametrize("index", range(3), ids=IMPOSTOR_IDS)
def test_anthropic_sends_nothing_for_impostors(index: int, field: str) -> None:
    anchor = _safe()
    impostor = _impostors(anchor)[index]
    recorder = Recorder()
    request = _request()
    object.__setattr__(request, field, (impostor,) if field == "content" else impostor)

    with pytest.raises(NotCloudSafeError) as info:
        _anthropic(recorder).complete(request)

    assert recorder.requests == []
    _assert_clean(info.value)


# --- 替身：同樣擋下、不記錄、不消耗回覆 -------------------------------------------------------


@pytest.mark.parametrize("index", range(3), ids=IMPOSTOR_IDS)
def test_fake_llm_rejects_impostors_before_recording_or_consuming(index: int) -> None:
    anchor = _safe()
    impostor = _impostors(anchor)[index]
    fake = FakeLlmProvider([fake_completion("x")])
    request = _request()
    object.__setattr__(request, "content", (impostor,))

    with pytest.raises(NotCloudSafeError):
        fake.complete(request)

    assert fake.requests == []
    assert fake.remaining == 1


@pytest.mark.parametrize("index", range(3), ids=IMPOSTOR_IDS)
def test_fake_embedding_rejects_impostors_before_recording(index: int) -> None:
    anchor = _safe()
    impostor: Any = _impostors(anchor)[index]
    fake = FakeEmbeddingProvider()

    with pytest.raises(NotCloudSafeError) as info:
        fake.embed_documents([_safe(), impostor])
    with pytest.raises(NotCloudSafeError):
        fake.embed_query(impostor)

    assert fake.document_calls == []
    assert fake.query_calls == []
    _assert_clean(info.value)
