"""管理者查 AI 結果與依摘要來源移除（`services/admin_runs.py`，Story 2.12、FR-11、AD-26、ADR-0044）
對真的 PostgreSQL：

- 查詢：三個區塊的狀態、有效值與來歷（含只有人工值的分類），歷次 run（active、superseded、
  rejected）；找不到回 None。
- 預覽：各表各狀態與細項的計數、還在跑的 run、目前摘要仍是這個來源的論文、來源不明的表頭；前後
  全部相關表的 `count(*)` 一樣。
- 執行：只刪指定來源的表頭與細項；標題輸入、其他來源、來源不明、run、帳、構面、摘要、人工判斷
  事件都不動，`effective_classification` 仍是人工值；稽核只記來源與計數。
- 擋下（還在跑、目前摘要仍是這個來源）什麼都不刪、不寫稽核；刪完再跑刪 0 列、不寫稽核；兩個同時
  執行只有一筆稽核；稽核寫入前出錯整個交易退回。

模型一律用依 schema 回覆的替身（假回覆不是真的錄的），不連網路；論文與回覆都是自己編的。
"""

import threading
import uuid
from collections.abc import Mapping
from typing import Any

import pytest

import radar.services.admin_runs as admin_runs
from radar.domain.admin_runs import REMOVAL_TABLES, RemovalBlock, render_paper_ai_report
from radar.domain.ai_blocks import AiBlockState, AiProduct
from radar.domain.ai_outputs import InputKind, LlmTask, ResultStatus
from radar.domain.eval_split import EvalSplit
from radar.domain.judgments import ActorRole, classification_fix
from radar.domain.papers import AbstractSource
from radar.domain.permissions import PermissionDeniedError
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.ai_outputs import delete_ai_results_by_abstract_source
from radar.repo.db import Database
from radar.repo.judgments import create_judgment_event
from radar.services.admin_runs import (
    get_paper_ai_report,
    preview_source_removal,
    remove_source_ai_outputs,
)
from radar.services.llm_batches import llm_job
from radar.services.llm_gateway import GatewayOutcome, GatewayResult, run_llm_job
from tests.batch_support import NOW
from tests.database import TestDatabase, migrate
from tests.llm_support import create_paper, execute, set_abstract
from tests.trial_support import (
    BANKING_ABSTRACT,
    WAIT,
    SchemaReplyProvider,
    banking_paper,
    doi_in,
    good_replies,
    reply,
    trial_setup,
)

SYSTEM = SystemViewer()
MEMBER = uuid.UUID("00000000-0000-7000-8000-00000000000a")
# 不是 JSON：拒收（llm_malformed_response）。假回覆，不是真的錄的。
BAD = reply("研究問題：行動銀行的採用")
# 依來源移除前後要比的表（被刪的六張以外都不能少一列）。
RELATED = (
    *REMOVAL_TABLES,
    "derivation_run",
    "llm_call",
    "construct",
    "paper",
    "paper_abstract",
    "judgment_event",
    "audit_event",
)


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=4)


def _gateway(
    database: Database,
    provider: SchemaReplyProvider,
    paper_id: uuid.UUID,
    task: LlmTask,
    input_kind: InputKind = InputKind.ABSTRACT,
) -> GatewayResult:
    setup = trial_setup()
    return run_llm_job(
        SYSTEM,
        database,
        provider,
        setup.gateway,
        llm_job(setup, paper_id, task, input_kind),
        budget_allows=lambda _task: True,
    )


def _all_tasks(database: Database, paper_id: uuid.UUID) -> None:
    provider = SchemaReplyProvider(good_replies())
    for task in (LlmTask.ZH_SUMMARY, LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL):
        assert _gateway(database, provider, paper_id, task).outcome is GatewayOutcome.ACTIVE


def _fix_topic(database: Database, paper_id: uuid.UUID, keys: list[str]) -> None:
    with database.transaction() as connection:
        create_judgment_event(
            connection,
            classification_fix(
                member_id=MEMBER,
                actor_role=ActorRole.MEMBER,
                paper_id=paper_id,
                field=TaxonomyField.TOPIC,
                keys=keys,
                reason=None,
            ),
        )


def _counts(db: TestDatabase) -> dict[str, int]:
    return {table: int(execute(db, f"SELECT count(*) FROM {table}")[0][0]) for table in RELATED}


