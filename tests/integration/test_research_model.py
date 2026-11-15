"""研究模型擷取對真的 PostgreSQL（Story 2.6）：真的 prompt（`prompts/extract_model/v1.md`、正式
登記）、真的情境值清單（`config/context_values.yaml`）＋模型替身，spec 2.6 的 I/O 表每一列。

- 正常：表頭 `active`、`en`、`abstract`、計數 0；構面以正規化名稱為唯一鍵、新構面 canonical 指向
  自己；構面提及與假設指向原本的構面。
- 情境值清單外、國家推測、支持與否改值、同一句重複：不拒收，改值並計數。
- 原句捏造、數字超出範圍或捏造、構面不對、夾標記、同一句衝突、格式錯：整筆拒收、沒有內容列。
- 沒有摘要 → `no_input`；被排除、手填 → `not_applicable`：不呼叫閘門、不寫任何列。
- 兩篇論文提到同一個構面只有一列 `construct`；同一份輸入再跑不重付；清單升版後重跑、舊的
  `superseded`；評估的 `candidate` 也寫內容。

模型一律用 `FakeLlmProvider`（不連網路）；標題、摘要、回應全是測試自己編的。
"""

import copy
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from radar.adapters.fake import FakeLlmProvider, fake_completion
from radar.config import load_context_values
from radar.domain.ai_outputs import (
    GatewayConfig,
    InputKind,
    LlmTask,
    OutputLang,
    ResultStatus,
)
from radar.domain.budget import BudgetConfig
from radar.domain.context_values import ContextValues
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import hypothesis_sentence_sha256
from radar.domain.llm import LlmUsage, system_prompt
from radar.domain.llm_schemas import ResearchModelOutput
from radar.domain.permissions import PermissionDeniedError
from radar.domain.processing import SkipState
from radar.domain.validators.research_model import ResearchModelValidator, quote_key
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.services.ai_results import record_llm_result
from radar.services.llm_gateway import GatewayOutcome, LlmJob, run_llm_job
from radar.services.research_model import ResearchModelRun, run_research_model
from tests.database import TestDatabase, migrate
from tests.llm_support import (
    ROOT,
    budget_config,
    create_paper,
    execute,
    gateway_config,
    table_counts,
)

SYSTEM = SystemViewer()
NOW = datetime(2026, 11, 15, 4, 0, tzinfo=UTC)
USAGE = LlmUsage(input_tokens=1500, output_tokens=400)
TITLE = "Perceived usefulness and trust in mobile banking adoption"
ABSTRACT = (
    "Based on a survey of 200 bank customers in Taiwan, we test a model of mobile banking "
    "adoption. Perceived usefulness is defined as the degree to which users believe the app "
    "improves their banking. Perceived usefulness positively affects trust (β = 0.42, p = .01). "
    "Interviews with managers suggest that trust shapes loyalty. "
    "Future research should test other countries."
)
SENTENCE = "Perceived usefulness positively affects trust (β = 0.42, p = .01)."
GOOD: dict[str, Any] = {
    "is_review_or_meta_analysis": False,
    "context": {
        "country": "tw",
        "industry": "financial_services",
        "research_design": "survey",
        "unit_of_analysis": "individual",
        "sample_size": 200,
    },
    "limitations": None,
    "future_research": "Future research should test other countries.",
    "constructs": [
        {
            "name_en": "Perceived usefulness",
            "label_zh": "知覺有用性",
            "definition": "the degree to which users believe the app improves their banking",
            "measurement": None,
        },
        {"name_en": "Trust", "label_zh": "信任", "definition": None, "measurement": None},
    ],
    "hypotheses": [
        {
            "sentence": SENTENCE,
            "cause": "Perceived usefulness",
            "effect": "Trust",
            "evidence_type": "empirical_test",
            "support": "supported",
            "result_quote": SENTENCE,
            "statistics": {
                "beta": 0.42,
                "p_value": 0.01,
                "sample_size": 200,
                "effect_type": None,
            },
        }
    ],
}
COUNTS = ("dropped", "unsupported", "corrected", "cleared")
DETAIL_TABLES = ("construct", "construct_mention", "hypothesis")


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=4)


