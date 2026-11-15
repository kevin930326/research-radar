"""登入設定的組合（Story 3.3、ADR-0046）：三種環境＋Google 登入、試用環境缺項逐一、天數、來源格式、
`.env.example` 列齊、api `main()` 設定錯了回 1 並印原因、開發＋假登入走得到組裝。

管理者 email 一律用 `admin@example.com` 這類假值；不連資料庫、不起 uvicorn。
"""

import json
import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import Any

import pytest

import radar.entrypoints.api.main as api_main
from radar.config import ConfigError, env_name, load_auth_settings
from radar.domain.auth_settings import (
    DEVELOPMENT_ORIGINS,
    AuthSettings,
    Environment,
    FakeLoginMode,
    allowed_origins,
    fake_login_mode,
    session_policy,
)
from radar.entrypoints.bootstrap import build_fake_login_confirm

ROOT = Path(__file__).resolve().parents[2]
ADMIN_EMAIL = "admin@example.com"
TRIAL = {
    "RADAR_ENVIRONMENT": "trial",
    "RADAR_FAKE_LOGIN": "true",
    "RADAR_ACCESS_TEAM_DOMAIN": "testteam.cloudflareaccess.com",
    "RADAR_ACCESS_AUDIENCE": "test-audience",
    "RADAR_ADMIN_EMAIL": ADMIN_EMAIL,
    "RADAR_SITE_ORIGIN": "https://radar.example.com",
}


def _problems(environ: dict[str, str]) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        load_auth_settings(environ)
    return caught.value.problems


# --- 三種環境 ---------------------------------------------------------------------------------


def test_nothing_set_is_production_without_fake_login() -> None:
    settings = load_auth_settings({})

    assert settings.environment is Environment.PRODUCTION
    assert not settings.fake_login
    assert fake_login_mode(settings) is None
    # 正式環境沒設站台來源：拒絕所有會改資料的請求。
    assert allowed_origins(settings) == frozenset()


def test_development_with_fake_login_can_start() -> None:
    settings = load_auth_settings({"RADAR_ENVIRONMENT": "development", "RADAR_FAKE_LOGIN": "true"})

    assert fake_login_mode(settings) is FakeLoginMode.PICK_MEMBER
    assert allowed_origins(settings) == DEVELOPMENT_ORIGINS
    assert {"http://localhost:8000", "http://127.0.0.1:8000"} == DEVELOPMENT_ORIGINS


def test_trial_with_everything_set_can_start() -> None:
    settings = load_auth_settings(TRIAL)

    assert fake_login_mode(settings) is FakeLoginMode.ACCESS_ADMIN
    assert allowed_origins(settings) == {"https://radar.example.com"}
    assert settings.admin_email is not None
    assert settings.admin_email.get_secret_value() == ADMIN_EMAIL
    # 印出設定物件時看不到管理者 email。
    assert ADMIN_EMAIL not in repr(settings)
    assert ADMIN_EMAIL not in str(settings)


def test_admin_email_is_lowercased() -> None:
    settings = load_auth_settings({**TRIAL, "RADAR_ADMIN_EMAIL": "Admin@Example.COM"})

    assert settings.admin_email is not None
    assert settings.admin_email.get_secret_value() == ADMIN_EMAIL


@pytest.mark.parametrize(
    "field",
    ["access_team_domain", "access_audience", "admin_email", "site_origin"],
)
def test_trial_fake_login_missing_one_setting_does_not_start(field: str) -> None:
    environ = {key: value for key, value in TRIAL.items() if key != env_name(field)}

    problems = _problems(environ)

    assert problems == [f"缺少環境變數 {env_name(field)}（試用環境開假登入時一定要設定）"]


def test_trial_fake_login_missing_everything_lists_all_four() -> None:
    problems = _problems({"RADAR_ENVIRONMENT": "trial", "RADAR_FAKE_LOGIN": "true"})

    assert problems == [
        "缺少環境變數 RADAR_ACCESS_TEAM_DOMAIN（試用環境開假登入時一定要設定）",
        "缺少環境變數 RADAR_ACCESS_AUDIENCE（試用環境開假登入時一定要設定）",
        "缺少環境變數 RADAR_ADMIN_EMAIL（試用環境開假登入時一定要設定）",
        "缺少環境變數 RADAR_SITE_ORIGIN（試用環境開假登入時一定要設定）",
    ]


