"""Story 4.1 的效能量測（一次性，不屬於正式程式架構）：10,000 篇假論文、每篇一個 1,024 維的假向量
（真的模型 `@cf/qwen/qwen3-embedding-0.6b` 的 HNSW 部分索引），量 `services/retrieval.retrieve`
（搜尋：查詢向量用假 provider 回，不連網路；找相似論文：用存好的向量）每次呼叫的 p50／p95。

- 只連 `RADAR_TEST_ADMIN_DATABASE_URL` 指的測試用 PostgreSQL（解析連線設定，主機不是
  127.0.0.1／localhost 或埠不是 55434 就停）；建一個全新的
  `radar_test_<亂數>`，量完刪掉。
- 標題與摘要是從 200 個假字裡隨機挑的；向量是 200 個「主題」方向加上雜訊（同主題的論文彼此相近）。
- 每種情況先暖身 20 次，再量 200 次。結果寫到 `output/results.json`。

用法（在 repo 根目錄）：
    RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:55434/postgres \
        uv run --no-sync python spikes/epic-4a-retrieval-p95/measure.py
"""

import json
import random
import statistics
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from radar.config import load_embedding_config, load_retrieval_config
from radar.domain.library import AddedVia
from radar.domain.paper_list import PaperListFilters, PaperListScope
from radar.domain.policy import CloudPolicy, CloudSafeText
from radar.domain.retrieval import RetrievalPage, RetrievalScope
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.repo.library import create_library_item
from radar.services.members import allow_member
from radar.services.retrieval import RetrievalSetup, retrieve
from tests.database import admin_url, connect_admin, create_database, drop_database, migrate

ROOT = Path(__file__).resolve().parents[2]
CONFIG_ENV = {"RADAR_CONFIG_DIR": str(ROOT / "config")}
PAPERS = 10_000
TOPICS = 200
SIZE = 1024
LIBRARY = 500
WARMUP = 20
RUNS = 200
SEED = 4_1

_SETUP_SQL = """
INSERT INTO weekly_run (status) VALUES ('failed');
INSERT INTO snapshot (number, weekly_run_id, status, as_of)
    SELECT 1, id, 'published', now() FROM weekly_run;
CREATE TEMP TABLE words AS
    SELECT array_agg('w' || n) AS w FROM generate_series(1, 200) AS n;
INSERT INTO paper (origin, title, in_scope, lab_visible, journal_issn_l, publication_year,
                   first_snapshot_id)
    SELECT 'tracked',
           (SELECT string_agg(w[1 + floor(random() * 200)::int], ' ')
              FROM words, generate_series(1, 8) AS g WHERE g > 0 * n),
           true, true, '0276-7783', 2016 + (n / 200) % 10,  -- 同主題的論文年份分散
           (SELECT id FROM snapshot)
      FROM generate_series(1, %(papers)s) AS n;
INSERT INTO paper_abstract (paper_id, source, content, is_current)
    SELECT p.id, 'openalex',
           (SELECT string_agg(w[1 + floor(random() * 200)::int], ' ')
              FROM words, generate_series(1, 120) AS g WHERE g > 0 * length(p.title)),
           true
      FROM paper AS p;
"""


def _topics(rng: random.Random) -> list[list[float]]:
    return [[rng.gauss(0, 1) for _ in range(SIZE)] for _ in range(TOPICS)]


def _vectors(rng: random.Random, topics: list[list[float]]) -> list[list[float]]:
    result = []
    for index in range(PAPERS):
        base = topics[index % TOPICS]
        result.append([value + rng.gauss(0, 0.6) for value in base])
    return result


def _fill(
    name: str, rng: random.Random, model_id: str, topics: list[list[float]]
) -> list[uuid.UUID]:
    with connect_admin(name) as admin:
        admin.execute("SELECT setseed(0.41)")
        for statement in _SETUP_SQL.split(";\n"):
            if statement.strip():
                admin.execute(statement.replace("%(papers)s", str(PAPERS)))
        ids = [
            uuid.UUID(str(row[0]))
            for row in admin.execute("SELECT id FROM paper ORDER BY id").fetchall()
        ]
        vectors = _vectors(rng, topics)
        with admin.cursor() as cursor:
            with cursor.copy(
                "COPY derivation_run (id, paper_id, task, model, input_sha256, input_kind, params,"
                " code_version, status, call_mode, finished_at) FROM STDIN"
            ) as copy:
                for paper_id in ids:
                    copy.write_row(
                        (
                            paper_id,
                            paper_id,
                            "embed",
                            model_id,
                            paper_id.hex * 2,
                            "abstract",
                            "{}",
                            "p95",
                            "succeeded",
                            "interactive",
                            datetime.now(UTC),
                        )
                    )
            with cursor.copy(
                "COPY embedding (item_type, item_id, model_id, dimensions, vector, input_sha256,"
                " run_id) FROM STDIN"
            ) as copy:
                for paper_id, vector in zip(ids, vectors, strict=True):
                    text = "[" + ",".join(f"{value:.5f}" for value in vector) + "]"
                    copy.write_row(
                        ("paper", paper_id, model_id, SIZE, text, paper_id.hex * 2, paper_id)
                    )
        admin.execute("ANALYZE")
    return ids