def _other_source_header(db: TestDatabase, paper_id: uuid.UUID, source: str | None) -> None:
    """一列看摘要的 rejected 中文重點，run 記的來源是 `source`（None＝來源不明）。"""
    [(run_id,)] = execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
        " input_sha256, input_kind, params, code_version, call_mode, status, finished_at,"
        " abstract_source) VALUES (%s, 'zh_summary', 9, %s, 'claude-haiku-5-5', %s, 'abstract',"
        " '{}'::jsonb, 'test', 'interactive', 'succeeded', now(), %s) RETURNING id",
        paper_id,
        "a" * 64,
        uuid.uuid4().hex * 2,
        source,
    )
    execute(
        db,
        "INSERT INTO zh_summary (paper_id, run_id, input_kind, input_sha256, lang, status,"
        " reject_reason) SELECT paper_id, id, input_kind, input_sha256, 'zh-Hant', 'rejected',"
        " 'llm_output_invalid' FROM derivation_run WHERE id = %s",
        run_id,
    )


class _World:
    """依來源移除的測試資料：

    - `full`（OpenAlex）：三個工作都有 active（含細項），主題另有人工修正。
    - `rejected`（OpenAlex）：中文重點被拒收。
    - `titled`：沒有摘要，分類看標題（active，run 沒有來源）。
    - `full` 另有一列 Semantic Scholar 來源的表頭、一列來源不明的表頭（SQL 編的）。
    """

    def __init__(self, db: TestDatabase, database: Database) -> None:
        self.full = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
        _all_tasks(database, self.full)
        _fix_topic(database, self.full, ["ai_data_analytics"])
        self.rejected = banking_paper(database, doi_in(EvalSplit.TUNING, "2"), 2)
        result = _gateway(
            database,
            SchemaReplyProvider({LlmTask.ZH_SUMMARY: BAD}),
            self.rejected,
            LlmTask.ZH_SUMMARY,
        )
        assert result.outcome is GatewayOutcome.REJECTED
        self.titled = banking_paper(database, doi_in(EvalSplit.TUNING, "3"), 3, abstract=None)
        titled = _gateway(
            database,
            SchemaReplyProvider(good_replies()),
            self.titled,
            LlmTask.CLASSIFY,
            InputKind.TITLE,
        )
        assert titled.outcome is GatewayOutcome.ACTIVE
        _other_source_header(db, self.full, "semantic_scholar")
        _other_source_header(db, self.full, None)


def _drop_openalex_abstracts(db: TestDatabase) -> None:
    """來源授權改變的前一步：停用來源、移除它的摘要（測試直接刪測試資料）。"""
    execute(db, "DELETE FROM paper_abstract WHERE source = 'openalex'")


def _classification(db: TestDatabase, paper_id: uuid.UUID) -> dict[str, tuple[Any, ...]]:
    rows = execute(
        db,
        "SELECT field, keys, human_verified FROM effective_classification WHERE paper_id = %s",
        paper_id,
    )
    return {str(row[0]): (list(row[1]), row[2]) for row in rows}


# --- 查詢 -------------------------------------------------------------------------------------


def test_show_reports_blocks_provenance_and_history(db: TestDatabase, database: Database) -> None:
    doi = doi_in(EvalSplit.TUNING, "1")
    paper_id = banking_paper(database, doi, 1)
    provider = SchemaReplyProvider(good_replies())
    assert _gateway(database, provider, paper_id, LlmTask.ZH_SUMMARY).outcome is (
        GatewayOutcome.ACTIVE
    )
    # 摘要改了再跑一次：舊的變 superseded。
    set_abstract(db, paper_id, BANKING_ABSTRACT + " We also interviewed managers.")
    assert _gateway(database, provider, paper_id, LlmTask.ZH_SUMMARY).outcome is (
        GatewayOutcome.ACTIVE
    )
    rejected = _gateway(
        database, SchemaReplyProvider({LlmTask.CLASSIFY: BAD}), paper_id, LlmTask.CLASSIFY
    )
    assert rejected.outcome is GatewayOutcome.REJECTED
    _fix_topic(database, paper_id, ["e_commerce"])

    report = get_paper_ai_report(SYSTEM, database, trial_setup(), doi, now=NOW)

    assert report is not None
    assert report.paper_id == paper_id
    assert report.dois == (doi,)
    zh = report.blocks[AiProduct.ZH_SUMMARY]
    assert zh.state is AiBlockState.READY
    assert zh.provenance is not None
    assert zh.provenance.model == "claude-haiku-5-5"
    classification = report.blocks[AiProduct.CLASSIFICATION]
    assert classification.state is AiBlockState.READY
    assert classification.provenance is not None
    assert classification.provenance.model is None
    outcomes = [(row.task, row.outcome, row.reason) for row in report.history]
    assert outcomes == [
        (LlmTask.ZH_SUMMARY, "superseded", None),
        (LlmTask.ZH_SUMMARY, "active", None),
        (LlmTask.CLASSIFY, "rejected", "llm_malformed_response"),
    ]
    assert {row.abstract_source for row in report.history} == {AbstractSource.OPENALEX}
    assert report.active_prompt_version(LlmTask.ZH_SUMMARY) == 1
    text = "\n".join(render_paper_ai_report(report))
    assert "來歷：人工（沒有 AI 值）" in text
    assert "主題：e_commerce（人工值）" in text
    # 用論文 ID 查也一樣。
    by_id = get_paper_ai_report(SYSTEM, database, trial_setup(), paper_id, now=NOW)
    assert by_id is not None
    assert by_id.history == report.history


