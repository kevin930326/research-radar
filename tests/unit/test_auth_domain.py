"""登入的 domain 規則（Story 3.3、ADR-0008、ADR-0046）：session token、email、顯示名稱、轉回路徑、
session 期限、稽核事件的形狀。資料全是測試自己編的（`*@example.com`、假名字）。"""

import base64
import hashlib
import uuid
from datetime import timedelta

import pytest

from radar.domain.audit import AuditActorRole, NewAuditEvent, system_audit_event
from radar.domain.auth import (
    ADMIN_SETUP_COMMANDS,
    AdminMemberMissingError,
    InvalidDisplayNameError,
    InvalidEmailError,
    LoginRejectedError,
    LoginRejection,
    NewSession,
    SessionPolicy,
    check_display_name,
    is_well_formed_token,
    new_session_token,
    normalize_email,
    safe_next_path,
    token_sha256,
)

MEMBER = uuid.UUID("00000000-0000-7000-8000-000000000001")

# --- session token ---------------------------------------------------------------------------


def test_new_token_is_256_bits_of_base64url() -> None:
    tokens = {new_session_token() for _ in range(50)}

    assert len(tokens) == 50
    for token in tokens:
        assert len(token) == 43
        assert is_well_formed_token(token)
        # 43 個 base64url 字元＝32 位元組＝256 位元。
        padded = token + "="
        assert len(base64.urlsafe_b64decode(padded)) == 32


def test_token_hash_is_sha256_of_the_token() -> None:
    token = new_session_token()

    digest = token_sha256(token)

    assert digest == hashlib.sha256(token.encode("ascii")).digest()
    assert len(digest) == 32


@pytest.mark.parametrize(
    "value",
    [None, "", "a" * 42, "a" * 44, "a" * 42 + "=", "a" * 42 + "+", "a" * 42 + "/", "é" * 43, 123],
)
def test_malformed_tokens_are_rejected(value: object) -> None:
    assert not is_well_formed_token(value)
    if isinstance(value, str):
        with pytest.raises(ValueError, match="格式"):
            token_sha256(value)


def test_new_session_repr_hides_the_token() -> None:
    token = new_session_token()
    session = NewSession(session_id=uuid.uuid4(), member_id=MEMBER, token=token)

    assert token not in repr(session)
    assert token not in str(session)


# --- email ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Alice@Example.com", "alice@example.com"),
        ("member.one+lab@example.com", "member.one+lab@example.com"),
        ("A_B-C@sub.example.co.uk", "a_b-c@sub.example.co.uk"),
    ],
)
def test_email_is_lowercased(value: str, expected: str) -> None:
    assert normalize_email(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "alice",
        "@example.com",
        "alice@",
        "alice@example",
        "alice@@example.com",
        " alice@example.com",
        "alice@example.com ",
        "ali ce@example.com",
        "alice@exa mple.com",
        "alice@-example.com",
        "alice@example..com",
        ".alice@example.com",
        "alice.@example.com",
        "al..ice@example.com",
        "ålice@example.com",
        "alice@example.com\n",
        "alice\x00@example.com",
        "a" * 65 + "@example.com",
        "a@" + "b" * 250 + ".com",
        None,
        42,
    ],
)
def test_bad_emails_are_rejected_without_echoing_them(value: object) -> None:
    with pytest.raises(InvalidEmailError) as caught:
        normalize_email(value)

    # 訊息只有固定的幾句，不帶輸入的值。
    assert str(caught.value) in {
        "email 要是字串",
        "email 要是 1～254 個 ASCII 字元",
        "email 的格式不對（要像 name@example.com）",
    }


# --- 顯示名稱 ---------------------------------------------------------------------------------


def test_display_name_is_stripped() -> None:
    assert check_display_name("  測試甲  ") == "測試甲"
    assert check_display_name("x" * 100) == "x" * 100


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "　",
        "x" * 101,
        "測試\n甲",
        "測試\t甲",
        "測試\x00甲",
        "測試‮甲",  # 雙向控制字元
        "測試​甲",  # 零寬空白（格式字元）
        "測試 甲",  # 行分隔
        "測試\ud800甲",  # 單獨的代理字元
        None,
        7,
    ],
)
def test_bad_display_names_are_rejected(value: object) -> None:
    with pytest.raises(InvalidDisplayNameError):
        check_display_name(value)


