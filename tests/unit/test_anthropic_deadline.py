"""Anthropic adapter 的總時限（`deadline`，AD-8 的硬性時間上限）：交給 stamina 的 `timeout`。

bootstrap 由 `RADAR_ANTHROPIC_DEADLINE` 傳進來；沒給時維持 2.1 的行為（不限）。
"""

from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
import stamina

from radar.adapters.anthropic import AnthropicLlmProvider
from radar.config import load_settings
from radar.domain.llm import LlmRequest
from radar.domain.policy import CloudPolicy, ContentSource, clear_for_cloud
from radar.entrypoints.bootstrap import build_llm_provider
from tests.anthropic_cassette import cassette_response
from tests.prompt_support import template_text

FAKE_KEY = "sk-ant-fake-deadline-test"
POLICY = CloudPolicy()


def _request() -> LlmRequest:
    return LlmRequest(
        model="claude-haiku-5-5",
        system=template_text("（假）任務"),
        content=(clear_for_cloud("（假）標題", ContentSource.TITLE, None, POLICY),),
        max_tokens=1024,
    )


@pytest.fixture
def retry_kwargs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """包住 stamina.retry_context，記下每次的參數（照常重試）。"""
    seen: list[dict[str, Any]] = []
    original = stamina.retry_context

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(stamina, "retry_context", spy)
    with stamina.set_testing(True, attempts=100, cap=True):
        yield seen


def _provider(**changes: Any) -> AnthropicLlmProvider:
    values: dict[str, Any] = {
        "timeout": 30,
        "attempts": 3,
        "retry_wait_initial": 1.0,
        "retry_wait_max": 10.0,
        "retry_wait_jitter": 1.0,
        "retry_after_max": 60,
        "transport": httpx2.MockTransport(lambda _request: cassette_response("ok_text")),
    }
    values.update(changes)
    return AnthropicLlmProvider(FAKE_KEY, **values)


def test_deadline_is_passed_to_stamina_timeout(retry_kwargs: list[dict[str, Any]]) -> None:
    with _provider(deadline=45.0) as provider:
        provider.complete(_request())

    assert [kwargs["timeout"] for kwargs in retry_kwargs] == [45.0]


def test_default_deadline_is_unlimited(retry_kwargs: list[dict[str, Any]]) -> None:
    with _provider() as provider:
        provider.complete(_request())

    assert [kwargs["timeout"] for kwargs in retry_kwargs] == [None]


@pytest.mark.parametrize("deadline", [0, -1.0])
def test_deadline_must_be_positive(deadline: float) -> None:
    with pytest.raises(ValueError, match="deadline"):
        _provider(deadline=deadline)


def test_bootstrap_passes_the_setting(retry_kwargs: list[dict[str, Any]]) -> None:
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql://radar_app@db:5432/radar",
            "RADAR_ANTHROPIC_API_KEY": FAKE_KEY,
            "RADAR_ANTHROPIC_DEADLINE": "75",
        }
    )

    with build_llm_provider(settings) as provider:
        assert provider._deadline == 75
