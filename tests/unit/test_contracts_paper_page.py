"""論文頁的共用形狀（`domain/contracts.py`，Story 3.6、ADR-0048）與層間資料
（`domain/paper_page.py`）。

- 時間一律轉成 UTC、JSON 結尾 `Z`；沒有時區的不收。
- `Decimal` 的 JSON 是字串、不用科學記號；NaN 不收。
- 三種 AI 區塊的信封由 `AiBlock` 轉換，不變式（`ready` 才有值、`ready`／`empty` 才有來歷、分類不會是
  `empty`）照 `domain/ai_blocks.py` 檢查，直接建 JSON 形狀時一樣擋。
- 書目的 DOI／OpenAlex 連結由識別碼產生；全文連結的開放取用連結一律 null。
- 顯示名稱照設定檔的順序、不含 `unclassified`；設定值的時區是 `Asia/Taipei`。
資料全是測試自己編的。
"""

import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from radar.config import load_domain_config, load_taxonomy
from radar.domain import contracts
from radar.domain.ai_blocks import (
    AiBlock,
    AiBlockState,
    AiProduct,
    ClassificationFieldValue,
    ClassificationValue,
    ConstructValue,
    HypothesisValue,
    Provenance,
    ResearchModelValue,
    ZhSummaryValue,
)
from radar.domain.ai_outputs import CurrentAbstract, InputKind
from radar.domain.contracts import (
    ClassificationBlock,
    ClientConfig,
    DisplayNames,
    FullTextLinks,
    PaperAbstract,
    PaperBiblio,
    ResearchModelBlock,
    SnapshotRef,
    ZhSummaryBlock,
    build_client_config,
    build_display_names,
    doi_url,
    openalex_url,
)
from radar.domain.domain_config import Journal, JournalsConfig
from radar.domain.judgments import SummaryField
from radar.domain.llm_schemas import EvidenceType, SupportLevel
from radar.domain.paper_page import PaperPage, PaperPageFacts, journal_key_for
from radar.domain.papers import AbstractSource, PaperOrigin
from radar.domain.snapshot import Snapshot, SnapshotStatus
from radar.domain.taxonomy import UNCLASSIFIED, TaxonomyField
from tests.llm_support import ROOT

TAIPEI = timezone(timedelta(hours=8))
AT = datetime(2026, 11, 15, 12, 0, tzinfo=TAIPEI)  # = 04:00Z
AT_Z = "2026-11-15T04:00:00Z"
PROVENANCE = Provenance(
    model="claude-haiku-5-5", generated_at=AT, input_kind=InputKind.ABSTRACT, human_verified=False
)
ZH = ZhSummaryValue(
    zh_summary_id=uuid.UUID(int=1),
    research_question="遠距團隊如何採用協作平台？",
    method="問卷調查",
    theory=None,
    findings=None,
    reported_fields=(SummaryField.METHOD,),
)
TOPIC = ClassificationFieldValue(
    keys=("collaboration", "erp"), human_verified=True, fix_member_count=2, members_disagree=True
)
CLASSIFICATION = ClassificationValue(topic=TOPIC, method=None, fix_member_count=2)
TRUST = uuid.UUID(int=0x71)
LOYALTY = uuid.UUID(int=0x72)
RESEARCH_MODEL = ResearchModelValue(
    research_model_id=uuid.UUID(int=3),
    is_review_or_meta_analysis=False,
    country="taiwan",
    industry=None,
    research_design=None,
    unit_of_analysis=None,
    sample_size=200,
    limitations=None,
    future_research=None,
    constructs=(
        ConstructValue(
            construct_id=TRUST,
            name_en="Trust",
            label_zh="信任",
            definition=None,
            scale_source=None,
            item_location=None,
            item_count=4,
            cronbach_alpha=Decimal("0.910"),
            composite_reliability=None,
            ave=None,
        ),
        ConstructValue(
            construct_id=LOYALTY,
            name_en="Loyalty",
            label_zh="忠誠度",
            definition=None,
            scale_source=None,
            item_location=None,
            item_count=None,
            cronbach_alpha=None,
            composite_reliability=None,
            ave=None,
        ),
    ),
    hypotheses=(
        HypothesisValue(
            ordinal=1,
            sentence="Trust increases loyalty.",
            cause_construct_id=TRUST,
            effect_construct_id=LOYALTY,
            evidence_type=EvidenceType.EMPIRICAL_TEST,
            support=SupportLevel.SUPPORTED,
            beta=Decimal("-0.10000000000000001"),
            p_value=Decimal("1E-7"),
            sample_size=200,
            effect_type=None,
            result_quote="trust increased loyalty",
        ),
    ),
)
FACTS = PaperPageFacts(
    paper_id=uuid.UUID(int=0xBEEF),
    origin=PaperOrigin.TRACKED,
    title="A study of information systems",
    authors=("Alice Example", "Bob Example"),
    publication_year=2025,
    journal_issn_l="0276-7783",
    volume="49",
    issue="2",
    first_page="1",
    last_page="20",
    cited_by_count=7,
    in_scope=True,
    is_retracted=False,
    doi="10.1/abc",
    openalex_id="W1",
    abstract=CurrentAbstract(
        id=uuid.UUID(int=9), source=AbstractSource.OPENALEX, content="We study teams."
    ),
)
SNAPSHOT = Snapshot(
    id=uuid.UUID(int=5),
    number=3,
    weekly_run_id=uuid.UUID(int=6),
    status=SnapshotStatus.PUBLISHED,
    as_of=AT,
    created_at=AT - timedelta(minutes=5),
)
JOURNALS = JournalsConfig(
    start_year=2016,
    journals=(
        Journal(key="misq", name="MIS Quarterly", issn_l="0276-7783"),
        Journal(key="isr", name="Information Systems Research", issn_l="1047-7047"),
    ),
)


