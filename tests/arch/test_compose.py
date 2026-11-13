"""compose.yaml 的記憶體上限、關機等待時間、日誌上限與秘密規則（ADR-0003 決定第 10 點）。"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from radar.config import env_name
from radar.domain.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "compose.yaml"
TOTAL_LIMIT_MB = 750
SERVICES = ("db", "rustfs", "api", "worker")
# 服務 → 它的 graceful timeout 設定欄位。
GRACEFUL_FIELDS = {
    "api": "api_shutdown_graceful_timeout",
    "worker": "worker_shutdown_graceful_timeout",
}


def _compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return data


def _services() -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = _compose()["services"]
    return services


def _megabytes(value: str) -> float:
    match = re.fullmatch(r"(\d+)([kmg])b?", str(value).lower())
    assert match, value
    number, unit = int(match[1]), match[2]
    return number * {"k": 1 / 1024, "m": 1, "g": 1024}[unit]


def _seconds(value: str) -> float:
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", str(value))
    assert parts, value
    assert "".join(n + u for n, u in parts) == str(value), value
    scale = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    return sum(float(n) * scale[u] for n, u in parts)


def _graceful_timeout(service: dict[str, Any], field: str) -> float:
    """compose 有覆寫就用覆寫值，否則用設定的預設值。"""
    environment = service.get("environment") or {}
    assert isinstance(environment, dict), "environment 請用對照表寫法"
    override = environment.get(env_name(field))
    if override is not None:
        return float(override)
    default = Settings.model_fields[field].default
    assert isinstance(default, int)
    return float(default)


def test_four_services() -> None:
    assert set(_services()) == set(SERVICES)


def test_memory_limits() -> None:
    services = _services()

    assert _megabytes(services["api"]["mem_limit"]) == 350
    assert _megabytes(services["worker"]["mem_limit"]) == 400
    total = _megabytes(services["api"]["mem_limit"]) + _megabytes(services["worker"]["mem_limit"])
    assert total <= TOTAL_LIMIT_MB


@pytest.mark.parametrize("name", SERVICES)
def test_log_size_and_count_capped(name: str) -> None:
    logging_config = _services()[name]["logging"]

    assert logging_config["driver"] == "json-file"
    assert logging_config["options"]["max-size"] == "10m"
    assert logging_config["options"]["max-file"] == "3"


@pytest.mark.parametrize(("name", "field"), GRACEFUL_FIELDS.items())
def test_stop_grace_period_longer_than_graceful_timeout(name: str, field: str) -> None:
    service = _services()[name]

    assert _seconds(service["stop_grace_period"]) > _graceful_timeout(service, field)


def test_seconds_parser() -> None:
    assert _seconds("45s") == 45
    assert _seconds("1m30s") == 90


def test_no_env_file_or_interpolation() -> None:
    raw = COMPOSE_PATH.read_text(encoding="utf-8")

    assert all("env_file" not in service for service in _services().values())
    assert "${" not in raw
    assert not re.search(r"\$[A-Za-z_]", raw)


def test_published_ports_bind_localhost_only() -> None:
    for name, service in _services().items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)


def test_database_uses_no_password() -> None:
    raw = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "POSTGRES_PASSWORD" not in raw
    assert _services()["db"]["environment"]["POSTGRES_HOST_AUTH_METHOD"] == "trust"


def test_image_excludes_env_files() -> None:
    rules = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert rules[0] == "*"  # 白名單：預設全部排除
    assert ".env" in rules
    assert ".env.*" in rules
    assert not any(rule.startswith("!") and ".env" in rule for rule in rules)
