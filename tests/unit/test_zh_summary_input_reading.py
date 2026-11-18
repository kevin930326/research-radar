"""中文重點：把輸入讀得更完整（ADR-0032 的 2026-11-18 更新，`ExtendedVocabulary`）。

每一項放行都有「仍然擋得住編造」的反例；另外固定共用函式（`numbers`、`structured_numbers`、
`InputVocabulary`）的預設行為不變，研究模型看到的和以前一樣。標題、摘要、輸出全是測試自己編的。
"""

import contextlib
import itertools
import time
from decimal import Decimal

import pytest

from radar.config import load_zh_summary_rules
from radar.domain.ai_outputs import OutputRejectedError
from radar.domain.errors import ErrorCode
from radar.domain.validators import text_checks
from radar.domain.validators.text_checks import (
    _ALNUM_PIECE,
    _NUMBER,
    ExtendedVocabulary,
    InputVocabulary,
    Number,
    _keeps_whole_numbers,
    _prepared_for_numbers,
    _whole_numbers,
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
        # 開頭不能在指數裡（審查 C4）：`1e+3 100` 不能合併成 1e+3100。
        ("The estimate was 1e+3 100.", "估計值為 1e3100"),
        ("The estimate was 1e+3 100.", "估計值為 3100"),
        ("The estimate was 1e-3 100.", "估計值為 1e-3100"),
        ("The estimate was 1e-3 100.", "估計值為 3100"),
        # 最後一組後面接指數時不合併進去（修正時自己找到的同類問題）。
        ("We sampled 1 000e3 firms.", "抽樣 1000e3 家企業"),
    ],
)
def test_space_thousands_still_reject_invented_numbers(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


def test_space_thousands_leave_exponents_alone() -> None:
    assert _method_only("估計值為 1e+3 與 100", "The estimate was 1e+3 100.") is not None


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
        ("Data were collected in Mar.", "3 月收集資料"),
        ("Data were collected in MARCH.", "3 月收集資料"),
        # 指數照原本的方式讀，輸入有同一個值就過（審查 C2：修改前會過，上一版誤擋）。
        ("The observation period was 3e-1 months, starting in January.", "觀察 3e-1 月"),
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
        # 全大寫的縮寫不是月份（審查 A-5）：MAR（missing at random）、APR（年利率）。
        ("Data were MAR (missing at random).", "3 月收集資料"),
        ("Loans with a high APR were studied.", "4 月收集資料"),
        ("Data were collected in SEPT.", "9 月收集資料"),
        # 月份位置的數字必須是完整、獨立的整數（審查 C2）：指數的尾段、負數不算。
        ("Data were collected in January. There were 3 groups.", "追蹤 3e-1 月"),
        ("Data were collected in January. There were 3 groups.", "追蹤 3e+1 月"),
        ("Data were collected in March.", "變動 -3 月"),
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
        # 開頭的負號照樣讀（輸出的正數可以對輸入同值的負數）。
        ("Adoption fell by -10% points.", "採用率下降 10 個百分點"),
        ("Adoption ranged from -10-40%.", "採用率最低 -10%"),
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
        # 開頭不能在指數裡（審查 C3）。
        ("The change was 1.2e-3% points.", "提高 3 個百分點"),
        ("Adoption ranged from 1e-10-40%.", "採用率為 10%"),
        ("The change was 10^-3% points.", "提高 3 個百分點"),  # 修正時自己找到的同類問題
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
        # 範圍的後半照樣是年代。
        ("Firms founded in the 1950s-1960s.", "50 至 60 年代成立的企業"),
        # 指數照原本的方式讀，輸入有同一個值就過（審查 C2）。
        ("The value was 3e-60, for firms of the 1960s.", "數值 3e-60 年代"),
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
        # 年代位置的數字必須是完整、獨立的整數（審查 C2）：指數的尾段、負數不算。
        ("Firms founded in the 1960s. There were 3 groups.", "3e-60 年代"),
        ("Firms founded in the 1960s. There were 3 groups.", "3e+60 年代"),
        ("Firms founded in the 1960s.", "變動 -60 年代"),
        # 輸入的指數尾段不是年代（修正時自己找到的同類問題）。
        ("The delay was 3e-10s.", "10 年代"),
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
        ("We rank the top 10 firms.", "排名 Top10 的企業"),
        ("We study K-12 schools.", "調查 K12 學校"),
    ],
)
def test_alphanumeric_words_from_the_input_are_allowed(abstract: str, method: str) -> None:
    assert _method_only(method, abstract) == method


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # 數字開頭的英數詞照原本的方式讀（審查 C1、B-3：修改前會過，上一版誤擋）。
        ("We ran a 2 × 2 experiment.", "2x2 實驗"),
        ("We ran a 2×2 experiment.", "2x2 實驗"),
        ("We ran a 2 by 2 experiment.", "2x2 實驗"),
        ("Data from Q1 2020 were used.", "使用 2020Q1 資料"),
        ("We reached 12 million users.", "觸及 12million 使用者"),
    ],
)
def test_alphanumeric_words_starting_with_a_digit_are_read_as_numbers(
    abstract: str, method: str
) -> None:
    assert _method_only(method, abstract) == method


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # 數字開頭的英數詞照原本的方式讀，正負號、百分比照樣檢查（審查 C1）。
        ("We reached 12 million users.", "變動為 -12million 使用者"),
        ("We reached 12 million users.", "比例為 12million%"),
        # 字母開頭、輸入有的英數詞後面接百分比、量詞時照原本的方式讀（審查 A-6）。
        ("We rank the top 10 firms.", "排名 Top10% 的企業"),
        ("We study K-12 schools.", "調查 K12 萬名學生"),
        ("We study K-12 schools.", "調查 K12,000 名學生"),
        ("We study Web 2.0 tools.", "研究 Web2.0% 的工具"),
        ("We study Web 2.0 tools.", "研究 Web2.0 percent 的工具"),
        # 輸入串接時最多一段含數字（審查 A-6）。
        ("We rank the top 5 10 firms.", "排名 Top510 的企業"),
        ("In Study 1 30 participants joined.", "Study130 的參與者"),
    ],
)
def test_alphanumeric_words_do_not_hide_signs_percents_or_joined_numbers(
    abstract: str, method: str
) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


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
        # 展開寫法：這裡的 RULES 沒有允許清單，照樣擋（有清單時見 test_zh_summary_acronyms.py）。
        ("We study peer-to-peer lending in two cities.", "研究 P2P 借貸"),
        ("We study business-to-business sales in two regions.", "研究 B2B 銷售"),
        ("Hypothesis 2b was supported.", "H2b 成立"),
        # 3 在輸入裡、EC3 不在（修改前會過；審查 B-9 換成分得出新舊行為的例子）。
        ("We study EC 2 in 3 regions.", "研究 EC3"),
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
    # 原本是「已知漏認」（KNOWN_MISSES）：-etic／-etical 的窄例外（審查 C6、A-4、B-1）。
    ("theoretic", "theorise"),
    ("theoretical", "theorise"),
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
# 審查 A-1 收緊後擋下的（輸入原形 → 輸出變化形）：-ism／-ist、-ive 不和原形配，-ity 和原形配時字根
# 至少 7 個字母。後兩組原本是「已知漏網」（KNOWN_LEAKS）。反方向（輸入 individualism、輸出
# individual）原形 6 個字母以上時仍由黏字規則認得（見 GLUED_LEAKS）。
TIGHTENED = [
    ("individual", "individualism"),
    ("collective", "collectivism"),
    ("material", "materialist"),
    ("posit", "positive"),
    ("object", "objective"),
    ("minor", "minority"),
    ("prior", "priority"),
    ("morale", "morality"),
    ("organ", "organism"),
    ("author", "authority"),
]
# 只看字尾、不查字典，擋不住（ADR-0032 2026-11-18 更新的「抓不到的情況」）。
KNOWN_LEAKS = [
    ("universe", "university"),
    ("organ", "organic"),
    ("digit", "digital"),
    ("execution", "executive"),
    ("personal", "personality"),
    ("institute", "institution"),
]


