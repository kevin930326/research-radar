"""成員修正分類的驗證與歷史（Story 3.11、`radar/domain/classification_fixes.py`）。

後端的每一條規則各有一條測試（前端的檢查不算數）：主題與方法各至少一個、每個都是現在分類表的
`key`（不收 `unclassified`、放錯欄位、不存在的 key、重複）、和目前的有效值不同才收、理由選填與上限
100 字、理由不收控制字元與存不進資料庫的字。錯誤訊息不回顯成員送的值。分類表用真的
`config/taxonomy.yaml`。資料全是測試自己編的。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from radar.config import load_taxonomy
from radar.domain.ai_blocks import (
    AiBlock,
    AiBlockState,
    AiProduct,
    ClassificationFieldValue,
    ClassificationValue,
    Provenance,
)
from radar.domain.classification_fixes import (
    REASON_MAX_LENGTH,
    ClassificationFix,
    ClassificationFixEvent,
    ClassificationFixInvalidError,
    ClassificationFixProblem,
    CurrentClassification,
    check_classification_fix,
    classification_fix_lock_key,
    group_fix_history,
    latest_per_member,
)
from radar.domain.taxonomy import TaxonomyField
from tests.llm_support import ROOT

TAXONOMY = load_taxonomy({"RADAR_CONFIG_DIR": str(ROOT / "config")})
CURRENT = CurrentClassification(topic=("e_commerce",), method=("experiment",))
NOTHING = CurrentClassification(topic=None, method=None)


def _check(**changes: Any) -> ClassificationFix:
    values: dict[str, Any] = {
        "topic": ["e_commerce", "digital_platforms"],
        "method": ["experiment"],
        "reason": None,
        "taxonomy": TAXONOMY,
        "current": CURRENT,
    }
    values.update(changes)
    return check_classification_fix(**values)


def _problem(**changes: Any) -> ClassificationFixInvalidError:
    with pytest.raises(ClassificationFixInvalidError) as caught:
        _check(**changes)
    return caught.value


def test_taxonomy_keys_used_here_exist() -> None:
    # 這個檔用到的 key 要真的在分類表裡，否則下面的「收」都是假的。
    assert {"e_commerce", "digital_platforms"} <= set(TAXONOMY.keys(TaxonomyField.TOPIC))
    assert {"experiment", "survey_sem"} <= set(TAXONOMY.keys(TaxonomyField.METHOD))


def test_a_valid_fix_is_sorted_and_keeps_both_fields() -> None:
    fix = _check(reason="  研究的是跨平台的評分  ")

    assert fix == ClassificationFix(
        topic=("digital_platforms", "e_commerce"),
        method=("experiment",),
        reason="研究的是跨平台的評分",
    )


@pytest.mark.parametrize("field", ["topic", "method"])
def test_each_field_needs_at_least_one_key(field: str) -> None:
    error = _problem(**{field: []})

    assert error.problem is ClassificationFixProblem.EMPTY
    assert error.field == field


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("topic", ["unclassified"]),  # 不能改成「未分類」（決定第 98 項）
        ("topic", ["experiment"]),  # 方法的 key 放到主題
        ("method", ["e_commerce"]),  # 主題的 key 放到方法
        ("topic", ["no_such_topic"]),
        ("method", ["Experiment"]),  # 大小寫不同就不是同一個 key
        ("topic", ["e_commerce", "<script>"]),
    ],
)
def test_every_key_must_be_in_the_current_taxonomy(field: str, value: list[str]) -> None:
    error = _problem(**{field: value})

    assert error.problem is ClassificationFixProblem.UNKNOWN_KEY
    assert error.field == field
    # 不回顯送來的值。
    for key in value:
        assert key not in str(error)


@pytest.mark.parametrize("field", ["topic", "method"])
def test_the_same_key_twice_is_rejected(field: str) -> None:
    key = "e_commerce" if field == "topic" else "experiment"

    assert _problem(**{field: [key, key]}).problem is ClassificationFixProblem.DUPLICATE


@pytest.mark.parametrize("value", ["e_commerce", None, 3, [1], [None], {"e_commerce": 1}])
def test_a_field_must_be_a_list_of_strings(value: object) -> None:
    assert _problem(topic=value).problem is ClassificationFixProblem.NOT_A_LIST


def test_unchanged_on_both_fields_is_rejected() -> None:
    error = _problem(topic=["e_commerce"], method=["experiment"])

    assert error.problem is ClassificationFixProblem.UNCHANGED


def test_unchanged_compares_as_a_set() -> None:
    current = CurrentClassification(
        topic=("digital_platforms", "e_commerce"), method=("experiment",)
    )

    error = _problem(topic=["e_commerce", "digital_platforms"], current=current)

    assert error.problem is ClassificationFixProblem.UNCHANGED


def test_unchanged_ignores_the_order_of_the_current_ai_value() -> None:
    # AI 給的值沒有排序（人工值才排序）：同一組 key、順序不同也算沒改。
    current = CurrentClassification(
        topic=("e_commerce", "digital_platforms"), method=("survey_sem", "experiment")
    )

    error = _problem(
        topic=["digital_platforms", "e_commerce"],
        method=["experiment", "survey_sem"],
        current=current,
    )

    assert error.problem is ClassificationFixProblem.UNCHANGED


@pytest.mark.parametrize(
    ("topic", "method"),
    [
        (["e_commerce"], ["survey_sem"]),  # 只改方法
        (["digital_platforms"], ["experiment"]),  # 只改主題
    ],
)
def test_changing_one_field_is_enough_and_both_are_sent(
    topic: list[str], method: list[str]
) -> None:
    fix = _check(topic=topic, method=method)

    assert (fix.topic, fix.method) == (tuple(topic), tuple(method))


def test_without_a_current_value_any_valid_fix_is_new() -> None:
    fix = _check(topic=["e_commerce"], method=["experiment"], current=NOTHING)

    assert fix.topic == ("e_commerce",)


def test_reason_is_optional_and_blank_counts_as_none() -> None:
    assert _check(reason=None).reason is None
    assert _check(reason="").reason is None
    assert _check(reason="   　").reason is None


def test_reason_up_to_100_characters() -> None:
    assert REASON_MAX_LENGTH == 100
    assert _check(reason="字" * 100).reason == "字" * 100
    # 字數算 Unicode 字元，不是位元組（中文一個字是三個位元組）。
    assert _check(reason=" " + "字" * 100 + " ").reason == "字" * 100
    error = _problem(reason="字" * 101)
    assert error.problem is ClassificationFixProblem.REASON_TOO_LONG
    assert "字" not in str(error)


@pytest.mark.parametrize("reason", ["一行\n兩行", "tab\there", "nul\x00", "bell\x07", "\ud800"])
def test_reason_rejects_control_and_unstorable_characters(reason: str) -> None:
    assert _problem(reason=reason).problem is ClassificationFixProblem.REASON_INVALID_CHARACTER


@pytest.mark.parametrize("reason", [3, ["理由"], {"r": 1}])
def test_reason_must_be_text(reason: object) -> None:
    assert _problem(reason=reason).problem is ClassificationFixProblem.REASON_NOT_TEXT


# --- 目前的值 ---------------------------------------------------------------------------------

PROVENANCE = Provenance(
    model=None,
    generated_at=datetime(2026, 11, 1, tzinfo=UTC),
    input_kind=None,
    human_verified=False,
)


def test_current_classification_from_a_ready_block() -> None:
    value = ClassificationValue(
        topic=ClassificationFieldValue(("e_commerce",), False, 0, False),
        method=None,
        fix_member_count=0,
    )
    block = AiBlock(AiProduct.CLASSIFICATION, AiBlockState.READY, value, PROVENANCE)

    assert CurrentClassification.from_block(block) == CurrentClassification(
        topic=("e_commerce",), method=None
    )


@pytest.mark.parametrize(
    "state", [AiBlockState.FAILED, AiBlockState.PENDING_BATCH, AiBlockState.NOT_APPLICABLE]
)
def test_current_classification_without_a_value_is_empty(state: AiBlockState) -> None:
    block = AiBlock(AiProduct.CLASSIFICATION, state)

    assert CurrentClassification.from_block(block) == NOTHING


def test_current_classification_only_from_a_classification_block() -> None:
    with pytest.raises(ValueError, match="分類區塊"):
        CurrentClassification.from_block(AiBlock(AiProduct.ZH_SUMMARY, AiBlockState.NO_INPUT))


def test_lock_key_is_per_paper() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()

    assert classification_fix_lock_key(a) == classification_fix_lock_key(a)
    assert classification_fix_lock_key(a) != classification_fix_lock_key(b)


# --- 歷史 ------------------------------------------------------------------------------------

PAPER = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
CHEN = uuid.UUID("00000000-0000-7000-8000-000000000001")
LIN = uuid.UUID("00000000-0000-7000-8000-000000000002")
T1 = datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
T2 = T1 + timedelta(days=5)


def _event(
    n: int, member: uuid.UUID, at: datetime, field: TaxonomyField, keys: tuple[str, ...], **kw: Any
) -> ClassificationFixEvent:
    return ClassificationFixEvent(
        event_id=uuid.UUID(int=n),
        paper_id=kw.get("paper", PAPER),
        member_id=member,
        member_name="陳柏翰" if member == CHEN else "林怡君",
        fixed_at=at,
        field=field,
        keys=keys,
        reason=kw.get("reason"),
    )


def test_one_submission_is_one_entry_newest_first() -> None:
    events = [
        _event(4, CHEN, T2, TaxonomyField.METHOD, ("experiment",), reason="跨平台"),
        _event(
            3, CHEN, T2, TaxonomyField.TOPIC, ("digital_platforms", "e_commerce"), reason="跨平台"
        ),
        _event(2, LIN, T1, TaxonomyField.METHOD, ("experiment",)),
        _event(1, LIN, T1, TaxonomyField.TOPIC, ("social_media_communities",)),
    ]

    history = group_fix_history(reversed(events))

    assert [(e.member_name, e.topic, e.method, e.reason) for e in history] == [
        ("陳柏翰", ("digital_platforms", "e_commerce"), ("experiment",), "跨平台"),
        ("林怡君", ("social_media_communities",), ("experiment",), None),
    ]
    assert history[0].fixed_at == T2


def test_an_old_one_field_fix_leaves_the_other_field_empty() -> None:
    [entry] = group_fix_history([_event(1, LIN, T1, TaxonomyField.METHOD, ("experiment",))])

    assert (entry.topic, entry.method) == (None, ("experiment",))


def test_history_entries_carry_no_member_id() -> None:
    [entry] = group_fix_history(
        [_event(1, LIN, T1, TaxonomyField.TOPIC, ("social_media_communities",))]
    )

    assert not hasattr(entry, "member_id")
    assert str(LIN) not in repr(entry)


def test_history_is_for_one_paper_only() -> None:
    events = [
        _event(1, LIN, T1, TaxonomyField.TOPIC, ("social_media_communities",)),
        _event(2, LIN, T1, TaxonomyField.METHOD, ("experiment",), paper=uuid.uuid4()),
    ]

    with pytest.raises(ValueError, match="同一篇"):
        group_fix_history(events)


def test_latest_per_member_keeps_each_members_newest_submission() -> None:
    events = [
        _event(1, CHEN, T1, TaxonomyField.TOPIC, ("erp",)),
        _event(2, CHEN, T1, TaxonomyField.METHOD, ("survey_sem",)),
        _event(3, LIN, T1 + timedelta(days=1), TaxonomyField.TOPIC, ("social_media_communities",)),
        _event(4, LIN, T1 + timedelta(days=1), TaxonomyField.METHOD, ("experiment",)),
        _event(5, CHEN, T2, TaxonomyField.TOPIC, ("e_commerce",)),
        _event(6, CHEN, T2, TaxonomyField.METHOD, ("experiment",)),
    ]

    picks = latest_per_member(events)

    assert [(p.member_name, p.topic, p.fixed_at) for p in picks] == [
        ("陳柏翰", ("e_commerce",), T2),
        ("林怡君", ("social_media_communities",), T1 + timedelta(days=1)),
    ]
