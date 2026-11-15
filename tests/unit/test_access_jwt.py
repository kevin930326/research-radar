"""試用環境的 Access JWT 驗證（Story 3.3、ADR-0008 第 1.3 項、ADR-0046）。

金鑰對全由測試自己產生，公鑰清單由注入的假函式給；不連 Cloudflare。每一個反例只改一個地方，
其他都和正例相同，證明擋下的就是那一項。
"""

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt
from joserfc.errors import SecurityWarning
from joserfc.jwk import RSAKey

from radar.adapters.cloudflare_access import (
    JWKS_PATH,
    AccessKeysError,
    confirm_access_identity,
    fetch_access_jwks,
    verify_access_jwt,
)
from radar.domain.auth import FakeLoginRequest, LoginRejectedError, LoginRejection, VerifiedEmail
from radar.entrypoints.logging_config import configure_logging

TEAM = "testteam.cloudflareaccess.com"
AUDIENCE = "aud-0123456789abcdef"
ADMIN = "admin@example.com"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
KID = "test-key-1"


@pytest.fixture(scope="module")
def key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": KID})


@pytest.fixture(scope="module")
def other_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": KID})


def _jwks(*keys: RSAKey, **extra: Any) -> dict[str, Any]:
    return {"keys": [{**k.as_dict(private=False), **extra} for k in keys]}


def _claims(**overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "iss": f"https://{TEAM}",
        "aud": [AUDIENCE],
        "email": ADMIN,
        "sub": "00000000-test-sub",
        "iat": int(NOW.timestamp()) - 10,
        "nbf": int(NOW.timestamp()) - 10,
        "exp": int((NOW + timedelta(hours=1)).timestamp()),
        "type": "app",
    }
    for name, value in overrides.items():
        if value is _DROP:
            claims.pop(name, None)
        else:
            claims[name] = value
    return claims


_DROP = object()


def _sign(signing_key: RSAKey, claims: Mapping[str, Any], **header: Any) -> str:
    return jwt.encode({"alg": "RS256", "kid": KID, **header}, dict(claims), signing_key)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _handmade(header: Mapping[str, Any], claims: Mapping[str, Any], signature: bytes) -> str:
    return ".".join(
        [_b64(json.dumps(header).encode()), _b64(json.dumps(claims).encode()), _b64(signature)]
    )


def _verify(token: str, jwks: Mapping[str, Any]) -> str:
    return verify_access_jwt(
        token, team_domain=TEAM, audience=AUDIENCE, fetch_jwks=lambda: jwks, now=NOW
    )


def _rejected(token: str, jwks: Mapping[str, Any]) -> LoginRejection:
    with pytest.raises(LoginRejectedError) as caught:
        _verify(token, jwks)
    return caught.value.reason


# --- 正例 -------------------------------------------------------------------------------------


def test_valid_token_returns_lowercased_email(key: RSAKey) -> None:
    token = _sign(key, _claims(email="Admin@Example.COM"))

    assert _verify(token, _jwks(key)) == ADMIN


def test_audience_as_plain_string_is_accepted(key: RSAKey) -> None:
    assert _verify(_sign(key, _claims(aud=AUDIENCE)), _jwks(key)) == ADMIN


def test_audience_list_with_other_entries_is_accepted(key: RSAKey) -> None:
    assert _verify(_sign(key, _claims(aud=["other", AUDIENCE])), _jwks(key)) == ADMIN


def test_nbf_is_optional(key: RSAKey) -> None:
    assert _verify(_sign(key, _claims(nbf=_DROP)), _jwks(key)) == ADMIN


def test_right_key_is_picked_by_kid(key: RSAKey, other_key: RSAKey) -> None:
    jwks = {
        "keys": [
            {**other_key.as_dict(private=False), "kid": "another"},
            key.as_dict(private=False),
        ]
    }

    assert _verify(_sign(key, _claims()), jwks) == ADMIN