def _both_ways(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [*pairs, *((second, first) for first, second in pairs)]


@pytest.mark.parametrize(("in_input", "in_output"), _both_ways(SAME_ROOT))
def test_word_forms_with_the_same_root_are_allowed(in_input: str, in_output: str) -> None:
    theory = f"以 {in_output} 觀點分析"

    assert _theory_only(theory, f"We draw on {in_input}.") == theory


@pytest.mark.parametrize(("in_input", "in_output"), _both_ways(DIFFERENT_WORDS))
def test_words_sharing_only_a_prefix_are_rejected(in_input: str, in_output: str) -> None:
    assert _theory_rejected(f"以 {in_output} 觀點分析", f"We draw on {in_input}.") is UNSUPPORTED


@pytest.mark.parametrize(("in_input", "in_output"), TIGHTENED)
def test_suffixes_that_change_the_meaning_do_not_pair_with_the_base(
    in_input: str, in_output: str
) -> None:
    assert _theory_rejected(f"以 {in_output} 觀點分析", f"We draw on {in_input}.") is UNSUPPORTED


@pytest.mark.parametrize(
    ("in_input", "in_output"),
    _both_ways([("relation", "relative"), ("general", "generation"), ("genetic", "generation")]),
)
def test_the_etic_exception_does_not_lower_the_sibling_threshold(
    in_input: str, in_output: str
) -> None:
    assert _theory_rejected(f"以 {in_output} 觀點分析", f"We draw on {in_input}.") is UNSUPPORTED


@pytest.mark.parametrize(("in_input", "in_output"), _both_ways(KNOWN_LEAKS))
def test_known_leaks_of_the_suffix_rule(in_input: str, in_output: str) -> None:
    theory = f"以 {in_output} 觀點分析"

    assert _theory_only(theory, f"We draw on {in_input}.") == theory


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


@pytest.mark.parametrize(
    ("in_input", "in_output"),
    [
        ("unemployment", "employment"),
        ("dissatisfaction", "satisfaction"),
        ("disconfirmation", "confirmation"),
        ("noncompliance", "compliance"),
        ("inefficiency", "efficiency"),
        ("misinformation", "information"),
        ("irrationality", "rationality"),
        ("illegitimacy", "legitimacy"),
        ("immateriality", "materiality"),
        ("dissatisfactions", "satisfaction"),  # 複數也一樣
    ],
)
def test_glued_word_rule_skips_a_negation_prefix(in_input: str, in_output: str) -> None:
    # 審查 A-2、B-5：命中前面剛好是一個否定字首、命中之後就是詞尾，那個命中不算。
    assert (
        _theory_rejected(f"以 {in_output} 觀點分析", f"We study {in_input} online.") is UNSUPPORTED
    )


@pytest.mark.parametrize(
    ("abstract", "word"),
    [
        # 同一個詞在輸入別處有別的命中，照算。
        ("We study dissatisfaction and satisfactionlevels online.", "satisfaction"),
        ("We study dissatisfaction and customersatisfaction online.", "satisfaction"),
        # 否定字首之後不是詞尾：照算。
        ("We study unemploymentrates online.", "employment"),
        # 前面不只一個否定字首：照算（黏字）。
        ("We study usingdissatisfaction online.", "satisfaction"),
    ],
)
def test_glued_word_rule_still_finds_other_hits(abstract: str, word: str) -> None:
    theory = f"以 {word} 觀點分析"

    assert _theory_only(theory, abstract) == theory


# 子字串比對不分詞義，擋不住（ADR-0032 2026-11-18 更新的「抓不到的情況」）。
GLUED_LEAKS = [
    ("information", "format"),
    ("international", "national"),
    ("asymmetric", "symmetric"),
    ("individualism", "individual"),
    ("authority", "author"),
]


@pytest.mark.parametrize(("in_input", "in_output"), GLUED_LEAKS)
def test_known_leaks_of_the_glued_word_rule(in_input: str, in_output: str) -> None:
    theory = f"以 {in_output} 觀點分析"

    assert _theory_only(theory, f"We study {in_input} sharing.") == theory


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
    "acceptance and use of technology model."
)
GREEK = "alpha beta gamma delta epsilon zeta eta theta"
CODES = "ab cd ef gh ij kl mn op qr st uv wx yz"
LONG_ABSTRACT = f"We study {GREEK}. The codes were {CODES}."


