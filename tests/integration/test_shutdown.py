"""起真的 api／worker 子行程，送 SIGTERM，確認退出碼 0 且日誌有「正常關機」。"""

import json
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# 連不上的資料庫位址：這些測試不需要資料庫。
UNREACHABLE_DB = "postgresql://radar@127.0.0.1:1/radar"
STARTUP_TIMEOUT = 20.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _spawn(module: str, env: dict[str, str]) -> subprocess.Popen[str]:
    # 刻意不繼承目前的環境變數，只給這次需要的。
    return subprocess.Popen(
        [sys.executable, "-m", module],
        env={"PYTHONUNBUFFERED": "1", **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _records(out: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _wait_http_ok(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"行程提早結束：{process.communicate()}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except urllib.error.URLError, ConnectionError, TimeoutError:
            time.sleep(0.1)
    process.kill()
    pytest.fail("api 沒有在時間內起來")


def _stop(
    process: subprocess.Popen[str], signum: signal.Signals = signal.SIGTERM
) -> tuple[int, str, str]:
    process.send_signal(signum)
    out, err = process.communicate(timeout=30)
    return process.returncode, out, err


def test_api_sigterm_exits_zero_and_logs_shutdown() -> None:
    port = _free_port()
    process = _spawn(
        "radar.entrypoints.api",
        {
            "DATABASE_URL": UNREACHABLE_DB,
            "RADAR_API_HOST": "127.0.0.1",
            "RADAR_API_PORT": str(port),
        },
    )
    _wait_http_ok(f"http://127.0.0.1:{port}/healthz", process)

    code, out, err = _stop(process)

    assert code == 0, err
    records = _records(out)
    assert any(r["event"] == "正常關機" and r["service"] == "api" for r in records)
    assert all("request_id" in r for r in records)
    assert err == ""


def test_api_readyz_503_when_database_unreachable() -> None:
    port = _free_port()
    process = _spawn(
        "radar.entrypoints.api",
        {
            "DATABASE_URL": UNREACHABLE_DB,
            "RADAR_API_HOST": "127.0.0.1",
            "RADAR_API_PORT": str(port),
        },
    )
    try:
        _wait_http_ok(f"http://127.0.0.1:{port}/healthz", process)
        started = time.monotonic()
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=10)
        assert caught.value.code == 503
        assert json.loads(caught.value.read()) == {"status": "not_ready"}
        assert time.monotonic() - started < 10
    finally:
        _code, out, _err = _stop(process)

    assert "127.0.0.1:1" not in out  # 連線字串不進日誌
    assert any(r["event"] == "資料庫連線失敗" for r in _records(out))


def test_api_readyz_respects_connect_timeout(silent_port: int) -> None:
    # 資料庫位址是本機一個只 listen、不回應的埠；
    # 沒把連線逾時傳下去的話 libpq 會一直等，/readyz 就不會在時間內回來。
    port = _free_port()
    process = _spawn(
        "radar.entrypoints.api",
        {
            "DATABASE_URL": f"postgresql://radar@127.0.0.1:{silent_port}/radar",
            "RADAR_DATABASE_CONNECT_TIMEOUT": "2",
            "RADAR_API_HOST": "127.0.0.1",
            "RADAR_API_PORT": str(port),
        },
    )
    try:
        _wait_http_ok(f"http://127.0.0.1:{port}/healthz", process)
        started = time.monotonic()
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=15)
        assert caught.value.code == 503
        assert time.monotonic() - started < 10
    finally:
        _stop(process)


# 掛一個測試用慢路由，其餘照正式的 main()（設定、訊號處理、uvicorn 設定）跑；
# 正式程式不需要為了測試加端點或開關。
SLOW_APP_SCRIPT = """
import asyncio
import sys
from pathlib import Path

import radar.entrypoints.api.main as api_main

marker = Path(sys.argv[1])
real_create_app = api_main.create_app


def create_app_with_slow_route(readiness_check):
    app = real_create_app(readiness_check)

    @app.get("/slow")
    async def slow():
        marker.write_text("started")
        await asyncio.sleep(2)
        return {"status": "done"}

    return app


api_main.create_app = create_app_with_slow_route
raise SystemExit(api_main.main())
"""


def test_api_sigterm_finishes_in_flight_request(tmp_path: Path) -> None:
    script = tmp_path / "slow_api.py"
    script.write_text(SLOW_APP_SCRIPT, encoding="utf-8")
    marker = tmp_path / "slow-started"
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-I", str(script), str(marker)],
        env={
            "PYTHONUNBUFFERED": "1",
            "DATABASE_URL": UNREACHABLE_DB,
            "RADAR_API_HOST": "127.0.0.1",
            "RADAR_API_PORT": str(port),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_http_ok(f"http://127.0.0.1:{port}/healthz", process)

    result: dict[str, object] = {}

    def call_slow() -> None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/slow", timeout=15) as response:
                result["status"] = response.status
                result["body"] = json.loads(response.read())
        except Exception as exc:  # 交給主執行緒的斷言回報
            result["error"] = repr(exc)

    caller = threading.Thread(target=call_slow)
    caller.start()
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not marker.exists():
        if time.monotonic() > deadline:
            process.kill()
            pytest.fail("慢請求沒有開始")
        time.sleep(0.05)

    code, out, err = _stop(process)
    caller.join(timeout=15)

    assert result == {"status": 200, "body": {"status": "done"}}
    assert code == 0, err
    assert any(r["event"] == "正常關機" and r["service"] == "api" for r in _records(out))


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_worker_signal_exits_zero_and_logs_shutdown(signum: signal.Signals) -> None:
    process = _spawn("radar.entrypoints.worker", {"DATABASE_URL": UNREACHABLE_DB})
    assert process.stdout is not None
    readable, _, _ = select.select([process.stdout], [], [], STARTUP_TIMEOUT)
    if not readable:
        process.kill()
        process.communicate()
        pytest.fail("worker 沒有在時間內起來")
    first = json.loads(process.stdout.readline())
    assert first["event"] == "worker 啟動"

    code, out, err = _stop(process, signum)

    assert code == 0, err
    records = _records(out)
    assert any(
        r["event"] == "正常關機" and r["service"] == "worker" and r["signal"] == signum.name
        for r in records
    )
    assert err == ""


@pytest.mark.parametrize("module", ["radar.entrypoints.api", "radar.entrypoints.worker"])
def test_missing_database_url_fails_without_printing_values(module: str) -> None:
    process = _spawn(module, {"RADAR_API_PORT": "SENTINEL-value"})
    out, err = process.communicate(timeout=30)

    assert process.returncode != 0
    assert "DATABASE_URL" in out
    assert "SENTINEL" not in out + err


@pytest.mark.parametrize("module", ["radar.entrypoints.api", "radar.entrypoints.worker"])
def test_unparseable_database_url_does_not_leak_password(module: str) -> None:
    # urlsplit 對含 ℀ 的主機名稱丟出的例外訊息會帶出帳密；啟動輸出不能出現它。
    process = _spawn(module, {"DATABASE_URL": "postgresql://radar:s3cret-PW@ho℀st:5432/radar"})
    out, err = process.communicate(timeout=30)

    assert process.returncode == 1
    assert "DATABASE_URL" in out
    assert "s3cret-PW" not in out + err
    assert err == ""
