"""備份工具 adapter 對假的子程序（Story 3.18 審查 C1、C4、C5、T4、S10）。

`pg_dump`、`age`、`pg_restore`、`psql` 換成 pytest 臨時資料夾裡的小 Python 腳本（只寫位元組、讀
標準輸入、照指定的退出碼結束），`subprocess.Popen` 包一層記下每次的參數與環境。不連資料庫；
物件儲存用 moto（在行程裡模擬 S3，不連網路）。本機與 CI 都跑得到，不需要真的工具。

驗的事：
- 參數沒有密碼、主機、連線字串，`pg_dump` 不寫檔；子程序的環境只有 `PG*`（`age`、`pg_restore` 是
  空的），本行程的秘密（例如 `ANTHROPIC_API_KEY`）不會傳進去；
- `pg_dump`／`age` 已經輸出資料後才失敗：物件儲存沒有鍵、沒有未完成的分段上傳、子程序都結束；
- 錯誤訊息去掉主機、帳號、密碼、IP；
- 還原：送進 `psql` 的順序（BEGIN、還原前的檢查、備份的 SQL、COMMIT 前的檢查、COMMIT），
  COPY 資料以外的交易控制語句讓轉送停下、不送 COMMIT；
- 逾時：`pg_dump` 一直不開始輸出、還原的輸入一直不來，都會停下。
"""

import io
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from moto import mock_aws

import radar.adapters.backup_tools as backup_tools
from radar.adapters.backup_tools import EncryptedDump, restore_archive
from radar.adapters.object_store import S3ObjectStore, create_s3_client
from radar.domain.backup import (
    PG_DUMP_LOCK_WAIT_TIMEOUT_MS,
    BackupMode,
    BackupToolError,
    DumpRequest,
    RestoreArchiveUnsafeError,
    RestoreRefusedError,
    SharedOnlyBackupHasMemberRowsError,
)
from radar.domain.viewer import SystemViewer
from radar.repo.backup import get_restore_request

BUCKET = "radar-test"
KEY = "backup/20261118T120000Z-full.radar-backup.age"
RECIPIENT = "age1" + "q" * 58
HEADER = b"RADAR-BACKUP 1\n{}\n"
PG_ENV = {
    "PGHOST": "db.example",
    "PGPORT": "5432",
    "PGUSER": "radar_owner",
    "PGPASSWORD": "s3cret-PW",
    "PGDATABASE": "radar",
    "PGCONNECT_TIMEOUT": "5",
    "PGAPPNAME": "radar-test",
}
SECRETS = ("s3cret-PW", "db.example", "radar_owner", "10.1.2.3", "postgresql://", "sk-test")
LEAKY_STDERR = (
    'connection to server at "db.example" (10.1.2.3), port 5432 failed: FATAL: password'
    ' authentication failed for user "radar_owner" (s3cret-PW)\n'
)
NINE_MIB = 9 * 1024 * 1024