@pytest.mark.parametrize(
    "theory",
    [
        "知識基礎觀（knowledge-based view of the firm）",
        "知識基礎觀 (knowledge-based view of the firm)",
        "Q 方法（Q methodology）",
        "整合型模型（unified theory of acceptance and use of technology）",
        # 9 個詞：整欄額度 8 個不算，只多算 1 個（審查 C7）。
        "模型（unified theory of acceptance and use of technology model）",
        # 原本「超過 8 個詞的括號整個照算」會擋；10 個詞現在只多算 2 個（審查 C7）。
        "觀點（the unified theory of acceptance and use of the technology）",
        # 兩個括號合計 8 個詞。
        "知識基礎觀（knowledge-based view of the firm）與 Q 方法（Q methodology）",
    ],
)
def test_english_in_parentheses_after_han_is_not_counted(theory: str) -> None:
    assert _theory_only(theory, THEORY_ABSTRACT) == theory


@pytest.mark.parametrize(
    "theory",
    [
        # 只有最外層、不巢狀的括號算例外（審查 C5）：內層緊接漢字也不算。
        f"研（研（{GREEK}））",
        # 整欄額度 8 個詞：拆成幾個括號也只有 8 個不算（審查 C7、A-3、B-6）。
        "研（ab cd ef gh ij kl mn op）果（qr st uv wx yz ab cd ef）效（gh ij kl）",
    ],
)
def test_nested_or_split_parentheses_still_count(theory: str) -> None:
    assert _theory_rejected(theory, LONG_ABSTRACT) is NOT_ZH_HANT


def test_known_leak_of_the_parenthesis_quota() -> None:
    # 一個漢字帶 8 個英文詞仍會過比例檢查（英文詞仍要在摘要裡，不是編造）。
    assert _theory_only(f"研（{GREEK}）", LONG_ABSTRACT) is not None


@pytest.mark.parametrize(
    "theory",
    [
        "knowledge-based view of the firm",  # 整欄英文
        "觀點 knowledge-based view of the firm",  # 括號外的英文照算
        "（knowledge-based view of the firm）",  # 沒有漢字
        "觀點（knowledge-based view of the firm",  # 沒閉合
        "觀點（knowledge-based (view) of the firm）",  # 巢狀
        "Q (knowledge-based view of the firm) 觀點",  # 括號前面不是漢字
        # 括號裡的英文超過整欄額度 8 個詞的部分照算（10 個詞算 2 個，漢字只有 1 個）。
        "觀（the unified theory of acceptance and use of the technology）",
    ],
)
def test_other_english_still_counts_against_the_han_ratio(theory: str) -> None:
    assert _theory_rejected(theory, THEORY_ABSTRACT) is NOT_ZH_HANT


def test_parenthesized_word_count_rules() -> None:
    assert _english_word_count(check_copy("知識基礎觀（knowledge based view）")) == 0
    assert _english_word_count(check_copy("知識（knowledge based view）TAM")) == 1
    assert _english_word_count(check_copy("知識 TAM（knowledge based view）")) == 4
    assert _english_word_count(check_copy("知識（a）（b）")) == 1  # 第二個括號前面不是漢字
    assert _english_word_count(check_copy("知（a（b）c）")) == 3  # 巢狀：外層內層都照算
    assert _english_word_count(check_copy("知（a 研（b）")) == 2  # 外層沒閉合：內層也照算
    assert _english_word_count(check_copy("知 a) 研（b）")) == 1  # 多出來的 `)` 不影響後面
    assert _english_word_count(check_copy("知（a b c d e）識（f g h i j）")) == 2  # 整欄額度 8


def test_han_ratio_threshold_still_comes_from_the_rules() -> None:
    strict = RULES.model_copy(update={"min_cjk_percent": 100})
    output = _output(
        research_question=None, method=None, findings=None, theory="Q 方法（Q methodology）"
    )

    with pytest.raises(OutputRejectedError) as caught:
        validate_zh_summary(output, _input(abstract=THEORY_ABSTRACT), strict)

    assert caught.value.code is NOT_ZH_HANT


# --- 第二輪審查（R1～R3、F2、F4）：新規則以既有讀法的完整數字為單位 -----------------------------


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # R1：Q 替換不能切開完整數字（`K1e-3` 的 `1e-3`）。
        ("Model K1e uses 3 groups.", "模型 K1e-3"),
        ("Model K1e uses 3 groups.", "模型 K1E+3"),
        ("Model K1e uses 3 groups.", "模型 Ｋ１ｅ－３"),
        ("Model K1e uses 3 groups and 3 percent.", "模型 K1e-3%"),
        # R2：百分比的新規則不從科學記號裡取值。
        ("The effect is 1.2 × 10-3% points.", "提高 10%"),
        ("The effect is 1.2 × 10-3% points.", "提高 3 個百分點"),
        ("The effect is 10^ -3% points.", "提高 3 個百分點"),
        ("The effect is 10^ -3% points.", "提高 3%"),
        ("The effect is １．２ × １０＾ －３％ points.", "提高 3 個百分點"),
        ("The effect is １．２ × １０＾ －３％ points.", "提高 10%"),
        # R3：年代的新規則不從科學記號裡取值。
        ("The delay was 1.2 × 10-10s.", "追溯 10 年代"),
        ("The delay was 1.2 × 10^ -10s.", "追溯 10 年代"),
        ("The delay was 10 ^ - 10s.", "追溯 10 年代"),
        # F2：指數仍不是年代。
        ("The delay was 3e-10s.", "追溯 10 年代"),
        ("The delay was 2E+10s.", "追溯 10 年代"),
        # F4：`±` 後面的數字不當月份、年代。
        ("Data were collected in March.", "前後 ±3 月"),
        ("Firms founded in the 1960s.", "前後 ±60 年代"),
        ("Data were collected in March.", "前後 ∓3 月"),
    ],
)
def test_new_rules_do_not_read_pieces_of_whole_numbers(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # 英數詞本身照樣認得（R1 的對照）。
        ("Model K1e uses 3 groups.", "模型 K1e 有 3 組"),
        # F2：年代前面是英文字首＋連字號照樣算（上一版誤擋）。
        ("Firms founded pre-1960s were studied.", "60 年代以前"),
        ("Firms in the late-1960s grew.", "60 年代後期"),
        ("Pre-1960s firms were studied.", "60 年代以前"),
        ("Firms in the mid-1960s grew.", "60 年代中期"),
    ],
)
def test_whole_number_rules_keep_ordinary_writings(abstract: str, method: str) -> None:
    assert _method_only(method, abstract) == method


