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
import time
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
    _reported_part,
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


@pytest.mark.parametrize(
    "lead",
    [
        "Using data from U.S. firms,",
        "Using data from U.K. firms,",
        "For large banks, e.g. HSBC,",
        "For older users, i.e. retirees,",
        "Following Smith et al. and others,",
        "Comparing apps vs. websites,",
    ],
)
def test_abbreviation_periods_do_not_end_the_source_sentence(lead: str) -> None:
    # 確認審查：U.S.、e.g. 的點原本被當成句尾，同一句的片段和完整版又算成兩句。
    fragment = "we find that perceived usefulness raises trust"
    full = f"{lead} {fragment}."
    data = _data()
    data["hypotheses"] += [
        _hypothesis(sentence=full, result_quote=fragment, statistics=None),
        _hypothesis(sentence=fragment, result_quote=fragment, statistics=None),
    ]

    content = _validate(data, _input(abstract=ABSTRACT + " " + full))

    assert len(content.hypotheses) == 2
    assert content.hypotheses[1].sentence_sha256 == hypothesis_sentence_sha256(
        quote_key(full).strip(" .,;:")
    )


def test_duplicate_is_compared_by_the_values_the_model_gave() -> None:
    # 確認審查：模型給的值一樣，只是其中一筆的結果原文不能採信、被改成 not_stated，原本會被當成
    # 衝突、整篇拒收。現在比模型原本給的值，留結果原文可以採信的那筆。
    weak = "One limitation is the single-country sample"
    fragment = "Perceived usefulness positively affects trust"
    later_weak = _data()
    later_weak["hypotheses"].append(_hypothesis(sentence=fragment, result_quote=weak))
    first_weak = _data()
    first_weak["hypotheses"].insert(0, _hypothesis(sentence=fragment, result_quote=weak))

    for data in (later_weak, first_weak):
        content = _validate(data)

        assert len(content.hypotheses) == 1
        kept = content.hypotheses[0]
        assert (kept.ordinal, kept.support, kept.result_quote) == (
            1,
            SupportLevel.SUPPORTED,
            SENTENCE,
        )
        assert content.corrected_support_count == 0


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
        # 確認審查：科學記號整段不取數字，輸出裡有就當讀不懂。
        ("constructs.0.label_zh", "有用性 9.9 x 10-7"),
        ("constructs.0.label_zh", "有用性 9.9 * 10-7"),
        ("constructs.0.label_zh", "有用性 9.9·10+3"),
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


def test_p_value_in_the_result_quote_wins_over_an_exact_value_elsewhere() -> None:
    # 確認審查：原句沒寫 p 值時先看結果原文，結果原文寫的是門檻，別處的 p = .01 不算。
    result = SENTENCE.replace("p = .01", "p < .01").rstrip(".")
    abstract = ABSTRACT.replace("p = .01", "p < .01") + " Loyalty rose with age (p = .01)."
    data = _data()
    data["hypotheses"][0] |= {
        "sentence": "Perceived usefulness positively affects trust",
        "result_quote": result,
    }

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
        # 確認審查：常見的假設句寫法（poc_26 情境一）。
        (
            "H1 posits that trust has a significant effect on loyalty",
            " H1 posits that trust has a significant effect on loyalty.",
        ),
        (
            "H2 predicts that trust significantly increases loyalty",
            " H2 predicts that trust significantly increases loyalty.",
        ),
        (
            "H1: trust significantly increases loyalty",
            " H1: trust significantly increases loyalty.",
        ),
        ("H1a: trust shows a positive effect", " H1a: trust shows a positive effect."),
        (
            "We test whether trust significantly increases loyalty",
            " We test whether trust significantly increases loyalty.",
        ),
        (
            "we examine whether trust results in loyalty",
            " Specifically, we examine whether trust results in loyalty.",
        ),
        ("Trust is proposed to increase loyalty", " Trust is proposed to increase loyalty."),
        # 確認審查：結果原文要提到這條關係（PU → trust）的因或果（poc_26 情境二）。
        (
            "Results show that satisfaction increases loyalty",
            " Results show that satisfaction increases loyalty.",
        ),
        (
            "The findings have implications for theory",
            " The findings have implications for theory.",
        ),
        # 0-A（決定 14 選 B）之後仍然擋：原句（SENTENCE）沒有 H1，代號對不上這條關係。原句有 H1
        # 的情況判成有結果，見 test_result_quote_matching_the_relation_by_label_all_or_abbreviation
        # _is_kept。
        ("H1 was supported", " H1 was supported."),
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
        ("H1 on trust was supported", " H1 on trust was supported.", "supported"),
        ("H1 (trust): supported", " H1 (trust): supported.", "supported"),
        (
            "the effect on trust was not significant",
            " However, the effect on trust was not significant.",
            "not_supported",
        ),
        (
            "Results partially support H2 on trust",
            " Results partially support H2 on trust.",
            "partially_supported",
        ),
        (
            "Perceived Usefulness (PU) was found to matter",
            " Perceived Usefulness (PU) was found to matter.",
            "supported",
        ),
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


def test_cleared_effect_type_of_a_dropped_duplicate_is_not_counted() -> None:
    # 確認審查：只數存下來的；重複的那筆被去掉，它的效果類型不算。
    data = _data()
    data["hypotheses"][0]["statistics"]["effect_type"] = "-"
    data["hypotheses"].append(copy.deepcopy(data["hypotheses"][0]))

    content = _validate(data)

    assert len(content.hypotheses) == 1
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


