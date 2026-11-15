"""`radar embeddings backfill` 對真的 PostgreSQL（`CliRunner`＋環境變數；Story 2.8）。

- `--estimate`：只讀資料庫，不需要 Cloudflare 的 token，不送任何請求、不寫任何列。
- `--limit N`：做 N 篇，印出這次的篇數、估計 token、照這次的平均推算全庫還要多少 token 與天數。
- 缺 account id／token、Cloudflare 回 401：退出碼 1，輸出不含 token 與 account id。
- 重跑：做好的論文不再送 Workers AI（替身數呼叫次數）；有一批失敗：印出報告、退出碼 1。
- `--estimate` 不能配 `--limit`，也會核對模型有沒有登記。

provider 換成假的（`FakeEmbeddingProvider`），或真的 adapter 接 `httpx2.MockTransport`（假回應
不是真的錄的）；不連網路。account id 與 token 是測試自己編的假值。
"""

import logging
import shutil
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx2
import pytest
from typer.testing import CliRunner, Result

import radar.entrypoints.cli as cli
from radar.adapters.fake import FakeEmbeddingProvider
from radar.adapters.workers_ai import WorkersAiEmbeddingProvider
from radar.domain.embeddings import (
    EmbeddingConfig,
    EmbeddingRequestError,
    EmbeddingTransientError,
)
from radar.domain.policy import CloudSafeText, cloud_text
from radar.domain.settings import Settings
from radar.repo.db import Database
from tests.database import ROOT, TestDatabase, migrate
from tests.llm_support import create_paper, execute

runner = CliRunner()
MODEL = "fake-embedding"
FAKE_ACCOUNT = "0123456789abcdef0123456789abcdef"
FAKE_TOKEN = "fake-token-for-tests"


class _ClosingFake(FakeEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__(8, model_id=MODEL)
        self.closed = False

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        return super().embed_documents(texts)

    def __enter__(self) -> _ClosingFake:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    database = Database(fresh_database.app_url, connect_timeout=5, pool_size=1)
    for n in (1, 2, 3):
        create_paper(
            database,
            doi=f"10.1/p{n}",
            openalex_id=f"W{n}",
            title=f"Paper {n} on platform adoption",
            abstract=f"Abstract {n}: teams adopt platforms.",
        )
    return fresh_database


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    custom = tmp_path / "config"
    shutil.copytree(ROOT / "config", custom)
    return custom


@pytest.fixture
def env(db: TestDatabase, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATABASE_URL", db.app_url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("RADAR_CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("RADAR_CLOUDFLARE_API_TOKEN", raising=False)
    return config_dir


def _fake_model(db: TestDatabase, config_dir: Path) -> None:
    execute(
        db,
        "INSERT INTO embedding_model (model_id, provider, dimensions) VALUES (%s, 'fake', 8)",
        MODEL,
    )
    path = config_dir / "embeddings.yaml"
    text = path.read_text(encoding="utf-8")
    text = text.replace('model_id: "@cf/qwen/qwen3-embedding-0.6b"', f'model_id: "{MODEL}"')
    path.write_text(text.replace("dimensions: 1024", "dimensions: 8"), encoding="utf-8")


def _backfill(*args: str) -> Result:
    """執行 `radar embeddings backfill`；結束後把 root logger 的 handler 換回原本的。"""
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        return runner.invoke(cli.app, ["embeddings", "backfill", *args])
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)


def _lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if not line.startswith("{")]


def test_estimate_reads_only_and_needs_no_token(db: TestDatabase, env: Path) -> None:
    result = _backfill("--estimate")

    assert result.exit_code == 0, result.output
    lines = _lines(result.output)
    assert lines[0] == (
        "向量化估算（模型 @cf/qwen/qwen3-embedding-0.6b；只用本地資料，沒有送任何請求）"
    )
    assert "還要建或重算向量的論文：3 篇" in lines
    assert any(line.startswith("估計共 ") for line in lines)
    assert any("10,000 neurons" in line and "2026-11-15 查證" in line for line in lines)
    assert any(line.startswith("推算要 ") and line.endswith(" 天") for line in lines)
    assert execute(db, "SELECT count(*) FROM api_call") == [(0,)]
    assert execute(db, "SELECT count(*) FROM derivation_run") == [(0,)]