# 系統性的測試：帶指數、科學記號、量詞、百分比、千分位、負號的寫法（都不是「單純的數字」），接上
# 每一條新規則會看的前後文，斷言新規則讀不出這些寫法的片段。
WRITINGS = [
    "1e-3",
    "1E+3",
    "1e-10",
    "2E+10",
    "3e-1",
    "1.2e-10",
    "4.2 × 10^-2",
    "4.2 x 10-2",
    "4.2 x 10-20",
    "1.2 × 10 ^ - 3",
    "1.2 × 10-3",
    "1.2 × 10-10",
    "1.2 × 10^ -10",
    "10^-3",
    "10^ -3",
    "10 ^ - 10",
    "１．２ × １０＾ －３",
    # 第三輪 T2：既有讀法認不出整段的科學記號。
    "1.2 ⋅ 10-3",
    "1.2 ∙ 10-3",
    "7.7 ✕ 10-9",
    "1.2 E -3",
    "1.2 E - 3",
    "10^ ±3",
    "10**-9",
    "7.7e - 90",
    "12 million",
    "12M",
    "1,200",
    "1,960",
    "-0.35",
    "35%",
    "1.5 萬",
    "2.5 percent",
]
INPUT_PREFIXES = ["", "K", "pre-", "10-", "%"]
INPUT_SUFFIXES = [
    "",
    "% points",
    " percent points",
    "%-5%",
    "-40%",
    " - 40%",
    "s",
    "'s",
    " 000",
    ", 000",
    "$\\,$000",
    " 000 000",
]


def _magnitudes(values: frozenset[Number]) -> set[Decimal]:
    return {abs(value) for value, _ in values}


@pytest.mark.parametrize("writing", WRITINGS)
def test_extra_input_numbers_never_come_from_pieces(writing: str) -> None:
    # 不變量：新讀法多認得的數字與年代（只看大小），只能來自前後文自己的數字（把這個寫法換成 `Q`
    # 後讀到的），或這個寫法整個的值（`numbers` 讀到的；科學記號整段讀不到，所以是空的）；年代另外
    # 可以是四位數的後兩位。科學記號、指數、量詞、百分比、千分位裡的片段都不在其中。空白千分位會造出
    # 新值，但這些寫法沒有一個是 1～3 位的單純整數，所以也不該出現。
    whole = _magnitudes(frozenset(numbers(check_copy(writing), keep_unscaled=True)[0]))
    broken = []
    for prefix, suffix in itertools.product(INPUT_PREFIXES, INPUT_SUFFIXES):
        text = check_copy(f"The value was {prefix}{writing}{suffix} here.")
        context = check_copy(f"The value was {prefix}Q{suffix} here.")
        allowed = whole | _magnitudes(InputVocabulary.build(context).numbers)
        allowed_decades = allowed | {value % 100 for value in allowed if value % 1 == 0}
        vocabulary = ExtendedVocabulary.build(text)
        extra = _magnitudes(vocabulary.numbers - vocabulary.base.numbers)
        decades = {Decimal(decade) for decade in vocabulary.decades}
        if not extra <= allowed or not decades <= allowed_decades:
            broken.append((text, extra - allowed, decades - allowed_decades))
    assert broken == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("rose 10% points", _n("10")),
        ("rose 10 percent points", _n("10")),
        ("ranged 10-40%", _n("10", percent=True)),
        ("ranged 10 - 40%", _n("10", percent=True)),
        ("ranged 3%-5%", _n("5", percent=True)),
        ("coded 1 000 posts", _n("1000")),
        ("coded 1, 000 posts", _n("1000")),
        ("coded 1$\\,$000 posts", _n("1000")),
        ("coded 1 000 000 posts", _n("1000000")),
        ("coded -1 000 posts", _n("-1000")),
        ("coded 54 000 million posts", _n("54000000000")),
    ],
)
def test_the_combination_contexts_do_add_numbers_after_plain_numbers(
    text: str, expected: Number
) -> None:
    # 上面不變量測試的對照：同樣的前後文接在單純的數字後面，新規則確實會多認得（測試不是空轉）。
    vocabulary = ExtendedVocabulary.build(check_copy(text))

    assert expected in vocabulary.numbers - vocabulary.base.numbers


@pytest.mark.parametrize(
    ("text", "decade"),
    [
        ("the 1960s", 60),
        ("the 1960's", 60),
        ("the '90s", 90),
        ("pre-1960s", 60),
        ("1950s-1960s", 60),
        ("1950-60s", 60),
    ],
)
def test_the_combination_contexts_do_add_decades_after_plain_numbers(
    text: str, decade: int
) -> None:
    assert decade in ExtendedVocabulary.build(check_copy(text)).decades


@pytest.mark.parametrize(
    "text",
    [
        "1 234 5678 posts",
        "1 234 567.89 posts",
        "1 000 000e3 posts",
        "1 300.5 posts",
        "COVID-19 300 posts",
        "K12 345 posts",
        "1e+3 100 posts",
    ],
)
def test_space_thousands_need_a_clean_whole_run(text: str) -> None:
    # 整串要從獨立的 1～3 位整數開始、每組剛好 3 位，後面不再接「分隔＋數字」，否則整串不合併。
    vocabulary = ExtendedVocabulary.build(check_copy(text))

    assert vocabulary.numbers - vocabulary.base.numbers == frozenset()


