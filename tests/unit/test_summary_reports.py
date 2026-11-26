"""回報中文重點錯誤的規則（Story 3.12、`domain/summary_reports`）。

- 四欄選一欄：不是四欄之一（含大小寫不同、不是字串）不收。
- 說明必填：沒寫、只有空白不收；去掉前後空白；最多 100 個 code point（表情符號算一個）；控制字元、
  U+2028、U+2029、落單的代理字元、格式字元（Cf）不收；只有零寬連接 U+200D 放行（組合 emoji）。
- 只有 `ready` 的中文重點可以回報（`reportable_summary`）。
- 錯誤只帶原因代號，不帶說明。
資料全是測試自己編的。
"""

import uuid
from datetime import UTC, datetime

import pytest

from radar.domain.ai_blocks import AiBlock, AiBlockState, AiProduct, Provenance, ZhSummaryValue
from radar.domain.ai_outputs import InputKind
from radar.domain.judgments import SummaryField
from radar.domain.summary_reports import (
    NOTE_MAX_LENGTH,
    SummaryReport,
    SummaryReportEvent,
    SummaryReportInvalidError,
    SummaryReportProblem,
    check_summary_report,
    reportable_summary,
    summary_report_lock_key,
)

SUMMARY_ID = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
PROVENANCE = Provenance(
    model="claude-haiku-4-5",
    generated_at=datetime(2026, 11, 1, tzinfo=UTC),
    input_kind=InputKind.ABSTRACT,
    human_verified=False,
)
VALUE = ZhSummaryValue(
    zh_summary_id=SUMMARY_ID,
    research_question="假的研究問題",
    method=None,
    theory="假的理論",
    findings="假的發現",
    reported_fields=(),
)


def _problem(**kw: object) -> SummaryReportProblem:
    with pytest.raises(SummaryReportInvalidError) as caught:
        check_summary_report(**{"field": "findings", "note": "說明", **kw})
    return caught.value.problem


@pytest.mark.parametrize("field", list(SummaryField))
def test_each_of_the_four_fields_is_accepted(field: SummaryField) -> None:
    assert check_summary_report(field=field.value, note="摘要沒有寫") == SummaryReport(
        field=field, note="摘要沒有寫"
    )


@pytest.mark.parametrize("field", ["Findings", "abstract", "", None, 1, ["findings"]])
def test_anything_but_the_four_fields_is_rejected(field: object) -> None:
    assert _problem(field=field) is SummaryReportProblem.FIELD_INVALID


@pytest.mark.parametrize("note", [None, "", "   ", "　"])
def test_the_note_is_required(note: object) -> None:
    assert _problem(note=note) is SummaryReportProblem.NOTE_MISSING


def test_the_note_is_trimmed() -> None:
    assert check_summary_report(field="method", note="  摘要沒有寫「明顯」 ").note == (
        "摘要沒有寫「明顯」"
    )


@pytest.mark.parametrize("note", ["長" * 100, "😀" * 100, " " + "長" * 100 + " "])
def test_one_hundred_code_points_are_accepted(note: str) -> None:
    assert len(check_summary_report(field="theory", note=note).note) == NOTE_MAX_LENGTH


@pytest.mark.parametrize("note", ["長" * 101, "😀" * 101])
def test_more_than_one_hundred_code_points_are_rejected(note: str) -> None:
    assert _problem(note=note) is SummaryReportProblem.NOTE_TOO_LONG


@pytest.mark.parametrize(
    "note", ["第一行\n第二行", "\t說明", "說明\x00", "句中 換行", "句中 換段", "\ud800"]
)
def test_control_characters_and_separators_are_rejected(note: str) -> None:
    assert _problem(note=note) is SummaryReportProblem.NOTE_INVALID_CHARACTER


@pytest.mark.parametrize("note", [3, ["說明"], b"bytes"])
def test_a_note_that_is_not_text_is_rejected(note: object) -> None:
    assert _problem(note=note) is SummaryReportProblem.NOTE_NOT_TEXT


