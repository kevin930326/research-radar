"""中文重點：通用縮寫的允許清單（`config/zh_summary_acronyms.yaml`，ADR-0032 的「通用縮寫的允許清單
與整欄說明存成空值」更新）。

放行要兩個條件都成立：輸出寫的縮寫和登記的一模一樣，而且輸入有它登記的全名。每一項放行都有「仍然
擋得住編造」的反例；設定檔的每種錯誤都在讀取時擋下。標題、摘要、輸出全是測試自己編的。
"""

import contextlib
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
    }


def test_real_list_documents_how_to_add_an_entry() -> None:
    assert "ADR-0032" in REAL_ACRONYMS
    assert "version" in REAL_ACRONYMS
    assert "輸入指紋" in REAL_ACRONYMS


@pytest.mark.parametrize(("acronym", "full_name"), ENTRIES)
def test_every_registered_full_name_allows_its_acronym(acronym: str, full_name: str) -> None:
    abstract = f"This study applies {full_name} to firm data."

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
        # 複數與詞形變化。
        ("We estimate vector autoregressive models.", "VAR"),
        ("We use decision-making trials and evaluation laboratories.", "DEMATEL"),
        ("We report areas under the curves.", "AUC"),
        ("We use self-determined theory.", "SDT"),  # 詞形規則的已知寬度（決定紀錄）
        # 兩種意思都登記：輸入有其中一個就放行。
        ("We draw on signal detection theory.", "SDT"),
        ("We draw on self-determination theory.", "SDT"),
        # 在標題裡也算。
        ("Explainable artificial intelligence in banking.", "XAI"),
    ],
)
def test_full_name_variants_allow_the_acronym(abstract: str, acronym: str) -> None:
    assert _method(_uses(acronym), abstract) == _uses(acronym)


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
        # 全名的詞沒有連續出現（中間隔了逗號、括號、別的詞、換行）。
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
            "version: 1\nacronyms:\n  - acronym: SEM\n    full_names: [a b]\n    note: x\n",
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
    text = f"version: 1\nacronyms:\n  - acronym: {acronym}\n    full_names: [a b]\n"

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


@pytest.mark.parametrize("pattern", ["acronym", "full_names"])
def test_config_patterns_run_in_linear_time(pattern: str) -> None:
    long_name = "ab " * 66_666 + "a!"
    long_acronym = "A1" * 100_000 + "!"
    started = time.perf_counter()

    with contextlib.suppress(ValueError):
        if pattern == "acronym":
            AcronymEntry(acronym=long_acronym, full_names=("a b",))
        else:
            AcronymEntry(acronym="AB", full_names=(long_name,))

    assert time.perf_counter() - started < LONG_INPUT_SECONDS


def test_long_output_with_acronyms_is_validated_quickly() -> None:
    abstract = "We apply decision making trial and evaluation laboratory. " * 3_000
    loose = REAL_RULES.model_copy(update={"max_chars_per_field": 10**6, "min_cjk_percent": 1})
    output = _output(
        research_question="研究" + "（DEMATEL）、（TOPSIS）" * 10_000,
        method=None,
        theory=None,
        findings=None,
    )
    started = time.perf_counter()

    with contextlib.suppress(OutputRejectedError):
        validate_zh_summary(output, _input(title="Field notes", abstract=abstract), loose)

    assert time.perf_counter() - started < LONG_INPUT_SECONDS