@pytest.fixture
def config() -> GatewayConfig:
    # 真的 prompt 資料夾，登記表不換（正式的 extract_model v1）。
    return gateway_config(ROOT / "prompts")


@pytest.fixture
def context_values() -> ContextValues:
    return load_context_values({"RADAR_CONFIG_DIR": str(ROOT / "config")})


@pytest.fixture
def paper_id(database: Database) -> uuid.UUID:
    return create_paper(database, title=TITLE, abstract=ABSTRACT)


def _good() -> dict[str, Any]:
    return copy.deepcopy(GOOD)


def _reply(fields: dict[str, Any] | str | None, **kwargs: Any) -> Any:
    text = fields if fields is None or isinstance(fields, str) else json.dumps(fields)
    return fake_completion(text, usage=USAGE, **kwargs)


def _run(
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
    provider: FakeLlmProvider,
    *,
    budget: BudgetConfig | None = None,
) -> ResearchModelRun:
    return run_research_model(
        SYSTEM,
        database,
        provider,
        config,
        budget or budget_config(),
        context_values,
        paper_id,
        clock=lambda: NOW,
    )


_HEADER_COLUMNS = (
    "id",
    "status",
    "lang",
    "input_kind",
    "abstract_id",
    "reject_reason",
    "is_review_or_meta_analysis",
    "country",
    "industry",
    "research_design",
    "unit_of_analysis",
    "sample_size",
    "limitations",
    "future_research",
    "dropped_context_value_count",
    "unsupported_context_value_count",
    "corrected_support_count",
    "cleared_value_count",
)


