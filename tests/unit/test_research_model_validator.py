"""研究模型的正規化與 domain 驗證（Story 2.6、ADR-0034）：

- `validate_research_model`：spec 2.6 I/O 表的每一列（正常、原句捏造、情境清單外、國家推測、
  質性標支持、實證無結果、同一句重複／衝突、數字範圍、數字捏造、構面不對、夾指令／標記），
  每個拒收代碼與 FR-3 的四條擷取規則。
- 原文欄位的比對：大小寫、彎引號、頭尾標點不算；字的邊界要對；標題與摘要不能跨段。
- `ResearchModelValidator`：輸出不是 `ResearchModelOutput` 就拒收（`llm_output_invalid`）。
- `ConstructMentionContent`、`HypothesisContent`、`ResearchModelContent`、`NewAiResult`、`LlmJob`
  的防呆。

輸入與輸出都是自己編的假論文。
"""

import contextlib
import copy
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from radar.config import load_context_values
from radar.domain.ai_outputs import (
    ClassificationContent,
    ConstructMentionContent,
    CurrentAbstract,
    HypothesisContent,
    InputKind,
    LlmInput,
    LlmTask,
    NewAiResult,
    OutputLang,
    OutputRejectedError,
    PaperLlmSource,
    ResearchModelContent,
    ResultStatus,
    build_llm_input,
)
from radar.domain.context_values import ContextValues
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import hypothesis_sentence_sha256
from radar.domain.llm_schemas import (
    EvidenceType,
    FakeResearchQuestion,
    ResearchModelOutput,
    SupportLevel,
)
from radar.domain.papers import AbstractSource
from radar.domain.validators.research_model import (
    ResearchModelValidator,
    quote_key,
    validate_research_model,
)
from radar.services.llm_gateway import LlmJob
from tests.llm_support import ROOT

CONTEXT_VALUES = load_context_values({"RADAR_CONFIG_DIR": str(ROOT / "config")})

TITLE = "Perceived usefulness and trust in mobile banking adoption"
ABSTRACT = (
    "Based on a survey of 200 bank customers in Taiwan, we test a model of mobile banking "
    "adoption. Perceived usefulness is defined as the degree to which users believe the app "
    "improves their banking. Perceived usefulness positively affects trust (β = 0.42, p = .01). "
    "Trust was measured with 4 items adapted from Gefen (2003) (Cronbach's alpha = 0.91, "
    "CR = 0.93, AVE = 0.78). Interviews with managers suggest that trust shapes loyalty. "
    "One limitation is the single-country sample. Future research should test other countries."
)
SENTENCE = "Perceived usefulness positively affects trust (β = 0.42, p = .01)."
QUALITATIVE_SENTENCE = "Interviews with managers suggest that trust shapes loyalty."


def _input(title: str = TITLE, abstract: str = ABSTRACT) -> LlmInput:
    return build_llm_input(
        PaperLlmSource(
            uuid.uuid4(),
            title,
            CurrentAbstract(uuid.uuid4(), AbstractSource.OPENALEX, abstract),
        ),
        InputKind.ABSTRACT,
    )


INPUT = _input()