def test_trial_site_origin_must_be_https() -> None:
    problems = _problems({**TRIAL, "RADAR_SITE_ORIGIN": "http://radar.example.com"})

    assert problems == ["環境變數 RADAR_SITE_ORIGIN 在試用環境要是 https://"]


def test_trial_without_fake_login_needs_nothing() -> None:
    settings = load_auth_settings({"RADAR_ENVIRONMENT": "trial"})

    assert fake_login_mode(settings) is None


@pytest.mark.parametrize("environment", [None, "production"])
def test_production_with_fake_login_does_not_start(environment: str | None) -> None:
    environ = {"RADAR_FAKE_LOGIN": "true"}
    if environment is not None:
        environ["RADAR_ENVIRONMENT"] = environment

    problems = _problems(environ)

    assert problems == [
        "正式環境（RADAR_ENVIRONMENT 是 production 或沒設定）不能開假登入（RADAR_FAKE_LOGIN）"
    ]


@pytest.mark.parametrize("environment", ["development", "trial", "production"])
def test_fake_login_with_google_login_does_not_start(environment: str) -> None:
    environ = {**TRIAL, "RADAR_ENVIRONMENT": environment, "RADAR_GOOGLE_LOGIN": "true"}

    problems = _problems(environ)

    assert (
        "RADAR_FAKE_LOGIN 與 RADAR_GOOGLE_LOGIN 不能同時開（假登入與 Google 登入不能並存）"
        in problems
    )


def test_google_login_alone_is_allowed() -> None:
    settings = load_auth_settings({"RADAR_GOOGLE_LOGIN": "true"})

    assert settings.google_login
    assert fake_login_mode(settings) is None


def test_fake_login_mode_refuses_production_even_if_constructed_directly() -> None:
    settings = AuthSettings(fake_login=True)

    with pytest.raises(ValueError, match="正式環境"):
        fake_login_mode(settings)


# --- 格式 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RADAR_ENVIRONMENT", "staging"),
        ("RADAR_FAKE_LOGIN", "maybe"),
        ("RADAR_ADMIN_EMAIL", "not-an-email"),
        ("RADAR_ACCESS_TEAM_DOMAIN", "https://testteam.cloudflareaccess.com"),
        ("RADAR_ACCESS_TEAM_DOMAIN", "TestTeam.cloudflareaccess.com"),
        ("RADAR_ACCESS_TEAM_DOMAIN", "localhost"),
        ("RADAR_ACCESS_AUDIENCE", "has space"),
        ("RADAR_SITE_ORIGIN", "https://radar.example.com/"),
        ("RADAR_SITE_ORIGIN", "https://radar.example.com/app"),
        ("RADAR_SITE_ORIGIN", "https://radar.example.com?x=1"),
        ("RADAR_SITE_ORIGIN", "https://user@radar.example.com"),
        ("RADAR_SITE_ORIGIN", "ftp://radar.example.com"),
        ("RADAR_SITE_ORIGIN", "https://Radar.Example.com"),
        ("RADAR_SITE_ORIGIN", "radar.example.com"),
        ("RADAR_SESSION_IDLE_DAYS", "0"),
        ("RADAR_SESSION_MAX_DAYS", "abc"),
    ],
)
def test_bad_values_are_reported_without_the_value(name: str, value: str) -> None:
    problems = _problems({**TRIAL, name: value})

    assert len(problems) == 1, problems
    assert problems[0].startswith(f"環境變數 {name} 格式不對"), problems
    assert value not in problems[0]


def test_site_origin_with_port_is_accepted() -> None:
    settings = load_auth_settings(
        {"RADAR_ENVIRONMENT": "development", "RADAR_SITE_ORIGIN": "http://localhost:5173"}
    )

    assert allowed_origins(settings) == {"http://localhost:5173"}


