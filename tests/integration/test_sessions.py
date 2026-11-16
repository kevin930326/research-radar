"""登入與 session 對真的 PostgreSQL（Story 3.3、ADR-0008 第 2 項、ADR-0046）。

用 `build_auth_routes` 接真的資料庫，時鐘由測試控制。涵蓋：開發環境假登入、資料庫只存 token 的
SHA-256、閒置與最長期限、撤銷（刪 session）、移出允許名單、不在名單的成員、Origin、登出後 token
失效、試用環境只有管理者能登入（金鑰對由測試自己產生、公鑰清單由假函式給，不連 Cloudflare）、
管理者不存在時印出兩個指令、日誌不出現管理者 email、期限的精確邊界與時區、已經失效的 session
登出時也刪掉，以及 Epic 7 前半段：試用環境 session 的成員寫進
`judgment_event.member_id`。資料全是測試自己編的（`*@example.com`、假名字）。
"""

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from functools import partial
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import SecretStr

from radar.adapters.cloudflare_access import confirm_access_identity
from radar.adapters.fake.login import pick_member_identity
from radar.domain.auth import ADMIN_SETUP_COMMANDS, FakeLoginRequest, LoginIdentity
from radar.domain.auth_settings import AuthSettings, Environment
from radar.domain.judgments import ActorRole, classification_fix
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import SystemViewer
from radar.entrypoints.api.app import create_app
from radar.entrypoints.api.auth import (
    ACCESS_ASSERTION_HEADER,
    SESSION_COOKIE,
    build_auth_routes,
    install_auth_routes,
)
from radar.entrypoints.logging_config import configure_logging
from radar.repo.db import Database
from radar.repo.judgments import create_judgment_event
from radar.services.members import allow_member, set_member_admin
from tests.database import TestDatabase, migrate
from tests.llm_support import create_paper, execute

DEV_ORIGIN = "http://localhost:8000"
TRIAL_ORIGIN = "https://radar.example.com"
TEAM = "testteam.cloudflareaccess.com"
AUDIENCE = "test-audience"
ADMIN_EMAIL = "lab.admin@example.com"
KID = "test-key-1"
START = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@dataclass
class Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _client(
    database: Database,
    settings: AuthSettings,
    confirm: Callable[[FakeLoginRequest], LoginIdentity],
    clock: Clock,
) -> TestClient:
    app = create_app(readiness_check=lambda: True)
    install_auth_routes(app, build_auth_routes(database, settings, confirm, clock))
    # https：Secure cookie 才會被測試用的瀏覽器替身帶著；測試仍明確用標頭帶 cookie。
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def dev_client(database: Database, clock: Clock) -> TestClient:
    settings = AuthSettings(environment=Environment.DEVELOPMENT, fake_login=True)
    return _client(database, settings, pick_member_identity, clock)


def _allow(database: Database, email: str, name: str, *, admin: bool = False) -> uuid.UUID:
    change = allow_member(SystemViewer(), database, email=email, display_name=name)
    if admin:
        set_member_admin(SystemViewer(), database, email=email)
    return change.member_id


def _token(response: Response) -> str:
    set_cookie = response.headers["set-cookie"]
    name, _, rest = set_cookie.partition("=")
    assert name == SESSION_COOKIE
    return rest.split(";", 1)[0]


def _dev_login(client: TestClient, member_id: uuid.UUID) -> str:
    response = client.post(
        "/api/v1/auth/fake-login",
        json={"member_id": str(member_id)},
        headers={"Origin": DEV_ORIGIN},
    )
    assert response.status_code == 200, response.text
    client.cookies.clear()
    return _token(response)


def _me(client: TestClient, token: str) -> Response:
    client.cookies.clear()
    return client.get("/api/v1/me", headers={"Cookie": f"{SESSION_COOKIE}={token}"})


def _sessions(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db, "SELECT id, member_id, token_sha256, created_at, last_seen_at FROM session ORDER BY id"
    )


# --- 開發環境 ---------------------------------------------------------------------------------