# --- 0-A（Kevin 2026-11-15）-----------------------------------------------------------------
# 決定 13 選 B：字母縮寫的量級（12M、10k、3.5K、2B）讀不懂，模型照縮寫填的數字整筆拒收。


@pytest.mark.parametrize(
    ("written", "sample_size"),
    [
        ("12M bank customers", 12),
        ("12M bank customers", 12_000_000),
        ("12m bank customers", 12),
        ("12 M bank customers", 12),
        ("12 m bank customers", 12_000_000),
        ("10k bank customers", 10),
        ("10K bank customers", 10_000),
        ("10 k bank customers", 10),
        ("10 K bank customers", 10_000),
        ("3.5K bank customers", 3_500),
        ("3.5 k bank customers", 3_500),
        ("2B bank customers", 2),
        ("2b bank customers", 2_000_000_000),
        ("2 B bank customers", 2),
        ("1,200K bank customers", 1_200),
        ("5bn bank customers", 5),
    ],
)
def test_sample_size_from_an_abbreviated_magnitude_is_rejected(
    written: str, sample_size: int
) -> None:
    abstract = ABSTRACT.replace("200 bank customers", written)
    data = _data()
    data["context"]["sample_size"] = sample_size
    data["hypotheses"][0]["statistics"]["sample_size"] = None

    assert (
        _rejected(data, _input(abstract=abstract)) is ErrorCode.RESEARCH_MODEL_UNSUPPORTED_CONTENT
    )


def test_sample_size_null_is_fine_when_the_input_abbreviates_the_magnitude() -> None:
    # prompt v2 規則 5：縮寫的量級填 null。
    abstract = ABSTRACT.replace("200 bank customers", "12M bank customers")
    data = _data()
    data["context"]["sample_size"] = None
    data["hypotheses"][0]["statistics"]["sample_size"] = None

    assert _validate(data, _input(abstract=abstract)).sample_size is None


# 決定 14 選 B：結果原文對得上這條關係的方式放寬（假設代號、全部假設、研究問題後面的結果、縮寫）。

H1_SENTENCE = "H1: Perceived usefulness (PU) positively affects trust (TR)."
H2_SENTENCE = "H2: Trust positively affects loyalty (LOY)."
LABELLED_ABSTRACT = (
    "Based on a survey of 200 bank customers in Taiwan, we test a model of mobile banking"
    f" adoption. {H1_SENTENCE} {H2_SENTENCE} "
    "We propose that perceived usefulness affects trust (H1a) and that trust affects loyalty"
    " (H2a). "
    "H3: Satisfaction increases loyalty. "
    "We examine whether perceived usefulness shapes trust. "
    "H1 was supported. H2 was supported. H3 was supported. H1a was supported. H2a was supported. "
    "Results support H1 and H2. All hypotheses were supported. "
    "PU significantly affects TR (β = 0.42). TR significantly affects LOY (β = 0.31). "
    "SA significantly affects LO (β = 0.27). "
    "Results for H1 are reported in the appendix. All hypotheses were tested with PLS-SEM. "
    "All hypotheses except H2 were supported. Not all hypotheses were supported. "
    "We test whether H1 is supported. "
    "H1 posits that perceived usefulness significantly increases trust."
)


def _labelled(
    sentence: str,
    result_quote: str,
    *,
    cause: str = "Perceived usefulness",
    effect: str = "Trust",
    abstract: str = LABELLED_ABSTRACT,
) -> HypothesisContent:
    """用 `abstract`（預設 `LABELLED_ABSTRACT`）當輸入，驗一條判了 supported 的關係，回存下來的
    那一條。"""
    data = _with_loyalty(
        _hypothesis(
            sentence=sentence,
            cause=cause,
            effect=effect,
            result_quote=result_quote,
            statistics=None,
        )
    )
    data["limitations"] = None
    data["future_research"] = None
    for construct in data["constructs"]:
        construct |= {"definition": None, "measurement": None}
    (hypothesis,) = _validate(data, _input(abstract=abstract)).hypotheses
    return hypothesis


@pytest.mark.parametrize(
    ("sentence", "result_quote", "cause", "effect"),
    [
        # 原句只有一個代號，結果原文寫了同一個代號（Kevin 指定的必備測試）。
        (H1_SENTENCE, "H1 was supported", "Perceived usefulness", "Trust"),
        (H1_SENTENCE, "Results support H1 and H2", "Perceived usefulness", "Trust"),
        (H2_SENTENCE, "Results support H1 and H2", "Trust", "Loyalty"),
        (H2_SENTENCE, "H2 was supported", "Trust", "Loyalty"),
        # 全部假設：原句本身是一條假設（有代號）。
        (H1_SENTENCE, "All hypotheses were supported", "Perceived usefulness", "Trust"),
        # 全部假設：原句沒有代號，但有假設句的寫法（we propose that）。
        (
            "We propose that perceived usefulness affects trust (H1a) and that trust affects"
            " loyalty (H2a).",
            "All hypotheses were supported",
            "Perceived usefulness",
            "Trust",
        ),
        # 只用縮寫：PU 是 Perceived usefulness 的首字母，TR 在輸入裡寫成 trust (TR)。
        (H1_SENTENCE, "PU significantly affects TR (β = 0.42)", "Perceived usefulness", "Trust"),
        (
            "PU significantly affects TR (β = 0.42).",
            "PU significantly affects TR (β = 0.42)",
            "Perceived usefulness",
            "Trust",
        ),
        # 只靠輸入裡的「名稱 (縮寫)」：Trust、Loyalty 都是一個詞，沒有首字母縮寫。
        (H2_SENTENCE, "TR significantly affects LOY (β = 0.31)", "Trust", "Loyalty"),
    ],
)
def test_result_quote_matching_the_relation_by_label_all_or_abbreviation_is_kept(
    sentence: str, result_quote: str, cause: str, effect: str
) -> None:
    hypothesis = _labelled(sentence, result_quote, cause=cause, effect=effect)

    assert hypothesis.support is SupportLevel.SUPPORTED
    assert hypothesis.result_quote == result_quote


