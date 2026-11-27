"""成員修正分類的驗證與歷史（Story 3.11、`radar/domain/classification_fixes.py`）。

後端的每一條規則各有一條測試（前端的檢查不算數）：主題與方法各至少一個、每個都是現在分類表的
`key`（不收 `unclassified`、放錯欄位、不存在的 key、重複）、和目前的有效值不同才收、理由選填與上限
100 字（code point）、理由不收控制字元、行分隔符與段落分隔符、格式字元（零寬連接放行）、存不進
資料庫的字；分類沒有成功的
論文先不收（等 Kevin 決定）。錯誤訊息不回顯成員送的值。分類表用真的 `config/taxonomy.yaml`。
資料全是測試自己編的（成員名字是明顯的假名）。
"""

import unicodedata
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
    MemberPicks,
    check_classification_fix,
    classification_fix_lock_key,
    group_fix_history,
    latest_picks_per_member,
)
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer
from radar.repo.visibility import visible_member_judgments
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


def test_without_a_successful_ai_classification_the_fix_is_rejected() -> None:
    # 分類沒有成功的論文能不能修正等 Kevin 決定（TASK 第 6 部分第 8 點）；決定前後端不收
    # （triage T7）。
    error = _problem(topic=["e_commerce"], method=["experiment"], current=NOTHING)

    assert error.problem is ClassificationFixProblem.NO_AI_CLASSIFICATION
    assert "e_commerce" not in str(error)


def test_an_old_value_on_one_field_is_still_a_current_classification() -> None:
    # 只有一欄有值（Story 2.7 之前或只修正過一欄）不算「沒有分類」。
    fix = _check(current=CurrentClassification(topic=("e_commerce",), method=None))

    assert fix.method == ("experiment",)


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


def test_reason_length_counts_code_points_not_utf16_units() -> None:
    # 表情符號是一個 code point、兩個 UTF-16 單位：算一個字（前端同一種算法，triage T16）。
    assert _check(reason="😀" * 100).reason == "😀" * 100
    assert _problem(reason="😀" * 101).problem is ClassificationFixProblem.REASON_TOO_LONG


@pytest.mark.parametrize(
    "reason",
    [
        "一行\n兩行",
        "tab\there",
        "nul\x00",
        "bell\x07",
        "\ud800",
        "行分隔\u2028第二行",
        "段落分隔\u2029第二段",
        "\u2028",
    ],
)
def test_reason_rejects_control_and_unstorable_characters(reason: str) -> None:
    assert _problem(reason=reason).problem is ClassificationFixProblem.REASON_INVALID_CHARACTER


@pytest.mark.parametrize("reason", [3, ["理由"], {"r": 1}])
def test_reason_must_be_text(reason: object) -> None:
    assert _problem(reason=reason).problem is ClassificationFixProblem.REASON_NOT_TEXT


# --- 理由裡的格式字元（Cf；epic-3m PR B，照回報說明的做法） ------------------------------------

REASON_FORMAT_CHARACTERS = [
    "\u200b",  # 零寬空白
    "\u200c",  # 零寬不連接
    "\ufeff",  # BOM（零寬不換行空白）
    "\u00ad",  # 軟連字號
    "\u200e",  # 由左至右標記
    "\u200f",  # 由右至左標記
    *[chr(code) for code in range(0x202A, 0x202F)],  # 雙向嵌入與覆寫 U+202A～U+202E
    *[chr(code) for code in range(0x2066, 0x206A)],  # 雙向隔離 U+2066～U+2069
    "\U000e0041",  # 標籤字元
]


def test_the_reason_format_character_list_is_what_it_says() -> None:
    assert len(REASON_FORMAT_CHARACTERS) == 16
    assert {unicodedata.category(char) for char in REASON_FORMAT_CHARACTERS} == {"Cf"}


@pytest.mark.parametrize("char", REASON_FORMAT_CHARACTERS, ids=lambda char: f"U+{ord(char):04X}")
def test_reason_rejects_format_characters_at_the_start_middle_and_end(char: str) -> None:
    for reason in (char + "跨平台", "跨" + char + "平台", "跨平台" + char, char):
        error = _problem(reason=reason)
        assert error.problem is ClassificationFixProblem.REASON_INVALID_CHARACTER
        assert error.field == "reason"


