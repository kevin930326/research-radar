"""起真的 api／worker 子行程，送 SIGTERM，確認退出碼 0 且日誌有「正常關機」。"""

import json
import select
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

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
        except urllib.error.URLError, ConnectionError:
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


def test_api_readyz_respects_connect_timeout() -> None:
    # 192.0.2.0/24 是保留給文件的位址（TEST-NET-1），不會有人回應；
    # 沒把連線逾時傳下去的話會等到作業系統的預設逾時。
    port = _free_port()
    process = _spawn(
        "radar.entrypoints.api",
        {
            "DATABASE_URL": "postgresql://radar@192.0.2.1:5432/radar",
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