@pytest.mark.parametrize(
    ("sentence", "result_quote"),
    [
        # 另一條關係的結果：原句是 H1，結果原文只寫 H2、H3。
        (H1_SENTENCE, "H2 was supported"),
        (H1_SENTENCE, "H3 was supported"),
        # H1 不等於 H1a。
        (H1_SENTENCE, "H1a was supported"),
        # 原句有兩個代號：分不出這條關係是哪一個，保守不判（連對的 H1a 也不判）。
        (
            "We propose that perceived usefulness affects trust (H1a) and that trust affects"
            " loyalty (H2a).",
            "H2a was supported",
        ),
        (
            "We propose that perceived usefulness affects trust (H1a) and that trust affects"
            " loyalty (H2a).",
            "H1a was supported",
        ),
        # 原句的代號對得上，但原句講的不是這條關係（沒有提到 PU 或 trust）。
        ("H3: Satisfaction increases loyalty.", "H3 was supported"),
        # 代號對得上，但只描述研究設計或報告位置，沒有判定字或統計量。
        (H1_SENTENCE, "Results for H1 are reported in the appendix"),
        (H1_SENTENCE, "All hypotheses were tested with PLS-SEM"),
        # 全部假設，但有例外或不是全部。
        (H1_SENTENCE, "All hypotheses except H2 were supported"),
        (H1_SENTENCE, "Not all hypotheses were supported"),
        # 全部假設，但原句不是假設（是結果句，或只是研究問題）。
        ("PU significantly affects TR (β = 0.42).", "All hypotheses were supported"),
        ("We examine whether perceived usefulness shapes trust.", "All hypotheses were supported"),
        # 研究問題裡的判定字不是結果；假設句照舊不算。
        (H1_SENTENCE, "We test whether H1 is supported"),
        (H1_SENTENCE, "H1 posits that perceived usefulness significantly increases trust"),
        # 縮寫對不上這條關係的構面。
        (H1_SENTENCE, "SA significantly affects LO (β = 0.27)"),
    ],
)
def test_result_quote_not_matching_the_relation_still_becomes_not_stated(
    sentence: str, result_quote: str
) -> None:
    hypothesis = _labelled(sentence, result_quote)

    assert hypothesis.support is SupportLevel.NOT_STATED
    assert hypothesis.result_quote is None


@pytest.mark.parametrize(
    ("result_quote", "abstract_extra"),
    [
        (
            "we examine whether perceived usefulness increases trust and find that it does",
            " Specifically, we examine whether perceived usefulness increases trust and find that"
            " it does.",
        ),
        (
            "We test whether trust rises with perceived usefulness; the results show that it does",
            " We test whether trust rises with perceived usefulness; the results show that it"
            " does.",
        ),
        (
            "We examine whether trust depends on perceived usefulness, and we find strong support",
            " We examine whether trust depends on perceived usefulness, and we find strong"
            " support.",
        ),
    ],
)
def test_result_after_a_research_question_in_the_same_sentence_is_kept(
    result_quote: str, abstract_extra: str
) -> None:
    data = _data()
    data["hypotheses"][0]["result_quote"] = result_quote

    content = _validate(data, _input(abstract=ABSTRACT + abstract_extra))

    assert content.hypotheses[0].support is SupportLevel.SUPPORTED
    assert content.hypotheses[0].result_quote == result_quote
    assert content.corrected_support_count == 0


@pytest.mark.parametrize(
    ("result_quote", "abstract_extra"),
    [
        # 結果寫法在研究問題的同一個子句裡（沒有斷點）：仍是問題，不是結果。
        (
            "we test whether trust is supported by perceived usefulness",
            " Finally, we test whether trust is supported by perceived usefulness.",
        ),
        (
            "we examine whether users find that perceived usefulness builds trust",
            " Finally, we examine whether users find that perceived usefulness builds trust.",
        ),
        # 斷點後面不是主詞（是另一個構面），不算。
        (
            "we examine whether perceived usefulness and trust were confirmed",
            " Finally, we examine whether perceived usefulness and trust were confirmed.",
        ),
        # 研究問題後面只有 results、significant 這種一般的結果用語，不算。
        (
            "we examine whether perceived usefulness significantly affects trust, and results"
            " follow",
            " Finally, we examine whether perceived usefulness significantly affects trust, and"
            " results follow.",
        ),
        # 前面有假設句寫法的照舊不算（只放寬研究問題）。
        (
            "We hypothesize that perceived usefulness increases trust and find that it does",
            " We hypothesize that perceived usefulness increases trust and find that it does.",
        ),
    ],
)
def test_research_question_without_a_separate_result_still_becomes_not_stated(
    result_quote: str, abstract_extra: str
) -> None:
    data = _data()
    data["hypotheses"][0]["result_quote"] = result_quote

    content = _validate(data, _input(abstract=ABSTRACT + abstract_extra))

    assert content.hypotheses[0].support is SupportLevel.NOT_STATED
    assert content.hypotheses[0].result_quote is None