def test_dev_fake_login_lists_members_and_starts_a_session(
    db: TestDatabase, database: Database, dev_client: TestClient
) -> None:
    first = _allow(database, "member.one@example.com", "測試甲")
    second = _allow(database, "member.two@example.com", "測試乙")

    options = dev_client.get("/api/v1/auth/fake-login")
    assert options.json() == {
        "mode": "pick_member",
        "members": [
            {"member_id": str(first), "display_name": "測試甲"},
            {"member_id": str(second), "display_name": "測試乙"},
        ],
    }
    assert "@" not in options.text

    token = _dev_login(dev_client, second)

    ((_id, member_id, digest, created_at, last_seen_at),) = _sessions(db)
    assert member_id == second
    # 資料庫只有 token 的 SHA-256，沒有 token 本身。
    assert bytes(digest) == hashlib.sha256(token.encode("ascii")).digest()
    assert token not in json.dumps([str(v) for v in _sessions(db)[0]])
    assert created_at == last_seen_at == START
    me = _me(dev_client, token)
    assert me.status_code == 200
    assert me.json() == {"member_id": str(second), "display_name": "測試乙"}


def test_last_seen_is_written_only_after_five_minutes(
    db: TestDatabase, database: Database, dev_client: TestClient, clock: Clock
) -> None:
    token = _dev_login(dev_client, _allow(database, "member.one@example.com", "測試甲"))

    clock.advance(timedelta(minutes=4))
    assert _me(dev_client, token).status_code == 200
    assert _sessions(db)[0][4] == START

    clock.advance(timedelta(minutes=2))
    assert _me(dev_client, token).status_code == 200
    assert _sessions(db)[0][4] == START + timedelta(minutes=6)


def test_session_expires_after_14_idle_days(
    database: Database, dev_client: TestClient, clock: Clock
) -> None:
    token = _dev_login(dev_client, _allow(database, "member.one@example.com", "測試甲"))

    clock.advance(timedelta(days=14, minutes=1))

    assert _me(dev_client, token).status_code == 401


@pytest.mark.parametrize(
    ("idle", "status"),
    [
        (timedelta(days=14) - timedelta(seconds=1), 200),  # 到期前一秒
        (timedelta(days=14), 401),  # 恰好閒置 14 天就失效
        (timedelta(days=14, seconds=1), 401),
    ],
)
def test_idle_deadline_boundary(
    database: Database, dev_client: TestClient, clock: Clock, idle: timedelta, status: int
) -> None:
    token = _dev_login(dev_client, _allow(database, "member.one@example.com", "測試甲"))

    clock.advance(idle)

    assert _me(dev_client, token).status_code == status


@pytest.mark.parametrize(
    ("age", "status"),
    [
        (timedelta(days=30) - timedelta(seconds=1), 200),  # 到期前一秒
        (timedelta(days=30), 401),  # 建立滿 30 天就失效，就算最近才用過
        (timedelta(days=30, seconds=1), 401),
    ],
)
def test_max_age_deadline_boundary(
    database: Database, dev_client: TestClient, clock: Clock, age: timedelta, status: int
) -> None:
    token = _dev_login(dev_client, _allow(database, "member.one@example.com", "測試甲"))
    for _ in range(2):  # 第 13、26 天各用一次，讓閒置期限不先到
        clock.advance(timedelta(days=13))
        assert _me(dev_client, token).status_code == 200

    clock.advance(age - timedelta(days=26))

    assert _me(dev_client, token).status_code == status


def test_deadlines_do_not_depend_on_the_clock_timezone(
    db: TestDatabase, database: Database, dev_client: TestClient, clock: Clock
) -> None:
    # 主機時鐘用哪個時區表示同一個瞬間，存進去的時間與期限的判斷都一樣。
    taipei, new_york = timezone(timedelta(hours=8)), timezone(timedelta(hours=-5))
    clock.now = START.astimezone(taipei)
    first = _dev_login(dev_client, _allow(database, "member.one@example.com", "測試甲"))
    second = _dev_login(dev_client, _allow(database, "member.two@example.com", "測試乙"))
    assert [row[3] for row in _sessions(db)] == [START, START]

    clock.now = (START + timedelta(days=14) - timedelta(seconds=1)).astimezone(new_york)
    assert _me(dev_client, first).status_code == 200
    clock.now = (START + timedelta(days=14)).astimezone(taipei)
    assert _me(dev_client, second).status_code == 401


def test_active_session_still_expires_after_30_days(
    database: Database, dev_client: TestClient, clock: Clock
) -> None:
    token = _dev_login(dev_client, _allow(database, "member.one@example.com", "測試甲"))

    for days in (13, 13):
        clock.advance(timedelta(days=days))
        assert _me(dev_client, token).status_code == 200
    clock.advance(timedelta(days=4, minutes=1))  # 建立後 30 天又 1 分鐘；最後使用才 4 天前

    assert _me(dev_client, token).status_code == 401