BASE: dict[str, Any] = {
    "is_review_or_meta_analysis": False,
    "context": {
        "country": "tw",
        "industry": "financial_services",
        "research_design": "survey",
        "unit_of_analysis": "individual",
        "sample_size": 200,
    },
    "limitations": "One limitation is the single-country sample.",
    "future_research": "Future research should test other countries.",
    "constructs": [
        {
            "name_en": "Perceived usefulness",
            "label_zh": "知覺有用性",
            "definition": "the degree to which users believe the app improves their banking",
            "measurement": None,
        },
        {
            "name_en": "Trust",
            "label_zh": "信任",
            "definition": None,
            "measurement": {
                "scale_source": "Gefen (2003)",
                "item_location": None,
                "item_count": 4,
                "cronbach_alpha": 0.91,
                "composite_reliability": 0.93,
                "ave": 0.78,
            },
        },
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

LOYALTY = {"name_en": "Loyalty", "label_zh": "忠誠度", "definition": None, "measurement": None}


def _data() -> dict[str, Any]:
    return copy.deepcopy(BASE)


def _hypothesis(**changes: Any) -> dict[str, Any]:
    hypothesis: dict[str, Any] = copy.deepcopy(BASE["hypotheses"][0])
    hypothesis.update(changes)
    return hypothesis


def _qualitative(**changes: Any) -> dict[str, Any]:
    return (
        _hypothesis(
            sentence=QUALITATIVE_SENTENCE,
            cause="Trust",
            effect="Loyalty",
            evidence_type="qualitative",
            support="not_determined",
            statistics=None,
        )
        | changes
    )


def _with_loyalty(*hypotheses: dict[str, Any]) -> dict[str, Any]:
    data = _data()
    data["constructs"].append(copy.deepcopy(LOYALTY))
    data["hypotheses"] = list(hypotheses)
    return data


def _output(data: dict[str, Any]) -> ResearchModelOutput:
    return ResearchModelOutput.model_validate(data)


def _validate(data: dict[str, Any], llm_input: LlmInput = INPUT) -> ResearchModelContent:
    return validate_research_model(_output(data), llm_input, CONTEXT_VALUES)


def _rejected(data: dict[str, Any], llm_input: LlmInput = INPUT) -> ErrorCode:
    with pytest.raises(OutputRejectedError) as caught:
        _validate(data, llm_input)
    return caught.value.code


# --- 正常 -----------------------------------------------------------------------------------------


def test_normal_output_becomes_content() -> None:
    content = _validate(_data())

    assert content.is_review_or_meta_analysis is False
    assert (
        content.country,
        content.industry,
        content.research_design,
        content.unit_of_analysis,
        content.sample_size,
    ) == ("tw", "financial_services", "survey", "individual", 200)
    assert content.limitations == "One limitation is the single-country sample."
    assert content.future_research == "Future research should test other countries."
    assert (
        content.dropped_context_value_count,
        content.unsupported_context_value_count,
        content.corrected_support_count,
    ) == (0, 0, 0)
    assert content.normalized_names() == ("perceived usefulness", "trust")

    usefulness, trust = content.constructs
    assert usefulness.name_en == "Perceived usefulness"
    assert usefulness.label_zh == "知覺有用性"
    assert usefulness.definition == (
        "the degree to which users believe the app improves their banking"
    )
    assert usefulness.item_count is None
    assert trust.scale_source == "Gefen (2003)"
    assert trust.item_location is None
    assert trust.item_count == 4
    assert (trust.cronbach_alpha, trust.composite_reliability, trust.ave) == (
        Decimal("0.91"),
        Decimal("0.93"),
        Decimal("0.78"),
    )

    (hypothesis,) = content.hypotheses
    assert hypothesis.ordinal == 1
    assert hypothesis.sentence == SENTENCE
    assert hypothesis.sentence_sha256 == hypothesis_sentence_sha256(
        quote_key(SENTENCE).strip(" .,;:")
    )
    assert (hypothesis.cause, hypothesis.effect) == ("perceived usefulness", "trust")
    assert hypothesis.evidence_type is EvidenceType.EMPIRICAL_TEST
    assert hypothesis.support is SupportLevel.SUPPORTED
    assert (hypothesis.beta, hypothesis.p_value, hypothesis.sample_size) == (
        Decimal("0.42"),
        Decimal("0.01"),
        200,
    )
    assert hypothesis.effect_type is None


def test_quoted_item_location_and_effect_type_are_kept() -> None:
    abstract = (
        ABSTRACT + "  The items are listed in   Appendix A. Trust has a direct effect on loyalty."
    )
    data = _data()
    data["constructs"][1]["measurement"]["item_location"] = "listed in Appendix A"
    data["hypotheses"][0]["statistics"]["effect_type"] = "direct  effect"

    content = _validate(data, _input(abstract=abstract))

    assert content.constructs[1].item_location == "listed in Appendix A"
    assert content.hypotheses[0].effect_type == "direct effect"


def test_empty_model_is_accepted() -> None:
    data = _data()
    data["constructs"] = []
    data["hypotheses"] = []
    data["context"] = dict.fromkeys(data["context"])
    data["limitations"] = None
    data["future_research"] = None

    content = _validate(data)

    assert content.constructs == ()
    assert content.hypotheses == ()
    assert content.country is None
    assert content.sample_size is None


def test_blank_optional_text_is_stored_as_null() -> None:
    data = _data()
    data["limitations"] = "   "
    data["constructs"][0]["definition"] = " \n "
    data["constructs"][1]["measurement"]["item_location"] = "  "

    content = _validate(data)

    assert content.limitations is None
    assert content.constructs[0].definition is None
    assert content.constructs[1].item_location is None


def test_names_and_quotes_are_stored_with_spaces_collapsed() -> None:
    data = _data()
    data["constructs"][0]["name_en"] = "  Perceived   usefulness "
    data["hypotheses"][0]["cause"] = "perceived usefulness"
    data["future_research"] = "Future  research should test   other countries"

    content = _validate(data)

    assert content.constructs[0].name_en == "Perceived usefulness"
    assert content.future_research == "Future research should test other countries"


def test_cause_and_effect_match_after_normalize_name() -> None:
    data = _data()
    data["hypotheses"][0]["cause"] = "PERCEIVED USEFULNESS (PU)"
    data["hypotheses"][0]["effect"] = " trust "

    (hypothesis,) = _validate(data).hypotheses

    assert (hypothesis.cause, hypothesis.effect) == ("perceived usefulness", "trust")


# --- 原文欄位 -------------------------------------------------------------------------------------


def _set(data: dict[str, Any], path: str, value: object) -> None:
    *parents, last = path.split(".")
    target: Any = data
    for part in parents:
        target = target[int(part)] if part.isdigit() else target[part]
    target[int(last) if last.isdigit() else last] = value


@pytest.mark.parametrize(
    "path",
    [
        "hypotheses.0.sentence",
        "constructs.0.definition",
        "limitations",
        "future_research",
        "constructs.1.measurement.scale_source",
        "constructs.1.measurement.item_location",
        "hypotheses.0.statistics.effect_type",
    ],
)
def test_quote_field_not_in_input_is_rejected(path: str) -> None:
    data = _data()
    _set(data, path, "Perceived usefulness strongly drives user loyalty in every market")

    assert _rejected(data) is ErrorCode.RESEARCH_MODEL_QUOTE_NOT_IN_INPUT


@pytest.mark.parametrize(
    "sentence",
    [
        # 大小寫、頭尾的標點與空白不算。
        "perceived usefulness POSITIVELY affects trust (β = 0.42, p = .01)",
        "  Perceived usefulness positively affects trust (β = 0.42, p = .01);  ",
        # 一段連續的子字串就算。
        "Perceived usefulness positively affects trust",
        # 中間多的空白縮成一個。
        "Perceived usefulness  positively\naffects trust",
    ],
)
def test_quote_matching_ignores_case_outer_punctuation_and_spacing(sentence: str) -> None:
    data = _data()
    data["hypotheses"][0]["sentence"] = sentence

    _validate(data)


def test_curly_quotes_match_straight_quotes() -> None:
    data = _data()
    data["constructs"][1]["measurement"]["scale_source"] = "Cronbach" + chr(0x2019) + "s alpha"

    content = _validate(data)

    assert content.constructs[1].scale_source == "Cronbach" + chr(0x2019) + "s alpha"


def test_quote_must_align_with_word_boundaries() -> None:
    data = _data()
    # 「positive」在輸入裡只是「positively」的一部分。
    data["hypotheses"][0]["sentence"] = "Perceived usefulness positive"

    assert _rejected(data) is ErrorCode.RESEARCH_MODEL_QUOTE_NOT_IN_INPUT


def test_quote_cannot_span_title_and_abstract() -> None:
    data = _data()
    data["hypotheses"][0]["sentence"] = "mobile banking adoption Based on a survey"

    assert _rejected(data) is ErrorCode.RESEARCH_MODEL_QUOTE_NOT_IN_INPUT


def test_quote_from_the_title_is_accepted() -> None:
    data = _data()
    data["hypotheses"][0]["sentence"] = "Perceived usefulness and trust in mobile banking"

    _validate(data)


# --- 情境值（不拒收、計數） -----------------------------------------------------------------------


def test_out_of_list_context_values_are_dropped_and_counted() -> None:
    data = _data()
    data["context"]["country"] = "usa"
    data["context"]["industry"] = "fintech"

    content = _validate(data)

    assert (content.country, content.industry) == (None, None)
    assert content.dropped_context_value_count == 2
    assert content.unsupported_context_value_count == 0
    assert content.research_design == "survey"


def test_context_value_must_match_the_key_exactly() -> None:
    data = _data()
    data["context"]["research_design"] = "Survey"
    data["context"]["unit_of_analysis"] = "個人"

    content = _validate(data)

    assert (content.research_design, content.unit_of_analysis) == (None, None)
    assert content.dropped_context_value_count == 2


def test_country_not_written_in_input_is_cleared_and_counted() -> None:
    data = _data()
    data["context"]["country"] = "cn"

    content = _validate(data)

    assert content.country is None
    assert content.unsupported_context_value_count == 1
    assert content.dropped_context_value_count == 0


def test_industry_not_written_in_input_is_cleared_and_counted() -> None:
    data = _data()
    data["context"]["industry"] = "retail"
    data["context"]["country"] = "us"

    content = _validate(data)

    assert (content.country, content.industry) == (None, None)
    assert content.unsupported_context_value_count == 2


def test_design_and_unit_need_no_terms() -> None:
    data = _data()
    data["context"]["research_design"] = "experiment"
    data["context"]["unit_of_analysis"] = "organization"

    content = _validate(data)

    assert (content.research_design, content.unit_of_analysis) == ("experiment", "organization")
    assert content.unsupported_context_value_count == 0


@pytest.mark.parametrize(
    ("abstract", "country", "industry", "kept"),
    [
        # 不分大小寫；字尾多 s、es 也算。
        ("A survey of BANKS in TAIWAN.", "tw", "financial_services", True),
        ("We studied U.S. hospitals.", "us", "healthcare", True),
        ("Data come from United States retailers.", "us", "retail", True),
        # y 結尾寫成 ies 也算。
        ("We surveyed factories in Japan.", "jp", "manufacturing", True),
        # 前後接著英文字母或數字的不算。
        ("A survey of firms in Indianapolis and Bankstown.", "in", "financial_services", False),
        ("A survey of Taiwanization in schools2.", "tw", "education", False),
        # 會出現在別的國家或樣本描述裡的字不收（設定檔註解）。
        ("Latin American university students rated apps.", "us", "education", False),
        ("North Korean news sites offer free shipping.", "kr", "logistics", False),
    ],
)
def test_terms_match_whole_words(abstract: str, country: str, industry: str, kept: bool) -> None:
    data = _data()
    data["context"] |= {"country": country, "industry": industry, "sample_size": None}
    data["limitations"] = None
    data["future_research"] = None
    data["constructs"] = []
    data["hypotheses"] = []

    content = _validate(data, _input(title="A study", abstract=abstract))

    if kept:
        assert (content.country, content.industry) == (country, industry)
        assert content.unsupported_context_value_count == 0
    else:
        assert (content.country, content.industry) == (None, None)
        assert content.unsupported_context_value_count == 2


# --- 擷取規則 1、2：支持與否 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("evidence_type", "support", "stored"),
    [
        ("qualitative", "supported", SupportLevel.NOT_DETERMINED),
        ("conceptual", "not_stated", SupportLevel.NOT_DETERMINED),
        ("analytical", "not_supported", SupportLevel.NOT_DETERMINED),
        ("simulation", "partially_supported", SupportLevel.NOT_DETERMINED),
        ("empirical_test", "not_determined", SupportLevel.NOT_STATED),
    ],
)
def test_support_is_corrected_and_counted(
    evidence_type: str, support: str, stored: SupportLevel
) -> None:
    data = _with_loyalty(_qualitative(evidence_type=evidence_type, support=support))

    content = _validate(data)

    (hypothesis,) = content.hypotheses
    assert hypothesis.support is stored
    assert content.corrected_support_count == 1


@pytest.mark.parametrize(
    ("evidence_type", "support"),
    [
        ("empirical_test", "supported"),
        ("empirical_test", "not_supported"),
        ("empirical_test", "partially_supported"),
        ("empirical_test", "not_stated"),
        ("qualitative", "not_determined"),
        ("conceptual", "not_determined"),
    ],
)
def test_consistent_support_is_kept(evidence_type: str, support: str) -> None:
    data = _with_loyalty(_qualitative(evidence_type=evidence_type, support=support))

    content = _validate(data)

    assert content.hypotheses[0].support is SupportLevel(support)
    assert content.corrected_support_count == 0


# --- 擷取規則 3：同一句多條關係、重複、衝突 -------------------------------------------------------


def test_one_sentence_may_carry_several_relations() -> None:
    data = _with_loyalty(
        _qualitative(),
        _qualitative(cause="Perceived usefulness", effect="Loyalty"),
    )

    content = _validate(data)

    first, second = content.hypotheses
    assert (first.ordinal, second.ordinal) == (1, 2)
    assert first.sentence_sha256 == second.sentence_sha256
    assert {first.cause, second.cause} == {"trust", "perceived usefulness"}


def test_identical_duplicate_is_stored_once() -> None:
    data = _data()
    duplicate = _hypothesis(sentence=SENTENCE.upper().rstrip("."), cause="perceived usefulness")
    data["hypotheses"].append(duplicate)

    content = _validate(data)

    assert len(content.hypotheses) == 1
    assert content.hypotheses[0].sentence == SENTENCE


def test_duplicates_count_one_correction() -> None:
    data = _with_loyalty(
        _qualitative(support="supported"),
        _qualitative(support="supported"),
    )

    content = _validate(data)

    assert len(content.hypotheses) == 1
    assert content.corrected_support_count == 1


def test_ordinals_follow_the_kept_hypotheses() -> None:
    data = _with_loyalty(_hypothesis(), _hypothesis(), _qualitative())

    content = _validate(data)

    assert [item.ordinal for item in content.hypotheses] == [1, 2]
    assert content.hypotheses[1].cause == "trust"


@pytest.mark.parametrize(
    "changes",
    [
        {"support": "not_supported"},
        {"evidence_type": "qualitative", "support": "not_determined"},
        {"statistics": None},
        {"statistics": {"beta": 0.42, "p_value": 0.01, "sample_size": None, "effect_type": None}},
    ],
)
def test_conflicting_duplicate_is_rejected(changes: dict[str, Any]) -> None:
    data = _data()
    data["hypotheses"].append(_hypothesis(**changes))

    assert _rejected(data) is ErrorCode.RESEARCH_MODEL_CONFLICTING_HYPOTHESIS


def test_same_relation_from_different_sentences_is_kept_twice() -> None:
    # 兩句不同的來源句子都說 PU → trust：兩條都留（「不同原句」是 2）。
    other = "Older users also report that perceived usefulness positively affects trust."
    data = _data()
    data["hypotheses"].append(_hypothesis(sentence=other, support="not_stated"))

    content = _validate(data, _input(abstract=ABSTRACT + " " + other))

    assert len(content.hypotheses) == 2
    assert content.hypotheses[0].sentence_sha256 != content.hypotheses[1].sentence_sha256


def test_fragment_and_full_sentence_are_the_same_source_sentence() -> None:
    # Codex 審查：同一句的完整版與片段原本算成兩句（可重複計數、避開衝突檢查）。改成依輸入裡的
    # 來源句子算雜湊：值一樣只留一筆，值不一樣就是衝突。
    fragment = "Perceived usefulness positively affects trust"
    data = _data()
    data["hypotheses"].append(_hypothesis(sentence=fragment))

    content = _validate(data)

    assert len(content.hypotheses) == 1
    assert content.hypotheses[0].sentence_sha256 == hypothesis_sentence_sha256(
        quote_key(SENTENCE).strip(" .,;:")
    )

    conflicting = _data()
    conflicting["hypotheses"].append(_hypothesis(sentence=fragment, support="not_stated"))
    assert _rejected(conflicting) is ErrorCode.RESEARCH_MODEL_CONFLICTING_HYPOTHESIS


def test_quote_spanning_two_sentences_hashes_both_sentences() -> None:
    two = "Interviews with managers suggest that trust shapes loyalty. One limitation"
    data = _with_loyalty(_qualitative(sentence=two))

    content = _validate(data)

    expected = quote_key(
        "Interviews with managers suggest that trust shapes loyalty. One limitation is the"
        " single-country sample"
    )
    assert content.hypotheses[0].sentence_sha256 == hypothesis_sentence_sha256(expected)


# --- 數字 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("hypotheses.0.statistics.p_value", 1.5),
        ("hypotheses.0.statistics.p_value", -0.01),
        ("hypotheses.0.statistics.sample_size", 0),
        ("context.sample_size", 0),
        ("context.sample_size", -200),
        ("context.sample_size", 2**31),
        ("constructs.1.measurement.cronbach_alpha", 1.2),
        ("constructs.1.measurement.composite_reliability", -0.93),
        ("constructs.1.measurement.ave", 7.8),
        ("constructs.1.measurement.item_count", 0),
    ],
)
def test_number_out_of_range_is_rejected(path: str, value: float) -> None:
    data = _data()
    _set(data, path, value)

    assert _rejected(data) is ErrorCode.RESEARCH_MODEL_NUMBER_OUT_OF_RANGE


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("hypotheses.0.statistics.beta", 0.43),
        ("hypotheses.0.statistics.beta", -0.42),
        ("hypotheses.0.statistics.p_value", 0.05),
        ("hypotheses.0.statistics.sample_size", 201),
        ("context.sample_size", 312),
        ("constructs.1.measurement.cronbach_alpha", 0.9),
        ("constructs.1.measurement.item_count", 5),
    ],
)
def test_number_not_in_input_is_rejected(path: str, value: float) -> None:
    data = _data()
    _set(data, path, value)

    assert _rejected(data) is ErrorCode.RESEARCH_MODEL_UNSUPPORTED_CONTENT


