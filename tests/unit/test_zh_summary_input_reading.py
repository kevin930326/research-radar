"""中文重點：把輸入讀得更完整（ADR-0032 的 2026-11-18 更新，`ExtendedVocabulary`）。

每一項放行都有「仍然擋得住編造」的反例；另外固定共用函式（`numbers`、`structured_numbers`、
`InputVocabulary`）的預設行為不變，研究模型看到的和以前一樣。標題、摘要、輸出全是測試自己編的。
"""

import contextlib
import time
from decimal import Decimal

import pytest

from radar.config import load_zh_summary_rules
from radar.domain.ai_outputs import OutputRejectedError
from radar.domain.errors import ErrorCode
from radar.domain.validators.text_checks import (
    ExtendedVocabulary,
    InputVocabulary,
    Number,
    check_copy,
    numbers,
    structured_numbers,
)
from radar.domain.validators.zh_summary import (
    ZhSummaryRules,
    _english_word_count,
    validate_zh_summary,
)
from tests.llm_support import ROOT
from tests.unit.test_zh_summary_validator import RULES, _input, _output, _validate

UNSUPPORTED = ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT
NOT_ZH_HANT = ErrorCode.ZH_SUMMARY_NOT_ZH_HANT


def _n(value: str, *, percent: bool = False) -> Number:
    return Decimal(value).normalize(), percent


def _method_only(method: str, abstract: str) -> str | None:
    output = _output(research_question=None, theory=None, findings=None, method=method)
    return _validate(output, _input(title="Field notes", abstract=abstract)).method


def _theory_only(theory: str, abstract: str) -> str | None:
    output = _output(research_question=None, method=None, findings=None, theory=theory)
    return _validate(output, _input(title="Field notes", abstract=abstract)).theory


def _method_rejected(method: str, abstract: str) -> ErrorCode:
    with pytest.raises(OutputRejectedError) as caught:
        _method_only(method, abstract)
    return caught.value.code


def _theory_rejected(theory: str, abstract: str) -> ErrorCode:
    with pytest.raises(OutputRejectedError) as caught:
        _theory_only(theory, abstract)
    return caught.value.code


