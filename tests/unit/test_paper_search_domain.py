"""找論文清單 API 的搜尋與找相似論文（Story 4.1 PR B）的 domain：下一頁的位置（名次位移）、一頁的
形狀、錯誤代碼的 HTTP 狀態、api 啟動時要有查詢向量自己的時限。

- 檢索的位置只收自己產生的寫法，而且要是同一種檢索（搜尋／找相似論文）；清單的 keyset 位置一定不收，
  反過來清單也不收檢索的位置（互相誤用是 422，不是 500）。
"""

import base64
import shutil
import signal
import uuid
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
import uvicorn

import radar.adapters.workers_ai as workers_ai
import radar.entrypoints.api.main as api_main
import radar.entrypoints.api.paper_list as paper_list_routes
from radar.domain.errors import ErrorCode
from radar.domain.paper_list import (
    PaperListCursor,
    decode_paper_list_cursor,
    encode_paper_list_cursor,
)
from radar.domain.policy import CloudPolicy, ContentSource, clear_for_cloud
from radar.domain.retrieval import (
    MAX_RETRIEVAL_OFFSET,
    PaperSearchPage,
    RetrievalCursor,
    RetrievalMode,
    decode_retrieval_cursor,
    encode_retrieval_cursor,
)
from radar.domain.snapshot import Snapshot, SnapshotStatus
from radar.entrypoints.api.openapi import build_schema_app
from radar.entrypoints.api.paper_list import PaperSearchSetup
from radar.entrypoints.api.problems import PROBLEM_STATUS
from radar.entrypoints.bootstrap import Database, ReadinessProbes

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mode", list(RetrievalMode))
@pytest.mark.parametrize("offset", [1, 20, 199, MAX_RETRIEVAL_OFFSET])
def test_retrieval_cursor_round_trips(mode: RetrievalMode, offset: int) -> None:
    cursor = RetrievalCursor(mode=mode, offset=offset)
    encoded = encode_retrieval_cursor(cursor)

    assert decode_retrieval_cursor(encoded, mode=mode) == cursor
    assert "=" not in encoded


def test_search_cursor_is_not_a_similar_cursor_and_the_reverse() -> None:
    search = encode_retrieval_cursor(RetrievalCursor(mode=RetrievalMode.SEARCH, offset=20))
    similar = encode_retrieval_cursor(RetrievalCursor(mode=RetrievalMode.SIMILAR, offset=20))

    with pytest.raises(ValueError, match="下一頁的位置"):
        decode_retrieval_cursor(search, mode=RetrievalMode.SIMILAR)
    with pytest.raises(ValueError, match="下一頁的位置"):
        decode_retrieval_cursor(similar, mode=RetrievalMode.SEARCH)


@pytest.mark.parametrize("year", [2020, None])
def test_list_cursor_and_retrieval_cursor_never_decode_as_each_other(year: int | None) -> None:
    listed = encode_paper_list_cursor(PaperListCursor(year=year, paper_id=uuid.uuid4()))
    ranked = encode_retrieval_cursor(RetrievalCursor(mode=RetrievalMode.SEARCH, offset=20))

    for mode in RetrievalMode:
        with pytest.raises(ValueError, match="下一頁的位置"):
            decode_retrieval_cursor(listed, mode=mode)
    with pytest.raises(ValueError, match="下一頁的位置"):
        decode_paper_list_cursor(ranked)


def _b64(plain: str) -> str:
    return base64.urlsafe_b64encode(plain.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-cursor",
        _b64("search:0"),  # 第一頁沒有位置
        _b64("search:-1"),
        _b64(f"search:{MAX_RETRIEVAL_OFFSET + 1}"),
        _b64("search:01"),  # 重新編碼不一樣
        _b64("search:+5"),
        _b64("search: 5"),
        _b64("search:5:5"),
        _b64("search:٥"),  # 不是 ASCII 的數字
        _b64("search:1e3"),
        _b64("other:5"),
        _b64("SEARCH:5"),
        _b64("search:5") + "==",
        "A" * 41,
    ],
)
def test_bad_retrieval_cursors_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="下一頁的位置"):
        decode_retrieval_cursor(value, mode=RetrievalMode.SEARCH)


@pytest.mark.parametrize("offset", [0, -1, MAX_RETRIEVAL_OFFSET + 1, True, 1.0])
def test_retrieval_cursor_offset_must_be_a_positive_int(offset: Any) -> None:
    with pytest.raises(ValueError, match="offset"):
        RetrievalCursor(mode=RetrievalMode.SEARCH, offset=offset)


