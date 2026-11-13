"""設定的逾時一路傳到資料庫驅動的連線參數（不真的連線）。"""

from typing import Any

import psycopg
import pytest

from radar.domain.settings import Settings
from radar.entrypoints.bootstrap import build_database_ping


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

    assert build_database_ping(settings)() is False
    assert captured["connect_timeout"] == 7
    assert captured["options"] == "-c statement_timeout=7000"
