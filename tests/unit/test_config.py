import pytest
from pydantic import SecretStr

from radar.config import ConfigError, env_name, load_settings
from radar.domain.settings import Settings

DB_URL = "postgresql://radar:s3cret-PASSWORD@db:5432/radar"


def test_loads_database_url_as_secret_and_defaults() -> None:
    settings = load_settings({"DATABASE_URL": DB_URL})

    assert isinstance(settings.database_url, SecretStr)
    assert settings.database_url.get_secret_value() == DB_URL
    assert settings.api_port == 8000
    assert settings.api_shutdown_graceful_timeout == 10
    assert settings.worker_shutdown_graceful_timeout == 30


def test_secret_not_shown_in_repr_or_str() -> None:
    settings = load_settings({"DATABASE_URL": DB_URL})

    assert "s3cret-PASSWORD" not in repr(settings)
    assert "s3cret-PASSWORD" not in str(settings)
    assert "s3cret-PASSWORD" not in settings.model_dump_json()


def test_radar_prefixed_overrides() -> None:
    settings = load_settings(
        {
            "DATABASE_URL": DB_URL,
            "RADAR_API_PORT": "9123",
            "RADAR_WORKER_SHUTDOWN_GRACEFUL_TIMEOUT": "12",
        }
    )

    assert settings.api_port == 9123
    assert settings.worker_shutdown_graceful_timeout == 12


def test_missing_database_url_names_the_variable() -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings({})

    assert caught.value.problems == ["缺少環境變數 DATABASE_URL"]
    assert "DATABASE_URL" in str(caught.value)


def test_empty_value_counts_as_missing() -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings({"DATABASE_URL": ""})

    assert caught.value.problems == ["缺少環境變數 DATABASE_URL"]


def test_invalid_value_is_not_echoed() -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings({"DATABASE_URL": DB_URL, "RADAR_API_PORT": "SENTINEL-not-a-port"})

    message = str(caught.value)
    assert "RADAR_API_PORT" in message
    assert "SENTINEL" not in message
    assert "s3cret-PASSWORD" not in message


def test_unrelated_variables_are_ignored() -> None:
    settings = load_settings({"DATABASE_URL": DB_URL, "RADAR_UNKNOWN": "x", "HOME": "/tmp"})

    assert settings.api_port == 8000


def test_every_field_has_an_env_name() -> None:
    names = {env_name(field) for field in Settings.model_fields}

    assert "DATABASE_URL" in names
    assert all(name == "DATABASE_URL" or name.startswith("RADAR_") for name in names)


@pytest.mark.parametrize(
    "url",
    [
        "postgres://radar:s3cret-PASSWORD@db:5432/radar",
        "mysql://radar:s3cret-PASSWORD@db:5432/radar",
        "postgresql://radar:s3cret-PASSWORD@db:notaport/radar",
        "postgresql://radar:s3cret-PASSWORD@db:99999/radar",
        "postgresql:///radar-s3cret-PASSWORD",
    ],
)
def test_malformed_database_url_rejected_without_echo(url: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings({"DATABASE_URL": url})

    message = str(caught.value)
    assert "DATABASE_URL" in message
    assert "s3cret-PASSWORD" not in message
    assert "notaport" not in message


@pytest.mark.parametrize(
    "url",
    ["postgresql://radar@db:5432/radar", "postgresql+psycopg://radar@127.0.0.1/radar"],
)
def test_wellformed_database_url_accepted(url: str) -> None:
    assert load_settings({"DATABASE_URL": url}).database_url.get_secret_value() == url


# 主機名稱含 NFKC 正規化後會變的字元（℀）時，urlsplit 的例外訊息會帶出整段 netloc（含密碼）。
UNPARSEABLE_URL = "postgresql://radar:s3cret-PASSWORD@ho℀st:5432/radar"


def test_unparseable_database_url_hides_parser_message() -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings({"DATABASE_URL": UNPARSEABLE_URL})

    assert caught.value.problems == [
        "環境變數 DATABASE_URL 格式不對（Value error, 連線字串無法解析）"
    ]
    assert "s3cret-PASSWORD" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