def test_lowercase_word_is_not_read_as_an_abbreviation() -> None:
    # 「Information technology」的首字母是 it；結果原文裡小寫的 it 不是縮寫。
    abstract = ABSTRACT + " Information technology affects trust. Overall, it was significant."
    data = _data()
    data["constructs"].append(
        {
            "name_en": "Information technology",
            "label_zh": "資訊科技",
            "definition": None,
            "measurement": None,
        }
    )
    data["hypotheses"] = [
        _hypothesis(
            sentence="Information technology affects trust.",
            cause="Information technology",
            effect="Perceived usefulness",
            result_quote="it was significant",
            statistics=None,
        )
    ]

    content = _validate(data, _input(abstract=abstract))

    assert content.hypotheses[0].support is SupportLevel.NOT_STATED


def test_uppercase_initials_of_the_construct_count_as_a_mention() -> None:
    abstract = ABSTRACT + " Information technology affects trust. Overall, IT was significant."
    data = _data()
    data["constructs"].append(
        {
            "name_en": "Information technology",
            "label_zh": "資訊科技",
            "definition": None,
            "measurement": None,
        }
    )
    data["hypotheses"] = [
        _hypothesis(
            sentence="Information technology affects trust.",
            cause="Information technology",
            effect="Perceived usefulness",
            result_quote="IT was significant",
            statistics=None,
        )
    ]

    content = _validate(data, _input(abstract=abstract))

    assert content.hypotheses[0].support is SupportLevel.SUPPORTED
    assert content.hypotheses[0].result_quote == "IT was significant"


def test_label_needs_the_sentence_to_name_both_cause_and_effect() -> None:
    # H1 的原句講 PU → trust；拿它配 trust → loyalty（只共用 trust）時，H1 的結果不算這條的。
    hypothesis = _labelled(H1_SENTENCE, "H1 was supported", cause="Trust", effect="Loyalty")

    assert hypothesis.support is SupportLevel.NOT_STATED
    assert hypothesis.result_quote is None


def test_abbreviation_written_only_in_the_model_name_does_not_count() -> None:
    # 模型把構面取名成「Perceived usefulness (SA)」：輸入沒有把 SA 定義成這個構面，SA 的結果不算。
    data = _with_loyalty(
        _hypothesis(
            sentence=H1_SENTENCE,
            cause="Perceived usefulness (SA)",
            effect="Trust",
            result_quote="SA significantly affects LO (β = 0.27)",
            statistics=None,
        )
    )
    data["limitations"] = None
    data["future_research"] = None
    for construct in data["constructs"]:
        construct |= {"definition": None, "measurement": None}
    data["constructs"][0]["name_en"] = "Perceived usefulness (SA)"

    (hypothesis,) = _validate(data, _input(abstract=LABELLED_ABSTRACT)).hypotheses

    assert hypothesis.support is SupportLevel.NOT_STATED


def test_model_name_with_the_defined_abbreviation_matches_the_input_definition() -> None:
    # 模型把構面取名成「Trust (TR)」（正規化名稱是 trust tr）：輸入寫了 trust (TR)，TR 照樣算。
    data = _with_loyalty(
        _hypothesis(
            sentence=H2_SENTENCE,
            cause="Trust (TR)",
            effect="Loyalty",
            result_quote="TR significantly affects LOY (β = 0.31)",
            statistics=None,
        )
    )
    data["limitations"] = None
    data["future_research"] = None
    for construct in data["constructs"]:
        construct |= {"definition": None, "measurement": None}
    data["constructs"][1]["name_en"] = "Trust (TR)"

    (hypothesis,) = _validate(data, _input(abstract=LABELLED_ABSTRACT)).hypotheses

    assert hypothesis.support is SupportLevel.SUPPORTED


@pytest.mark.parametrize(("cause", "kept"), [("Satisfaction with trust", True), ("Trust", False)])
def test_defined_abbreviation_goes_to_the_longest_matching_construct(
    cause: str, kept: bool
) -> None:
    abstract = (
        ABSTRACT + " We measure satisfaction with trust (ST). ST rose significantly (β = 0.2)."
    )
    data = _data()
    data["constructs"].append(
        {
            "name_en": "Satisfaction with trust",
            "label_zh": "信任滿意度",
            "definition": None,
            "measurement": None,
        }
    )
    data["hypotheses"] = [
        _hypothesis(
            sentence="We measure satisfaction with trust (ST).",
            cause=cause,
            effect="Perceived usefulness",
            result_quote="ST rose significantly (β = 0.2)",
            statistics=None,
        )
    ]

    content = _validate(data, _input(abstract=abstract))

    expected = SupportLevel.SUPPORTED if kept else SupportLevel.NOT_STATED
    assert content.hypotheses[0].support is expected


@pytest.mark.parametrize(
    "unit",
    ["zz find that ", ", zz find that ", "and qq was supported ", "trust (AB) ", "(PU) "],
)
def test_long_adversarial_result_quote_is_quick(unit: str) -> None:
    # 研究問題後面塞滿結果寫法、輸入塞滿縮寫括號：驗證仍在線性時間內做完（自我審查補）。
    quote = "we examine whether trust " + unit * 5_000
    data = _data()
    data["hypotheses"][0]["result_quote"] = quote.strip()
    started = time.perf_counter()

    with contextlib.suppress(OutputRejectedError):
        _validate(data, _input(abstract=ABSTRACT + " " + quote))

    assert time.perf_counter() - started < 2


# --- Codex 主審查（PR #68）第 1～3 條：結果原文要直接陳述這條關係的結果 --------------------------
# 第 1 條：研究問題（we examine whether…）後面才接結果時，核對「提到因或果」只看寫出結果的那個子句；
# 那個子句只用代名詞指回去（and find that it does）時，才看研究問題提到的關係。


