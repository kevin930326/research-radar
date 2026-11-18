"""備份與還原的外部工具：`pg_dump`、`age`、`pg_restore`、`psql`（Story 3.18、AD-28、ADR-0051）。

- 子程序一律用清單傳參數、不經 shell；執行檔用 `shutil.which` 找到的絕對路徑。
- 子程序的環境只有 libpq 的連線變數（密碼在 `PGPASSWORD`，不在命令列參數，`ps` 看不到），不繼承
  這個行程的環境（裡面有 API key 等秘密）；`age` 與 `pg_restore`（不連資料庫）的環境是空的。
- 加密只用公鑰（`age --recipient`）；這裡沒有、也不需要私鑰。
- 沒加密的傾印不落地：`pg_dump` 的標準輸出經一條執行緒直接寫進 `age` 的標準輸入，加密後的輸出由
  呼叫的一方（上傳）讀走；中間只有記憶體裡的一小段。
- 錯誤訊息裡的 stderr 只取最後幾行，並把連線的主機、帳號、資料庫名稱、密碼換成 `…`。
"""

import io
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterable, Mapping
from contextlib import suppress
from io import Reader
from types import TracebackType
from typing import IO, Self, cast

from radar.domain.backup import (
    BackupToolError,
    DumpRequest,
    RestoreRequest,
    SharedOnlyBackupHasMemberRowsError,
)

_CHUNK = 64 * 1024
_STDERR_TAIL_LINES = 20
_STDERR_TAIL_CHARS = 2000
_VERSION_TIMEOUT_SECONDS = 30
# 擴充套件由管理者在目標資料庫先建好（非超級使用者建不了 vector）；備份不帶 CREATE EXTENSION。
_EXCLUDED_EXTENSIONS = ("vector", "pg_trgm")
# 只有共用資料的備份還原時，commit 前檢查成員資料表沒有任何一列；有就用這個 SQLSTATE 退回。
MEMBER_ROWS_SQLSTATE = "RR001"
_POSTGRES_VERSION = re.compile(r"\(PostgreSQL\)\s+([0-9]+(?:\.[0-9]+)*)")


def find_tool(name: str) -> str:
    """執行檔的絕對路徑。

    Raises:
        BackupToolError: 找不到（映像裡要有 PostgreSQL 18 的用戶端工具與 age）。
    """
    path = shutil.which(name)
    if path is None:
        raise BackupToolError(name, "找不到：映像裡要有 PostgreSQL 18 的用戶端工具與 age")
    return path


