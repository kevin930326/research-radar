"""依來源移除與 AI 寫入路徑的摘要來源鎖（Story 2.12 審查後第 1、2、5 點、ADR-0044）對真的
PostgreSQL：

- 第 1 點：工作在移除之前讀到摘要、移除做完之後才要取得資格 → 不建 run、不呼叫；呼叫途中摘要被
  移除 → 不寫表頭（run 改 `abstract_source_changed`），之後的移除沒有東西可刪；移除進行中要取得
  資格的工作會等移除結束，結束後核對失敗、不呼叫；Batch 匯入在核對輸入之後摘要才被移除 → 不寫
  表頭。
- 第 2 點：只被這個來源用到的構面才刪（其他來源的結果、人工合併判斷用到的留著）；用這個來源算的
  向量一併刪掉。
- 第 5 點：接續遷移前沒記來源的 run 時補上來源；寫表頭時辨識出遷移前 run 的來源並補上。
- 第二輪 R1、O1：遷移前的 run 在鎖內辨識不出來源（輸入已經變了、移除做完之後才回來、拿鎖之後
  摘要剛好被拿掉）→ 不寫表頭、run 改 `abstract_source_changed`；拿鎖時辨識出的來源照樣補記。
- 第二輪 R2：看摘要卻沒記來源的向量，移除任何來源時保守一起刪；只看標題的不動。
- 第二輪 R3：構面候選被別的交易鎖住 → 移除整次退回、一列都沒少；鎖放開後重跑清乾淨。

模型一律用依 schema 回覆的替身（假回覆不是真的錄的），不連網路；論文與回覆都是自己編的。
"""

import functools
import threading
import uuid
from collections.abc import Mapping
from typing import Any

import psycopg
import pytest

import radar.services.admin_runs as admin_runs
import radar.services.ai_results as ai_results
import radar.services.llm_gateway as llm_gateway
from radar.domain.admin_runs import RemovalBlock
from radar.domain.ai_outputs import InputKind, LlmTask, OutputLang, RunKey
from radar.domain.errors import ErrorCode, LockWaitTimeoutError
from radar.domain.eval_split import EvalSplit
from radar.domain.judgments import ActorRole, EvidenceLevel, MergeDecisionValue, merge_decision
from radar.domain.llm import LlmRequest, LlmResult
from radar.domain.llm_batches import BatchItemOutcome
from radar.domain.papers import AbstractSource
from radar.domain.viewer import SystemViewer
from radar.repo.ai_outputs import delete_ai_results_by_abstract_source
from radar.repo.db import Database
from radar.repo.judgments import create_judgment_event
from radar.repo.papers import find_paper_llm_source
from radar.services.ai_results import AbstractSourceChangedError, record_llm_result
from radar.services.llm_batches import llm_job
from radar.services.llm_gateway import (
    GatewayOutcome,
    GatewayResult,
    PreparedLlmRequest,
    prepare_llm_request,
    run_llm_job,
)
from radar.services.prompt_loader import load_prompt
from tests.batch_import_support import (
    by_task,
    headers,
    poll,
    provider_with,
    run_states,
    submitted_batch,
    succeeded,
)
from tests.batch_support import batch_setup, insert_pending_run, insert_run
from tests.database import TestDatabase, migrate
from tests.llm_support import execute
from tests.trial_support import (
    WAIT,
    SchemaReplyProvider,
    banking_paper,
    doi_in,
    good_replies,
    trial_setup,
)

SYSTEM = SystemViewer()
OPENALEX = AbstractSource.OPENALEX
MEMBER = uuid.UUID("00000000-0000-7000-8000-00000000000b")


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=4)


def _gateway(
    database: Database, provider: Any, paper_id: uuid.UUID, task: LlmTask
) -> GatewayResult:
    setup = trial_setup()
    return run_llm_job(
        SYSTEM,
        database,
        provider,
        setup.gateway,
        llm_job(setup, paper_id, task, InputKind.ABSTRACT),
        budget_allows=lambda _task: True,
    )