@pytest.mark.parametrize(
    "result_quote",
    [
        # Codex 的例子：研究問題提到 PU、trust，後面寫出結果的卻是 satisfaction → loyalty。
        "We examine whether perceived usefulness affects trust, and we find that satisfaction"
        " affects loyalty",
        "We examine whether perceived usefulness affects trust; the results show that satisfaction"
        " drives loyalty",
        # 結果是統計量時也一樣：統計量所在的子句講的是另一條關係。
        "We examine whether perceived usefulness affects trust, and satisfaction affects loyalty"
        " (β = 0.30)",
        # 研究問題有兩個 whether：it 指的是哪一個分不出來，不看研究問題。
        "We examine whether perceived usefulness affects trust and whether satisfaction affects"
        " loyalty, and we find that it does",
        # 寫出結果的子句只有另一個代號，不是指回研究問題。
        "We examine whether perceived usefulness affects trust, and H2 was supported",
    ],
)
def test_result_after_a_research_question_about_another_relation_becomes_not_stated(
    result_quote: str,
) -> None:
    data = _data()
    data["hypotheses"][0]["result_quote"] = result_quote

    content = _validate(data, _input(abstract=f"{ABSTRACT} {result_quote}."))

    assert content.hypotheses[0].support is SupportLevel.NOT_STATED
    assert content.hypotheses[0].result_quote is None


@pytest.mark.parametrize(
    "result_quote",
    [
        # 寫出結果的子句自己提到這條關係的因或果。
        "We examine whether perceived usefulness affects trust, and we find that perceived"
        " usefulness increases trust",
        "We examine whether perceived usefulness affects trust, and we find that trust rises",
        # 寫出結果的子句只用代名詞指回研究問題（同一條關係）。
        "We examine whether perceived usefulness affects trust, and we find that it does"
        " (β = 0.42)",
        "We examine whether perceived usefulness affects trust, and it does (β = 0.42)",
    ],
)
def test_result_after_a_research_question_about_the_same_relation_is_kept(
    result_quote: str,
) -> None:
    data = _data()
    data["hypotheses"][0]["result_quote"] = result_quote

    content = _validate(data, _input(abstract=f"{ABSTRACT} {result_quote}."))

    assert content.hypotheses[0].support is SupportLevel.SUPPORTED
    assert content.hypotheses[0].result_quote == result_quote


# 第 2、3 條：代號、全部假設要直接陳述結果（`H1 was supported`、`Results support H1`、`All
# hypotheses were supported`）；整段有 whether、to determine、if、remains unknown、will 這類未定
# 敘述時不算；範圍（`H1-H3`）兩端都不算。

NOT_YET_REPORTED = (
    "H1 was tested to determine whether the effect was significant",
    "H1 was tested to see if it was significant",
    "Whether H1 was supported remains unknown",
    "H1 was not tested, but H2 was supported",
    "Prior studies support H1",
    "H1-H3 were supported",
    "Results support H1-H3",
    "In prior work, support was found for H1",
    "All hypotheses were tested to determine whether they were supported",
    "Whether all hypotheses were supported remains unknown",
    "Future studies will test whether all hypotheses are supported",
)
DIRECTLY_REPORTED = (
    "We find support for H1",
    "H1 (β = 0.42) was supported",
    "H1 and H2 were supported",
    "Support was found for H1",
    "H1: supported",
    "We examine whether H1 holds, and we find support for H1",
    "The results support all hypotheses",
    "All of the proposed hypotheses were supported",
)
DIRECT_ABSTRACT = (
    LABELLED_ABSTRACT
    + " "
    + " ".join(f"{quote}." for quote in NOT_YET_REPORTED + DIRECTLY_REPORTED)
)


@pytest.mark.parametrize("result_quote", NOT_YET_REPORTED)
def test_label_or_all_hypotheses_without_a_direct_result_becomes_not_stated(
    result_quote: str,
) -> None:
    hypothesis = _labelled(H1_SENTENCE, result_quote, abstract=DIRECT_ABSTRACT)

    assert hypothesis.support is SupportLevel.NOT_STATED
    assert hypothesis.result_quote is None


@pytest.mark.parametrize("result_quote", DIRECTLY_REPORTED)
def test_label_or_all_hypotheses_with_a_direct_result_is_kept(result_quote: str) -> None:
    hypothesis = _labelled(H1_SENTENCE, result_quote, abstract=DIRECT_ABSTRACT)

    assert hypothesis.support is SupportLevel.SUPPORTED
    assert hypothesis.result_quote == result_quote


def test_label_list_reports_each_label_in_it() -> None:
    hypothesis = _labelled(
        H2_SENTENCE,
        "H1 and H2 were supported",
        cause="Trust",
        effect="Loyalty",
        abstract=DIRECT_ABSTRACT,
    )

    assert hypothesis.support is SupportLevel.SUPPORTED


@pytest.mark.parametrize("unit", ["H1 and ", "H1, ", "H1 & ", "Results support H1 and "])
def test_long_label_list_is_quick(unit: str) -> None:
    # 很長的代號清單後面接的不是判定字：直接陳述的比對不能變成平方時間（自我審查補）。
    quote = "Results show " + unit * 20_000 + "tested"
    started = time.perf_counter()

    with contextlib.suppress(OutputRejectedError):
        _labelled(H1_SENTENCE, quote, abstract=f"{LABELLED_ABSTRACT} {quote}.")

    assert time.perf_counter() - started < 2


# --- Codex 主審查（PR #68）第 4～6 條 ------------------------------------------------------------
# 第 4 條：輸入裡「名稱 (縮寫)」的縮寫，只給名稱完全一樣的構面；模型漏列較長的名稱時不能錯給尾端的
# 另一個構面。