# --- 反例：簽章與演算法 -----------------------------------------------------------------------


def test_signature_from_another_key_is_rejected(key: RSAKey, other_key: RSAKey) -> None:
    token = _sign(other_key, _claims())

    assert _rejected(token, _jwks(key)) is LoginRejection.ASSERTION_INVALID


def test_tampered_payload_is_rejected(key: RSAKey) -> None:
    header, _payload, signature = _sign(key, _claims()).split(".")
    forged = _b64(json.dumps(_claims(email="intruder@example.com")).encode())

    assert _rejected(f"{header}.{forged}.{signature}", _jwks(key)) is (
        LoginRejection.ASSERTION_INVALID
    )


def test_hs256_signed_with_the_public_key_is_rejected(key: RSAKey) -> None:
    # 把 RSA 公鑰當 HMAC 密鑰偽造 HS256（joserfc 2026-11-03 的公告）。
    public_pem = key.as_pem(private=False)
    header = {"alg": "HS256", "kid": KID, "typ": "JWT"}
    claims = _claims()
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()

    token = f"{signing_input}.{_b64(signature)}"

    assert _rejected(token, _jwks(key)) is LoginRejection.ASSERTION_INVALID


@pytest.mark.parametrize("signature", [b"", b"x"])
def test_alg_none_is_rejected(key: RSAKey, signature: bytes) -> None:
    token = _handmade({"alg": "none", "kid": KID}, _claims(), signature)

    assert _rejected(token, _jwks(key)) is LoginRejection.ASSERTION_INVALID


@pytest.mark.parametrize("alg", ["RS512", "PS256", "ES256", ["RS256"], None])
def test_other_algorithms_in_the_header_are_rejected(key: RSAKey, alg: object) -> None:
    header: dict[str, object] = {"kid": KID}
    if alg is not None:
        header["alg"] = alg
    token = _handmade(header, _claims(), b"signature")

    assert _rejected(token, _jwks(key)) is LoginRejection.ASSERTION_INVALID


@pytest.mark.parametrize("kid", [None, "", 7, ["k"], "x" * 257])
def test_missing_or_malformed_kid_is_rejected_before_fetching_keys(
    key: RSAKey, kid: object
) -> None:
    header: dict[str, object] = {"alg": "RS256"}
    if kid is not None:
        header["kid"] = kid
    token = _handmade(header, _claims(), b"signature")
    calls: list[int] = []

    def counting() -> Mapping[str, Any]:
        calls.append(1)
        return _jwks(key)

    with pytest.raises(LoginRejectedError) as caught:
        verify_access_jwt(token, team_domain=TEAM, audience=AUDIENCE, fetch_jwks=counting, now=NOW)

    assert caught.value.reason is LoginRejection.ASSERTION_INVALID
    assert calls == []


def test_unknown_kid_is_rejected(key: RSAKey) -> None:
    token = jwt.encode({"alg": "RS256", "kid": "unknown-kid"}, _claims(), key)

    assert _rejected(token, _jwks(key)) is LoginRejection.ASSERTION_INVALID


def test_crit_header_is_rejected_before_fetching_keys(key: RSAKey) -> None:
    token = _handmade({"alg": "RS256", "kid": KID, "crit": ["x-ext"], "x-ext": 1}, _claims(), b"s")
    calls: list[int] = []

    def counting() -> Mapping[str, Any]:
        calls.append(1)
        return _jwks(key)

    with pytest.raises(LoginRejectedError):
        verify_access_jwt(token, team_domain=TEAM, audience=AUDIENCE, fetch_jwks=counting, now=NOW)

    assert calls == []


@pytest.mark.parametrize(
    "jwks_extra", [{"kty": "oct"}, {"use": "enc"}, {"alg": "RS512"}, {"n": 5}, {"e": None}]
)
def test_unusable_public_keys_are_skipped(key: RSAKey, jwks_extra: dict[str, Any]) -> None:
    token = _sign(key, _claims())

    assert _rejected(token, _jwks(key, **jwks_extra)) is LoginRejection.ASSERTION_INVALID