def _headers(db: TestDatabase, paper_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    sql = f"SELECT {', '.join(_HEADER_COLUMNS)} FROM research_model"
    params: tuple[object, ...] = ()
    if paper_id is not None:
        sql += " WHERE paper_id = %s"
        params = (paper_id,)
    rows = execute(db, sql + " ORDER BY created_at, id", *params)
    headers = [dict(zip(_HEADER_COLUMNS, row, strict=True)) for row in rows]
    for header in headers:
        header["dropped"] = header.pop("dropped_context_value_count")
        header["unsupported"] = header.pop("unsupported_context_value_count")
        header["corrected"] = header.pop("corrected_support_count")
        header["cleared"] = header.pop("cleared_value_count")
    return headers


def _mentions(db: TestDatabase, header_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return execute(
        db,
        "SELECT c.normalized_name, m.name_en, m.label_zh, m.definition"
        " FROM construct_mention m JOIN construct c ON c.id = m.construct_id"
        " WHERE m.research_model_id = %s ORDER BY c.normalized_name",
        header_id,
    )


def _hypotheses(db: TestDatabase, header_id: uuid.UUID) -> list[dict[str, Any]]:
    columns = ("ordinal", "sentence", "cause", "effect", "evidence_type", "support", "beta", "p")
    rows = execute(
        db,
        "SELECT h.ordinal, h.sentence, cause.normalized_name, effect.normalized_name,"
        " h.evidence_type, h.support, h.beta::text, h.p_value::text"
        " FROM hypothesis h"
        " JOIN construct cause ON cause.id = h.cause_construct_id"
        " JOIN construct effect ON effect.id = h.effect_construct_id"
        " WHERE h.research_model_id = %s ORDER BY h.ordinal",
        header_id,
    )
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _detail_counts(db: TestDatabase) -> dict[str, int]:
    return {
        table: int(execute(db, f"SELECT count(*) FROM {table}")[0][0]) for table in DETAIL_TABLES
    }


def _only(db: TestDatabase) -> dict[str, Any]:
    (header,) = _headers(db)
    return header


# --- 正常 ---------------------------------------------------------------------------------------


def test_good_reply_is_stored_as_active_en_with_constructs_and_hypotheses(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
) -> None:
    provider = FakeLlmProvider([_reply(GOOD)])

    run = _run(database, config, context_values, paper_id, provider)

    assert run.plan.input_kind is InputKind.ABSTRACT
    assert run.result is not None
    assert run.result.outcome is GatewayOutcome.ACTIVE
    assert run.result.output == ResearchModelOutput.model_validate(GOOD)
    header = _only(db)
    assert header["status"] == "active"
    assert header["lang"] == "en"
    assert header["input_kind"] == "abstract"
    assert header["abstract_id"] is not None
    assert header["reject_reason"] is None
    assert header["is_review_or_meta_analysis"] is False
    assert (
        header["country"],
        header["industry"],
        header["research_design"],
        header["unit_of_analysis"],
        header["sample_size"],
    ) == ("tw", "financial_services", "survey", "individual", 200)
    assert header["limitations"] is None
    assert header["future_research"] == "Future research should test other countries."
    assert [header[name] for name in COUNTS] == [0, 0, 0, 0]

    # 兩列構面，新構面的 canonical 指向自己。
    constructs = execute(
        db, "SELECT normalized_name, id = canonical_id FROM construct ORDER BY normalized_name"
    )
    assert constructs == [("perceived usefulness", True), ("trust", True)]
    assert _mentions(db, header["id"]) == [
        (
            "perceived usefulness",
            "Perceived usefulness",
            "知覺有用性",
            "the degree to which users believe the app improves their banking",
        ),
        ("trust", "Trust", "信任", None),
    ]
    assert _hypotheses(db, header["id"]) == [
        {
            "ordinal": 1,
            "sentence": SENTENCE,
            "cause": "perceived usefulness",
            "effect": "trust",
            "evidence_type": "empirical_test",
            "support": "supported",
            "beta": "0.42",
            "p": "0.01",
        }
    ]

    # 送出的是正式 prompt 與 ResearchModelOutput 的 schema，設定照 prompt 的設定區；情境值清單在
    # 系統提示裡，文件區塊是標題＋摘要。
    (request,) = provider.requests
    assert request.output_schema is ResearchModelOutput
    assert request.max_tokens == 2560
    assert request.effort == "low"
    assert request.disable_thinking is True
    assert "- tw：" in system_prompt(request)
    assert len(request.content) == 2
    assert execute(
        db,
        "SELECT task, prompt_version, context_values_version, taxonomy_version,"
        " params->'extraction_schema_version' FROM derivation_run",
    ) == [("extract_model", 1, "1", None, 1)]


# --- 不拒收：改值並計數 -------------------------------------------------------------------------


def _change(change: str) -> dict[str, Any]:
    data = _good()
    hypothesis = data["hypotheses"][0]
    match change:
        case "out_of_list":
            data["context"] |= {"country": "usa", "industry": "fintech"}
        case "inferred_country":
            data["context"]["country"] = "cn"
        case "qualitative_supported":
            hypothesis |= {"evidence_type": "qualitative", "statistics": None}
        case "empirical_not_determined":
            hypothesis["support"] = "not_determined"
        case "duplicate":
            data["hypotheses"].append(copy.deepcopy(hypothesis))
        case "blank_text":
            data["future_research"] = "   "
        case "no_result_quote":
            hypothesis["result_quote"] = None
        case "hypothesis_only_result_quote":
            hypothesis["result_quote"] = "Perceived usefulness positively affects trust"
        case "punctuation_only_text":
            data["future_research"] = "-"
    return data


def test_measurement_and_statistics_are_stored_in_their_columns(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
) -> None:
    # 每個量測與統計欄位都寫進自己的欄（值彼此不同，對調或漏寫都抓得到）。
    abstract = (
        ABSTRACT + " Trust was measured with 4 items adapted from Gefen (2003), listed in"
        " Appendix B (Cronbach's alpha = 0.91, CR = 0.93, AVE = 0.78). Among 180 complete"
        " responses, the mediated effect was significant."
    )
    paper = create_paper(database, title=TITLE, abstract=abstract)
    reply = _good()
    reply["constructs"][1]["measurement"] = {
        "scale_source": "Gefen (2003)",
        "item_location": "listed in Appendix B",
        "item_count": 4,
        "cronbach_alpha": 0.91,
        "composite_reliability": 0.93,
        "ave": 0.78,
    }
    reply["hypotheses"][0]["statistics"] = {
        "beta": 0.42,
        "p_value": None,
        "sample_size": 180,
        "effect_type": "mediated effect",
    }

    run = _run(database, config, context_values, paper, FakeLlmProvider([_reply(reply)]))

    assert run.result is not None
    assert run.result.outcome is GatewayOutcome.ACTIVE
    header = _only(db)
    assert execute(
        db,
        "SELECT c.normalized_name, m.scale_source, m.item_location, m.item_count,"
        " m.cronbach_alpha::text, m.composite_reliability::text, m.ave::text"
        " FROM construct_mention m JOIN construct c ON c.id = m.construct_id"
        " WHERE m.research_model_id = %s ORDER BY c.normalized_name",
        header["id"],
    ) == [
        ("perceived usefulness", None, None, None, None, None, None),
        ("trust", "Gefen (2003)", "listed in Appendix B", 4, "0.91", "0.93", "0.78"),
    ]
    assert execute(
        db,
        "SELECT beta::text, p_value, sample_size, effect_type, sentence_sha256, result_quote"
        " FROM hypothesis"
        " WHERE research_model_id = %s",
        header["id"],
    ) == [
        (
            "0.42",
            None,
            180,
            "mediated effect",
            hypothesis_sentence_sha256(quote_key(SENTENCE).strip(" .,;:")),
            SENTENCE,
        )
    ]


def test_p_value_threshold_is_stored_as_null_and_counted(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
) -> None:
    # Codex 審查：摘要只寫 p < .01，模型回 p_value = 0.01 時不存成確切的值。
    threshold = SENTENCE.replace("p = .01", "p < .01")
    paper = create_paper(database, title=TITLE, abstract=ABSTRACT.replace("p = .01", "p < .01"))
    reply = _good()
    reply["hypotheses"][0] |= {"sentence": threshold, "result_quote": threshold}

    run = _run(database, config, context_values, paper, FakeLlmProvider([_reply(reply)]))

    assert run.result is not None
    assert run.result.outcome is GatewayOutcome.ACTIVE
    header = _only(db)
    assert [header[name] for name in COUNTS] == [0, 0, 0, 1]
    (hypothesis,) = _hypotheses(db, header["id"])
    assert (hypothesis["beta"], hypothesis["p"], hypothesis["support"]) == (
        "0.42",
        None,
        "supported",
    )


@pytest.mark.parametrize(
    ("change", "counts", "check"),
    [
        ("out_of_list", [2, 0, 0, 0], {"country": None, "industry": None}),
        ("inferred_country", [0, 1, 0, 0], {"country": None, "industry": "financial_services"}),
        ("qualitative_supported", [0, 0, 1, 0], {}),
        ("empirical_not_determined", [0, 0, 1, 0], {}),
        ("duplicate", [0, 0, 0, 0], {}),
        ("blank_text", [0, 0, 0, 0], {"future_research": None}),
        ("no_result_quote", [0, 0, 1, 0], {}),
        ("hypothesis_only_result_quote", [0, 0, 1, 0], {}),
        ("punctuation_only_text", [0, 0, 0, 1], {"future_research": None}),
    ],
)
def test_reply_is_corrected_and_counted_not_rejected(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
    change: str,
    counts: list[int],
    check: dict[str, Any],
) -> None:
    provider = FakeLlmProvider([_reply(_change(change))])

    run = _run(database, config, context_values, paper_id, provider)

    assert run.result is not None
    assert run.result.outcome is GatewayOutcome.ACTIVE
    header = _only(db)
    assert header["status"] == "active"
    assert [header[name] for name in COUNTS] == counts
    for name, value in check.items():
        assert header[name] == value, name
    (hypothesis,) = _hypotheses(db, header["id"])
    expected_support = {
        "qualitative_supported": "not_determined",
        "empirical_not_determined": "not_stated",
        "no_result_quote": "not_stated",
        "hypothesis_only_result_quote": "not_stated",
    }.get(change, "supported")
    assert hypothesis["support"] == expected_support


# --- 拒收 ---------------------------------------------------------------------------------------


def _reject_case(change: str) -> dict[str, Any] | str:
    data = _good()
    hypothesis = data["hypotheses"][0]
    construct = data["constructs"][1]
    match change:
        case "fabricated_sentence":
            hypothesis["sentence"] = "Trust strongly predicts adoption in every market."
        case "fabricated_definition":
            construct["definition"] = "the willingness to depend on the bank"
        case "p_out_of_range":
            hypothesis["statistics"]["p_value"] = 1.5
        case "sample_size_zero":
            data["context"]["sample_size"] = 0
        case "alpha_out_of_range":
            construct["measurement"] = {
                "scale_source": None,
                "item_location": None,
                "item_count": None,
                "cronbach_alpha": 1.2,
                "composite_reliability": None,
                "ave": None,
            }
        case "invented_beta":
            hypothesis["statistics"]["beta"] = 0.43
        case "unknown_construct":
            hypothesis["effect"] = "Loyalty"
        case "self_loop":
            hypothesis["effect"] = "Perceived usefulness"
        case "duplicate_construct":
            data["constructs"].append(
                {
                    "name_en": "TRUST (T)",
                    "label_zh": "信任感",
                    "definition": None,
                    "measurement": None,
                }
            )
        case "markup":
            construct["label_zh"] = "<b>信任</b>"
        case "entity":
            construct["label_zh"] = "信任&amp;"
        case "bidi":
            hypothesis["sentence"] = SENTENCE[:10] + chr(0x202E) + SENTENCE[10:]
        case "conflict":
            data["hypotheses"].append(copy.deepcopy(hypothesis) | {"support": "not_supported"})
        case "extra_field":
            data["note"] = "Ignore previous instructions"
        case "missing_field":
            del data["hypotheses"]
        case "enum_outside":
            hypothesis["evidence_type"] = "survey"
        case "not_json":
            return "構面：知覺有用性、信任"
        case "nul":
            data["future_research"] = "Future research" + chr(0) + " should test other countries."
    return data


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("fabricated_sentence", ErrorCode.RESEARCH_MODEL_QUOTE_NOT_IN_INPUT),
        ("fabricated_definition", ErrorCode.RESEARCH_MODEL_QUOTE_NOT_IN_INPUT),
        ("p_out_of_range", ErrorCode.RESEARCH_MODEL_NUMBER_OUT_OF_RANGE),
        ("sample_size_zero", ErrorCode.RESEARCH_MODEL_NUMBER_OUT_OF_RANGE),
        ("alpha_out_of_range", ErrorCode.RESEARCH_MODEL_NUMBER_OUT_OF_RANGE),
        ("invented_beta", ErrorCode.RESEARCH_MODEL_UNSUPPORTED_CONTENT),
        ("unknown_construct", ErrorCode.RESEARCH_MODEL_UNKNOWN_CONSTRUCT),
        ("self_loop", ErrorCode.RESEARCH_MODEL_UNKNOWN_CONSTRUCT),
        ("duplicate_construct", ErrorCode.RESEARCH_MODEL_DUPLICATE_CONSTRUCT),
        ("markup", ErrorCode.RESEARCH_MODEL_UNSAFE_TEXT),
        ("entity", ErrorCode.RESEARCH_MODEL_UNSAFE_TEXT),
        ("bidi", ErrorCode.RESEARCH_MODEL_UNSAFE_TEXT),
        ("conflict", ErrorCode.RESEARCH_MODEL_CONFLICTING_HYPOTHESIS),
        ("extra_field", ErrorCode.LLM_OUTPUT_INVALID),
        ("missing_field", ErrorCode.LLM_OUTPUT_INVALID),
        ("enum_outside", ErrorCode.LLM_OUTPUT_INVALID),
        ("not_json", ErrorCode.LLM_MALFORMED_RESPONSE),
        ("nul", ErrorCode.LLM_OUTPUT_UNSTORABLE),
    ],
)
def test_bad_reply_is_rejected_without_content(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
    change: str,
    code: ErrorCode,
) -> None:
    run = _run(
        database, config, context_values, paper_id, FakeLlmProvider([_reply(_reject_case(change))])
    )

    assert run.result is not None
    assert run.result.outcome is GatewayOutcome.REJECTED
    assert run.result.reason is code
    assert run.result.output is None
    header = _only(db)
    assert header["status"] == "rejected"
    assert header["reject_reason"] == code.value
    assert all(header[name] is None for name in (*COUNTS, "country", "is_review_or_meta_analysis"))
    assert _detail_counts(db) == dict.fromkeys(DETAIL_TABLES, 0)
    # 拒收照樣記帳（模型已經回答）。
    assert table_counts(db)["llm_call"] == 1


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (
            _reply('{"is_review_or_meta_analysis": false, "con', stop_reason="max_tokens"),
            ErrorCode.LLM_TRUNCATED,
        ),
        (_reply(None, stop_reason="refusal"), ErrorCode.LLM_REFUSAL),
    ],
)
def test_truncated_or_refused_reply_is_rejected(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
    reply: Any,
    code: ErrorCode,
) -> None:
    run = _run(database, config, context_values, paper_id, FakeLlmProvider([reply]))

    assert run.result is not None
    assert run.result.reason is code
    assert _only(db)["status"] == "rejected"
    assert _detail_counts(db) == dict.fromkeys(DETAIL_TABLES, 0)