def _paper(database: Database, n: int) -> uuid.UUID:
    return banking_paper(database, doi_in(EvalSplit.TUNING, str(n)), n)


def _drop_openalex(db: TestDatabase) -> None:
    """來源授權改變的前一步：停用來源、移除它的摘要（測試直接刪測試資料）。"""
    execute(db, "DELETE FROM paper_abstract WHERE source = 'openalex'")


def _count(db: TestDatabase, table: str) -> int:
    return int(execute(db, f"SELECT count(*) FROM {table}")[0][0])


def _source_headers(db: TestDatabase, source: str = "openalex") -> int:
    """`derivation_run.abstract_source` 是 `source` 的表頭數（三張表頭表）。"""
    return int(
        execute(
            db,
            "SELECT count(*) FROM (SELECT run_id FROM zh_summary UNION ALL SELECT run_id"
            " FROM classification UNION ALL SELECT run_id FROM research_model) AS h"
            " JOIN derivation_run AS r ON r.id = h.run_id WHERE r.abstract_source = %s",
            source,
        )[0][0]
    )


def _run_source(db: TestDatabase, run_id: uuid.UUID) -> object:
    return execute(db, "SELECT abstract_source FROM derivation_run WHERE id = %s", run_id)[0][0]


def _current_key(database: Database, paper_id: uuid.UUID, task: LlmTask) -> RunKey:
    """這篇論文這個工作現在會送的請求的鍵（和閘門算的一樣）。"""
    setup = trial_setup()
    with database.transaction() as connection:
        source = find_paper_llm_source(SYSTEM, connection, paper_id, for_update=False)
    assert source is not None
    prepared = prepare_llm_request(
        setup.gateway,
        llm_job(setup, paper_id, task, InputKind.ABSTRACT),
        source,
        load_prompt(setup.gateway.prompts_root, task),
    )
    assert isinstance(prepared, PreparedLlmRequest)
    return prepared.key


# --- 第 1 點：取得資格、寫表頭都在來源鎖裡核對 -------------------------------------------------