def test_number_written_differently_in_input_is_accepted() -> None:
    # 輸入寫 `.01`、模型寫 0.01；輸入寫 0.42、模型寫整數形式的 β 不算。
    data = _data()
    data["hypotheses"][0]["statistics"]["p_value"] = 0.010

    content = _validate(data)

    assert content.hypotheses[0].p_value == Decimal("0.01")


def test_beta_may_be_negative_when_input_says_so() -> None:
    abstract = ABSTRACT.replace("β = 0.42", "β = -0.42")
    data = _data()
    data["hypotheses"][0]["sentence"] = "Perceived usefulness positively affects trust"
    data["hypotheses"][0]["statistics"]["beta"] = -0.42

    content = _validate(data, _input(abstract=abstract))

    assert content.hypotheses[0].beta == Decimal("-0.42")


# --- 構面 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cause", "effect"),
    [
        ("Loyalty", "Trust"),
        ("Perceived usefulness", "Satisfaction"),
        ("Trust", "Trust"),
        ("trust", "TRUST (T)"),
        ("Perceived-usefulness", "Trust"),
    ],
)
def test_hypothesis_with_unknown_or_same_construct_is_rejected(cause: str, effect: str) -> None:
    data = _data()
    data["hypotheses"][0] |= {"cause": cause, "effect": effect}

    assert _rejected(data) is ErrorCode.RESEARCH_MODEL_UNKNOWN_CONSTRUCT