def test_defined_abbreviation_is_not_given_to_a_construct_at_the_end_of_a_longer_name() -> None:
    # Codex 的例子：模型只列 Trust、Perceived usefulness、Loyalty，沒列 Satisfaction with trust。
    abstract = (
        ABSTRACT + " H1: Trust affects perceived usefulness. We measure satisfaction with trust"
        " (ST). ST significantly affects loyalty."
    )
    data = _with_loyalty(
        _hypothesis(
            sentence="H1: Trust affects perceived usefulness.",
            cause="Trust",
            effect="Perceived usefulness",
            result_quote="ST significantly affects loyalty",
            statistics=None,
        )
    )

    content = _validate(data, _input(abstract=abstract))

    assert content.hypotheses[0].support is SupportLevel.NOT_STATED
    assert content.hypotheses[0].result_quote is None


@pytest.mark.parametrize(
    ("definition", "name_en", "abbreviation", "kept"),
    [
        ("perceived ease of use (PEOU)", "Perceived ease of use", "PEOU", True),
        ("electronic word-of-mouth (eWOM)", "Electronic word-of-mouth", "eWOM", True),
        ("customer loyalty (CL)", "Customer loyalty", "CL", True),
        ("trust in technology (TT)", "Trust in technology", "TT", True),
        ("information and communication technologies (ICTs)", "Communication technologies",
         "ICTs", False),
        # 名稱是 mobile banking usage，不是 Usage（模型漏列較長的名稱）。
        ("mobile banking usage (MBU)", "Usage", "MBU", False),
    ],
)  # fmt: skip
def test_defined_abbreviation_goes_only_to_the_construct_with_the_same_name(
    definition: str, name_en: str, abbreviation: str, kept: bool
) -> None:
    result_quote = f"{abbreviation} rose significantly (β = 0.2)"
    abstract = f"{ABSTRACT} We measure {definition}. {result_quote}."
    data = _data()
    data["constructs"].append(
        {"name_en": name_en, "label_zh": "測試構面", "definition": None, "measurement": None}
    )
    data["hypotheses"] = [
        _hypothesis(
            sentence=f"We measure {definition}.",
            cause=name_en,
            effect="Perceived usefulness",
            result_quote=result_quote,
            statistics=None,
        )
    ]

    content = _validate(data, _input(abstract=abstract))

    expected = SupportLevel.SUPPORTED if kept else SupportLevel.NOT_STATED
    assert content.hypotheses[0].support is expected


# 第 5 條：量級縮寫的常見變體（12MM、12-M、12 mn）照縮寫填的數字整筆拒收。


@pytest.mark.parametrize(
    ("written", "sample_size"),
    [
        ("12MM bank customers", 12),
        ("12 MM bank customers", 12),
        ("12-M bank customers", 12),
        ("12-k bank customers", 12),
        ("12 mn bank customers", 12),
        ("12 mln bank customers", 12),
    ],
)
def test_sample_size_from_an_abbreviated_magnitude_variant_is_rejected(
    written: str, sample_size: int
) -> None:
    abstract = ABSTRACT.replace("200 bank customers", written)
    data = _data()
    data["context"]["sample_size"] = sample_size
    data["hypotheses"][0]["statistics"]["sample_size"] = None

    assert (
        _rejected(data, _input(abstract=abstract)) is ErrorCode.RESEARCH_MODEL_UNSUPPORTED_CONTENT
    )


# 第 6 條：研究問題後面只有一個斷點、接著大量結果寫法時，不能變成平方時間。


def _seconds_for_markers(repeats: int) -> float:
    # 直接量 `_reported_part`（Opus 確認審查 M1）：量整個 `_validate` 時，正規化、找原文這些線性
    # 的步驟會把平方那一段的放大效果稀釋掉（還原修正後整個 `_validate` 只放大 13.7～15.4 倍，抓
    # 不到）。
    quote = "we examine whether trust, " + "zz find that " * repeats
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        _reported_part(quote)
        best = min(best, time.perf_counter() - started)
    return best


def test_many_result_markers_after_one_clause_break_is_quick() -> None:
    # 比「輸入變 8 倍、時間變幾倍」，不比絕對秒數（CI 機器比本機慢，絕對秒數會時好時壞）：修正後
    # 約 8 倍（線性），修正前約 47 倍（平方時間）；門檻取 20 倍，另加 0.01 秒吸收很短時間的雜訊。
    # 已用還原修正確認會失敗：把 `_result_after_question` 的條件還原成 `if position == 0:` 後，這個
    # 測試不通過。
    small = _seconds_for_markers(16_000)
    large = _seconds_for_markers(128_000)

    assert large < small * 20 + 0.01


# --- Codex 複查（PR #68）R1、R2 ------------------------------------------------------------------
# R1：研究問題後面寫出結果的那段，在結果寫法之後的下一個句點／分號／冒號（不含小數點）結束；後面的
# 無關敘述不能替另一條關係背書。