def test_reason_rejects_every_format_character_except_the_zero_width_joiner() -> None:
    # 整個 Unicode 的 Cf 都擋，只有零寬連接 U+200D 放行（和回報中文重點的說明同一份規則）。
    format_characters = [
        chr(code) for code in range(0x110000) if unicodedata.category(chr(code)) == "Cf"
    ]
    assert "\u200d" in format_characters
    for char in format_characters:
        if char == "\u200d":
            continue
        assert (
            _problem(reason="理由" + char).problem
            is ClassificationFixProblem.REASON_INVALID_CHARACTER
        ), f"U+{ord(char):04X}"


@pytest.mark.parametrize(
    "reason",
    ["\u200d跨平台", "跨\u200d平台", "跨平台\u200d", "跨平台👩\u200d💻", "👨\u200d👩\u200d👧"],
    ids=["start", "middle", "end", "emoji", "family"],
)
def test_reason_allows_the_zero_width_joiner(reason: str) -> None:
    # 零寬連接 U+200D 放行：組合 emoji 要用它，它本身不改變文字方向。
    assert _check(reason=reason).reason == reason


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
        member_name="測試成員甲" if member == CHEN else "測試成員乙",
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
        ("測試成員甲", ("digital_platforms", "e_commerce"), ("experiment",), "跨平台"),
        ("測試成員乙", ("social_media_communities",), ("experiment",), None),
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


def test_latest_picks_take_each_field_on_its_own() -> None:
    # 甲先改了主題與方法，後來只改主題：方法仍是早先那一次（有效值 view 也是按欄取最新），
    # 時間各自分開。
    events = [
        _event(1, CHEN, T1, TaxonomyField.TOPIC, ("erp",)),
        _event(2, CHEN, T1, TaxonomyField.METHOD, ("survey_sem",)),
        _event(3, LIN, T1 + timedelta(days=1), TaxonomyField.TOPIC, ("social_media_communities",)),
        _event(4, LIN, T1 + timedelta(days=1), TaxonomyField.METHOD, ("experiment",)),
        _event(5, CHEN, T2, TaxonomyField.TOPIC, ("e_commerce",)),
    ]

    picks = latest_picks_per_member(reversed(events))

    assert picks == (
        MemberPicks("測試成員甲", ("e_commerce",), T2, ("survey_sem",), T1),
        MemberPicks(
            "測試成員乙",
            ("social_media_communities",),
            T1 + timedelta(days=1),
            ("experiment",),
            T1 + timedelta(days=1),
        ),
    )


def test_latest_picks_break_ties_by_event_id() -> None:
    # 同一個時間的兩筆同欄事件（不會發生，保險）：事件 ID 大的算新的，和 view 的 `id DESC` 一樣。
    events = [
        _event(2, CHEN, T1, TaxonomyField.TOPIC, ("erp",)),
        _event(1, CHEN, T1, TaxonomyField.TOPIC, ("e_commerce",)),
    ]

    [pick] = latest_picks_per_member(events)

    assert (pick.topic, pick.method, pick.method_at) == (("erp",), None, None)


def test_member_picks_need_matching_times() -> None:
    with pytest.raises(ValueError, match="主題"):
        MemberPicks("測試成員甲", ("erp",), None, None, None)
    with pytest.raises(ValueError, match="至少"):
        MemberPicks("測試成員甲", None, None, None, None)
    with pytest.raises(ValueError, match="時區"):
        MemberPicks("測試成員甲", ("erp",), datetime(2026, 11, 1), None, None)


# --- 誰讀得到修正歷史（triage T1）--------------------------------------------------------------


@pytest.mark.parametrize("viewer", [MemberViewer(member_id=CHEN, is_admin=False), SystemViewer()])
def test_members_and_the_system_may_read_fix_history(viewer: MemberViewer | SystemViewer) -> None:
    assert visible_member_judgments(viewer) == "TRUE"


@pytest.mark.parametrize("viewer", [PublicViewer(), object()])
def test_the_public_viewer_never_reads_fix_history(viewer: object) -> None:
    # 示範站產生器（公開身分）讀不到修正歷史、姓名與理由：不組查詢、直接丟錯。
    with pytest.raises(TypeError, match="成員"):
        visible_member_judgments(viewer)  # type: ignore[arg-type]
