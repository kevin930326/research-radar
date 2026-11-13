"""設定的逾時一路傳到資料庫驅動的連線參數（不真的連線）。"""

from typing import Any

import psycopg
import pytest

from radar.domain.settings import Settings
from radar.entrypoints.bootstrap import build_readiness_probes
from radar.repo.db import Database


def test_timeouts_reach_driver_connect_params(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_connect(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise psycopg.OperationalError("fake: not connecting")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    # 7 秒不是預設值（3），確認傳下去的是設定值而不是預設。
    settings = Settings.model_validate(
        {"database_url": "postgresql://radar@db:5432/radar", "database_connect_timeout": 7}
    )

    assert build_readiness_probes(settings, pool_size=1).ping() is False
    assert captured["connect_timeout"] == 7
    assert captured["options"] == "-c statement_timeout=7000"
    # 用戶端的 TCP 期限：網路卡住時也會在有限時間內失敗。
    assert captured["tcp_user_timeout"] == 7000
    assert captured["keepalives"] == 1
    assert captured["keepalives_idle"] == 7
    assert captured["keepalives_interval"] == 1
    assert captured["keepalives_count"] == 7


def test_existing_url_options_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_connect(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise psycopg.OperationalError("fake: not connecting")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    settings = Settings.model_validate(
        {
            "database_url": "postgresql://radar@db:5432/radar?options=-c%20role%3Dradar_app",
            "database_connect_timeout": 7,
        }
    )

    assert build_readiness_probes(settings, pool_size=1).ping() is False
    assert captured["options"] == "-c role=radar_app -c statement_timeout=7000"


def test_pool_size_reaches_readiness_pool() -> None:
    settings = Settings.model_validate({"database_url": "postgresql://radar_app@db:5432/radar"})

    probes = build_readiness_probes(settings, pool_size=5)
    database = probes.ping.__self__  # type: ignore[attr-defined]

    assert isinstance(database, Database)
    assert database._engine.pool.size() == 5  # type: ignore[attr-defined]