def _json(model: BaseModel) -> Any:
    return json.loads(model.model_dump_json())


# --- 時間、快照、來歷 ---------------------------------------------------------------------------


def test_snapshot_ref_is_number_and_utc_as_of() -> None:
    assert _json(SnapshotRef.from_snapshot(SNAPSHOT)) == {"number": 3, "as_of": AT_Z}


def test_snapshot_ref_rejects_an_unpublished_snapshot() -> None:
    building = dataclasses.replace(SNAPSHOT, status=SnapshotStatus.BUILDING, as_of=None)

    with pytest.raises(ValueError, match="已發佈"):
        SnapshotRef.from_snapshot(building)


@pytest.mark.parametrize(
    "fields",
    [
        {"number": 0, "as_of": AT},
        {"number": True, "as_of": AT},
        {"number": "3", "as_of": AT},
        {"number": 3, "as_of": datetime(2026, 11, 15, 4, 0)},  # 沒有時區
        {"number": 3, "as_of": AT, "extra": 1},
    ],
)
def test_snapshot_ref_rejects_bad_shapes(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SnapshotRef.model_validate(fields)


def test_provenance_is_converted_to_utc() -> None:
    assert _json(contracts.Provenance.from_domain(PROVENANCE)) == {
        "model": "claude-haiku-5-5",
        "generated_at": AT_Z,
        "input_kind": "abstract",
        "human_verified": False,
    }


def test_provenance_of_a_human_value_has_no_model() -> None:
    human = Provenance(model=None, generated_at=AT, input_kind=None, human_verified=True)

    assert _json(contracts.Provenance.from_domain(human)) == {
        "model": None,
        "generated_at": AT_Z,
        "input_kind": None,
        "human_verified": True,
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"model": "m", "generated_at": AT, "input_kind": None, "human_verified": False},
        {"model": None, "generated_at": AT, "input_kind": "abstract", "human_verified": False},
        {"model": "", "generated_at": AT, "input_kind": "abstract", "human_verified": False},
        {"model": "m", "generated_at": AT, "input_kind": "abstract", "human_verified": "false"},
    ],
)
def test_provenance_rejects_inconsistent_fields(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        contracts.Provenance.model_validate(fields)


# --- 三種 AI 區塊的信封 ------------------------------------------------------------------------


def test_zh_summary_ready_block() -> None:
    block = AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.READY, ZH, PROVENANCE)

    assert _json(ZhSummaryBlock.from_block(block)) == {
        "state": "ready",
        "value": {
            "zh_summary_id": str(ZH.zh_summary_id),
            "research_question": "遠距團隊如何採用協作平台？",
            "method": "問卷調查",
            "theory": None,
            "findings": None,
            "reported_fields": ["method"],
        },
        "provenance": {
            "model": "claude-haiku-5-5",
            "generated_at": AT_Z,
            "input_kind": "abstract",
            "human_verified": False,
        },
    }


