"""組裝：設定的逾時一路傳到資料庫驅動；抓取用的 adapter 缺設定丟 ConfigError（都不真的連線）。"""

import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest

from radar.adapters.object_store import S3ObjectStore
from radar.adapters.openalex import OpenAlexSource
from radar.config import ConfigError, load_domain_config
from radar.domain.domain_config import OpenAlexConfig
from radar.domain.settings import Settings
from radar.entrypoints.bootstrap import (
    build_object_store,
    build_paper_source,
    build_readiness_probes,
)
from radar.repo.db import Database

ROOT = Path(__file__).resolve().parents[2]


def test_timeouts_reach_driver_connect_params(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_connect(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise psycopg.OperationalError("fake: not connecting")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    # 7 秒不是預設值（3），確認傳下去的是設定值而不是預設。
    settings = Settings.model_validate(
        {"database_url": "postgresql://radar@db:5432/radar", "database_connect_timeout": 7}
    )

    assert build_readiness_probes(settings, pool_size=1).ping() is False
    assert captured["connect_timeout"] == 7
    assert captured["options"] == "-c statement_timeout=7000"
    # 用戶端的 TCP 期限：網路卡住時也會在有限時間內失敗。
    assert captured["tcp_user_timeout"] == 7000
    assert captured["keepalives"] == 1
    assert captured["keepalives_idle"] == 7
    assert captured["keepalives_interval"] == 1
    assert captured["keepalives_count"] == 7


def test_existing_url_options_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_connect(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise psycopg.OperationalError("fake: not connecting")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    settings = Settings.model_validate(
        {
            "database_url": "postgresql://radar@db:5432/radar?options=-c%20role%3Dradar_app",
            "database_connect_timeout": 7,
        }
    )

    assert build_readiness_probes(settings, pool_size=1).ping() is False
    assert captured["options"] == "-c role=radar_app -c statement_timeout=7000"


def test_pool_size_reaches_readiness_pool() -> None:
    settings = Settings.model_validate({"database_url": "postgresql://radar_app@db:5432/radar"})

    probes = build_readiness_probes(settings, pool_size=5)
    database = probes.ping.__self__  # type: ignore[attr-defined]

    assert isinstance(database, Database)
    assert database._engine.pool.size() == 5  # type: ignore[attr-defined]


# --- 抓取用的 adapter（Story 1.6）：缺必要設定丟 ConfigError，組裝時不連線 ---------------------

APP_URL = "postgresql://radar_app@db:5432/radar"
OBJECT_STORE_ENV = {
    "object_store_endpoint_url": "http://127.0.0.1:9000",
    "object_store_region": "auto",
    "object_store_bucket": "radar-test",
    "object_store_access_key_id": "test-access-key",
    "object_store_secret_access_key": "test-secret-key",
}


def _openalex_config() -> OpenAlexConfig:
    return load_domain_config({"RADAR_CONFIG_DIR": str(ROOT / "config")}).openalex


def test_paper_source_needs_api_key() -> None:
    settings = Settings.model_validate({"database_url": APP_URL})

    with pytest.raises(ConfigError) as caught:
        build_paper_source(settings, _openalex_config())

    assert caught.value.problems == ["缺少環境變數 RADAR_OPENALEX_API_KEY"]


def test_paper_source_is_built_with_key_and_config() -> None:
    settings = Settings.model_validate({"database_url": APP_URL, "openalex_api_key": "test-key"})

    with build_paper_source(settings, _openalex_config()) as source:
        assert isinstance(source, OpenAlexSource)
        assert source.name == "openalex"
        assert "test-key" not in repr(source)


def test_object_store_lists_every_missing_setting() -> None:
    settings = Settings.model_validate(
        {"database_url": APP_URL, "object_store_bucket": "radar-test"}
    )

    with pytest.raises(ConfigError) as caught:
        build_object_store(settings)

    assert caught.value.problems == [
        "缺少環境變數 RADAR_OBJECT_STORE_ENDPOINT_URL",
        "缺少環境變數 RADAR_OBJECT_STORE_REGION",
        "缺少環境變數 RADAR_OBJECT_STORE_ACCESS_KEY_ID",
        "缺少環境變數 RADAR_OBJECT_STORE_SECRET_ACCESS_KEY",
    ]
    assert "test-" not in str(caught.value)


def test_object_store_client_has_botocore_retries_off() -> None:
    settings = Settings.model_validate(
        {"database_url": APP_URL, **OBJECT_STORE_ENV, "object_store_timeout": 7}
    )

    store = build_object_store(settings)

    assert isinstance(store, S3ObjectStore)
    client = store._client
    assert client.meta.endpoint_url == "http://127.0.0.1:9000"
    assert client.meta.region_name == "auto"
    assert client.meta.config.retries["total_max_attempts"] == 1
    assert client.meta.config.connect_timeout == 7
    assert client.meta.config.read_timeout == 7
    assert store._bucket == "radar-test"
    assert store._attempts == settings.object_store_attempts


def test_object_store_retry_waits_follow_settings() -> None:
    # Codex 審查第 6 點：退避起點、上限、抖動、Retry-After 上限由 bootstrap 從設定傳進 adapter。
    settings = Settings.model_validate(
        {
            "database_url": APP_URL,
            **OBJECT_STORE_ENV,
            "object_store_attempts": 4,
            "object_store_retry_wait_initial": 2,
            "object_store_retry_wait_max": 40,
            "object_store_retry_wait_jitter": 0,
            "object_store_retry_after_max": 90,
        }
    )

    store = build_object_store(settings)

    assert (
        store._attempts,
        store._wait_initial,
        store._wait_max,
        store._wait_jitter,
        store._retry_after_max,
    ) == (4, 2.0, 40.0, 0.0, 90)


def test_object_store_constructor_defaults_match_settings_defaults() -> None:
    # 預設值寫在兩處（設定與 adapter 的建構子），要一致。
    defaults = Settings.model_validate({"database_url": APP_URL})
    store = S3ObjectStore(object(), "radar-test", attempts=3)

    assert (
        store._wait_initial,
        store._wait_max,
        store._wait_jitter,
        store._retry_after_max,
    ) == (
        defaults.object_store_retry_wait_initial,
        defaults.object_store_retry_wait_max,
        defaults.object_store_retry_wait_jitter,
        defaults.object_store_retry_after_max,
    )


def test_api_process_does_not_load_boto3() -> None:
    # api 為了 build_readiness_probes import bootstrap；OpenAlex 與物件儲存的 adapter 在 build_*
    # 函式裡才 import，api 行程不會載入 boto3（約 11 MB，mem_limit 很緊）。
    code = (
        "import sys, radar.entrypoints.api.main\n"
        "print('boto3' in sys.modules, 'radar.adapters.object_store' in sys.modules,"
        " 'radar.adapters.openalex' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )

    assert result.stdout.split() == ["False", "False", "False"]