# --- plan_for 說不做 ----------------------------------------------------------------------------


def test_paper_without_abstract_is_no_input_and_the_gateway_is_not_called(
    db: TestDatabase, database: Database, config: GatewayConfig, context_values: ContextValues
) -> None:
    title_only = create_paper(
        database, doi="10.1/title-only", openalex_id="W2", title=TITLE, abstract=None
    )
    provider = FakeLlmProvider()
    before = table_counts(db)

    run = _run(database, config, context_values, title_only, provider)

    assert run.plan.skip is SkipState.NO_INPUT
    assert run.result is None
    assert provider.requests == []
    assert table_counts(db) == before
    assert _detail_counts(db) == dict.fromkeys(DETAIL_TABLES, 0)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE paper SET excluded_reason = 'editorial' WHERE id = %s",
        "UPDATE paper SET origin = 'manual' WHERE id = %s",
    ],
)
def test_excluded_or_manual_paper_is_not_applicable_and_the_gateway_is_not_called(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
    change: str,
) -> None:
    execute(db, change, paper_id)
    provider = FakeLlmProvider()
    before = table_counts(db)

    run = _run(database, config, context_values, paper_id, provider)

    assert run.plan.skip is SkipState.NOT_APPLICABLE
    assert run.result is None
    assert provider.requests == []
    assert table_counts(db) == before
    assert _detail_counts(db) == dict.fromkeys(DETAIL_TABLES, 0)