def postgres_tool_version(name: str) -> str:
    """`pg_dump --version` 這類輸出（`pg_dump (PostgreSQL) 18.7 (Debian …)`）的版本（`18.7`）。

    Raises:
        BackupToolError: 找不到、執行失敗或認不出版本。
    """
    try:
        result = subprocess.run(  # 參數是清單、不經 shell
            [find_tool(name), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={},
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupToolError(name, f"無法執行（{type(exc).__name__}）") from None
    match = _POSTGRES_VERSION.search(result.stdout.decode("utf-8", "replace"))
    if result.returncode != 0 or match is None:
        raise BackupToolError(name, "認不出版本")
    return match.group(1)


def _redact(text: str, secrets: Iterable[str]) -> str:
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "…")
    return text


def _stderr_tail(file: IO[bytes], secrets: Iterable[str]) -> str:
    """子程序 stderr 的最後幾行，去掉連線資訊。"""
    file.seek(0)
    lines = file.read().decode("utf-8", "replace").strip().splitlines()
    tail = " / ".join(line.strip() for line in lines[-_STDERR_TAIL_LINES:] if line.strip())
    return _redact(tail, secrets)[-_STDERR_TAIL_CHARS:]


def _connection_secrets(pg_env: Mapping[str, str]) -> list[str]:
    return [
        pg_env.get(name, "")
        for name in ("PGHOST", "PGHOSTADDR", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGPORT")
    ]


def _read1(stream: IO[bytes], size: int) -> bytes:
    """讀到一些就回來（最多 `size`，結尾回 b""）。Popen 的管線預設是 `BufferedReader`。"""
    return cast(io.BufferedReader, stream).read1(size)


def _kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


class EncryptedDump:
    """`pg_dump` → `age` 的加密串流（`with` 區塊結束時，還在跑的子程序一律停掉）。

    建立時等到 `pg_dump` 寫出第一段輸出（或結束）才回來：那時它已經匯入了 `--snapshot` 的快照，
    呼叫端可以結束匯出快照的交易，再開始上傳（不在開著的交易裡呼叫外部服務）。

    `read` 讀到結尾時才確認兩個子程序都成功結束；任何一個失敗就丟 `BackupToolError`，所以讀的一方
    （物件儲存的上傳）在完成之前就失敗，不會留下內容不完整的備份檔。最後一段資料也要確認完才交出。
    """

    def __init__(
        self,
        request: DumpRequest,
        *,
        pg_env: Mapping[str, str],
        pg_dump_path: str,
        age_path: str,
    ) -> None:
        self._secrets = _connection_secrets(pg_env)
        # 兩個 stderr 檔跟著這個物件活到 close()，不能用 with。
        self._pg_dump_err = tempfile.TemporaryFile()  # noqa: SIM115
        self._age_err = tempfile.TemporaryFile()  # noqa: SIM115
        self._finished = False
        self._pump_error: BaseException | None = None
        self._started = threading.Event()
        arguments = [
            pg_dump_path,
            "--format=custom",
            "--no-password",
            f"--snapshot={request.snapshot_id}",
            *(f"--exclude-extension={name}" for name in _EXCLUDED_EXTENSIONS),
            *(f"--exclude-table-data=public.{table}" for table in request.exclude_table_data),
        ]
        self._pg_dump = subprocess.Popen(  # 參數是清單、不經 shell
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=self._pg_dump_err,
            env=dict(pg_env),
        )
        try:
            self._age = subprocess.Popen(  # 參數是清單、不經 shell
                [age_path, "--encrypt", "--recipient", request.recipient],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._age_err,
                env={},
            )
        except BaseException:
            _kill(self._pg_dump)
            self._close_files()
            raise
        self._thread = threading.Thread(
            target=self._pump, args=(request.header,), name="backup-pump", daemon=True
        )
        self._thread.start()
        self._started.wait()

    def _pump(self, header: bytes) -> None:
        """`pg_dump` 的輸出前面加上 `header`，寫進 `age`；任何一邊斷掉就停，兩邊都關掉。"""
        source = self._pg_dump.stdout
        target = self._age.stdin
        assert source is not None
        assert target is not None
        try:
            chunk = _read1(source, _CHUNK)
            self._started.set()
            target.write(header)
            while chunk:
                target.write(chunk)
                chunk = _read1(source, _CHUNK)
        except BaseException as exc:
            self._pump_error = exc
        finally:
            self._started.set()
            for stream in (target, source):
                with suppress(OSError):
                    stream.close()

    def read(self, size: int = -1, /) -> bytes:
        """讀加密後的位元組；`size` 是正數時讀滿 `size` 或讀到結尾才回來。

        Raises:
            BackupToolError: 讀到結尾時發現 `pg_dump` 或 `age` 失敗。
        """
        if self._finished:
            return b""
        output = self._age.stdout
        assert output is not None
        buffer = bytearray()
        while size < 0 or len(buffer) < size:
            want = _CHUNK if size < 0 else min(_CHUNK, size - len(buffer))
            chunk = _read1(output, want)
            if not chunk:
                self._check_success()
                self._finished = True
                break
            buffer += chunk
        return bytes(buffer)

    def _check_success(self) -> None:
        self._thread.join()
        dump_code = self._pg_dump.wait()
        age_code = self._age.wait()
        if dump_code != 0:
            raise BackupToolError(
                "pg_dump",
                f"失敗（退出碼 {dump_code}）",
                _stderr_tail(self._pg_dump_err, self._secrets),
            )
        if age_code != 0:
            raise BackupToolError(
                "age", f"失敗（退出碼 {age_code}）", _stderr_tail(self._age_err, ())
            )
        if self._pump_error is not None:
            raise BackupToolError("pg_dump → age", f"中斷（{type(self._pump_error).__name__}）")

    def close(self) -> None:
        """停掉還在跑的子程序、關掉暫存的 stderr 檔。可以重複呼叫。"""
        for process in (self._pg_dump, self._age):
            _kill(process)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    with suppress(OSError):
                        stream.close()
        self._thread.join(timeout=5)
        self._close_files()

    def _close_files(self) -> None:
        self._pg_dump_err.close()
        self._age_err.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def start_encrypted_dump(request: DumpRequest, *, pg_env: Mapping[str, str]) -> EncryptedDump:
    """開始一次加密傾印（等到 `pg_dump` 開始輸出才回來）。

    Raises:
        BackupToolError: 找不到 `pg_dump` 或 `age`。
    """
    return EncryptedDump(
        request,
        pg_env=pg_env,
        pg_dump_path=find_tool("pg_dump"),
        age_path=find_tool("age"),
    )


def member_rows_guard_sql(tables: Iterable[str]) -> str:
    """commit 前的檢查：這些表（存在的話）只要有任何一列，就用 `MEMBER_ROWS_SQLSTATE` 退回。

    傾印會把 `search_path` 設成空的，所以表名一律寫 `public.`；表名已經過小寫識別字的檢查。
    """
    names = ", ".join(f"'{table}'" for table in tables)
    return (
        "DO $radar_guard$ DECLARE t text; has_rows boolean; BEGIN"
        f" FOREACH t IN ARRAY ARRAY[{names}]::text[] LOOP"
        " IF to_regclass(format('public.%I', t)) IS NOT NULL THEN"
        " EXECUTE format('SELECT EXISTS (SELECT 1 FROM public.%I)', t) INTO has_rows;"
        " IF has_rows THEN"
        " RAISE EXCEPTION 'radar restore: member table has rows'"
        f" USING ERRCODE = '{MEMBER_ROWS_SQLSTATE}';"
        " END IF; END IF; END LOOP; END $radar_guard$;\n"
    )


def restore_archive(
    request: RestoreRequest, source: Reader[bytes], *, pg_env: Mapping[str, str]
) -> None:
    """把 `pg_dump --format=custom` 的串流還原到 `pg_env` 指的資料庫，整個在一個交易裡。

    `source` → `pg_restore --file=-`（不連資料庫，只把備份換成 SQL）→ 這裡 → `psql`。送進 `psql` 的
    第一句是 `BEGIN`；`pg_restore` 成功結束、讀完整個輸入之後，才送出成員資料表的檢查（只有共用資料
    的備份）與 `COMMIT`。任何一步失敗都不送 `COMMIT`：`psql` 結束時交易退回，目標資料庫不留下任何
    東西（不是事後刪除）。`psql` 用 `VERBOSITY=sqlstate`，錯誤只印 SQLSTATE，不印資料內容。

    Raises:
        SharedOnlyBackupHasMemberRowsError: 只有共用資料的備份裡成員資料表有資料（已退回）。
        BackupToolError: 找不到工具，或 `pg_restore`、`psql` 失敗、輸入讀到一半出錯（已退回）。
    """
    secrets = _connection_secrets(pg_env)
    pg_restore_path = find_tool("pg_restore")
    psql_path = find_tool("psql")
    with (
        tempfile.TemporaryFile() as restore_err,
        tempfile.TemporaryFile() as psql_err,
    ):
        pg_restore = subprocess.Popen(  # 參數是清單、不經 shell
            [pg_restore_path, "--file=-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=restore_err,
            env={},
        )
        try:
            psql = subprocess.Popen(  # 參數是清單、不經 shell
                [
                    psql_path,
                    "--no-psqlrc",
                    "--quiet",
                    "--no-password",
                    "--set=ON_ERROR_STOP=1",
                    "--set=VERBOSITY=sqlstate",
                    "--file=-",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=psql_err,
                env=dict(pg_env),
            )
        except BaseException:
            _kill(pg_restore)
            raise
        feeder = _Feeder(source, pg_restore)
        try:
            committed = _relay(request, pg_restore, psql, feeder)
        finally:
            for process in (pg_restore, psql):
                _kill(process)
            # 失敗時 feeder 可能還卡在讀輸入；它是 daemon 執行緒，不無限期等它。
            feeder.join(timeout=5)
        psql_code = psql.returncode
        restore_code = pg_restore.returncode
        if psql_code != 0:
            detail = _stderr_tail(psql_err, secrets)
            if MEMBER_ROWS_SQLSTATE in detail:
                raise SharedOnlyBackupHasMemberRowsError
            raise BackupToolError("psql", f"失敗（退出碼 {psql_code}），交易已退回", detail)
        if restore_code != 0:
            raise BackupToolError(
                "pg_restore",
                f"失敗（退出碼 {restore_code}），交易已退回",
                _stderr_tail(restore_err, ()),
            )
        if feeder.error is not None:
            raise BackupToolError(
                "radar restore",
                f"讀輸入或送進 pg_restore 時中斷（{type(feeder.error).__name__}），交易已退回",
            )
        if not committed:
            raise BackupToolError("psql", "沒有完成，交易已退回")


class _Feeder:
    """把還原的輸入送進 `pg_restore` 的標準輸入（另一條執行緒）。"""

    def __init__(self, source: Reader[bytes], pg_restore: subprocess.Popen[bytes]) -> None:
        self.error: BaseException | None = None
        self._source = source
        self._target = pg_restore.stdin
        self._thread = threading.Thread(target=self._run, name="restore-feed", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        target = self._target
        assert target is not None
        try:
            while chunk := self._source.read(_CHUNK):
                target.write(chunk)
        except BaseException as exc:
            self.error = exc
        finally:
            with suppress(OSError):
                target.close()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)


def _relay(
    request: RestoreRequest,
    pg_restore: subprocess.Popen[bytes],
    psql: subprocess.Popen[bytes],
    feeder: _Feeder,
) -> bool:
    """`pg_restore` 的輸出轉給 `psql`，前面加 `BEGIN`；都成功才加檢查與 `COMMIT`。回有沒有送出
    `COMMIT` 而且 `psql` 成功結束。"""
    source = pg_restore.stdout
    target = psql.stdin
    assert source is not None
    assert target is not None
    try:
        target.write(b"BEGIN;\n")
        while chunk := _read1(source, _CHUNK):
            target.write(chunk)
        source.close()
        feeder.join()
        if pg_restore.wait() != 0 or feeder.error is not None:
            target.close()
            psql.wait()
            return False
        if request.member_tables_must_be_empty:
            target.write(member_rows_guard_sql(request.member_tables_must_be_empty).encode())
        target.write(b"COMMIT;\n")
        target.close()
    except BrokenPipeError:
        # psql 已經因為錯誤結束（ON_ERROR_STOP），交易已退回；退出碼與 stderr 說明原因。
        psql.wait()
        return False
    return psql.wait() == 0
