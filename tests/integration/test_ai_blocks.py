"""AI 區塊的判定對真的 PostgreSQL（Story 2.7、ADR-0015、ADR-0035）：八個狀態各一個測試、新的一次
被拒收不讓舊的有效值失效、「目前這組鍵」以外的拒收與執行中不算、可見範圍，以及 `get_ai_blocks`
接上真的 prompt 登記、模型設定與預算帳本，和真的流程重跑分類後人工值仍有效（FR-4）。

`find_ai_block` 的測試直接用 SQL 造 AI 產出（`test_effective_views.py` 的小工具），「目前這組鍵」
用和正式程式同一套 domain 函式算；`get_ai_blocks` 的測試另外用真的流程（閘門＋模型替身）寫出
結果，確認判定和閘門算的鍵對得上。模型一律用 `FakeLlmProvider`；資料全是測試自己編的。
"""

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from radar.adapters.fake import FakeLlmProvider, fake_completion
from radar.config import load_context_values, load_taxonomy, load_zh_summary_rules
from radar.domain.ai_blocks import (
    AiBlock,
    AiBlockSettings,
    AiBlockState,
    AiProduct,
    ClassificationValue,
    ResearchModelValue,
    ZhSummaryValue,
)
from radar.domain.ai_outputs import GatewayConfig, InputKind, LlmTask, build_llm_input
from radar.domain.context_values import ContextValues
from radar.domain.fingerprints import input_sha256
from radar.domain.judgments import SummaryField
from radar.domain.llm import LlmUsage
from radar.domain.taxonomy import Taxonomy, TaxonomyField
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer, Viewer
from radar.repo.ai_blocks import find_ai_block
from radar.repo.db import Database
from radar.repo.papers import find_paper_llm_source
from radar.services.ai_blocks import get_ai_blocks
from radar.services.classification import run_classification
from radar.services.research_model import run_research_model
from radar.services.zh_summary import run_zh_summary
from tests.database import TestDatabase, migrate
from tests.integration import test_classification as classification_case
from tests.integration import test_research_model as research_model_case
from tests.integration import test_zh_summary as zh_summary_case
from tests.integration.test_effective_views import (
    MODEL,
    fix,
    insert_classification,
    insert_header,
    insert_run,
    report,
)
from tests.llm_support import (
    ROOT,
    budget_config,
    create_paper,
    execute,
    gateway_config,
    insert_ledger,
    set_abstract,
)

SYSTEM = SystemViewer()
NOW = datetime(2026, 11, 15, 4, 0, tzinfo=UTC)
USAGE = LlmUsage(input_tokens=500, output_tokens=50)
ZH_CONTENT = {
    "research_question": "遠距團隊如何採用協作平台？",
    "method": None,
    "theory": None,
    "findings": None,
}
EMPTY_ZH = dict.fromkeys(ZH_CONTENT)


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=4)


@pytest.fixture
def paper_id(database: Database) -> uuid.UUID:
    return create_paper(database)


def _settings(product: AiProduct, *, budget_allows: bool = True) -> AiBlockSettings:
    return AiBlockSettings(
        prompt_version=1,
        model=MODEL,
        taxonomy_version="1" if product is AiProduct.CLASSIFICATION else None,
        context_values_version="1" if product is AiProduct.RESEARCH_MODEL else None,
        budget_allows=budget_allows,
    )


def _block(
    database: Database,
    paper_id: uuid.UUID,
    product: AiProduct = AiProduct.ZH_SUMMARY,
    *,
    viewer: Viewer = SYSTEM,
    budget_allows: bool = True,
) -> AiBlock | None:
    with database.transaction() as connection:
        return find_ai_block(
            viewer,
            connection,
            paper_id,
            product,
            _settings(product, budget_allows=budget_allows),
        )