@pytest.mark.parametrize(
    "result_quote",
    [
        # Codex 的例子：結果只有 satisfaction → loyalty，問卷敘述提到 PU、trust。
        "We examine whether perceived usefulness affects trust, and we find that satisfaction"
        " affects loyalty; the questionnaire measures perceived usefulness and trust",
        "We examine whether perceived usefulness affects trust, and we find that satisfaction"
        " affects loyalty. The questionnaire measures perceived usefulness and trust",
        "We examine whether perceived usefulness affects trust, and we find that satisfaction"
        " affects loyalty: the questionnaire measures perceived usefulness and trust",
        # 統計量的子句也一樣（小數點不是句點）。
        "We examine whether perceived usefulness affects trust, and satisfaction affects loyalty"
        " (β = 0.30); the questionnaire measures perceived usefulness and trust",
        # 結果寫法後面直接接冒號：冒號後面才是找到的內容，不能只看冒號前面就當成指回研究問題。
        "We examine whether perceived usefulness affects trust, and we find that: satisfaction"
        " affects loyalty",
    ],
)
def test_result_clause_after_a_research_question_ends_at_the_next_sentence_break(
    result_quote: str,
) -> None:
    data = _data()
    data["hypotheses"][0]["result_quote"] = result_quote

    content = _validate(data, _input(abstract=f"{ABSTRACT} {result_quote}."))

    assert content.hypotheses[0].support is SupportLevel.NOT_STATED
    assert content.hypotheses[0].result_quote is None


@pytest.mark.parametrize(
    "result_quote",
    [
        # 小數點與 p 值的點不是句點：結果子句仍包含後面的構面名稱。
        "We examine whether perceived usefulness affects trust, and we find that the effect"
        " (β = 0.42, p < .01) is positive for trust",
        # 結果子句只用代名詞指回研究問題，後面的敘述不影響。
        "We examine whether perceived usefulness affects trust, and we find that it does;"
        " the questionnaire measures satisfaction and loyalty",
    ],
)
def test_result_clause_after_a_research_question_keeps_its_own_relation(
    result_quote: str,
) -> None:
    data = _data()
    data["hypotheses"][0]["result_quote"] = result_quote

    content = _validate(data, _input(abstract=f"{ABSTRACT} {result_quote}."))

    assert content.hypotheses[0].support is SupportLevel.SUPPORTED
    assert content.hypotheses[0].result_quote == result_quote


# R2：代號與全部假設的每一種直接陳述寫法（含被動句與 `Support was found for`）都要在子句開頭：前面
# 只能是段落開頭、標點、（標點後的）and／but／thus，或「本研究主詞＋find／show…＋that」。否認、
# 轉述別人的研究、其他外層主詞或動詞都不算。

OUTER_CLAIMS = (
    "We dispute that H1 was supported",
    "We dispute that all hypotheses were supported",
    "Other researchers report that H1 was supported",
    "Other researchers report that all hypotheses were supported",
    "Other researchers report that the results support H1",
    "Other researchers report that support was found for H1",
    "Others argue that H1 received support",
    "It is not the case that H1 was supported",
    "Other researchers report that loyalty and H1 were supported",
)
OWN_CLAIMS = (
    "The results show that H1 was supported",
    "We find that all hypotheses were supported",
    "Thus, H1 was supported",
    "As expected, H1 was supported",
    "Overall, the results support H1",
    "Strong support was found for H1",
)
OUTER_ABSTRACT = (
    LABELLED_ABSTRACT + " " + " ".join(f"{quote}." for quote in OUTER_CLAIMS + OWN_CLAIMS)
)


@pytest.mark.parametrize("result_quote", OUTER_CLAIMS)
def test_label_or_all_hypotheses_inside_an_outer_claim_becomes_not_stated(
    result_quote: str,
) -> None:
    hypothesis = _labelled(H1_SENTENCE, result_quote, abstract=OUTER_ABSTRACT)

    assert hypothesis.support is SupportLevel.NOT_STATED
    assert hypothesis.result_quote is None


@pytest.mark.parametrize("result_quote", OWN_CLAIMS)
def test_label_or_all_hypotheses_reported_by_the_study_itself_is_kept(result_quote: str) -> None:
    hypothesis = _labelled(H1_SENTENCE, result_quote, abstract=OUTER_ABSTRACT)

    assert hypothesis.support is SupportLevel.SUPPORTED
    assert hypothesis.result_quote == result_quote


def test_label_after_but_in_a_new_clause_is_kept() -> None:
    quote = "H1 was not tested, but H2 was supported"
    hypothesis = _labelled(
        H2_SENTENCE, quote, cause="Trust", effect="Loyalty", abstract=f"{OUTER_ABSTRACT} {quote}."
    )

    assert hypothesis.support is SupportLevel.SUPPORTED


# --- Opus 確認審查（PR #68）M2、L1、L2：輸入定義的縮寫要分得出名稱 ------------------------------


def _support_with_defined_abbreviation(
    definition: str, name_en: str, abbreviation: str, extra: str = ""
) -> SupportLevel:
    """輸入多一句 `We measure {definition}.`（和 `extra`），模型多列構面 `name_en`，這個構面 →
    Perceived usefulness 的結果原文寫 `{abbreviation} rose significantly (β = 0.2)`。"""
    result_quote = f"{abbreviation} rose significantly (β = 0.2)"
    abstract = f"{ABSTRACT} We measure {definition}.{extra} {result_quote}."
    data = _data()
    data["constructs"].append(
        {"name_en": name_en, "label_zh": "測試構面", "definition": None, "measurement": None}
    )
    data["hypotheses"] = [
        _hypothesis(
            sentence=f"We measure {definition}.",
            cause=name_en,
            effect="Perceived usefulness",
            result_quote=result_quote,
            statistics=None,
        )
    ]
    return _validate(data, _input(abstract=abstract)).hypotheses[0].support


# M2：縮寫的字母全部落在最後一個詞裡時，名稱不能只剩那個詞（嚴格首字母的較長名稱優先；寬鬆的寫法
# 只在唯一時用；分不出就不給）。


