"""組裝 Workers AI 的 `EmbeddingProvider`（Story 2.8）：缺 account id 或 token 丟 `ConfigError`
（只列環境變數名稱）；都有時建得出來、設定一路傳下去、`repr` 不帶秘密。都不連線。

account id 與 token 是測試自己編的假值。
"""

import subprocess
import sys
from pathlib import Path

import pytest

from radar.adapters.workers_ai import WorkersAiEmbeddingProvider
from radar.config import ConfigError, load_embedding_config, load_settings
from radar.entrypoints.bootstrap import build_embedding_provider

ROOT = Path(__file__).resolve().parents[2]
BASE_ENV = {"DATABASE_URL": "postgresql://radar@db:5432/radar"}
FAKE_ACCOUNT = "0123456789abcdef0123456789abcdef"
FAKE_TOKEN = "fake-token-for-tests"
CONFIG = load_embedding_config({"RADAR_CONFIG_DIR": str(ROOT / "config")})


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({}, ["RADAR_CLOUDFLARE_ACCOUNT_ID", "RADAR_CLOUDFLARE_API_TOKEN"]),
        ({"RADAR_CLOUDFLARE_ACCOUNT_ID": FAKE_ACCOUNT}, ["RADAR_CLOUDFLARE_API_TOKEN"]),
        ({"RADAR_CLOUDFLARE_API_TOKEN": FAKE_TOKEN}, ["RADAR_CLOUDFLARE_ACCOUNT_ID"]),
    ],
)
def test_missing_secrets_raise_config_error_with_only_env_names(
    env: dict[str, str], missing: list[str]
) -> None:
    settings = load_settings({**BASE_ENV, **env})

    with pytest.raises(ConfigError) as info:
        build_embedding_provider(settings, CONFIG)

    assert info.value.problems == [f"缺少環境變數 {name}" for name in missing]
    assert FAKE_ACCOUNT not in str(info.value)
    assert FAKE_TOKEN not in str(info.value)


def test_builds_with_config_and_hides_secrets() -> None:
    settings = load_settings(
        {
            **BASE_ENV,
            "RADAR_CLOUDFLARE_ACCOUNT_ID": FAKE_ACCOUNT,
            "RADAR_CLOUDFLARE_API_TOKEN": FAKE_TOKEN,
        }
    )

    with build_embedding_provider(settings, CONFIG) as provider:
        assert isinstance(provider, WorkersAiEmbeddingProvider)
        assert provider.model_id == "@cf/qwen/qwen3-embedding-0.6b"
        assert provider.dimensions == 1024
        assert provider._client.timeout.read == 60
        for text in (repr(provider), str(provider)):
            assert FAKE_ACCOUNT not in text
            assert FAKE_TOKEN not in text


def test_api_and_worker_processes_do_not_load_the_workers_ai_adapter() -> None:
    # adapter 在 build_embedding_provider 裡才 import：api 不載入；worker 只有跑向量化工作時才載入。
    code = (
        "import sys, radar.entrypoints.api.main, radar.entrypoints.worker.tasks\n"
        "print('radar.adapters.workers_ai' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )

    assert result.stdout.split() == ["False"]


def test_request_longer_than_the_sweep_limit_is_a_config_error() -> None:
    # 一筆向量化的 run 最久會跑的時間不小於 sweep 的時限時，還在等回應的 run 會被誤判逾時收掉。
    # 審查後的修正（Codex 中 3）：用含總時限與鎖等待的 run_limit_seconds 核對（480＋2 × 60＋
    # (2 × 32＋2) × 2＝732 秒）。
    settings = load_settings(
        {
            **BASE_ENV,
            "RADAR_CLOUDFLARE_ACCOUNT_ID": FAKE_ACCOUNT,
            "RADAR_CLOUDFLARE_API_TOKEN": FAKE_TOKEN,
            "RADAR_LLM_RUN_STALE_AFTER": "732",
        }
    )
    assert CONFIG.run_limit_seconds == 732

    with pytest.raises(ConfigError) as info:
        build_embedding_provider(settings, CONFIG)

    [problem] = info.value.problems
    assert "最久約 732 秒" in problem
    assert "RADAR_LLM_RUN_STALE_AFTER（732 秒）" in problem
    assert FAKE_TOKEN not in problem


def test_default_settings_leave_room_for_the_longest_request() -> None:
    settings = load_settings(BASE_ENV)
    assert CONFIG.run_limit_seconds < settings.llm_run_stale_after