def test_missing_paper_and_member_actor_are_refused(
    database: Database, config: GatewayConfig, context_values: ContextValues, paper_id: uuid.UUID
) -> None:
    provider = FakeLlmProvider()

    with pytest.raises(LookupError):
        _run(database, config, context_values, uuid.uuid4(), provider)
    with pytest.raises(PermissionDeniedError):
        run_research_model(
            MemberViewer(member_id=uuid.uuid4(), is_admin=True),
            database,
            provider,
            config,
            budget_config(),
            context_values,
            paper_id,
            clock=lambda: NOW,
        )
    assert provider.requests == []


# --- 構面重用 -----------------------------------------------------------------------------------


def test_two_papers_mentioning_the_same_construct_share_one_construct_row(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
) -> None:
    other_abstract = (
        "Using data from 150 online shoppers, we find that trust increases loyalty (p < .05)."
    )
    other = create_paper(
        database,
        doi="10.1/other",
        openalex_id="W3",
        title="Trust and loyalty in online retail",
        abstract=other_abstract,
    )
    other_reply = {
        "is_review_or_meta_analysis": False,
        "context": dict.fromkeys(GOOD["context"]),
        "limitations": None,
        "future_research": None,
        "constructs": [
            # 寫法不同，正規化後同名：沿用同一列構面。
            {"name_en": "TRUST", "label_zh": "信任", "definition": None, "measurement": None},
            {"name_en": "Loyalty", "label_zh": "忠誠度", "definition": None, "measurement": None},
        ],
        "hypotheses": [
            {
                "sentence": "we find that trust increases loyalty (p < .05)",
                "cause": "TRUST",
                "effect": "Loyalty",
                "evidence_type": "empirical_test",
                "support": "supported",
                "result_quote": "we find that trust increases loyalty (p < .05)",
                "statistics": {
                    "beta": None,
                    "p_value": None,
                    "sample_size": 150,
                    "effect_type": None,
                },
            }
        ],
    }
    provider = FakeLlmProvider([_reply(GOOD), _reply(other_reply)])

    _run(database, config, context_values, paper_id, provider)
    second = _run(database, config, context_values, other, provider)

    assert second.result is not None
    assert second.result.outcome is GatewayOutcome.ACTIVE
    assert execute(
        db, "SELECT normalized_name, id = canonical_id FROM construct ORDER BY normalized_name"
    ) == [("loyalty", True), ("perceived usefulness", True), ("trust", True)]
    # 兩篇各一列構面提及，都指向同一列 trust。
    assert execute(
        db,
        "SELECT count(DISTINCT m.research_model_id), count(DISTINCT m.construct_id)"
        " FROM construct_mention m JOIN construct c ON c.id = m.construct_id"
        " WHERE c.normalized_name = 'trust'",
    ) == [(2, 1)]
    (header,) = _headers(db, other)
    assert _mentions(db, header["id"])[1] == ("trust", "TRUST", "信任", None)