def test_deleted_session_is_rejected_at_once(
    db: TestDatabase, database: Database, dev_client: TestClient
) -> None:
    token = _dev_login(dev_client, _allow(database, "member.one@example.com", "測試甲"))

    execute(db, "DELETE FROM session")

    assert _me(dev_client, token).status_code == 401


def test_member_removed_from_allowlist_is_rejected_at_once(
    db: TestDatabase, database: Database, dev_client: TestClient
) -> None:
    member_id = _allow(database, "member.one@example.com", "測試甲")
    token = _dev_login(dev_client, member_id)

    execute(db, "DELETE FROM allowed_email WHERE member_id = %s", member_id)

    assert _me(dev_client, token).status_code == 401
    # 也不能再選這位成員登入。
    response = dev_client.post(
        "/api/v1/auth/fake-login",
        json={"member_id": str(member_id)},
        headers={"Origin": DEV_ORIGIN},
    )
    assert response.status_code == 403


def test_member_not_on_allowlist_cannot_log_in(
    db: TestDatabase, database: Database, dev_client: TestClient
) -> None:
    _allow(database, "member.one@example.com", "測試甲")

    response = dev_client.post(
        "/api/v1/auth/fake-login",
        json={"member_id": str(uuid.uuid4())},
        headers={"Origin": DEV_ORIGIN},
    )

    assert response.status_code == 403
    assert response.json() == {
        "type": "about:blank",
        "title": "Forbidden",
        "status": 403,
        "code": "login_rejected",
    }
    assert _sessions(db) == []


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example.com"}])
def test_wrong_origin_creates_no_session(
    db: TestDatabase, database: Database, dev_client: TestClient, headers: dict[str, str]
) -> None:
    member_id = _allow(database, "member.one@example.com", "測試甲")

    response = dev_client.post(
        "/api/v1/auth/fake-login", json={"member_id": str(member_id)}, headers=headers
    )

    assert response.status_code == 403
    assert _sessions(db) == []


def test_logout_makes_the_token_useless(
    db: TestDatabase, database: Database, dev_client: TestClient
) -> None:
    token = _dev_login(dev_client, _allow(database, "member.one@example.com", "測試甲"))
    other = _dev_login(dev_client, _allow(database, "member.two@example.com", "測試乙"))

    response = dev_client.post(
        "/api/v1/auth/logout",
        headers={"Origin": DEV_ORIGIN, "Cookie": f"{SESSION_COOKIE}={token}"},
    )

    assert response.status_code == 204
    assert _me(dev_client, token).status_code == 401
    # 別人的 session 不受影響。
    assert _me(dev_client, other).status_code == 200
    assert len(_sessions(db)) == 1


@pytest.mark.parametrize("why", ["idle", "max_age", "removed_from_allowlist"])
def test_logout_deletes_a_session_that_is_no_longer_valid(
    db: TestDatabase, database: Database, dev_client: TestClient, clock: Clock, why: str
) -> None:
    member_id = _allow(database, "member.one@example.com", "測試甲")
    token = _dev_login(dev_client, member_id)
    other_id = _allow(database, "member.two@example.com", "測試乙")
    _dev_login(dev_client, other_id)
    if why == "idle":
        clock.advance(timedelta(days=14, minutes=1))
    elif why == "max_age":
        execute(
            db,
            "UPDATE session SET created_at = %s WHERE member_id = %s",
            START - timedelta(days=31),
            member_id,
        )
    else:
        execute(db, "DELETE FROM allowed_email WHERE member_id = %s", member_id)
    assert _me(dev_client, token).status_code == 401

    response = dev_client.post(
        "/api/v1/auth/logout",
        headers={"Origin": DEV_ORIGIN, "Cookie": f"{SESSION_COOKIE}={token}"},
    )

    assert response.status_code == 204
    # 已經失效的 session 也要刪掉那一列：之後調長期限或把人加回名單，這個 token 都不會再有效。
    # 別人的 session（就算一樣過期了）不受影響。
    assert [row[1] for row in _sessions(db)] == [other_id]


# --- Epic 7 前半段：session 的成員就是判斷事件的成員 -----------------------------------------