@pytest.mark.parametrize("name", ["Trust", "TRUST (T)", " trust ", "Trust;"])
def test_duplicate_construct_is_rejected(name: str) -> None:
    data = _data()
    data["constructs"].append(
        {"name_en": name, "label_zh": "信任感", "definition": None, "measurement": None}
    )

    assert _rejected(data) is ErrorCode.RESEARCH_MODEL_DUPLICATE_CONSTRUCT


# --- 夾指令、標記、看不見的字 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("constructs.1.label_zh", "<b>信任</b>"),
        ("constructs.1.name_en", "[Trust](http://evil.example)"),
        ("constructs.1.name_en", "Trust evil.com"),
        ("constructs.1.label_zh", "信任&amp;"),
        ("limitations", "One limitation is the single-country sample&#46;"),
        ("hypotheses.0.sentence", SENTENCE[:10] + chr(0x202E) + SENTENCE[10:]),
        ("constructs.0.definition", "the degree" + chr(0xE0041) + " to which users believe"),
    ],
)
def test_unsafe_text_is_rejected(path: str, value: str) -> None:
    data = _data()
    _set(data, path, value)

    assert _rejected(data) is ErrorCode.RESEARCH_MODEL_UNSAFE_TEXT


def test_comparison_in_label_is_not_markup() -> None:
    data = _data()
    data["constructs"][1]["label_zh"] = "信任 p < .01"

    _validate(data)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("constructs.0.name_en", ""),
        ("constructs.0.name_en", "   "),
        ("constructs.0.name_en", "Trust " * 21),
        ("constructs.0.name_en", "信任"),
        ("constructs.0.name_en", "Trust 信任"),
        ("constructs.0.name_en", "2003"),
        ("constructs.0.name_en", "(-)"),
        ("constructs.0.name_en", "Perceived" + chr(0x200D) + "usefulness"),
        ("constructs.0.label_zh", ""),
        ("constructs.0.label_zh", "Trust"),
        ("constructs.0.label_zh", "知覺有用性" * 7),
        ("constructs.0.label_zh", "认知有用性"),
        ("constructs.0.label_zh", "知覺" + chr(0x7) + "有用性"),
        ("constructs.0.definition", "users believe"),
        ("hypotheses.0.sentence", "Perceived usefulness"),
        ("hypotheses.0.sentence", "   "),
    ],
)
def test_invalid_text_is_rejected(path: str, value: str) -> None:
    data = _data()
    _set(data, path, value)

    assert _rejected(data) is ErrorCode.RESEARCH_MODEL_INVALID_TEXT


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("constructs.0.name_en", "Perceived enjoyment"),
        ("constructs.0.name_en", "Perceived usefulness 2"),
        ("constructs.0.label_zh", "知覺有用性 2"),
        ("constructs.0.label_zh", "Enjoyment 有用性"),
        ("constructs.0.label_zh", "有用性 5 題"),
    ],
)
def test_invented_words_or_numbers_in_names_are_rejected(path: str, value: str) -> None:
    data = _data()
    _set(data, path, value)
    if path.endswith("name_en"):
        data["hypotheses"][0]["cause"] = value

    assert _rejected(data) is ErrorCode.RESEARCH_MODEL_UNSUPPORTED_CONTENT


def test_name_with_input_words_and_numbers_is_accepted() -> None:
    data = _data()
    data["constructs"][1]["label_zh"] = "信任（Gefen 2003）"

    _validate(data)


# --- 存不進資料庫、型別 ---------------------------------------------------------------------------


def test_nul_in_output_is_unstorable() -> None:
    data = _data()
    data["limitations"] = "One limitation" + chr(0) + " is the single-country sample."

    assert _rejected(data) is ErrorCode.LLM_OUTPUT_UNSTORABLE


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_number_is_unstorable(value: float) -> None:
    # 非有限的數字在範圍檢查之前就被「存不進資料庫」擋下。
    data = _data()
    data["hypotheses"][0]["statistics"]["beta"] = value

    assert _rejected(data) is ErrorCode.LLM_OUTPUT_UNSTORABLE


@pytest.mark.parametrize(
    "output", [FakeResearchQuestion(research_question="x"), '{"constructs": []}']
)
def test_validate_refuses_other_output_types(output: Any) -> None:
    with pytest.raises(TypeError, match="ResearchModelOutput"):
        validate_research_model(output, INPUT, CONTEXT_VALUES)


