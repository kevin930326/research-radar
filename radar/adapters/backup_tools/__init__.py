"""備份與還原的外部工具：`pg_dump`、`age`、`pg_restore`、`psql`（Story 3.18、AD-28、ADR-0051）。

- 子程序一律用清單傳參數、不經 shell；執行檔用 `shutil.which` 找到的絕對路徑。
- 子程序的環境只有 libpq 的連線變數（密碼在 `PGPASSWORD`，不在命令列參數，`ps` 看不到），不繼承
  這個行程的環境（裡面有 API key 等秘密）；`age` 與 `pg_restore`（不連資料庫）的環境是空的。
- 加密只用公鑰（`age --recipient`）；這裡沒有、也不需要私鑰。
- 沒加密的傾印不落地：`pg_dump` 的標準輸出經一條執行緒直接寫進 `age` 的標準輸入，加密後的輸出由
  呼叫的一方（上傳）讀走；中間只有記憶體裡的一小段。
- 錯誤訊息裡的 stderr 只取最後幾行，把連線的密碼、帳號、主機、IP 位址、資料庫名稱（完整單字）
  換成 `…`；埠號不遮。
- 逾時（審查 C4）：`pg_dump` 等鎖有上限、等它開始輸出有上限；還原時輸入多久沒有新資料就放棄。
- 還原轉送（審查 C1）：`pg_restore` 產生的 SQL 逐行檢查，COPY 資料以外出現交易控制語句（例如
  大型物件自己的 `BEGIN;`／`COMMIT;`）或 `\\connect` 就停下，那一行不送進 `psql`，也不送 `COMMIT`。
"""

import io
import ipaddress
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from io import Reader
from types import TracebackType
from typing import IO, Self, cast

from radar.domain.backup import (
    DUMP_START_TIMEOUT_SECONDS,
    FOREIGN_RELATION_SQLSTATE,
    LARGE_OBJECT_SQLSTATE,
    MEMBER_ROWS_SQLSTATE,
    OTHER_CLIENT_SQLSTATE,
    PG_DUMP_LOCK_WAIT_TIMEOUT_MS,
    RESTORE_INPUT_IDLE_TIMEOUT_SECONDS,
    TARGET_NOT_EMPTY_SQLSTATE,
    BackupToolError,
    DumpRequest,
    RestoreArchiveUnsafeError,
    RestoreRefusedError,
    RestoreRequest,
    SharedOnlyBackupHasMemberRowsError,
)

_CHUNK = 64 * 1024
_STDERR_TAIL_LINES = 20
_STDERR_TAIL_CHARS = 2000
_VERSION_TIMEOUT_SECONDS = 30
# 擴充套件由管理者在目標資料庫先建好（非超級使用者建不了 vector）；備份不帶 CREATE EXTENSION。
_EXCLUDED_EXTENSIONS = ("vector", "pg_trgm")
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
    """把 `secrets` 的每一個（任何位置出現的）換成 `…`；長的先換。"""
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "…")
    return text


def _redact_words(text: str, values: Iterable[str]) -> str:
    """`values` 只在前後不是英數字或底線時（完整的一個詞）換成 `…`；長的先換。"""
    for value in sorted({v for v in values if v}, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(value)}(?!\w)", "…", text)
    return text


# IPv6 的候選（至少兩個冒號）；再用 `ipaddress` 確認真的是位址，時間（`12:00:05`）不算。
_IPV6_CANDIDATE = re.compile(
    r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{0,4}:){2,7}(?:[0-9A-Fa-f]{1,4}|(?:\d{1,3}\.){3}\d{1,3})?"
    r"(?![0-9A-Za-z:])"
)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d])")


def _redact_ip(match: re.Match[str]) -> str:
    try:
        ipaddress.IPv6Address(match.group(0))
    except ValueError:
        return match.group(0)
    return "…"


def redact_connection_details(text: str, pg_env: Mapping[str, str]) -> str:
    """錯誤訊息去掉連線資訊（審查 S10）：密碼（任何位置）、帳號與主機（完整的詞）、IPv4／IPv6
    位址、資料庫名稱（完整的詞）換成 `…`。埠號不遮（不是秘密，遮了訊息會難讀）。"""
    text = _redact(text, [pg_env.get("PGPASSWORD", "")])
    text = _redact_words(text, [pg_env.get(name, "") for name in ("PGHOST", "PGHOSTADDR")])
    text = _redact_words(text, [pg_env.get("PGUSER", "")])
    text = _IPV6_CANDIDATE.sub(_redact_ip, text)
    text = _IPV4.sub("…", text)
    return _redact_words(text, [pg_env.get("PGDATABASE", "")])


