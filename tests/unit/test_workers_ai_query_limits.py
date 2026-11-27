"""搜尋的查詢向量用自己的、較短的時限與重試（Story 4.1 PR B）：`embed_query` 照 `query_api`，
`embed_documents` 照原本的 `api`，兩者互不影響；設定只准比背景向量化短或少（讀檔時擋）。

`httpx2.MockTransport` 與假的單調時鐘（transport 裡往前撥），一個真的請求都不送、不真的等。
account id 與 token 是測試自己編的假值。
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
import stamina
from pydantic import SecretStr

from radar.adapters.workers_ai import WorkersAiEmbeddingProvider
from radar.config import ConfigError, load_embedding_config, load_settings
from radar.domain.embeddings import (
    EmbeddingAttempt,
    EmbeddingConfig,
    EmbeddingTransientError,
    WorkersAiApi,
    report_attempts_to,
)
from radar.domain.policy import CloudPolicy, CloudSafeText, ContentSource, clear_for_cloud
from radar.entrypoints.bootstrap import build_embedding_provider
from tests.unit.test_workers_ai_adapter import _envelope, _FakeClock, _Hooks, _vectors

ROOT = Path(__file__).resolve().parents[2]
REAL_ENV = {"RADAR_CONFIG_DIR": str(ROOT / "config")}
FAKE_ACCOUNT = "0123456789abcdef0123456789abcdef"
FAKE_TOKEN = "fake-token-for-tests"
MODEL = "@cf/qwen/qwen3-embedding-0.6b"
DIMENSIONS = 4
BASE_URL = "https://api.cloudflare.example/client/v4"


def _api(*, timeout: float, total: float, attempts: int) -> WorkersAiApi:
    return WorkersAiApi.model_validate(
        {
            "base_url": BASE_URL,
            "timeout_seconds": timeout,
            "total_timeout_seconds": total,
            "retry": {
                "attempts": attempts,
                "wait_initial_seconds": 1,
                "wait_max_seconds": 30,
                "wait_jitter_seconds": 1,
                "retry_after_max_seconds": 120,
            },
        }
    )


# 背景向量化：逾時 30、總時限 100、5 次；查詢：逾時 2、總時限 3、2 次。
DOCUMENT_API = _api(timeout=30, total=100, attempts=5)
QUERY_API = _api(timeout=2, total=3, attempts=2)


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Iterator[None]:
    with stamina.set_testing(True, attempts=100, cap=True):
        yield


def _query(text: str) -> CloudSafeText:
    return clear_for_cloud(text, ContentSource.USER_QUERY, None, CloudPolicy())


def _document(text: str) -> CloudSafeText:
    return clear_for_cloud(text, ContentSource.OPENALEX_ABSTRACT, None, CloudPolicy())


class _Slow503:
    """每次請求花 `seconds` 秒（撥假時鐘）、回 503；記下每次的讀取逾時。"""

    def __init__(self, clock: _FakeClock, seconds: float) -> None:
        self.clock = clock
        self.seconds = seconds
        self.read_timeouts: list[float] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.read_timeouts.append(request.extensions["timeout"]["read"])
        self.clock.now += self.seconds
        return httpx2.Response(503)


def _provider(
    handler: Any, clock: _FakeClock, *, query_api: WorkersAiApi | None = QUERY_API
) -> WorkersAiEmbeddingProvider:
    return WorkersAiEmbeddingProvider(
        SecretStr(FAKE_ACCOUNT),
        SecretStr(FAKE_TOKEN),
        DOCUMENT_API,
        model_id=MODEL,
        dimensions=DIMENSIONS,
        query_api=query_api,
        transport=httpx2.MockTransport(handler),
        monotonic=clock,
    )


def test_query_uses_its_own_attempts_and_read_timeout() -> None:
    # 每次 0.5 秒、都 503：查詢最多 2 次，每次的讀取逾時是 2 秒（不是 30）。
    clock = _FakeClock()
    handler = _Slow503(clock, 0.5)
    hooks = _Hooks()

    with report_attempts_to(hooks.hooks()), pytest.raises(EmbeddingTransientError):
        _provider(handler, clock).embed_query(_query("trust in platforms"))

    assert handler.read_timeouts == [2, 2]
    assert hooks.attempts == [EmbeddingAttempt(sent=True, http_status=503, billable=True)] * 2


def test_query_stops_at_its_own_total_timeout() -> None:
    # 每次 2 秒：第 1 次結束在 2 秒，再等（≥ 1 秒）就到 3 秒的總時限，不再試
    # （背景的總時限是 100 秒，照背景的數字會再試）。
    clock = _FakeClock()
    handler = _Slow503(clock, 2.0)

    with pytest.raises(EmbeddingTransientError) as caught:
        _provider(handler, clock).embed_query(_query("trust in platforms"))

    assert handler.read_timeouts == [2]
    assert "總時限 3 秒" in str(caught.value)
    assert clock.now == 2.0


def test_query_never_runs_past_its_request_limit() -> None:
    # 每次都花滿逾時並失敗：結束時間不超過查詢自己的上限（3＋2 × 2＝7 秒），遠小於背景的 160 秒。
    clock = _FakeClock()

    def handler(request: httpx2.Request) -> httpx2.Response:
        clock.now += request.extensions["timeout"]["read"]
        raise httpx2.ReadTimeout("slow")

    with pytest.raises(EmbeddingTransientError):
        _provider(handler, clock).embed_query(_query("trust in platforms"))

    assert clock.now <= QUERY_API.request_limit_seconds == 7
    assert DOCUMENT_API.request_limit_seconds == 160


def test_documents_keep_the_background_limits() -> None:
    # 同一個 provider 的 embed_documents 照舊：5 次、讀取逾時 30 秒。
    clock = _FakeClock()
    handler = _Slow503(clock, 0.5)

    with pytest.raises(EmbeddingTransientError):
        _provider(handler, clock).embed_documents([_document("an abstract")])

    assert handler.read_timeouts == [30] * 5


def test_without_query_api_the_query_uses_the_background_limits() -> None:
    clock = _FakeClock()
    handler = _Slow503(clock, 0.5)

    with pytest.raises(EmbeddingTransientError):
        _provider(handler, clock, query_api=None).embed_query(_query("trust in platforms"))

    assert handler.read_timeouts == [30] * 5


def test_query_succeeds_within_its_limits() -> None:
    clock = _FakeClock()
    replies = [httpx2.Response(503), httpx2.Response(200, json=_envelope(_vectors(1)))]

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert json.loads(request.content) == {"queries": ["trust in platforms"]}
        return replies.pop(0)

    assert _provider(handler, clock).embed_query(_query("trust in platforms")) == _vectors(1)[0]


def test_query_api_must_use_the_same_base_url() -> None:
    other = QUERY_API.model_copy(update={"base_url": "https://elsewhere.example/client/v4"})
    with pytest.raises(ValueError, match="base_url"):
        _provider(_Slow503(_FakeClock(), 0), _FakeClock(), query_api=other)


# --- 設定 -------------------------------------------------------------------------------------


def test_shipped_config_has_short_query_limits_and_unchanged_background_limits() -> None:
    config = load_embedding_config(REAL_ENV)

    assert config.query_request is not None
    assert (
        config.query_request.timeout_seconds,
        config.query_request.total_timeout_seconds,
        config.query_request.attempts,
    ) == (2, 3, 2)
    query_api = config.query_api
    assert query_api is not None
    assert (query_api.timeout_seconds, query_api.total_timeout_seconds) == (2, 3)
    assert query_api.retry.attempts == 2
    assert query_api.request_limit_seconds == 7
    # 背景向量化的數字一個都沒變（480 秒、60 秒、5 次）。
    assert (config.api.timeout_seconds, config.api.total_timeout_seconds) == (60, 480)
    assert config.api.retry.attempts == 5
    assert query_api.base_url == config.api.base_url
    assert query_api.retry.wait_initial_seconds == config.api.retry.wait_initial_seconds


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("timeout_seconds", 61, "timeout_seconds 不能比"),
        ("total_timeout_seconds", 481, "total_timeout_seconds 不能比"),
        ("attempts", 6, "attempts 不能比"),
        ("timeout_seconds", 0, "timeout_seconds"),
        ("attempts", 0, "attempts"),
    ],
)
def test_query_limits_longer_than_the_background_are_rejected_at_load(
    tmp_path: Path, field: str, value: float, message: str
) -> None:
    config = load_embedding_config(REAL_ENV)
    data = config.model_dump(mode="json")
    data["query_request"][field] = value
    with pytest.raises(ValueError, match=message):
        EmbeddingConfig.model_validate(data)

    # 讀檔時同樣擋下（api 啟動就失敗）。
    text = (ROOT / "config" / "embeddings.yaml").read_text(encoding="utf-8")
    current = {"timeout_seconds": "2", "total_timeout_seconds": "3", "attempts": "2"}[field]
    old = f"  {field}: {current}\n"
    assert text.count(old) == 1
    (tmp_path / "embeddings.yaml").write_text(
        text.replace(old, f"  {field}: {value}\n"), encoding="utf-8"
    )
    with pytest.raises(ConfigError) as caught:
        load_embedding_config({"RADAR_CONFIG_DIR": str(tmp_path)})
    assert all(problem.startswith("embeddings.yaml：") for problem in caught.value.problems)


def test_without_query_request_the_config_still_loads_for_the_worker() -> None:
    data = load_embedding_config(REAL_ENV).model_dump(mode="json")
    del data["query_request"]

    config = EmbeddingConfig.model_validate(data)

    assert config.query_request is None
    assert config.query_api is None


def test_bootstrap_passes_the_query_limits_to_the_adapter() -> None:
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql://radar@db:5432/radar",
            "RADAR_CLOUDFLARE_ACCOUNT_ID": FAKE_ACCOUNT,
            "RADAR_CLOUDFLARE_API_TOKEN": FAKE_TOKEN,
        }
    )
    config = load_embedding_config(REAL_ENV)

    with build_embedding_provider(settings, config) as provider:
        assert provider._query_api == config.query_api
        assert provider._api == config.api