def test_validate_requires_context_values() -> None:
    with pytest.raises(TypeError, match="ContextValues"):
        validate_research_model(_output(_data()), INPUT, None)  # type: ignore[arg-type]


def test_validation_follows_the_given_context_values() -> None:
    smaller = ContextValues.model_validate(
        CONTEXT_VALUES.model_dump() | {"countries": [CONTEXT_VALUES.countries[1].model_dump()]}
    )

    content = validate_research_model(_output(_data()), INPUT, smaller)

    assert content.country is None
    assert content.dropped_context_value_count == 1


# --- 驗證函式 -------------------------------------------------------------------------------------


def test_validator_accepts_valid_output() -> None:
    ResearchModelValidator(CONTEXT_VALUES)(_output(_data()), INPUT)


def test_validator_rejects_invalid_content() -> None:
    data = _data()
    data["hypotheses"][0]["sentence"] = "Trust drives everything in this paper"

    with pytest.raises(OutputRejectedError) as caught:
        ResearchModelValidator(CONTEXT_VALUES)(_output(data), INPUT)

    assert caught.value.code is ErrorCode.RESEARCH_MODEL_QUOTE_NOT_IN_INPUT


@pytest.mark.parametrize(
    "output", [FakeResearchQuestion(research_question="x"), '{"constructs": []}']
)
def test_validator_rejects_output_that_is_not_research_model_output(output: Any) -> None:
    with pytest.raises(OutputRejectedError) as caught:
        ResearchModelValidator(CONTEXT_VALUES)(output, INPUT)

    assert caught.value.code is ErrorCode.LLM_OUTPUT_INVALID


# --- schema（閘門用 strict 的 model_validate_json） -----------------------------------------------


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


@pytest.mark.parametrize(
    "change",
    [
        "extra_top",
        "extra_nested",
        "missing_field",
        "missing_nullable",
        "enum_outside",
        "string_number",
        "float_count",
    ],
)
def test_schema_refuses_extra_missing_and_wrong_types(change: str) -> None:
    data = _data()
    match change:
        case "extra_top":
            data["note"] = "x"
        case "extra_nested":
            data["constructs"][0]["weight"] = 1
        case "missing_field":
            del data["hypotheses"]
        case "missing_nullable":
            del data["constructs"][0]["definition"]
        case "enum_outside":
            data["hypotheses"][0]["support"] = "proven"
        case "string_number":
            data["hypotheses"][0]["statistics"]["beta"] = "0.42"
        case "float_count":
            data["context"]["sample_size"] = 200.5

    with pytest.raises(ValidationError):
        ResearchModelOutput.model_validate_json(_json(data), strict=True)


def test_schema_accepts_the_base_output_as_json() -> None:
    output = ResearchModelOutput.model_validate_json(_json(_data()), strict=True)

    assert output.hypotheses[0].evidence_type is EvidenceType.EMPIRICAL_TEST


# --- 內容的防呆 -----------------------------------------------------------------------------------


def _mention(**changes: Any) -> ConstructMentionContent:
    values: dict[str, Any] = {
        "normalized_name": "trust",
        "name_en": "Trust",
        "label_zh": "信任",
        "definition": None,
        "scale_source": None,
        "item_location": None,
        "item_count": None,
        "cronbach_alpha": None,
        "composite_reliability": None,
        "ave": None,
    }
    values.update(changes)
    return ConstructMentionContent(**values)


def _hypothesis_content(**changes: Any) -> HypothesisContent:
    values: dict[str, Any] = {
        "ordinal": 1,
        "sentence": "Trust increases loyalty.",
        "sentence_sha256": "a" * 64,
        "cause": "trust",
        "effect": "loyalty",
        "evidence_type": EvidenceType.EMPIRICAL_TEST,
        "support": SupportLevel.SUPPORTED,
        "beta": None,
        "p_value": None,
        "sample_size": None,
        "effect_type": None,
        "result_quote": "Trust increases loyalty.",
    }
    values.update(changes)
    return HypothesisContent(**values)


