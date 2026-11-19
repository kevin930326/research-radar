"""中文重點：通用縮寫的允許清單（`config/zh_summary_acronyms.yaml`，ADR-0032 的「通用縮寫的允許清單
與整欄說明存成空值」更新）。

放行要兩個條件都成立：輸出寫的縮寫和登記的一模一樣，而且輸入有它登記的全名。每一項放行都有「仍然
擋得住編造」的反例；設定檔的每種錯誤都在讀取時擋下。標題、摘要、輸出全是測試自己編的。
"""

import time
from pathlib import Path

import pytest

from radar.config import (
    ZH_SUMMARY_ACRONYMS_FILE,
    ZH_SUMMARY_FILE,
    ConfigError,
    load_zh_summary_rules,
)
from radar.domain.ai_outputs import OutputRejectedError
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import input_sha256
from radar.domain.validators.text_checks import ExtendedVocabulary, check_copy
from radar.domain.validators.zh_summary import (
    MAX_ACRONYMS,
    MAX_FULL_NAMES_PER_ACRONYM,
    AcronymEntry,
    ZhSummaryAcronyms,
    ZhSummaryRules,
    validate_zh_summary,
)
from tests.llm_support import ROOT
from tests.unit.test_zh_summary_input_reading import LONG_INPUT_SECONDS
from tests.unit.test_zh_summary_validator import _input, _output

UNSUPPORTED = ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT
CONFIG = ROOT / "config"
REAL_RULES = load_zh_summary_rules({"RADAR_CONFIG_DIR": str(CONFIG)})
REAL_ACRONYMS = (CONFIG / ZH_SUMMARY_ACRONYMS_FILE).read_text(encoding="utf-8")
REAL_THRESHOLDS = (CONFIG / ZH_SUMMARY_FILE).read_text(encoding="utf-8")


def _method(method: str, abstract: str, rules: ZhSummaryRules = REAL_RULES) -> str | None:
    output = _output(research_question=None, theory=None, findings=None, method=method)
    return validate_zh_summary(output, _input(title="Field notes", abstract=abstract), rules).method


def _rejected(method: str, abstract: str, rules: ZhSummaryRules = REAL_RULES) -> ErrorCode:
    with pytest.raises(OutputRejectedError) as caught:
        _method(method, abstract, rules)
    return caught.value.code


def _uses(acronym: str) -> str:
    return f"以某種分析方法研究企業資料（{acronym}）"


# --- 真的設定檔：每一筆、每一個全名寫法都真的放行 ---------------------------------------------

ENTRIES = [(entry.acronym, name) for entry in REAL_RULES.acronyms for name in entry.full_names]


def test_real_list_has_the_initial_acronyms() -> None:
    acronyms = {entry.acronym for entry in REAL_RULES.acronyms}

    # 初版清單（brief 列的 29 個）扣掉全名拿不準的 MICMAC、VIKOR（決定紀錄）。
    assert acronyms == {
        "DEMATEL",
        "VAR",
        "SEM",
        "PLS",
        "DID",
        "EEG",
        "fNIRS",
        "MLP",
        "BiLSTM",
        "MDS",
        "MCDM",
        "AUC",
        "SHAP",
        "GloVe",
        "IPFS",
        "TOPSIS",
        "COPRAS",
        "WASPAS",
        "MULTIMOORA",
        "LMX",
        "TMX",
        "RBV",
        "SDT",
        "XAI",
        "QCA",
        "P2P",
        "B2B",
        # 2026-11-19 第二批（ADR-0032 更新）。
        "ARMA",
        "GARCH",
        "PVAR",
        "ISM",
        "FUCOM",
        "MACONT",
        "ARAS",
        "MARCOS",
        "FDEMATEL",
        "EC2",
        "O2O",
    }


def test_real_list_documents_how_to_add_an_entry() -> None:
    assert "ADR-0032" in REAL_ACRONYMS
    assert "version" in REAL_ACRONYMS
    assert "輸入指紋" in REAL_ACRONYMS


@pytest.mark.parametrize(("acronym", "full_name"), ENTRIES)
def test_every_registered_full_name_allows_its_acronym(acronym: str, full_name: str) -> None:
    abstract = f"This study applies {full_name} to firm data."

    # 直接斷言清單（第一輪修正，審查 B3：有些組不帶清單也會靠首字母規則過）。
    assert ExtendedVocabulary.build(check_copy(abstract), {acronym: (full_name,)}).acronyms == {
        acronym
    }
    assert _method(_uses(acronym), abstract) == _uses(acronym)


@pytest.mark.parametrize("acronym", sorted({acronym for acronym, _ in ENTRIES}))
def test_every_acronym_is_rejected_without_its_full_name(acronym: str) -> None:
    assert _rejected(_uses(acronym), "This study applies a new approach to firm data.") is (
        UNSUPPORTED
    )


