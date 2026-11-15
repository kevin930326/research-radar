"""人工判斷事件的形狀（Story 2.7、`radar/domain/judgments.py`）：四個建構函式各自做得出合格的事件，
每種形狀錯誤都在建構時丟 `ValueError`（資料庫的 CHECK 是第二道，見 `test_migration_0014.py`）。"""

import dataclasses
import uuid
from typing import Any, cast

import pytest

from radar.domain.judgments import (
    ActorRole,
    EvidenceLevel,
    JudgmentKind,
    JudgmentTarget,
    MergeDecisionValue,
    NewJudgmentEvent,
    SummaryField,
    SummaryResolution,
    classification_fix,
    merge_decision,
    summary_report,
    summary_report_resolution,
)
from radar.domain.taxonomy import TaxonomyField

MEMBER = uuid.UUID("00000000-0000-7000-8000-000000000001")
PAPER = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
SUMMARY = uuid.UUID("00000000-0000-7000-8000-0000000000bb")
REPORT = uuid.UUID("00000000-0000-7000-8000-0000000000cc")
LOW = uuid.UUID("00000000-0000-7000-8000-000000000010")
HIGH = uuid.UUID("00000000-0000-7000-8000-000000000020")


def _fix(**changes: Any) -> NewJudgmentEvent:
    values: dict[str, Any] = {
        "member_id": MEMBER,
        "actor_role": ActorRole.MEMBER,
        "paper_id": PAPER,
        "field": TaxonomyField.TOPIC,
        "keys": ["supply_chain"],
        "reason": None,
    } | changes
    return classification_fix(**values)


# --- classification_fix ---------------------------------------------------------------------


def test_classification_fix_sorts_and_dedupes_keys() -> None:
    event = _fix(
        keys=["supply_chain", "erp", "erp"],
        previous_keys=["erp"],
        ai_result_id=SUMMARY,
        reason="  這篇主要談供應鏈 ",
    )

    assert event.kind is JudgmentKind.CLASSIFICATION_FIX
    assert event.target_type is JudgmentTarget.PAPER
    assert event.target_id == PAPER
    assert event.field == "topic"
    assert event.new_value == ("erp", "supply_chain")
    assert event.old_value == ("erp",)
    assert event.ai_result_id == SUMMARY
    assert event.json_values() == (["erp"], ["erp", "supply_chain"])


def test_classification_fix_method_may_be_empty_and_unclassified_topic_alone_is_ok() -> None:
    assert _fix(field=TaxonomyField.METHOD, keys=[]).new_value == ()
    assert _fix(keys=["unclassified"]).new_value == ("unclassified",)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"keys": []}, "主題至少"),
        ({"keys": ["unclassified", "erp"]}, "只能單獨"),
        ({"field": TaxonomyField.METHOD, "keys": ["unclassified"]}, "只能單獨"),
        ({"keys": ["ERP"]}, "寫法"),
        ({"keys": ["supply-chain"]}, "寫法"),
        ({"previous_keys": ["Bad Key"]}, "寫法"),
        ({"reason": "   "}, "理由"),
        ({"reason": "a\x00b"}, "NUL"),
    ],
)
def test_classification_fix_rejects_bad_shapes(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _fix(**changes)


@pytest.mark.parametrize("name", ["keys", "previous_keys"])
def test_classification_fix_rejects_a_bare_string_instead_of_keys(name: str) -> None:
    # 字串也是序列；不擋的話 "erp" 會被拆成 ('e', 'p', 'r') 通過驗證。
    with pytest.raises(ValueError, match="單一字串"):
        _fix(**{name: "erp"})


def test_direct_construction_rejects_unsorted_keys_and_wrong_field() -> None:
    event = _fix()
    with pytest.raises(ValueError, match="排序"):
        dataclasses.replace(event, new_value=("supply_chain", "erp"))
    with pytest.raises(ValueError, match="排序"):
        dataclasses.replace(event, new_value=("erp", "erp"))
    with pytest.raises(ValueError, match="topic 或 method"):
        dataclasses.replace(event, field="theory")
    with pytest.raises(ValueError, match="tuple"):
        dataclasses.replace(event, new_value=cast(Any, ["erp"]))
    with pytest.raises(ValueError, match="對象"):
        dataclasses.replace(event, target_type=JudgmentTarget.ZH_SUMMARY)
    with pytest.raises(ValueError, match="report_event_id"):
        dataclasses.replace(event, report_event_id=REPORT)
    with pytest.raises(ValueError, match="related_id"):
        dataclasses.replace(event, related_id=HIGH)


# --- merge_decision -------------------------------------------------------------------------


def test_merge_decision_puts_smaller_id_first() -> None:
    event = merge_decision(
        member_id=MEMBER,
        actor_role=ActorRole.ADMIN,
        construct_a=HIGH,
        construct_b=LOW,
        decision=MergeDecisionValue.ACCEPT,
        evidence_level=EvidenceLevel.DEFINITION,
        reason=None,
    )

    assert (event.target_type, event.target_id, event.related_id) == (
        JudgmentTarget.CONSTRUCT_PAIR,
        LOW,
        HIGH,
    )
    assert event.field is None
    assert event.json_values() == (None, {"decision": "accept", "evidence_level": "definition"})


def test_merge_decision_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError, match="不同的構面"):
        merge_decision(
            member_id=MEMBER,
            actor_role=ActorRole.MEMBER,
            construct_a=LOW,
            construct_b=LOW,
            decision=MergeDecisionValue.REJECT,
            evidence_level=EvidenceLevel.NAME,
            reason=None,
        )
    event = merge_decision(
        member_id=MEMBER,
        actor_role=ActorRole.MEMBER,
        construct_a=LOW,
        construct_b=HIGH,
        decision=MergeDecisionValue.REJECT,
        evidence_level=EvidenceLevel.NAME,
        reason=None,
    )
    with pytest.raises(ValueError, match="ID 小的"):
        dataclasses.replace(event, target_id=HIGH, related_id=LOW)
    with pytest.raises(ValueError, match="decision 與 evidence_level"):
        dataclasses.replace(event, new_value={"decision": "accept"})
    with pytest.raises(ValueError, match="decision 要是"):
        dataclasses.replace(event, new_value={"decision": "maybe", "evidence_level": "name"})
    with pytest.raises(ValueError, match="evidence_level 要是"):
        dataclasses.replace(event, new_value={"decision": "accept", "evidence_level": "vibes"})
    with pytest.raises(ValueError, match="沒有欄位"):
        dataclasses.replace(event, field="topic")
    with pytest.raises(ValueError, match="old_value"):
        dataclasses.replace(event, old_value="accept")


