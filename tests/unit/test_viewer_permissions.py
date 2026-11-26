"""三種讀者身分、寫入權限表（AD-3、AD-27）與可見條件（AD-4）。"""

import dataclasses
import uuid

import pytest

from radar.domain.permissions import (
    PERMISSIONS,
    Action,
    PermissionDeniedError,
    Rule,
    is_allowed,
    require,
)
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer, Viewer
from radar.repo.visibility import visible_lab_papers, visible_papers, visible_to_system_only

OWNER_ID = uuid.UUID("018f0000-0000-7000-8000-0000000000a1")
OTHER_ID = uuid.UUID("018f0000-0000-7000-8000-0000000000b2")

OWNER = MemberViewer(member_id=OWNER_ID, is_admin=False)
OWNER_ADMIN = MemberViewer(member_id=OWNER_ID, is_admin=True)
MEMBER = MemberViewer(member_id=OTHER_ID, is_admin=False)
ADMIN = MemberViewer(member_id=OTHER_ID, is_admin=True)
PUBLIC = PublicViewer()
SYSTEM = SystemViewer()

ACTORS: dict[str, Viewer] = {
    "owner": OWNER,
    "owner_admin": OWNER_ADMIN,
    "member": MEMBER,
    "admin": ADMIN,
    "public": PUBLIC,
    "system": SYSTEM,
}

# 每種規則：（對擁有者是 OWNER_ID 的資源）哪些身分可以做。
ALLOWED_BY_RULE: dict[Rule, set[str]] = {
    Rule.ANY_MEMBER: {"owner", "owner_admin", "member", "admin"},
    Rule.OWNER: {"owner", "owner_admin"},
    Rule.ADMIN: {"owner_admin", "admin"},
    Rule.SYSTEM: {"system"},
}

# FR-7 權限表（寫入列）＋ `ingest_papers`、`run_ai_jobs`，照 PRD 的字面寫一次，和程式裡的表對照。
EXPECTED_TABLE: dict[Action, Rule] = {
    Action.CORRECT_CLASSIFICATION: Rule.ANY_MEMBER,
    # FR-14 附則：成員回報中文重點錯誤；Kevin（管理者）用 CLI 處理回報（Story 3.12）。
    Action.REPORT_SUMMARY: Rule.ANY_MEMBER,
    Action.RESOLVE_SUMMARY_REPORT: Rule.ADMIN,
    Action.CONFIRM_CONSTRUCT_MERGE: Rule.ANY_MEMBER,
    Action.MERGE_CONSTRUCTS: Rule.ADMIN,
    Action.ADD_LIBRARY_ITEM: Rule.OWNER,
    Action.REMOVE_LIBRARY_ITEM: Rule.OWNER,
    Action.UPLOAD_PDF: Rule.ANY_MEMBER,
    Action.DELETE_PDF: Rule.OWNER,
    Action.MANAGE_ALLOWLIST: Rule.ADMIN,
    Action.MANAGE_SETTINGS: Rule.ADMIN,
    Action.INGEST_PAPERS: Rule.SYSTEM,
    Action.RUN_AI_JOBS: Rule.SYSTEM,
}


def test_permission_table_matches_fr7() -> None:
    assert dict(PERMISSIONS) == EXPECTED_TABLE
    assert set(PERMISSIONS) == set(Action)


def test_permission_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        PERMISSIONS[Action.INGEST_PAPERS] = Rule.ANY_MEMBER  # type: ignore[index]


@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("actor_name", list(ACTORS))
def test_every_rule_for_every_actor(action: Action, actor_name: str) -> None:
    actor = ACTORS[actor_name]
    expected = actor_name in ALLOWED_BY_RULE[PERMISSIONS[action]]

    assert is_allowed(actor, action, OWNER_ID) is expected
    if expected:
        require(actor, action, OWNER_ID)
    else:
        with pytest.raises(PermissionDeniedError):
            require(actor, action, OWNER_ID)


@pytest.mark.parametrize("action", list(Action))
def test_public_viewer_cannot_write_anything(action: Action) -> None:
    with pytest.raises(PermissionDeniedError):
        require(PUBLIC, action, None)


def test_admin_does_not_pass_owner_rule_for_someone_else() -> None:
    with pytest.raises(PermissionDeniedError):
        require(ADMIN, Action.DELETE_PDF, OWNER_ID)


def test_owner_rule_without_owner_denies() -> None:
    with pytest.raises(PermissionDeniedError):
        require(OWNER, Action.DELETE_PDF, None)


def test_denied_message_does_not_contain_member_id() -> None:
    with pytest.raises(PermissionDeniedError) as caught:
        require(MEMBER, Action.MANAGE_SETTINGS, None)

    assert str(OTHER_ID) not in str(caught.value)
    assert caught.value.action is Action.MANAGE_SETTINGS