@pytest.mark.parametrize(
    "state",
    [
        AiBlockState.PENDING_INTERACTIVE,
        AiBlockState.PENDING_BATCH,
        AiBlockState.DEFERRED_BUDGET,
        AiBlockState.NO_INPUT,
        AiBlockState.FAILED,
        AiBlockState.NOT_APPLICABLE,
    ],
)
def test_blocks_without_output_have_null_value_and_provenance(state: AiBlockState) -> None:
    for product, model in (
        (AiProduct.ZH_SUMMARY, ZhSummaryBlock),
        (AiProduct.CLASSIFICATION, ClassificationBlock),
        (AiProduct.RESEARCH_MODEL, ResearchModelBlock),
    ):
        converted = model.from_block(AiBlock(product, state))
        # 沒有預計時間或進度：信封只有這三個欄位。
        assert _json(converted) == {"state": state.value, "value": None, "provenance": None}


def test_empty_block_carries_provenance_only() -> None:
    block = AiBlock(AiProduct.RESEARCH_MODEL, AiBlockState.EMPTY, provenance=PROVENANCE)

    body = _json(ResearchModelBlock.from_block(block))

    assert body["state"] == "empty"
    assert body["value"] is None
    assert body["provenance"]["generated_at"] == AT_Z


def test_classification_block_has_the_fields_for_verified_and_disagreement() -> None:
    block = AiBlock(AiProduct.CLASSIFICATION, AiBlockState.READY, CLASSIFICATION, PROVENANCE)

    assert _json(ClassificationBlock.from_block(block))["value"] == {
        "topic": {
            "keys": ["collaboration", "erp"],
            "human_verified": True,
            "fix_member_count": 2,
            "members_disagree": True,
        },
        "method": None,
        "fix_member_count": 2,
    }


def test_research_model_numbers_are_strings_without_exponent() -> None:
    block = AiBlock(AiProduct.RESEARCH_MODEL, AiBlockState.READY, RESEARCH_MODEL, PROVENANCE)

    value = _json(ResearchModelBlock.from_block(block))["value"]

    assert value["constructs"][0]["cronbach_alpha"] == "0.910"
    assert value["constructs"][1]["cronbach_alpha"] is None
    hypothesis = value["hypotheses"][0]
    assert hypothesis["beta"] == "-0.10000000000000001"
    assert hypothesis["p_value"] == "0.0000001"
    assert hypothesis["cause_construct_id"] == str(TRUST)
    assert value["country"] == "taiwan"
    assert value["sample_size"] == 200


def test_block_round_trips_through_json() -> None:
    block = AiBlock(AiProduct.RESEARCH_MODEL, AiBlockState.READY, RESEARCH_MODEL, PROVENANCE)
    converted = ResearchModelBlock.from_block(block)

    again = ResearchModelBlock.model_validate_json(converted.model_dump_json())

    assert again == converted
    assert again.value is not None
    assert again.value.to_domain() == RESEARCH_MODEL


def test_from_block_rejects_the_wrong_product() -> None:
    block = AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.PENDING_BATCH)

    with pytest.raises(ValueError, match="classification"):
        ClassificationBlock.from_block(block)


def _zh_value_json() -> dict[str, object]:
    return {
        "zh_summary_id": str(uuid.UUID(int=1)),
        "research_question": "問題",
        "method": None,
        "theory": None,
        "findings": None,
        "reported_fields": [],
    }