def test_trial_session_member_is_the_judgment_event_member(
    db: TestDatabase, database: Database, trial_client: TestClient, access_key: RSAKey
) -> None:
    allowed = _allow(database, ADMIN_EMAIL, "管理者", admin=True)
    login = _trial_login(trial_client, _assertion(access_key, ADMIN_EMAIL))
    assert login.status_code == 200, login.text
    member_id = uuid.UUID(_me(trial_client, _token(login)).json()["member_id"])
    paper_id = create_paper(database)

    with database.transaction() as connection:
        event_id = create_judgment_event(
            connection,
            classification_fix(
                member_id=member_id,
                actor_role=ActorRole.ADMIN,
                paper_id=paper_id,
                field=TaxonomyField.TOPIC,
                keys=["supply_chain"],
                reason=None,
            ),
        )

    ((event_member, google_sub),) = execute(
        db,
        "SELECT j.member_id, m.google_sub FROM judgment_event j"
        " JOIN member m ON m.id = j.member_id WHERE j.id = %s",
        event_id,
    )
    assert event_member == allowed
    assert google_sub is None


# --- 試用環境 ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def access_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": KID})


@pytest.fixture
def trial_client(database: Database, clock: Clock, access_key: RSAKey) -> TestClient:
    settings = AuthSettings(
        environment=Environment.TRIAL,
        fake_login=True,
        admin_email=SecretStr(ADMIN_EMAIL),
        access_team_domain=TEAM,
        access_audience=AUDIENCE,
        site_origin=TRIAL_ORIGIN,
    )
    jwks = {"keys": [access_key.as_dict(private=False)]}
    confirm = partial(
        confirm_access_identity,
        team_domain=TEAM,
        audience=AUDIENCE,
        admin_email=ADMIN_EMAIL,
        fetch_jwks=lambda: jwks,
        clock=clock,
    )
    return _client(database, settings, confirm, clock)


def _assertion(key: RSAKey, email: str) -> str:
    now = int(START.timestamp())
    claims = {
        "iss": f"https://{TEAM}",
        "aud": [AUDIENCE],
        "email": email,
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
    }
    return jwt.encode({"alg": "RS256", "kid": KID}, claims, key)


def _trial_login(client: TestClient, assertion: str, **body: Any) -> Response:
    response = client.post(
        "/api/v1/auth/fake-login",
        json=body,
        headers={"Origin": TRIAL_ORIGIN, ACCESS_ASSERTION_HEADER: assertion},
    )
    client.cookies.clear()
    return response


def test_trial_without_admin_member_prints_the_two_commands(
    db: TestDatabase,
    database: Database,
    trial_client: TestClient,
    access_key: RSAKey,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    # 在名單上但不是管理者，也算「管理者不存在」。
    _allow(database, ADMIN_EMAIL, "管理者")

    response = _trial_login(trial_client, _assertion(access_key, ADMIN_EMAIL))

    assert response.status_code == 403
    assert response.json() == {
        "type": "about:blank",
        "title": "Forbidden",
        "status": 403,
        "code": "admin_member_missing",
    }
    assert _sessions(db) == []
    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert any(r.get("commands") == list(ADMIN_SETUP_COMMANDS) for r in records)
    assert ADMIN_EMAIL not in output


def test_trial_logs_in_only_the_admin(
    db: TestDatabase,
    database: Database,
    trial_client: TestClient,
    access_key: RSAKey,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    admin_id = _allow(database, ADMIN_EMAIL, "管理者", admin=True)
    member_id = _allow(database, "member.one@example.com", "測試甲")

    # 不能選人、不列出成員。
    assert trial_client.get("/api/v1/auth/fake-login").json() == {
        "mode": "access_admin",
        "members": [],
    }
    chose = _trial_login(
        trial_client, _assertion(access_key, ADMIN_EMAIL), member_id=str(member_id)
    )
    assert chose.status_code == 403
    # 別人的 JWT（簽章有效）不行；沒有 JWT 不行。
    assert (
        _trial_login(trial_client, _assertion(access_key, "member.one@example.com")).status_code
        == 403
    )
    no_jwt = trial_client.post("/api/v1/auth/fake-login", json={}, headers={"Origin": TRIAL_ORIGIN})
    assert no_jwt.status_code == 403
    # 開發環境的來源在試用環境不算數。
    wrong_origin = trial_client.post(
        "/api/v1/auth/fake-login",
        json={},
        headers={
            "Origin": DEV_ORIGIN,
            ACCESS_ASSERTION_HEADER: _assertion(access_key, ADMIN_EMAIL),
        },
    )
    assert wrong_origin.status_code == 403
    assert _sessions(db) == []

    response = _trial_login(trial_client, _assertion(access_key, ADMIN_EMAIL.upper()))

    assert response.status_code == 200, response.text
    ((_id, session_member, *_rest),) = _sessions(db)
    assert session_member == admin_id
    me = _me(trial_client, _token(response))
    assert me.json() == {"member_id": str(admin_id), "display_name": "管理者"}
    assert ADMIN_EMAIL not in capsys.readouterr().out
