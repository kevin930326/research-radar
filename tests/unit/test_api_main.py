"""api 的 main() 把連線數預算交給就緒檢查的連線池（不起 uvicorn、不連資料庫）。"""

from pathlib import Path
from typing import Any

import pytest

import radar.entrypoints.api.main as api_main
from radar.entrypoints.bootstrap import MigrationsNotFoundError


def _record_pool_size(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    recorded: list[int] = []

    def fake_build(_settings: Any, *, pool_size: int, migrations_dir: Path | None = None) -> Any:
        recorded.append(pool_size)
        raise MigrationsNotFoundError("測試：不起 uvicorn")

    monkeypatch.setattr(api_main, "build_readiness_probes", fake_build)
    monkeypatch.setenv("DATABASE_URL", "postgresql://radar_app@db:5432/radar")
    return recorded


def test_api_uses_default_pool_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record_pool_size(monkeypatch)
    monkeypatch.delenv("RADAR_API_DATABASE_POOL_SIZE", raising=False)

    assert api_main.main() == 1
    assert recorded == [8]


def test_api_pool_size_override(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record_pool_size(monkeypatch)
    monkeypatch.setenv("RADAR_API_DATABASE_POOL_SIZE", "3")

    assert api_main.main() == 1
    assert recorded == [3]
