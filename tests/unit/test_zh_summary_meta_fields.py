"""中文重點：整欄只有說明的存成空值（`is_placeholder` 的 `_WHOLE_FIELD_META`，ADR-0032 的
「通用縮寫的允許清單與整欄說明存成空值」更新）。

整欄只由「講摘要沒寫什麼」的子句組成時存 NULL，其他欄位照常驗證；前面有內容、後面多一句說明的維持
拒收；帶了詞表外實質內容的不算整欄。標題、摘要、輸出全是測試自己編的。
"""

import time
import uuid

import pytest

from radar.domain.ai_outputs import (
    InputKind,
    NewAiResult,
    OutputLang,
    OutputRejectedError,
    ResultStatus,
    ZhSummaryContent,
)
from radar.domain.errors import ErrorCode
from radar.domain.validators.zh_summary import (
    is_placeholder,
    normalize_zh_summary,
    validate_zh_summary,
)
from tests.unit.test_zh_summary_input_reading import LONG_INPUT_SECONDS
from tests.unit.test_zh_summary_validator import GOOD, RULES, _input, _output, _rejected, _validate

PLACEHOLDER = ErrorCode.ZH_SUMMARY_PLACEHOLDER_TEXT

# brief 指定一定要存 NULL 的四種寫法。
REQUIRED = [
    "摘要未明確說明具體研究方法與資料來源",
    "未於摘要中說明具體資料來源與分析方法",
    "摘要未明確提出理論或理論框架，填 null",
    "未明確說明（摘要僅提出解釋框架，未描述實證資料或分析方法）",
]
# 同一類的其他寫法（原本被拒收或當成內容存，現在存 NULL；決定紀錄逐條列）。
MORE = [
    "（摘要未說明研究方法）",
    "摘要中並未明確說明所使用的研究方法、資料來源及樣本數。",
    "摘要未說明研究方法；故填 null。",
    "理論：摘要未明確提出理論",
    "未明確說明研究方法與資料來源",
    "未說明具體方法",
    "文中未具體說明理論框架與研究假設",
    "未提及具體的研究設計，也未說明樣本",
    "在摘要中並未清楚地描述其研究問題",
    "研究未說明的變數",
    "摘要沒有提到相關理論，而僅提及分析框架",
]


@pytest.mark.parametrize("value", REQUIRED + MORE)
@pytest.mark.parametrize("field", list(GOOD))
def test_a_field_that_only_explains_what_the_abstract_lacks_is_null(field: str, value: str) -> None:
    content = _validate(_output(**{field: value}))

    assert getattr(content, field) is None
    # 其他欄位照常存。
    others = {name: text for name, text in GOOD.items() if name != field}
    assert {name: getattr(content, name) for name in others} == others


@pytest.mark.parametrize("value", REQUIRED + MORE)
def test_normalization_before_writing_uses_the_same_rule(value: str) -> None:
    # `services/ai_results.py` 寫入前用 `normalize_zh_summary`，和驗證是同一個函式。
    assert is_placeholder(value)
    assert normalize_zh_summary(_output(theory=value)).theory is None


def test_other_fields_are_still_validated() -> None:
    output = _output(theory=REQUIRED[0], method="問卷調查 999 名員工")

    assert _rejected(output) is ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT


# --- 必備反例 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        # 真正的研究發現，不是說明。
        "受訪者未提及隱私疑慮",
        "受訪者未提及隱私疑慮，認知易用性沒有顯著效果。",
        # 既有測試裡的研究結果寫法。
        "原文未揭露姓名時仍可推得作者身分",
        "本文探討使用者為何不揭露個人資訊",
        "作者未揭露身分的評論更受信任",
        "姓名未在原文中出現時仍可推得作者身分",
        # 前半像說明、後半有內容：照既有規則當內容（不會變成 NULL）。
        "未說明研究方法，但採用了深度訪談",
        # 主詞是本文、本研究…時，「提出、提供、呈現」可能是在講研究本身。
        "作者未提出假設",
        "本研究未提出理論",
        "本研究未提供實證資料",
        # 只有「僅提出…」、沒有否定子句。
        "摘要僅提出解釋框架",
    ],
)
def test_real_content_is_kept(value: str) -> None:
    abstract = "We study de-identification and information disclosure in online reviews."
    output = _output(research_question=None, method=None, theory=None, findings=value)

    content = _validate(output, _input(title="Disclosure", abstract=abstract))

    assert not is_placeholder(value)
    assert content.findings == value