def test_job_that_read_the_abstract_before_removal_does_not_claim(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper_id = _paper(database, 1)
    real = llm_gateway._prepare_request
    removed: list[Any] = []

    def removal_in_between(*args: Any, **kwargs: Any) -> Any:
        # 第 1 步讀完摘要之後、第 5 步取得資格之前：來源被停用、摘要移除、依來源移除做完。
        if not removed:
            _drop_openalex(db)
            removed.append(admin_runs.remove_source_ai_outputs(SYSTEM, database, OPENALEX))
        return real(*args, **kwargs)

    monkeypatch.setattr(llm_gateway, "_prepare_request", removal_in_between)
    provider = SchemaReplyProvider(good_replies())

    result = _gateway(database, provider, paper_id, LlmTask.ZH_SUMMARY)

    assert removed[0].blocks == ()
    assert result == GatewayResult(
        GatewayOutcome.INPUT_CHANGED, reason=ErrorCode.ABSTRACT_SOURCE_CHANGED
    )
    assert provider.total_calls == 0
    assert _count(db, "derivation_run") == 0
    assert _source_headers(db) == 0


class _RemovedDuringCall(SchemaReplyProvider):
    """呼叫途中來源被停用、摘要移除，並試著依來源移除（run 還在跑，應該被擋下）。"""

    def __init__(self, db: TestDatabase, database: Database) -> None:
        super().__init__(good_replies())
        self.db = db
        self.database = database
        self.blocks: tuple[RemovalBlock, ...] = ()

    def complete(self, request: LlmRequest) -> LlmResult:
        _drop_openalex(self.db)
        self.blocks = admin_runs.remove_source_ai_outputs(SYSTEM, self.database, OPENALEX).blocks
        return super().complete(request)


def test_abstract_removed_during_the_call_writes_no_header(
    db: TestDatabase, database: Database
) -> None:
    paper_id = _paper(database, 1)
    provider = _RemovedDuringCall(db, database)

    result = _gateway(database, provider, paper_id, LlmTask.ZH_SUMMARY)

    assert provider.blocks == (RemovalBlock.RUNNING_RUNS,)
    assert result.outcome is GatewayOutcome.INPUT_CHANGED
    assert result.reason is ErrorCode.ABSTRACT_SOURCE_CHANGED
    assert result.run_id is not None
    assert result.cost_usd is not None  # 帳照記
    assert _count(db, "llm_call") == 1
    assert _source_headers(db) == 0
    assert execute(
        db, "SELECT status, failure_reason FROM derivation_run WHERE id = %s", result.run_id
    ) == [("failed", "abstract_source_changed")]
    # 不會再接續：之後的移除沒有東西可刪、也不被擋。
    after = admin_runs.remove_source_ai_outputs(SYSTEM, database, OPENALEX)
    assert (after.blocks, after.total_deleted, after.audit_event_id) == ((), 0, None)


def test_claim_waits_for_a_removal_in_progress_then_does_not_call(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper_id = _paper(database, 1)
    read = threading.Event()
    go = threading.Event()
    real_prompt = llm_gateway._prepare_request

    def pause_after_reading(*args: Any, **kwargs: Any) -> Any:
        read.set()
        assert go.wait(WAIT)
        return real_prompt(*args, **kwargs)

    monkeypatch.setattr(llm_gateway, "_prepare_request", pause_after_reading)
    entered = threading.Event()
    release = threading.Event()
    real_delete = delete_ai_results_by_abstract_source

    def slow_delete(connection: Any, source: AbstractSource) -> Mapping[str, int]:
        entered.set()
        assert release.wait(WAIT)
        return real_delete(connection, source)

    monkeypatch.setattr(admin_runs, "delete_ai_results_by_abstract_source", slow_delete)
    provider = SchemaReplyProvider(good_replies())
    results: list[GatewayResult] = []
    removals: list[Any] = []
    job = threading.Thread(
        target=lambda: results.append(_gateway(database, provider, paper_id, LlmTask.ZH_SUMMARY))
    )
    removal = threading.Thread(
        target=lambda: removals.append(
            admin_runs.remove_source_ai_outputs(SYSTEM, database, OPENALEX)
        )
    )

    job.start()
    assert read.wait(WAIT)  # 工作已經讀到 OpenAlex 的摘要
    _drop_openalex(db)
    removal.start()
    assert entered.wait(WAIT)  # 移除拿著來源的獨占鎖
    go.set()
    job.join(0.5)
    assert job.is_alive()  # 取得資格卡在來源的共用鎖上
    release.set()
    removal.join(WAIT)
    job.join(WAIT)

    assert removals[0].blocks == ()
    assert [result.outcome for result in results] == [GatewayOutcome.INPUT_CHANGED]
    assert provider.total_calls == 0
    assert _count(db, "derivation_run") == 0


def test_batch_import_writes_no_header_once_the_source_is_removed(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = batch_setup(enabled=False)
    submission_id = submitted_batch(database, setup)
    found = by_task(database, submission_id)
    real = llm_gateway._current_batch_input

    def removed_after_the_check(*args: Any, **kwargs: Any) -> Any:
        # 匯入核對完輸入指紋之後、寫表頭之前，來源的摘要被移除。
        current = real(*args, **kwargs)
        _drop_openalex(db)
        return current

    monkeypatch.setattr(llm_gateway, "_current_batch_input", removed_after_the_check)

    result = poll(database, provider_with([succeeded(item) for item in found.values()]), setup)

    [row] = result.rows
    assert row.counts.count(BatchItemOutcome.INPUT_CHANGED) == 3
    assert headers(db) == {}
    run_ids = [item.run_id for item in found.values()]
    # 第一項在寫表頭前（來源鎖裡）發現；之後的項目在核對輸入指紋時就發現了。
    states = set(run_states(db, run_ids).values())
    assert ("failed", "abstract_source_changed") in states
    assert states <= {("failed", "abstract_source_changed"), ("failed", "batch_input_changed")}


# --- 第 2 點：構面與向量 ------------------------------------------------------------------------


def _research_model_header(db: TestDatabase, paper_id: uuid.UUID) -> uuid.UUID:
    [(header_id,)] = execute(db, "SELECT id FROM research_model WHERE paper_id = %s", paper_id)
    return uuid.UUID(str(header_id))


def _construct_ids(db: TestDatabase, header_id: uuid.UUID) -> list[uuid.UUID]:
    rows = execute(
        db,
        "SELECT construct_id FROM construct_mention WHERE research_model_id = %s"
        " ORDER BY construct_id",
        header_id,
    )
    return [uuid.UUID(str(row[0])) for row in rows]


def _extra_mention(db: TestDatabase, header_id: uuid.UUID, name: str) -> uuid.UUID:
    """在這列研究模型表頭底下多掛一個只有它用到的構面（SQL 編的測試資料），回構面 id。"""
    [(construct_id,)] = execute(
        db,
        "INSERT INTO construct (id, normalized_name, canonical_id)"
        " SELECT x, %s, x FROM (SELECT uuidv7() AS x) AS new RETURNING id",
        name,
    )
    execute(
        db,
        "INSERT INTO construct_mention (research_model_id, construct_id, name_en, label_zh)"
        " VALUES (%s, %s, %s, %s)",
        header_id,
        construct_id,
        name,
        name,
    )
    return uuid.UUID(str(construct_id))


def test_removal_deletes_only_constructs_no_one_else_uses(
    db: TestDatabase, database: Database
) -> None:
    provider = SchemaReplyProvider(good_replies())
    mine = _paper(database, 1)
    other = _paper(database, 2)
    for paper_id in (mine, other):
        result = _gateway(database, provider, paper_id, LlmTask.EXTRACT_MODEL)
        assert result.outcome is GatewayOutcome.ACTIVE
    # 第二篇的結果當成 Semantic Scholar 來源的（兩篇的構面一樣，所以這些構面兩個來源都用到）。
    execute(
        db,
        "UPDATE derivation_run SET abstract_source = 'semantic_scholar' WHERE paper_id = %s",
        other,
    )
    header = _research_model_header(db, mine)
    shared = _construct_ids(db, header)
    assert shared
    only_mine = _extra_mention(db, header, "only openalex construct")
    judged = _extra_mention(db, header, "judged construct")
    # 人工合併判斷用到 judged：留著。
    with database.transaction() as connection:
        create_judgment_event(
            connection,
            merge_decision(
                member_id=MEMBER,
                actor_role=ActorRole.ADMIN,
                construct_a=judged,
                construct_b=shared[0],
                decision=MergeDecisionValue.REJECT,
                evidence_level=EvidenceLevel.NAME,
                reason=None,
            ),
        )
    _drop_openalex(db)
    preview = admin_runs.preview_source_removal(SYSTEM, database, OPENALEX)
    assert preview.counts.constructs == 1

    removal = admin_runs.remove_source_ai_outputs(SYSTEM, database, OPENALEX)

    assert removal.deleted["construct"] == 1
    remaining = {uuid.UUID(str(row[0])) for row in execute(db, "SELECT id FROM construct")}
    assert only_mine not in remaining
    assert {*shared, judged} <= remaining


def _embedding(
    db: TestDatabase, paper_id: uuid.UUID, source: str | None, *, input_kind: str | None = None
) -> None:
    """一筆 qwen3（遷移 0015 登記的 1,024 維）向量，記的摘要來源是 `source`（沒給 `input_kind`
    時：沒有來源的是只看標題）。"""
    input_kind = input_kind or ("title" if source is None else "abstract")
    [(run_id,)] = execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, model, input_sha256, input_kind, params,"
        " code_version, call_mode, status, finished_at) VALUES (%s, 'embed',"
        " '@cf/qwen/qwen3-embedding-0.6b', %s, %s, '{}'::jsonb, 'test', 'interactive',"
        " 'succeeded', now()) RETURNING id",
        paper_id,
        uuid.uuid4().hex * 2,
        input_kind,
    )
    execute(
        db,
        "INSERT INTO embedding (item_type, item_id, model_id, dimensions, vector, input_sha256,"
        " run_id, abstract_source) SELECT 'paper', paper_id, model, 1024, CAST(%s AS halfvec),"
        " input_sha256, id, %s FROM derivation_run WHERE id = %s",
        "[" + ",".join(["0.5"] * 1024) + "]",
        source,
        run_id,
    )


def test_removal_deletes_vectors_computed_from_the_source(
    db: TestDatabase, database: Database
) -> None:
    openalex = _paper(database, 1)
    other = _paper(database, 2)
    titled = _paper(database, 3)
    _embedding(db, openalex, "openalex")
    _embedding(db, other, "semantic_scholar")
    _embedding(db, titled, None)
    _drop_openalex(db)
    assert admin_runs.preview_source_removal(SYSTEM, database, OPENALEX).counts.embeddings == 1

    result = admin_runs.remove_source_ai_outputs(SYSTEM, database, OPENALEX)

    assert result.deleted["embedding"] == 1
    left = {uuid.UUID(str(row[0])) for row in execute(db, "SELECT item_id FROM embedding")}
    assert left == {other, titled}
    [(detail,)] = execute(db, "SELECT detail FROM audit_event")
    assert detail["counts"]["embedding"] == 1


def test_removal_also_deletes_abstract_vectors_without_a_source(
    db: TestDatabase, database: Database
) -> None:
    # 第二輪 R2：遷移 0018 證明不了來源的看摘要向量（NULL），可能就是用這個來源算的：保守一起刪
    # （之後照指紋規則重算）；只看標題的向量、其他來源的向量不動。
    unknown = _paper(database, 1)
    other = _paper(database, 2)
    titled = _paper(database, 3)
    _embedding(db, unknown, None, input_kind="abstract")
    _embedding(db, other, "semantic_scholar")
    _embedding(db, titled, None)
    _drop_openalex(db)
    assert admin_runs.preview_source_removal(SYSTEM, database, OPENALEX).counts.embeddings == 1

    result = admin_runs.remove_source_ai_outputs(SYSTEM, database, OPENALEX)

    assert result.deleted["embedding"] == 1
    left = {uuid.UUID(str(row[0])) for row in execute(db, "SELECT item_id FROM embedding")}
    assert left == {other, titled}


def _two_research_models(db: TestDatabase, database: Database) -> tuple[uuid.UUID, uuid.UUID]:
    """兩篇論文各跑完研究模型；第二篇的當成 Semantic Scholar 來源的。回（OpenAlex 的表頭,
    Semantic Scholar 的表頭）。"""
    provider = SchemaReplyProvider(good_replies())
    mine = _paper(database, 1)
    other = _paper(database, 2)
    for paper_id in (mine, other):
        result = _gateway(database, provider, paper_id, LlmTask.EXTRACT_MODEL)
        assert result.outcome is GatewayOutcome.ACTIVE
    execute(
        db,
        "UPDATE derivation_run SET abstract_source = 'semantic_scholar' WHERE paper_id = %s",
        other,
    )
    return _research_model_header(db, mine), _research_model_header(db, other)


def test_removal_rolls_back_when_a_construct_is_locked_then_succeeds_on_rerun(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 第二輪 R3：別的交易正在新增引用候選構面的提及（還沒 commit）。移除等不到鎖 → 整次退回、
    # 一列都沒少（候選線索還在）；那個交易退回之後重跑，構面照樣清掉。
    header, other_header = _two_research_models(db, database)
    only_mine = _extra_mention(db, header, "only openalex construct")
    _drop_openalex(db)
    before = {
        table: _count(db, table)
        for table in ("research_model", "construct_mention", "hypothesis", "construct")
    }
    real = delete_ai_results_by_abstract_source
    monkeypatch.setattr(
        admin_runs,
        "delete_ai_results_by_abstract_source",
        functools.partial(real, construct_lock_timeout_ms=200),
    )
    writer = psycopg.connect(db.admin_url, autocommit=False, connect_timeout=5)
    try:
        writer.execute(
            "INSERT INTO construct_mention (research_model_id, construct_id, name_en, label_zh)"
            " VALUES (%s, %s, 'in flight', 'in flight')",
            (other_header, only_mine),
        )
        with pytest.raises(LockWaitTimeoutError):
            admin_runs.remove_source_ai_outputs(SYSTEM, database, OPENALEX)
        after = {table: _count(db, table) for table in before}
        assert after == before
        assert _count(db, "audit_event") == 0
    finally:
        writer.rollback()
        writer.close()

    removal = admin_runs.remove_source_ai_outputs(SYSTEM, database, OPENALEX)

    assert removal.blocks == ()
    assert removal.deleted["construct"] == 1
    remaining = {uuid.UUID(str(row[0])) for row in execute(db, "SELECT id FROM construct")}
    assert only_mine not in remaining
    assert _source_headers(db) == 0


def test_construct_used_by_a_committed_writer_is_kept(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 第二輪 R3：移除等鎖的期間，持鎖的交易 commit 了引用 → 拿到鎖之後看得到它，構面留著。
    header, other_header = _two_research_models(db, database)
    only_mine = _extra_mention(db, header, "only openalex construct")
    _drop_openalex(db)
    writer = psycopg.connect(db.admin_url, autocommit=False, connect_timeout=5)
    results: list[Any] = []
    try:
        writer.execute(
            "INSERT INTO construct_mention (research_model_id, construct_id, name_en, label_zh)"
            " VALUES (%s, %s, 'in flight', 'in flight')",
            (other_header, only_mine),
        )
        removal = threading.Thread(
            target=lambda: results.append(
                admin_runs.remove_source_ai_outputs(SYSTEM, database, OPENALEX)
            )
        )
        removal.start()
        removal.join(0.5)
        assert removal.is_alive()  # 等候選構面的鎖
        writer.commit()
        removal.join(WAIT)
    finally:
        writer.close()

    assert results[0].deleted["construct"] == 0
    remaining = {uuid.UUID(str(row[0])) for row in execute(db, "SELECT id FROM construct")}
    assert only_mine in remaining


# --- 第 5 點：遷移前沒記來源的 run --------------------------------------------------------------


def test_resuming_a_run_without_a_source_fills_it_in(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper_id = _paper(database, 1)
    run_id = insert_pending_run(db, _current_key(database, paper_id, LlmTask.ZH_SUMMARY))
    assert _run_source(db, run_id) is None

    def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("寫入失敗（測試）")

    # 寫表頭失敗：接續（取得資格的交易）已經 commit，來源要在那一步就補上。
    monkeypatch.setattr(llm_gateway, "record_llm_result", broken)
    provider = SchemaReplyProvider(good_replies())
    with pytest.raises(RuntimeError):
        _gateway(database, provider, paper_id, LlmTask.ZH_SUMMARY)

    assert provider.total_calls == 0
    assert _run_source(db, run_id) == "openalex"


def test_writing_a_header_identifies_the_source_of_an_old_run(
    db: TestDatabase, database: Database
) -> None:
    paper_id = _paper(database, 1)
    run_id = insert_run(db, _current_key(database, paper_id, LlmTask.ZH_SUMMARY), status="running")
    assert _run_source(db, run_id) is None

    record_llm_result(
        SYSTEM,
        database,
        trial_setup().gateway,
        paper_id=paper_id,
        run_id=run_id,
        lang=OutputLang.ZH_HANT,
        abstract_id=None,
        reject_reason=ErrorCode.LLM_OUTPUT_INVALID,
    )

    assert _run_source(db, run_id) == "openalex"
    assert _source_headers(db) == 1


# --- 第二輪 R1、O1：遷移前的 run 鎖內辨識不出來源就不寫 ---------------------------------------


def _record_rejected(database: Database, paper_id: uuid.UUID, run_id: uuid.UUID) -> None:
    record_llm_result(
        SYSTEM,
        database,
        trial_setup().gateway,
        paper_id=paper_id,
        run_id=run_id,
        lang=OutputLang.ZH_HANT,
        abstract_id=None,
        reject_reason=ErrorCode.LLM_OUTPUT_INVALID,
    )


def _run_state(db: TestDatabase, run_id: uuid.UUID) -> tuple[Any, ...]:
    return execute(db, "SELECT status, failure_reason FROM derivation_run WHERE id = %s", run_id)[0]


def test_old_run_whose_input_changed_writes_no_header(db: TestDatabase, database: Database) -> None:
    paper_id = _paper(database, 1)
    key = _current_key(database, paper_id, LlmTask.ZH_SUMMARY)
    stale = RunKey(key.paper_id, key.task, key.prompt_version, key.model, "c" * 64)
    run_id = insert_run(db, stale, status="running")  # 遷移前的 run，輸入已經變了

    with pytest.raises(AbstractSourceChangedError):
        _record_rejected(database, paper_id, run_id)

    assert _count(db, "zh_summary") == 0
    assert _run_state(db, run_id) == ("failed", "abstract_source_changed")
    assert _run_source(db, run_id) is None


def test_old_run_coming_back_after_the_removal_writes_no_header(
    db: TestDatabase, database: Database
) -> None:
    paper_id = _paper(database, 1)
    run_id = insert_run(db, _current_key(database, paper_id, LlmTask.ZH_SUMMARY), status="running")
    _drop_openalex(db)
    removal = admin_runs.remove_source_ai_outputs(SYSTEM, database, OPENALEX)
    assert removal.blocks == ()  # 來源 NULL 的 run 不算「還在跑、用這個來源的 run」

    with pytest.raises(AbstractSourceChangedError):
        _record_rejected(database, paper_id, run_id)

    assert _count(db, "zh_summary") == 0
    assert _run_state(db, run_id) == ("failed", "abstract_source_changed")
    after = admin_runs.remove_source_ai_outputs(SYSTEM, database, OPENALEX)
    assert (after.blocks, after.total_deleted) == ((), 0)


def test_old_run_identified_before_the_lock_is_recorded_but_not_written(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # O1：拿鎖前用指紋辨識出 OpenAlex、拿了它的共用鎖；鎖住論文之前摘要被拿掉（另一條連線
    # commit）。鎖內辨識不出 → 不寫表頭，但 run 補記 OpenAlex（指紋已經證明用的是它）。
    paper_id = _paper(database, 1)
    run_id = insert_run(db, _current_key(database, paper_id, LlmTask.ZH_SUMMARY), status="running")
    real = ai_results._hold_abstract_source
    held: list[Any] = []

    def then_removed(connection: Any, rid: uuid.UUID) -> Any:
        held.append(real(connection, rid))
        _drop_openalex(db)
        return held[-1]

    monkeypatch.setattr(ai_results, "_hold_abstract_source", then_removed)

    with pytest.raises(AbstractSourceChangedError):
        _record_rejected(database, paper_id, run_id)

    assert held == [OPENALEX]
    assert _count(db, "zh_summary") == 0
    assert _run_state(db, run_id) == ("failed", "abstract_source_changed")
    assert _run_source(db, run_id) == "openalex"
