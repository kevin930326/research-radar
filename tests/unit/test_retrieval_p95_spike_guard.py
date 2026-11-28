"""Story 4.1 效能量測腳本（`spikes/epic-4a-retrieval-p95/measure.py`）連線前的檢查（審查 M10）：
解析連線設定，只放行本機（127.0.0.1／localhost）的 55434；不連資料庫。

連線字串全是測試自己編的（沒有密碼）。
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

MEASURE = Path(__file__).resolve().parents[2] / "spikes" / "epic-4a-retrieval-p95" / "measure.py"


def _measure() -> ModuleType:
    spec = importlib.util.spec_from_file_location("epic_4a_retrieval_p95_measure", MEASURE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://postgres@127.0.0.1:55434/postgres",
        "postgresql://postgres@localhost:55434/postgres",
        "host=127.0.0.1 port=55434 user=postgres dbname=postgres",
    ],
)
def test_local_55434_is_allowed(url: str) -> None:
    _measure().require_local_test_database(url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://postgres@db.example.com:55434/postgres",  # 別台主機的 55434
        "postgresql://postgres@10.0.0.5:55434/postgres",
        "postgresql://postgres@127.0.0.1:5432/postgres",
        "postgresql://postgres@127.0.0.1/postgres?port=55434&host=db.example.com",
        "postgresql://postgres@:55434/postgres",  # 沒寫主機：libpq 用環境變數或預設值
        "postgresql://postgres@127.0.0.1/postgres",  # 沒寫埠
        "host=127.0.0.1 hostaddr=10.0.0.5 port=55434 dbname=postgres",
        "postgresql://postgres@127.0.0.1,db.example.com:55434/postgres",
        "not a = valid = conninfo",
    ],
)
def test_other_hosts_and_ports_stop_before_touching_anything(url: str) -> None:
    with pytest.raises(SystemExit):
        _measure().require_local_test_database(url)
