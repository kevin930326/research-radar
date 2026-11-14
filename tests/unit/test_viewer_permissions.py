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
from radar.repo.visibility import visible_papers

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

# FR-7 權限表（寫入列）＋ `ingest_papers`，照 PRD 的字面寫一次，和程式裡的表對照。
EXPECTED_TABLE: dict[Action, Rule] = {
    Action.CORRECT_CLASSIFICATION: Rule.ANY_MEMBER,
    Action.CONFIRM_CONSTRUCT_MERGE: Rule.ANY_MEMBER,
    Action.MERGE_CONSTRUCTS: Rule.ADMIN,
    Action.ADD_LIBRARY_ITEM: Rule.OWNER,
    Action.REMOVE_LIBRARY_ITEM: Rule.OWNER,
    Action.UPLOAD_PDF: Rule.ANY_MEMBER,
    Action.DELETE_PDF: Rule.OWNER,
    Action.MANAGE_ALLOWLIST: Rule.ADMIN,
    Action.MANAGE_SETTINGS: Rule.ADMIN,
    Action.INGEST_PAPERS: Rule.SYSTEM,
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
    assert visible_papers(MEMBER) == '"paper".lab_visible'
    assert visible_papers(ADMIN, alias="p") == '"p".lab_visible'


def test_system_sees_everything_and_public_nothing_yet() -> None:
    assert visible_papers(SYSTEM) == "TRUE"
    assert visible_papers(PUBLIC) == "FALSE"


@pytest.mark.parametrize("alias", ["", "Paper", "p.x", 'p"', "1p", "p; DROP TABLE paper", "a" * 64])
def test_alias_must_be_lowercase_identifier(alias: str) -> None:
    with pytest.raises(ValueError, match="別名"):
        visible_papers(MEMBER, alias=alias)


def test_unknown_viewer_is_rejected() -> None:
    with pytest.raises(TypeError):
        visible_papers(object())  # type: ignore[arg-type]
