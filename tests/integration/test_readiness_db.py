"""api／worker 起在還沒遷移的資料庫：服務不自己遷移（AD-15），`/readyz` 503；遷移後回 200。"""

import json
import select
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.database import ROOT, TestDatabase

BIN = Path(sys.executable).parent
STARTUP_TIMEOUT = 20.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _spawn(module: str, env: dict[str, str], cwd: Path = ROOT) -> subprocess.Popen[str]:
    # 刻意不繼承目前的環境變數，只給這次需要的。
    return subprocess.Popen(
        [sys.executable, "-m", module],
        env={"PYTHONUNBUFFERED": "1", **env},
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _records(out: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            status: int = response.status
            return status
    except urllib.error.HTTPError as error:
        return error.code


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


def _stop(process: subprocess.Popen[str]) -> tuple[int, str, str]:
    process.send_signal(signal.SIGTERM)
    out, err = process.communicate(timeout=30)
    return process.returncode, out, err


def test_api_not_ready_until_migrated_and_never_migrates_itself(
    fresh_database: TestDatabase,
) -> None:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    process = _spawn(
        "radar.entrypoints.api",
        {
            "DATABASE_URL": fresh_database.app_url,
            "RADAR_API_HOST": "127.0.0.1",
            "RADAR_API_PORT": str(port),
        },
    )
    try:
        _wait_http_ok(f"{base}/healthz", process)

        assert _status(f"{base}/readyz") == 503
        assert not fresh_database.has_table("alembic_version")

        migrate = subprocess.run(
            [str(BIN / "radar"), "migrate"],
            env={"PYTHONUNBUFFERED": "1", "RADAR_MIGRATION_DATABASE_URL": fresh_database.owner_url},
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert migrate.returncode == 0, migrate.stdout + migrate.stderr

        assert _status(f"{base}/readyz") == 200
        assert _status(f"{base}/healthz") == 200
    finally:
        code, out, err = _stop(process)

    assert code == 0, err
    records = _records(out)
    assert any(r.get("schema_state") == "not_migrated" for r in records)
    assert "radar_app@" not in out + err


def test_worker_start_and_stop_does_not_migrate(fresh_database: TestDatabase) -> None:
    process = _spawn("radar.entrypoints.worker", {"DATABASE_URL": fresh_database.app_url})
    assert process.stdout is not None
    readable, _, _ = select.select([process.stdout], [], [], STARTUP_TIMEOUT)
    if not readable:
        process.kill()
        process.communicate()
        pytest.fail("worker 沒有在時間內起來")
    assert json.loads(process.stdout.readline())["event"] == "worker 啟動"

    code, _out, err = _stop(process)

    assert code == 0, err
    assert not fresh_database.has_table("alembic_version")


def test_api_without_migrations_dir_exits_with_error(tmp_path: Path) -> None:
    process = _spawn(
        "radar.entrypoints.api",
        {
            "DATABASE_URL": "postgresql://radar_app@127.0.0.1:1/radar",
            "RADAR_API_HOST": "127.0.0.1",
            "RADAR_API_PORT": str(_free_port()),
            # 工作目錄換掉後領域設定也不在了；指回 repo 的 config/，才測得到「找不到遷移檔」。
            "RADAR_CONFIG_DIR": str(ROOT / "config"),
        },
        cwd=tmp_path,
    )
    out, err = process.communicate(timeout=30)

    assert process.returncode == 1, out + err
    assert any("找不到遷移檔" in str(r["event"]) for r in _records(out))
    assert err == ""