def _stderr_tail(file: IO[bytes], pg_env: Mapping[str, str]) -> str:
    """子程序 stderr 的最後幾行，去掉連線資訊。"""
    file.seek(0)
    lines = file.read().decode("utf-8", "replace").strip().splitlines()
    tail = " / ".join(line.strip() for line in lines[-_STDERR_TAIL_LINES:] if line.strip())
    return redact_connection_details(tail, pg_env)[-_STDERR_TAIL_CHARS:]


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
    呼叫端可以結束匯出快照的交易，再開始上傳（不在開著的交易裡呼叫外部服務）。超過
    `DUMP_START_TIMEOUT_SECONDS` 還沒開始輸出就停掉兩個子程序、丟 `BackupToolError`。

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
        self._pg_env = dict(pg_env)
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
            f"--lock-wait-timeout={PG_DUMP_LOCK_WAIT_TIMEOUT_MS}",
            # 大型物件不備份（審查 C1、C3）：還原時它們會自己 BEGIN／COMMIT，而且 `--shared-only`
            # 的 `--exclude-table-data` 管不到它們。radar 不用大型物件。
            "--no-large-objects",
            # 統計資料不備份（審查 S5）：成員表的統計（例如常見值）可能有 email。
            "--no-statistics",
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
        if not self._started.wait(timeout=DUMP_START_TIMEOUT_SECONDS):
            self.close()
            raise BackupToolError(
                "pg_dump",
                f"超過 {DUMP_START_TIMEOUT_SECONDS} 秒沒有開始輸出（可能在等資料表的鎖或連線），"
                "已停止；沒有備份",
            )

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
                _stderr_tail(self._pg_dump_err, self._pg_env),
            )
        if age_code != 0:
            raise BackupToolError(
                "age", f"失敗（退出碼 {age_code}）", _stderr_tail(self._age_err, {})
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


# --- 還原 -------------------------------------------------------------------------------------

# COPY 資料以外，句子的第一個詞是這些就是交易控制語句（會提早 commit 或結束我們送的 BEGIN）。
_TRANSACTION_WORDS = frozenset(
    {"begin", "commit", "rollback", "end", "abort", "savepoint", "release"}
)
_TRANSACTION_PAIRS = frozenset({("start", "transaction"), ("prepare", "transaction")})
_SPECIAL = re.compile(rb"[A-Za-z_\x80-\xff0-9$'\"\-/();\\]")
_IDENTIFIER_TOKEN = re.compile(rb"[A-Za-z_\x80-\xff][A-Za-z0-9_$\x80-\xff]*")
# 數字與 `$1` 後面緊接著識別字時，PostgreSQL 18 的掃描器把整串當成一個（不合法的）記號
# （`decinteger_junk`、`param_junk`），例如 `1E'…'` 的 `'` 是一般字串、不是 `E'…'`。
_JUNK_SUFFIX = rb"(?:[A-Za-z_\x80-\xff][A-Za-z0-9_$\x80-\xff]*)?"
_NUMBER_TOKEN = re.compile(rb"[0-9][0-9_]*(?:\.[0-9_]*)?(?:[eE][+-]?[0-9]+)?" + _JUNK_SUFFIX)
_DOLLAR_QUOTE = re.compile(rb"\$(?:[A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*)?\$")
_POSITIONAL_PARAMETER = re.compile(rb"\$[0-9][0-9_]*" + _JUNK_SUFFIX)
# psql 的反斜線指令名稱：到空白或下一個反斜線為止（psqlscanslash.l）。
_META_COMMAND = re.compile(rb"\\([^\s\\]*)")
# 准許的反斜線指令只有 `pg_restore` 18 自己寫的這兩種整行（金鑰是英數字）；同一行不准有別的東西
# （psql 在 `\\` 之後會把同一行剩下的當 SQL 執行）。
_RESTRICT_LINE = re.compile(rb"\\(?:un)?restrict [A-Za-z0-9]+\r?\n?")
# `--` 註解在 CR 或 LF 結束（PostgreSQL 18 的 scan.l 與 psqlscan.l：`newline [\n\r]`）。
_NEWLINE = re.compile(rb"[\r\n]")
# COPY 資料的結尾：psql 18 只認整行剛好是 `\.` 加 LF 或 CRLF（copy.c 的 handleCopyIn）。
_COPY_END_LINES = (b"\\.\n", b"\\.\r\n")
_E_STRING_STOP = re.compile(rb"[\\']")
_COMMENT_TOKEN = re.compile(rb"/\*|\*/")
# psql 判斷 `CREATE [OR REPLACE] FUNCTION|PROCEDURE … BEGIN ATOMIC … END` 用的前幾個詞。
_DEFINITION_WORDS = frozenset({"create", "function", "procedure", "or", "replace"})


class _SqlScriptGuard:
    r"""逐行檢查 `pg_restore --file=-` 產生的 SQL（審查 C1）：COPY 資料以外不准有交易控制語句。

    大型物件的還原會自己寫 `BEGIN;`…`COMMIT;`，那個 `COMMIT` 會把我們送的外層交易提早 commit，
    之後的檢查與退回就沒有用了。這裡照 psql 自己切句子的方式（單引號、`E'…'`、雙引號、`$tag$`、
    `--` 與巢狀 `/* */` 註解、括號、`CREATE FUNCTION … BEGIN ATOMIC … END`）找每一句的第一個詞；
    `COPY … FROM stdin;` 之後到單獨一行 `\.` 是 COPY 資料，不檢查（資料裡剛好有一行 `COMMIT;`
    不算）。不准的寫法丟 `RestoreArchiveUnsafeError`，那一行還沒送進 `psql`。

    行尾照 PostgreSQL 18（審查第 2 輪 R1）：psql 只用 LF 分行；`--` 註解在 CR 或 LF 結束；COPY 資料
    只在整行剛好是 `\.` 加 LF 或 CRLF 時結束。和 psql 可能讀得不一樣的寫法（NUL 位元組、反斜線
    緊接著引號、COPY 的選項、`\restrict` 以外的反斜線指令）一律停下，不猜。
    """

    def __init__(self) -> None:
        self._in_copy = False
        self._copy_next_line = False
        self._quote: bytes | None = None  # b"'"、b"E'"、b'"'、b"$"、b"/*"
        self._dollar_tag = b""
        self._comment_depth = 0
        self._new_statement()

    def _new_statement(self) -> None:
        self._words: list[str] = []
        self._markers: list[str] = []
        self._previous_word = ""
        self._paren_depth = 0
        self._begin_depth = 0
        self._copy_from_stdin = False

    @property
    def at_statement_boundary(self) -> bool:
        """不在字串、註解、COPY 資料裡，也沒有寫到一半的句子（可以接著送檢查與 `COMMIT`）。"""
        return (
            not self._in_copy
            and not self._copy_next_line
            and self._quote is None
            and not self._words
            and self._paren_depth == 0
        )

    def check_line(self, line: bytes) -> None:
        """檢查一行（只用 LF 分行，和 psql 用 `fgets` 讀的一樣；含結尾的 LF，最後一行可以沒有）。

        Raises:
            RestoreArchiveUnsafeError: COPY 資料以外有交易控制語句、不准的 psql 指令，或有和
                psql 斷句可能不一致的寫法（不確定時一律停下）。
        """
        if b"\0" in line:
            # psql 讀到 NUL 就把那一行剩下的丟掉（字串以 NUL 結尾），COPY 的結尾判斷也會錯開。
            raise RestoreArchiveUnsafeError("NUL 位元組（psql 斷句會和這裡不一致）")
        if self._in_copy:
            if line in _COPY_END_LINES:
                self._in_copy = False
            return
        self._scan(line)
        if self._copy_next_line:
            self._copy_next_line = False
            self._in_copy = True

    def _scan(self, line: bytes) -> None:
        i, n = 0, len(line)
        while i < n:
            if self._quote is not None:
                i = self._skip_quoted(line, i)
                continue
            match = _SPECIAL.search(line, i)
            if match is None:
                return
            i = match.start()
            char = line[i : i + 1]
            if char == b"\\":
                self._meta_command(line, i)
                return  # 只准整行的 `\restrict`／`\unrestrict`，這一行沒有別的東西
            if char == b"-" and line[i + 1 : i + 2] == b"-":
                # 註解到 CR 或 LF 為止；CR 後面同一行的東西 psql 會當 SQL（審查第 2 輪 R1）。
                end = _NEWLINE.search(line, i + 2)
                if end is None:
                    return
                i = end.end()
                continue
            i = self._next_token(line, i, char)

    def _next_token(self, line: bytes, i: int, char: bytes) -> int:
        if self._copy_next_line:
            # `COPY … FROM stdin;` 同一行分號後面還有東西：psql 會在 COPY 資料之後才執行它，
            # pg_restore 不會這樣寫，停下。
            raise RestoreArchiveUnsafeError("COPY 那一行的分號後面還有內容")
        if self._copy_from_stdin and char != b";":
            # pg_restore 寫的是 `COPY … FROM stdin;`；後面有選項（例如 binary 格式會讓 psql 把
            # 剩下的輸入全當 COPY 資料）就停下。
            raise RestoreArchiveUnsafeError("COPY … FROM stdin 後面有選項")
        if char.isdigit():
            number = _NUMBER_TOKEN.match(line, i)
            return number.end() if number else i + 1
        identifier = _IDENTIFIER_TOKEN.match(line, i)
        if identifier is not None:
            word = identifier.group(0)
            if word in (b"E", b"e") and line[identifier.end() : identifier.end() + 1] == b"'":
                self._quote = b"E'"
                return identifier.end() + 1
            self._identifier(word.decode("utf-8", "replace").lower())
            return identifier.end()
        if char == b"$":
            dollar = _DOLLAR_QUOTE.match(line, i)
            if dollar is not None:
                self._quote, self._dollar_tag = b"$", dollar.group(0)
                return dollar.end()
            parameter = _POSITIONAL_PARAMETER.match(line, i)
            return parameter.end() if parameter else i + 1
        if char in (b"'", b'"'):
            self._quote = char
        elif char == b"/" and line[i + 1 : i + 2] == b"*":
            self._quote, self._comment_depth = b"/*", 1
            return i + 2
        elif char == b"(":
            self._paren_depth += 1
        elif char == b")":
            self._paren_depth = max(0, self._paren_depth - 1)
        elif char == b";" and self._paren_depth == 0 and self._begin_depth == 0:
            self._copy_next_line = self._copy_from_stdin
            self._new_statement()
        return i + 1

    def _skip_quoted(self, line: bytes, i: int) -> int:
        """從 `i` 往後跳過字串、名稱或註解的內容；結束時回下一個位置，沒結束就回行尾。"""
        quote = self._quote
        if quote in (b"'", b'"'):
            assert quote is not None
            while (j := line.find(quote, i)) >= 0:
                if quote == b"'":
                    _check_backslashes_before_quote(line, j)
                if line[j + 1 : j + 2] == quote:  # 兩個連在一起是跳脫
                    i = j + 2
                    continue
                self._quote = None
                return j + 1
        elif quote == b"E'":
            while (stop := _E_STRING_STOP.search(line, i)) is not None:
                j = stop.start()
                if line[j : j + 1] == b"\\":
                    if j > 0 and line[j - 1] >= 0x80:
                        # client_encoding 是 SJIS、BIG5 這類時，psql 把非 ASCII 字元後面的 `\`
                        # 當成同一個字，跳脫的位置會和這裡不一致。
                        raise RestoreArchiveUnsafeError("E'…' 字串裡非 ASCII 字元後面接反斜線")
                    i = j + 2
                    continue
                if line[j + 1 : j + 2] == b"'":
                    i = j + 2
                    continue
                self._quote = None
                return j + 1
        elif quote == b"$":
            j = line.find(self._dollar_tag, i)
            if j >= 0:
                self._quote = None
                return j + len(self._dollar_tag)
        else:
            while (token := _COMMENT_TOKEN.search(line, i)) is not None:
                i = token.end()
                self._comment_depth += 1 if token.group(0) == b"/*" else -1
                if self._comment_depth == 0:
                    self._quote = None
                    return i
        return len(line)

    def _identifier(self, word: str) -> None:
        position = len(self._words)
        if position == 0 and word in _TRANSACTION_WORDS:
            raise RestoreArchiveUnsafeError(f"交易控制語句 {word.upper()}，例如大型物件")
        if position == 1 and (self._words[0], word) in _TRANSACTION_PAIRS:
            raise RestoreArchiveUnsafeError(f"交易控制語句 {self._words[0].upper()} TRANSACTION")
        self._words.append(word)
        if len(self._markers) < 4:
            self._markers.append(word[0] if word in _DEFINITION_WORDS else "")
        if self._words[0] == "copy":
            if word == "binary":  # 舊寫法 `COPY BINARY t FROM stdin`：psql 會讀到輸入結尾
                raise RestoreArchiveUnsafeError("binary 格式的 COPY")
            # `FROM STDOUT` 也是從用戶端讀資料（PostgreSQL 的文法裡 STDIN、STDOUT 都代表用戶端）。
            if self._previous_word == "from" and word in ("stdin", "stdout"):
                self._copy_from_stdin = True
        self._previous_word = word
        if self._is_routine_definition() and self._paren_depth == 0:
            if word == "begin" or (word == "case" and self._begin_depth >= 1):
                self._begin_depth += 1
            elif word == "end" and self._begin_depth > 0:
                self._begin_depth -= 1

    def _is_routine_definition(self) -> bool:
        """這一句是 `CREATE [OR REPLACE] FUNCTION|PROCEDURE`（照 psql 的判斷）。"""
        m = [*self._markers, "", "", "", ""][:4]
        return m[0] == "c" and (
            m[1] in ("f", "p") or (m[1] == "o" and m[2] == "r" and m[3] in ("f", "p"))
        )

    def _meta_command(self, line: bytes, i: int) -> None:
        """只准整行、在兩句之間的 `\\restrict 金鑰`／`\\unrestrict 金鑰`（pg_restore 18 的寫法）。

        Raises:
            RestoreArchiveUnsafeError: 其他的反斜線指令，或同一行還有別的內容。
        """
        if (
            i == 0
            and not self._words
            and self._paren_depth == 0
            and _RESTRICT_LINE.fullmatch(line) is not None
        ):
            return
        match = _META_COMMAND.match(line, i)
        name = match.group(1).decode("utf-8", "replace") if match else ""
        raise RestoreArchiveUnsafeError(f"psql 的指令 \\{name}")


def _check_backslashes_before_quote(line: bytes, j: int) -> None:
    """一般字串 `'…'` 裡，`line[j]` 的引號前面緊接著反斜線時，判斷 psql 會不會斷在別處。

    這裡照 `standard_conforming_strings = on`（pg_restore 寫在開頭）把反斜線當一般字元；但備份的
    SQL 可以把它關掉，那時 `\\'` 是跳脫、字串沒有結束。前面有奇數個反斜線（兩種讀法不一樣），或
    反斜線前面是非 ASCII 字元（SJIS 這類編碼會把那個反斜線當成同一個字），就停下。

    Raises:
        RestoreArchiveUnsafeError: 這個引號在兩種讀法下的意思不同。
    """
    start = j
    while start > 0 and line[start - 1] == 0x5C:  # 反斜線
        start -= 1
    count = j - start
    if count % 2 == 1 or (count and start > 0 and line[start - 1] >= 0x80):
        raise RestoreArchiveUnsafeError("字串裡的反斜線緊接著引號（讀法可能和 psql 不同）")


class _Feeder:
    """把還原的輸入送進 `pg_restore` 的標準輸入（另一條執行緒）。

    `waiting_since` 是正在等輸入的開始時間（沒在等時是 None），給 `_InputWatchdog` 判斷輸入多久
    沒有新資料；在等 `pg_restore` 收資料（下游忙）時不算。
    """

    def __init__(self, source: Reader[bytes], pg_restore: subprocess.Popen[bytes]) -> None:
        self.error: BaseException | None = None
        self.waiting_since: float | None = None
        self.done = threading.Event()
        # 有 read1 就用（標準輸入的管線讀到一些就回來，不等湊滿一段）。
        self._read: Callable[[int], bytes] = getattr(source, "read1", source.read)
        self._target = pg_restore.stdin
        self._thread = threading.Thread(target=self._run, name="restore-feed", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        target = self._target
        assert target is not None
        try:
            while True:
                self.waiting_since = time.monotonic()
                chunk = self._read(_CHUNK)
                self.waiting_since = None
                if not chunk:
                    break
                target.write(chunk)
        except BaseException as exc:
            self.error = exc
        finally:
            self.waiting_since = None
            with suppress(OSError):
                target.close()
            self.done.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)


class _InputWatchdog:
    """還原的輸入超過 `RESTORE_INPUT_IDLE_TIMEOUT_SECONDS` 沒有新資料（例如 SSH 斷了卻沒有 EOF），
    就呼叫 `on_timeout`（停掉 `psql` 與 `pg_restore`，不送 `COMMIT`）。審查 C4。"""

    def __init__(self, feeder: _Feeder, on_timeout: Callable[[], None]) -> None:
        self.timed_out = False
        self.limit = RESTORE_INPUT_IDLE_TIMEOUT_SECONDS
        self._feeder = feeder
        self._on_timeout = on_timeout
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="restore-watch", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        interval = min(1.0, self.limit / 4)
        while not self._stopped.wait(interval):
            since = self._feeder.waiting_since
            if since is not None and time.monotonic() - since > self.limit:
                self.timed_out = True
                self._on_timeout()
                return

    def stop(self) -> None:
        """停止看守；回來之後 `timed_out` 不會再變。可以重複呼叫。"""
        self._stopped.set()
        self._thread.join()


def _signal_kill(process: subprocess.Popen[bytes]) -> None:
    """送 SIGKILL（不等它結束；給看守執行緒用，等結束由主執行緒做）。"""
    with suppress(OSError):
        process.kill()


def restore_archive(
    request: RestoreRequest, source: Reader[bytes], *, pg_env: Mapping[str, str]
) -> None:
    """把 `pg_dump --format=custom` 的串流還原到 `pg_env` 指的資料庫，整個在一個交易裡。

    `source` → `pg_restore --file=-`（不連資料庫，只把備份換成 SQL）→ 這裡逐行檢查 → `psql`。
    送進 `psql` 的是：`BEGIN`、`request.before_restore_sql`（目標還是空的）、備份的 SQL；
    `pg_restore` 成功結束、讀完整個輸入、SQL 在完整的一句結束之後，才送
    `request.before_commit_sql`（只有這個交易建的 relation、沒有大型物件、只有共用資料時沒有成員
    資料）與 `COMMIT`。任何一步失敗都不送 `COMMIT`：`psql` 結束時交易退回，目標資料庫不留下任何
    東西（不是事後刪除）。`psql` 用 `VERBOSITY=sqlstate`，錯誤只印 SQLSTATE，不印資料內容。

    Raises:
        RestoreArchiveUnsafeError: 備份的 SQL 有交易控制語句、不准的 psql 指令或大型物件（已退回）。
        RestoreRefusedError: 交易裡發現目標不是空的、有別的連線同時建了 relation、或 COMMIT 前
            還有別的用戶端連著目標資料庫（已退回）。
        SharedOnlyBackupHasMemberRowsError: 只有共用資料的備份裡有成員資料（已退回）。
        BackupToolError: 找不到工具，`pg_restore`、`psql` 失敗，輸入讀到一半出錯或逾時（已退回）。
    """
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

        def stop_both() -> None:
            _signal_kill(psql)
            _signal_kill(pg_restore)

        watchdog = _InputWatchdog(feeder, stop_both)
        try:
            committed = _relay(request, pg_restore, psql, feeder, watchdog)
        finally:
            watchdog.stop()
            for process in (psql, pg_restore):
                _kill(process)
            # 失敗時 feeder 可能還卡在讀輸入；它是 daemon 執行緒，不無限期等它（輸入逾時就不等）。
            feeder.join(timeout=0 if watchdog.timed_out else 5)
            # 子程序已經停了：管線上還沒送出的緩衝丟掉就好（送不出去的錯誤不用管）。
            for stream in (psql.stdin, pg_restore.stdin, pg_restore.stdout):
                if stream is not None:
                    with suppress(OSError, ValueError):
                        stream.close()
        if watchdog.timed_out:
            raise BackupToolError(
                "radar restore",
                f"超過 {watchdog.limit} 秒沒有收到輸入，已停止；沒有送 COMMIT，交易已退回",
            )
        _raise_for_exit_codes(psql, pg_restore, psql_err, restore_err, pg_env)
        if feeder.error is not None:
            raise BackupToolError(
                "radar restore",
                f"讀輸入或送進 pg_restore 時中斷（{type(feeder.error).__name__}），交易已退回",
            )
        if not committed:
            raise BackupToolError("psql", "沒有完成，交易已退回")


def _raise_for_exit_codes(
    psql: subprocess.Popen[bytes],
    pg_restore: subprocess.Popen[bytes],
    psql_err: IO[bytes],
    restore_err: IO[bytes],
    pg_env: Mapping[str, str],
) -> None:
    if psql.returncode != 0:
        detail = _stderr_tail(psql_err, pg_env)
        if MEMBER_ROWS_SQLSTATE in detail:
            raise SharedOnlyBackupHasMemberRowsError
        if LARGE_OBJECT_SQLSTATE in detail:
            raise RestoreArchiveUnsafeError("大型物件")
        if TARGET_NOT_EMPTY_SQLSTATE in detail:
            raise RestoreRefusedError(
                [
                    "還原開始時目標資料庫已經不是空的（檢查之後有別的連線建了資料表、view"
                    " 或序列）；交易已退回"
                ]
            )
        if FOREIGN_RELATION_SQLSTATE in detail:
            raise RestoreRefusedError(
                ["還原期間有別的連線在目標資料庫建了資料表、view 或序列；交易已退回，沒有還原"]
            )
        if OTHER_CLIENT_SQLSTATE in detail:
            raise RestoreRefusedError(
                [
                    "還原時有別的程式連著目標資料庫（例如 api、worker）；交易已退回，沒有還原。"
                    "先停掉它們、只留還原自己再試"
                ]
            )
        raise BackupToolError("psql", f"失敗（退出碼 {psql.returncode}），交易已退回", detail)
    if pg_restore.returncode != 0:
        raise BackupToolError(
            "pg_restore",
            f"失敗（退出碼 {pg_restore.returncode}），交易已退回",
            _stderr_tail(restore_err, {}),
        )


def _forward_lines(
    data: bytes, guard: _SqlScriptGuard, target: IO[bytes], psql: subprocess.Popen[bytes]
) -> None:
    """`data` 逐行檢查過才送進 `psql`；檢查不過就先停掉 `psql`（那一行沒送出）再丟錯。"""
    start = 0
    while start < len(data):
        end = data.find(b"\n", start)
        end = len(data) if end < 0 else end + 1
        try:
            guard.check_line(data[start:end])
        except RestoreArchiveUnsafeError:
            _signal_kill(psql)
            raise
        start = end
    target.write(data)


def _relay(
    request: RestoreRequest,
    pg_restore: subprocess.Popen[bytes],
    psql: subprocess.Popen[bytes],
    feeder: _Feeder,
    watchdog: _InputWatchdog,
) -> bool:
    """`pg_restore` 的輸出逐行檢查後轉給 `psql`，前面加 `BEGIN` 與還原前的檢查；都成功才加
    COMMIT 前的檢查與 `COMMIT`。回有沒有送出 `COMMIT` 而且 `psql` 成功結束。

    Raises:
        RestoreArchiveUnsafeError: 備份的 SQL 有不准的內容（`psql` 已停掉，沒有送 `COMMIT`）。
        BackupToolError: 備份的 SQL 沒有在完整的一句結束（`psql` 已停掉，沒有送 `COMMIT`）。
    """
    source = pg_restore.stdout
    target = psql.stdin
    assert source is not None
    assert target is not None
    guard = _SqlScriptGuard()
    pending = b""
    try:
        target.write(b"BEGIN;\n" + request.before_restore_sql.encode())
        while chunk := _read1(source, _CHUNK):
            data = pending + chunk
            cut = data.rfind(b"\n") + 1
            pending = data[cut:]
            if cut:
                _forward_lines(data[:cut], guard, target, psql)
        if pending:
            _forward_lines(pending, guard, target, psql)  # 最後一行沒有換行
        source.close()
        while not feeder.done.wait(0.1):
            if watchdog.timed_out:
                return False
        if pg_restore.wait() != 0 or feeder.error is not None or watchdog.timed_out:
            target.close()
            psql.wait()
            return False
        if not guard.at_statement_boundary:
            _signal_kill(psql)
            raise BackupToolError(
                "pg_restore", "輸出的 SQL 沒有在完整的一句結束；沒有送 COMMIT，交易已退回"
            )
        watchdog.stop()
        if watchdog.timed_out:
            return False
        target.write(request.before_commit_sql.encode() + b"COMMIT;\n")
        target.close()
    except BrokenPipeError:
        # psql 已經因為錯誤結束（ON_ERROR_STOP）或被停掉，交易已退回；退出碼與 stderr 說明原因。
        psql.wait()
        return False
    return psql.wait() == 0