# --- 冪等與清單升版 -----------------------------------------------------------------------------


def test_same_input_twice_does_not_pay_twice(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
) -> None:
    provider = FakeLlmProvider([_reply(GOOD)])
    first = _run(database, config, context_values, paper_id, provider)
    after_first = table_counts(db)
    details_after_first = _detail_counts(db)

    second = _run(database, config, context_values, paper_id, provider)

    assert first.result is not None
    assert first.result.outcome is GatewayOutcome.ACTIVE
    assert second.result is not None
    assert second.result.outcome is GatewayOutcome.SKIPPED
    assert len(provider.requests) == 1
    assert table_counts(db) == after_first
    assert _detail_counts(db) == details_after_first


def test_context_values_bump_reruns_and_supersedes_the_old_row(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
) -> None:
    provider = FakeLlmProvider([_reply(GOOD)])
    _run(database, config, context_values, paper_id, provider)
    again = _run(database, config, context_values, paper_id, provider)
    assert again.result is not None
    assert again.result.outcome is GatewayOutcome.SKIPPED
    assert len(provider.requests) == 1

    bumped = ContextValues.model_validate(context_values.model_dump() | {"version": 2})
    provider.add(_reply(_change("out_of_list")))
    rerun = _run(database, config, bumped, paper_id, provider)

    # 清單版本算進 input_sha256：同一篇、同一份文字，指紋變了，重新呼叫模型。
    assert len(provider.requests) == 2
    assert rerun.result is not None
    assert rerun.result.outcome is GatewayOutcome.ACTIVE
    old, new = _headers(db, paper_id)
    assert (old["status"], new["status"]) == ("superseded", "active")
    assert (old["country"], new["country"]) == ("tw", None)
    assert new["dropped"] == 2
    # 舊的構面提及與假設跟著舊表頭留著（細項不 UPDATE）；構面沿用同一列。
    assert len(_mentions(db, old["id"])) == 2
    assert len(_mentions(db, new["id"])) == 2
    assert _detail_counts(db) == {"construct": 2, "construct_mention": 4, "hypothesis": 2}
    runs = execute(
        db,
        "SELECT context_values_version, input_sha256 FROM derivation_run WHERE paper_id = %s"
        " ORDER BY created_at",
        paper_id,
    )
    assert [row[0] for row in runs] == ["1", "2"]
    assert runs[0][1] != runs[1][1]