def _model_content(**changes: Any) -> ResearchModelContent:
    values: dict[str, Any] = {
        "is_review_or_meta_analysis": False,
        "country": None,
        "industry": None,
        "research_design": None,
        "unit_of_analysis": None,
        "sample_size": None,
        "limitations": None,
        "future_research": None,
        "dropped_context_value_count": 0,
        "unsupported_context_value_count": 0,
        "corrected_support_count": 0,
        "cleared_value_count": 0,
        "constructs": (
            _mention(),
            _mention(normalized_name="loyalty", name_en="Loyalty", label_zh="忠誠度"),
        ),
        "hypotheses": (_hypothesis_content(),),
    }
    values.update(changes)
    return ResearchModelContent(**values)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"normalized_name": None}, "normalized_name"),
        ({"name_en": "  "}, "name_en"),
        ({"definition": ""}, "definition"),
        ({"scale_source": "a" + chr(0) + "b"}, "scale_source"),
        ({"item_count": 0}, "item_count"),
        ({"item_count": True}, "item_count"),
        ({"cronbach_alpha": Decimal("1.01")}, "cronbach_alpha"),
        ({"ave": 0.5}, "ave"),
        ({"composite_reliability": Decimal("NaN")}, "composite_reliability"),
    ],
)
def test_construct_mention_content_guards(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _mention(**changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"ordinal": 0}, "ordinal"),
        ({"effect": "trust"}, "同一個構面"),
        ({"sentence_sha256": "A" * 64}, "sentence_sha256"),
        ({"evidence_type": "empirical_test"}, "列舉"),
        ({"support": SupportLevel.NOT_DETERMINED}, "not_determined"),
        (
            {"evidence_type": EvidenceType.QUALITATIVE, "support": SupportLevel.SUPPORTED},
            "not_determined",
        ),
        ({"beta": Decimal("Infinity")}, "beta"),
        ({"p_value": Decimal("1.5")}, "p_value"),
        ({"sample_size": 0}, "sample_size"),
        ({"effect_type": " "}, "effect_type"),
    ],
)
def test_hypothesis_content_guards(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _hypothesis_content(**changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"is_review_or_meta_analysis": 0}, "布林"),
        ({"country": "TW"}, "country"),
        ({"sample_size": 0}, "sample_size"),
        ({"limitations": " "}, "limitations"),
        ({"dropped_context_value_count": -1}, "dropped_context_value_count"),
        ({"corrected_support_count": None}, "corrected_support_count"),
        ({"constructs": [_mention()]}, "constructs"),
        ({"hypotheses": [_hypothesis_content()]}, "hypotheses"),
        ({"constructs": (_mention(), _mention())}, "重複"),
        ({"hypotheses": (_hypothesis_content(ordinal=2),)}, "ordinal"),
        (
            {"hypotheses": (_hypothesis_content(), _hypothesis_content(ordinal=2))},
            "同一句",
        ),
        ({"hypotheses": (_hypothesis_content(effect="satisfaction"),)}, "這篇的構面"),
    ],
)
def test_research_model_content_guards(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _model_content(**changes)


def _result(**changes: object) -> NewAiResult:
    values: dict[str, object] = {
        "paper_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "input_kind": InputKind.ABSTRACT,
        "input_sha256": "a" * 64,
        "abstract_id": None,
        "lang": OutputLang.EN,
        "status": ResultStatus.ACTIVE,
        "reject_reason": None,
    }
    values.update(changes)
    return NewAiResult(**values)  # type: ignore[arg-type]


def test_result_carries_research_model() -> None:
    content = _model_content()

    assert _result(research_model=content).research_model is content


def test_rejected_result_cannot_carry_research_model() -> None:
    with pytest.raises(ValueError, match="rejected"):
        _result(
            status=ResultStatus.REJECTED,
            reject_reason=ErrorCode.RESEARCH_MODEL_QUOTE_NOT_IN_INPUT,
            research_model=_model_content(),
        )


def test_result_carries_only_one_kind_of_content() -> None:
    with pytest.raises(ValueError, match="一種內容"):
        _result(
            research_model=_model_content(),
            classification=ClassificationContent(("erp",), (), 0),
        )


# --- LlmJob 的防呆 --------------------------------------------------------------------------------


def _job(**changes: object) -> LlmJob:
    values: dict[str, object] = {
        "paper_id": uuid.uuid4(),
        "task": LlmTask.EXTRACT_MODEL,
        "input_kind": InputKind.ABSTRACT,
        "lang": OutputLang.EN,
        "context_values_version": CONTEXT_VALUES.version_label,
        "context_values": CONTEXT_VALUES,
        "validate_output": ResearchModelValidator(CONTEXT_VALUES),
    }
    values.update(changes)
    return LlmJob(**values)  # type: ignore[arg-type]


def test_llm_job_accepts_matching_context_values() -> None:
    assert _job().context_values is CONTEXT_VALUES


@pytest.mark.parametrize("version", [None, "2", "v1"])
def test_llm_job_refuses_context_values_version_that_does_not_match(version: str | None) -> None:
    with pytest.raises(ValueError, match="context_values_version"):
        _job(context_values_version=version)


def test_llm_job_refuses_context_values_for_other_tasks() -> None:
    with pytest.raises(ValueError, match="extract_model"):
        _job(task=LlmTask.ZH_SUMMARY, lang=OutputLang.ZH_HANT)


def test_llm_job_refuses_extract_model_without_context_values() -> None:
    # 建構時就擋：少了清單的 extract_model 不會走到閘門，不呼叫模型、不記帳。
    with pytest.raises(ValueError, match="一定要帶情境值清單"):
        _job(context_values=None)


def test_llm_job_refuses_extract_model_without_a_validator() -> None:
    # 沒有 domain 驗證的話，不合格的輸出要到寫入才被發現（已付費、之後接續一直失敗）。
    with pytest.raises(ValueError, match="ResearchModelValidator"):
        _job(validate_output=None)


def test_llm_job_refuses_a_validator_of_another_kind_or_list() -> None:
    other_list = ContextValues.model_validate(
        CONTEXT_VALUES.model_dump(mode="json") | {"version": 2}
    )
    for validator in (lambda output, llm_input: None, ResearchModelValidator(other_list)):
        with pytest.raises(ValueError, match="同一份情境值清單"):
            _job(validate_output=validator)


# --- Codex 主審查後補 ---------------------------------------------------------------------------

# 3. p 值：只有確切值（p = 值）才存；只寫門檻存空值並計數。


@pytest.mark.parametrize(
    ("written", "p_value", "stored", "cleared"),
    [
        ("p = .01", 0.01, Decimal("0.01"), 0),
        ("p-value = 0.01", 0.01, Decimal("0.01"), 0),
        ("p: 0.01", 0.01, Decimal("0.01"), 0),
        ("p < .01", 0.01, None, 1),
        ("p<0.01", 0.01, None, 1),
        ("p ≤ .01", 0.01, None, 1),
        ("P > .01", 0.01, None, 1),
    ],
)
def test_p_value_is_kept_only_when_written_as_an_exact_value(
    written: str, p_value: float, stored: Decimal | None, cleared: int
) -> None:
    sentence = SENTENCE.replace("p = .01", written)
    data = _data()
    data["hypotheses"][0] |= {"sentence": sentence, "result_quote": sentence}
    data["hypotheses"][0]["statistics"]["p_value"] = p_value

    content = _validate(data, _input(abstract=ABSTRACT.replace("p = .01", written)))

    assert content.hypotheses[0].p_value == stored
    assert content.cleared_value_count == cleared


def test_p_value_threshold_in_the_sentence_wins_over_an_exact_value_elsewhere() -> None:
    # 原句寫的是門檻；別處剛好有 p = .01（別的關係）也不算這條的確切值。
    sentence = SENTENCE.replace("p = .01", "p < .01")
    abstract = ABSTRACT.replace("p = .01", "p < .01") + " Loyalty rose with age (p = .01)."
    data = _data()
    data["hypotheses"][0] |= {"sentence": sentence, "result_quote": sentence}

    content = _validate(data, _input(abstract=abstract))

    assert content.hypotheses[0].p_value is None
    assert content.cleared_value_count == 1


def test_p_value_not_written_as_p_is_cleared() -> None:
    # 0.01 只是別的數字（不是 p 值）：不當成確切的 p 值。
    sentence = "Perceived usefulness positively affects trust (β = 0.42)"
    abstract = ABSTRACT.replace(", p = .01", "") + " The error rate was 0.01."
    data = _data()
    data["hypotheses"][0] |= {"sentence": sentence, "result_quote": sentence}

    content = _validate(data, _input(abstract=abstract))

    assert content.hypotheses[0].p_value is None
    assert content.cleared_value_count == 1


# 4、5. 結構化數字：負號隔空白、科學記號完整讀出；量級只認完整的。


@pytest.mark.parametrize(
    ("written", "beta", "ok"),
    [
        ("β = - 0.42", -0.42, True),
        ("β = - 0.42", 0.42, False),
        ("β = 4.2 × 10^-2", 0.042, True),
        ("β = 4.2 × 10^-2", 4.2, False),
        ("β = 4.2 x 10-2", 0.042, True),
        ("β = 4.2 × 10⁻²", 0.042, True),
        ("β = 4.2 × 10⁻²", 4.2, False),
        ("β = 4.2 × 10⁻²", 10, False),
    ],
)
def test_beta_must_match_the_whole_written_number(written: str, beta: float, ok: bool) -> None:
    abstract = ABSTRACT.replace("β = 0.42", written)
    data = _data()
    sentence = "Perceived usefulness positively affects trust"
    data["hypotheses"][0] |= {"sentence": sentence}
    data["hypotheses"][0]["statistics"]["beta"] = beta

    if ok:
        content = _validate(data, _input(abstract=abstract))
        assert content.hypotheses[0].beta == Decimal(repr(beta))
    else:
        assert (
            _rejected(data, _input(abstract=abstract))
            is ErrorCode.RESEARCH_MODEL_UNSUPPORTED_CONTENT
        )


@pytest.mark.parametrize(
    ("written", "sample_size", "ok"),
    [
        ("12 million bank customers", 12_000_000, True),
        ("12 million bank customers", 12, False),
        ("13 thousand bank customers", 13_000, True),
        ("13 thousand bank customers", 13, False),
        ("two hundred bank customers", 200, True),
        ("two hundred bank customers", 2, False),
    ],
)
def test_sample_size_needs_the_full_magnitude(written: str, sample_size: int, ok: bool) -> None:
    abstract = ABSTRACT.replace("200 bank customers", written)
    data = _data()
    data["context"]["sample_size"] = sample_size
    data["hypotheses"][0]["statistics"]["sample_size"] = None

    if ok:
        assert _validate(data, _input(abstract=abstract)).sample_size == sample_size
    else:
        assert (
            _rejected(data, _input(abstract=abstract))
            is ErrorCode.RESEARCH_MODEL_UNSUPPORTED_CONTENT
        )


# 6. 較長的別的地名：北韓全名、中華民國、New England、World Bank 不算寫出。


@pytest.mark.parametrize(
    ("abstract", "country", "industry", "kept"),
    [
        ("Data from the Democratic People's Republic of Korea banks.", "kr", None, False),
        ("Data from the Democratic People’s Republic of Korea.", "kr", None, False),
        ("Data from the Republic of Korea.", "kr", None, True),
        ("Firms in the Republic of China (Taiwan) were surveyed.", "cn", None, False),
        ("Firms in the People's Republic of China were surveyed.", "cn", None, True),
        ("Firms in the Republic of China and mainland China were surveyed.", "cn", None, True),
        ("Hospitals in New England were surveyed.", "uk", None, False),
        ("Hospitals in New England and England were surveyed.", "uk", None, True),
        ("Firms in British Columbia were surveyed.", "uk", None, False),
        ("American Indian communities in India were surveyed.", "in", None, True),
        ("American Indian communities were surveyed.", "in", None, False),
        ("We use World Bank data on hospitals.", None, "financial_services", False),
        ("We use World Bank data on retail banks.", None, "financial_services", True),
    ],
)
def test_longer_other_names_do_not_count_as_written(
    abstract: str, country: str | None, industry: str | None, kept: bool
) -> None:
    data = _data()
    data["context"] |= {"country": country, "industry": industry, "sample_size": None}
    data["limitations"] = None
    data["future_research"] = None
    data["constructs"] = []
    data["hypotheses"] = []

    content = _validate(data, _input(title="A study", abstract=abstract))

    stored = content.country if country is not None else content.industry
    assert stored == (country or industry if kept else None)
    assert content.unsupported_context_value_count == (0 if kept else 1)


# 7. 結果原文（FR-3 擷取規則 1「報告了結果才判定」）。


@pytest.mark.parametrize(
    ("result_quote", "abstract_extra"),
    [
        (None, ""),
        ("   ", ""),
        ("Trust strongly increases loyalty in every study", ""),  # 不在輸入裡
        (
            "We hypothesize that trust increases loyalty",
            " We hypothesize that trust increases loyalty.",
        ),
        ("Trust is expected to increase loyalty", " Trust is expected to increase loyalty."),
        ("One limitation is the single-country sample", ""),  # 在輸入裡，但沒寫結果
    ],
)
def test_decided_support_without_a_reported_result_becomes_not_stated(
    result_quote: str | None, abstract_extra: str
) -> None:
    data = _data()
    data["hypotheses"][0]["result_quote"] = result_quote

    content = _validate(data, _input(abstract=ABSTRACT + abstract_extra))

    hypothesis = content.hypotheses[0]
    assert hypothesis.support is SupportLevel.NOT_STATED
    assert hypothesis.result_quote is None
    assert content.corrected_support_count == 1


@pytest.mark.parametrize(
    ("result_quote", "abstract_extra", "support"),
    [
        (SENTENCE, "", "supported"),
        ("H1 was supported", " H1 was supported.", "supported"),
        (
            "the effect was not significant",
            " However, the effect was not significant.",
            "not_supported",
        ),
        ("Results partially support H2", " Results partially support H2.", "partially_supported"),
        (
            "we found that trust increases loyalty",
            " Overall, we found that trust increases loyalty.",
            "supported",
        ),
    ],
)
def test_decided_support_with_a_reported_result_is_kept(
    result_quote: str, abstract_extra: str, support: str
) -> None:
    data = _data()
    data["hypotheses"][0] |= {"result_quote": result_quote, "support": support}

    content = _validate(data, _input(abstract=ABSTRACT + abstract_extra))

    hypothesis = content.hypotheses[0]
    assert hypothesis.support is SupportLevel(support)
    assert hypothesis.result_quote == result_quote
    assert content.corrected_support_count == 0


def test_result_quote_is_dropped_when_support_is_not_decided() -> None:
    data = _data()
    data["hypotheses"][0]["support"] = "not_stated"

    content = _validate(data)

    assert content.hypotheses[0].result_quote is None
    assert content.corrected_support_count == 0


def test_result_quote_with_markup_rejects_the_whole_result() -> None:
    data = _data()
    data["hypotheses"][0]["result_quote"] = "H1 was supported&amp;"

    assert _rejected(data) is ErrorCode.RESEARCH_MODEL_UNSAFE_TEXT


def test_hypothesis_content_requires_result_quote_exactly_when_decided() -> None:
    with pytest.raises(ValueError, match="result_quote"):
        _hypothesis_content(result_quote=None)
    with pytest.raises(ValueError, match="result_quote"):
        _hypothesis_content(support=SupportLevel.NOT_STATED)
    _hypothesis_content(support=SupportLevel.NOT_STATED, result_quote=None)


# 8. 只有標點的選填原文欄位：清成空值並計數。


@pytest.mark.parametrize(
    "path",
    [
        "limitations",
        "future_research",
        "constructs.0.definition",
        "constructs.1.measurement.scale_source",
        "constructs.1.measurement.item_location",
        "hypotheses.0.statistics.effect_type",
    ],
)
@pytest.mark.parametrize("value", ["-", " - ", "—", "(", "...", "–;"])
def test_punctuation_only_quote_field_is_cleared_and_counted(path: str, value: str) -> None:
    data = _data()
    _set(data, path, value)

    content = _validate(data)

    stored = {
        "limitations": content.limitations,
        "future_research": content.future_research,
        "constructs.0.definition": content.constructs[0].definition,
        "constructs.1.measurement.scale_source": content.constructs[1].scale_source,
        "constructs.1.measurement.item_location": content.constructs[1].item_location,
        "hypotheses.0.statistics.effect_type": content.hypotheses[0].effect_type,
    }[path]
    assert stored is None
    assert content.cleared_value_count == 1


def test_blank_quote_field_is_not_counted_as_cleared() -> None:
    data = _data()
    data["limitations"] = "   "

    content = _validate(data)

    assert content.limitations is None
    assert content.cleared_value_count == 0


# 1. 模糊測試：不可信的輸出只會通過或丟 OutputRejectedError，不會丟別的例外。

_FUZZ_TEXT = st.one_of(
    st.none(),
    st.text(st.characters(codec="utf-8"), max_size=30),
    st.sampled_from(
        [
            SENTENCE,
            "Trust (🅰)",
            "Perceived usefulness (PU)",
            "-",
            "p < .01",
            "β = - 0.42",
            "4.2 × 10^-2",
            "H1 was supported",
            "信任",
            "Democratic People's Republic of Korea",
        ]
    ),
)
_FUZZ_NONEMPTY = st.one_of(
    st.text(st.characters(codec="utf-8"), min_size=1, max_size=30),
    st.sampled_from([SENTENCE, "Trust (🅰)", "-", "β = - 0.42", "信任", "Trust"]),
)
_FUZZ_NUMBER = st.one_of(
    st.none(),
    st.integers(min_value=-(2**40), max_value=2**40),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.sampled_from([0.42, -0.42, 0.01, 200, 4, 0.91, 0.042, 12_000_000]),
)


def _fuzzed(source: st.DataObject) -> dict[str, Any]:
    draw = source.draw
    data = _data()
    data["limitations"] = draw(_FUZZ_TEXT)
    data["future_research"] = draw(_FUZZ_TEXT)
    data["context"]["country"] = draw(st.sampled_from([None, "tw", "kr", "cn", "zz"]))
    data["context"]["sample_size"] = draw(st.one_of(st.none(), st.integers(-5, 2**32)))
    for construct in data["constructs"]:
        construct["name_en"] = draw(st.one_of(_FUZZ_NONEMPTY, st.just(construct["name_en"])))
        construct["label_zh"] = draw(st.one_of(_FUZZ_NONEMPTY, st.just(construct["label_zh"])))
        construct["definition"] = draw(_FUZZ_TEXT)
    measurement = data["constructs"][1]["measurement"]
    measurement["scale_source"] = draw(_FUZZ_TEXT)
    measurement["item_location"] = draw(_FUZZ_TEXT)
    measurement["cronbach_alpha"] = draw(_FUZZ_NUMBER)
    hypothesis = data["hypotheses"][0]
    hypothesis["sentence"] = draw(st.one_of(_FUZZ_NONEMPTY, st.just(SENTENCE)))
    hypothesis["cause"] = draw(st.one_of(_FUZZ_NONEMPTY, st.just("Perceived usefulness")))
    hypothesis["result_quote"] = draw(_FUZZ_TEXT)
    hypothesis["support"] = draw(st.sampled_from([level.value for level in SupportLevel]))
    hypothesis["evidence_type"] = draw(st.sampled_from([kind.value for kind in EvidenceType]))
    hypothesis["statistics"]["beta"] = draw(_FUZZ_NUMBER)
    hypothesis["statistics"]["p_value"] = draw(_FUZZ_NUMBER)
    hypothesis["statistics"]["effect_type"] = draw(_FUZZ_TEXT)
    data["hypotheses"].append(copy.deepcopy(hypothesis) | {"sentence": draw(_FUZZ_NONEMPTY)})
    return data


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.data())
def test_untrusted_output_only_passes_or_is_rejected(data: st.DataObject) -> None:
    raw = _fuzzed(data)
    try:
        output = ResearchModelOutput.model_validate(raw)
    except ValidationError:
        return
    with contextlib.suppress(OutputRejectedError):
        validate_research_model(output, INPUT, CONTEXT_VALUES)