# --- 轉回路徑 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["/", "/guide", "/papers/123?tab=summary#top", "/a/b-c_d.e~f", "/search?q=a%20b"],
)
def test_site_paths_are_kept(value: str) -> None:
    assert safe_next_path(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "//evil.example.com",
        "///evil.example.com",
        "https://evil.example.com",
        "http:/evil",
        "javascript:alert(1)",
        "/\\evil.example.com",
        "\\\\evil.example.com",
        "/path with space",
        "/tab\there",
        "/new\nline",
        "/\x00",
        "/é",
        "guide",
        "",
        "/" + "a" * 2048,
        None,
        12,
    ],
)
def test_anything_else_becomes_root(value: object) -> None:
    assert safe_next_path(value) == "/"


# --- session 期限 -----------------------------------------------------------------------------


def test_session_policy_rejects_bad_limits() -> None:
    SessionPolicy(idle=timedelta(days=14), max_age=timedelta(days=30))
    with pytest.raises(ValueError, match="正的"):
        SessionPolicy(idle=timedelta(0), max_age=timedelta(days=30))
    with pytest.raises(ValueError, match="不能比閒置期限短"):
        SessionPolicy(idle=timedelta(days=14), max_age=timedelta(days=13))


# --- 錯誤訊息 ---------------------------------------------------------------------------------


def test_admin_missing_message_lists_the_two_commands_with_placeholders() -> None:
    error = AdminMemberMissingError()

    assert error.commands == ADMIN_SETUP_COMMANDS
    assert error.commands == (
        "radar members allow <管理者 email> --name <名稱>",
        "radar members set-admin <管理者 email>",
    )
    for command in ADMIN_SETUP_COMMANDS:
        assert command in str(error)
    assert "@" not in str(error)


def test_login_rejection_message_has_only_the_reason() -> None:
    error = LoginRejectedError(LoginRejection.EMAIL_NOT_ADMIN)

    assert error.reason is LoginRejection.EMAIL_NOT_ADMIN
    assert str(error) == "假登入被拒：email_not_admin"


# --- 稽核事件 ---------------------------------------------------------------------------------


def test_system_audit_event_has_no_member() -> None:
    event = system_audit_event(action="allow_member", target_type="member", target_id=MEMBER)

    assert event.actor_role is AuditActorRole.SYSTEM
    assert event.actor_member_id is None
    assert event.detail_json() == "{}"


def test_audit_detail_is_frozen_copy() -> None:
    detail: dict[str, object] = {"source": "cli"}
    event = NewAuditEvent(
        actor_member_id=MEMBER,
        actor_role=AuditActorRole.ADMIN,
        action="set_member_admin",
        target_type="member",
        target_id=MEMBER,
        detail=detail,
    )
    detail["source"] = "changed"

    assert event.detail_json() == '{"source": "cli"}'


@pytest.mark.parametrize(
    "overrides",
    [
        {"actor_role": AuditActorRole.SYSTEM},  # system 卻有成員
        {"actor_member_id": None},  # 成員角色卻沒有成員
        {"action": "Allow"},
        {"action": "allow-member"},
        {"action": ""},
        {"target_type": "1member"},
        {"detail": {1: "x"}},
        {"detail": {"x": float("nan")}},
        {"detail": {"x": object()}},
        {"target_id": "not-a-uuid"},
    ],
)
def test_bad_audit_events_are_rejected(overrides: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "actor_member_id": MEMBER,
        "actor_role": AuditActorRole.MEMBER,
        "action": "allow_member",
        "target_type": "member",
        "target_id": MEMBER,
        "detail": {},
        **overrides,
    }

    with pytest.raises(ValueError, match=r"."):
        NewAuditEvent(**fields)  # type: ignore[arg-type]