def test_paper_search_page_checks_total_and_snapshot() -> None:
    created = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
    published = Snapshot(
        id=uuid.uuid4(),
        number=1,
        weekly_run_id=uuid.uuid4(),
        status=SnapshotStatus.PUBLISHED,
        as_of=datetime(2026, 11, 16, 2, 30, tzinfo=UTC),
        created_at=created,
    )
    assert PaperSearchPage(rows=(), next_cursor=None, total=0, snapshot=published).total == 0
    with pytest.raises(ValueError, match="total"):
        PaperSearchPage(rows=(), next_cursor=None, total=-1, snapshot=None)
    building = Snapshot(
        id=uuid.uuid4(),
        number=2,
        weekly_run_id=uuid.uuid4(),
        status=SnapshotStatus.BUILDING,
        as_of=None,
        created_at=created,
    )
    with pytest.raises(ValueError, match="快照"):
        PaperSearchPage(rows=(), next_cursor=None, total=0, snapshot=building)


def test_search_errors_are_503_with_their_own_codes() -> None:
    assert PROBLEM_STATUS[ErrorCode.SEARCH_QUOTA_EXHAUSTED] == 503
    assert PROBLEM_STATUS[ErrorCode.SEARCH_EMBEDDING_UNAVAILABLE] == 503
    assert ErrorCode.SEARCH_QUOTA_EXHAUSTED.value == "search_quota_exhausted"
    assert ErrorCode.SEARCH_EMBEDDING_UNAVAILABLE.value == "search_embedding_unavailable"


class _ShouldNotBuildError(Exception):
    pass


def _fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
    raise _ShouldNotBuildError


def test_api_does_not_start_without_short_query_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # embeddings.yaml 拿掉 query_request：背景向量化照樣能讀（worker 不用它），api 不啟動。
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    path = target / "embeddings.yaml"
    text = path.read_text(encoding="utf-8")
    block = "query_request:\n  timeout_seconds: 2\n  total_timeout_seconds: 3\n  attempts: 2\n"
    assert block in text
    path.write_text(text.replace(block, ""), encoding="utf-8")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(target))
    monkeypatch.setenv("DATABASE_URL", "postgresql://radar_app@db:5432/radar")
    monkeypatch.setattr(api_main, "build_readiness_probes", _fail_if_called)

    assert api_main.main() == 1

    assert "embeddings.yaml：query_request" in capsys.readouterr().out


def test_api_does_not_start_with_a_broken_retrieval_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # load_retrieval_config 接到 api 的啟動流程：retrieval.yaml 寫錯就不啟動。
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    path = target / "retrieval.yaml"
    text = path.read_text(encoding="utf-8")
    assert "rrf_k: 60" in text
    path.write_text(text.replace("rrf_k: 60", "rrf_k: 0"), encoding="utf-8")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(target))
    monkeypatch.setenv("DATABASE_URL", "postgresql://radar_app@db:5432/radar")
    monkeypatch.setattr(api_main, "build_readiness_probes", _fail_if_called)

    assert api_main.main() == 1

    assert "retrieval.yaml：rrf_k" in capsys.readouterr().out


# --- api 的正式組裝：Cloudflare 金鑰、請求日誌（審查 M6、M7）-----------------------------------
#
# 照正式的 `main()` 跑到最後（設定、組路由、uvicorn 的設定），只換掉三樣：就緒檢查的連線池（不連資料
# 庫）、uvicorn 的 Server（不開連接埠，記下設定）、訊號處理（不動測試行程的訊號）。搜尋的設定從
# `build_paper_list_routes` 的引數記下來（呼叫照樣交給真的函式）。

FAKE_ACCOUNT = "0123456789abcdef0123456789abcdef"
FAKE_TOKEN = "fake-token-for-tests"
KEY_NAMES = ("RADAR_CLOUDFLARE_ACCOUNT_ID", "RADAR_CLOUDFLARE_API_TOKEN")


class _Assembled:
    def __init__(self) -> None:
        self.searches: list[PaperSearchSetup] = []
        self.servers: list[uvicorn.Config] = []