def _current_sha(
    database: Database, paper_id: uuid.UUID, product: AiProduct = AiProduct.ZH_SUMMARY
) -> str:
    """這篇論文現在的 `input_sha256`（和 `find_ai_block` 用同一套 domain 函式）。"""
    settings = _settings(product)
    with database.transaction() as connection:
        source = find_paper_llm_source(SYSTEM, connection, paper_id, for_update=False)
    assert source is not None
    kind = InputKind.ABSTRACT if source.abstract is not None else InputKind.TITLE
    return input_sha256(
        build_llm_input(source, kind).blocks,
        product.llm_task,
        taxonomy_version=settings.taxonomy_version,
        context_values_version=settings.context_values_version,
    )


def _state(block: AiBlock | None) -> AiBlockState:
    assert block is not None
    return block.state


# --- 八個狀態 ---------------------------------------------------------------------------------


def test_not_applicable_for_excluded_paper(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_header(db, paper_id, "zh_summary", ZH_CONTENT)
    execute(db, "UPDATE paper SET excluded_reason = 'editorial' WHERE id = %s", paper_id)

    for product in AiProduct:
        block = _block(database, paper_id, product)
        assert block == AiBlock(product, AiBlockState.NOT_APPLICABLE)


def test_no_input_for_summary_of_paper_without_abstract(database: Database) -> None:
    paper_id = create_paper(database, abstract=None)

    assert _block(database, paper_id) == AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.NO_INPUT)
    assert _state(_block(database, paper_id, AiProduct.RESEARCH_MODEL)) is AiBlockState.NO_INPUT
    # 沒有摘要的論文仍然用標題分類：不是 no_input。
    assert (
        _state(_block(database, paper_id, AiProduct.CLASSIFICATION)) is AiBlockState.PENDING_BATCH
    )