@dataclass
class Spawned:
    argv: list[str]
    kwargs: dict[str, Any]
    process: subprocess.Popen[bytes]

    @property
    def name(self) -> str:
        return Path(self.argv[0]).name


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[Spawned]:
    """包住 `subprocess.Popen`：照常啟動子程序，記下參數、環境與子程序本身。"""
    real = subprocess.Popen
    records: list[Spawned] = []

    def recording(args: Any, *rest: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = real(args, *rest, **kwargs)
        records.append(Spawned([str(a) for a in args], dict(kwargs), process))
        return process

    monkeypatch.setattr(subprocess, "Popen", recording)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return records


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return directory


@pytest.fixture
def store() -> Iterator[tuple[S3ObjectStore, Any]]:
    with mock_aws():
        client = create_s3_client(
            endpoint_url="https://s3.us-east-1.amazonaws.com",
            region="us-east-1",
            access_key_id="test-access-key",
            secret_access_key="test-secret-key",
            timeout=5,
        )
        client.create_bucket(Bucket=BUCKET)
        yield S3ObjectStore(client, BUCKET, attempts=3), client


def _tool(directory: Path, name: str, body: str) -> str:
    """寫一個假的工具（Python 腳本，`-I` 不讀任何環境變數）。"""
    path = directory / name
    path.write_text(f"#!{sys.executable} -I\nimport sys, time\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _writes(size: int, *, code: int = 0, stderr: str = "") -> str:
    """寫 `PGDMP` 加 `size` 個位元組到標準輸出，印 `stderr`，以 `code` 結束。"""
    return (
        "out = sys.stdout.buffer\n"
        "out.write(b'PGDMP')\n"
        f"left = {size}\n"
        "while left > 0:\n"
        "    n = min(left, 65536)\n"
        "    out.write(b'x' * n)\n"
        "    left -= n\n"
        "out.flush()\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({code})"
    )


def _passes_through(prefix: bytes = b"", *, code: int = 0) -> str:
    """標準輸入原樣（前面加 `prefix`）寫到標準輸出，以 `code` 結束。"""
    return (
        "out = sys.stdout.buffer\n"
        f"out.write({prefix!r})\n"
        "while chunk := sys.stdin.buffer.read1(65536):\n"
        "    out.write(chunk)\n"
        "out.flush()\n"
        f"sys.exit({code})"
    )


def _captures(path: Path, *, code: int = 0, stderr: str = "") -> str:
    """標準輸入邊讀邊寫進 `path`（假的 psql），印 `stderr`，以 `code` 結束。"""
    return (
        f"with open({str(path)!r}, 'wb') as f:\n"
        "    while chunk := sys.stdin.buffer.read1(65536):\n"
        "        f.write(chunk)\n"
        "        f.flush()\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({code})"
    )


def _dump_request() -> DumpRequest:
    return DumpRequest(
        snapshot_id="00000003-0000001B-1",
        header=HEADER,
        exclude_table_data=("member",),
        recipient=RECIPIENT,
    )


def _assert_no_secrets(spawned: list[Spawned]) -> None:
    for record in spawned:
        text = " ".join(record.argv)
        for secret in ("s3cret-PW", "db.example", "postgresql://"):
            assert secret not in text, (record.name, secret)
        env = record.kwargs["env"]
        assert env is not None, record.name  # 一定明確給環境，不繼承本行程的
        assert all(key.startswith("PG") for key in env), (record.name, sorted(env))
        assert "ANTHROPIC_API_KEY" not in env
        assert "sk-test" not in env.values()


def _ended(spawned: list[Spawned]) -> bool:
    return all(record.process.poll() is not None for record in spawned)


# --- 備份 -------------------------------------------------------------------------------------


def test_dump_arguments_and_environment_carry_no_secrets(
    spawned: list[Spawned], tmp_path: Path
) -> None:
    pg_dump = _tool(tmp_path, "pg_dump", _writes(1000))
    age = _tool(tmp_path, "age", _passes_through(b"age:"))

    with EncryptedDump(_dump_request(), pg_env=PG_ENV, pg_dump_path=pg_dump, age_path=age) as dump:
        output = dump.read()

    assert output == b"age:" + HEADER + b"PGDMP" + b"x" * 1000
    _assert_no_secrets(spawned)
    by_name = {record.name: record for record in spawned}
    dump_args = by_name["pg_dump"].argv
    assert not any(arg == "-f" or arg.startswith("--file") for arg in dump_args)
    assert "--no-large-objects" in dump_args
    assert "--no-statistics" in dump_args
    assert f"--lock-wait-timeout={PG_DUMP_LOCK_WAIT_TIMEOUT_MS}" in dump_args
    assert "--exclude-table-data=public.member" in dump_args
    assert by_name["pg_dump"].kwargs["stdout"] == subprocess.PIPE
    assert by_name["pg_dump"].kwargs["env"] == PG_ENV
    assert by_name["age"].kwargs["env"] == {}
    assert by_name["age"].argv[1:] == ["--encrypt", "--recipient", RECIPIENT]
    assert _ended(spawned)


@pytest.mark.parametrize(
    ("pg_dump_code", "age_code", "failed"),
    [(1, 0, "pg_dump 失敗（退出碼 1）"), (0, 1, "age 失敗（退出碼 1）")],
)
def test_late_failure_leaves_no_object_and_no_open_upload(
    spawned: list[Spawned],
    tmp_path: Path,
    store: tuple[S3ObjectStore, Any],
    pg_dump_code: int,
    age_code: int,
    failed: str,
) -> None:
    """已經輸出 9 MiB（第一段已經分段上傳）之後才失敗：上傳被放棄，不留鍵。"""
    s3, client = store
    pg_dump = _tool(tmp_path, "pg_dump", _writes(NINE_MIB, code=pg_dump_code, stderr=LEAKY_STDERR))
    age = _tool(tmp_path, "age", _passes_through(code=age_code))

    with (
        pytest.raises(BackupToolError, match=failed) as caught,
        EncryptedDump(_dump_request(), pg_env=PG_ENV, pg_dump_path=pg_dump, age_path=age) as dump,
    ):
        s3.put_stream(KEY, dump, content_type="application/octet-stream")

    assert client.list_objects_v2(Bucket=BUCKET, Prefix="backup/").get("KeyCount") == 0
    assert client.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []
    assert _ended(spawned)
    for secret in SECRETS:
        assert secret not in str(caught.value), secret


def test_pump_interrupted_when_age_stops_reading(
    spawned: list[Spawned], tmp_path: Path, store: tuple[S3ObjectStore, Any]
) -> None:
    """age 不讀輸入就結束：`pg_dump → age` 的轉送斷掉，上傳失敗、不留鍵。"""
    s3, client = store
    pg_dump = _tool(tmp_path, "pg_dump", _writes(NINE_MIB))
    age = _tool(tmp_path, "age", "sys.exit(0)")

    with (
        pytest.raises(BackupToolError),
        EncryptedDump(_dump_request(), pg_env=PG_ENV, pg_dump_path=pg_dump, age_path=age) as dump,
    ):
        s3.put_stream(KEY, dump, content_type="application/octet-stream")

    assert client.list_objects_v2(Bucket=BUCKET, Prefix="backup/").get("KeyCount") == 0
    assert _ended(spawned)


def test_close_stops_tools_that_are_still_running(spawned: list[Spawned], tmp_path: Path) -> None:
    pg_dump = _tool(
        tmp_path,
        "pg_dump",
        "sys.stdout.buffer.write(b'PGDMP')\nsys.stdout.buffer.flush()\ntime.sleep(60)",
    )
    age = _tool(tmp_path, "age", _passes_through())

    with EncryptedDump(_dump_request(), pg_env=PG_ENV, pg_dump_path=pg_dump, age_path=age):
        assert not _ended(spawned)

    assert _ended(spawned)


def test_pg_dump_that_never_starts_output_times_out(
    spawned: list[Spawned], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """審查 C4：例如排在別人的鎖後面。逾時就停掉兩個子程序，沒有備份（逾時在測試裡調小）。"""
    monkeypatch.setattr(backup_tools, "DUMP_START_TIMEOUT_SECONDS", 0.3)
    pg_dump = _tool(tmp_path, "pg_dump", "time.sleep(60)")
    age = _tool(tmp_path, "age", _passes_through())

    with pytest.raises(BackupToolError, match="沒有開始輸出"):
        EncryptedDump(_dump_request(), pg_env=PG_ENV, pg_dump_path=pg_dump, age_path=age)

    assert _ended(spawned)


# --- 還原 -------------------------------------------------------------------------------------

REQUEST = get_restore_request(SystemViewer(), mode=BackupMode.FULL, member_tables=("member",))


@pytest.fixture
def captured(tmp_path: Path) -> Path:
    return tmp_path / "psql-input.sql"


def _restore_tools(bin_dir: Path, captured: Path, *, psql_code: int = 0, stderr: str = "") -> None:
    _tool(bin_dir, "pg_restore", _passes_through())
    _tool(bin_dir, "psql", _captures(captured, code=psql_code, stderr=stderr))


def test_restore_sends_begin_guards_script_and_commit_in_order(
    spawned: list[Spawned], bin_dir: Path, captured: Path
) -> None:
    _restore_tools(bin_dir, captured)
    script = b"CREATE TABLE public.t (body text);\nCOPY public.t (body) FROM stdin;\nCOMMIT;\n\\.\n"

    restore_archive(REQUEST, io.BytesIO(script), pg_env=PG_ENV)

    assert captured.read_bytes() == (
        b"BEGIN;\n"
        + REQUEST.before_restore_sql.encode()
        + script
        + REQUEST.before_commit_sql.encode()
        + b"COMMIT;\n"
    )
    _assert_no_secrets(spawned)
    by_name = {record.name: record for record in spawned}
    assert by_name["pg_restore"].kwargs["env"] == {}
    assert by_name["pg_restore"].argv[1:] == ["--file=-"]
    assert by_name["psql"].kwargs["env"] == PG_ENV
    assert _ended(spawned)


@pytest.mark.parametrize(
    "script",
    [
        b"CREATE TABLE public.t (id int);\nBEGIN;\nSELECT pg_catalog.lo_open('1', 131072);\n",
        b"CREATE TABLE public.t (id int);\nCOMMIT;\n",
        b"SELECT 1;\n\\connect other\n",
    ],
)
def test_unsafe_script_stops_psql_without_commit(
    spawned: list[Spawned], bin_dir: Path, captured: Path, script: bytes
) -> None:
    """審查 C1：COPY 資料以外的交易控制語句（例如大型物件自己的 BEGIN／COMMIT）。"""
    _restore_tools(bin_dir, captured)

    with pytest.raises(RestoreArchiveUnsafeError, match="會自己 commit"):
        restore_archive(REQUEST, io.BytesIO(script), pg_env=PG_ENV)

    sent = captured.read_bytes() if captured.exists() else b""
    assert b"COMMIT" not in sent
    assert REQUEST.before_commit_sql.encode() not in sent
    assert _ended(spawned)


@pytest.mark.parametrize(
    "script",
    [
        b"CREATE TABLE public.t (id int);\n-- c\rCOMMIT;\nSELECT 1;\n",
        b"CREATE TABLE public.t (id int);\n-- c\r\nCOMMIT;\nSELECT 1;\n",
        b"CREATE TABLE public.t (id int);\n/* c */\rCOMMIT;\nSELECT 1;\n",
        b"CREATE TABLE public.t (id int);\nCOPY public.t (id) FROM stdin;\r\n1\r\n\\.\r\nCOMMIT;\n",
    ],
)
def test_commit_after_a_carriage_return_stops_psql_without_commit(
    spawned: list[Spawned], bin_dir: Path, captured: Path, script: bytes
) -> None:
    """審查第 2 輪 R1：PostgreSQL 的 `--` 註解在 CR 就結束；COPY 資料用 CRLF 結束。"""
    _restore_tools(bin_dir, captured)

    with pytest.raises(RestoreArchiveUnsafeError, match="COMMIT"):
        restore_archive(REQUEST, io.BytesIO(script), pg_env=PG_ENV)

    sent = captured.read_bytes() if captured.exists() else b""
    assert b"COMMIT" not in sent
    assert REQUEST.before_commit_sql.encode() not in sent
    assert _ended(spawned)
    psql = next(record for record in spawned if record.name == "psql")
    assert psql.process.returncode == -signal.SIGKILL  # 被停掉，不是讀到結尾自己結束


def test_crlf_comments_and_copy_data_with_commit_are_relayed_and_committed(
    spawned: list[Spawned], bin_dir: Path, captured: Path
) -> None:
    """註解裡、COPY 資料裡的 COMMIT（CRLF 換行）照樣轉送，最後才送 COMMIT。"""
    _restore_tools(bin_dir, captured)
    script = (
        b"-- COMMIT;\r\n/* COMMIT; */\r\nCREATE TABLE public.t (body text);\r\n"
        b"COPY public.t (body) FROM stdin;\r\nCOMMIT;\r\n\\.\rCOMMIT;\r\n\\.\r\nSELECT 1;\r\n"
    )

    restore_archive(REQUEST, io.BytesIO(script), pg_env=PG_ENV)

    assert captured.read_bytes() == (
        b"BEGIN;\n"
        + REQUEST.before_restore_sql.encode()
        + script
        + REQUEST.before_commit_sql.encode()
        + b"COMMIT;\n"
    )
    assert _ended(spawned)


def test_unfinished_script_is_not_committed(
    spawned: list[Spawned], bin_dir: Path, captured: Path
) -> None:
    _restore_tools(bin_dir, captured)

    with pytest.raises(BackupToolError, match="沒有在完整的一句結束"):
        restore_archive(REQUEST, io.BytesIO(b"SELECT 'never closed;\n"), pg_env=PG_ENV)

    sent = captured.read_bytes() if captured.exists() else b""
    assert b"COMMIT;\n" not in sent
    assert _ended(spawned)


def test_psql_failure_message_hides_connection_details(
    spawned: list[Spawned], bin_dir: Path, captured: Path
) -> None:
    _restore_tools(bin_dir, captured, psql_code=2, stderr=LEAKY_STDERR)

    with pytest.raises(BackupToolError, match="psql 失敗") as caught:
        restore_archive(REQUEST, io.BytesIO(b"SELECT 1;\n"), pg_env=PG_ENV)

    for secret in SECRETS:
        assert secret not in str(caught.value), secret
    assert "port 5432" in str(caught.value)


@pytest.mark.parametrize(
    ("sqlstate", "error", "message"),
    [
        ("RR001", SharedOnlyBackupHasMemberRowsError, "成員資料"),
        ("RR002", RestoreRefusedError, "還原開始時目標資料庫已經不是空的"),
        ("RR003", RestoreRefusedError, "還原期間有別的連線"),
        ("RR004", RestoreArchiveUnsafeError, "大型物件"),
        ("RR005", RestoreRefusedError, "別的程式連著目標資料庫"),
    ],
)
def test_guard_sqlstates_become_their_own_errors(
    spawned: list[Spawned],
    bin_dir: Path,
    captured: Path,
    sqlstate: str,
    error: type[Exception],
    message: str,
) -> None:
    _restore_tools(bin_dir, captured, psql_code=3, stderr=f"psql:<stdin>:9: ERROR:  {sqlstate}\n")

    with pytest.raises(error, match=message):
        restore_archive(REQUEST, io.BytesIO(b"SELECT 1;\n"), pg_env=PG_ENV)


def test_other_client_message_mentions_the_health_check(
    spawned: list[Spawned], bin_dir: Path, captured: Path
) -> None:
    """審查第 3 輪 P1：資料庫的健康檢查可能剛好連進來；訊息說明目標仍是空的、可以直接重跑。"""
    _restore_tools(bin_dir, captured, psql_code=3, stderr="psql:<stdin>:9: ERROR:  RR005\n")

    with pytest.raises(RestoreRefusedError) as caught:
        restore_archive(REQUEST, io.BytesIO(b"SELECT 1;\n"), pg_env=PG_ENV)

    message = str(caught.value)
    assert "可能是資料庫的健康檢查剛好連進來；目標仍是空的，可以直接重跑" in message
    for secret in SECRETS:
        assert secret not in message, secret


def test_stalled_restore_input_times_out_without_commit(
    spawned: list[Spawned], bin_dir: Path, captured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """審查 C4：輸入送了一段之後就不再來、也不關（例如 SSH 斷了沒有 EOF）。逾時在測試裡調小。"""
    monkeypatch.setattr(backup_tools, "RESTORE_INPUT_IDLE_TIMEOUT_SECONDS", 0.3)
    _restore_tools(bin_dir, captured)
    read_end, write_end = os.pipe()
    os.write(write_end, b"SELECT 1;\n")
    source = os.fdopen(read_end, "rb")
    try:
        with pytest.raises(BackupToolError, match="沒有收到輸入"):
            restore_archive(REQUEST, source, pg_env=PG_ENV)
    finally:
        # 先關寫的一端，卡在讀的執行緒讀到結尾，才關讀的一端。
        os.close(write_end)
        source.close()

    sent = captured.read_bytes() if captured.exists() else b""
    assert b"COMMIT" not in sent
    assert _ended(spawned)