# --- 全名的比對：會過的寫法 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("abstract", "acronym"),
    [
        # 連字號、空白、連在一起都一樣。
        ("We apply the decision-making trial and evaluation laboratory.", "DEMATEL"),
        ("We apply the Decision Making Trial and Evaluation Laboratory.", "DEMATEL"),
        ("We use multicriteria decision making.", "MCDM"),
        ("We use multi criteria decision-making.", "MCDM"),
        ("We use multi-criteria decision making.", "MCDM"),
        ("We estimate a vector auto-regression.", "VAR"),
        ("We study peer-to-peer lending.", "P2P"),
        ("We study business-to-business sales.", "B2B"),
        ("We study leader–member exchange.", "LMX"),  # 破折號在檢查用副本裡是連字號
        # 摘要少了空白黏成一串（OpenAlex 常見）：全名要從輸入的詞頭開始、在詞尾結束。
        ("We apply decisionmakingtrialandevaluationlaboratory here.", "DEMATEL"),
        ("We apply decisionmaking-trialandevaluation laboratory here.", "DEMATEL"),
        # 摘要裡的換行在檢查用副本裡是一個空白，和空白一樣算連續（第一輪修正，審查 A5／B2）。
        ("We apply the decision making\ntrial and evaluation laboratory.", "DEMATEL"),
        ("We study peer to\r\npeer lending.", "P2P"),
        ("We study peer to\tpeer lending.", "P2P"),
        # 只有最後一個詞容許複數與詞形變化（第一輪修正，審查 A3）。
        ("We estimate vector autoregressive models.", "VAR"),
        ("We estimate a vector autoregressive model.", "VAR"),
        ("We report areas under the curves.", "AUC"),  # 登記了 areas under the curve
        ("We use structural equation models.", "SEM"),
        ("We use structural equation modelling.", "SEM"),
        ("We use partial least square regression.", "PLS"),
        ("We use differences-in-differences.", "DID"),  # 登記了 differences in differences
        ("We use difference-in-difference.", "DID"),
        ("Leader–member exchange shapes trust.", "LMX"),  # en dash 在檢查用副本裡是連字號
        # 兩種意思都登記：輸入有其中一個就放行。
        ("We draw on signal detection theory.", "SDT"),
        ("We draw on self-determination theory.", "SDT"),
        # 在標題裡也算。
        ("Explainable artificial intelligence in banking.", "XAI"),
    ],
)
def test_full_name_variants_allow_the_acronym(abstract: str, acronym: str) -> None:
    # 直接斷言清單（第一輪修正，審查 B3），再斷言整筆驗證。
    allowed = ExtendedVocabulary.build(check_copy(abstract), REAL_RULES.acronym_full_names())
    assert acronym in allowed.acronyms
    assert _method(_uses(acronym), abstract) == _uses(acronym)


PASSES = "passes"


@pytest.mark.parametrize(
    ("abstract", "acronym", "expected"),
    [
        # 前面的詞不容許詞形變化（第一輪修正，審查 A3）：一般句子不再對上全名。
        ("We examine how team members exchange knowledge in firms.", "TMX", UNSUPPORTED),
        ("Team members exchanged ideas.", "TMX", UNSUPPORTED),
        ("We study leader members exchange.", "LMX", UNSUPPORTED),
        ("We use complexity proportional assessment.", "COPRAS", UNSUPPORTED),
        # GloVe 拿掉了 global vectors：只有 global vectors for word representation 才算。
        ("We build a global vector of prices for firms.", "GloVe", UNSUPPORTED),
        ("We use global vectors for prices.", "GloVe", UNSUPPORTED),
        # 這個寫法因此救不回來（ADR 已知的偏保守）。
        ("We use decision-making trials and evaluation laboratories.", "DEMATEL", UNSUPPORTED),
        # 清單對不上，但整筆照樣過：既有的首字母規則（`knows_word` 的 initials）認得 SDT、SEM，
        # 和允許清單無關（修改前也過）。
        ("We draw on signaling detection theory.", "SDT", PASSES),
        ("We use self-determined theory.", "SDT", PASSES),
        ("We use structure equation models.", "SEM", PASSES),
    ],
)
def test_only_the_last_word_of_a_full_name_may_change_form(
    abstract: str, acronym: str, expected: object
) -> None:
    allowed = ExtendedVocabulary.build(check_copy(abstract), REAL_RULES.acronym_full_names())

    assert acronym not in allowed.acronyms
    if expected == PASSES:
        assert _method(_uses(acronym), abstract) == _uses(acronym)
    else:
        assert _rejected(_uses(acronym), abstract) is expected


@pytest.mark.parametrize(
    ("abstract", "acronym"),
    [
        # 第二輪修正（審查 L1）：全名第一個詞被切開、第一段只有 1～2 個字母時也對得上（128ed23 擋；
        # 字母樹不要求開頭的詞前 3 個字母一樣）。輸入仍然要有全名的每一個字母。
        ("We use bi-directional long short-term memory.", "BiLSTM"),
        ("We use bi directional long short term memory.", "BiLSTM"),
        ("We adopt the re-source based view.", "RBV"),
    ],
)
def test_a_split_first_word_with_a_short_first_piece_allows_the_acronym(
    abstract: str, acronym: str
) -> None:
    allowed = ExtendedVocabulary.build(check_copy(abstract), REAL_RULES.acronym_full_names())

    assert allowed.acronyms == {acronym}
    assert _method(_uses(acronym), abstract) == _uses(acronym)