# --- 評估的候選也寫內容 ------------------------------------------------------------------------


def test_evaluation_candidate_has_content_and_does_not_block_the_normal_flow(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
) -> None:
    provider = FakeLlmProvider([_reply(_change("inferred_country"))])

    result = run_llm_job(
        SYSTEM,
        database,
        provider,
        config,
        LlmJob(
            paper_id=paper_id,
            task=LlmTask.EXTRACT_MODEL,
            input_kind=InputKind.ABSTRACT,
            lang=OutputLang.EN,
            validate_output=ResearchModelValidator(context_values),
            context_values_version=context_values.version_label,
            context_values=context_values,
        ),
        budget_allows=lambda _: True,
        eval_run_id=uuid.uuid4(),
    )

    assert result.outcome is GatewayOutcome.CANDIDATE
    header = _only(db)
    assert header["status"] == "candidate"
    assert header["unsupported"] == 1
    assert len(_mentions(db, header["id"])) == 2
    assert len(_hypotheses(db, header["id"])) == 1

    provider.add(_reply(GOOD))
    normal = _run(database, config, context_values, paper_id, provider)

    assert normal.result is not None
    assert normal.result.outcome is GatewayOutcome.ACTIVE


# --- ai_results 的防呆 --------------------------------------------------------------------------