OUTPUT_CONTEXTS = [
    "{w}",
    "K{w}",
    "Top{w}",
    "研究 K{w} 的",
    "{w} 月",
    "{w}月",
    "K{w} 月",
    "7-{w} 月",
    "{w}-3 月",
    "±{w} 月",
    "{w} 年代",
    "{w}-60 年代",
    "K{w}%",
    "K{w} 萬",
]
# 輸出側的詞彙：所有月份、年代都在輸入裡，所以只要結構允許，月份、年代位置的數字都會被換掉。
CONTEXT_WORDS = (
    "From January, February, March, April, May 1, June, July, August, September, October, "
    "November to December, in the 1900s, 1910s, 1920s, 1930s, 1940s, 1950s, 1960s, 1970s, "
    "1980s, 1990s, 2000s, 2010s and 2020s."
)


def _whole_spans(text: str) -> set[tuple[int, int, str]]:
    prepared, wholes = _whole_numbers(text)
    return {(number.start, number.end, prepared[number.start : number.end]) for number in wholes}


@pytest.mark.parametrize("writing", [*WRITINGS, "3", "12", "60", "1960"])
def test_output_replacements_only_remove_whole_numbers(writing: str) -> None:
    # 不變量（不經保險）：替換後讀到的每一個完整數字，都是替換前就在同一個位置、一模一樣的完整數字
    # （替換只能把完整數字整個拿掉），讀到的數字只會變少，讀不懂的仍讀不懂。
    broken = []
    for context in OUTPUT_CONTEXTS:
        text = check_copy(context.format(w=writing))
        tokens = [m.group(0) for m in _ALNUM_PIECE.finditer(text) if m.group(0)[0].isalpha()]
        vocabulary = ExtendedVocabulary.build(
            check_copy(f"{CONTEXT_WORDS} We study {' '.join(tokens)}.")
        )
        replaced, _ = vocabulary._replaced_number_text(text)
        if not _whole_spans(replaced) <= _whole_spans(text) or not _keeps_whole_numbers(
            text, replaced
        ):
            broken.append((text, replaced))
    assert broken == []


@pytest.mark.parametrize(
    ("text", "replaced"),
    [
        ("研究 K12 的", "研究 QQQ 的"),
        ("9 月", "Q 月"),
        ("7-8月", "Q-Q月"),
        ("60 年代", "QQ 年代"),
        ("1960 年代", "QQQQ 年代"),
    ],
)
def test_output_replacements_still_happen_for_plain_numbers(text: str, replaced: str) -> None:
    # 上面不變量測試的對照：單純的數字照樣會被換掉（測試不是空轉）。
    vocabulary = ExtendedVocabulary.build(check_copy(f"{CONTEXT_WORDS} We study K12."))

    assert vocabulary.number_text(text) == (replaced, None)


@pytest.mark.parametrize(
    ("before", "after", "safe"),
    [
        ("研究 B2B", "研究 QQQ", True),
        ("9 月", "Q 月", True),
        ("7-8月", "Q-Q月", True),
        # 切開完整數字：讀出新的值。
        ("模型 K1e-3", "模型 QQQ-3", False),
        # 切出來的值剛好在別處也有（審查 T5）：只比數值集合會判成安全，比位置才擋得住。
        ("模型 K1e-3、共 3 組", "模型 QQQ-3、共 3 組", False),
        # 把讀不懂（科學記號）變成讀得懂。
        ("3 x 10-2 月", "3 x QQ-Q 月", False),
    ],
)
def test_the_safety_net_compares_whole_numbers(before: str, after: str, *, safe: bool) -> None:
    # 第三輪審查 T5：保險改成比位置——替換後的每一個完整數字都要是替換前同位置、各部分一模一樣的。
    assert _keeps_whole_numbers(before, after) is safe


def test_the_safety_net_is_not_fooled_by_a_matching_value(monkeypatch: pytest.MonkeyPatch) -> None:
    # 審查 T5 的例子：結構檢查失效時，切出來的 3 剛好也在輸出別處、也在輸入裡，保險仍整欄不換。
    monkeypatch.setattr(text_checks, "_covers_whole_numbers", lambda *_: True)
    monkeypatch.setattr(text_checks, "_exact_integer", lambda *_: True)
    abstract = "Model K1e uses 3 groups in March."
    vocabulary = ExtendedVocabulary.build(check_copy(abstract))
    method = check_copy("模型 K1e-3，共 3 組")

    assert vocabulary._replaced_number_text(method)[0] == "模型 QQQ-3、共 3 組"
    assert vocabulary.number_text(method) == (method, None)
    assert _method_rejected("模型 K1e-3，共 3 組", abstract) is UNSUPPORTED


def test_unreadable_is_also_taken_from_the_text_before_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `zh_summary.py` 的「讀不懂取聯集」那一行是多一層的保險：讓結構檢查與保險都失效，
    # `3 x 10-2 月` 換成讀得懂的 `3 x QQ-Q 月`，仍因為替換前讀不懂而拒收。
    monkeypatch.setattr(text_checks, "_covers_whole_numbers", lambda *_: True)
    monkeypatch.setattr(text_checks, "_exact_integer", lambda *_: True)
    monkeypatch.setattr(text_checks, "_keeps_whole_numbers", lambda *_: True)
    abstract = "Data were collected in February and October from 3 groups."
    vocabulary = ExtendedVocabulary.build(check_copy(abstract))

    assert vocabulary.number_text("觀察 3 x 10-2 月") == ("觀察 3 x QQ-Q 月", None)
    assert _method_rejected("觀察 3 x 10-2 月", abstract) is UNSUPPORTED


