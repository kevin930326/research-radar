"""向量化的並行與重跑（Story 2.8 補充要求）：真的用兩個執行緒、兩條連線製造並行。

Procrastinate 可能把還活著的 worker 誤判失聯、把工作交給另一個 worker（`finish_job` 不檢查
負責人，佇列鎖擋不住），所以向量化的寫入要靠資料層自己擋：

1. 兩個 worker 同時跑同一批：每份輸入只送一次 Workers AI、每篇只有一列向量、一筆成功的 run。
2. 舊輸入算的向量晚到：不會蓋掉新輸入的。
3. 寫入與取得執行資格都會等每週更新改論文時的列鎖，等完看到新的摘要（不寫舊的向量、不送舊的輸入）。
4. 同一批跑完再跑一次：什麼都不送、列的內容不變。
5. 跑到一半中斷（例外中斷、行程被砍）後重跑：只補沒做完的，最後的列數與內容正確。

provider 一律用 `FakeEmbeddingProvider`（向量由 sha256 推出，不是真的模型，不連網路）；標題、
摘要都是測試自己編的。假模型 `fake-embedding`（8 維）由超級使用者登記。
"""

import threading
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from radar.adapters.fake import FakeEmbeddingProvider
from radar.domain.embeddings import EmbedRunResult, EmbedStop
from radar.domain.fingerprints import embedding_input_sha256
from radar.domain.policy import CloudSafeText, cloud_text
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.services.embeddings import embed_papers
from radar.services.llm_gateway import expire_stale_runs
from tests.database import TestDatabase, migrate
from tests.integration.test_embeddings import CONFIG, DIMENSIONS, MODEL, NOW, POLICY
from tests.llm_support import CODE_VERSION, create_paper, execute, set_abstract

SYSTEM = SystemViewer()
WAIT = 20.0


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    execute(
        fresh_database,
        "INSERT INTO embedding_model (model_id, provider, dimensions) VALUES (%s, 'fake', %s)",
        MODEL,
        DIMENSIONS,
    )
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=8)


