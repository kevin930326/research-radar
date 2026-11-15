"""向量化服務對真的 PostgreSQL（Story 2.8，spec 的 I/O 表每一列）。

provider 一律用 `FakeEmbeddingProvider`（不連網路；向量由 sha256 推出，不是真的模型）或包著它的
測試替身；標題、摘要都是測試自己編的。假模型 `fake-embedding`（8 維）由超級使用者登記進
`embedding_model`（應用程式角色不能寫模型登錄）。

- 新論文：標題＋空行＋摘要；沒摘要或手填：只送標題；被排除：不送。
- 輸入沒變不重算；摘要變了只重算那一篇。
- 放行擋下（Semantic Scholar 的摘要還沒授權）不送。
- 額度：今天（UTC）的估計用量到比例就停下、不算失敗，下一次（隔天）接著做。
- 失敗：回應不對、暫時性錯誤整批不寫、run 標成 failed；設定錯往上丟。
- 送出後輸入又變了：那一篇不寫，下一次重算。
- 兩個模型的向量並存，查詢只比指定的模型（FR-5）。
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from structlog.testing import capture_logs

from radar.adapters.fake import FakeEmbeddingProvider
from radar.domain.embeddings import (
    EmbeddingConfig,
    EmbeddingConfigurationError,
    EmbeddingRequestError,
    EmbeddingTransientError,
    EmbedRunResult,
    EmbedStop,
    estimate_embedding_tokens,
)
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import embedding_input_sha256
from radar.domain.permissions import PermissionDeniedError
from radar.domain.policy import CloudPolicy, CloudSafeText, cloud_text
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.repo.embeddings import find_paper_embedding
from radar.repo.retrieval import search_paper_vectors
from radar.services.embeddings import (
    EmbeddingModelMismatchError,
    embed_papers,
    estimate_embedding_backlog,
)
from tests.database import TestDatabase, migrate
from tests.llm_support import CODE_VERSION, create_paper, execute, set_abstract

SYSTEM = SystemViewer()
MEMBER = MemberViewer(member_id=uuid.UUID("018f0000-0000-7000-8000-0000000000a1"), is_admin=True)
POLICY = CloudPolicy()
NOW = datetime(2026, 11, 15, 4, 0, tzinfo=UTC)
MODEL = "fake-embedding"
OTHER_MODEL = "fake-embedding-b"
DIMENSIONS = 8


def _config(**changes: Any) -> EmbeddingConfig:
    values: dict[str, Any] = {
        "model_id": MODEL,
        "dimensions": DIMENSIONS,
        "batch_size": 2,
        "query_instruction": None,
        "api": {
            "base_url": "https://api.cloudflare.example/client/v4",
            "timeout_seconds": 30,
            "retry": {
                "attempts": 3,
                "wait_initial_seconds": 1,
                "wait_max_seconds": 30,
                "wait_jitter_seconds": 1,
                "retry_after_max_seconds": 120,
            },
        },
        "quota": {
            "daily_free_neurons": 10000,
            "neurons_per_million_input_tokens": 1075,
            "stop_at_ratio": "0.9",
            "verified_on": "2026-11-15",
            "source_url": "https://developers.cloudflare.com/workers-ai/platform/pricing/",
        },
        "token_estimate": {"bytes_per_token": "3", "overhead_tokens_per_text": 4},
    }
    values.update(changes)
    return EmbeddingConfig.model_validate(values)


CONFIG = _config()


def _register(db: TestDatabase, model_id: str, dimensions: int) -> None:
    execute(
        db,
        "INSERT INTO embedding_model (model_id, provider, dimensions) VALUES (%s, 'fake', %s)",
        model_id,
        dimensions,
    )


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    _register(fresh_database, MODEL, DIMENSIONS)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=4)


def _run(
    database: Database,
    provider: Any = None,
    *,
    config: EmbeddingConfig = CONFIG,
    clock: datetime = NOW,
    **kwargs: Any,
) -> EmbedRunResult:
    return embed_papers(
        SYSTEM,
        database,
        provider if provider is not None else FakeEmbeddingProvider(DIMENSIONS),
        config,
        POLICY,
        code_version=CODE_VERSION,
        clock=lambda: clock,
        **kwargs,
    )


def _paper(database: Database, n: int, *, abstract: str | None = "auto") -> uuid.UUID:
    return create_paper(
        database,
        doi=f"10.1/p{n}",
        openalex_id=f"W{n}",
        title=f"Paper {n} on platform adoption",
        abstract=f"Abstract {n}: teams adopt platforms." if abstract == "auto" else abstract,
    )


def _texts(provider: FakeEmbeddingProvider) -> list[list[str]]:
    return [[cloud_text(text) for text in call] for call in provider.document_calls]


def _embeddings(db: TestDatabase, model_id: str = MODEL) -> dict[uuid.UUID, tuple[Any, ...]]:
    rows = execute(
        db,
        "SELECT item_id, input_sha256, run_id, dimensions FROM embedding WHERE model_id = %s",
        model_id,
    )
    return {row[0]: row[1:] for row in rows}


def _runs(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db,
        "SELECT paper_id, status, failure_reason, model, input_kind, input_sha256, prompt_version,"
        " prompt_sha256, call_mode FROM derivation_run WHERE task = 'embed' ORDER BY created_at",
    )


def _calls(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db,
        "SELECT operation, http_status, cost_usd, estimated_input_tokens, called_at"
        " FROM api_call WHERE source = 'workers_ai' ORDER BY called_at, id",
    )


def _sha(title: str, abstract: str | None = None) -> str:
    if abstract is None:
        return embedding_input_sha256(title, "title")
    return embedding_input_sha256(f"{title}\n\n{abstract}", "abstract")


# --- 正常的路 ---------------------------------------------------------------------------------


def test_new_papers_get_title_blank_line_abstract_in_batches(
    db: TestDatabase, database: Database
) -> None:
    ids = [_paper(database, n) for n in (1, 2, 3)]
    provider = FakeEmbeddingProvider(DIMENSIONS)

    result = _run(database, provider)

    texts = [
        f"Paper {n} on platform adoption\n\nAbstract {n}: teams adopt platforms." for n in (1, 2, 3)
    ]
    # 一批 2 篇：兩次請求；論文依 id 順序（uuidv7，建立順序）。
    assert _texts(provider) == [texts[:2], texts[2:]]
    assert provider.query_calls == []
    tokens = estimate_embedding_tokens(texts, CONFIG.token_estimate)
    assert result == EmbedRunResult(
        stop=EmbedStop.COMPLETED, embedded=3, estimated_tokens=tokens, requests=2
    )
    stored = _embeddings(db)
    assert set(stored) == set(ids)
    for paper_id, text in zip(ids, texts, strict=True):
        sha = embedding_input_sha256(text, "abstract")
        assert stored[paper_id][0] == sha
        assert stored[paper_id][2] == DIMENSIONS
    # 向量照存（halfvec 精度內和 provider 給的一樣）。
    expected = FakeEmbeddingProvider(DIMENSIONS).embed_documents([provider.document_calls[0][0]])[0]
    with database.transaction() as connection:
        found = find_paper_embedding(SYSTEM, connection, ids[0], model_id=MODEL)
    assert found is not None
    assert found.vector == pytest.approx(expected, abs=1e-3)
    # 來歷：每篇一筆 embed 的 run，沒有 prompt，成功；run_id 指到它。
    runs = _runs(db)
    assert [(row[0], row[1], row[2], row[3], row[4]) for row in runs] == [
        (paper_id, "succeeded", None, MODEL, "abstract") for paper_id in ids
    ]
    assert all(row[6] is None and row[7] is None and row[8] == "interactive" for row in runs)
    run_ids = {row[0] for row in execute(db, "SELECT id FROM derivation_run WHERE task = 'embed'")}
    assert {value[1] for value in stored.values()} == run_ids
    # 用量：每次請求一筆，免費額度內記 0 元，token 是自己估的。
    calls = _calls(db)
    assert [(op, status, cost) for op, status, cost, _tokens, _at in calls] == [
        ("embed_documents", 200, Decimal(0)),
        ("embed_documents", 200, Decimal(0)),
    ]
    assert sum(row[3] for row in calls) == tokens
    assert all(row[4] == NOW for row in calls)


def test_title_only_without_abstract_and_for_manual_papers(
    db: TestDatabase, database: Database
) -> None:
    no_abstract = _paper(database, 1, abstract=None)
    manual = _paper(database, 2)
    execute(db, "UPDATE paper SET origin = 'manual' WHERE id = %s", manual)
    provider = FakeEmbeddingProvider(DIMENSIONS)

    result = _run(database, provider)

    assert result.embedded == 2
    assert _texts(provider) == [["Paper 1 on platform adoption", "Paper 2 on platform adoption"]]
    stored = _embeddings(db)
    assert stored[no_abstract][0] == _sha("Paper 1 on platform adoption")
    assert stored[manual][0] == _sha("Paper 2 on platform adoption")
    assert {row[4] for row in _runs(db)} == {"title"}


def test_excluded_papers_are_not_embedded(db: TestDatabase, database: Database) -> None:
    excluded = _paper(database, 1)
    execute(db, "UPDATE paper SET excluded_reason = 'editors_comments' WHERE id = %s", excluded)
    provider = FakeEmbeddingProvider(DIMENSIONS)

    result = _run(database, provider)

    assert result == EmbedRunResult(stop=EmbedStop.COMPLETED, not_applicable=1)
    assert provider.document_calls == []
    assert _embeddings(db) == {}
    assert _runs(db) == []
    assert _calls(db) == []


def test_unchanged_input_is_not_recomputed(db: TestDatabase, database: Database) -> None:
    for n in (1, 2, 3):
        _paper(database, n)
    _run(database)
    before = (_embeddings(db), _runs(db), _calls(db))
    provider = FakeEmbeddingProvider(DIMENSIONS)

    result = _run(database, provider, clock=NOW + timedelta(hours=1))

    assert result == EmbedRunResult(stop=EmbedStop.COMPLETED, unchanged=3)
    assert provider.document_calls == []
    assert (_embeddings(db), _runs(db), _calls(db)) == before


def test_changed_abstract_recomputes_only_that_paper(db: TestDatabase, database: Database) -> None:
    first = _paper(database, 1)
    second = _paper(database, 2)
    _run(database)
    old = _embeddings(db)
    set_abstract(db, second, "A revised abstract.")
    provider = FakeEmbeddingProvider(DIMENSIONS)

    result = _run(database, provider)

    assert result.embedded == 1
    assert result.unchanged == 1
    assert _texts(provider) == [["Paper 2 on platform adoption\n\nA revised abstract."]]
    new = _embeddings(db)
    assert new[first] == old[first]
    assert new[second][0] == _sha("Paper 2 on platform adoption", "A revised abstract.")
    assert new[second][1] != old[second][1]  # 新的 run


def test_blocked_source_is_not_sent(db: TestDatabase, database: Database) -> None:
    blocked = _paper(database, 1)
    execute(
        db, "UPDATE paper_abstract SET source = 'semantic_scholar' WHERE paper_id = %s", blocked
    )
    _paper(database, 2)
    provider = FakeEmbeddingProvider(DIMENSIONS)

    with capture_logs() as logs:
        result = _run(database, provider)

    assert result.blocked == 1
    assert result.embedded == 1
    assert _texts(provider) == [
        ["Paper 2 on platform adoption\n\nAbstract 2: teams adopt platforms."]
    ]
    assert blocked not in _embeddings(db)
    [log] = [entry for entry in logs if entry["event"] == "向量化的輸入放行擋下"]
    assert log["source"] == "semantic_scholar_abstract"
    # 標題與摘要都沒進日誌。
    assert "Abstract 1" not in str(logs)


def test_limit_stops_after_n_papers(db: TestDatabase, database: Database) -> None:
    for n in (1, 2, 3):
        _paper(database, n)

    result = _run(database, limit=1)

    assert result.stop is EmbedStop.LIMIT_REACHED
    assert result.embedded == 1
    assert len(_embeddings(db)) == 1
    # 再跑一次接著做剩下的。
    assert _run(database).embedded == 2


# --- 額度 -------------------------------------------------------------------------------------


def _tiny_quota(neurons: int = 1) -> EmbeddingConfig:
    # 每日 1 neuron、到 100% 才停：一天約 930 token（1 ÷ 1,075 × 1,000,000）。
    return _config(
        quota={
            "daily_free_neurons": neurons,
            "neurons_per_million_input_tokens": 1075,
            "stop_at_ratio": "1",
            "verified_on": "2026-11-15",
            "source_url": "https://developers.cloudflare.com/workers-ai/platform/pricing/",
        }
    )


def _insert_usage(db: TestDatabase, tokens: int, at: datetime) -> None:
    execute(
        db,
        "INSERT INTO api_call (source, operation, http_status, cost_usd, estimated_input_tokens,"
        " called_at) VALUES ('workers_ai', 'embed_documents', 200, 0, %s, %s)",
        tokens,
        at,
    )


def test_quota_stop_is_not_a_failure_and_resumes_the_next_utc_day(
    db: TestDatabase, database: Database
) -> None:
    for n in (1, 2, 3, 4):
        _paper(database, n)
    config = _tiny_quota()
    # 今天（UTC 00:00 起）已經估計用了 850 token；第一批（2 篇，約 40 token）還在 930 內，
    # 第二批就超過。昨天 23:59:59 的用量不算。
    _insert_usage(db, 850, datetime(2026, 11, 15, 0, 0, tzinfo=UTC))
    _insert_usage(db, 100_000, datetime(2026, 11, 14, 23, 59, 59, tzinfo=UTC))

    with capture_logs() as logs:
        result = _run(database, config=config)

    assert result.stop is EmbedStop.QUOTA_REACHED
    assert result.embedded == 2
    assert result.failed == 0
    assert result.failure_reason is None
    [warning] = [entry for entry in logs if entry["log_level"] == "warning"]
    assert "留到下一次續跑" in warning["event"]
    assert len(_embeddings(db)) == 2
    assert {row[1] for row in _runs(db)} == {"succeeded"}

    # 同一天再跑：一篇都不送。
    provider = FakeEmbeddingProvider(DIMENSIONS)
    again = _run(database, provider, config=config, clock=NOW + timedelta(hours=10))
    assert again.stop is EmbedStop.QUOTA_REACHED
    assert provider.document_calls == []

    # 隔天（UTC）重置，接著做剩下的兩篇。
    tomorrow = _run(database, config=config, clock=datetime(2026, 11, 16, 0, 0, 1, tzinfo=UTC))
    assert tomorrow.stop is EmbedStop.COMPLETED
    assert tomorrow.embedded == 2
    assert tomorrow.unchanged == 2
    assert len(_embeddings(db)) == 4


def test_clock_in_another_timezone_still_uses_the_utc_day(
    db: TestDatabase, database: Database
) -> None:
    _paper(database, 1)
    # 台北 11/15 07:00＝UTC 11/14 23:00：UTC 11/14 的用量算「今天」（照台北的日期算就會漏掉）。
    taipei = datetime(2026, 11, 15, 7, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    _insert_usage(db, 1000, datetime(2026, 11, 14, 1, 0, tzinfo=UTC))

    result = _run(database, config=_tiny_quota(), clock=taipei)

    assert result.stop is EmbedStop.QUOTA_REACHED


# --- 失敗 -------------------------------------------------------------------------------------


class _Failing(FakeEmbeddingProvider):
    """第 `fail_on` 次（從 1 起算）`embed_documents` 丟 `error`（之前照常回）。"""

    def __init__(self, error: Exception, fail_on: int = 1) -> None:
        super().__init__(DIMENSIONS)
        self.error = error
        self.fail_on = fail_on

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        vectors = super().embed_documents(texts)
        if len(self.document_calls) == self.fail_on:
            raise self.error
        return vectors


class _WrongDimensions(FakeEmbeddingProvider):
    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        return [vector[:-1] for vector in super().embed_documents(texts)]


def test_wrong_dimensions_write_nothing_and_fail_the_runs(
    db: TestDatabase, database: Database
) -> None:
    for n in (1, 2, 3):
        _paper(database, n)

    result = _run(database, _WrongDimensions(DIMENSIONS))

    assert result.stop is EmbedStop.FAILED
    assert result.failure_reason is ErrorCode.EMBED_RESPONSE_INVALID
    assert result.failed == 2
    assert result.requests == 1  # 第一批失敗就停下
    assert _embeddings(db) == {}
    assert [(row[1], row[2]) for row in _runs(db)] == [
        ("failed", "embed_response_invalid"),
        ("failed", "embed_response_invalid"),
    ]
    # provider 回了（200）就照記用量。
    assert [(row[1], row[3] > 0) for row in _calls(db)] == [(200, True)]


def test_transient_failure_stops_after_that_batch_keeping_earlier_batches(
    db: TestDatabase, database: Database
) -> None:
    for n in (1, 2, 3, 4, 5):
        _paper(database, n)
    provider = _Failing(EmbeddingTransientError("重試用完", http_status=503), fail_on=2)

    result = _run(database, provider)

    assert result.stop is EmbedStop.FAILED
    assert result.failure_reason is ErrorCode.EMBED_TRANSIENT
    assert result.embedded == 2
    assert result.failed == 2
    assert len(provider.document_calls) == 2
    assert len(_embeddings(db)) == 2
    assert [row[1] for row in _calls(db)] == [200, 503]
    # 下一次重算失敗的那兩篇與沒送的那一篇。
    assert _run(database).embedded == 3


def test_configuration_error_is_raised_after_failing_the_runs(
    db: TestDatabase, database: Database
) -> None:
    _paper(database, 1)
    provider = _Failing(EmbeddingConfigurationError("HTTP 401", http_status=401))

    with pytest.raises(EmbeddingConfigurationError):
        _run(database, provider)

    assert [(row[1], row[2]) for row in _runs(db)] == [("failed", "embed_configuration")]
    assert [row[1] for row in _calls(db)] == [401]
    assert _embeddings(db) == {}


def test_unexpected_error_fails_the_runs_and_is_raised(
    db: TestDatabase, database: Database
) -> None:
    _paper(database, 1)

    with pytest.raises(RuntimeError, match="boom"):
        _run(database, _Failing(RuntimeError("boom")))

    assert [(row[1], row[2]) for row in _runs(db)] == [("failed", "embed_failed")]
    assert [row[1] for row in _calls(db)] == [None]


class _ChangesAbstractWhileCalling(FakeEmbeddingProvider):
    """送出後、寫入前改掉某一篇的摘要（模擬每週更新同時改了它）。"""

    def __init__(self, db: TestDatabase, paper_id: uuid.UUID) -> None:
        super().__init__(DIMENSIONS)
        self.db = db
        self.paper_id = paper_id

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        vectors = super().embed_documents(texts)
        if len(self.document_calls) == 1:
            set_abstract(self.db, self.paper_id, "Changed while the request was in flight.")
        return vectors


def test_input_changed_before_write_skips_that_paper_and_recomputes_next_time(
    db: TestDatabase, database: Database
) -> None:
    kept = _paper(database, 1)
    changed = _paper(database, 2)

    result = _run(database, _ChangesAbstractWhileCalling(db, changed))

    assert result.embedded == 1
    assert result.input_changed == 1
    assert set(_embeddings(db)) == {kept}
    statuses = {row[0]: (row[1], row[2]) for row in _runs(db)}
    assert statuses == {
        kept: ("succeeded", None),
        changed: ("failed", "embed_input_changed"),
    }

    again = _run(database)
    assert again.embedded == 1
    assert _embeddings(db)[changed][0] == _sha(
        "Paper 2 on platform adoption", "Changed while the request was in flight."
    )


def test_same_input_running_elsewhere_is_busy(db: TestDatabase, database: Database) -> None:
    busy = _paper(database, 1)
    _paper(database, 2)
    sha = _sha("Paper 1 on platform adoption", "Abstract 1: teams adopt platforms.")
    execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, model, input_sha256, input_kind, params,"
        " code_version, status, call_mode) VALUES (%s, 'embed', %s, %s, 'abstract', '{}',"
        " 'test', 'running', 'interactive')",
        busy,
        MODEL,
        sha,
    )
    provider = FakeEmbeddingProvider(DIMENSIONS)

    result = _run(database, provider)

    assert result.busy == 1
    assert result.embedded == 1
    assert busy not in _embeddings(db)


class _RejectsText(FakeEmbeddingProvider):
    """送出的這一批裡有 `poison` 這段文字就回請求不合法（400），其他照常。"""

    def __init__(self, poison: str) -> None:
        super().__init__(DIMENSIONS)
        self.poison = poison

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        vectors = super().embed_documents(texts)
        if any(self.poison in cloud_text(text) for text in texts):
            raise EmbeddingRequestError("HTTP 400", http_status=400)
        return vectors


def test_rejected_batch_is_resent_one_by_one_so_one_bad_paper_does_not_block_the_rest(
    db: TestDatabase, database: Database
) -> None:
    bad = _paper(database, 1)
    for n in (2, 3):
        _paper(database, n)
    provider = _RejectsText("Paper 1 ")

    result = _run(database, provider)

    assert result.stop is EmbedStop.COMPLETED
    assert result.embedded == 2
    assert result.failed == 1
    assert result.failure_reason is ErrorCode.EMBED_REQUEST_INVALID
    # 第一批（1、2）被拒 → 1 單獨重送還是被拒 → 2 單獨送 → 3。
    assert [len(call) for call in provider.document_calls] == [2, 1, 1, 1]
    assert bad not in _embeddings(db)
    assert len(_embeddings(db)) == 2
    assert [row[1] for row in _calls(db)] == [400, 400, 200, 200]
    failed = [row for row in _runs(db) if row[1] == "failed"]
    assert {row[2] for row in failed} == {"embed_request_invalid"}
    assert [row[0] for row in failed].count(bad) == 2


class _ExpiresRunsWhileCalling(FakeEmbeddingProvider):
    """送出後、寫入前，sweep 把還在跑的 run 判定逾時收掉。"""

    def __init__(self, db: TestDatabase) -> None:
        super().__init__(DIMENSIONS)
        self.db = db

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        vectors = super().embed_documents(texts)
        execute(
            self.db,
            "UPDATE derivation_run SET status = 'failed', failure_reason = 'llm_run_expired',"
            " finished_at = now() WHERE status = 'running'",
        )
        return vectors


def test_run_expired_by_sweep_before_write_is_not_written(
    db: TestDatabase, database: Database
) -> None:
    _paper(database, 1)

    result = _run(database, _ExpiresRunsWhileCalling(db))

    assert result.embedded == 0
    assert result.failed == 1
    assert _embeddings(db) == {}
    assert [(row[1], row[2]) for row in _runs(db)] == [("failed", "llm_run_expired")]
    # 下一次重算。
    assert _run(database).embedded == 1


def test_configuration_error_records_zero_estimated_tokens(
    db: TestDatabase, database: Database
) -> None:
    _paper(database, 1)
    provider = _Failing(EmbeddingConfigurationError("HTTP 403", http_status=403))

    with pytest.raises(EmbeddingConfigurationError):
        _run(database, provider)

    assert [(row[1], row[3]) for row in _calls(db)] == [(403, 0)]


def test_busy_papers_do_not_count_toward_the_limit(db: TestDatabase, database: Database) -> None:
    busy = _paper(database, 1)
    _paper(database, 2)
    _paper(database, 3)
    execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, model, input_sha256, input_kind, params,"
        " code_version, status, call_mode) VALUES (%s, 'embed', %s, %s, 'abstract', '{}',"
        " 'test', 'running', 'interactive')",
        busy,
        MODEL,
        _sha("Paper 1 on platform adoption", "Abstract 1: teams adopt platforms."),
    )

    result = _run(database, limit=2)

    assert result.stop is EmbedStop.LIMIT_REACHED
    assert result.busy == 1
    assert result.embedded == 2
    assert busy not in _embeddings(db)


# --- 前置檢查 ---------------------------------------------------------------------------------


def test_member_cannot_run_embeddings(db: TestDatabase, database: Database) -> None:
    _paper(database, 1)
    with pytest.raises(PermissionDeniedError):
        embed_papers(
            MEMBER,
            database,
            FakeEmbeddingProvider(DIMENSIONS),
            CONFIG,
            POLICY,
            code_version=CODE_VERSION,
            clock=lambda: NOW,
        )
    assert _runs(db) == []


@pytest.mark.parametrize(
    ("provider", "config", "message"),
    [
        (FakeEmbeddingProvider(DIMENSIONS, model_id="other"), CONFIG, "provider"),
        (
            FakeEmbeddingProvider(DIMENSIONS, model_id="unregistered"),
            _config(model_id="unregistered"),
            "沒有登記",
        ),
        (FakeEmbeddingProvider(4), _config(dimensions=4), "維度"),
    ],
)
def test_model_mismatch_sends_nothing(
    db: TestDatabase,
    database: Database,
    provider: FakeEmbeddingProvider,
    config: EmbeddingConfig,
    message: str,
) -> None:
    _paper(database, 1)
    with pytest.raises(EmbeddingModelMismatchError, match=message):
        _run(database, provider, config=config)
    assert provider.document_calls == []
    assert _runs(db) == []


@pytest.mark.parametrize("limit", [0, -1, True])
def test_limit_must_be_positive(database: Database, limit: Any) -> None:
    with pytest.raises(ValueError, match="limit"):
        _run(database, limit=limit)


def test_naive_clock_is_rejected(database: Database) -> None:
    _paper(database, 1)
    with pytest.raises(ValueError, match="時區"):
        _run(database, clock=datetime(2026, 11, 15, 4, 0))


# --- 估算 -------------------------------------------------------------------------------------


def test_backlog_estimate_matches_what_a_run_would_send(
    db: TestDatabase, database: Database
) -> None:
    for n in (1, 2, 3):
        _paper(database, n)
    blocked = _paper(database, 4)
    execute(
        db, "UPDATE paper_abstract SET source = 'semantic_scholar' WHERE paper_id = %s", blocked
    )
    before = _calls(db)

    backlog = estimate_embedding_backlog(SYSTEM, database, CONFIG, POLICY)
    result = _run(database)

    assert backlog.papers == result.embedded == 3
    assert backlog.estimated_tokens == result.estimated_tokens
    assert backlog.blocked == 1
    assert before == []
    after = estimate_embedding_backlog(SYSTEM, database, CONFIG, POLICY)
    assert (after.papers, after.estimated_tokens) == (0, 0)


# --- 兩個模型並存與查詢（FR-5） ---------------------------------------------------------------


def test_two_models_coexist_and_queries_only_compare_the_named_model(
    db: TestDatabase, database: Database
) -> None:
    _register(db, OTHER_MODEL, 4)
    ids = [_paper(database, n) for n in (1, 2, 3)]
    first = FakeEmbeddingProvider(DIMENSIONS)
    other = FakeEmbeddingProvider(4, model_id=OTHER_MODEL)

    _run(database, first)
    _run(database, other, config=_config(model_id=OTHER_MODEL, dimensions=4))

    # 同一篇論文兩個模型各一筆。
    assert set(_embeddings(db, MODEL)) == set(_embeddings(db, OTHER_MODEL)) == set(ids)
    assert execute(db, "SELECT count(*) FROM embedding") == [(6,)]
    with database.transaction() as connection:
        a = find_paper_embedding(SYSTEM, connection, ids[0], model_id=MODEL)
        b = find_paper_embedding(SYSTEM, connection, ids[0], model_id=OTHER_MODEL)
    assert a is not None
    assert b is not None
    assert (len(a.vector), len(b.vector)) == (DIMENSIONS, 4)

    query = list(a.vector)
    with database.transaction() as connection:
        matches = search_paper_vectors(SYSTEM, connection, model_id=MODEL, vector=query, limit=10)
        # 另一個模型的維度不同：拿這個模型的查詢向量去查它直接擋下，不會混著比。
        with pytest.raises(ValueError, match="維度"):
            search_paper_vectors(SYSTEM, connection, model_id=OTHER_MODEL, vector=query, limit=10)
        with pytest.raises(LookupError):
            search_paper_vectors(SYSTEM, connection, model_id="nope", vector=query, limit=10)
    # 只比 MODEL 的 3 筆；最近的就是它自己。
    assert len(matches) == 3
    assert matches[0].paper_id == ids[0]
    assert matches[0].distance == pytest.approx(0, abs=1e-3)
    assert [match.distance for match in matches] == sorted(match.distance for match in matches)


def test_same_dimension_models_never_mix_in_a_query(db: TestDatabase, database: Database) -> None:
    _register(db, OTHER_MODEL, DIMENSIONS)
    only_other = _paper(database, 1)
    _run(
        database,
        FakeEmbeddingProvider(DIMENSIONS, model_id=OTHER_MODEL),
        config=_config(model_id=OTHER_MODEL),
    )
    with database.transaction() as connection:
        stored = find_paper_embedding(SYSTEM, connection, only_other, model_id=OTHER_MODEL)
        assert stored is not None
        # MODEL 一筆向量都沒有：同維度的另一個模型的向量不會被查到。
        assert (
            search_paper_vectors(
                SYSTEM, connection, model_id=MODEL, vector=list(stored.vector), limit=10
            )
            == []
        )


def test_excluded_papers_are_left_out_of_vector_queries(
    db: TestDatabase, database: Database
) -> None:
    ids = [_paper(database, n) for n in (1, 2)]
    _run(database)
    execute(db, "UPDATE paper SET excluded_reason = 'editors_comments' WHERE id = %s", ids[1])
    with database.transaction() as connection:
        stored = find_paper_embedding(SYSTEM, connection, ids[1], model_id=MODEL)
        assert stored is not None
        matches = search_paper_vectors(
            SYSTEM, connection, model_id=MODEL, vector=list(stored.vector), limit=10
        )
    assert [match.paper_id for match in matches] == [ids[0]]


def test_vector_query_requires_model_id(database: Database) -> None:
    with database.transaction() as connection, pytest.raises(TypeError, match="model_id"):
        search_paper_vectors(SYSTEM, connection, model_id="", vector=[0.1] * 8, limit=1)