def test_the_safety_net_keeps_the_original_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # 讓結構檢查失效（什麼片段都換）：保險仍把整欄照原本的方式讀。
    monkeypatch.setattr(text_checks, "_covers_whole_numbers", lambda *_: True)
    monkeypatch.setattr(text_checks, "_exact_integer", lambda *_: True)
    vocabulary = ExtendedVocabulary.build(
        check_copy("Model K1e uses 3 groups in January and October.")
    )

    assert vocabulary._replaced_number_text("模型 K1e-3")[0] == "模型 QQQ-3"
    assert vocabulary.number_text("模型 K1e-3") == ("模型 K1e-3", None)
    # 第三輪 T4 之後 `2.1 月` 的 1 前面是 `.`，正規式就不配了（原本這裡用 `2.1 月` 當例子），改用
    # 科學記號裡的月份位置。
    assert vocabulary._replaced_number_text("觀察 2.1 月")[0] == "觀察 2.1 月"
    assert vocabulary._replaced_number_text("觀察 3 x 10-1 月")[0] == "觀察 3 x QQ-Q 月"
    assert vocabulary.number_text("觀察 3 x 10-1 月") == ("觀察 3 x 10-1 月", None)
    assert _method_rejected("模型 K1e-3", "Model K1e uses 3 groups.") is UNSUPPORTED


@pytest.mark.parametrize(
    "text",
    [
        *WRITINGS,
        "β = - 0.42",
        "β =- 0.42",
        "(- 0.42) and : - 3",
        "x 10-2 and 10 ^ - 3 and 1.2 × 10^ -10",
        "12 萬 and 3% and 1,200.5 and .05",
    ],
)
def test_whole_numbers_match_the_shared_reading(text: str) -> None:
    # 完整數字的序列就是既有讀法（`_prepared_for_numbers` 之後的 `_NUMBER`）讀到的那些，只是位置
    # 對得回原文。
    copy = check_copy(text)
    shared = [match.group(0) for match in _NUMBER.finditer(_prepared_for_numbers(copy))]
    prepared, wholes = _whole_numbers(copy)

    assert [prepared[number.start : number.end] for number in wholes] == shared
    assert len(prepared) == len(copy)
    # `_whole_numbers` 依位置取 `_NUMBER` 的五個群組。
    assert list(_NUMBER.groupindex) == ["sign", "mantissa", "exponent", "scale", "percent"]
    assert _NUMBER.groups == 5


# --- 第三輪審查（S1、T1、T2、T4）-----------------------------------------------------------------


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # S1：月份、年代位置的數字前面隔一個空白是正負號。
        ("Data were collected in March.", "前後 ± 3 月"),
        ("Data were collected in March.", "前後 ∓ 3 月"),
        ("Data were collected in March.", "約 + 3 月"),
        ("Data were collected in March.", "前後 - 3 月"),
        ("Data were collected in March.", "- 3 月"),
        ("Data were collected in March and July.", "前後 ± 3-7 月"),
        ("Firms founded in the 1960s.", "前後 ± 60 年代"),
        ("Firms founded in the 1960s.", "前後 - 60 年代"),
        ("Firms founded in the 1960s.", "前後 + 60 年代"),
    ],
)
def test_signs_one_space_before_months_or_decades_are_rejected(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # S1：「-＋空白」在兩個「月」／「年代」之間是範圍；同一段裡的範圍不受影響。
        ("Data were collected from December to March.", "12 月 - 3 月"),
        ("Data were collected from December 2014 to March 2015.", "2014 年 12 月 - 2015 年 3 月"),
        ("Data were collected from December 2014 to March.", "2014 年 12 月 - 3 月"),
        ("Firms founded in the 1950s and 1960s.", "50 年代 - 60 年代"),
        ("Firms founded in the 1950s and 1960s.", "1950 年代 - 1960 年代"),
        ("Data were collected in July and August.", "7 - 8月"),
        ("Data were collected in July and August.", "7-8月"),
        ("Data were collected in July and August.", "7 至 8 月"),
        # T4：範圍前半是某個完整數字的片段時，後面獨立的「N 月」照樣換。
        ("Cases rose during COVID-19 in March.", "COVID-19 至 3 月"),
        ("We studied Web2.0 sites in August.", "Web2.0至8月"),
    ],
)
def test_month_and_decade_ranges_still_match(abstract: str, method: str) -> None:
    assert _method_only(method, abstract) == method


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # T4 的反例：輸入沒有 March。
        ("Cases rose during COVID-19 in April.", "COVID-19 至 3 月"),
        ("We studied Web2.0 sites in July.", "Web2.0至8月"),
        # 第二輪的收緊照樣在（數字黏在詞、小數、指數、連字號後面）。
        ("Data were collected in March by 1 team.", "觀察 1,3 月"),
        ("Data were collected in March of 2014.", "觀察 2014-3 月"),
        ("Data were collected in March. There were 3 groups.", "觀察 3e-3 月"),
    ],
)
def test_month_numbers_attached_to_other_numbers_are_rejected(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # T1：空白千分位後面接「逗號＋空白＋別的數字」是列舉，前面那串照樣合併。
        ("The sample (n = 12 345, 51.2% female) was used.", "樣本 12345 人"),
        ("Samples of 1 200, 3 400 and 5 600 firms.", "1200 家企業"),
        ("Samples of 1 200, 3 400 and 5 600 firms.", "3400 與 5600 家企業"),
        ("We coded 131 978, 27 397 and 54 000 posts.", "編碼 131978 則"),
        ("We coded 54 000, 2019 data.", "編碼 54000 則"),
    ],
)
def test_space_thousands_followed_by_a_list_are_merged(abstract: str, method: str) -> None:
    assert _method_only(method, abstract) == method


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        ("Samples of 1 200, 3 400 and 5 600 firms.", "1201 家企業"),
        ("Samples of 1 200, 3 400 and 5 600 firms.", "12003400 家企業"),
        ("The sample (n = 12 345, 51.2% female) was used.", "樣本 12346 人"),
        ("We coded 54 000, 2019 data.", "編碼 540002019 則"),
    ],
)
def test_space_thousands_followed_by_a_list_still_reject_invented_numbers(
    abstract: str, method: str
) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