def test_the_seam_between_title_and_abstract_breaks_a_full_name() -> None:
    # 標題與摘要是兩份檢查用副本、用換行接起來，接縫不算連續（摘要裡的換行算）。
    output = _output(research_question=None, theory=None, findings=None, method=_uses("DEMATEL"))
    llm_input = _input(title="Decision making", abstract="trial and evaluation laboratory is used.")
    joined = (
        check_copy("Decision making")
        + "\n"
        + check_copy("trial and evaluation laboratory is used.")
    )

    assert (
        "DEMATEL" not in ExtendedVocabulary.build(joined, REAL_RULES.acronym_full_names()).acronyms
    )
    with pytest.raises(OutputRejectedError) as caught:
        validate_zh_summary(output, llm_input, REAL_RULES)
    assert caught.value.code is UNSUPPORTED


def test_hyphenated_acronym_strings_check_each_part() -> None:
    abstract = "We combine bidirectional long short-term memory with Shapley additive explanations."

    assert _method("模型為（BiLSTM-SHAP）", abstract) == "模型為（BiLSTM-SHAP）"
    assert _rejected(
        "模型為（BiLSTM-SHAP）", "We use bidirectional long short-term memory networks."
    ) is (UNSUPPORTED)


def test_title_counts_as_input() -> None:
    output = _output(research_question=None, theory=None, findings=None, method=_uses("DEMATEL"))
    title = "A decision making trial and evaluation laboratory study"

    content = validate_zh_summary(
        output, _input(title=title, abstract="We study firms."), REAL_RULES
    )

    assert content.method == _uses("DEMATEL")


# --- 2026-11-19 第二批（ADR-0032 更新）：新登記的縮寫與全名 -----------------------------------


@pytest.mark.parametrize(
    ("abstract", "acronym"),
    [
        ("We estimate an Autoregressive Moving Average model.", "ARMA"),
        ("We estimate an autoregressive-moving-average process.", "ARMA"),
        ("We fit a Generalized Autoregressive Conditional Heteroskedasticity model.", "GARCH"),
        ("We fit a generalized autoregressive conditional heteroscedasticity model.", "GARCH"),
        ("We fit a Generalised Autoregressive Conditional Heteroscedasticity model.", "GARCH"),
        ("We fit a generalised autoregressive conditional heteroskedasticity model.", "GARCH"),
        ("We estimate a Panel Vector Autoregression.", "PVAR"),
        ("We estimate a panel vector autoregressive model.", "PVAR"),
        ("We apply Interpretive Structural Modeling.", "ISM"),
        # 英式拼法靠最後一個詞的詞形變化涵蓋，沒有另外登記。
        ("We apply Interpretive Structural Modelling.", "ISM"),
        ("We apply interpretive structural models.", "ISM"),
        ("We weight criteria with the Full Consistency Method.", "FUCOM"),
        (
            "We rank firms with Mixed Aggregation by Comprehensive Normalization Technique.",
            "MACONT",
        ),
        (
            "We rank firms with mixed aggregation by comprehensive normalisation technique.",
            "MACONT",
        ),
        ("We rank firms with the Additive Ratio Assessment method.", "ARAS"),
        (
            "We rank firms with Measurement of Alternatives and Ranking according to "
            "COmpromise Solution.",
            "MARCOS",
        ),
        (
            "We rank firms with measurement of alternatives and ranking according to the "
            "compromise solution.",
            "MARCOS",
        ),
        (
            "We rank firms with measurement alternatives and ranking according to compromise "
            "solution.",
            "MARCOS",
        ),
        (
            "We rank firms with measurement alternatives and ranking according to the "
            "compromise solution.",
            "MARCOS",
        ),
        (
            "We rank firms with measurement alternatives and ranking based on the compromise "
            "solution.",
            "MARCOS",
        ),
        (
            "We rank firms with measurement of alternatives and ranking based on the compromise "
            "solution.",
            "MARCOS",
        ),
        ("We apply the fuzzy decision-making trial and evaluation laboratory.", "FDEMATEL"),
        ("We deploy the system on Amazon Elastic Compute Cloud.", "EC2"),
        ("We study online-to-offline commerce.", "O2O"),
        ("We study Online to Offline commerce.", "O2O"),
        # 既有縮寫的新寫法。
        ("We use structural equation analysis.", "SEM"),
        ("We use Structural Equation Analysis.", "SEM"),
        ("We apply the decision-making trial evaluation laboratory.", "DEMATEL"),
        ("We apply the Decision Making Trial Evaluation Laboratory.", "DEMATEL"),
    ],
)
def test_batch_two_full_names_allow_the_acronym(abstract: str, acronym: str) -> None:
    allowed = ExtendedVocabulary.build(check_copy(abstract), REAL_RULES.acronym_full_names())

    assert acronym in allowed.acronyms
    assert _method(_uses(acronym), abstract) == _uses(acronym)


