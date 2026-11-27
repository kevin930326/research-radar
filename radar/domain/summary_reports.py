"""成員回報中文重點錯誤（Story 3.12、FR-14 附則、FR-4、AD-10、ADR-0015、
EXPERIENCE.md 畫面 8 第 3 點、
決定第 97、98 項）。

- **四欄選一欄**：`research_question`、`method`、`theory`、`findings`（`SummaryField`）。
- **說明必填**（`_clean_note`）：一行、去掉前後空白、最多 100 個 code point（前端同一種
  算法）、去掉空白後是空的不收；去掉空白與零寬連接 U+200D 之後沒有字也算沒寫（終端機與畫面上
  看起來是空的，Codex 審查 3.12 中）。不收的字元：控制字元（Cc）、落單的代理字元（Cs）、
  行分隔符 U+2028（Zl）、段落分隔符 U+2029（Zp）、格式字元（Cf：零寬空白 U+200B、
  BOM U+FEFF、雙向覆寫 U+202A～U+202E、U+2066～U+2069 等，看不見卻會改變顯示或比對）
  與存不進資料庫的字。只有零寬連接 U+200D 放行：組合 emoji 要用它，它本身不改變文字方向。
  擋哪些、放行哪些在 `member_text_characters`，和修正分類的理由（Story 3.11 的 `_check_reason`）共用
  同一份（epic-3m 起理由也擋格式字元）。
- **只回報成員看到的、而且現在有效的那一筆中文重點**：中文重點是 `ready`（有內容）才收；成員送來
  畫面上那一筆的 `zh_summary_id`，和鎖裡讀到的有效那一筆不同（送出前重新產生了）就不收
  （`SummaryChangedError`，api 回 409 `summary_changed`，ADR-0064），回報指向那一筆；還在產生、
  沒有產生成功、四欄都沒有（`empty`）、沒有摘要都不收（`NO_SUMMARY`）。回報只對那一筆有效：重新產生的新一筆沒有回報（`effective_zh_summary`，AD-10）。
- 成員送來的值一律當使用者輸入驗（AGENTS.md），前端的檢查不算數。錯誤只帶原因代號
  （`SummaryReportProblem`），不回顯成員寫的說明。
- 同一篇的回報排隊做（`summary_report_lock_key`）：讀目前的中文重點、寫回報在同一把鎖裡。
- 管理者的處理結果（`summary_report_resolution`）只經管理指令寫；這裡定管理指令列出的一筆回報
  （`OpenSummaryReport`）的形狀。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from radar.domain.ai_blocks import AiBlock, AiBlockState, AiProduct, ZhSummaryValue
from radar.domain.fingerprints import named_lock_key
from radar.domain.judgments import SummaryField
from radar.domain.member_text_characters import ZERO_WIDTH_JOINER, is_rejected_character
from radar.domain.papers import check_storable_text

# 說明的上限（EXPERIENCE.md 決定第 98 項：理由與說明上限 100 字）。
NOTE_MAX_LENGTH: Final = 100

# 稽核事件的動作與對象（小寫識別字，寫出去之後不改名）。
REPORT_SUMMARY_ACTION: Final = "report_summary"
RESOLVE_SUMMARY_REPORT_ACTION: Final = "resolve_summary_report"
ZH_SUMMARY_TARGET: Final = "zh_summary"


class SummaryReportProblem(StrEnum):
    """成員送來的回報為什麼不收。"""

    FIELD_INVALID = "field_invalid"  # 不是四欄之一（含不是字串）
    NOTE_MISSING = "note_missing"  # 說明沒寫（或只有空白）
    NOTE_NOT_TEXT = "note_not_text"  # 說明不是字串
    NOTE_TOO_LONG = "note_too_long"  # 說明超過 100 字
    NOTE_INVALID_CHARACTER = "note_invalid_character"  # 控制、分隔、格式字元或存不進資料庫的字
    NO_SUMMARY = "no_summary"  # 這篇現在沒有可以回報的中文重點（不是 ready）


class SummaryReportInvalidError(ValueError):
    """成員送來的回報不收。訊息只有原因代號，不含成員寫的說明。"""

    def __init__(self, problem: SummaryReportProblem) -> None:
        self.problem = problem
        super().__init__(problem.value)


def _clean_note(note: object) -> str:
    """驗說明（見模組說明），回去掉前後空白的字。先看字元再去空白（頭尾的控制字元也不收）。

    Raises:
        SummaryReportInvalidError: 不收的原因。
    """
    if note is None:
        raise SummaryReportInvalidError(SummaryReportProblem.NOTE_MISSING)
    if not isinstance(note, str):
        raise SummaryReportInvalidError(SummaryReportProblem.NOTE_NOT_TEXT)
    if any(is_rejected_character(char) for char in note):
        raise SummaryReportInvalidError(SummaryReportProblem.NOTE_INVALID_CHARACTER)
    try:
        check_storable_text(note, "說明")
    except ValueError:
        raise SummaryReportInvalidError(SummaryReportProblem.NOTE_INVALID_CHARACTER) from None
    text = note.strip()
    # 只有空白與零寬連接（U+200D 本身看不見）也算沒寫。
    if not text.replace(ZERO_WIDTH_JOINER, "").strip():
        raise SummaryReportInvalidError(SummaryReportProblem.NOTE_MISSING)
    if len(text) > NOTE_MAX_LENGTH:
        raise SummaryReportInvalidError(SummaryReportProblem.NOTE_TOO_LONG)
    return text


@dataclass(frozen=True, slots=True)
class SummaryReport:
    """驗過的一次回報：哪一欄、說明（去掉前後空白，一定有字）。"""

    field: SummaryField
    note: str


def check_summary_report(*, field: object, note: object) -> SummaryReport:
    """驗成員送來的一次回報（見模組說明）。

    Raises:
        SummaryReportInvalidError: 不收的原因（先驗欄位、再驗說明）。
    """
    if not isinstance(field, str) or field not in set(SummaryField):
        raise SummaryReportInvalidError(SummaryReportProblem.FIELD_INVALID)
    return SummaryReport(field=SummaryField(field), note=_clean_note(note))


class SummaryChangedError(Exception):
    """成員看到的那一筆中文重點已經不是現在有效的那一筆（送出前重新產生了）。訊息不帶任何 ID。"""

    def __init__(self) -> None:
        super().__init__("中文重點已經換了一筆")


def reportable_summary(block: AiBlock, seen_zh_summary_id: uuid.UUID) -> ZhSummaryValue:
    """中文重點區塊 → 可以回報的那一筆（`ready` 才有，而且要是成員看到的 `seen_zh_summary_id`）。

    Raises:
        ValueError: 區塊不是中文重點。
        SummaryReportInvalidError: 不是 `ready`（`NO_SUMMARY`：還在產生、沒有產生成功、四欄都沒有、
            沒有摘要等）。
        SummaryChangedError: 現在有效的那一筆不是成員看到的那一筆。
    """
    if block.product is not AiProduct.ZH_SUMMARY:
        raise ValueError("要是中文重點區塊")
    value = block.value
    if block.state is not AiBlockState.READY or not isinstance(value, ZhSummaryValue):
        raise SummaryReportInvalidError(SummaryReportProblem.NO_SUMMARY)
    if value.zh_summary_id != seen_zh_summary_id:
        raise SummaryChangedError
    return value


def summary_report_lock_key(paper_id: uuid.UUID) -> int:
    """一篇論文的回報中文重點錯誤的交易級 advisory lock 鍵：同一篇的回報排隊做（讀目前的中文重點、
    寫回報在同一把鎖裡）。名稱帶 `summary_report:` 前綴，和其他鎖分在不同的命名空間。"""
    return named_lock_key(f"summary_report:{uuid.UUID(str(paper_id))}")


def _check_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 要是帶時區的時間")


@dataclass(frozen=True, slots=True)
class SummaryReportEvent:
    """`judgment_event` 的一筆回報（`repo/judgments.list_open_summary_reports` 讀的）：
    只給管理指令。"""

    report_id: uuid.UUID
    zh_summary_id: uuid.UUID
    field: SummaryField
    member_name: str
    reported_at: datetime
    note: str

    def __post_init__(self) -> None:
        _check_aware(self.reported_at, "reported_at")
        SummaryField(self.field)


@dataclass(frozen=True, slots=True)
class OpenSummaryReport:
    """管理指令列出的一筆還沒處理的回報：回報本身、哪一篇論文（標題）、被回報的那一筆中文重點是不是
    現在有效的那一筆（`is_current`；重新產生或摘要換了之後是 False，論文頁已經不顯示標記）。"""

    report: SummaryReportEvent
    paper_id: uuid.UUID
    title: str
    is_current: bool