def test_ready_carries_value_and_provenance(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    summary_id = insert_header(db, paper_id, "zh_summary", ZH_CONTENT)
    report(database, summary_id, SummaryField.RESEARCH_QUESTION)

    block = _block(database, paper_id)

    assert block is not None
    assert block.state is AiBlockState.READY
    assert block.value == ZhSummaryValue(
        zh_summary_id=summary_id,
        reported_fields=(SummaryField.RESEARCH_QUESTION,),
        **ZH_CONTENT,
    )
    assert block.provenance is not None
    assert (block.provenance.model, block.provenance.input_kind) == (MODEL, InputKind.ABSTRACT)
    assert block.provenance.human_verified is False
    (created_at,) = execute(db, "SELECT created_at FROM zh_summary WHERE id = %s", summary_id)[0]
    assert block.provenance.generated_at == created_at


def test_empty_carries_provenance_only(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_header(db, paper_id, "zh_summary", EMPTY_ZH)

    block = _block(database, paper_id)

    assert block is not None
    assert (block.state, block.value) == (AiBlockState.EMPTY, None)
    assert block.provenance is not None
    assert block.provenance.model == MODEL


def test_failed_when_only_a_rejection_for_the_current_key(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    run_id = insert_run(db, paper_id, "zh_summary", input_sha256=_current_sha(database, paper_id))
    insert_header(db, paper_id, "zh_summary", status="rejected", run_id=run_id)

    assert _block(database, paper_id) == AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.FAILED)
    # 拒收排在預算前面：預算不准也是 failed。
    assert _state(_block(database, paper_id, budget_allows=False)) is AiBlockState.FAILED


def test_deferred_budget_when_budget_does_not_allow_and_no_effective_value(
    database: Database, paper_id: uuid.UUID
) -> None:
    assert _block(database, paper_id, budget_allows=False) == AiBlock(
        AiProduct.ZH_SUMMARY, AiBlockState.DEFERRED_BUDGET
    )


def test_pending_interactive_when_an_interactive_run_is_running(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_run(
        db,
        paper_id,
        "zh_summary",
        input_sha256=_current_sha(database, paper_id),
        status="running",
        call_mode="interactive",
    )

    assert _block(database, paper_id) == AiBlock(
        AiProduct.ZH_SUMMARY, AiBlockState.PENDING_INTERACTIVE
    )


def test_deferred_budget_comes_before_an_interactive_run(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    # 判定第 4 步（預算）排在第 5 步（執行中）前面。
    insert_run(
        db,
        paper_id,
        "zh_summary",
        input_sha256=_current_sha(database, paper_id),
        status="running",
        call_mode="interactive",
    )

    assert _block(database, paper_id, budget_allows=False) == AiBlock(
        AiProduct.ZH_SUMMARY, AiBlockState.DEFERRED_BUDGET
    )


def test_pending_batch_when_nothing_yet_or_a_batch_is_running(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    assert _block(database, paper_id) == AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.PENDING_BATCH)

    insert_run(
        db,
        paper_id,
        "zh_summary",
        input_sha256=_current_sha(database, paper_id),
        status="running",
        call_mode="batch",
    )

    assert _state(_block(database, paper_id)) is AiBlockState.PENDING_BATCH


# --- 「目前這組鍵」與有效值優先 -----------------------------------------------------------------


def test_new_rejection_does_not_invalidate_the_old_active(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_header(db, paper_id, "zh_summary", ZH_CONTENT)
    set_abstract(db, paper_id, "We now study something else entirely.")
    run_id = insert_run(db, paper_id, "zh_summary", input_sha256=_current_sha(database, paper_id))
    insert_header(db, paper_id, "zh_summary", status="rejected", run_id=run_id)

    block = _block(database, paper_id)

    assert block is not None
    assert block.state is AiBlockState.READY
    assert isinstance(block.value, ZhSummaryValue)
    assert block.value.research_question == ZH_CONTENT["research_question"]


def test_rejections_and_runs_for_other_keys_do_not_count(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    current = _current_sha(database, paper_id)
    # 舊輸入、舊 prompt 版本、評估、別的模型的拒收；別的模型、舊輸入、評估的互動執行中：都不是
    # 目前這組鍵的平常流程。
    for run_id in (
        insert_run(db, paper_id, "zh_summary"),
        insert_run(db, paper_id, "zh_summary", input_sha256=current, prompt_version=2),
        insert_run(db, paper_id, "zh_summary", input_sha256=current, eval_run_id=uuid.uuid4()),
        insert_run(db, paper_id, "zh_summary", input_sha256=current, model="other-model"),
    ):
        insert_header(db, paper_id, "zh_summary", status="rejected", run_id=run_id)
    insert_run(
        db, paper_id, "zh_summary", input_sha256=current, model="other-model", status="running"
    )
    insert_run(db, paper_id, "zh_summary", status="running", call_mode="interactive")
    insert_run(
        db,
        paper_id,
        "zh_summary",
        input_sha256=current,
        status="running",
        call_mode="interactive",
        eval_run_id=uuid.uuid4(),
    )
    # 暫時性失敗沒有結果：等下一次排程。
    insert_run(db, paper_id, "zh_summary", input_sha256=current, status="failed")

    assert _state(_block(database, paper_id)) is AiBlockState.PENDING_BATCH


def test_classification_human_values_and_provenance(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_classification(db, paper_id, ["erp"], ["survey_sem"])
    fix(database, paper_id, TaxonomyField.TOPIC, ["supply_chain"])

    block = _block(database, paper_id, AiProduct.CLASSIFICATION)

    assert block is not None
    assert block.state is AiBlockState.READY
    assert isinstance(block.value, ClassificationValue)
    assert block.value.topic is not None
    assert block.value.method is not None
    assert (block.value.topic.keys, block.value.topic.human_verified) == (("supply_chain",), True)
    assert (block.value.method.keys, block.value.method.human_verified) == (("survey_sem",), False)
    assert block.provenance is not None
    assert block.provenance.model == MODEL
    assert block.provenance.human_verified is False

    fix(database, paper_id, TaxonomyField.METHOD, ["case_study"])

    verified = _block(database, paper_id, AiProduct.CLASSIFICATION)
    assert verified is not None
    assert verified.provenance is not None
    assert verified.provenance.human_verified is True


def test_classification_with_only_a_human_value_is_ready_without_model(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    fix(database, paper_id, TaxonomyField.TOPIC, ["erp"])

    block = _block(database, paper_id, AiProduct.CLASSIFICATION)

    assert block is not None
    assert block.state is AiBlockState.READY
    assert isinstance(block.value, ClassificationValue)
    assert block.value.method is None
    assert block.provenance is not None
    assert (block.provenance.model, block.provenance.input_kind) == (None, None)
    # 只修正了主題、方法沒有值：兩欄不都是人工值，不算經人工核對（ADR-0035 保守解讀第 2、5 項）。
    assert block.provenance.human_verified is False

    fix(database, paper_id, TaxonomyField.METHOD, ["case_study"])

    both = _block(database, paper_id, AiProduct.CLASSIFICATION)
    assert both is not None
    assert both.provenance is not None
    assert (both.provenance.model, both.provenance.input_kind) == (None, None)
    assert both.provenance.human_verified is True


def test_research_model_empty_and_ready(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    counts = {
        "is_review_or_meta_analysis": True,
        "dropped_context_value_count": 0,
        "unsupported_context_value_count": 0,
        "corrected_support_count": 0,
    }
    insert_header(db, paper_id, "extract_model", counts)

    assert _state(_block(database, paper_id, AiProduct.RESEARCH_MODEL)) is AiBlockState.EMPTY

    insert_header(db, paper_id, "extract_model", {**counts, "sample_size": 120})
    block = _block(database, paper_id, AiProduct.RESEARCH_MODEL)

    assert block is not None
    assert block.state is AiBlockState.READY
    assert isinstance(block.value, ResearchModelValue)
    assert (block.value.sample_size, block.value.constructs) == (120, ())


# --- 可見範圍與參數 -------------------------------------------------------------------------------


def test_invisible_or_missing_paper_is_none(
    db: TestDatabase, database: Database, paper_id: uuid.UUID
) -> None:
    insert_header(db, paper_id, "zh_summary", ZH_CONTENT)

    # 還沒進已發佈的快照：示範站看不到。
    assert _block(database, paper_id, viewer=PublicViewer()) is None
    assert _block(database, uuid.uuid4()) is None
    member = MemberViewer(member_id=uuid.uuid4(), is_admin=False)
    assert _state(_block(database, paper_id, viewer=member)) is AiBlockState.READY
    execute(db, "UPDATE paper SET in_scope = false, lab_visible = false WHERE id = %s", paper_id)
    assert _block(database, paper_id, viewer=member) is None


def test_settings_must_match_product(database: Database, paper_id: uuid.UUID) -> None:
    with database.transaction() as connection, pytest.raises(ValueError, match="taxonomy"):
        find_ai_block(
            SYSTEM, connection, paper_id, AiProduct.CLASSIFICATION, _settings(AiProduct.ZH_SUMMARY)
        )


# --- get_ai_blocks：真的設定與流程 -------------------------------------------------------------


@pytest.fixture
def config() -> GatewayConfig:
    # 真的 prompt 資料夾與登記（正式的 v1）、真的模型設定。
    return gateway_config(ROOT / "prompts")


@pytest.fixture
def taxonomy() -> Taxonomy:
    return load_taxonomy({"RADAR_CONFIG_DIR": str(ROOT / "config")})


@pytest.fixture
def context_values() -> ContextValues:
    return load_context_values({"RADAR_CONFIG_DIR": str(ROOT / "config")})


def _blocks(
    database: Database,
    config: GatewayConfig,
    taxonomy: Taxonomy,
    context_values: ContextValues,
    paper_id: uuid.UUID,
    **budgets: str | None,
) -> dict[AiProduct, AiBlock]:
    blocks = get_ai_blocks(
        SYSTEM,
        database,
        config,
        budget_config(**budgets),
        taxonomy,
        context_values,
        paper_id,
        now=NOW,
    )
    assert blocks is not None
    return blocks


def test_get_ai_blocks_follows_the_real_gateway(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    taxonomy: Taxonomy,
    context_values: ContextValues,
) -> None:
    zh_paper = create_paper(
        database,
        doi="10.1/zh",
        openalex_id="W11",
        title=zh_summary_case.TITLE,
        abstract=zh_summary_case.ABSTRACT,
    )
    run_zh_summary(
        SYSTEM,
        database,
        FakeLlmProvider(
            [fake_completion(json.dumps(zh_summary_case.GOOD, ensure_ascii=False), usage=USAGE)]
        ),
        config,
        budget_config(),
        load_zh_summary_rules({"RADAR_CONFIG_DIR": str(ROOT / "config")}),
        zh_paper,
        clock=lambda: NOW,
    )
    rejected_paper = create_paper(
        database,
        doi="10.1/classify",
        openalex_id="W12",
        title=classification_case.TITLE,
        abstract=classification_case.ABSTRACT,
    )
    run_classification(
        SYSTEM,
        database,
        FakeLlmProvider([fake_completion("not json", usage=USAGE)]),
        config,
        budget_config(),
        taxonomy,
        rejected_paper,
        clock=lambda: NOW,
    )
    run_research_model(
        SYSTEM,
        database,
        FakeLlmProvider([fake_completion("not json", usage=USAGE)]),
        config,
        budget_config(),
        context_values,
        rejected_paper,
        clock=lambda: NOW,
    )
    model_paper = create_paper(
        database,
        doi="10.1/model",
        openalex_id="W13",
        title=research_model_case.TITLE,
        abstract=research_model_case.ABSTRACT,
    )
    run_research_model(
        SYSTEM,
        database,
        FakeLlmProvider([fake_completion(json.dumps(research_model_case.GOOD), usage=USAGE)]),
        config,
        budget_config(),
        context_values,
        model_paper,
        clock=lambda: NOW,
    )

    zh_blocks = _blocks(database, config, taxonomy, context_values, zh_paper)
    rejected_blocks = _blocks(database, config, taxonomy, context_values, rejected_paper)
    model_blocks = _blocks(database, config, taxonomy, context_values, model_paper)

    assert zh_blocks[AiProduct.ZH_SUMMARY].state is AiBlockState.READY
    assert zh_blocks[AiProduct.CLASSIFICATION].state is AiBlockState.PENDING_BATCH
    # 閘門寫的拒收就是 get_ai_blocks 算出來的「目前這組鍵」。
    assert rejected_blocks[AiProduct.CLASSIFICATION].state is AiBlockState.FAILED
    # 研究模型的鍵另外帶情境值清單版本。
    assert rejected_blocks[AiProduct.RESEARCH_MODEL].state is AiBlockState.FAILED
    model = model_blocks[AiProduct.RESEARCH_MODEL]
    assert model.state is AiBlockState.READY
    assert isinstance(model.value, ResearchModelValue)
    (hypothesis,) = model.value.hypotheses
    assert hypothesis.beta == Decimal("0.42")
    assert [item.name_en for item in model.value.constructs] == ["Perceived usefulness", "Trust"]
    assert model.provenance is not None
    assert model.provenance.model == config.models.task_model(LlmTask.EXTRACT_MODEL).model


def test_rerun_classification_keeps_the_human_topic(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    taxonomy: Taxonomy,
    context_values: ContextValues,
) -> None:
    """FR-4：真的流程重跑分類（新的 active、舊的 superseded）後，人工修正的主題仍是有效值，
    沒修正的方法換成新的 AI 值。"""
    paper = create_paper(
        database,
        doi="10.1/rerun",
        openalex_id="W21",
        title=classification_case.TITLE,
        abstract=classification_case.ABSTRACT,
    )

    def classify(response: dict[str, list[str]]) -> None:
        run_classification(
            SYSTEM,
            database,
            FakeLlmProvider([fake_completion(json.dumps(response), usage=USAGE)]),
            config,
            budget_config(),
            taxonomy,
            paper,
            clock=lambda: NOW,
        )

    classify(classification_case.GOOD)
    fix(database, paper, TaxonomyField.TOPIC, ["e_commerce"])
    set_abstract(db, paper, "We interviewed managers at three retailers about ERP rollouts.")
    classify({"topics": ["erp"], "methods": ["interview_case_study"]})

    assert [
        row[0]
        for row in execute(
            db,
            "SELECT status FROM classification WHERE paper_id = %s ORDER BY created_at, id",
            paper,
        )
    ] == ["superseded", "active"]
    assert sorted(
        execute(
            db,
            "SELECT field, keys, human_verified FROM effective_classification WHERE paper_id = %s",
            paper,
        )
    ) == [("method", ["interview_case_study"], False), ("topic", ["e_commerce"], True)]
    block = _blocks(database, config, taxonomy, context_values, paper)[AiProduct.CLASSIFICATION]
    assert block.state is AiBlockState.READY
    assert isinstance(block.value, ClassificationValue)
    assert block.value.topic is not None
    assert block.value.method is not None
    assert (block.value.topic.keys, block.value.topic.human_verified) == (("e_commerce",), True)
    assert (block.value.method.keys, block.value.method.human_verified) == (
        ("interview_case_study",),
        False,
    )


def test_get_ai_blocks_reads_the_budget(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    taxonomy: Taxonomy,
    context_values: ContextValues,
    paper_id: uuid.UUID,
) -> None:
    insert_header(db, paper_id, "zh_summary", ZH_CONTENT)

    blocks = _blocks(database, config, taxonomy, context_values, paper_id)
    assert {product: block.state for product, block in blocks.items()} == {
        AiProduct.ZH_SUMMARY: AiBlockState.READY,
        AiProduct.CLASSIFICATION: AiBlockState.PENDING_BATCH,
        AiProduct.RESEARCH_MODEL: AiBlockState.PENDING_BATCH,
    }

    # 某個功能用完自己的預算只擋那個功能。
    insert_ledger(db, paper_id, LlmTask.CLASSIFY, "1.00", NOW)
    blocks = _blocks(database, config, taxonomy, context_values, paper_id, classify="1.00")
    assert blocks[AiProduct.CLASSIFICATION].state is AiBlockState.DEFERRED_BUDGET
    assert blocks[AiProduct.RESEARCH_MODEL].state is AiBlockState.PENDING_BATCH

    # 本月到熔斷線：沒有有效值的都延後，有效值照常顯示。
    insert_ledger(db, paper_id, LlmTask.EXTRACT_MODEL, "50.00", NOW)
    blocks = _blocks(database, config, taxonomy, context_values, paper_id)
    assert {product: block.state for product, block in blocks.items()} == {
        AiProduct.ZH_SUMMARY: AiBlockState.READY,
        AiProduct.CLASSIFICATION: AiBlockState.DEFERRED_BUDGET,
        AiProduct.RESEARCH_MODEL: AiBlockState.DEFERRED_BUDGET,
    }


def test_get_ai_blocks_returns_none_for_invisible_paper(
    database: Database,
    config: GatewayConfig,
    taxonomy: Taxonomy,
    context_values: ContextValues,
    paper_id: uuid.UUID,
) -> None:
    for viewer, target in ((PublicViewer(), paper_id), (SYSTEM, uuid.uuid4())):
        assert (
            get_ai_blocks(
                viewer,
                database,
                config,
                budget_config(),
                taxonomy,
                context_values,
                target,
                now=NOW,
            )
            is None
        )