def test_limit_runs_n_papers_and_projects_the_rest(
    db: TestDatabase, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_model(db, env)
    provider = _ClosingFake()
    monkeypatch.setattr(cli, "build_embedding_provider", lambda *_: provider)

    result = _backfill("--limit", "2")

    assert result.exit_code == 0, result.output
    lines = _lines(result.output)
    assert lines[0] == "向量化結束（模型 fake-embedding）：到了這次指定的篇數（--limit）"
    assert "這次寫入向量：2 篇（送出 1 次請求）" in lines
    assert any(line.startswith("這次估計用了：") for line in lines)
    assert any(line.startswith("平均每篇約 ") for line in lines)
    assert "還沒有向量（或要重算）的論文：1 篇" in lines
    assert any(line.startswith("照這次的平均推算還要：") for line in lines)
    assert any(line.startswith("推算還要 ") for line in lines)
    assert provider.closed
    assert execute(db, "SELECT count(*) FROM embedding") == [(2,)]

    # 再跑一次：只做剩下的一篇，做過的不重送。
    again = _backfill()
    assert again.exit_code == 0, again.output
    assert "這次寫入向量：1 篇（送出 1 次請求）" in _lines(again.output)
    assert execute(db, "SELECT count(*) FROM embedding") == [(3,)]


def test_run_without_cloudflare_settings_fails_with_names_only(db: TestDatabase, env: Path) -> None:
    result = _backfill("--limit", "1")

    assert result.exit_code == 1
    assert "缺少環境變數 RADAR_CLOUDFLARE_ACCOUNT_ID" in result.output
    assert "缺少環境變數 RADAR_CLOUDFLARE_API_TOKEN" in result.output
    assert execute(db, "SELECT count(*) FROM derivation_run") == [(0,)]


def test_cloudflare_401_exits_1_without_printing_secrets(
    db: TestDatabase, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RADAR_CLOUDFLARE_ACCOUNT_ID", FAKE_ACCOUNT)
    monkeypatch.setenv("RADAR_CLOUDFLARE_API_TOKEN", FAKE_TOKEN)
    seen: list[httpx2.Request] = []

    def unauthorized(request: httpx2.Request) -> httpx2.Response:
        # 假回應（不是真的錄的，形狀照 REST 指南的信封）。
        seen.append(request)
        return httpx2.Response(
            401, json={"result": None, "success": False, "errors": [{"code": 10000}]}
        )

    def build(settings: Settings, config: EmbeddingConfig) -> Any:
        assert settings.cloudflare_account_id is not None
        assert settings.cloudflare_api_token is not None
        return WorkersAiEmbeddingProvider(
            settings.cloudflare_account_id,
            settings.cloudflare_api_token,
            config.api,
            model_id=config.model_id,
            dimensions=config.dimensions,
            transport=httpx2.MockTransport(unauthorized),
        )

    monkeypatch.setattr(cli, "build_embedding_provider", build)

    result = _backfill("--limit", "1")

    assert result.exit_code == 1
    assert len(seen) == 1
    assert "Cloudflare 拒絕請求（HTTP 401）" in result.output
    assert "Workers AI 的 Read 與 Edit 權限" in result.output
    assert FAKE_TOKEN not in result.output
    assert FAKE_ACCOUNT not in result.output
    assert execute(
        db, "SELECT status, failure_reason FROM derivation_run WHERE task = 'embed'"
    ) == [("failed", "embed_configuration")]


def test_rerunning_backfill_skips_done_papers_without_calling_workers_ai(
    db: TestDatabase, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kevin 手動跑的回填指令重跑時：做好的論文不再送 Workers AI（替身數呼叫次數）。
    _fake_model(db, env)
    first = _ClosingFake()
    monkeypatch.setattr(cli, "build_embedding_provider", lambda *_: first)
    assert _backfill().exit_code == 0
    assert len(first.document_calls) == 1
    before = execute(db, "SELECT item_id, input_sha256, run_id, vector::text FROM embedding")

    second = _ClosingFake()
    monkeypatch.setattr(cli, "build_embedding_provider", lambda *_: second)
    result = _backfill()

    assert result.exit_code == 0, result.output
    assert second.document_calls == []
    lines = _lines(result.output)
    assert lines[0] == "向量化結束（模型 fake-embedding）：要做的論文都做完了"
    assert "這次寫入向量：0 篇（送出 0 次請求）" in lines
    assert "還沒有向量（或要重算）的論文：0 篇" in lines
    assert execute(db, "SELECT item_id, input_sha256, run_id, vector::text FROM embedding") == (
        before
    )
    assert execute(db, "SELECT count(*) FROM api_call") == [(1,)]


class _TransientFake(_ClosingFake):
    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        super().embed_documents(texts)
        raise EmbeddingTransientError("重試用完", http_status=503)


def test_failed_batch_prints_the_report_and_exits_1(
    db: TestDatabase, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_model(db, env)
    monkeypatch.setattr(cli, "build_embedding_provider", lambda *_: _TransientFake())

    result = _backfill()

    assert result.exit_code == 1
    assert _lines(result.output)[0] == "向量化結束（模型 fake-embedding）：失敗（embed_transient）"
    assert execute(db, "SELECT count(*) FROM embedding") == [(0,)]


def test_estimate_with_limit_is_rejected(db: TestDatabase, env: Path) -> None:
    result = _backfill("--estimate", "--limit", "5")

    assert result.exit_code == 1
    assert "--estimate 只估算、不送請求，不能和 --limit 一起用" in result.output


def test_estimate_checks_the_model_is_registered(db: TestDatabase, env: Path) -> None:
    path = env / "embeddings.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace('model_id: "@cf/qwen/qwen3-embedding-0.6b"', 'model_id: "unregistered"'),
        encoding="utf-8",
    )

    result = _backfill("--estimate")

    assert result.exit_code == 1
    assert "向量模型的設定對不上，沒有送任何請求：" in result.output
    assert "沒有登記在 embedding_model" in result.output


def test_projection_failure_after_writing_still_reports_what_was_done(
    db: TestDatabase, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_model(db, env)
    monkeypatch.setattr(cli, "build_embedding_provider", lambda *_: _ClosingFake())

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("db went away")

    monkeypatch.setattr(cli, "estimate_embedding_backlog", broken)

    result = _backfill()

    assert result.exit_code == 1
    assert "這次寫入向量：3 篇（送出 1 次請求）" in result.output
    assert "推算剩下的篇數失敗（RuntimeError）" in result.output
    assert "db went away" not in result.output
    assert execute(db, "SELECT count(*) FROM embedding") == [(3,)]


class _RejectsPaperOne(_ClosingFake):
    """送出的這一批裡有論文 1 就回請求不合法（400），其他照常（審查後的修正，Codex 中 4）。"""

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        vectors = super().embed_documents(texts)
        if any("Paper 1 " in cloud_text(text) for text in texts):
            raise EmbeddingRequestError("HTTP 400", http_status=400)
        return vectors


def test_single_rejected_paper_finishes_the_rest_but_exits_1(
    db: TestDatabase, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 被拒的那一篇略過、其他照做（停下的原因是「做完了」），但這次有失敗：結束碼 1、報告寫出來。
    _fake_model(db, env)
    monkeypatch.setattr(cli, "build_embedding_provider", lambda *_: _RejectsPaperOne())

    result = _backfill()

    assert result.exit_code == 1
    lines = _lines(result.output)
    assert lines[0] == "向量化結束（模型 fake-embedding）：要做的論文都做完了"
    assert lines[1] == "這次有 1 篇失敗（embed_request_invalid），結束碼 1"
    assert execute(db, "SELECT count(*) FROM embedding") == [(2,)]