@pytest.mark.parametrize("jwks", [{}, {"keys": "x"}, {"keys": [1, "a"]}, []])
def test_malformed_key_sets_are_rejected(key: RSAKey, jwks: Any) -> None:
    assert _rejected(_sign(key, _claims()), jwks) is LoginRejection.ASSERTION_INVALID


def test_short_rsa_keys_are_rejected() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.warns(SecurityWarning, match="2048"):
        weak = RSAKey.import_key(pem, parameters={"kid": KID})

    assert _rejected(_sign(weak, _claims()), _jwks(weak)) is LoginRejection.ASSERTION_INVALID


@pytest.mark.parametrize(
    "token", ["", "a.b", "a.b.c.d", "a..c", "a.b.c=", "a b.c.d", "é.b.c", "x" * 9000]
)
def test_malformed_tokens_are_rejected(key: RSAKey, token: str) -> None:
    assert _rejected(token, _jwks(key)) is LoginRejection.ASSERTION_INVALID


def test_header_that_is_not_json_object_is_rejected(key: RSAKey) -> None:
    token = f"{_b64(b'[1, 2]')}.{_b64(b'{}')}.{_b64(b'sig')}"

    assert _rejected(token, _jwks(key)) is LoginRejection.ASSERTION_INVALID


# --- 反例：claims ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://otherteam.cloudflareaccess.com"},
        {"iss": TEAM},  # 少了 https://
        {"iss": [f"https://{TEAM}"]},  # 陣列型別的 iss（joserfc 2026-08 的公告）
        {"iss": None},
        {"iss": "https://téam.example.com"},  # 非 ASCII 也只是拒絕，不會出錯
        {"iss": _DROP},
        {"aud": "other-audience"},
        {"aud": ["other-audience"]},
        {"aud": []},
        {"aud": [AUDIENCE, 5]},
        {"aud": 5},
        {"aud": "aud-é"},
        {"aud": {"aud": AUDIENCE}},
        {"aud": _DROP},
        {"exp": int((NOW - timedelta(seconds=1)).timestamp())},  # 過期
        {"exp": int(NOW.timestamp())},  # 剛好到期
        {"exp": str(int((NOW + timedelta(hours=1)).timestamp()))},
        {"exp": float(int((NOW + timedelta(hours=1)).timestamp()))},
        {"exp": True},
        {"exp": _DROP},
        {"nbf": int((NOW + timedelta(minutes=5)).timestamp())},  # 還沒生效
        {"nbf": "0"},
        {"nbf": True},
        {"email": _DROP},
        {"email": ["admin@example.com"]},
        {"email": "not-an-email"},
        {"email": " admin@example.com"},
    ],
)
def test_bad_claims_are_rejected(key: RSAKey, overrides: dict[str, Any]) -> None:
    token = _sign(key, _claims(**overrides))

    assert _rejected(token, _jwks(key)) is LoginRejection.ASSERTION_INVALID


def test_failing_key_fetch_is_its_own_reason(key: RSAKey) -> None:
    def broken() -> Mapping[str, Any]:
        raise AccessKeysError("HTTP 503")

    with pytest.raises(LoginRejectedError) as caught:
        verify_access_jwt(
            _sign(key, _claims()), team_domain=TEAM, audience=AUDIENCE, fetch_jwks=broken, now=NOW
        )

    assert caught.value.reason is LoginRejection.ACCESS_KEYS_UNAVAILABLE


def test_keys_are_not_fetched_for_malformed_tokens() -> None:
    calls: list[int] = []

    def counting() -> Mapping[str, Any]:
        calls.append(1)
        return {"keys": []}

    with pytest.raises(LoginRejectedError):
        verify_access_jwt("a.b", team_domain=TEAM, audience=AUDIENCE, fetch_jwks=counting, now=NOW)

    assert calls == []