@pytest.mark.parametrize(
    ("abstract", "acronym"),
    [
        # 只有全名的一部分。
        ("We deploy the system on elastic compute resources.", "EC2"),
        ("We use structural equation approaches.", "SEM"),
        ("We use structural equations.", "SEM"),
        ("We study online offline commerce.", "O2O"),
        ("We study offline to online commerce.", "O2O"),
        ("We estimate a moving average model.", "ARMA"),
        ("We estimate an autoregressive model.", "ARMA"),
        ("We fit a conditional heteroskedasticity model.", "GARCH"),
        ("We fit an autoregressive conditional heteroskedasticity model.", "GARCH"),
        ("We estimate a panel regression.", "PVAR"),
        ("We use structural modeling.", "ISM"),
        ("We apply a consistency method.", "FUCOM"),
        ("We apply comprehensive normalization.", "MACONT"),
        ("We apply ratio assessment.", "ARAS"),
        ("We rank alternatives by compromise solution.", "MARCOS"),
        ("We use fuzzy logic for decisions.", "FDEMATEL"),
        ("We use decision making trial evaluation.", "DEMATEL"),
        # 只有別的縮寫的全名：VAR 的全名不讓 PVAR 過；DEMATEL 的全名不讓 FDEMATEL 過。
        ("We estimate a vector autoregression.", "PVAR"),
        ("We apply decision making trial and evaluation laboratory.", "FDEMATEL"),
        ("We study peer-to-peer lending.", "O2O"),
        ("We study business-to-business sales.", "EC2"),
    ],
)
def test_batch_two_acronyms_without_a_full_name_are_rejected(abstract: str, acronym: str) -> None:
    allowed = ExtendedVocabulary.build(check_copy(abstract), REAL_RULES.acronym_full_names())

    assert acronym not in allowed.acronyms
    assert _rejected(_uses(acronym), abstract) is UNSUPPORTED


def test_a_longer_full_name_also_allows_the_shorter_acronym_inside_it() -> None:
    # 全名包著另一筆的全名時，兩個縮寫都由輸入撐著（輸入真的有兩個全名）。
    vocabulary = ExtendedVocabulary.build(
        check_copy(
            "We estimate a panel vector autoregression with fuzzy decision making trial "
            "and evaluation laboratory weights."
        ),
        REAL_RULES.acronym_full_names(),
    )

    assert {"PVAR", "VAR", "FDEMATEL", "DEMATEL"} <= vocabulary.acronyms


def test_u2010_hyphen_in_a_full_name_allows_the_acronym() -> None:
    # 現在的行為，照 TASK 不改程式：U+2010 在檢查用副本裡是一般連字號，所以用 U+2010 連起來的
    # online to offline 也對得上。
    abstract = "We study online\u2010to\u2010offline commerce."

    assert check_copy(abstract) == "We study online-to-offline commerce."
    assert "O2O" in (
        ExtendedVocabulary.build(check_copy(abstract), REAL_RULES.acronym_full_names()).acronyms
    )
    assert _method(_uses("O2O"), abstract) == _uses("O2O")


EM_DASH_ABSTRACT = (
    "We combine Interpretive Structural Modeling\u2014Decision Making Trial and Evaluation "
    "Laboratory in one framework."
)


def test_em_dash_after_a_full_name_still_allows_its_acronym() -> None:
    # 現在的行為，照 TASK 不改程式：破折號（U+2014）在檢查用副本裡是連字號；破折號前面那個全名
    # （ISM）的結尾後面接連字號，照樣對得上。
    allowed = ExtendedVocabulary.build(
        check_copy(EM_DASH_ABSTRACT), REAL_RULES.acronym_full_names()
    )

    assert "ISM" in allowed.acronyms
    assert _method(_uses("ISM"), EM_DASH_ABSTRACT) == _uses("ISM")


def test_em_dash_right_before_a_full_name_blocks_its_acronym() -> None:
    # 現在的行為，照 TASK 不改程式：全名前面緊接破折號（檢查用副本裡是連字號、沒有空白）時，和
    # 「全名前面用連字號接著別的詞」一樣擋（偏保守，見 ADR-0032 的取捨）。破折號兩邊有空白就過。
    allowed = ExtendedVocabulary.build(
        check_copy(EM_DASH_ABSTRACT), REAL_RULES.acronym_full_names()
    )

    assert "DEMATEL" not in allowed.acronyms
    assert _rejected(_uses("DEMATEL"), EM_DASH_ABSTRACT) is UNSUPPORTED
    spaced = EM_DASH_ABSTRACT.replace("\u2014", " \u2014 ")
    assert _method(_uses("DEMATEL"), spaced) == _uses("DEMATEL")


@pytest.mark.parametrize(
    ("abstract", "acronym"),
    [
        # 短全名出現在摘要裡時，意思就是那個縮寫：放行是對的（決定紀錄的評估）。
        ("We host the experiment on Amazon Elastic Compute Cloud instances.", "EC2"),
        ("Structural equation analysis supports the hypotheses.", "SEM"),
        ("Online-to-offline platforms link web orders with stores.", "O2O"),
        ("Criteria weights come from the full consistency method.", "FUCOM"),
        ("Suppliers are ranked by additive ratio assessment.", "ARAS"),
    ],
)
def test_short_full_names_in_an_abstract_mean_the_acronym(abstract: str, acronym: str) -> None:
    assert _method(_uses(acronym), abstract) == _uses(acronym)