@pytest.mark.parametrize("origin", ["https://radar.example.com:443", "http://localhost:80"])
def test_site_origin_with_default_port_is_rejected(origin: str) -> None:
    # 瀏覽器送的 Origin 不寫預設連接埠；寫了就每個 POST 都 403。
    with pytest.raises(ConfigError) as caught:
        load_auth_settings({"RADAR_ENVIRONMENT": "development", "RADAR_SITE_ORIGIN": origin})

    (problem,) = caught.value.problems
    assert problem.startswith("環境變數 RADAR_SITE_ORIGIN 格式不對")
    assert "預設連接埠" in problem
    assert origin not in problem


def test_session_days_default_to_14_and_30() -> None:
    policy = session_policy(load_auth_settings({}))

    assert policy.idle.days == 14
    assert policy.max_age.days == 30


def test_session_days_can_be_changed() -> None:
    policy = session_policy(
        load_auth_settings({"RADAR_SESSION_IDLE_DAYS": "7", "RADAR_SESSION_MAX_DAYS": "10"})
    )

    assert policy.idle.days == 7
    assert policy.max_age.days == 10


def test_max_days_shorter_than_idle_days_does_not_start() -> None:
    problems = _problems({"RADAR_SESSION_IDLE_DAYS": "20", "RADAR_SESSION_MAX_DAYS": "10"})

    assert problems == ["環境變數 RADAR_SESSION_MAX_DAYS 不能小於 RADAR_SESSION_IDLE_DAYS"]


def test_env_example_lists_every_auth_setting_without_values() -> None:
    entries = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )

    for field in AuthSettings.model_fields:
        assert env_name(field) in entries, field
        assert entries[env_name(field)] == ""


def test_admin_email_is_not_in_any_tracked_config_file() -> None:
    # repo 裡的設定檔（compose、範例檔、config/）都不放管理者 email 的值。
    files = [ROOT / "compose.yaml", ROOT / ".env.example", ROOT / ".env.radar.example"]
    files += sorted((ROOT / "config").glob("*.yaml"))
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("RADAR_ADMIN_EMAIL"):
                assert line.split("=", 1)[1:] in ([""], []), path
            assert "RADAR_ADMIN_EMAIL:" not in line, path


# --- 組裝 -------------------------------------------------------------------------------------


def test_confirm_function_follows_the_environment() -> None:
    from radar.adapters.fake.login import pick_member_identity

    assert build_fake_login_confirm(load_auth_settings({})) is None
    development = load_auth_settings({"RADAR_ENVIRONMENT": "development", "RADAR_FAKE_LOGIN": "1"})
    assert build_fake_login_confirm(development) is pick_member_identity
    from radar.adapters.cloudflare_access import confirm_access_identity, fetch_access_jwks

    trial = build_fake_login_confirm(
        load_auth_settings({**TRIAL, "RADAR_ADMIN_EMAIL": ADMIN_EMAIL.upper()})
    )
    assert isinstance(trial, partial)
    assert trial.func is confirm_access_identity
    assert trial.keywords["team_domain"] == TRIAL["RADAR_ACCESS_TEAM_DOMAIN"]
    assert trial.keywords["audience"] == TRIAL["RADAR_ACCESS_AUDIENCE"]
    assert trial.keywords["admin_email"] == ADMIN_EMAIL
    fetch = trial.keywords["fetch_jwks"]
    assert isinstance(fetch, partial)
    assert fetch.func is fetch_access_jwks
    assert fetch.args == (TRIAL["RADAR_ACCESS_TEAM_DOMAIN"],)