def test_show_returns_none_when_not_found(db: TestDatabase, database: Database) -> None:
    create_paper(database)
    for target in (uuid.uuid4(), "10.1/nowhere"):
        assert get_paper_ai_report(SYSTEM, database, trial_setup(), target, now=NOW) is None


def test_admin_services_need_a_system_actor(database: Database) -> None:
    member = MemberViewer(MEMBER, is_admin=True)
    with pytest.raises(PermissionDeniedError):
        get_paper_ai_report(member, database, trial_setup(), uuid.uuid4(), now=NOW)
    with pytest.raises(PermissionDeniedError):
        preview_source_removal(member, database, AbstractSource.OPENALEX)
    with pytest.raises(PermissionDeniedError):
        remove_source_ai_outputs(member, database, AbstractSource.OPENALEX)


# --- 預覽 -------------------------------------------------------------------------------------


def test_preview_counts_everything_and_writes_nothing(db: TestDatabase, database: Database) -> None:
    world = _World(db, database)
    before = _counts(db)

    preview = preview_source_removal(SYSTEM, database, AbstractSource.OPENALEX)

    assert _counts(db) == before
    headers = preview.counts.headers
    assert headers[LlmTask.ZH_SUMMARY][ResultStatus.ACTIVE] == 1
    assert headers[LlmTask.ZH_SUMMARY][ResultStatus.REJECTED] == 1
    assert headers[LlmTask.CLASSIFY][ResultStatus.ACTIVE] == 1
    assert headers[LlmTask.EXTRACT_MODEL][ResultStatus.ACTIVE] == 1
    assert preview.counts.total_headers == 4
    labels = int(
        execute(
            db,
            "SELECT count(*) FROM classification_label AS d JOIN classification AS h"
            " ON h.id = d.classification_id WHERE h.paper_id = %s",
            world.full,
        )[0][0]
    )
    assert labels > 0
    assert preview.counts.details["classification_label"] == labels
    assert preview.counts.details["construct_mention"] > 0
    assert preview.counts.details["hypothesis"] > 0
    assert preview.counts.unknown_source == 1
    assert preview.running_runs == 0
    assert preview.current_papers == 2
    assert preview.blocks == (RemovalBlock.CURRENT_ABSTRACTS,)
    other = preview_source_removal(SYSTEM, database, AbstractSource.SEMANTIC_SCHOLAR)
    assert other.counts.total_headers == 1
    assert (other.current_papers, other.blocks) == (0, ())
    assert _counts(db) == before


# --- 執行 -------------------------------------------------------------------------------------


def test_execute_removes_only_the_target_source(db: TestDatabase, database: Database) -> None:
    world = _World(db, database)
    _drop_openalex_abstracts(db)
    expected = preview_source_removal(SYSTEM, database, AbstractSource.OPENALEX).counts.by_table()
    before = _counts(db)
    human_before = _classification(db, world.full)["topic"]
    titled_before = execute(
        db, "SELECT id, status FROM classification WHERE paper_id = %s", world.titled
    )

    result = remove_source_ai_outputs(SYSTEM, database, AbstractSource.OPENALEX)

    assert result.blocks == ()
    assert dict(result.deleted) == expected
    assert result.total_deleted > 4
    after = _counts(db)
    for table in REMOVAL_TABLES:
        assert after[table] == before[table] - expected[table], table
    for table in (
        "derivation_run",
        "llm_call",
        "construct",
        "paper",
        "paper_abstract",
        "judgment_event",
    ):
        assert after[table] == before[table], table
    assert after["audit_event"] == before["audit_event"] + 1
    # 標題輸入、其他來源、來源不明的表頭都還在。
    assert (
        execute(db, "SELECT id, status FROM classification WHERE paper_id = %s", world.titled)
        == titled_before
    )
    remaining = execute(
        db,
        "SELECT r.abstract_source FROM zh_summary AS h JOIN derivation_run AS r"
        " ON r.id = h.run_id ORDER BY r.abstract_source NULLS LAST",
    )
    assert remaining == [("semantic_scholar",), (None,)]
    # 人工值仍是有效值。
    assert _classification(db, world.full)["topic"] == human_before
    assert human_before == (["ai_data_analytics"], True)
    assert "method" not in _classification(db, world.full)
    # 稽核：系統身分，只有來源與各表計數。
    [audit] = execute(
        db,
        "SELECT id, actor_role, actor_member_id, action, target_type, target_id, detail"
        " FROM audit_event",
    )
    assert uuid.UUID(str(audit[0])) == result.audit_event_id
    assert audit[1:6] == ("system", None, "remove_source_ai_outputs", "abstract_source", None)
    assert audit[6] == {"source": "openalex", "counts": expected}