@pytest.mark.parametrize(
    ("abstract", "acronyms"),
    [
        # 已知漏網，ADR-0032 2026-11-19 更新：方向的一般說法也對得上 online to offline，和
        # peer to peer comparison 同類；要不要拿掉 O2O 由 Kevin 決定。
        ("Consumers migrate from online to offline channels.", ("O2O",)),
        # 已知漏網，ADR-0032 2026-11-19 更新：一般說法裡的 elastic compute cloud 也讓 EC2 過。
        ("We rent an elastic compute cloud for the experiments.", ("EC2",)),
        # 已知漏網，ADR-0032 2026-11-19 更新：VARMA 的全名讓 ARMA 與 VAR 都過。
        ("We estimate a vector autoregressive moving average model.", ("ARMA", "VAR")),
    ],
)
def test_known_gaps_short_full_names_in_general_wording(
    abstract: str, acronyms: tuple[str, ...]
) -> None:
    # 固定現在的行為：這些不是想要的放行，但照 TASK 不改程式，先寫成已知漏網。
    allowed = ExtendedVocabulary.build(check_copy(abstract), REAL_RULES.acronym_full_names())

    for acronym in acronyms:
        assert acronym in allowed.acronyms
        assert _method(_uses(acronym), abstract) == _uses(acronym)


# --- 必備反例：仍然擋得住編造 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # 清單裡的縮寫、輸入沒有全名（模型憑背景知識多寫）。
        ("We surveyed users about adoption.", "以問卷分析（SEM）"),
        ("We surveyed users about adoption.", _uses("DEMATEL")),
        # 輸入只有全名的一部分。
        ("We study decision making in firms.", _uses("DEMATEL")),
        ("We use a trial and evaluation laboratory.", _uses("DEMATEL")),
        ("We use multi-criteria methods for decisions.", _uses("MCDM")),
        ("We study peer review and peer effects.", "研究借貸平台（P2P）"),
        # 全名的詞沒有連續出現（中間隔了逗號、括號、別的詞、兩個分隔；換行見上面會過的寫法）。
        ("We study decision making, trial and evaluation laboratory.", _uses("DEMATEL")),
        ("We study decision making (trial and evaluation laboratory).", _uses("DEMATEL")),
        ("We study decision making in a trial and evaluation laboratory.", _uses("DEMATEL")),
        ("We study decision - making trial and evaluation laboratory.", _uses("DEMATEL")),
        # 全名前面用連字號接著別的詞、或在別的詞的中間。
        ("We study non-leader-member exchange ties.", _uses("LMX")),
        ("We study xdecision making trial and evaluation laboratory.", _uses("DEMATEL")),
        ("We study decision making trial and evaluation laboratoryx.", _uses("DEMATEL")),
        # 全名黏在前一個詞後面也不算（偏保守：和上一個例子分不出來）。
        ("Weapplythedecisionmakingtrialandevaluationlaboratory here.", _uses("DEMATEL")),
        # 輸入有全名，但輸出寫的是清單外的縮寫。
        ("We use multicriteria optimization and compromise solution.", _uses("VIKOR")),
        ("We use decision making trial and evaluation laboratory.", _uses("DEMATELX")),
        # 輸入有 A 的全名，輸出寫 B 的縮寫。
        ("We use decision making trial and evaluation laboratory.", _uses("TOPSIS")),
        ("We draw on leader-member exchange.", _uses("TMX")),
        ("We study peer-to-peer lending.", "研究借貸平台（B2B）"),
        # 輸入寫 business-to-business，輸出 B2C（不在清單）。
        ("We study business-to-business sales.", "研究銷售（B2C）"),
        # 大小寫要和登記的一樣。
        ("We use global vectors for word representation.", _uses("Glove")),
        ("We use global vectors for word representation.", _uses("GLOVE")),
        ("We use functional near-infrared spectroscopy.", _uses("FNIRS")),
        ("We use bidirectional long short-term memory.", _uses("BILSTM")),
        ("We study peer-to-peer lending.", "研究借貸平台（p2p）"),
        # 縮寫後面加了字（複數、別的字母）。
        ("We use decision making trial and evaluation laboratory.", _uses("DEMATELs")),
    ],
)
def test_acronyms_without_a_full_name_are_still_rejected(abstract: str, method: str) -> None:
    assert _rejected(method, abstract) is UNSUPPORTED


def test_rules_without_a_list_allow_nothing() -> None:
    rules = ZhSummaryRules(
        max_chars_per_field=150, translation_min_abstract_sentences=7, min_cjk_percent=50
    )

    assert rules.acronyms == ()
    assert _rejected("研究借貸平台（P2P）", "We study peer-to-peer lending.", rules) is UNSUPPORTED