class QueryVector:
    """假的 provider：`embed_query` 回「某個主題」附近的向量（不連網路）。"""

    def __init__(self, model_id: str, rng: random.Random, topics: list[list[float]]) -> None:
        self.model_id = model_id
        self._rng = rng
        self._topics = topics

    def embed_documents(self, texts: object) -> list[list[float]]:
        raise AssertionError("不該呼叫")

    def embed_query(self, text: CloudSafeText) -> list[float]:
        topic = self._rng.choice(self._topics)
        return [value + self._rng.gauss(0, 0.6) for value in topic]


def _measure(call: Callable[[int], RetrievalPage]) -> dict[str, float]:
    for index in range(WARMUP):
        call(index)
    timings = []
    totals = []
    vector_hits = []
    for index in range(RUNS):
        start = time.perf_counter()
        page = call(index)
        timings.append((time.perf_counter() - start) * 1000)
        totals.append(page.total)
        vector_hits.append(sum(hit.vector_rank is not None for hit in page.hits))
    timings.sort()
    return {
        "median_total": statistics.median(totals),
        "median_vector_hits_on_page": statistics.median(vector_hits),
        "p50_ms": round(statistics.median(timings), 1),
        "p95_ms": round(timings[int(len(timings) * 0.95) - 1], 1),
        "max_ms": round(timings[-1], 1),
    }


ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
ALLOWED_PORT = "55434"


def require_local_test_database(url: str) -> None:
    """連線設定解析後，主機（含 `hostaddr`）只能是 127.0.0.1／localhost、埠只能是 55434（審查 M10）。

    只看字串裡有沒有 `:55434/` 擋不住別台主機的 55434；沒寫主機或埠時 libpq 會用環境變數或預設值，
    也不放行。

    Raises:
        SystemExit: 不是本機 55434 的測試資料庫（不建庫、不遷移、不刪任何東西）。
    """
    try:
        info = conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        sys.exit("連線設定解析不了，不跑")
    host, port, hostaddr = info.get("host"), info.get("port"), info.get("hostaddr")
    if host not in ALLOWED_HOSTS or port != ALLOWED_PORT:
        sys.exit("只准連 127.0.0.1:55434 的測試資料庫")
    if hostaddr is not None and hostaddr != "127.0.0.1":
        sys.exit("只准連 127.0.0.1:55434 的測試資料庫")


def main() -> None:
    require_local_test_database(admin_url())
    rng = random.Random(SEED)
    embedding = load_embedding_config(CONFIG_ENV)
    retrieval = load_retrieval_config(CONFIG_ENV)
    db = create_database(with_extensions=True)
    try:
        migrate(db)
        started = time.perf_counter()
        topics = _topics(rng)
        ids = _fill(db.name, rng, embedding.model_id, topics)
        fill_seconds = round(time.perf_counter() - started, 1)
        database = Database(db.app_url, connect_timeout=5, pool_size=2)
        owner_id = allow_member(
            SystemViewer(), database, email="p95.owner@example.com", display_name="量測"
        ).member_id
        with database.transaction() as connection:
            for paper_id in rng.sample(ids, LIBRARY):
                create_library_item(
                    connection,
                    SystemViewer(),
                    owner_id=owner_id,
                    paper_id=paper_id,
                    added_via=AddedVia.UPLOAD,
                )
        viewer = MemberViewer(member_id=owner_id, is_admin=False)
        setup = RetrievalSetup(
            embedding=embedding,
            retrieval=retrieval,
            cloud_policy=CloudPolicy(),
            provider=QueryVector(embedding.model_id, rng, topics),
            clock=lambda: datetime.now(UTC),
        )
        words = [f"w{n}" for n in range(1, 201)]
        results: dict[str, object] = {
            "papers": PAPERS,
            "dimensions": SIZE,
            "library_items": LIBRARY,
            "runs": RUNS,
            "fill_seconds": fill_seconds,
            "config": retrieval.model_dump(mode="json"),
        }
        for scope in (RetrievalScope.TRACKED, RetrievalScope.LAB, RetrievalScope.PERSONAL):
            filters = PaperListFilters(scope=PaperListScope(scope.value))
            results[f"search_{scope.value}"] = _measure(
                lambda index, scope=scope, filters=filters: retrieve(
                    viewer,
                    database,
                    setup,
                    query=f"{words[index % 200]} {words[(index * 7) % 200]}",
                    scope=scope,
                    filters=filters,
                    k=retrieval.page_size,
                )
            )
            results[f"similar_{scope.value}"] = _measure(
                lambda index, scope=scope, filters=filters: retrieve(
                    viewer,
                    database,
                    setup,
                    paper_id=ids[(index * 37) % PAPERS],
                    scope=scope,
                    filters=filters,
                    k=retrieval.page_size,
                )
            )
        filtered = PaperListFilters(years=(2020, 2021), journal_issn_ls=("0276-7783",))
        results["search_tracked_with_year_filter"] = _measure(
            lambda index: retrieve(
                viewer,
                database,
                setup,
                query=words[index % 200],
                scope=RetrievalScope.TRACKED,
                filters=filtered,
                k=retrieval.page_size,
            )
        )
        with connect_admin(db.name) as admin:
            results["postgres"] = str(admin.execute("SELECT version()").fetchone()[0])
            results["pgvector"] = str(
                admin.execute(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                ).fetchone()[0]
            )
        output = Path(__file__).parent / "output" / "results.json"
        output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        drop_database(db)


if __name__ == "__main__":
    main()