def _provenance_json() -> dict[str, object]:
    return {"model": "m", "generated_at": AT_Z, "input_kind": "abstract", "human_verified": False}


@pytest.mark.parametrize(
    ("model", "body"),
    [
        # ready 沒有值、沒有來歷
        (ZhSummaryBlock, {"state": "ready", "value": None, "provenance": None}),
        # 還在排隊卻帶來歷
        (ZhSummaryBlock, {"state": "pending_batch", "value": None, "provenance": "P"}),
        # empty 帶值
        (ZhSummaryBlock, {"state": "empty", "value": "V", "provenance": "P"}),
        # 分類不會是 empty
        (ClassificationBlock, {"state": "empty", "value": None, "provenance": "P"}),
        # 不認得的狀態
        (ZhSummaryBlock, {"state": "almost_ready", "value": None, "provenance": None}),
        # 多一個欄位（例如預計完成時間）
        (ZhSummaryBlock, {"state": "pending_batch", "value": None, "provenance": None, "eta": 3}),
    ],
)
def test_envelope_invariants_hold_for_json_shapes(
    model: type[BaseModel], body: dict[str, object]
) -> None:
    filled = {
        key: _zh_value_json() if value == "V" else _provenance_json() if value == "P" else value
        for key, value in body.items()
    }

    with pytest.raises(ValidationError):
        model.model_validate(filled)


