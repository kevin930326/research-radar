"""組裝 Anthropic 的 `LlmProvider`：缺 key 丟 `ConfigError`（只列環境變數名稱）；有 key 時建得出來、
設定值一路傳下去、`repr` 不帶 key。都不連線。
"""

import subprocess
import sys
from pathlib import Path

import pytest

from radar.adapters.anthropic import AnthropicLlmProvider
from radar.config import ConfigError, load_settings
from radar.entrypoints.bootstrap import build_llm_provider

FAKE_KEY = "sk-ant-FAKE-bootstrap-key"
BASE_ENV = {"DATABASE_URL": "postgresql://radar@db:5432/radar"}
ROOT = Path(__file__).resolve().parents[2]


def test_missing_key_raises_config_error_with_only_the_env_name() -> None:
    settings = load_settings(BASE_ENV)

    with pytest.raises(ConfigError) as info:
        build_llm_provider(settings)

    assert info.value.problems == ["缺少環境變數 RADAR_ANTHROPIC_API_KEY"]


def test_builds_with_key_and_settings_without_showing_the_key() -> None:
    settings = load_settings(
        {
            **BASE_ENV,
            "RADAR_ANTHROPIC_API_KEY": FAKE_KEY,
            "RADAR_ANTHROPIC_TIMEOUT": "42",
            "RADAR_ANTHROPIC_ATTEMPTS": "5",
            "RADAR_ANTHROPIC_RETRY_AFTER_MAX": "90",
        }
    )

    with build_llm_provider(settings) as provider:
        assert isinstance(provider, AnthropicLlmProvider)
        assert FAKE_KEY not in repr(provider)
        assert FAKE_KEY not in str(provider)
        assert provider._attempts == 5
        assert provider._retry_after_max == 90
        assert provider._client.timeout == 42
        assert provider._client.max_retries == 0
        assert str(provider._client.base_url).rstrip("/") == "https://api.anthropic.com"


def test_settings_defaults() -> None:
    settings = load_settings(BASE_ENV)

    assert settings.anthropic_api_key is None
    assert settings.anthropic_timeout == 60
    assert settings.anthropic_attempts == 3
    assert settings.anthropic_retry_wait_initial == 1.0
    assert settings.anthropic_retry_wait_max == 10.0
    assert settings.anthropic_retry_wait_jitter == 1.0
    assert settings.anthropic_retry_after_max == 60
    assert settings.paywalled_fulltext_llm is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RADAR_ANTHROPIC_TIMEOUT", "0"),
        ("RADAR_ANTHROPIC_TIMEOUT", "601"),
        ("RADAR_ANTHROPIC_ATTEMPTS", "0"),
        ("RADAR_ANTHROPIC_ATTEMPTS", "11"),
        ("RADAR_ANTHROPIC_RETRY_AFTER_MAX", "601"),
    ],
)
def test_out_of_range_settings_are_rejected(name: str, value: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_settings({**BASE_ENV, name: value})

    assert name in str(info.value)


def test_settings_repr_hides_the_key() -> None:
    settings = load_settings({**BASE_ENV, "RADAR_ANTHROPIC_API_KEY": FAKE_KEY})

    assert FAKE_KEY not in repr(settings)


def test_retry_waits_follow_settings() -> None:
    # 退避起點、上限、抖動由 bootstrap 從設定傳進 adapter（都不是預設值）。
    settings = load_settings(
        {
            **BASE_ENV,
            "RADAR_ANTHROPIC_API_KEY": FAKE_KEY,
            "RADAR_ANTHROPIC_RETRY_WAIT_INITIAL": "2.5",
            "RADAR_ANTHROPIC_RETRY_WAIT_MAX": "40",
            "RADAR_ANTHROPIC_RETRY_WAIT_JITTER": "0",
        }
    )

    with build_llm_provider(settings) as provider:
        assert (
            provider._retry_wait_initial,
            provider._retry_wait_max,
            provider._retry_wait_jitter,
        ) == (2.5, 40.0, 0.0)


@pytest.mark.parametrize(
    "key", [f"{FAKE_KEY}\n", f" {FAKE_KEY}", f"{FAKE_KEY} ", "sk-ant-FAKE key", "sk-ant\tFAKE"]
)
def test_key_with_whitespace_is_rejected(key: str) -> None:
    # 帶空白或換行的 key 會變成不合法的 HTTP 標頭；讀設定時就擋下，訊息不帶值。
    with pytest.raises(ConfigError) as info:
        load_settings({**BASE_ENV, "RADAR_ANTHROPIC_API_KEY": key})

    assert "RADAR_ANTHROPIC_API_KEY" in str(info.value)
    assert "FAKE" not in str(info.value)


def test_api_process_does_not_load_the_anthropic_sdk() -> None:
    # api 為了 build_readiness_probes import bootstrap；Anthropic 的 adapter 在 build_llm_provider
    # 裡才 import，api 行程不會載入 SDK（約 48 MB，mem_limit 很緊）。
    code = (
        "import sys, radar.entrypoints.api.main\n"
        "print('anthropic' in sys.modules, 'radar.adapters.anthropic' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )

    assert result.stdout.split() == ["False", "False"]