def test_second_execute_deletes_nothing_and_writes_no_audit(
    db: TestDatabase, database: Database
) -> None:
    _World(db, database)
    _drop_openalex_abstracts(db)
    remove_source_ai_outputs(SYSTEM, database, AbstractSource.OPENALEX)
    before = _counts(db)

    again = remove_source_ai_outputs(SYSTEM, database, AbstractSource.OPENALEX)

    assert again.blocks == ()
    assert again.total_deleted == 0
    assert again.audit_event_id is None
    assert _counts(db) == before


def test_current_abstracts_block_the_removal(db: TestDatabase, database: Database) -> None:
    _World(db, database)
    before = _counts(db)

    result = remove_source_ai_outputs(SYSTEM, database, AbstractSource.OPENALEX)

    assert result.blocks == (RemovalBlock.CURRENT_ABSTRACTS,)
    assert result.current_papers == 2
    assert result.total_deleted == 0
    assert result.audit_event_id is None
    assert _counts(db) == before


def test_running_runs_block_the_removal(db: TestDatabase, database: Database) -> None:
    world = _World(db, database)
    _drop_openalex_abstracts(db)
    execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
        " input_sha256, input_kind, params, code_version, call_mode, status, abstract_source)"
        " VALUES (%s, 'classify', 9, %s, 'claude-haiku-5-5', %s, 'abstract', '{}'::jsonb,"
        " 'test', 'batch', 'running', 'openalex')",
        world.rejected,
        "a" * 64,
        uuid.uuid4().hex * 2,
    )
    before = _counts(db)

    preview = preview_source_removal(SYSTEM, database, AbstractSource.OPENALEX)
    result = remove_source_ai_outputs(SYSTEM, database, AbstractSource.OPENALEX)

    assert preview.running_runs == 1
    assert result.blocks == (RemovalBlock.RUNNING_RUNS,)
    assert result.total_deleted == 0
    assert _counts(db) == before


def test_concurrent_removals_write_one_audit(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _World(db, database)
    _drop_openalex_abstracts(db)
    expected = preview_source_removal(SYSTEM, database, AbstractSource.OPENALEX).counts.by_table()
    real = delete_ai_results_by_abstract_source
    entered = threading.Event()
    release = threading.Event()
    first = threading.Lock()

    def slow_delete(connection: Any, source: AbstractSource) -> Mapping[str, int]:
        # 第一個拿到鎖的在刪除時停住，讓第二個卡在鎖上。
        if first.acquire(blocking=False):
            entered.set()
            assert release.wait(WAIT)
        return real(connection, source)

    monkeypatch.setattr(admin_runs, "delete_ai_results_by_abstract_source", slow_delete)
    results: list[Any] = []

    def remove() -> None:
        results.append(remove_source_ai_outputs(SYSTEM, database, AbstractSource.OPENALEX))

    one = threading.Thread(target=remove)
    two = threading.Thread(target=remove)
    one.start()
    assert entered.wait(WAIT)
    two.start()
    two.join(0.5)
    assert two.is_alive()  # 卡在交易級鎖上
    release.set()
    one.join(WAIT)
    two.join(WAIT)

    assert sorted(result.total_deleted for result in results) == [0, sum(expected.values())]
    assert sum(result.audit_event_id is not None for result in results) == 1
    assert int(execute(db, "SELECT count(*) FROM audit_event")[0][0]) == 1


def test_failure_before_the_audit_rolls_everything_back(
    db: TestDatabase, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _World(db, database)
    _drop_openalex_abstracts(db)
    before = _counts(db)

    def broken(*_args: object) -> uuid.UUID:
        raise RuntimeError("稽核寫入失敗（測試）")

    monkeypatch.setattr(admin_runs, "create_audit_event", broken)

    with pytest.raises(RuntimeError):
        remove_source_ai_outputs(SYSTEM, database, AbstractSource.OPENALEX)

    assert _counts(db) == before