def test_viewers_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        OWNER.is_admin = True  # type: ignore[misc]


# --- 可見條件 -----------------------------------------------------------------------------


def test_member_sees_lab_visible_papers() -> None:
    # Story 3.16：加上「或在自己的文獻庫」（AD-4），成員 ID 以 UUID 常值組進去。
    assert visible_papers(MEMBER) == (
        '("paper".lab_visible OR EXISTS (SELECT 1 FROM library_item AS visible_library'
        ' WHERE visible_library.paper_id = "paper".id'
        f" AND visible_library.owner_id = '{OTHER_ID}'::uuid))"
    )
    assert visible_papers(ADMIN, alias="p") == (
        '("p".lab_visible OR EXISTS (SELECT 1 FROM library_item AS visible_library'
        ' WHERE visible_library.paper_id = "p".id'
        f" AND visible_library.owner_id = '{OTHER_ID}'::uuid))"
    )


def test_system_sees_everything_and_public_sees_counted_papers() -> None:
    assert visible_papers(SYSTEM) == "TRUE"
    assert visible_papers(PUBLIC) == (
        "EXISTS (SELECT 1 FROM counted_paper AS visible_counted"
        ' WHERE visible_counted.id = "paper".id)'
    )
    assert visible_papers(PUBLIC, alias="c") == (
        'EXISTS (SELECT 1 FROM counted_paper AS visible_counted WHERE visible_counted.id = "c".id)'
    )


@pytest.mark.parametrize("viewer", [PUBLIC, MEMBER, SYSTEM])
def test_alias_cannot_be_the_subquery_alias(viewer: Viewer) -> None:
    # 和 EXISTS 子查詢裡的別名相同時，條件會變成「自己等於自己」而永遠成立。
    with pytest.raises(ValueError, match="visible_counted"):
        visible_papers(viewer, alias="visible_counted")


@pytest.mark.parametrize("viewer", [PUBLIC, MEMBER, SYSTEM])
def test_alias_cannot_be_the_library_subquery_alias(viewer: Viewer) -> None:
    # 成員分支的 EXISTS 子查詢用 visible_library（Story 3.16）：外面的別名也不能是它。
    with pytest.raises(ValueError, match="visible_library"):
        visible_papers(viewer, alias="visible_library")


@pytest.mark.parametrize("alias", ["", "Paper", "p.x", 'p"', "1p", "p; DROP TABLE paper", "a" * 64])
def test_alias_must_be_lowercase_identifier(alias: str) -> None:
    with pytest.raises(ValueError, match="別名"):
        visible_papers(MEMBER, alias=alias)


def test_unknown_viewer_is_rejected() -> None:
    with pytest.raises(TypeError):
        visible_papers(object())  # type: ignore[arg-type]


def test_system_only_condition() -> None:
    assert visible_to_system_only(SYSTEM) == "TRUE"


@pytest.mark.parametrize("viewer", [MEMBER, ADMIN, PUBLIC, object()])
def test_system_only_rejects_everyone_else(viewer: object) -> None:
    with pytest.raises(TypeError, match="SystemViewer"):
        visible_to_system_only(viewer)  # type: ignore[arg-type]


# --- visible_lab_papers（Story 3.13 的 lab 範圍）------------------------------------------------


def test_lab_papers_are_lab_visible_and_visible() -> None:
    # 成員：lab_visible 且（lab_visible 或自己的文獻庫）＝只剩 lab_visible；私人論文不算。
    assert visible_lab_papers(MEMBER, alias="p") == (
        '("p".lab_visible AND ("p".lab_visible OR EXISTS (SELECT 1 FROM library_item AS'
        ' visible_library WHERE visible_library.paper_id = "p".id'
        f" AND visible_library.owner_id = '{OTHER_ID}'::uuid)))"
    )
    assert visible_lab_papers(SYSTEM, alias="p") == '("p".lab_visible AND TRUE)'
    assert visible_lab_papers(PUBLIC, alias="p") == (
        '("p".lab_visible AND EXISTS (SELECT 1 FROM counted_paper AS visible_counted'
        ' WHERE visible_counted.id = "p".id))'
    )


@pytest.mark.parametrize("alias", ["visible_counted", "visible_library", "P", "a;b", "1p", ""])
@pytest.mark.parametrize("viewer", [MEMBER, SYSTEM, PUBLIC])
def test_lab_papers_check_the_alias(viewer: Viewer, alias: str) -> None:
    with pytest.raises(ValueError, match="別名"):
        visible_lab_papers(viewer, alias=alias)


def test_lab_papers_reject_unknown_viewers() -> None:
    with pytest.raises(TypeError):
        visible_lab_papers(object(), alias="p")  # type: ignore[arg-type]
