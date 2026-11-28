"""搜尋的評估題組對真的 PostgreSQL（Story 4.4、FR-26、FR-27）：`radar eval import-queries` 與
`radar eval search` 的整條流程（`CliRunner`＋環境變數），以及 `retrieve` 重用查詢向量。

- 匯入：範例檔（`tests/fixtures/eval/search_queries_sample.csv`，自己編的）從標準輸入或 `--file`
  讀；DOI 對不到論文的正解與起點印出來並跳過、不中斷；整份取代原本的題目；題目檔有問題（例如不是
  UTF-8）時什麼都不寫。
- 跑題：`tracked` 範圍、系統的 viewer；兩個相似度下限各自印「只用全文檢索腿／只用向量腿／融合」三組
  的命中率、MRR、「找不到相關的論文」的題數與每題的命中名次（三組分開）；`similar` 題只有向量腿。
  每題的查詢向量只算一次（兩個下限共用，假 provider 只被呼叫題數次、`api_call` 也只記題數筆）；
  不在追蹤範圍的論文不出現；結果存一筆 `eval_run`。
- 連不上 Workers AI：退出碼 1、不存 `eval_run`。還沒有題目：退出碼 1。起點被排除：那一題跳過。
- 日誌不記查詢內容。
- `retrieve(query_vector=…)`：不呼叫 provider、結果和一般搜尋一樣；和 `query` 或模型對不上就丟錯。

向量是測試自己編的 8 維數字（模型 `fake-embedding`，沒有 HNSW 索引，精確比對）；provider 是假的，
不連網路。論文標題、DOI（`10.9999/fake.*`）、查詢都是測試自己編的。
"""

import dataclasses
import logging
import math
import shutil
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
import sqlalchemy.exc
from typer.testing import CliRunner, Result

import radar.entrypoints.cli as cli
import radar.entrypoints.cli.evaluation as cli_evaluation
from radar.domain.embeddings import EmbeddingTransientError
from radar.domain.evaluation import (
    EVAL_QUERIES_LOCK_KEY,
    EvalLeg,
    EvalQuerySet,
    EvalQuestion,
    EvalRelevant,
    EvalSkipReason,
    ParsedEvalQueries,
    parse_eval_queries_csv,
)
from radar.domain.paper_list import PaperListFilters
from radar.domain.papers import PaperOrigin, ParsedPaper
from radar.domain.policy import CloudPolicy, CloudSafeText, cloud_text
from radar.domain.retrieval import RetrievalScope
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.evaluation import create_eval_queries
from radar.repo.papers import upsert_by_external_id
from radar.services.evaluation import import_eval_queries, run_search_eval
from radar.services.retrieval import (
    RetrievalSetup,
    SearchQueryVector,
    embed_retrieval_query,
    retrieve,
)
from tests.database import ROOT, TestDatabase, migrate
from tests.integration.test_embeddings import CONFIG, MODEL
from tests.integration.test_paper_list_api import FETCHED_AT, MISQ, SCOPE, make_snapshot
from tests.integration.test_retrieval import FAR, QUERY, RETRIEVAL, near, put_vector
from tests.llm_support import execute