class _Provider(FakeEmbeddingProvider):
    """假 provider：可以停在「等回應」（`gated`）、每次呼叫後慢一點（`delay`）、呼叫時跑一段
    `during_call`；呼叫記錄有鎖（兩個執行緒共用時也數得準）。"""

    def __init__(
        self,
        *,
        gated: bool = False,
        delay: float = 0.0,
        during_call: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(DIMENSIONS, model_id=MODEL)
        self.entered = threading.Event()
        self.release = threading.Event()
        if not gated:
            self.release.set()
        self.delay = delay
        self.during_call = during_call
        self._lock = threading.Lock()

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        with self._lock:
            vectors = super().embed_documents(texts)
            calls = len(self.document_calls)
        self.entered.set()
        if not self.release.wait(WAIT):
            raise AssertionError("等不到 release")
        if self.delay:
            time.sleep(self.delay)
        if self.during_call is not None:
            self.during_call(calls)
        return vectors

    def texts(self) -> list[str]:
        with self._lock:
            return [cloud_text(text) for call in self.document_calls for text in call]


class _Killed(BaseException):
    """模擬行程在呼叫中被中斷（不是 `Exception`，服務不會把它當一般錯誤吞掉）。"""


def _embed(database: Database, provider: FakeEmbeddingProvider, **kwargs: Any) -> EmbedRunResult:
    return embed_papers(
        SYSTEM,
        database,
        provider,
        CONFIG,
        POLICY,
        code_version=CODE_VERSION,
        clock=lambda: NOW,
        **kwargs,
    )


class _Worker:
    """在背景執行緒跑一次 `embed_papers`（模擬另一個 worker）；`start_gate` 讓幾個同時起跑。"""

    def __init__(
        self,
        database: Database,
        provider: FakeEmbeddingProvider,
        *,
        start_gate: threading.Barrier | None = None,
    ) -> None:
        self.result: EmbedRunResult | None = None
        self.error: BaseException | None = None
        self.done = threading.Event()

        def target() -> None:
            try:
                if start_gate is not None:
                    start_gate.wait(WAIT)
                self.result = _embed(database, provider)
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()

    def join(self) -> EmbedRunResult:
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "執行緒沒有在時限內結束"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT
    while not condition():
        assert time.monotonic() < deadline, "等不到條件成立"
        time.sleep(0.01)


def _paper(database: Database, n: int) -> uuid.UUID:
    return create_paper(
        database,
        doi=f"10.1/p{n}",
        openalex_id=f"W{n}",
        title=f"Paper {n} on platform adoption",
        abstract=f"Abstract {n}: teams adopt platforms.",
    )


def _text(n: int, abstract: str | None = None) -> str:
    body = abstract or f"Abstract {n}: teams adopt platforms."
    return f"Paper {n} on platform adoption\n\n{body}"


def _sha(n: int, abstract: str | None = None) -> str:
    return embedding_input_sha256(_text(n, abstract), "abstract")


def _rows(db: TestDatabase) -> dict[uuid.UUID, tuple[Any, ...]]:
    """每篇的向量列：（input_sha256, run_id, 向量文字, updated_at）。"""
    rows = execute(
        db,
        "SELECT item_id, input_sha256, run_id, vector::text, updated_at FROM embedding"
        " WHERE model_id = %s",
        MODEL,
    )
    return {row[0]: row[1:] for row in rows}


def _assert_one_valid_row_per_paper(db: TestDatabase, expected: dict[uuid.UUID, str]) -> None:
    """每篇正好一列、指紋是現在的輸入、`run_id` 指到這篇成功的 run；每篇只有一筆成功的 run。"""
    rows = _rows(db)
    assert {paper: row[0] for paper, row in rows.items()} == expected
    assert execute(db, "SELECT count(*) FROM embedding") == [(len(expected),)]
    runs = {
        row[0]: (row[1], row[2], row[3])
        for row in execute(
            db, "SELECT id, paper_id, status, input_sha256 FROM derivation_run WHERE task = 'embed'"
        )
    }
    for paper, row in rows.items():
        assert runs[row[1]] == (paper, "succeeded", expected[paper])
    succeeded = execute(
        db,
        "SELECT paper_id, count(*) FROM derivation_run WHERE task = 'embed'"
        " AND status = 'succeeded' GROUP BY paper_id",
    )
    assert dict(succeeded) == dict.fromkeys(expected, 1)
    assert execute(
        db, "SELECT count(*) FROM derivation_run WHERE task = 'embed' AND status = 'running'"
    ) == [(0,)]


# --- 1. 兩個 worker 同時跑同一批 ------------------------------------------------------------------


def test_two_workers_at_once_send_each_input_once_and_keep_one_row_per_paper(
    db: TestDatabase, database: Database
) -> None:
    papers = {_paper(database, n): n for n in range(1, 7)}
    providers = [_Provider(delay=0.05), _Provider(delay=0.05)]
    start = threading.Barrier(2)
    workers = [_Worker(database, provider, start_gate=start) for provider in providers]

    results = [worker.join() for worker in workers]

    # 每份輸入只送一次（兩個 worker 合起來），兩邊寫入的篇數加起來正好是全部。
    sent = providers[0].texts() + providers[1].texts()
    assert sorted(sent) == sorted(_text(n) for n in papers.values())
    assert sum(result.embedded for result in results) == 6
    assert all(result.stop is EmbedStop.COMPLETED for result in results)
    _assert_one_valid_row_per_paper(db, {paper: _sha(n) for paper, n in papers.items()})


def test_second_worker_finds_the_batch_running_and_sends_nothing(
    db: TestDatabase, database: Database
) -> None:
    papers = {_paper(database, n): n for n in (1, 2)}
    first = _Provider(gated=True)
    worker = _Worker(database, first)
    first.entered.wait(WAIT)  # 第一個 worker 拿到資格、停在等回應

    second = _Provider()
    result = _embed(database, second)

    assert result.busy == 2
    assert result.embedded == 0
    assert second.document_calls == []
    first.release.set()
    assert worker.join().embedded == 2
    _assert_one_valid_row_per_paper(db, {paper: _sha(n) for paper, n in papers.items()})


# --- 2. 舊輸入的向量晚到 --------------------------------------------------------------------------


def test_old_input_vector_arriving_late_never_overwrites_the_new_one(
    db: TestDatabase, database: Database
) -> None:
    paper = _paper(database, 1)
    old = _Provider(gated=True)
    worker = _Worker(database, old)
    old.entered.wait(WAIT)  # 舊輸入已經送出、停在等回應

    set_abstract(db, paper, "A newer abstract.")
    new_result = _embed(database, _Provider())
    assert new_result.embedded == 1
    new_row = _rows(db)[paper]

    old.release.set()
    old_result = worker.join()

    assert old_result.embedded == 0
    assert old_result.input_changed == 1
    assert _rows(db)[paper] == new_row
    _assert_one_valid_row_per_paper(db, {paper: _sha(1, "A newer abstract.")})


# --- 3. 等每週更新改論文時的列鎖 ------------------------------------------------------------------


def _hold_update(db: TestDatabase, paper: uuid.UUID, abstract: str) -> psycopg.Connection[Any]:
    """另一條連線照每週更新的做法先鎖住論文、改摘要，但還沒 commit。"""
    connection = psycopg.connect(db.admin_url, connect_timeout=5)
    connection.execute("SELECT id FROM paper WHERE id = %s FOR UPDATE", (paper,))
    connection.execute(
        "UPDATE paper_abstract SET content = %s WHERE paper_id = %s", (abstract, paper)
    )
    return connection


def _waiting_on_lock(db: TestDatabase) -> bool:
    rows = execute(
        db,
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        " AND wait_event_type = 'Lock'",
    )
    return int(rows[0][0]) > 0


def test_write_waits_for_the_update_lock_and_does_not_write_the_stale_vector(
    db: TestDatabase, database: Database
) -> None:
    paper = _paper(database, 1)
    held: list[psycopg.Connection[Any]] = []

    def update_while_calling(_calls: int) -> None:
        # 回應回來之前，每週更新鎖住論文、改了摘要；等寫入真的卡在鎖上才 commit。
        connection = _hold_update(db, paper, "Changed under the lock.")
        held.append(connection)

        def commit_when_blocked() -> None:
            _wait_until(lambda: _waiting_on_lock(db))
            connection.commit()
            connection.close()

        threading.Thread(target=commit_when_blocked, daemon=True).start()

    result = _embed(database, _Provider(during_call=update_while_calling))

    assert held
    assert result.embedded == 0
    assert result.input_changed == 1
    assert _rows(db) == {}
    # 下一次照新的摘要做。
    assert _embed(database, _Provider()).embedded == 1
    _assert_one_valid_row_per_paper(db, {paper: _sha(1, "Changed under the lock.")})


def test_claim_waits_for_the_update_lock_and_does_not_send_the_stale_input(
    db: TestDatabase, database: Database
) -> None:
    paper = _paper(database, 1)
    connection = _hold_update(db, paper, "Changed under the lock.")
    provider = _Provider()
    try:
        worker = _Worker(database, provider)
        _wait_until(lambda: _waiting_on_lock(db))  # 取得資格卡在論文的列鎖上
        connection.commit()
    finally:
        connection.close()

    result = worker.join()

    assert result.input_changed == 1
    assert provider.document_calls == []
    assert execute(db, "SELECT count(*) FROM derivation_run") == [(0,)]
    assert _embed(database, _Provider()).embedded == 1
    _assert_one_valid_row_per_paper(db, {paper: _sha(1, "Changed under the lock.")})


# --- 4. 同一批跑完再跑一次 ------------------------------------------------------------------------


def test_running_the_same_batch_again_sends_nothing_and_keeps_the_rows(
    db: TestDatabase, database: Database
) -> None:
    papers = {_paper(database, n): n for n in (1, 2, 3)}
    assert _embed(database, _Provider()).embedded == 3
    before = _rows(db)

    provider = _Provider()
    again = _embed(database, provider)

    assert again.unchanged == 3
    assert again.embedded == 0
    assert again.requests == 0
    assert provider.document_calls == []
    assert _rows(db) == before
    assert execute(db, "SELECT count(*) FROM derivation_run") == [(3,)]
    _assert_one_valid_row_per_paper(db, {paper: _sha(n) for paper, n in papers.items()})


# --- 5. 跑到一半中斷後重跑 ------------------------------------------------------------------------


def test_run_interrupted_midway_then_rerun_finishes_only_the_rest(
    db: TestDatabase, database: Database
) -> None:
    papers = {_paper(database, n): n for n in range(1, 6)}

    def interrupt(calls: int) -> None:
        if calls == 2:
            raise _Killed

    with pytest.raises(_Killed):
        _embed(database, _Provider(during_call=interrupt))  # 每批 2 篇：第二批中斷

    first_two = dict(list(sorted(papers.items(), key=lambda item: item[1]))[:2])
    assert {paper: row[0] for paper, row in _rows(db).items()} == {
        paper: _sha(n) for paper, n in first_two.items()
    }
    assert execute(
        db,
        "SELECT status, failure_reason, count(*) FROM derivation_run GROUP BY 1, 2 ORDER BY 1",
    ) == [("failed", "embed_failed", 2), ("succeeded", None, 2)]

    provider = _Provider()
    rerun = _embed(database, provider)

    # 只補沒做完的三篇（中斷那一批的兩篇＋還沒送的一篇），做好的不重送。
    assert sorted(provider.texts()) == sorted(_text(n) for n in (3, 4, 5))
    assert rerun.embedded == 3
    assert rerun.unchanged == 2
    _assert_one_valid_row_per_paper(db, {paper: _sha(n) for paper, n in papers.items()})


def test_killed_worker_leaves_running_runs_that_block_until_sweep_then_rerun_finishes(
    db: TestDatabase, database: Database
) -> None:
    killed = _paper(database, 1)
    other = _paper(database, 2)
    # 行程直接被砍：run 停在 running（沒有機會改成 failed）。
    execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, model, input_sha256, input_kind, params,"
        " code_version, status, call_mode) VALUES (%s, 'embed', %s, %s, 'abstract', '{}',"
        " 'test', 'running', 'interactive')",
        killed,
        MODEL,
        _sha(1),
    )

    first = _embed(database, _Provider())
    assert first.busy == 1
    assert set(_rows(db)) == {other}

    # sweep 照互動呼叫的時限把它收掉之後，重跑就補上。
    execute(db, "UPDATE derivation_run SET updated_at = now() - interval '1 hour'")
    assert expire_stale_runs(SYSTEM, database, timedelta(minutes=15)) == 1
    provider = _Provider()
    rerun = _embed(database, provider)

    assert provider.texts() == [_text(1)]
    assert rerun.embedded == 1
    assert rerun.unchanged == 1
    _assert_one_valid_row_per_paper(db, {killed: _sha(1), other: _sha(2)})