@pytest.mark.parametrize(
    ("definition", "name_en", "abbreviation", "kept"),
    [
        ("system satisfaction (SS)", "Satisfaction", "SS", False),
        ("system satisfaction (SS)", "System satisfaction", "SS", True),
        ("trust in transactions (TT)", "Transactions", "TT", False),
        ("trust in transactions (TT)", "Trust in transactions", "TT", True),
        ("effort expectancy (EE)", "Expectancy", "EE", False),
        ("effort expectancy (EE)", "Effort expectancy", "EE", True),
        # satisfaction 和 sales satisfaction 都只有寬鬆的寫法對得上：分不出名稱，不給。
        ("sales satisfaction (SAS)", "Satisfaction", "SAS", False),
        # 只有一段對得上時照舊用寬鬆的寫法。
        ("satisfaction (SAT)", "Satisfaction", "SAT", True),
        ("loyalty (LOY)", "Loyalty", "LOY", True),
        ("electronic word-of-mouth (eWOM)", "Electronic word-of-mouth", "eWOM", True),
        ("satisfaction with trust (ST)", "Satisfaction with trust", "ST", True),
        ("perceived ease of use (PEOU)", "Perceived ease of use", "PEOU", True),
    ],
)
def test_defined_abbreviation_name_prefers_strict_initials(
    definition: str, name_en: str, abbreviation: str, kept: bool
) -> None:
    support = _support_with_defined_abbreviation(definition, name_en, abbreviation)

    assert support is (SupportLevel.SUPPORTED if kept else SupportLevel.NOT_STATED)


def test_defined_system_satisfaction_is_not_given_to_satisfaction() -> None:
    # 確認審查的完整例子：模型列了 Satisfaction、漏列 System satisfaction。
    abstract = (
        f"{ABSTRACT} H1: Satisfaction affects trust. We measure system satisfaction (SS)."
        " SS significantly affects loyalty."
    )
    data = _with_loyalty(
        _hypothesis(
            sentence="H1: Satisfaction affects trust.",
            cause="Satisfaction",
            effect="Trust",
            result_quote="SS significantly affects loyalty",
            statistics=None,
        )
    )
    data["constructs"].append(
        {"name_en": "Satisfaction", "label_zh": "滿意度", "definition": None, "measurement": None}
    )

    content = _validate(data, _input(abstract=abstract))

    assert content.hypotheses[0].support is SupportLevel.NOT_STATED


# L1（鎖住現在的行為，ADR-0039 取捨有寫）：縮寫跳過的詞不是虛詞、或縮寫只取名稱的一部分時認不得，
# 錯在少判。


@pytest.mark.parametrize(
    ("definition", "name_en", "abbreviation"),
    [
        ("attitude toward using (ATT)", "Attitude toward using", "ATT"),
        ("behavioral intention to use (BI)", "Behavioral intention to use", "BI"),
        ("trust in vendor (TR)", "Trust in vendor", "TR"),
        ("customer satisfaction (SAT)", "Customer satisfaction", "SAT"),
    ],
)
def test_partial_or_skipping_abbreviation_is_not_recognized(
    definition: str, name_en: str, abbreviation: str
) -> None:
    support = _support_with_defined_abbreviation(definition, name_en, abbreviation)

    assert support is SupportLevel.NOT_STATED


# L2：輸入已把縮寫定義成別的名稱時，不從首字母給這個構面。


@pytest.mark.parametrize(("defines_elsewhere", "kept"), [(True, False), (False, True)])
def test_initials_defined_as_another_name_in_the_input_do_not_count(
    defines_elsewhere: bool, kept: bool
) -> None:
    extra = " We also measure price uncertainty (PU)." if defines_elsewhere else ""
    result_quote = "PU significantly affects loyalty"
    abstract = f"{ABSTRACT}{extra} {result_quote}."
    data = _with_loyalty(
        _hypothesis(
            sentence=SENTENCE,
            cause="Perceived usefulness",
            effect="Trust",
            result_quote=result_quote,
            statistics=None,
        )
    )

    content = _validate(data, _input(abstract=abstract))

    expected = SupportLevel.SUPPORTED if kept else SupportLevel.NOT_STATED
    assert content.hypotheses[0].support is expected


# --- Opus 確認審查 M3、M4：量級縮寫的完整驗證器成對測試 ------------------------------------------


@pytest.mark.parametrize(
    ("written", "sample_size"),
    [("a 2M-user panel of", 2), ("a 10k-respondent panel of", 10)],
)
def test_sample_size_from_an_abbreviated_magnitude_before_a_hyphen_is_rejected(
    written: str, sample_size: int
) -> None:
    abstract = ABSTRACT.replace("200 bank customers", f"{written} bank customers")
    data = _data()
    data["context"]["sample_size"] = sample_size
    data["hypotheses"][0]["statistics"]["sample_size"] = None

    assert (
        _rejected(data, _input(abstract=abstract)) is ErrorCode.RESEARCH_MODEL_UNSUPPORTED_CONTENT
    )


@pytest.mark.parametrize(
    ("written", "sample_size"),
    [("412 M&A deals of", 412), ("120 M.B.A. students and", 120), ("200 M-Turk workers and", 200)],
)
def test_sample_size_before_a_magnitude_letter_in_another_word_is_kept(
    written: str, sample_size: int
) -> None:
    abstract = ABSTRACT.replace("200 bank customers", f"{written} bank customers")
    data = _data()
    data["context"]["sample_size"] = sample_size
    data["hypotheses"][0]["statistics"]["sample_size"] = None

    assert _validate(data, _input(abstract=abstract)).sample_size == sample_size