@pytest.mark.parametrize(
    "change",
    [
        {"research_question": None},  # 四欄全空是 empty
        {"reported_fields": ["method", "method"]},
        {"reported_fields": ["findings", "method"]},
    ],
)
def test_zh_summary_content_rejects_bad_values(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        contracts.ZhSummaryContent.model_validate(_zh_value_json() | change)


@pytest.mark.parametrize(
    "fields",
    [
        {"keys": ["b", "a"], "human_verified": False, "fix_member_count": 0},
        {"keys": ["a"], "human_verified": True, "fix_member_count": 0},
        {"keys": ["a"], "human_verified": False, "fix_member_count": -1},
    ],
)
def test_classification_field_rejects_bad_values(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        contracts.ClassificationField.model_validate(fields | {"members_disagree": False})


def test_decimal_text_rejects_nan() -> None:
    construct = contracts.ConstructItem.from_domain(RESEARCH_MODEL.constructs[0])
    body = _json(construct) | {"ave": "NaN"}

    with pytest.raises(ValidationError):
        contracts.ConstructItem.model_validate(body)


# --- 書目、摘要、全文連結 -----------------------------------------------------------------------


def test_biblio_from_facts() -> None:
    assert _json(PaperBiblio.from_facts(FACTS, "misq")) == {
        "id": str(FACTS.paper_id),
        "title": "A study of information systems",
        "authors": ["Alice Example", "Bob Example"],
        "publication_year": 2025,
        "journal": "misq",
        "journal_issn_l": "0276-7783",
        "volume": "49",
        "issue": "2",
        "first_page": "1",
        "last_page": "20",
        "doi": "10.1/abc",
        "doi_url": "https://doi.org/10.1/abc",
        "openalex_id": "W1",
        "openalex_url": "https://openalex.org/W1",
        "cited_by_count": 7,
        "in_scope": True,
        "origin": "tracked",
        "is_retracted": False,
    }


def test_biblio_without_identifiers_has_null_links() -> None:
    facts = dataclasses.replace(FACTS, doi=None, journal_issn_l=None)

    body = _json(PaperBiblio.from_facts(facts, None))

    assert (body["doi"], body["doi_url"], body["journal"]) == (None, None, None)
    assert _json(FullTextLinks.from_facts(facts)) == {"doi_url": None, "open_access_url": None}


def test_doi_url_encodes_special_characters() -> None:
    assert doi_url("10.1002/(sici)1097-4571<1>#x?y") == (
        "https://doi.org/10.1002/%28sici%291097-4571%3C1%3E%23x%3Fy"
    )
    assert openalex_url("W2741809807") == "https://openalex.org/W2741809807"


def test_biblio_rejects_links_that_do_not_match_the_identifiers() -> None:
    body = _json(PaperBiblio.from_facts(FACTS, "misq")) | {"doi_url": "https://example.com/x"}

    with pytest.raises(ValidationError, match="doi_url"):
        PaperBiblio.model_validate(body)


def test_full_text_links_have_no_open_access_url_yet() -> None:
    assert _json(FullTextLinks.from_facts(FACTS)) == {
        "doi_url": "https://doi.org/10.1/abc",
        "open_access_url": None,
    }


def test_abstract_has_content_and_source() -> None:
    assert FACTS.abstract is not None
    assert _json(PaperAbstract.from_current(FACTS.abstract)) == {
        "content": "We study teams.",
        "source": "openalex",
    }


def test_journal_key_is_found_by_issn_l() -> None:
    assert journal_key_for(JOURNALS, "1047-7047") == "isr"
    assert journal_key_for(JOURNALS, "9999-9994") is None
    assert journal_key_for(JOURNALS, None) is None


def test_paper_page_checks_block_products_and_snapshot() -> None:
    pending = {product: AiBlock(product, AiBlockState.PENDING_BATCH) for product in AiProduct}
    page = PaperPage(
        facts=FACTS,
        journal_key="misq",
        zh_summary=pending[AiProduct.ZH_SUMMARY],
        classification=pending[AiProduct.CLASSIFICATION],
        research_model=pending[AiProduct.RESEARCH_MODEL],
        snapshot=SNAPSHOT,
    )
    assert page.snapshot is SNAPSHOT

    with pytest.raises(ValueError, match="classification"):
        dataclasses.replace(page, classification=pending[AiProduct.ZH_SUMMARY])
    with pytest.raises(ValueError, match="已發佈"):
        dataclasses.replace(
            page, snapshot=dataclasses.replace(SNAPSHOT, status=SnapshotStatus.BUILDING)
        )


# --- 顯示名稱與設定值 ---------------------------------------------------------------------------


def test_display_names_follow_the_config_order_without_unclassified() -> None:
    environ = {"RADAR_CONFIG_DIR": str(ROOT / "config")}
    journals = load_domain_config(environ).journals
    taxonomy = load_taxonomy(environ)

    names = build_display_names(journals, taxonomy)

    assert [(item.key, item.name) for item in names.journals] == [
        (journal.key, journal.name) for journal in journals.journals
    ]
    assert [item.key for item in names.topics] == list(taxonomy.keys(TaxonomyField.TOPIC))
    assert [item.key for item in names.methods] == list(taxonomy.keys(TaxonomyField.METHOD))
    all_keys = [item.key for item in (*names.journals, *names.topics, *names.methods)]
    assert UNCLASSIFIED not in all_keys
    assert set(_json(names)) == {"journals", "topics", "methods"}
    assert set(_json(names)["topics"][0]) == {"key", "name"}


def test_client_config_is_the_taipei_timezone() -> None:
    assert _json(build_client_config()) == {"timezone": "Asia/Taipei", "library_page_size": 50}


@pytest.mark.parametrize(
    "model",
    [
        SnapshotRef,
        contracts.Provenance,
        contracts.ZhSummaryContent,
        contracts.ClassificationField,
        contracts.ClassificationContent,
        contracts.ConstructItem,
        contracts.HypothesisItem,
        contracts.ResearchModelContent,
        ZhSummaryBlock,
        ClassificationBlock,
        ResearchModelBlock,
        PaperBiblio,
        PaperAbstract,
        FullTextLinks,
        contracts.DisplayName,
        DisplayNames,
        ClientConfig,
    ],
)
def test_every_paper_page_model_forbids_extra_fields(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid"


def test_naive_time_is_rejected_and_other_offsets_become_utc() -> None:
    with pytest.raises(ValidationError):
        contracts.Provenance.model_validate(
            _provenance_json() | {"generated_at": datetime(2026, 11, 15, 4, 0)}
        )
    converted = contracts.Provenance.model_validate(_provenance_json() | {"generated_at": AT})
    assert converted.generated_at.tzinfo is UTC
    assert converted.generated_at == AT