def _running_run(db: TestDatabase, paper_id: uuid.UUID, task: str, version: str | None) -> Any:
    return execute(
        db,
        "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
        " input_sha256, input_kind, params, code_version, call_mode, context_values_version,"
        " status) VALUES (%s, %s, 1, %s, 'claude-haiku-5-5', %s, 'abstract', '{}', 'test',"
        " 'interactive', %s, 'running') RETURNING id",
        paper_id,
        task,
        "a" * 64,
        "b" * 64,
        version,
    )[0][0]


def test_research_model_output_without_context_values_or_input_is_refused(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
) -> None:
    run_id = _running_run(db, paper_id, "extract_model", "1")
    output = ResearchModelOutput.model_validate(GOOD)
    before = table_counts(db)

    def record(**kwargs: Any) -> ResultStatus:
        return record_llm_result(
            SYSTEM,
            database,
            config,
            paper_id=paper_id,
            run_id=run_id,
            lang=OutputLang.EN,
            abstract_id=None,
            reject_reason=None,
            output=output,
            **kwargs,
        )

    with pytest.raises(ValueError, match="情境值清單"):
        record(context_values_version="1")
    with pytest.raises(ValueError, match="情境值清單"):
        record(context_values=context_values)
    with pytest.raises(ValueError, match="版本"):
        record(context_values_version="2", context_values=context_values)
    assert table_counts(db) == before
    assert _detail_counts(db) == dict.fromkeys(DETAIL_TABLES, 0)
    assert execute(db, "SELECT status FROM derivation_run") == [("running",)]


def test_context_values_for_a_zh_summary_run_is_refused_and_nothing_is_written(
    db: TestDatabase,
    database: Database,
    config: GatewayConfig,
    context_values: ContextValues,
    paper_id: uuid.UUID,
) -> None:
    run_id = _running_run(db, paper_id, "zh_summary", None)
    before = table_counts(db)

    with pytest.raises(ValueError, match="只有 extract_model"):
        record_llm_result(
            SYSTEM,
            database,
            config,
            paper_id=paper_id,
            run_id=run_id,
            lang=OutputLang.ZH_HANT,
            abstract_id=None,
            reject_reason=None,
            context_values=context_values,
        )

    assert table_counts(db) == before
    assert execute(db, "SELECT status FROM derivation_run") == [("running",)]
