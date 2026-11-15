"""哪一篇論文做哪些處理（AD-30、ADR-0015「哪一種論文做哪些處理」、ADR-0032）。

只由 `plan_for(事實)` 回答；匯入、上傳、每週更新、指定論文試跑、合併建議都問它，不自己判斷。
表只看三件事（`ProcessingFacts`）：是不是被排除的非研究文章、是不是手填書目、有沒有目前的摘要。
**不看**是不是在追蹤範圍內、有沒有進快照。由上往下，先符合的那一列算：

| 論文的種類 | 中文重點 | 分類 | 研究模型 | 向量 |
|---|---|---|---|---|
| 被排除的非研究文章 | 不做 | 不做 | 不做 | 不做 |
| 手填書目 | 不做 | 不做 | 不做 | 只有標題 |
| 其餘，有目前的摘要 | 做 | 做 | 做 | 標題＋摘要 |
| 其餘，沒有摘要 | 不做 | 做（只根據標題） | 不做 | 只有標題 |

不做的工作，AI 區塊是 `not_applicable`（前兩列）或 `no_input`（最後一列）。「有摘要」＝目前的摘要
去掉空白後不是空的（只有空白的摘要當作沒有，和 `build_llm_input` 的判斷一致）。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from radar.domain.ai_outputs import InputKind
from radar.domain.papers import PaperOrigin


class PaperKind(StrEnum):
    """`plan_for` 判定的論文種類（AD-30 的四列）。"""

    EXCLUDED = "excluded"  # 被排除的非研究文章
    MANUAL = "manual"  # 手填書目
    WITH_ABSTRACT = "with_abstract"  # 其餘，有目前的摘要
    NO_ABSTRACT = "no_abstract"  # 其餘，沒有摘要


class ProcessingTask(StrEnum):
    """`plan_for` 回答的四種處理。前三種走 LLM 閘門（值和 `LlmTask` 相同）；`embed` 是向量化
    （不經閘門，AD-12）。"""

    ZH_SUMMARY = "zh_summary"
    CLASSIFY = "classify"
    EXTRACT_MODEL = "extract_model"
    EMBED = "embed"


class SkipState(StrEnum):
    """不做的工作在 AI 區塊上的狀態（ADR-0015 的信封）。"""

    NOT_APPLICABLE = "not_applicable"  # 這種論文不做這項工作
    NO_INPUT = "no_input"  # 這項工作需要摘要，而這篇沒有


@dataclass(frozen=True, slots=True)
class ProcessingFacts:
    """`plan_for` 只看的三件事（由 `repo/papers.find_paper_processing_facts` 讀）。

    - `origin`：論文怎麼進資料庫的（`manual`＝手填書目）。
    - `excluded_reason`：被排除的非研究文章才有值。
    - `abstract`：目前的摘要內容（`paper_abstract` 裡 `is_current` 的那一筆）；沒有時是 None。
    """

    origin: PaperOrigin
    excluded_reason: str | None
    abstract: str | None


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """一項工作做不做：`input_kind` 與 `skip` 剛好一個有值。

    - 要做：`input_kind` 是模型（或向量）要看的內容（`title`＝只有標題；`abstract`＝標題＋摘要）。
    - 不做：`skip` 是 AI 區塊要顯示的狀態。

    Raises:
        ValueError: 兩個都有值或都沒有。
    """

    input_kind: InputKind | None
    skip: SkipState | None

    def __post_init__(self) -> None:
        if (self.input_kind is None) == (self.skip is None):
            raise ValueError("input_kind 與 skip 要剛好一個有值")

    @property
    def runs(self) -> bool:
        """這項工作要做。"""
        return self.input_kind is not None


@dataclass(frozen=True, slots=True)
class ProcessingPlan:
    """一篇論文的處理計畫：種類與四項工作各自做不做。"""

    kind: PaperKind
    tasks: Mapping[ProcessingTask, TaskPlan]

    def for_task(self, task: ProcessingTask) -> TaskPlan:
        """這項工作的計畫。

        Raises:
            ValueError: `task` 不是 `ProcessingTask` 的值。
        """
        return self.tasks[ProcessingTask(task)]


def _plan(kind: PaperKind, **tasks: TaskPlan) -> ProcessingPlan:
    plans = {ProcessingTask(name): plan for name, plan in tasks.items()}
    if set(plans) != set(ProcessingTask):
        raise RuntimeError("每一列都要寫出四項工作")
    return ProcessingPlan(kind, MappingProxyType(plans))


_NOT_APPLICABLE: Final = TaskPlan(None, SkipState.NOT_APPLICABLE)
_NO_INPUT: Final = TaskPlan(None, SkipState.NO_INPUT)
_TITLE: Final = TaskPlan(InputKind.TITLE, None)
_ABSTRACT: Final = TaskPlan(InputKind.ABSTRACT, None)

# AD-30 的四列，順序就是判定順序。
_PLANS: Final[Mapping[PaperKind, ProcessingPlan]] = MappingProxyType(
    {
        PaperKind.EXCLUDED: _plan(
            PaperKind.EXCLUDED,
            zh_summary=_NOT_APPLICABLE,
            classify=_NOT_APPLICABLE,
            extract_model=_NOT_APPLICABLE,
            embed=_NOT_APPLICABLE,
        ),
        PaperKind.MANUAL: _plan(
            PaperKind.MANUAL,
            zh_summary=_NOT_APPLICABLE,
            classify=_NOT_APPLICABLE,
            extract_model=_NOT_APPLICABLE,
            embed=_TITLE,
        ),
        PaperKind.WITH_ABSTRACT: _plan(
            PaperKind.WITH_ABSTRACT,
            zh_summary=_ABSTRACT,
            classify=_ABSTRACT,
            extract_model=_ABSTRACT,
            embed=_ABSTRACT,
        ),
        PaperKind.NO_ABSTRACT: _plan(
            PaperKind.NO_ABSTRACT,
            zh_summary=_NO_INPUT,
            classify=_TITLE,
            extract_model=_NO_INPUT,
            embed=_TITLE,
        ),
    }
)


def paper_kind(facts: ProcessingFacts) -> PaperKind:
    """AD-30 的哪一列：由上往下，先符合的那一列算。"""
    if facts.excluded_reason is not None:
        return PaperKind.EXCLUDED
    if PaperOrigin(facts.origin) is PaperOrigin.MANUAL:
        return PaperKind.MANUAL
    if facts.abstract is not None and facts.abstract.strip():
        return PaperKind.WITH_ABSTRACT
    return PaperKind.NO_ABSTRACT


def plan_for(facts: ProcessingFacts) -> ProcessingPlan:
    """這篇論文做哪些處理（見模組說明的表）。"""
    return _PLANS[paper_kind(facts)]