def test_direct_merge_decision_keeps_a_read_only_copy_not_the_callers_dict() -> None:
    value = {"decision": "accept", "evidence_level": "items"}
    event = NewJudgmentEvent(
        kind=JudgmentKind.MERGE_DECISION,
        member_id=MEMBER,
        actor_role=ActorRole.MEMBER,
        target_type=JudgmentTarget.CONSTRUCT_PAIR,
        target_id=LOW,
        field=None,
        old_value=None,
        new_value=value,
        reason=None,
        related_id=HIGH,
    )

    value["decision"] = "maybe"

    assert event.new_value is not value
    assert event.json_values() == (None, {"decision": "accept", "evidence_level": "items"})
    with pytest.raises(TypeError):
        cast(Any, event.new_value)["decision"] = "reject"


# --- summary_report 與處理結果 -----------------------------------------------------------------


def test_summary_report_and_resolution() -> None:
    report = summary_report(
        member_id=MEMBER,
        actor_role=ActorRole.MEMBER,
        zh_summary_id=SUMMARY,
        field=SummaryField.THEORY,
        reason="摘要沒有提到這個理論",
    )
    resolution = summary_report_resolution(
        member_id=MEMBER,
        zh_summary_id=SUMMARY,
        field=SummaryField.THEORY,
        report_event_id=REPORT,
        resolution=SummaryResolution.CONFIRMED_OK,
        reason=None,
    )

    assert (report.target_type, report.target_id, report.field) == (
        JudgmentTarget.ZH_SUMMARY,
        SUMMARY,
        "theory",
    )
    assert report.json_values() == (None, None)
    assert resolution.actor_role is ActorRole.ADMIN
    assert resolution.report_event_id == REPORT
    assert resolution.json_values() == (None, "confirmed_ok")


def test_summary_report_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError, match="理由"):
        summary_report(
            member_id=MEMBER,
            actor_role=ActorRole.MEMBER,
            zh_summary_id=SUMMARY,
            field=SummaryField.METHOD,
            reason=" ",
        )
    report = summary_report(
        member_id=MEMBER,
        actor_role=ActorRole.MEMBER,
        zh_summary_id=SUMMARY,
        field=SummaryField.METHOD,
        reason="方法寫錯",
    )
    with pytest.raises(ValueError, match="一定要寫理由"):
        dataclasses.replace(report, reason=None)
    with pytest.raises(ValueError, match="四欄之一"):
        dataclasses.replace(report, field="topic")
    with pytest.raises(ValueError, match="new_value"):
        dataclasses.replace(report, new_value="confirmed_ok")
    with pytest.raises(ValueError, match="對象"):
        dataclasses.replace(report, target_type=JudgmentTarget.PAPER)


def test_summary_report_resolution_rejects_bad_shapes() -> None:
    resolution = summary_report_resolution(
        member_id=MEMBER,
        zh_summary_id=SUMMARY,
        field=SummaryField.FINDINGS,
        report_event_id=REPORT,
        resolution=SummaryResolution.REGENERATED,
        reason="已重新產生",
    )
    with pytest.raises(ValueError, match="管理者"):
        dataclasses.replace(resolution, actor_role=ActorRole.MEMBER)
    with pytest.raises(ValueError, match="confirmed_ok 或 regenerated"):
        dataclasses.replace(resolution, new_value="fixed")
    with pytest.raises(ValueError, match="report_event_id"):
        dataclasses.replace(resolution, report_event_id=None)
    with pytest.raises(ValueError, match="四欄之一"):
        dataclasses.replace(resolution, field=None)


def test_unknown_kind_and_role_are_rejected() -> None:
    event = _fix()
    with pytest.raises(ValueError, match="JudgmentKind"):
        dataclasses.replace(event, kind=cast(Any, "classification_note"))
    with pytest.raises(ValueError, match="ActorRole"):
        dataclasses.replace(event, actor_role=cast(Any, "system"))
    with pytest.raises(ValueError, match="UUID"):
        dataclasses.replace(event, member_id=cast(Any, "someone"))


def test_kind_values_are_the_four_fixed_ones() -> None:
    assert {kind.value for kind in JudgmentKind} == {
        "classification_fix",
        "merge_decision",
        "summary_report",
        "summary_report_resolution",
    }