@pytest.mark.parametrize(
    "text",
    [
        # T1 不動「空白或 `$\,$` 後面還接數字就不合併」。
        "1 234 567.89 posts",
        "1 300.5 posts",
        "1 000 000e3 posts",
        "1 000 5G sites",
        "1 000$\\,$5 sites",
    ],
)
def test_space_thousands_followed_by_more_digits_are_not_merged(text: str) -> None:
    vocabulary = ExtendedVocabulary.build(check_copy(text))

    assert vocabulary.numbers - vocabulary.base.numbers == frozenset()


@pytest.mark.parametrize(
    "text",
    ["⋅ 1 200 978 posts", "± 1 200 978 posts", "- 131 978 000 posts", "⊗ 1 200 978 posts"],
)
def test_space_thousands_with_an_unclean_head_drop_the_whole_run(text: str) -> None:
    # 第一段沒過白名單時整串都不用，也不從第二段重新開始（修正時自己找到的：否則
    # `⋅ 1 200 978` 會多認 200978，白名單反而多放行）。
    vocabulary = ExtendedVocabulary.build(check_copy(text))

    assert vocabulary.numbers - vocabulary.base.numbers == frozenset()


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # T2：既有讀法認不出整段的科學記號（`_SCIENTIFIC_PIECE` 與白名單）。
        ("The effect is 1.2 ⋅ 10-3% points.", "提高 10%"),
        ("The effect is 1.2 ∙ 10-3% points.", "提高 10%"),
        ("The effect is 1.2 E -3% points.", "提高 3 個百分點"),
        ("The effect is 1.2 E - 3% points.", "提高 3 個百分點"),
        ("The effect is 10^ ±3% points.", "提高 3 個百分點"),
        ("The rate was 7.7 ✕ 10-9%.", "比例為 10%"),
        ("The value was 10**-9 300s.", "數值 9300"),
        ("The value was 7.7e - 90 000.", "數值 90000"),
        # 沒列在任何清單裡的符號：白名單預設不放行。
        ("The effect is 1.2 ⊗ 10-3% points.", "提高 10%"),
        ("The effect is 1.2 ⨂ 10-3% points.", "提高 10%"),
        ("The value was 7.7 ⊗ 10 - 90 000.", "數值 90000"),
        # 年代的後兩位也要前面乾淨（第三輪確認年代規則時找到）。
        ("The value was 1.2 ⊗ 10-1960s.", "追溯 60 年代"),
        ("The value was 1.2 ⊗ 10 - 1960s.", "追溯 60 年代"),
    ],
)
def test_unrecognized_scientific_writings_add_no_new_numbers(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # T2：白名單之後，TASK 的正向樣式照樣過。
        ("We raised $150 000 in total.", "募得 150000 元"),
        ("Adoption was about 10–40%.", "採用率約 10%"),
        ("Adoption (10-40%) varied.", "採用率 10%"),
        ("Adoption rose by 10% points.", "提高 10 個百分點"),
        ("Adoption rose, 4.5% points in total.", "提高 4.5 個百分點"),
        # 摘要第一個字就是數字：輸入是「標題\n摘要」，換行算文字開頭。
        ("131 978 firms were sampled.", "抽樣 131978 家"),
        ("Firms (131 978 in total) were sampled.", "抽樣 131978 家"),
        ("We sampled ≈131 978 firms.", "抽樣 131978 家"),
        ("We sampled ~ 131 978 firms.", "抽樣 131978 家"),
        ("We sampled firms: 131 978.", "抽樣 131978 家"),
        # `= - 4.5`：既有讀法把負號挪到數字旁邊，白名單從原本的 `-` 往前看。
        ("The change was β = - 4.5% points.", "下降 4.5 個百分點"),
        # 年代的後兩位：前面是詞、連字號接在字母或乾淨的數字後面。
        ("Firms founded 1950-1960s grew.", "60 年代"),
        ("Firms founded pre-1960s grew.", "60 年代"),
        ("Firms founded (1960s) grew.", "60 年代"),
    ],
)
def test_the_start_whitelist_keeps_ordinary_writings(abstract: str, method: str) -> None:
    assert _method_only(method, abstract) == method


@pytest.mark.parametrize(
    ("abstract", "method"),
    [
        # 白名單偏保守的誤擋（ADR 有列）：`±`、破折號後面的數字不當新規則的起點。
        ("Adoption ranged ±10-40%.", "採用率 10%"),
        ("Firms - 131 978 in total - were sampled.", "抽樣 131978 家"),
        ("Firms founded 1950s – 1960s grew.", "60 年代"),
    ],
)
def test_the_start_whitelist_is_conservative(abstract: str, method: str) -> None:
    assert _method_rejected(method, abstract) is UNSUPPORTED


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


# --- 新加的正規式：20 萬字元的最壞情況在 3 秒內跑完 ---------------------------------------------

# 長輸入測試的時間上限（第三輪審查 T7 從 1 秒放寬）：本機最慢約 0.45 秒，CI 的機器慢 2～3 倍，1 秒會
# 偶發失敗；平方級的寫法在 20 萬字元要幾十秒以上，3 秒仍抓得到。
LONG_INPUT_SECONDS = 3