def test_allowlist_does_not_change_number_rules() -> None:
    # P2P 換成 Q 之後，別的數字照樣要在輸入裡。
    abstract = "We study peer-to-peer lending in 12 cities."

    assert _method("研究 12 個城市的借貸平台（P2P）", abstract) is not None
    assert _rejected("研究 13 個城市的借貸平台（P2P）", abstract) is UNSUPPORTED
    assert _rejected("研究借貸平台（P2P3）", abstract) is UNSUPPORTED


def test_vocabulary_without_a_list_has_no_acronyms() -> None:
    text = check_copy("We study peer-to-peer lending.")

    assert ExtendedVocabulary.build(text).acronyms == frozenset()
    assert ExtendedVocabulary.build(text, {"P2P": ("peer to peer",)}).acronyms == {"P2P"}
    assert not ExtendedVocabulary.build(text).knows_alnum("P2P")


# --- 輸入指紋：允許清單不算進去 -----------------------------------------------------------------


def test_input_fingerprint_has_no_place_for_the_list() -> None:
    # 中文重點的指紋只算區塊與工作（`domain/fingerprints.input_sha256`）；設定檔版本只有分類表與
    # 情境值清單兩個參數，中文重點帶任何一個都會丟 ValueError（`test_input_fingerprint.py`）。
    code = input_sha256.__code__
    names = code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]

    assert names == ("blocks", "task", "taxonomy_version", "context_values_version")


# --- 設定檔的驗證 -------------------------------------------------------------------------------


def _load(tmp_path: Path, acronyms: str, thresholds: str = REAL_THRESHOLDS) -> ZhSummaryRules:
    (tmp_path / ZH_SUMMARY_FILE).write_text(thresholds, encoding="utf-8")
    (tmp_path / ZH_SUMMARY_ACRONYMS_FILE).write_text(acronyms, encoding="utf-8")
    return load_zh_summary_rules({"RADAR_CONFIG_DIR": str(tmp_path)})


def _problems(tmp_path: Path, acronyms: str) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, acronyms)
    problems = caught.value.problems
    assert problems
    assert all("\n" not in problem for problem in problems)
    assert all(problem.startswith(f"{ZH_SUMMARY_ACRONYMS_FILE}：") for problem in problems)
    return problems


ONE = "version: 1\nacronyms:\n  - acronym: SEM\n    full_names:\n      - {name}\n"


def test_a_minimal_list_loads(tmp_path: Path) -> None:
    rules = _load(tmp_path, ONE.format(name="structural equation modeling"))

    assert rules.acronyms == (
        AcronymEntry(acronym="SEM", full_names=("structural equation modeling",)),
    )
    assert rules.max_chars_per_field == REAL_RULES.max_chars_per_field


def test_an_empty_list_loads(tmp_path: Path) -> None:
    assert _load(tmp_path, "version: 1\nacronyms: []\n").acronyms == ()