# --- A1 空白當千分位 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        ("We sampled 131 978 firms.", "抽樣 131978 家企業"),
        ("We sampled 131 978 firms.", "抽樣 131,978 家企業"),
        ("We sampled 131 978 firms.", "抽樣 131978 家企業"),  # 不換行空白
        ("We sampled 131 978 firms.", "抽樣 131978 家企業"),  # 窄空白
        ("We sampled 131 978 firms.", "抽樣 131978 家企業"),  # 窄的不換行空白
        ("We coded 54 000 posts.", "編碼 54000 則貼文"),
        ("We coded 27 397 posts.", "編碼 27397 則貼文"),
        ("We coded 1 234 567 posts.", "編碼 1234567 則貼文"),
        ("We coded 387, 000 posts.", "編碼 387000 則貼文"),
        ("We coded 150$\\,$000 posts.", "編碼 150000 則貼文"),
        ("We coded 54 000 posts.", "編碼 54 000 則貼文"),
        # 合併後，沒合併的各段也留著。
        ("We coded 54 000 posts.", "編碼 54 則"),
        ("In 2019 300 firms joined.", "2019 年有 300 家企業加入"),
    ],
)
def test_space_separated_thousands_are_merged(abstract: str, method: str) -> None:
    assert _method_only(method, abstract) == method


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # 前一段 4 位數，不合併。
        ("In 2019 300 firms joined.", "2019300 家企業加入"),
        # 合併後的值也要一模一樣。
        ("We coded 12 345 posts.", "編碼 12346 則貼文"),
        # 後面是 4 位數，不是千分位。
        ("We coded 12 3456 posts.", "編碼 123456 則貼文"),
        ("We coded 12 34 posts.", "編碼 1234 則貼文"),
    ],
)
def test_space_thousands_still_reject_invented_numbers(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


# --- A2 月份英文名 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        ("Data were collected in September.", "9 月收集資料"),
        ("Data were collected in September.", "9月收集資料"),
        ("Data were collected in Sept. and Oct.", "9、10 月收集資料"),
        ("Data were collected in July and August.", "7–8月收集資料"),
        ("Data were collected in July and August.", "7 至 8 月收集資料"),
        ("Data were collected in JULY.", "7 月收集資料"),
        (
            "Data were collected from September 2014 to May 2015.",
            "2014 年 9 月至 2015 年 5 月收集資料",
        ),
        ("Data were collected on 5 May.", "5 月收集資料"),
        ("Data were collected in Dec.", "12 月收集資料"),
    ],
)
def test_month_names_match_numbers_before_the_month_character(abstract: str, method: str) -> None:
    assert _method_only(method, abstract) == method


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # 不是接「月」的 9 不因為 September 放行。
        ("Data were collected in September.", "訪談 9 名受訪者"),
        # 輸入沒有任何月份。
        ("Data were collected last year.", "3 月收集資料"),
        # 月份不一樣。
        ("Data were collected in September.", "8 月收集資料"),
        ("Data were collected in July and August.", "7–9月收集資料"),
        # 助動詞 may、小寫的月份名稱不算。
        ("Users may share data.", "5 月收集資料"),
        ("May users share data?", "5 月收集資料"),
        ("Data were collected in september.", "9 月收集資料"),
        # 「3 個月」是期間，不是月份。
        ("Data were collected in March.", "持續 3 個月"),
    ],
)
def test_month_names_do_not_allow_other_numbers(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


# --- A3 範圍與百分點 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        ("Adoption ranged from 10–40%.", "採用率 10% 到 40%"),
        ("Adoption ranged from 10-40%.", "採用率 10%"),
        ("Adoption ranged from 2.84%–38.94%.", "採用率 2.84% 到 38.94%"),
        ("Adoption ranged from 2.84%–38.94%.", "採用率最高 38.94%"),
        ("Adoption rose by 10% points.", "採用率提高 10 個百分點"),
        ("Adoption rose by 10% point.", "採用率提高 10 百分點"),
        ("Adoption rose by 10 percent points.", "採用率提高 10 個百分點"),
        ("Adoption rose by 10 percentage points.", "採用率提高 10 個百分點"),
    ],
)
def test_percent_ranges_and_percentage_points_match(abstract: str, method: str) -> None:
    assert _method_only(method, abstract) == method


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # 沒有 points：10% 不是 10 個百分點。
        ("Adoption rose by 10%.", "採用率提高 10 個百分點"),
        # 百分點不是百分比。
        ("Adoption rose by 10 percentage points.", "採用率提高 10%"),
        # 前半要緊接 `-`；`10 to 40%` 不處理。
        ("Adoption ranged from 10 to 40%.", "採用率 10%"),
        # 沒有 % 的範圍不是百分比。
        ("Adoption ranged from 10-40 units.", "採用率 10%"),
    ],
)
def test_percent_rules_still_reject_mismatches(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


# --- A4 正負號 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        ("Prices changed by -4.40%.", "價格下跌約 4.40%"),
        ("The effect was -0.35.", "效果為 0.35"),  # 原本在 test_sign_exponent… 是拒收
        ("The effect was β = - 0.42.", "效果為 0.42"),  # 原本在 test_text_checks 是拒收
        ("The effect was -0.35.", "效果為 -0.35"),
    ],
)
def test_positive_output_matches_a_negative_input(abstract: str, method: str) -> None:
    assert _method_only(method, abstract) == method


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        ("Prices changed by 4.40%.", "價格變動 -4.40%"),
        ("The effect was 0.35.", "效果為 -0.35"),
        ("Prices changed by -4.40%.", "價格下跌約 4.40"),  # 百分比照樣要一致
        ("Prices changed by -4.40%.", "價格下跌約 4.41%"),
    ],
)
def test_negative_output_still_needs_a_negative_input(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


# --- A5 年代 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        ("Firms founded in the 1960s.", "60年代成立的企業"),
        ("Firms founded in the 1960s.", "60 年代成立的企業"),
        ("Firms founded in the 1960s.", "1960 年代成立的企業"),
        ("Firms founded in the 1960's.", "60 年代成立的企業"),
        ("Firms founded in the '90s.", "90 年代成立的企業"),
        ("Firms founded in the 1960s and 1970s.", "60 至 70 年代成立的企業"),
    ],
)
def test_decades_match_numbers_before_the_decade_word(abstract: str, method: str) -> None:
    assert _method_only(method, abstract) == method


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        ("Firms founded in the 1960s.", "訪談 60 名經理"),
        ("Firms founded in the 1960s.", "70 年代成立的企業"),
        ("Firms founded in 1960.", "60 年代成立的企業"),  # 沒有 s，不是年代
    ],
)
def test_decades_do_not_allow_other_numbers(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


# --- A6 英數混寫的詞 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        ("We study B2B platforms.", "研究 B2B 平台"),
        ("We study b2b platforms.", "研究 B2B 平台"),
        ("We study P2P lending.", "研究 P2P 借貸"),
        ("We study Amazon EC2 usage.", "研究 EC2 使用"),
        ("We study 3D printing.", "研究 3D 列印"),
        ("We study 3-D printing.", "研究 3D 列印"),  # 連字號差異算同一個詞
        ("We study 2D and 3D maps.", "研究 2D 與 3D 地圖"),
        ("H2b was supported.", "H2b 成立"),
        ("We study Web 2.0 tools.", "研究 Web2.0 工具"),
        ("We study COVID-19 apps.", "研究 COVID19 應用"),
        ("We study SARS-CoV-2 apps.", "研究 SARS-CoV2 應用"),
        ("We ran a 2 x 2 experiment.", "2x2 實驗"),
    ],
)
def test_alphanumeric_words_from_the_input_are_allowed(abstract: str, method: str) -> None:
    assert _method_only(method, abstract) == method


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # 原本的讀法不變（這些都和修改前一樣會過）。
        ("The effect was 1e-3.", "效果為 1e-3"),
        ("The effect was 2E5.", "效果為 2E5"),
        ("We study Web 2.0 tools.", "研究 Web 2.0 工具"),
        ("We reached 12M users.", "觸及 12M 使用者"),
        ("We reached 12 million users.", "觸及 12M 使用者"),
        ("We reached 561K users.", "觸及 561K 使用者"),
        ("We reached 10k users.", "觸及 10k 使用者"),
        ("Firms founded in the 1960s.", "1960s 成立"),
        ("Since the 20th century.", "20th 世紀以來"),
        ("The R² was 0.4.", "R2 為 0.4"),
        ("The R² was 0.4.", "R² 為 0.4"),
        ("We study COVID-19 apps.", "研究 COVID-19 應用"),
        ("Hypothesis 1 was supported.", "H1 成立"),
        ("We sampled n=120 users.", "抽樣 n=120"),
        ("We sampled 120 users.", "抽樣 120名使用者"),
        ("We study 5G networks.", "研究 5G 網路"),
        # 一個字母＋一位數字、數字開頭只接字母：輸入沒有同一個詞時照原本的方式讀。
        ("We study fifth-generation networks.", "研究 5G 網路"),
        ("We study three-dimensional printing.", "研究 3D 列印"),
    ],
)
def test_ordinary_number_writings_are_read_as_before(abstract: str, method: str) -> None:
    assert _method_only(method, abstract) == method


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # 編造的數字不能藏在英數詞裡。
        ("We sampled N = 120 users.", "抽樣 N120"),
        ("We sampled 120 users.", "抽樣 N120"),
        # 這個 PR 不處理展開寫法（留給 PR 2 的允許清單）。
        ("We study peer-to-peer lending in two cities.", "研究 P2P 借貸"),
        ("We study business-to-business sales in two regions.", "研究 B2B 銷售"),
        ("Hypothesis 2b was supported.", "H2b 成立"),
        ("We study Amazon EC 2.", "研究 EC3"),
        # 原本的讀法照樣擋輸入沒有的數字。
        ("We study fourth-generation networks.", "研究 5G 網路"),
        ("Hypothesis 1 was supported.", "H2 成立"),
    ],
)
def test_unknown_alphanumeric_words_are_rejected(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


# --- 不做的事：單位換算、取概數、加總（照樣擋）＋必備反例 ---------------------------------------


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        ("We reached 561K users.", "觸及 56.1 萬名使用者"),
        ("The deal was £21M.", "交易金額 2100 萬英鎊"),
        ("We reached 385,306 users.", "觸及 38.5 萬名使用者"),
        ("We surveyed 120 managers and 80 staff.", "調查 200 人"),
        ("We analyzed 12 million posts.", "分析 12 萬則貼文"),
        ("We surveyed 188 managers.", "調查 189 名經理"),
        ("We surveyed three managers.", "調查 2 名經理"),
    ],
)
def test_conversions_rounding_and_sums_are_still_rejected(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


# --- B1 詞形變化 -------------------------------------------------------------------------------

SAME_ROOT = [
    ("theory", "theoretic"),
    ("theory", "theoretical"),
    ("theory", "theorise"),
    ("theoretic", "theoretical"),
    ("netnography", "netnographic"),
    ("sociomateriality", "sociomaterial"),
    ("realism", "realist"),
    ("Weber", "Weberian"),
    ("Schumpeter", "Schumpeterian"),
    ("paradox", "paradoxical"),
    ("calibrate", "calibration"),
    ("configuration", "configuring"),
    ("configuration", "configurational"),
    ("configuring", "configurational"),
    ("abduction", "abductive"),
    ("abduction", "abductively"),
    ("abductive", "abductively"),
    ("cognition", "cognitive"),
    ("mediation", "mediating"),
    ("data", "dataset"),
    ("data", "datasets"),
    ("diagram", "diagramming"),
    ("hacking", "hacks"),
]
DIFFERENT_WORDS = [
    ("model", "modern"),
    ("form", "formal"),
    ("organ", "organization"),
    ("policy", "police"),
    ("general", "generation"),
    ("relation", "relative"),
    ("community", "communication"),
    ("moral", "morale"),
    ("norm", "normal"),
    ("art", "artist"),
]
# 只看字尾、不查字典，擋不住（ADR-0032 2026-11-18 更新的「抓不到的情況」）。
KNOWN_LEAKS = [
    ("universe", "university"),
    ("organ", "organic"),
    ("organ", "organism"),
    ("author", "authority"),
    ("digit", "digital"),
]
# 保守而漏認的：兩邊都是 theory 的變化形，字根 theor 只有 5 個字母。
KNOWN_MISSES = [("theoretic", "theorise"), ("theoretical", "theorise")]


def _both_ways(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [*pairs, *((second, first) for first, second in pairs)]


@pytest.mark.parametrize(("in_input", "in_output"), _both_ways(SAME_ROOT))
def test_word_forms_with_the_same_root_are_allowed(in_input: str, in_output: str) -> None:
    theory = f"以 {in_output} 觀點分析"

    assert _theory_only(theory, f"We draw on {in_input}.") == theory


@pytest.mark.parametrize(("in_input", "in_output"), _both_ways(DIFFERENT_WORDS))
def test_words_sharing_only_a_prefix_are_rejected(in_input: str, in_output: str) -> None:
    assert _theory_rejected(f"以 {in_output} 觀點分析", f"We draw on {in_input}.") is UNSUPPORTED


@pytest.mark.parametrize(("in_input", "in_output"), _both_ways(KNOWN_LEAKS))
def test_known_leaks_of_the_suffix_rule(in_input: str, in_output: str) -> None:
    theory = f"以 {in_output} 觀點分析"

    assert _theory_only(theory, f"We draw on {in_input}.") == theory


@pytest.mark.parametrize(("in_input", "in_output"), _both_ways(KNOWN_MISSES))
def test_known_misses_of_the_suffix_rule(in_input: str, in_output: str) -> None:
    assert _theory_rejected(f"以 {in_output} 觀點分析", f"We draw on {in_input}.") is UNSUPPORTED


def test_plural_and_acronym_rules_are_unchanged() -> None:
    assert _theory_only("以 teams 觀點分析", "We study team work.") == "以 teams 觀點分析"
    assert _theory_only("計畫行為理論（TPB）", "We use the theory of planned behavior.") is not None
    assert _theory_rejected("計畫行為理論（TPB）", "We use the theory of behavior.") is UNSUPPORTED


# --- B2 輸入黏字 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("abstract", "word"),
    [
        ("We study usingFoocoinandBarledgerblockchain payments.", "Foocoin"),
        ("We study usingFoocoinandBarledgerblockchain payments.", "Barledger"),
        ("We study usingFoocoinandBarledgerblockchain payments.", "blockchain"),
        ("We study parasocialinteraction online.", "parasocial"),
        ("We study parasocialinteraction online.", "interactions"),
    ],
)
def test_words_glued_together_in_the_input_are_found(abstract: str, word: str) -> None:
    theory = f"以 {word} 觀點分析"

    assert _theory_only(theory, abstract) == theory


@pytest.mark.parametrize(
    ("abstract", "word"),
    [
        # 5 個字母以下不找子字串。
        ("We study usingFoocoinandBarledgerblockchain payments.", "chain"),
        ("We study parasocialinteraction online.", "inter"),
        # 不跨詞找：「social」＋「media」不是一個詞。
        ("We study social media.", "socialmedia"),
        # 輸入沒有的詞照樣擋。
        ("We study parasocialinteraction online.", "ethereum"),
    ],
)
def test_glued_word_rule_still_rejects_other_words(abstract: str, word: str) -> None:
    assert _theory_rejected(f"以 {word} 觀點分析", abstract) is UNSUPPORTED


def test_known_leak_of_the_glued_word_rule() -> None:
    # 子字串比對不分詞義：輸入有 information，輸出的 format 也算認得。
    assert _theory_only("以 format 觀點分析", "We study information sharing.") is not None


# --- B3 vs ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("findings", ["線上 vs 線下", "線上 vs. 線下", "線上 VS 線下"])
def test_versus_is_always_allowed(findings: str) -> None:
    output = _output(research_question=None, method=None, theory=None, findings=findings)

    assert _validate(output, _input(abstract="We compare channels.")).findings == findings


# --- E 逐句翻譯門檻 5 → 7 ------------------------------------------------------------------------

SIX = (
    "Teams adopt platforms. We surveyed 312 employees. TAM was used. "
    "Usefulness mattered. Ease of use did not. Size did not moderate."
)
SEVEN = SIX + " We discuss vendors."


def _sentences(count: int) -> dict[str, str]:
    extra = "".join("補充。" for _ in range(count - 4))
    return {
        "research_question": "團隊如何採用平台。" + extra,
        "method": "問卷調查 312 名員工。",
        "theory": "科技接受模型（TAM）。",
        "findings": "有用性重要。",
    }


def test_real_config_skips_abstracts_of_six_sentences() -> None:
    rules = load_zh_summary_rules({"RADAR_CONFIG_DIR": str(ROOT / "config")})
    output = _output(**_sentences(6))

    assert validate_zh_summary(output, _input(abstract=SIX), rules).method is not None


def test_real_config_still_rejects_translation_of_seven_sentences() -> None:
    rules = load_zh_summary_rules({"RADAR_CONFIG_DIR": str(ROOT / "config")})

    with pytest.raises(OutputRejectedError) as caught:
        validate_zh_summary(_output(**_sentences(7)), _input(abstract=SEVEN), rules)

    assert caught.value.code is ErrorCode.ZH_SUMMARY_SENTENCE_TRANSLATION


# --- F 繁中比例：「中文名稱（英文）」括號裡的英文不算 ---------------------------------------------

THEORY_ABSTRACT = (
    "We draw on the knowledge-based view of the firm, Q methodology, and the unified theory of "
    "acceptance and use of technology."
)


@pytest.mark.parametrize(
    "theory",
    [
        "知識基礎觀（knowledge-based view of the firm）",
        "知識基礎觀 (knowledge-based view of the firm)",
        "Q 方法（Q methodology）",
        "整合型模型（unified theory of acceptance and use of technology）",
    ],
)
def test_english_in_parentheses_after_han_is_not_counted(theory: str) -> None:
    assert _theory_only(theory, THEORY_ABSTRACT) == theory


@pytest.mark.parametrize(
    "theory",
    [
        "knowledge-based view of the firm",  # 整欄英文
        "觀點 knowledge-based view of the firm",  # 括號外的英文照算
        "（knowledge-based view of the firm）",  # 沒有漢字
        "觀點（knowledge-based view of the firm",  # 沒閉合
        "觀點（knowledge-based (view) of the firm）",  # 巢狀
        "Q (knowledge-based view of the firm) 觀點",  # 括號前面不是漢字
        # 括號裡超過 8 個英文詞：當成整句英文，照算。
        "觀點（the unified theory of acceptance and use of the technology）",
    ],
)
def test_other_english_still_counts_against_the_han_ratio(theory: str) -> None:
    assert _theory_rejected(theory, THEORY_ABSTRACT) is NOT_ZH_HANT


def test_parenthesized_word_count_rules() -> None:
    assert _english_word_count(check_copy("知識基礎觀（knowledge based view）")) == 0
    assert _english_word_count(check_copy("知識（knowledge based view）TAM")) == 1
    assert _english_word_count(check_copy("知識 TAM（knowledge based view）")) == 4
    assert _english_word_count(check_copy("知識（a）（b）")) == 1  # 第二個括號前面不是漢字


def test_han_ratio_threshold_still_comes_from_the_rules() -> None:
    strict = RULES.model_copy(update={"min_cjk_percent": 100})
    output = _output(
        research_question=None, method=None, findings=None, theory="Q 方法（Q methodology）"
    )

    with pytest.raises(OutputRejectedError) as caught:
        validate_zh_summary(output, _input(abstract=THEORY_ABSTRACT), strict)

    assert caught.value.code is NOT_ZH_HANT


# --- 共用函式的預設行為不變（研究模型看到的和修改前一樣）---------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We sampled 131 978 firms.", {_n("131"), _n("978")}),
        ("131 978", {_n("131"), _n("978")}),
        ("387, 000 users", {_n("387"), _n("0")}),
        ("150$\\,$000", {_n("150"), _n("0")}),
        ("in 2019 300 firms", {_n("2019"), _n("300")}),
        ("ranged 2.84%–38.94%", {_n("2.84", percent=True), _n("-38.94", percent=True)}),
        ("ranged 10–40%", {_n("10"), _n("40", percent=True)}),
        ("rose 10% points", {_n("10", percent=True)}),
        ("fell -4.40%", {_n("-4.4", percent=True)}),
        ("from September 2014 to May 2015", {_n("2014"), _n("2015")}),
        ("in the 1960s and '90s", {_n("1960"), _n("90")}),
    ],
)
def test_shared_number_reading_is_unchanged(text: str, expected: set[Number]) -> None:
    copy = check_copy(text)

    assert numbers(copy, keep_unscaled=True)[0] == expected
    assert numbers(copy)[0] == expected
    assert InputVocabulary.build(copy).numbers == frozenset(expected)
    assert structured_numbers(copy) == frozenset(expected)


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("theory", "theoretical"),
        ("realism", "realist"),
        ("data", "dataset"),
        ("hacks", "hacking"),
        ("parasocialinteraction", "parasocial"),
        ("information", "format"),
        ("compare", "vs"),
    ],
)
def test_shared_vocabulary_does_not_learn_the_new_rules(text: str, word: str) -> None:
    vocabulary = InputVocabulary.build(check_copy(text))

    assert not vocabulary.knows_word(word)
    assert not vocabulary.knows_token(word)
    assert ExtendedVocabulary.build(check_copy(text)).knows_word(word)