def test_api_process_does_not_load_joserfc_unless_trial() -> None:
    # api 行程 import main（含 bootstrap、登入路由）時不載入 joserfc；只有試用環境組裝時才 import。
    code = (
        "import sys, radar.entrypoints.api.main\n"
        "from radar.config import load_auth_settings\n"
        "from radar.entrypoints.bootstrap import build_fake_login_confirm\n"
        "build_fake_login_confirm(load_auth_settings("
        "{'RADAR_ENVIRONMENT': 'development', 'RADAR_FAKE_LOGIN': 'true'}))\n"
        "print('joserfc' in sys.modules, 'radar.adapters.cloudflare_access' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )

    assert result.stdout.split() == ["False", "False"]


# --- api 的 main() ----------------------------------------------------------------------------


class _ReachedError(Exception):
    """走到了組裝登入路由這一步（不起 uvicorn）。"""


class _ShouldNotBuildError(Exception):
    """設定錯了卻走到組裝這一步。"""


def _fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
    raise _ShouldNotBuildError


def _set_env(monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]) -> None:
    for field in AuthSettings.model_fields:
        monkeypatch.delenv(env_name(field), raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://radar_app@db:5432/radar")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(ROOT / "config"))
    for name, value in environ.items():
        monkeypatch.setenv(name, value)


def _startup_problems(out: str) -> list[str]:
    for line in out.splitlines():
        if line.startswith("{"):
            record = json.loads(line)
            if record.get("event") == "設定錯誤，無法啟動":
                return [str(problem) for problem in record["problems"]]
    return []


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        (
            {"RADAR_ENVIRONMENT": "production", "RADAR_FAKE_LOGIN": "true"},
            "正式環境（RADAR_ENVIRONMENT 是 production 或沒設定）不能開假登入（RADAR_FAKE_LOGIN）",
        ),
        (
            {
                "RADAR_ENVIRONMENT": "development",
                "RADAR_FAKE_LOGIN": "true",
                "RADAR_GOOGLE_LOGIN": "1",
            },
            "RADAR_FAKE_LOGIN 與 RADAR_GOOGLE_LOGIN 不能同時開（假登入與 Google 登入不能並存）",
        ),
        (
            {key: value for key, value in TRIAL.items() if key != "RADAR_ACCESS_AUDIENCE"},
            "缺少環境變數 RADAR_ACCESS_AUDIENCE（試用環境開假登入時一定要設定）",
        ),
    ],
)
def test_api_main_returns_1_and_logs_the_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
    expected: str,
) -> None:
    _set_env(monkeypatch, environ)
    monkeypatch.setattr(api_main, "build_readiness_probes", _fail_if_called)

    assert api_main.main() == 1

    out = capsys.readouterr().out
    assert _startup_problems(out) == [expected]
    assert ADMIN_EMAIL not in out


def test_api_main_with_development_fake_login_reaches_assembly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_env(monkeypatch, {"RADAR_ENVIRONMENT": "development", "RADAR_FAKE_LOGIN": "true"})

    class _Probes:
        ping = staticmethod(lambda: True)
        schema_state = staticmethod(lambda: None)
        database = object()

    built: list[Any] = []

    def fake_build(_settings: Any, *, pool_size: int, migrations_dir: Path | None = None) -> Any:
        return _Probes()

    def fake_install(_app: Any, routes: Any) -> None:
        built.append(routes)
        raise _ReachedError

    monkeypatch.setattr(api_main, "build_readiness_probes", fake_build)
    monkeypatch.setattr(api_main, "install_auth_routes", fake_install)

    with pytest.raises(_ReachedError):
        api_main.main()

    (routes,) = built
    assert routes.fake_login is not None
    assert routes.fake_login.mode is FakeLoginMode.PICK_MEMBER
    assert routes.allowed_origins == DEVELOPMENT_ORIGINS
    assert routes.cookie_max_age == 30 * 24 * 3600
    assert _startup_problems(capsys.readouterr().out) == []


def test_api_main_with_trial_fake_login_reaches_assembly_without_logging_admin_email(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_env(monkeypatch, TRIAL)

    class _Probes:
        ping = staticmethod(lambda: True)
        schema_state = staticmethod(lambda: None)
        database = object()

    def fake_build(_settings: Any, *, pool_size: int, migrations_dir: Path | None = None) -> Any:
        return _Probes()

    def fake_install(_app: Any, routes: Any) -> None:
        assert routes.fake_login.mode is FakeLoginMode.ACCESS_ADMIN
        assert list(routes.fake_login.list_members()) == []
        raise _ReachedError

    monkeypatch.setattr(api_main, "build_readiness_probes", fake_build)
    monkeypatch.setattr(api_main, "install_auth_routes", fake_install)

    with pytest.raises(_ReachedError):
        api_main.main()

    assert ADMIN_EMAIL not in capsys.readouterr().out