def _run_api_main(monkeypatch: pytest.MonkeyPatch, keys: dict[str, str]) -> tuple[int, _Assembled]:
    assembled = _Assembled()
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://radar_app@db:5432/radar")
    for name in KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in keys.items():
        monkeypatch.setenv(name, value)

    def probes(_settings: Any, *, pool_size: int) -> ReadinessProbes:
        return ReadinessProbes(
            ping=lambda: True, schema_state=_fail_if_called, database=cast(Database, object())
        )

    real_build = paper_list_routes.build_paper_list_routes

    def build_paper_list_routes(*args: Any) -> Any:
        assembled.searches.append(args[-1])
        return real_build(*args)

    class _Server:
        def __init__(self, config: uvicorn.Config) -> None:
            assembled.servers.append(config)

        def run(self) -> None:
            return None

    monkeypatch.setattr(api_main, "build_readiness_probes", probes)
    monkeypatch.setattr(api_main, "build_paper_list_routes", build_paper_list_routes)
    monkeypatch.setattr(uvicorn, "Server", _Server)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    return api_main.main(), assembled


def test_api_with_both_keys_really_queries_workers_ai_with_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 兩個金鑰都有：正式組裝出來的 provider 真的拿這兩個值送查詢（MockTransport，不連外）。
    seen: list[httpx2.Request] = []
    vector = [0.5] + [0.0] * 1023

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={"success": True, "errors": [], "result": {"shape": [1, 1024], "data": [vector]}},
        )

    monkeypatch.setattr(
        workers_ai,
        "WorkersAiEmbeddingProvider",
        partial(workers_ai.WorkersAiEmbeddingProvider, transport=httpx2.MockTransport(handler)),
    )
    code, assembled = _run_api_main(
        monkeypatch, {KEY_NAMES[0]: FAKE_ACCOUNT, KEY_NAMES[1]: FAKE_TOKEN}
    )

    assert code == 0
    (search,) = assembled.searches
    assert search.provider is not None
    query = clear_for_cloud("trust", ContentSource.USER_QUERY, None, CloudPolicy())
    assert search.provider.embed_query(query) == vector
    (request,) = seen
    assert f"/accounts/{FAKE_ACCOUNT}/ai/run/" in request.url.path
    assert request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


@pytest.mark.parametrize("given", KEY_NAMES)
def test_api_with_only_one_key_does_not_start(
    given: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 只有一個金鑰是設定寫錯：不啟動（不建連線池），訊息只有缺的變數名稱、不含值。
    (missing,) = [name for name in KEY_NAMES if name != given]
    value = FAKE_ACCOUNT if given == KEY_NAMES[0] else FAKE_TOKEN
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://radar_app@db:5432/radar")
    monkeypatch.delenv(missing, raising=False)
    monkeypatch.setenv(given, value)
    monkeypatch.setattr(api_main, "build_readiness_probes", _fail_if_called)

    assert api_main.main() == 1

    out = capsys.readouterr().out
    assert f"缺少環境變數 {missing}" in out
    assert value not in out


def test_api_without_keys_starts_warns_once_and_search_has_no_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, assembled = _run_api_main(monkeypatch, {})

    assert code == 0
    (search,) = assembled.searches
    assert search.provider is None
    out = capsys.readouterr().out
    warnings = [line for line in out.splitlines() if "沒有 Cloudflare 金鑰" in line]
    assert len(warnings) == 1


def test_api_server_writes_no_access_log(monkeypatch: pytest.MonkeyPatch) -> None:
    # 審查 M7：`q` 在網址裡。uvicorn 的請求日誌（access log）會印完整網址含查詢字串，所以正式的
    # uvicorn 設定一定關掉它；請求日誌只由 app 的中介層記（只記路徑）。也不用 uvicorn 自己的日誌設定
    # （`log_config=None`，交給 `configure_logging`）。
    code, assembled = _run_api_main(monkeypatch, {})

    assert code == 0
    (config,) = assembled.servers
    assert config.access_log is False
    assert config.log_config is None


def test_openapi_declares_the_search_errors_on_the_paper_list() -> None:
    # 審查 M9：404、503 宣告在端點上，內容型別和實際一樣是 problem+json（前端的型別才分得出來）。
    schema = build_schema_app().openapi()
    responses = schema["paths"]["/api/v1/papers"]["get"]["responses"]

    for status in ("404", "503"):
        assert responses[status]["content"] == {
            "application/problem+json": {"schema": {"$ref": "#/components/schemas/ProblemDetails"}}
        }
    assert "ProblemDetails" in schema["components"]["schemas"]