def test_shared_vocabulary_still_reads_digits_inside_alphanumeric_words() -> None:
    vocabulary = InputVocabulary.build(check_copy("B2B EC2 H2b 3-D COVID-19 R²"))

    assert vocabulary.numbers == frozenset({_n("2"), _n("3"), _n("19")})


# --- 新加的正規式：20 萬字元的最壞情況在 1 秒內跑完 ---------------------------------------------


@pytest.mark.parametrize(
    "unit",
    [
        # _SPACE_GROUPED、_NOT_DIGIT
        "1 000 ",
        "1 0000 ",
        "1, 000, ",
        "1$\\,$000",
        "1 23 ",
        # _PERCENT_DASH、_PERCENT_RANGE、_PERCENT_POINTS
        "%-1",
        "1.1-1.1 ",
        "1-1%",
        "1% ",
        "1% point ",
        # _DECADE、月份（_MAY_THEN_NUMBER）
        "1960s ",
        "'0s",
        "May 1 ",
        "May ",
        # _MONTH_CONTEXT、_DECADE_CONTEXT（輸出那一側）
        "1-1 ",
        "1 月",
        "1、",
        "1960-",
        "60 年代",
        # _ALNUM_PIECE、_EXPONENT_TOKEN、_OLD_READING_TOKEN
        "a1.",
        "a1.1x",
        "B2B ",
        "a-1 ",
        "1.1.1",
        "1e1",
        "1a",
        # 詞形變化、黏字
        "abcdefgation ",
        "a" * 7 + " ",
        # _HAN_PARENTHESIS
        "研(",
        "研 (a",
        "研(a)",
    ],
)
def test_new_patterns_run_in_linear_time(unit: str) -> None:
    text = check_copy(unit * (200_000 // len(unit)))
    started = time.perf_counter()

    vocabulary = ExtendedVocabulary.build(text)
    vocabulary.number_text(text)
    vocabulary.knows_token("abcdefghij-configurational")
    _english_word_count(text)

    assert time.perf_counter() - started < 1


def test_many_distinct_input_words_are_quick() -> None:
    letters = "abcdefghijklmnopqrstuvwxyz"
    words = " ".join(
        "w" + "".join(letters[int(digit)] for digit in f"{index:05d}") + "ation"
        for index in range(20_000)
    )
    text = check_copy(words)
    started = time.perf_counter()

    vocabulary = ExtendedVocabulary.build(text)
    for index in range(100):
        vocabulary.knows_word(f"unrelatedation{index}")

    assert time.perf_counter() - started < 1


def test_long_adversarial_output_is_validated_quickly() -> None:
    loose = ZhSummaryRules(
        max_chars_per_field=10**6, translation_min_abstract_sentences=7, min_cjk_percent=1
    )
    text = "研究（B2B 1 000 9 月 60 年代 theoretical）" * 4_000
    output = _output(research_question=text, method=None, theory=None, findings=None)
    started = time.perf_counter()

    with contextlib.suppress(OutputRejectedError):
        validate_zh_summary(output, _input(abstract=text), loose)

    assert time.perf_counter() - started < 2