# --- confirm_access_identity ------------------------------------------------------------------


def _confirm(request: FakeLoginRequest, key: RSAKey) -> VerifiedEmail:
    return confirm_access_identity(
        request,
        team_domain=TEAM,
        audience=AUDIENCE,
        admin_email=ADMIN,
        fetch_jwks=lambda: _jwks(key),
        clock=lambda: NOW,
    )


def test_admin_with_valid_assertion_is_confirmed(key: RSAKey) -> None:
    request = FakeLoginRequest(member_id=None, access_assertion=_sign(key, _claims()))

    assert _confirm(request, key) == VerifiedEmail(email=ADMIN)


@pytest.mark.parametrize(
    ("request_factory", "reason"),
    [
        (
            lambda token: FakeLoginRequest(member_id=None, access_assertion=None),
            LoginRejection.ASSERTION_MISSING,
        ),
        (
            lambda token: FakeLoginRequest(
                member_id=__import__("uuid").UUID(int=1), access_assertion=token
            ),
            LoginRejection.MEMBER_CHOICE_NOT_ALLOWED,
        ),
    ],
)
def test_missing_header_or_chosen_member_is_rejected(
    key: RSAKey,
    request_factory: Callable[[str], FakeLoginRequest],
    reason: LoginRejection,
) -> None:
    with pytest.raises(LoginRejectedError) as caught:
        _confirm(request_factory(_sign(key, _claims())), key)

    assert caught.value.reason is reason


def test_email_other_than_admin_is_rejected(
    key: RSAKey, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    token = _sign(key, _claims(email="member@example.com"))

    with pytest.raises(LoginRejectedError) as caught:
        _confirm(FakeLoginRequest(member_id=None, access_assertion=token), key)

    assert caught.value.reason is LoginRejection.EMAIL_NOT_ADMIN
    out = capsys.readouterr().out
    assert "member@example.com" not in out
    assert ADMIN not in out


def test_rejections_do_not_log_the_token_or_email(
    key: RSAKey, other_key: RSAKey, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    token = _sign(other_key, _claims())

    with pytest.raises(LoginRejectedError):
        _confirm(FakeLoginRequest(member_id=None, access_assertion=token), key)

    out = capsys.readouterr().out
    assert "Access 的 JWT 沒有通過驗證" in out
    assert token not in out
    assert token.split(".")[1] not in out
    assert ADMIN not in out


# --- fetch_access_jwks ------------------------------------------------------------------------


def _transport(
    reply: Callable[[httpx2.Request], httpx2.Response], seen: list[httpx2.Request]
) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return reply(request)

    return httpx2.MockTransport(handler)


def test_fetch_gets_the_team_certs_url(key: RSAKey) -> None:
    seen: list[httpx2.Request] = []
    transport = _transport(lambda _r: httpx2.Response(200, json=_jwks(key)), seen)

    jwks = fetch_access_jwks(TEAM, transport=transport)

    assert jwks == _jwks(key)
    assert [(r.method, str(r.url)) for r in seen] == [("GET", f"https://{TEAM}{JWKS_PATH}")]
    assert JWKS_PATH == "/cdn-cgi/access/certs"


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(503),
        httpx2.Response(302, headers={"Location": "https://elsewhere.example.com"}),
        httpx2.Response(200, content=b"not json"),
        httpx2.Response(200, json=[1, 2]),
    ],
)
def test_fetch_failures_raise_access_keys_error(response: httpx2.Response) -> None:
    transport = _transport(lambda _r: response, [])

    with pytest.raises(AccessKeysError):
        fetch_access_jwks(TEAM, transport=transport)


def test_fetch_transport_error_raises_access_keys_error() -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(AccessKeysError, match="ConnectError"):
        fetch_access_jwks(TEAM, transport=httpx2.MockTransport(boom))