@pytest.mark.parametrize(
    "unit",
    [
        # _SPACE_GROUPED（含 _WHOLE_NUMBER_START）、_NOT_DIGIT
        "1 000 ",
        "1 0000 ",
        "1, 000, ",
        "1$\\,$000",
        "1 23 ",
        "1e+3 100 ",
        "1e-1 ",
        "1 000e3 ",
        "1 000e",
        "-1 000 ",
        # _PERCENT_DASH、_PERCENT_RANGE、_PERCENT_POINTS（含 _SIGNED_WHOLE_NUMBER）
        "%-1",
        "1.1-1.1 ",
        "1-1%",
        "1% ",
        "1% point ",
        "-1-1%",
        "e-1-1%",
        "-1% points ",
        "1e-1% point ",
        "--1",
        # _DECADE、月份（_MAY_THEN_NUMBER、_MONTH_ABBREVIATIONS）
        "1960s ",
        "'0s",
        "1950s-60s ",
        "e-10s ",
        "May 1 ",
        "May ",
        "MAR ",
        # _MONTH_CONTEXT、_DECADE_CONTEXT 另有 test_context_patterns_run_in_linear_time
        "1-1 ",
        "1 月",
        "1、",
        "1960-",
        "60 年代",
        # _ALNUM_PIECE、_OLD_READING_TOKEN、_AFTER_ALNUM_NUMBER
        "a1.",
        "a1.1x",
        "B2B ",
        "a-1 ",
        "1.1.1",
        "1e1",
        "1a",
        "K12 萬 ",
        "K12%",
        "K12,0",
        "a 1 b 2 ",
        # 第二輪：_whole_numbers（_prepared_in_place、_SCIENTIFIC_PIECE）、_space_thousands、
        # _extra_input_numbers、_decade_numbers、_covers_whole_numbers、保險（_keeps_whole_numbers）
        "1 000 000 ",
        "1 000 0 ",
        "1%-1%",
        "1-1%-",
        "1 - 1% points ",
        "1960s-",
        "pre-1960s ",
        "K1e-3 ",
        "K1 ",
        "1.2 × 10-10s ",
        "K10^ -1 ",
        "x 10-1 ",
        "x10^",
        "^ - 1",
        "= - 1 ",
        "max 10-1%",
        # 第三輪：_SCIENTIFIC_PIECE 的新寫法、白名單（_clean_start、_START_PUNCTUATION）、
        # _number_continues（逗號列舉）、_decade_tail_clean、_keeps_whole_numbers
        "1.2 ⋅ 10-1% points ",
        "x 10**-1 ",
        "10**",
        "1 E -1 ",
        "1 e - 1 ",
        "1e ±1 ",
        "1E- 1",
        "^ ±1 ",
        "± 1-1%",
        "- 1 000 ",
        "⊗ 1% points ",
        "= - 1% points ",
        "1 000, 1 ",
        "1 000, 1",
        "10-1960s ",
        "1 - 1960s ",
        # 詞形變化、黏字
        "abcdefgation ",
        "a" * 7 + " ",
        # _english_word_count（_PARENTHESIS）
        "研(",
        "研 (a",
        "研(a)",
        "(",
        ")",
        "研(研(a))",
    ],
)
def test_new_patterns_run_in_linear_time(unit: str) -> None:
    text = check_copy(unit * (200_000 // len(unit)))
    started = time.perf_counter()

    vocabulary = ExtendedVocabulary.build(text)
    vocabulary.number_text(text)
    vocabulary.knows_token("abcdefghij-configurational")
    _english_word_count(text)

    assert time.perf_counter() - started < LONG_INPUT_SECONDS


# 有月份、年代的輸入（審查 B-2）：詞彙裡有月份與年代，`_MONTH_CONTEXT`、`_DECADE_CONTEXT` 才真的
# 會跑。
CONTEXT_INPUT = "From January to December in the 1960s and 1990s."


@pytest.mark.parametrize(
    "unit",
    [
        "1 月",
        "1月",
        "1-1 ",
        "1 - 1 月",
        "1-",
        "1、",
        "1 至 12 月",
        "12-",
        "1e-1 月",
        "-1 月",
        "60 年代",
        "1960-",
        "1960 - 1990 年代",
        "60至",
        "3e-60 年代",
        "Q1 月",
        # 第二輪：_exact_integer、新的 _CONTEXT_START（±、∓）
        "±1 月",
        "7 -8月",
        "1.1 月",
        "K1 x 10-1 月",
        "1960s 年代",
        # 第三輪：_signed_context（S1）、_CONTEXT_START 加回數字與 `.`（T4）
        "± 1 月",
        "月 - 1 月",
        "- 1 月",
        "代 - 60 年代",
        "1.1 至 1 月",
        "19 至 1 月",
    ],
)
def test_context_patterns_run_in_linear_time(unit: str) -> None:
    vocabulary = ExtendedVocabulary.build(check_copy(CONTEXT_INPUT))
    assert vocabulary.months
    assert vocabulary.decades
    text = check_copy(unit * (200_000 // len(unit)))
    started = time.perf_counter()

    vocabulary.number_text(text)

    assert time.perf_counter() - started < LONG_INPUT_SECONDS


def test_glued_negation_check_is_quick() -> None:
    # 最壞情況：每個輸出詞在輸入裡的命中全是「否定字首＋輸出詞（＋複數）」，一個都不算，要找完整份
    # 輸入；輸入約 20 萬字元。
    letters = "abcdefghijklmnopqrstuvwxyz"
    codes = [
        "".join(letters[int(digit)] for digit in f"{index:04d}") + "tion" for index in range(800)
    ]
    prefixes = ("un", "in", "im", "ir", "il", "dis", "non", "mis")
    words = " ".join(
        f"{prefix}{code}{plural}" for code in codes for prefix in prefixes for plural in ("", "s")
    )
    text = check_copy(words + " " + "u" + "x" * 50_000)
    assert 150_000 < len(text) < 300_000
    vocabulary = ExtendedVocabulary.build(text)
    started = time.perf_counter()

    for code in codes[:200]:
        assert not vocabulary.knows_word(code)

    assert time.perf_counter() - started < LONG_INPUT_SECONDS


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

    assert time.perf_counter() - started < LONG_INPUT_SECONDS


def test_long_adversarial_output_is_validated_quickly() -> None:
    loose = ZhSummaryRules(
        max_chars_per_field=10**6, translation_min_abstract_sentences=7, min_cjk_percent=1
    )
    text = "研究（B2B 1 000 9 月 60 年代 theoretical）" * 4_000
    abstract = text + " From September in the 1960s."
    output = _output(research_question=text, method=None, theory=None, findings=None)
    started = time.perf_counter()

    with contextlib.suppress(OutputRejectedError):
        validate_zh_summary(output, _input(abstract=abstract), loose)

    assert time.perf_counter() - started < LONG_INPUT_SECONDS