@pytest.mark.parametrize(
    "value",
    [
        # 帶了詞表外的實質資訊。
        "摘要未明確指出理論框架，僅提及『禮物經濟』的概念",
        # 前面有內容、後面多一句說明：維持拒收，不刪句子（Kevin 2026-11-18 決定 3）。
        "以問卷調查分析（摘要未說明樣本數）",
        "以某國資料分析外送平台對就業的影響（摘要未說明具體計量方法）",
        "問卷調查，摘要未明確說明具體研究方法與資料來源",
        "摘要未明確說明具體研究方法與資料來源，但以問卷調查",
        # 主詞是本研究、但動詞是說明類的整欄說明夾在內容後面。
        "科技接受模型，本研究未說明所用理論框架",
    ],
)
def test_explanations_next_to_real_content_are_still_rejected(value: str) -> None:
    assert not is_placeholder(value)
    assert _rejected(_output(method=value)) is PLACEHOLDER


@pytest.mark.parametrize(
    "value",
    [
        "摘要未提及",
        "未提及",
        "摘要未提及理論",
        "摘要中沒有提到相關理論。",
        "本研究未提及理論",
        "作者未說明使用的理論",
        "文章沒有提到理論框架",
        "理論：未提及",
        "未提供相關資訊",
        "N/A",
        "不詳",
    ],
)
def test_placeholders_that_were_null_stay_null(value: str) -> None:
    assert _validate(_output(theory=value)).theory is None


# --- 四欄全部變成 NULL ---------------------------------------------------------------------------


def test_four_explanation_fields_become_one_valid_empty_result() -> None:
    # 驗證通過、四欄都是 None（不是拒收）；寫入時照既有行為寫成一筆 active、四欄全空的列
    # （`NewAiResult` 收得下），論文頁的 AI 區塊是 empty（`repo/ai_blocks._zh_summary_block`），
    # 四欄都顯示「摘要未提及」。
    output = _output(
        research_question="摘要未明確說明研究問題",
        method=REQUIRED[1],
        theory=REQUIRED[2],
        findings="摘要未具體說明研究發現與結果",
    )

    content = validate_zh_summary(output, _input(), RULES)

    assert content == ZhSummaryContent(None, None, None, None)
    assert normalize_zh_summary(output) == content
    result = NewAiResult(
        paper_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        input_kind=InputKind.ABSTRACT,
        input_sha256="a" * 64,
        abstract_id=uuid.uuid4(),
        lang=OutputLang.ZH_HANT,
        status=ResultStatus.ACTIVE,
        reject_reason=None,
        zh_summary=content,
    )
    assert result.zh_summary == content


# --- 長輸入：線性時間 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unit",
    [
        # _WHOLE_FIELD_META：整段都對得上、最後一個字對不上；每個子句都只差一點。
        "未說明理論",
        "摘要未說明理論與",
        "摘要僅提出理論",
        "摘要中並未明確",
        "未明確具體清楚",
        "理論或",
        "本研究未提出",
        "在摘要中未",
        "未說明具體的所使用的",
        "填null",
        "但",
        "摘要",
    ],
)
def test_whole_field_explanations_are_checked_in_linear_time(unit: str) -> None:
    text = unit * (200_000 // len(unit)) + "X"
    started = time.perf_counter()

    assert not is_placeholder(text)

    assert time.perf_counter() - started < LONG_INPUT_SECONDS


def test_a_long_field_of_explanations_is_validated_quickly() -> None:
    loose = RULES.model_copy(update={"max_chars_per_field": 10**6, "min_cjk_percent": 1})
    output = _output(theory="摘要未說明理論與研究方法，" * 15_000)
    started = time.perf_counter()

    try:
        content = validate_zh_summary(output, _input(), loose)
    except OutputRejectedError:  # pragma: no cover - 只量時間
        content = None

    assert content is not None
    assert content.theory is None
    assert time.perf_counter() - started < LONG_INPUT_SECONDS