runner = CliRunner()
SAMPLE = ROOT / "tests" / "fixtures" / "eval" / "search_queries_sample.csv"
NOW = datetime(2026, 11, 28, 2, 0, tzinfo=UTC)
NUDGE = (0.5, math.sqrt(0.75), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
# 報告裡一題一組的結果（名次、命中、倒數名次）。
FIRST = "第 1 名（命中 1，倒數名次 1.000）"
SECOND = "第 2 名（命中 1，倒數名次 0.500）"
MISS = "沒命中（命中 0，倒數名次 0）"
NONE = "沒有結果（命中 0，倒數名次 0）"
# 範例檔每句查詢的假向量（假 provider 照這張表回）。
QUERY_VECTORS: Mapping[str, Sequence[float]] = {
    "平台採用": QUERY,
    "online reviews trust": FAR,
    "數位推力": NUDGE,
    "區塊鏈治理": FAR,
}


class FakeQueries:
    """假的 `EmbeddingProvider`：`embed_query` 照 `QUERY_VECTORS` 回向量、記下每次呼叫；可以當
    context manager 用（CLI 用 `with build_embedding_provider(...)`）。`fail` 時丟暫時性錯誤。"""

    model_id = MODEL

    def __init__(self, *, fail: bool = False) -> None:
        self.queries: list[str] = []
        self.fail = fail
        self.closed = False

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        raise AssertionError("評估不該呼叫 embed_documents")

    def embed_query(self, text: CloudSafeText) -> list[float]:
        value = cloud_text(text)
        self.queries.append(value)
        if self.fail:
            raise EmbeddingTransientError("連不上（測試）")
        return list(QUERY_VECTORS[value])

    def __enter__(self) -> FakeQueries:
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
    execute(
        fresh_database,
        "INSERT INTO embedding_model (model_id, provider, dimensions) VALUES (%s, 'fake', 8)",
        MODEL,
    )
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


def make_paper(
    database: Database,
    db: TestDatabase,
    number: int,
    title: str,
    vector: Sequence[float],
    *,
    snapshot_id: uuid.UUID | None,
) -> uuid.UUID:
    """一篇追蹤期刊的假論文（DOI `10.9999/fake.000N`）＋它的假向量；`snapshot_id` 是 None 時沒有進
    快照（不算在 `tracked` 範圍）。"""
    with database.transaction() as connection:
        result = upsert_by_external_id(
            connection,
            ParsedPaper.model_validate(
                {
                    "source": "openalex",
                    "fetched_at": FETCHED_AT,
                    "doi": f"10.9999/fake.{number:04d}",
                    "openalex_id": f"W9{number:08d}",
                    "title": title,
                    "journal_issn_l": MISQ,
                    "publication_year": 2021,
                    "abstract": f"Made-up abstract {number}.",
                    "abstract_source": "openalex",
                }
            ),
            origin=PaperOrigin.TRACKED,
            excluded_reason=None,
            scope=SCOPE,
        )
    if snapshot_id is not None:
        execute(
            db,
            "UPDATE paper SET first_snapshot_id = %s WHERE id = %s",
            snapshot_id,
            result.paper_id,
        )
    put_vector(db, result.paper_id, vector)
    return result.paper_id


@pytest.fixture
def papers(database: Database, db: TestDatabase) -> dict[int, uuid.UUID]:
    """範例檔用到的四篇（fake.0001～0004）與一篇不在追蹤範圍、向量和查詢一模一樣的論文（fake.0005）。

    相似度（和 `QUERY`／`FAR`／`NUDGE`）：1 是 0.999／0.05／0.54，2 是 0.6／0.8／0.99，
    3 是 0.5／0.87／1，4 是 0.995／0.1／0.58。只有 2 的標題有 online、reviews、trust。"""
    snapshot = make_snapshot(db, 1)
    made = {
        1: make_paper(
            database, db, 1, "Made-up platform adoption study", near(1), snapshot_id=snapshot
        ),
        2: make_paper(
            database,
            db,
            2,
            "Made-up trust in online reviews",
            (0.6, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            snapshot_id=snapshot,
        ),
        3: make_paper(
            database, db, 3, "Made-up digital nudges survey", NUDGE, snapshot_id=snapshot
        ),
        4: make_paper(
            database, db, 4, "Made-up cloud migration case", near(2), snapshot_id=snapshot
        ),
        5: make_paper(database, db, 5, "Made-up uncounted paper", QUERY, snapshot_id=None),
    }
    return made


@pytest.fixture
def env(db: TestDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", config_dir)
    path = config_dir / "embeddings.yaml"
    text = path.read_text(encoding="utf-8")
    text = text.replace('model_id: "@cf/qwen/qwen3-embedding-0.6b"', f'model_id: "{MODEL}"')
    path.write_text(text.replace("dimensions: 1024", "dimensions: 8"), encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", db.app_url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("RADAR_CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("RADAR_CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setattr(cli_evaluation, "_utc_now", lambda: NOW)
    return config_dir


def _invoke(*args: str, input: bytes | None = None) -> Result:
    """執行 `radar eval …`；結束後把 root logger 的 handler 換回原本的。"""
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        return runner.invoke(cli.app, ["eval", *args], input=input)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)


def _lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if not line.startswith("{")]


def _sections(lines: list[str]) -> dict[str, list[str]]:
    """報告依「== 相似度下限 X ==」切段（第一段是開頭的設定）。"""
    sections: dict[str, list[str]] = {"head": []}
    current = "head"
    for line in lines:
        if line.startswith("== 相似度下限 "):
            current = line.removeprefix("== 相似度下限 ").removesuffix(" ==")
            sections[current] = []
        else:
            sections[current].append(line)
    return sections


def _provider(monkeypatch: pytest.MonkeyPatch, provider: FakeQueries) -> None:
    monkeypatch.setattr(cli_evaluation, "build_embedding_provider", lambda *_: provider)


def _questions(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db,
        "SELECT q.query_set, q.query_key, q.query_text, r.relevant_doi, r.note FROM eval_query AS q"
        " JOIN eval_query_relevant AS r ON r.eval_query_id = q.id"
        " ORDER BY q.query_set, q.query_key, r.relevant_doi",
    )


# --- 匯入 -------------------------------------------------------------------------------------


def test_import_from_stdin_skips_unknown_dois_and_keeps_going(
    db: TestDatabase, env: Path, papers: dict[int, uuid.UUID]
) -> None:
    result = _invoke("import-queries", input=SAMPLE.read_bytes())

    assert result.exit_code == 0, result.output
    lines = _lines(result.output)
    assert lines == [
        "已匯入評估題目：search 4 題、similar 1 題，正解共 6 篇（整份取代原本的 0 題）",
        "跳過空列 1 列",
        "跳過的（2 筆）：",
        "  第 7 列 search q03 10.9999/fake.0099：正解的 DOI 對不到論文，這一篇跳過",
        "  第 10 列 similar s02 10.9999/fake.0098：起點論文的 DOI 對不到論文，整題跳過",
    ]
    assert _questions(db) == [
        ("search", "q01", "平台採用", "10.9999/fake.0001", "測試資料：自己編的題目"),
        ("search", "q01", "平台採用", "10.9999/fake.0002", None),
        ("search", "q02", "online reviews trust", "10.9999/fake.0002", None),
        ("search", "q03", "數位推力", "10.9999/fake.0003", None),
        ("search", "q04", "區塊鏈治理", "10.9999/fake.0001", None),
        ("similar", "s01", "10.9999/fake.0001", "10.9999/fake.0002", None),
    ]


def test_import_replaces_every_question_and_reads_a_file(
    db: TestDatabase, env: Path, papers: dict[int, uuid.UUID], tmp_path: Path
) -> None:
    assert _invoke("import-queries", input=SAMPLE.read_bytes()).exit_code == 0
    smaller = tmp_path / "題目.csv"
    smaller.write_text(
        "set,query_id,query,relevant_doi\nsearch,q01,改過的查詢,10.9999/fake.0003\n",
        encoding="utf-8",
    )

    result = _invoke("import-queries", "--file", str(smaller))

    assert result.exit_code == 0, result.output
    assert _lines(result.output)[0] == (
        "已匯入評估題目：search 1 題、similar 0 題，正解共 1 篇（整份取代原本的 5 題）"
    )
    assert _questions(db) == [("search", "q01", "改過的查詢", "10.9999/fake.0003", None)]


def test_bad_file_writes_nothing(db: TestDatabase, env: Path, papers: dict[int, uuid.UUID]) -> None:
    assert _invoke("import-queries", input=SAMPLE.read_bytes()).exit_code == 0
    before = _questions(db)
    big5 = "set,query_id,query,relevant_doi\nsearch,q09,平台,10.9999/fake.0001\n".encode("big5")

    result = _invoke("import-queries", input=big5)

    assert result.exit_code == 1
    assert "題目檔有 1 個問題，什麼都沒有寫：" in result.output
    assert "題目檔不是 UTF-8" in result.output
    assert _questions(db) == before

    broken = _invoke("import-queries", input=b"set,query_id\nsearch,q1\n")

    assert broken.exit_code == 1
    assert "少了欄位 query" in broken.output
    assert _questions(db) == before


# --- 跑題 -------------------------------------------------------------------------------------


def test_eval_search_reports_three_groups_per_floor_and_saves_one_run(
    db: TestDatabase,
    env: Path,
    papers: dict[int, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _invoke("import-queries", input=SAMPLE.read_bytes()).exit_code == 0
    provider = FakeQueries()
    _provider(monkeypatch, provider)

    result = _invoke("search", "--min-similarity", "0.9", "--min-similarity", "0.2")

    assert result.exit_code == 0, result.output
    sections = _sections(_lines(result.output))
    [(run_id,)] = execute(db, "SELECT id FROM eval_run")
    assert sections["head"][:5] == [
        f"搜尋評估（eval_run {run_id}）",
        f"向量模型：{MODEL}",
        "快照：第 1 號",
        "相似度下限：0.2、0.9",
        "題數：5（範圍 tracked；看前 10 名）",
    ]
    # 範例檔只有 3 題中文搜尋題（規格要至少 10 題）：照跑、印警告（審查 M3）。
    assert "注意：中文搜尋題只有 3 題（規格要至少 10 題），這次的數字只能參考" in sections["head"]
    low = sections["0.2"]
    assert low[: low.index("[找相似論文（similar，只有向量腿）] 1 題")] == [
        "[搜尋（search）] 4 題",
        "  只用全文檢索腿：命中 1/4（25.0%），MRR 0.250，找不到相關的論文 3 題",
        "  只用向量腿：命中 3/4（75.0%），MRR 0.625，找不到相關的論文 0 題",
        "  融合：命中 3/4（75.0%），MRR 0.750，找不到相關的論文 0 題",
        "  每題：",
        # 中文查詢全文檢索腿找不到東西；不在追蹤範圍、向量一模一樣的 fake.0005 沒有擠掉第 1 名。
        # 每題每組都印命中（0／1）與倒數名次（審查 M2）。
        f"    q01：只用全文檢索腿 {NONE}；只用向量腿 {FIRST}；融合 {FIRST}",
        f"    q02：只用全文檢索腿 {FIRST}；只用向量腿 {SECOND}；融合 {FIRST}",
        f"    q03：只用全文檢索腿 {NONE}；只用向量腿 {FIRST}；融合 {FIRST}",
        f"    q04：只用全文檢索腿 {NONE}；只用向量腿 {MISS}；融合 {MISS}",
    ]
    assert low[low.index("[找相似論文（similar，只有向量腿）] 1 題") :][:4] == [
        "[找相似論文（similar，只有向量腿）] 1 題",
        "  只用向量腿：命中 1/1（100.0%），MRR 0.500，找不到相關的論文 0 題",
        "  每題：",
        f"    s01：只用向量腿 {SECOND}",
    ]
    high = sections["0.9"]
    assert high[:9] == [
        "[搜尋（search）] 4 題",
        "  只用全文檢索腿：命中 1/4（25.0%），MRR 0.250，找不到相關的論文 3 題",
        "  只用向量腿：命中 2/4（50.0%），MRR 0.500，找不到相關的論文 2 題",
        "  融合：命中 3/4（75.0%），MRR 0.750，找不到相關的論文 1 題",
        "  每題：",
        f"    q01：只用全文檢索腿 {NONE}；只用向量腿 {FIRST}；融合 {FIRST}",
        f"    q02：只用全文檢索腿 {FIRST}；只用向量腿 {NONE}；融合 {FIRST}",
        f"    q03：只用全文檢索腿 {NONE}；只用向量腿 {FIRST}；融合 {FIRST}",
        f"    q04：只用全文檢索腿 {NONE}；只用向量腿 {NONE}；融合 {NONE}",
    ]
    assert f"    s01：只用向量腿 {MISS}" in high

    # 每題的查詢向量只算一次：兩個下限共用，provider 與帳本都只有題數次。
    assert sorted(provider.queries) == sorted(QUERY_VECTORS)
    assert provider.closed
    assert (
        execute(db, "SELECT operation, outcome FROM api_call ORDER BY called_at, id")
        == [("embed_query", "succeeded")] * 4
    )

    [row] = execute(
        db,
        "SELECT kind, query_sets, min_similarities, embedding_model_id, snapshot_id, query_count,"
        " results, started_at, finished_at FROM eval_run",
    )
    kind, sets, floors, model, snapshot_id, count, results, started, finished = row
    assert (kind, sets, floors, model, count) == (
        "search",
        ["search", "similar"],
        [Decimal("0.2"), Decimal("0.9")],
        MODEL,
        5,
    )
    assert snapshot_id == execute(db, "SELECT id FROM snapshot")[0][0]
    assert started == finished == NOW
    assert [floor["min_similarity"] for floor in results["floors"]] == ["0.2", "0.9"]
    assert results["floors"][0]["sets"]["search"]["summary"]["vector"] == {
        "questions": 4,
        "hits": 3,
        "hit_rate": "0.7500",
        "mrr": "0.6250",
        "no_results": 0,
    }
    assert results["floors"][1]["sets"]["similar"]["questions"] == [
        {"query_id": "s01", "ranks": {"vector": None}, "has_results": {"vector": True}}
    ]

    # 日誌（JSON 行，和報告一起印在輸出裡）只記查詢的長度，不記內容；報告也只印題目編號。
    logged = [line for line in result.output.splitlines() if line.startswith("{")]
    assert sum('"query_length"' in line for line in logged) >= 4
    for query in QUERY_VECTORS:
        assert query not in result.output


def test_default_floor_comes_from_the_config(
    db: TestDatabase, env: Path, papers: dict[int, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _invoke("import-queries", input=SAMPLE.read_bytes()).exit_code == 0
    _provider(monkeypatch, FakeQueries())
    # 暫存設定檔換成另一個合法值，分得出「讀設定」和「寫死 0.2」（審查 M9）。
    path = env / "retrieval.yaml"
    text = path.read_text(encoding="utf-8")
    assert 'vector_min_similarity: "0.2"' in text
    path.write_text(
        text.replace('vector_min_similarity: "0.2"', 'vector_min_similarity: "0.35"'),
        encoding="utf-8",
    )

    result = _invoke("search")

    assert result.exit_code == 0, result.output
    assert "相似度下限：0.35" in _lines(result.output)
    assert execute(db, "SELECT min_similarities FROM eval_run") == [([Decimal("0.35")],)]


def test_unreachable_workers_ai_saves_no_run(
    db: TestDatabase, env: Path, papers: dict[int, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _invoke("import-queries", input=SAMPLE.read_bytes()).exit_code == 0
    _provider(monkeypatch, FakeQueries(fail=True))

    result = _invoke("search")

    assert result.exit_code == 1
    assert "查詢沒能轉成向量" in result.output
    assert execute(db, "SELECT count(*) FROM eval_run") == [(0,)]
    assert execute(db, "SELECT outcome FROM api_call") == [("unreachable",)]


def test_no_questions_and_bad_floor_stop_before_sending(
    db: TestDatabase, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeQueries()
    _provider(monkeypatch, provider)

    empty = _invoke("search")
    bad = _invoke("search", "--min-similarity", "1.5")

    assert empty.exit_code == 1
    assert "還沒有評估題目" in empty.output
    assert bad.exit_code == 1
    assert "--min-similarity 要在 0（含）到 1（不含）之間" in bad.output
    assert provider.queries == []
    assert execute(db, "SELECT count(*) FROM eval_run") == [(0,)]


def test_excluded_anchor_is_skipped_when_running(
    db: TestDatabase, env: Path, papers: dict[int, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _invoke("import-queries", input=SAMPLE.read_bytes()).exit_code == 0
    execute(db, "UPDATE paper SET excluded_reason = 'erratum' WHERE id = %s", papers[1])
    _provider(monkeypatch, FakeQueries())

    result = _invoke("search")

    assert result.exit_code == 0, result.output
    lines = _lines(result.output)
    assert "題數：4（範圍 tracked；看前 10 名）" in lines
    assert "  similar s01 10.9999/fake.0001：起點論文被排除或看不到，整題跳過" in lines
    assert not any(line.startswith("[找相似論文") for line in lines)
    assert execute(db, "SELECT query_sets, query_count FROM eval_run") == [
        (["search", "similar"], 4)
    ]


# --- retrieve 重用查詢向量 -------------------------------------------------------------------


def _setup(provider: Any) -> RetrievalSetup:
    return RetrievalSetup(
        embedding=CONFIG,
        retrieval=RETRIEVAL,
        cloud_policy=CloudPolicy(),
        provider=provider,
        clock=lambda: NOW,
    )


def test_reused_query_vector_gives_the_same_result_without_calling_the_provider(
    db: TestDatabase, database: Database, papers: dict[int, uuid.UUID]
) -> None:
    provider = FakeQueries()
    setup = _setup(provider)
    viewer = SystemViewer()
    normal = retrieve(
        viewer,
        database,
        setup,
        query="  online reviews trust ",
        scope=RetrievalScope.TRACKED,
        filters=PaperListFilters(),
        k=20,
    )
    vector = embed_retrieval_query(viewer, database, setup, " online reviews trust")
    calls = len(provider.queries)

    reused = retrieve(
        viewer,
        database,
        setup,
        query="online reviews trust",
        query_vector=vector,
        scope=RetrievalScope.TRACKED,
        filters=PaperListFilters(),
        k=20,
    )

    assert vector.text == "online reviews trust"
    assert vector.model_id == MODEL
    assert len(provider.queries) == calls == 2
    assert reused.hits == normal.hits
    assert [hit.paper_id for hit in reused.hits] == [papers[2], papers[3]]
    assert execute(db, "SELECT count(*) FROM api_call") == [(2,)]


def test_query_vector_must_match_the_query_and_the_model(
    db: TestDatabase, database: Database, papers: dict[int, uuid.UUID]
) -> None:
    provider = FakeQueries()
    setup = _setup(provider)
    viewer = SystemViewer()
    vector = embed_retrieval_query(viewer, database, setup, "平台採用")
    other_model = RetrievalSetup(
        embedding=CONFIG.model_copy(update={"model_id": "fake-embedding-b"}),
        retrieval=RETRIEVAL,
        cloud_policy=CloudPolicy(),
        provider=provider,
        clock=lambda: NOW,
    )
    common: dict[str, Any] = {
        "scope": RetrievalScope.TRACKED,
        "filters": PaperListFilters(),
        "k": 20,
    }

    with pytest.raises(ValueError, match="對不上"):
        retrieve(viewer, database, setup, query="數位推力", query_vector=vector, **common)
    with pytest.raises(ValueError, match="對不上"):
        retrieve(viewer, database, other_model, query="平台採用", query_vector=vector, **common)
    with pytest.raises(ValueError, match="只能和 query 一起給"):
        retrieve(viewer, database, setup, paper_id=papers[1], query_vector=vector, **common)
    assert provider.queries == ["平台採用"]


def test_forged_or_altered_query_vectors_are_refused(
    db: TestDatabase, database: Database, papers: dict[int, uuid.UUID]
) -> None:
    # 審查 H1：只收 `embed_retrieval_query` 算出來的向量；自己建的、換過向量或查詢字的都不收。
    provider = FakeQueries()
    setup = _setup(provider)
    viewer = SystemViewer()
    real = embed_retrieval_query(viewer, database, setup, "平台採用")
    forged = SearchQueryVector(
        text="平台採用", model_id=MODEL, vector=tuple(QUERY), seal=bytes(len(real.seal))
    )
    common: dict[str, Any] = {
        "scope": RetrievalScope.TRACKED,
        "filters": PaperListFilters(),
        "k": 20,
    }

    for bad in (
        forged,
        dataclasses.replace(real, vector=tuple(FAR)),
        dataclasses.replace(real, text="數位推力"),
        dataclasses.replace(real, model_id="fake-embedding-b"),
    ):
        query = bad.text
        with pytest.raises(ValueError, match="不是 embed_retrieval_query 算出來的"):
            retrieve(viewer, database, setup, query=query, query_vector=bad, **common)
    assert provider.queries == ["平台採用"]
    assert execute(db, "SELECT count(*) FROM api_call") == [(1,)]


def test_search_inside_a_callers_transaction_needs_a_query_vector(
    db: TestDatabase, database: Database, papers: dict[int, uuid.UUID]
) -> None:
    provider = FakeQueries()
    setup = _setup(provider)

    with (
        database.read_only_snapshot_transaction() as connection,
        pytest.raises(ValueError, match="要同時給 query_vector"),
    ):
        retrieve(
            SystemViewer(),
            database,
            setup,
            query="平台採用",
            scope=RetrievalScope.TRACKED,
            filters=PaperListFilters(),
            k=20,
            connection=connection,
        )
    assert provider.queries == []


# --- 審查修正：同一份資料、匯入排隊、零題不動舊題組、寫到一半回滾、起點範圍 ---------------------


def _parsed(text: str) -> ParsedEvalQueries:
    return parse_eval_queries_csv(text.encode("utf-8"), query_max_length=500)


def test_every_floor_of_a_question_reads_the_same_data(
    db: TestDatabase,
    database: Database,
    papers: dict[int, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 審查 H2：第一個下限跑完、第二個下限還沒跑之前，另一條連線把正解排除（commit）；同一題的
    # 兩個下限仍讀同一份資料，所以兩個下限的數字一樣。
    import_eval_queries(
        SystemViewer(),
        database,
        _parsed("set,query_id,query,relevant_doi\nsearch,q01,平台採用,10.9999/fake.0001\n"),
    )
    real_retrieve = retrieve
    calls: list[int] = []

    def retrieve_then_write(*args: Any, **kwargs: Any) -> Any:
        page = real_retrieve(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            execute(db, "UPDATE paper SET excluded_reason = 'erratum' WHERE id = %s", papers[1])
        return page

    monkeypatch.setattr("radar.services.evaluation.retrieve", retrieve_then_write)
    setup = _setup(FakeQueries())
    floors = (Decimal("0.2"), Decimal("0.3"))

    report = run_search_eval(SystemViewer(), database, setup, min_similarities=floors)

    assert len(calls) == 2
    ranks = [floor.sets[0].outcomes[0].legs[EvalLeg.FUSED].rank for floor in report.floors]
    assert ranks == [1, 1]
    # 寫入真的生效了：之後再跑一次，正解已經不在結果裡。
    monkeypatch.setattr("radar.services.evaluation.retrieve", real_retrieve)
    again = run_search_eval(SystemViewer(), database, setup, min_similarities=floors)
    assert [f.sets[0].outcomes[0].legs[EvalLeg.FUSED].rank for f in again.floors] == [None, None]


def _waiting_for(db: TestDatabase, key: int) -> bool:
    """這個測試資料庫裡有沒有連線正在等 `key` 這把 advisory lock（`pg_locks` 沒拿到的列）。"""
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    ((waiting,),) = execute(
        db,
        "SELECT count(*) FROM pg_locks"
        " WHERE locktype = 'advisory' AND NOT granted AND objsubid = 1"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        " AND classid = %s::bigint::oid AND objid = %s::bigint::oid",
        unsigned >> 32,
        unsigned & 0xFFFF_FFFF,
    )
    return bool(waiting)


def test_two_imports_at_once_leave_exactly_one_file(
    db: TestDatabase,
    database: Database,
    papers: dict[int, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 審查 H3：A 寫完題目、還沒 commit 時停住（同步訊號）；B 這時開始，要卡在匯入的鎖上
    # （看 pg_locks，不猜時間）。放開 A 之後 B 才做，最後只剩 B 的題目。
    first = _parsed("set,query_id,query,relevant_doi\nsearch,a1,平台採用,10.9999/fake.0001\n")
    second = _parsed("set,query_id,query,relevant_doi\nsearch,b1,數位推力,10.9999/fake.0003\n")
    real_create = create_eval_queries
    inside = threading.Event()
    release = threading.Event()

    def create_then_wait(connection: Any, questions: Any) -> None:
        real_create(connection, questions)
        if questions[0].query_id == "a1":
            inside.set()
            assert release.wait(10)

    monkeypatch.setattr("radar.services.evaluation.create_eval_queries", create_then_wait)
    results: dict[str, Any] = {}

    def run(name: str, parsed: ParsedEvalQueries) -> None:
        try:
            results[name] = import_eval_queries(SystemViewer(), database, parsed)
        except Exception as exc:  # 交給主執行緒斷言
            results[name] = exc

    a = threading.Thread(target=run, args=("a", first))
    b = threading.Thread(target=run, args=("b", second))
    a.start()
    assert inside.wait(10), "A 沒有走到寫入"
    b.start()
    deadline = time.monotonic() + 4
    try:
        while not _waiting_for(db, EVAL_QUERIES_LOCK_KEY):
            assert time.monotonic() < deadline, "B 沒有在等匯入的鎖"
        assert b.is_alive()
        assert "b" not in results
    finally:
        release.set()
        a.join(timeout=10)
        b.join(timeout=10)

    assert not a.is_alive()
    assert not b.is_alive()
    assert results["a"].replaced == 0
    assert results["b"].replaced == 1
    assert _questions(db) == [("search", "b1", "數位推力", "10.9999/fake.0003", None)]


def test_import_with_no_question_left_keeps_the_old_questions(
    db: TestDatabase, env: Path, papers: dict[int, uuid.UUID]
) -> None:
    # 審查 H4：題目檔格式對，但 DOI 全部對不到（例如匯到錯的環境）：列出原因、舊題組不動、退出碼 1。
    assert _invoke("import-queries", input=SAMPLE.read_bytes()).exit_code == 0
    before = _questions(db)
    wrong = (
        "set,query_id,query,relevant_doi\n"
        "search,q09,平台,10.9999/fake.0097\n"
        "similar,s09,10.9999/fake.0096,10.9999/fake.0001\n"
    )

    result = _invoke("import-queries", input=wrong.encode("utf-8"))

    assert result.exit_code == 1
    assert _lines(result.output) == [
        "沒有任何一題可以匯入（DOI 都對不到論文），原本的評估題目沒有動：",
        "-   第 2 列 search q09 10.9999/fake.0097：正解的 DOI 對不到論文，這一篇跳過",
        "-   第 2 列 search q09：正解都對不到論文，整題跳過",
        "-   第 3 列 similar s09 10.9999/fake.0096：起點論文的 DOI 對不到論文，整題跳過",
    ]
    assert _questions(db) == before


def test_failure_halfway_through_writing_rolls_back_everything(
    db: TestDatabase, database: Database, env: Path, papers: dict[int, uuid.UUID]
) -> None:
    # 審查 M6：第二題違反資料庫的長度約束（繞過題目檔的驗證直接給服務）：舊題組完整、
    # 新題一題都沒留。
    assert _invoke("import-queries", input=SAMPLE.read_bytes()).exit_code == 0
    before = _questions(db)
    good = EvalQuestion(
        EvalQuerySet.SEARCH, "n1", "新的題目", (EvalRelevant("10.9999/fake.0001", None),)
    )
    too_long = EvalQuestion(
        EvalQuerySet.SEARCH, "n2", "字" * 2001, (EvalRelevant("10.9999/fake.0002", None),)
    )

    with pytest.raises(sqlalchemy.exc.IntegrityError, match="ck_eval_query_query_text_length"):
        import_eval_queries(
            SystemViewer(), database, ParsedEvalQueries(questions=(good, too_long), blank_lines=0)
        )

    assert _questions(db) == before
    assert execute(db, "SELECT count(*) FROM eval_query WHERE query_key IN ('n1', 'n2')") == [(0,)]


def test_similar_anchor_outside_the_tracked_scope_is_skipped(
    db: TestDatabase, env: Path, papers: dict[int, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 審查 M1：fake.0005 看得到（匯得進去），但不在 tracked 範圍（沒進快照）：跑題時整題跳過。
    extra = SAMPLE.read_bytes() + b"similar,s09,10.9999/fake.0005,10.9999/fake.0001,\n"
    assert _invoke("import-queries", input=extra).exit_code == 0
    assert ("similar", "s09", "10.9999/fake.0005", "10.9999/fake.0001", None) in _questions(db)
    _provider(monkeypatch, FakeQueries())

    result = _invoke("search")

    assert result.exit_code == 0, result.output
    lines = _lines(result.output)
    assert "題數：5（範圍 tracked；看前 10 名）" in lines
    assert "  similar s09 10.9999/fake.0005：起點論文不在追蹤範圍（tracked），整題跳過" in lines
    assert not any(line.startswith("    s09：") for line in lines)
    [(results,)] = execute(db, "SELECT results FROM eval_run")
    assert {
        "set": "similar",
        "query_id": "s09",
        "doi": "10.9999/fake.0005",
        "reason": EvalSkipReason.ANCHOR_NOT_TRACKED.value,
    } in results["skips"]


def test_nothing_to_run_saves_no_run(
    db: TestDatabase, env: Path, papers: dict[int, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 審查 M3：有題目但全部跳過（唯一一題的起點被排除）：列出原因、不存 eval_run、退出碼 1。
    only_similar = (
        "set,query_id,query,relevant_doi\nsimilar,s01,10.9999/fake.0001,10.9999/fake.0002\n"
    )
    assert _invoke("import-queries", input=only_similar.encode("utf-8")).exit_code == 0
    execute(db, "UPDATE paper SET excluded_reason = 'erratum' WHERE id = %s", papers[1])
    _provider(monkeypatch, FakeQueries())

    result = _invoke("search")

    assert result.exit_code == 1
    assert _lines(result.output) == [
        "沒有任何一題可以跑（全部跳過），沒有存這次的評估：",
        "-   similar s01 10.9999/fake.0001：起點論文被排除或看不到，整題跳過",
    ]
    assert execute(db, "SELECT count(*) FROM eval_run") == [(0,)]