def test_the_field_is_checked_before_the_note() -> None:
    assert _problem(field="nope", note="長" * 101) is SummaryReportProblem.FIELD_INVALID


def test_errors_never_carry_the_note() -> None:
    with pytest.raises(SummaryReportInvalidError) as caught:
        check_summary_report(field="findings", note="秘密說明" * 30)
    assert "秘密說明" not in str(caught.value)


def test_only_a_ready_summary_can_be_reported() -> None:
    ready = AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.READY, VALUE, PROVENANCE)

    assert reportable_summary(ready) == VALUE


@pytest.mark.parametrize(
    "block",
    [
        AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.EMPTY, provenance=PROVENANCE),
        AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.FAILED),
        AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.PENDING_BATCH),
        AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.PENDING_INTERACTIVE),
        AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.DEFERRED_BUDGET),
        AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.NO_INPUT),
        AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.NOT_APPLICABLE),
    ],
)
def test_a_summary_that_is_not_ready_cannot_be_reported(block: AiBlock) -> None:
    with pytest.raises(SummaryReportInvalidError) as caught:
        reportable_summary(block)
    assert caught.value.problem is SummaryReportProblem.NO_SUMMARY


def test_reportable_summary_needs_a_summary_block() -> None:
    with pytest.raises(ValueError, match="中文重點"):
        reportable_summary(AiBlock(AiProduct.CLASSIFICATION, AiBlockState.FAILED))


def test_the_lock_key_is_per_paper_and_apart_from_other_locks() -> None:
    paper = uuid.UUID(int=1)
    assert summary_report_lock_key(paper) == summary_report_lock_key(uuid.UUID(int=1))
    assert summary_report_lock_key(paper) != summary_report_lock_key(uuid.UUID(int=2))
    from radar.domain.classification_fixes import classification_fix_lock_key

    assert summary_report_lock_key(paper) != classification_fix_lock_key(paper)


def test_a_report_event_needs_an_aware_time() -> None:
    with pytest.raises(ValueError, match="時區"):
        SummaryReportEvent(
            report_id=uuid.UUID(int=1),
            zh_summary_id=SUMMARY_ID,
            field=SummaryField.METHOD,
            member_name="測試成員甲",
            reported_at=datetime(2026, 11, 1),
            note="說明",
        )


# --- 格式字元（Cf） ----------------------------------------------------------------------

FORMAT_CHARACTERS = [
    "\u200b",  # 零寬空白
    "\ufeff",  # BOM
    "\u00ad",  # 軟連字號
    "\u200e",  # 由左至右標記
    "\u200f",  # 由右至左標記
    *[chr(code) for code in range(0x202A, 0x202F)],  # 雙向嵌入與覆寫 U+202A～U+202E
    *[chr(code) for code in range(0x2066, 0x206A)],  # 雙向隔離 U+2066～U+2069
    "\U000e0041",  # 標籤字元
]


def test_the_format_character_list_is_not_empty() -> None:
    assert len(FORMAT_CHARACTERS) == 15


@pytest.mark.parametrize("char", FORMAT_CHARACTERS)
def test_format_characters_are_rejected_anywhere(char: str) -> None:
    for note in (char + "說明", "說" + char + "明", "說明" + char):
        assert _problem(note=note) is SummaryReportProblem.NOTE_INVALID_CHARACTER


def test_the_zero_width_joiner_is_kept_for_emoji() -> None:
    note = "摘要寫錯了👩\u200d💻"

    assert check_summary_report(field="findings", note=note).note == note


def test_the_zero_width_joiner_counts_as_one_code_point() -> None:
    assert _problem(note="長" * 100 + "\u200d") is SummaryReportProblem.NOTE_TOO_LONG
    assert len(check_summary_report(field="theory", note="長" * 99 + "\u200d").note) == 100
