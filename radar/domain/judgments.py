"""人工判斷事件的形狀（AD-10、ADR-0012、ADR-0015、ADR-0035、Story 2.7）。

人工判斷都寫進同一張 `judgment_event`，只新增、不修改；`kind` 固定四個值（`JudgmentKind`）。
每筆記誰（`member_id`）、當時的角色（`member`／`admin`）、對象（`target_type`, `target_id`）、
欄位、新舊值、理由；時間由資料庫填。各 `kind` 的形狀在建構 `NewJudgmentEvent` 時就擋，資料庫的
CHECK 再擋一次（遷移 0014）：

- `classification_fix`：對象 `paper`；欄位 `topic`／`method`；新值是排序、不重複的 `key`（主題至少
  一個；`unclassified` 只能單獨當主題），舊值同樣寫法或沒有；`ai_result_id`＝被修正的分類表頭
  （可以沒有）。
- `merge_decision`：對象 `construct_pair`，`target_id` ＜ `related_id`；沒有欄位；新值
  `{"decision", "evidence_level"}`，沒有舊值。
- `summary_report`：對象 `zh_summary`；四欄之一；沒有新舊值；理由必填。
- `summary_report_resolution`：對象 `zh_summary`（同回報）；欄位同回報；新值 `"confirmed_ok"`／
  `"regenerated"`，沒有舊值；`report_event_id` 指回報；角色一定是 `admin`。

理由有值時不能只有空白。處理結果「指到同一筆 `zh_summary`、同一欄的回報」要查資料庫，由
`repo/judgments.create_judgment_event` 在寫入時確認（不是就不寫、回 None）。

這裡只定形狀；誰能寫哪一種（權限）與同一個交易寫稽核，是 `services/judgments.py` 的事
（3.11、3.12、Epic 10）。
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from radar.domain.papers import check_storable_text
from radar.domain.taxonomy import KEY_PATTERN, UNCLASSIFIED, TaxonomyField


class JudgmentKind(StrEnum):
    """人工判斷的四種（AD-10）。值寫進 `judgment_event.kind`，寫出去之後不改名。"""

    CLASSIFICATION_FIX = "classification_fix"
    MERGE_DECISION = "merge_decision"
    SUMMARY_REPORT = "summary_report"
    SUMMARY_REPORT_RESOLUTION = "summary_report_resolution"


class ActorRole(StrEnum):
    """做判斷的人當時的角色。"""

    MEMBER = "member"
    ADMIN = "admin"


class JudgmentTarget(StrEnum):
    """事件的對象種類（`judgment_event.target_type`）。"""

    PAPER = "paper"
    CONSTRUCT_PAIR = "construct_pair"
    ZH_SUMMARY = "zh_summary"


class MergeDecisionValue(StrEnum):
    """合併的決定（ADR-0012）。"""

    ACCEPT = "accept"
    REJECT = "reject"


class EvidenceLevel(StrEnum):
    """合併的證據層級（ADR-0012）：只看了名稱、有並排定義、有比對題項。"""

    NAME = "name"
    DEFINITION = "definition"
    ITEMS = "items"


class SummaryResolution(StrEnum):
    """管理者對一筆中文重點回報的處理結果（ADR-0015）。"""

    CONFIRMED_OK = "confirmed_ok"
    REGENERATED = "regenerated"


class SummaryField(StrEnum):
    """中文重點可以被回報的四欄（和 `zh_summary` 的欄位同名，順序固定）。"""

    RESEARCH_QUESTION = "research_question"
    METHOD = "method"
    THEORY = "theory"
    FINDINGS = "findings"


# 新舊值寫進 jsonb 的形狀：分類的 key 陣列、合併決定的物件、處理結果的字串。
type JudgmentValue = tuple[str, ...] | Mapping[str, str] | str

_TARGETS: Final[Mapping[JudgmentKind, JudgmentTarget]] = MappingProxyType(
    {
        JudgmentKind.CLASSIFICATION_FIX: JudgmentTarget.PAPER,
        JudgmentKind.MERGE_DECISION: JudgmentTarget.CONSTRUCT_PAIR,
        JudgmentKind.SUMMARY_REPORT: JudgmentTarget.ZH_SUMMARY,
        JudgmentKind.SUMMARY_REPORT_RESOLUTION: JudgmentTarget.ZH_SUMMARY,
    }
)


def _check_keys(keys: object, field: TaxonomyField, name: str) -> tuple[str, ...]:
    """分類修正的整組 key：字串的 tuple、寫法對、排序過、不重複；主題至少一個，`unclassified`
    只能單獨當主題。"""
    if not isinstance(keys, tuple) or not all(isinstance(key, str) for key in keys):
        raise ValueError(f"{name} 要是字串的 tuple")
    if any(KEY_PATTERN.fullmatch(key) is None for key in keys):
        raise ValueError(f"{name} 的 key 寫法不對")
    if list(keys) != sorted(set(keys)):
        raise ValueError(f"{name} 要排序過、不重複")
    if field is TaxonomyField.TOPIC and not keys:
        raise ValueError(f"{name}：主題至少要有一個（都不屬於時是 {UNCLASSIFIED}）")
    if UNCLASSIFIED in keys and (field is not TaxonomyField.TOPIC or len(keys) > 1):
        raise ValueError(f"{name}：{UNCLASSIFIED} 只能單獨當主題")
    return keys


def _merge_value(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"decision", "evidence_level"}:
        raise ValueError("合併決定的 new_value 要剛好有 decision 與 evidence_level")
    decision, level = value["decision"], value["evidence_level"]
    if not isinstance(decision, str) or decision not in set(MergeDecisionValue):
        raise ValueError("decision 要是 accept 或 reject")
    if not isinstance(level, str) or level not in set(EvidenceLevel):
        raise ValueError("evidence_level 要是 name、definition 或 items")
    return MappingProxyType({"decision": str(decision), "evidence_level": str(level)})


@dataclass(frozen=True, slots=True)
class NewJudgmentEvent:
    """要新增的一筆 `judgment_event`。建議用下面四個建構函式做出來；直接建構也照 `kind` 驗形狀。

    - `related_id`：只有 `merge_decision` 有（一對構面的第二個，ID 比 `target_id` 大）。
    - `ai_result_id`：只有 `classification_fix` 可以有（被修正的那筆分類表頭）。
    - `report_event_id`：只有 `summary_report_resolution` 有（原本那筆回報）。

    Raises:
        ValueError: 形狀和 `kind` 對不上（見模組說明的表）、理由只有空白或存不進資料庫。
    """

    kind: JudgmentKind
    member_id: uuid.UUID
    actor_role: ActorRole
    target_type: JudgmentTarget
    target_id: uuid.UUID
    field: str | None
    old_value: JudgmentValue | None
    new_value: JudgmentValue | None
    reason: str | None
    related_id: uuid.UUID | None = None
    ai_result_id: uuid.UUID | None = None
    report_event_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        kind = JudgmentKind(self.kind)
        for name in ("member_id", "target_id"):
            if not isinstance(getattr(self, name), uuid.UUID):
                raise ValueError(f"{name} 要是 UUID")
        for name in ("related_id", "ai_result_id", "report_event_id"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, uuid.UUID):
                raise ValueError(f"{name} 有值時要是 UUID")
        ActorRole(self.actor_role)
        if JudgmentTarget(self.target_type) is not _TARGETS[kind]:
            raise ValueError(f"{kind.value} 的對象要是 {_TARGETS[kind].value}")
        if self.reason is not None:
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("理由有值時不能只有空白")
            check_storable_text(self.reason, "理由")
        if (kind is JudgmentKind.MERGE_DECISION) != (self.related_id is not None):
            raise ValueError("只有 merge_decision 帶 related_id，而且一定要帶")
        if self.ai_result_id is not None and kind is not JudgmentKind.CLASSIFICATION_FIX:
            raise ValueError("只有 classification_fix 可以帶 ai_result_id")
        if (kind is JudgmentKind.SUMMARY_REPORT_RESOLUTION) != (self.report_event_id is not None):
            raise ValueError("只有 summary_report_resolution 帶 report_event_id，而且一定要帶")
        if self.old_value is not None and kind is not JudgmentKind.CLASSIFICATION_FIX:
            raise ValueError("只有 classification_fix 可以帶 old_value")
        match kind:
            case JudgmentKind.CLASSIFICATION_FIX:
                if self.field not in set(TaxonomyField):
                    raise ValueError("分類修正的欄位要是 topic 或 method")
                field = TaxonomyField(str(self.field))
                _check_keys(self.new_value, field, "new_value")
                if self.old_value is not None:
                    _check_keys(self.old_value, field, "old_value")
            case JudgmentKind.MERGE_DECISION:
                if self.field is not None:
                    raise ValueError("合併決定沒有欄位")
                assert self.related_id is not None
                if not self.target_id < self.related_id:
                    raise ValueError("一對構面要把 ID 小的放在 target_id（ADR-0012）")
                # 存回正規化後的唯讀版本，免得呼叫端留著的可變 dict 在驗證後被改掉。
                object.__setattr__(self, "new_value", _merge_value(self.new_value))
            case JudgmentKind.SUMMARY_REPORT:
                self._check_summary_field()
                if self.new_value is not None:
                    raise ValueError("回報不帶 new_value")
                if self.reason is None:
                    raise ValueError("回報一定要寫理由")
            case JudgmentKind.SUMMARY_REPORT_RESOLUTION:
                self._check_summary_field()
                if ActorRole(self.actor_role) is not ActorRole.ADMIN:
                    raise ValueError("處理結果只有管理者能寫")
                if not isinstance(self.new_value, str) or self.new_value not in set(
                    SummaryResolution
                ):
                    raise ValueError("處理結果的 new_value 要是 confirmed_ok 或 regenerated")

    def _check_summary_field(self) -> None:
        if self.field not in set(SummaryField):
            raise ValueError("中文重點的欄位要是四欄之一")

    def json_values(self) -> tuple[object, object]:
        """（`old_value`, `new_value`）轉成 `json.dumps` 收得下的值（tuple → list、Mapping →
        dict）。"""
        return _to_json(self.old_value), _to_json(self.new_value)


def _to_json(value: JudgmentValue | None) -> object:
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        return dict(value)
    return value


# --- 建構函式 -----------------------------------------------------------------------------


def classification_fix(
    *,
    member_id: uuid.UUID,
    actor_role: ActorRole,
    paper_id: uuid.UUID,
    field: TaxonomyField,
    keys: Sequence[str],
    reason: str | None,
    previous_keys: Sequence[str] | None = None,
    ai_result_id: uuid.UUID | None = None,
) -> NewJudgmentEvent:
    """成員修正一篇論文的主題或方法：`keys` 是新的整組 key（會排序、去重）；`previous_keys` 是
    修正前看到的值（可以不給）；`ai_result_id` 是被修正的那筆分類表頭（可以不給）。

    Raises:
        ValueError: `keys`／`previous_keys` 是單一字串（會被拆成字元），或見 `NewJudgmentEvent`。
    """
    for name, value in (("keys", keys), ("previous_keys", previous_keys)):
        if isinstance(value, str):
            raise ValueError(f"{name} 要是 key 的序列，不是單一字串")
    return NewJudgmentEvent(
        kind=JudgmentKind.CLASSIFICATION_FIX,
        member_id=member_id,
        actor_role=actor_role,
        target_type=JudgmentTarget.PAPER,
        target_id=paper_id,
        field=TaxonomyField(field).value,
        old_value=None if previous_keys is None else tuple(sorted(set(previous_keys))),
        new_value=tuple(sorted(set(keys))),
        reason=reason,
        ai_result_id=ai_result_id,
    )


def merge_decision(
    *,
    member_id: uuid.UUID,
    actor_role: ActorRole,
    construct_a: uuid.UUID,
    construct_b: uuid.UUID,
    decision: MergeDecisionValue,
    evidence_level: EvidenceLevel,
    reason: str | None,
) -> NewJudgmentEvent:
    """一對構面合併與否的決定；兩個 ID 不分順序，ID 小的自動放在 `target_id`。

    Raises:
        ValueError: 兩個構面相同，或見 `NewJudgmentEvent`。
    """
    if construct_a == construct_b:
        raise ValueError("合併決定要是兩個不同的構面")
    low, high = sorted((construct_a, construct_b))
    return NewJudgmentEvent(
        kind=JudgmentKind.MERGE_DECISION,
        member_id=member_id,
        actor_role=actor_role,
        target_type=JudgmentTarget.CONSTRUCT_PAIR,
        target_id=low,
        field=None,
        old_value=None,
        new_value=MappingProxyType(
            {
                "decision": MergeDecisionValue(decision).value,
                "evidence_level": EvidenceLevel(evidence_level).value,
            }
        ),
        reason=reason,
        related_id=high,
    )


def summary_report(
    *,
    member_id: uuid.UUID,
    actor_role: ActorRole,
    zh_summary_id: uuid.UUID,
    field: SummaryField,
    reason: str,
) -> NewJudgmentEvent:
    """成員回報一筆中文重點的某一欄可能有誤（只對這一筆 `zh_summary` 有效）。

    Raises:
        ValueError: 見 `NewJudgmentEvent`（理由必填）。
    """
    return NewJudgmentEvent(
        kind=JudgmentKind.SUMMARY_REPORT,
        member_id=member_id,
        actor_role=actor_role,
        target_type=JudgmentTarget.ZH_SUMMARY,
        target_id=zh_summary_id,
        field=SummaryField(field).value,
        old_value=None,
        new_value=None,
        reason=reason,
    )


def summary_report_resolution(
    *,
    member_id: uuid.UUID,
    zh_summary_id: uuid.UUID,
    field: SummaryField,
    report_event_id: uuid.UUID,
    resolution: SummaryResolution,
    reason: str | None,
) -> NewJudgmentEvent:
    """管理者對一筆回報的處理結果（角色固定 `admin`）。`zh_summary_id`、`field` 要和原本那筆回報
    一樣，寫入時才確認。

    Raises:
        ValueError: 見 `NewJudgmentEvent`。
    """
    return NewJudgmentEvent(
        kind=JudgmentKind.SUMMARY_REPORT_RESOLUTION,
        member_id=member_id,
        actor_role=ActorRole.ADMIN,
        target_type=JudgmentTarget.ZH_SUMMARY,
        target_id=zh_summary_id,
        field=SummaryField(field).value,
        old_value=None,
        new_value=SummaryResolution(resolution).value,
        reason=reason,
        report_event_id=report_event_id,
    )