def _slice_of_input(source: st.DataObject) -> str | None:
    """輸入裡的一段（大多會通過原文比對，好走到更後面的檢查）。"""
    draw = source.draw
    words = draw(st.sampled_from([TITLE, ABSTRACT])).split(" ")
    start = draw(st.integers(0, len(words) - 1))
    end = draw(st.integers(start + 1, min(len(words), start + 12)))
    piece = " ".join(words[start:end])
    return draw(st.one_of(st.none(), st.just(piece), st.sampled_from(["-", " ", "…"])))


_SENTENCES = (
    SENTENCE,
    "Perceived usefulness positively affects trust",
    "positively affects trust (β = 0.42, p = .01)",
    QUALITATIVE_SENTENCE,
    "Interviews with managers suggest that trust shapes loyalty. One limitation",
)
_INPUT_NUMBERS = st.sampled_from([None, 0.42, -0.42, 0.01, 200, 4, 0.91, 0.93, 0.78, 2003, 1, 0.5])


def _fuzzed_near_valid(source: st.DataObject) -> dict[str, Any]:
    draw = source.draw
    data = _with_loyalty()
    data["limitations"] = _slice_of_input(source)
    data["future_research"] = _slice_of_input(source)
    data["context"] |= {
        "country": draw(st.sampled_from([None, "tw", "kr", "cn", "zz"])),
        "industry": draw(st.sampled_from([None, "financial_services", "retail", "zz"])),
        "sample_size": draw(st.sampled_from([None, 200, 4, 1, 3])),
    }
    for construct in data["constructs"]:
        construct["definition"] = _slice_of_input(source)
    data["constructs"][1]["measurement"] |= {
        "scale_source": _slice_of_input(source),
        "item_location": _slice_of_input(source),
        "item_count": draw(st.sampled_from([None, 4, 1, 5])),
        "cronbach_alpha": draw(_INPUT_NUMBERS.filter(lambda x: x is None or 0 <= x <= 1)),
    }
    names = ["Perceived usefulness", "Trust", "Loyalty", "TRUST (T)", "trust;"]
    hypotheses = []
    for _ in range(draw(st.integers(0, 4))):
        statistics = draw(
            st.one_of(
                st.none(),
                st.fixed_dictionaries(
                    {
                        "beta": _INPUT_NUMBERS,
                        "p_value": _INPUT_NUMBERS.filter(lambda x: x is None or 0 <= x <= 1),
                        "sample_size": st.sampled_from([None, 200, 4]),
                        "effect_type": st.builds(lambda: None),
                    }
                ),
            )
        )
        hypotheses.append(
            {
                "sentence": draw(st.sampled_from(_SENTENCES)),
                "cause": draw(st.sampled_from(names)),
                "effect": draw(st.sampled_from(names)),
                "evidence_type": draw(st.sampled_from([kind.value for kind in EvidenceType])),
                "support": draw(st.sampled_from([level.value for level in SupportLevel])),
                "result_quote": _slice_of_input(source),
                "statistics": statistics,
            }
        )
    data["hypotheses"] = hypotheses
    return data


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.data())
def test_near_valid_output_only_passes_or_is_rejected(data: st.DataObject) -> None:
    # 大多數欄位取自輸入：走到情境、假設、p 值、結果原文、來源句子這些後段的檢查。
    output = ResearchModelOutput.model_validate(_fuzzed_near_valid(data))
    try:
        content = validate_research_model(output, INPUT, CONTEXT_VALUES)
    except OutputRejectedError:
        return
    assert content.cleared_value_count >= 0