def test_missing_list_file_is_reported(tmp_path: Path) -> None:
    (tmp_path / ZH_SUMMARY_FILE).write_text(REAL_THRESHOLDS, encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load_zh_summary_rules({"RADAR_CONFIG_DIR": str(tmp_path)})

    assert caught.value.problems == [
        f"{ZH_SUMMARY_ACRONYMS_FILE}：找不到檔案（{tmp_path / ZH_SUMMARY_ACRONYMS_FILE}）"
    ]


def test_both_files_are_reported_together(tmp_path: Path) -> None:
    (tmp_path / ZH_SUMMARY_FILE).write_text(REAL_THRESHOLDS + "extra: 1\n", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load_zh_summary_rules({"RADAR_CONFIG_DIR": str(tmp_path)})

    problems = caught.value.problems
    assert problems[0].startswith(f"{ZH_SUMMARY_FILE}：extra：")
    assert problems[1].startswith(f"{ZH_SUMMARY_ACRONYMS_FILE}：找不到檔案")


def test_the_threshold_file_cannot_carry_the_list(tmp_path: Path) -> None:
    thresholds = REAL_THRESHOLDS + "acronyms: []\n"

    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, REAL_ACRONYMS, thresholds)

    assert len(caught.value.problems) == 1
    assert caught.value.problems[0].startswith(f"{ZH_SUMMARY_FILE}：acronyms：")


def test_not_yaml_is_rejected(tmp_path: Path) -> None:
    problems = _problems(tmp_path, "version: 1\nacronyms: [\n")

    assert "不是合法的 YAML" in problems[0]


def test_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    assert "mapping" in _problems(tmp_path, "- SEM\n")[0]


def test_duplicate_key_is_rejected(tmp_path: Path) -> None:
    text = ONE.format(name="structural equation modeling") + "version: 2\n"

    assert "寫了兩次" in _problems(tmp_path, text)[0]


@pytest.mark.parametrize(
    ("text", "location"),
    [
        ("acronyms: []\n", "version"),
        ("version: 1\n", "acronyms"),
        ("version: 1\nacronyms: []\nnotes: x\n", "notes"),
        ('version: "1"\nacronyms: []\n', "version"),
        ("version: 1.0\nacronyms: []\n", "version"),
        ("version: true\nacronyms: []\n", "version"),
        ("version: 0\nacronyms: []\n", "version"),
        ("version: 1\nacronyms: SEM\n", "acronyms"),
        ("version: 1\nacronyms:\n  - acronym: SEM\n", "acronyms.0.full_names"),
        (
            "version: 1\nacronyms:\n  - acronym: SEM\n    full_names: []\n",
            "acronyms.0.full_names",
        ),
        (
            "version: 1\nacronyms:\n  - acronym: SEM\n    full_names: structural\n",
            "acronyms.0.full_names",
        ),
        (
            "version: 1\nacronyms:\n  - full_names: [structural equation modeling]\n",
            "acronyms.0.acronym",
        ),
        (
            "version: 1\nacronyms:\n  - acronym: SEM\n    full_names: [alpha beta]\n    note: x\n",
            "acronyms.0.note",
        ),
    ],
)
def test_missing_extra_or_mistyped_keys_are_rejected(
    tmp_path: Path, text: str, location: str
) -> None:
    problems = _problems(tmp_path, text)

    assert len(problems) == 1
    assert problems[0].startswith(f"{ZH_SUMMARY_ACRONYMS_FILE}：{location}：")


@pytest.mark.parametrize(
    "acronym", ["123", '""', "S", "P-2", "2B", "S E M", "SEM.", "true", "NO", "SÉM"]
)
def test_bad_acronyms_are_rejected(tmp_path: Path, acronym: str) -> None:
    text = f"version: 1\nacronyms:\n  - acronym: {acronym}\n    full_names: [alpha beta]\n"

    problems = _problems(tmp_path, text)

    assert len(problems) == 1
    assert problems[0].startswith(f"{ZH_SUMMARY_ACRONYMS_FILE}：acronyms.0.acronym：")


@pytest.mark.parametrize(
    "name",
    [
        '""',
        '" "',
        '" structural equation"',
        '"structural equation "',
        '"structural  equation"',
        '"structural--equation"',
        "structural-",
        "Web 2.0",
        "structural, equation",
        "structural (equation)",
        "123",
        "true",
        "équation structurelle",
    ],
)
def test_bad_full_names_are_rejected(tmp_path: Path, name: str) -> None:
    problems = _problems(tmp_path, ONE.format(name=name))

    assert all(
        problem.startswith(f"{ZH_SUMMARY_ACRONYMS_FILE}：acronyms.0.full_names")
        for problem in problems
    )


@pytest.mark.parametrize("second", ["SEM", "sem", "Sem"])
def test_duplicate_acronyms_are_rejected(tmp_path: Path, second: str) -> None:
    text = (
        "version: 1\nacronyms:\n"
        "  - acronym: SEM\n    full_names: [structural equation modeling]\n"
        f"  - acronym: {second}\n    full_names: [search engine marketing]\n"
    )

    problems = _problems(tmp_path, text)

    assert problems == [
        f"{ZH_SUMMARY_ACRONYMS_FILE}：acronyms：acronym {second} 寫了兩次（不分大小寫）"
    ]


@pytest.mark.parametrize(
    "second",
    [
        "structural equation modeling",
        "Structural Equation Modeling",
        "structural-equation modeling",
    ],
)
def test_duplicate_full_names_are_rejected(tmp_path: Path, second: str) -> None:
    text = (
        "version: 1\nacronyms:\n  - acronym: SEM\n"
        f"    full_names: [structural equation modeling, {second}]\n"
    )

    problems = _problems(tmp_path, text)

    assert problems[0].startswith(f"{ZH_SUMMARY_ACRONYMS_FILE}：acronyms.0.full_names：")
    assert "重複" in problems[0]


def test_joined_and_split_full_names_are_duplicates() -> None:
    with pytest.raises(ValueError, match="重複"):
        AcronymEntry(
            acronym="MCDM", full_names=("multicriteria decision", "multi criteria decision")
        )


def test_rules_reject_duplicate_acronyms_too() -> None:
    entry = AcronymEntry(acronym="SEM", full_names=("structural equation modeling",))
    with pytest.raises(ValueError, match="寫了兩次"):
        ZhSummaryRules(
            max_chars_per_field=150,
            translation_min_abstract_sentences=7,
            min_cjk_percent=50,
            acronyms=(entry, entry),
        )
    with pytest.raises(ValueError, match="寫了兩次"):
        ZhSummaryAcronyms(version=1, acronyms=(entry, entry))


# --- 長輸入：線性時間 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unit",
    [
        # _allowed_acronyms、_phrase_at、_letters_aligned、_same_word_form：每個位置都對得上全名的
        # 開頭幾個詞、最後一個詞對不上。
        "decision making trial and evaluation ",
        "technique for order of preference by similarity to ideal ",
        "multi-criteria decision ",
        "area under the ",
        "peer to ",
        "peer-",
        "s ",
        "a-",
        "decisionmakingtrialand ",
        "structural equation modelx ",
        "vector auto-",
    ],
)
def test_full_name_matching_runs_in_linear_time(unit: str) -> None:
    text = check_copy(unit * (200_000 // len(unit)))
    started = time.perf_counter()

    ExtendedVocabulary.build(text, REAL_RULES.acronym_full_names())

    assert time.perf_counter() - started < LONG_INPUT_SECONDS


def test_many_distinct_words_with_a_shared_prefix_are_quick() -> None:
    # 每個詞都和某個全名的第一個詞同樣開頭，又都不一樣（詞形變化的快取用不上）。
    letters = "abcdefghijklmnopqrstuvwxyz"
    words = [f"deci{a}{b}{c}" for a in letters for b in letters for c in letters]
    text = (" ".join(words) + " ")[:200_000]
    started = time.perf_counter()

    ExtendedVocabulary.build(text, REAL_RULES.acronym_full_names())

    assert time.perf_counter() - started < LONG_INPUT_SECONDS


@pytest.mark.parametrize(
    ("acronym", "full_name", "message"),
    [
        ("A1" * 100_000 + "!", "a b", "acronym 要是英文字母開頭"),
        ("AB", "ab " * 66_666 + "a!", "full_names 的每一項要是英文詞"),
        # 合法的寫法、詞太多（第一輪修正：真的跑到 `_FULL_NAME_SEPARATOR` 數詞）。
        ("AB", "ab " * 66_666 + "ab", "full_names 的每一項最多 12 個詞"),
    ],
    ids=["acronym", "full_names", "many_words"],
)
def test_config_patterns_run_in_linear_time(acronym: str, full_name: str, message: str) -> None:
    started = time.perf_counter()

    with pytest.raises(ValueError, match=message):
        AcronymEntry(acronym=acronym, full_names=(full_name,))

    assert time.perf_counter() - started < LONG_INPUT_SECONDS


# 長輸出（第一輪修正，Codex X3）：原本的測試先被漢字比例擋掉、又用 suppress 蓋住結果，沒有跑到縮寫
# 比對。現在輸出能通過前面每一條規則（長度上限用到接近上限、漢字比例照真的設定），斷言明確的結果。
LONG_ABSTRACT = (
    "We apply decision making trial and evaluation laboratory and the technique for order of "
    "preference by similarity to ideal solution to firm data."
)
LONG_UNIT = "以決策試驗與評估實驗室法（DEMATEL）與理想解類似度偏好順序評估法（TOPSIS）評估企業，"
LONG_LIMIT = 200_000


def _long_rules() -> ZhSummaryRules:
    return REAL_RULES.model_copy(update={"max_chars_per_field": LONG_LIMIT})


def test_long_output_with_acronyms_is_validated_quickly() -> None:
    text = LONG_UNIT * (LONG_LIMIT // len(LONG_UNIT))
    output = _output(research_question=text, method=None, theory=None, findings=None)
    started = time.perf_counter()

    content = validate_zh_summary(
        output, _input(title="Field notes", abstract=LONG_ABSTRACT), _long_rules()
    )

    assert content.research_question == text
    assert len(text) > LONG_LIMIT - len(LONG_UNIT)
    assert time.perf_counter() - started < LONG_INPUT_SECONDS


def test_long_output_with_an_unsupported_acronym_at_the_end_is_rejected_quickly() -> None:
    tail = "以（VIKOR）排序"
    text = LONG_UNIT * ((LONG_LIMIT - len(tail)) // len(LONG_UNIT)) + tail
    output = _output(research_question=text, method=None, theory=None, findings=None)
    started = time.perf_counter()

    with pytest.raises(OutputRejectedError) as caught:
        validate_zh_summary(
            output, _input(title="Field notes", abstract=LONG_ABSTRACT), _long_rules()
        )

    assert caught.value.code is UNSUPPORTED
    assert caught.value.detail == "research_question 有輸入沒有的英文詞"
    assert time.perf_counter() - started < LONG_INPUT_SECONDS


# 輸入與清單一起變長（第一輪修正，Codex X2）：清單用到設定檔驗證的上限（200 筆、每筆 12 個全名、每個
# 全名 12 個詞），開頭的詞都一樣，輸入重複那個開頭的詞到 20 萬字元。
def _largest_list(shared_words: int) -> dict[str, tuple[str, ...]]:
    letters = "bcdfghjklmnpqrstvwxz"
    tails = (f"q{a}{b}{c}{d}" for a in letters for b in letters for c in letters for d in letters)
    entries = []
    for number in range(MAX_ACRONYMS):
        names = tuple(
            " ".join(["alpha"] * shared_words + [next(tails) for _ in range(12 - shared_words)])
            for _ in range(MAX_FULL_NAMES_PER_ACRONYM)
        )
        entries.append(AcronymEntry(acronym=f"ZZ{number}", full_names=names))
    # 合法的設定（設定檔驗證收得下）。
    listed = ZhSummaryAcronyms(version=1, acronyms=tuple(entries))
    return {entry.acronym: entry.full_names for entry in listed.acronyms}


@pytest.mark.parametrize("shared_words", [1, 11])
def test_matching_stays_quick_when_the_list_is_at_its_limits(shared_words: int) -> None:
    acronyms = _largest_list(shared_words)
    text = check_copy(("alpha " * 40_000)[:200_000])
    started = time.perf_counter()

    vocabulary = ExtendedVocabulary.build(text, acronyms)

    assert vocabulary.acronyms == frozenset()
    assert time.perf_counter() - started < LONG_INPUT_SECONDS
