"""備份工具 adapter 的純函式（Story 3.18 審查 C1、S10）：還原轉送的 SQL 檢查、錯誤訊息去敏。

不跑外部工具、不連資料庫。用假子程序的測試在 `tests/integration/test_backup_tools_fake.py`。
"""

import pytest

from radar.adapters.backup_tools import _SqlScriptGuard, redact_connection_details
from radar.domain.backup import RestoreArchiveUnsafeError


def _feed(script: bytes) -> _SqlScriptGuard:
    """逐行（含換行）餵給檢查；檢查不過時丟出的例外原樣往上丟。"""
    guard = _SqlScriptGuard()
    for line in script.splitlines(keepends=True):
        guard.check_line(line)
    return guard


# 照 pg_restore 18 的樣子編的一段（結構、COPY 資料、函式本體、註解）。
NORMAL_SCRIPT = b"""--
-- PostgreSQL database dump
--

\\restrict abcDEF123

SET statement_timeout = 0;
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);

CREATE FUNCTION public.f() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF NEW.kind = 'x;y' THEN
    RETURN NEW;
  END IF;
  RETURN NEW;
END;
$$;

CREATE FUNCTION public.g() RETURNS integer
    LANGUAGE sql
    BEGIN ATOMIC
 SELECT CASE WHEN true THEN 1 ELSE 2 END;
END;

COMMENT ON TABLE public.note IS 'first line;
COMMIT;
still the comment';

/* block /* nested */ COMMIT; */
SELECT E'it\\'s; COMMIT;' AS x, $tag$ BEGIN; $tag$ AS y;

CREATE TABLE public.note (
    id integer,
    body text
);

COPY public.note (id, body) FROM stdin;
1\tCOMMIT;
COMMIT;
BEGIN;
\\.

SELECT 1;

\\unrestrict abcDEF123
"""


def test_normal_script_passes_and_ends_at_a_statement_boundary() -> None:
    guard = _feed(NORMAL_SCRIPT)

    assert guard.at_statement_boundary


@pytest.mark.parametrize(
    ("line", "word"),
    [
        (b"BEGIN;\n", "BEGIN"),
        (b"COMMIT;\n", "COMMIT"),
        (b"commit ;\n", "COMMIT"),
        (b"ROLLBACK;\n", "ROLLBACK"),
        (b"END;\n", "END"),
        (b"ABORT;\n", "ABORT"),
        (b"SAVEPOINT a;\n", "SAVEPOINT"),
        (b"RELEASE a;\n", "RELEASE"),
        (b"START TRANSACTION;\n", "START TRANSACTION"),
        (b"PREPARE TRANSACTION 'x';\n", "PREPARE TRANSACTION"),
        (b"  /* c */ COMMIT;\n", "COMMIT"),
    ],
)
def test_transaction_control_outside_copy_is_refused(line: bytes, word: str) -> None:
    with pytest.raises(RestoreArchiveUnsafeError, match=word):
        _feed(b"SELECT 1;\n" + line)


def test_large_object_block_from_pg_restore_is_refused() -> None:
    """pg_restore 不加 --single-transaction 時，大型物件的資料前後自己寫 BEGIN／COMMIT。"""
    script = (
        b"SELECT pg_catalog.lo_create('16385');\n\n"
        b"--\n-- Data for Name: BLOBS; Type: BLOBS; Schema: -; Owner: -\n--\n\n"
        b"BEGIN;\n\n"
        b"SELECT pg_catalog.lo_open('16385', 131072);\n"
        b"COMMIT;\n"
    )

    with pytest.raises(RestoreArchiveUnsafeError, match="BEGIN"):
        _feed(script)


def test_commit_inside_copy_data_is_data() -> None:
    guard = _feed(b"COPY public.t (body) FROM stdin;\nCOMMIT;\nBEGIN;\n\\.\n")

    assert guard.at_statement_boundary
    with pytest.raises(RestoreArchiveUnsafeError, match="COMMIT"):
        guard.check_line(b"COMMIT;\n")


def test_statement_split_over_lines_is_still_caught() -> None:
    guard = _SqlScriptGuard()
    guard.check_line(b"SELECT 1;\n")

    guard.check_line(b"START\n")  # 這一行還看不出是什麼（句子沒結束，psql 也還沒執行）
    with pytest.raises(RestoreArchiveUnsafeError, match="START TRANSACTION"):
        guard.check_line(b"TRANSACTION;\n")


@pytest.mark.parametrize("command", [b"\\connect other\n", b"\\c other\n", b"\\! ls\n"])
def test_other_psql_commands_are_refused(command: bytes) -> None:
    with pytest.raises(RestoreArchiveUnsafeError, match="psql 的指令"):
        _feed(b"SELECT 1;\n" + command)


@pytest.mark.parametrize(
    "unfinished",
    [b"SELECT 'open\n", b"SELECT 1\n", b"COPY public.t (a) FROM stdin;\n1\n", b"/* open\n"],
)
def test_unfinished_script_is_not_at_a_statement_boundary(unfinished: bytes) -> None:
    assert not _feed(unfinished).at_statement_boundary


# --- 審查第 2 輪 R1：行尾和 PostgreSQL 18 一致（CR、CRLF），不確定的寫法停下 ----------------------


def _feed_lf(script: bytes) -> _SqlScriptGuard:
    """只用 LF 分行（和 psql 用 fgets 讀、轉送時 `_forward_lines` 切的一樣；CR 不分行）。"""
    guard = _SqlScriptGuard()
    for line in script.split(b"\n"):
        guard.check_line(line + b"\n")
    return guard


@pytest.mark.parametrize(
    "script",
    [
        b"SELECT 1;\n-- c\rCOMMIT;\n",  # `--` 註解在 CR 就結束，後面的 COMMIT 會被執行
        b"SELECT 1;\n-- c\r\nCOMMIT;\n",
        b"SELECT 1; -- c\rCOMMIT;\n",
        b"SELECT 1;\n/* c */\rCOMMIT;\n",
        b"SELECT 1;\n/* c\r*/\rCOMMIT;\n",
        b"COPY public.t (id) FROM stdin;\n1\r\n\\.\r\nCOMMIT;\n",  # COPY 用 CRLF 結束
        b"COPY public.t (id) FROM stdin;\r\n1\r\n\\.\r\n-- c\rCOMMIT;\r\n",
    ],
)
def test_commit_after_a_carriage_return_is_refused(script: bytes) -> None:
    with pytest.raises(RestoreArchiveUnsafeError, match="COMMIT"):
        _feed_lf(script)


def test_comments_and_copy_data_with_crlf_are_not_misjudged() -> None:
    guard = _feed_lf(
        b"-- COMMIT;\r\n"
        b"/* COMMIT; */\r\n"
        b"SELECT 1; -- then COMMIT\r\n"
        b"COMMENT ON TABLE public.t IS 'a\r\nCOMMIT;\r\n';\r\n"
        b"COPY public.t (body) FROM stdin;\r\n"
        b"COMMIT;\r\n"
        b"BEGIN;\r\n"
        b"\\.\rCOMMIT;\r\n"  # 不是整行的 `\.`：還是 COPY 資料（psql 也這樣認）
        b"\\.\r\n"
        b"SELECT 2;\r\n"
    )

    assert guard.at_statement_boundary


@pytest.mark.parametrize("not_an_end", [b"\\.\r\r\n", b"\\. \n", b" \\.\n", b"\\.\\.\n"])
def test_only_an_exact_copy_end_line_ends_the_copy_data(not_an_end: bytes) -> None:
    guard = _SqlScriptGuard()
    guard.check_line(b"COPY public.t (body) FROM stdin;\n")
    guard.check_line(not_an_end)
    guard.check_line(b"COMMIT;\n")  # 還在 COPY 資料裡（psql 也還沒結束 COPY）

    assert not guard.at_statement_boundary


def test_copy_end_without_a_newline_at_the_end_is_not_a_boundary() -> None:
    guard = _SqlScriptGuard()
    guard.check_line(b"COPY public.t (body) FROM stdin;\n")
    guard.check_line(b"\\.")

    assert not guard.at_statement_boundary


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        # psql 在 `\\` 之後會把同一行剩下的當 SQL 執行
        (b"\\restrict abc \\\\ COMMIT;\n", "psql 的指令"),
        (b"SELECT 1;\n\\unrestrict abc\\\\COMMIT;\n", "psql 的指令"),
        (b"SELECT 1 \\restrict abc\n", "psql 的指令"),
        (b"SELECT 1;\n\\;COMMIT;\n", "psql 的指令"),
        # psql 讀到 NUL 就丟掉那一行剩下的，斷句會錯開
        (b"SELECT 1;\x00'\nCOMMIT;\n'\n", "NUL"),
        (b"COPY public.t (body) FROM stdin;\n\\.\x00x\n", "NUL"),
        # COPY 的選項（binary 會讓 psql 把剩下的輸入全當資料）、分號後面還有東西
        (b"COPY BINARY public.t FROM stdin;\n", "binary"),
        (b"COPY public.t FROM stdin WITH (FORMAT binary);\n", "選項"),
        (b"COPY public.t FROM stdin (FORMAT 'binary');\n", "選項"),
        (b"COPY public.t FROM stdin; SELECT 1;\n", "分號後面"),
        # 關掉 standard_conforming_strings 時 `\'` 是跳脫：兩種讀法不一樣就停下
        (b"SET standard_conforming_strings = off;\nSELECT 'a\\' ' COMMIT; '';\n", "反斜線"),
        (b"SELECT 'a\\';\n", "反斜線"),
        (b"SELECT '\xe4\xb8\xad\\\\';\n", "反斜線"),
        (b"SELECT E'\x95\\';\n", "反斜線"),
        # 數字或 $1 後面緊接著 E：PostgreSQL 把 `1E` 當一個記號，`'…'` 是一般字串
        (b"SELECT 1E'\\';COMMIT;--';\n", "反斜線"),
        (b"SELECT $1E'\\';COMMIT;--';\n", "反斜線"),
    ],
)
def test_writings_psql_may_read_differently_are_refused(script: bytes, reason: str) -> None:
    with pytest.raises(RestoreArchiveUnsafeError, match=reason):
        _feed_lf(script)


def test_copy_from_stdout_reads_from_the_client_too() -> None:
    """`COPY … FROM STDOUT` 也會讓 psql 讀 COPY 資料；當成一般 SQL 會在資料的引號裡錯過 COMMIT。"""
    with pytest.raises(RestoreArchiveUnsafeError, match="COMMIT"):
        _feed_lf(b"COPY public.t (body) FROM STDOUT;\n'\n\\.\nCOMMIT;\n'\n")


@pytest.mark.parametrize(
    "line",
    [
        b"SELECT 'it''s', '\\\\', 'C:\\\\dir\\\\', 'no\\escape';\n",  # 偶數個反斜線、不接引號
        b"SELECT E'it\\'s', E'\\\\', E'\\x41';\n",
        b"SELECT 1e5, 1_000, 0x1F, $1, $2;\n",
        b"\\restrict abcDEF123\r\n",
    ],
)
def test_ordinary_writings_still_pass(line: bytes) -> None:
    assert _feed_lf(line).at_statement_boundary


# --- 審查第 3 輪 Q1：standard_conforming_strings 是 on 之後，反斜線是一般字元 -------------------

STD_ON = b"SET standard_conforming_strings = on;\n"


@pytest.mark.parametrize(
    "header",
    [
        STD_ON,
        b"SET standard_conforming_strings TO on;\n",
        b"set STANDARD_CONFORMING_STRINGS = ON ;\n",
    ],
)
def test_backslash_before_quote_is_plain_text_once_strings_are_standard(header: bytes) -> None:
    guard = _feed_lf(
        header + b"COMMENT ON TABLE public.t IS 'C:\\';\n"
        b"ALTER TABLE public.t ALTER COLUMN p SET DEFAULT 'a\\\\\\';\n"  # 三個反斜線接引號
        b"COMMENT ON COLUMN public.t.p IS '\xe4\xb8\xad\\';\n"
        b"SELECT 1;\n"
    )

    assert guard.at_statement_boundary


def test_commit_after_a_backslash_string_is_still_refused_when_standard() -> None:
    guard = _feed_lf(STD_ON + b"COMMENT ON TABLE public.t IS 'C:\\';\n")

    with pytest.raises(RestoreArchiveUnsafeError, match="COMMIT"):
        guard.check_line(b"COMMIT;\n")
    with pytest.raises(RestoreArchiveUnsafeError, match="COMMIT"):
        _feed_lf(STD_ON + b"SELECT 'C:\\';COMMIT;--';\n")


@pytest.mark.parametrize(
    "script",
    [
        b"BEGIN;\n",
        b"COMMIT;\n",
        b"ROLLBACK;\n",
        b"END;\n",
        b"ABORT;\n",
        b"SAVEPOINT a;\n",
        b"RELEASE a;\n",
        b"START TRANSACTION;\n",
        b"PREPARE TRANSACTION 'x';\n",
        b"SELECT 1;\n-- c\rCOMMIT;\n",
        b"SELECT 1;\n/* c */\rCOMMIT;\n",
        b"COPY public.t (id) FROM stdin;\n1\r\n\\.\r\nCOMMIT;\n",
        b"SELECT 1E'\\';COMMIT;--';\n",
        b"SELECT E'\\'';COMMIT;--';\n",
    ],
)
def test_transaction_control_is_still_refused_when_standard(script: bytes) -> None:
    with pytest.raises(
        RestoreArchiveUnsafeError,
        match="交易控制語句",
    ):
        _feed_lf(STD_ON + script)


def test_the_set_line_itself_is_still_read_the_old_way() -> None:
    """psql 每一行開始時才讀一次字串的讀法：SET 同一行後面的字串還照舊的讀。"""
    with pytest.raises(RestoreArchiveUnsafeError, match="反斜線"):
        _feed_lf(b"SET standard_conforming_strings = on; SELECT 'C:\\';\n")


@pytest.mark.parametrize(
    "script",
    [
        b"SET standard_conforming_strings = off;\n",
        b"SET standard_conforming_strings TO off;\n",
        b"SET standard_conforming_strings = 'on';\n",  # 認不得的寫法也停下
        b"SET standard_conforming_strings = DEFAULT;\n",
        b"SET standard_conforming_strings = on, off;\n",
        b"SET standard_conforming_strings = 1;\n",
        b"SET LOCAL standard_conforming_strings = off;\n",
        b"SET SESSION standard_conforming_strings = on;\n",
        b"RESET standard_conforming_strings;\n",
        b"RESET ALL;\n",
        b'SET "standard_conforming_strings" = off;\n',
        b'SET U&"standard_conforming_strings" = off;\n',
        b"SELECT pg_catalog.set_config('standard_conforming_strings', 'off', false);\n",
        b"SELECT set_config('search_path', '', false);\n",  # 不是 pg_restore 那一行
        b"ALTER ROLE r SET standard_conforming_strings = off;\n",
    ],
)
def test_other_changes_to_string_reading_are_refused(script: bytes) -> None:
    with pytest.raises(RestoreArchiveUnsafeError, match="反斜線"):
        _feed_lf(script)
    with pytest.raises(RestoreArchiveUnsafeError, match="反斜線"):
        _feed_lf(STD_ON + script)  # 已經是 on 之後再改也一樣停下


def test_pg_restore_search_path_line_is_allowed() -> None:
    guard = _feed_lf(STD_ON + b"SELECT pg_catalog.set_config('search_path', '', false);\r\n")

    assert guard.at_statement_boundary


# --- 錯誤訊息去敏（審查 S10）------------------------------------------------------------------

PG_ENV = {
    "PGHOST": "db.example",
    "PGPORT": "5432",
    "PGUSER": "radar_owner",
    "PGPASSWORD": "s3cret-PW",
    "PGDATABASE": "radar",
}


def test_password_user_host_and_ip_are_redacted_but_not_the_port() -> None:
    text = (
        'connection to server at "db.example" (172.18.0.2), port 5432 failed: '
        'FATAL: password authentication failed for user "radar_owner" (s3cret-PW)'
    )

    redacted = redact_connection_details(text, PG_ENV)

    assert redacted == (
        'connection to server at "…" (…), port 5432 failed: '
        'FATAL: password authentication failed for user "…" (…)'
    )


@pytest.mark.parametrize("address", ["::1", "fe80::1", "2001:db8::8a2e:370:7334"])
def test_ipv6_addresses_are_redacted(address: str) -> None:
    assert redact_connection_details(f"at {address}, port 5432", PG_ENV) == "at …, port 5432"


def test_times_and_line_numbers_are_kept() -> None:
    text = "psql:<stdin>:5432: ERROR at 12:00:05 in radar restore"

    # 埠號一樣的行號、時間不遮；資料庫名稱 radar 是完整的詞才遮。
    assert redact_connection_details(text, PG_ENV) == (
        "psql:<stdin>:5432: ERROR at 12:00:05 in … restore"
    )


def test_database_name_is_only_redacted_as_a_whole_word() -> None:
    text = 'database "radar" does not exist; radar_app and radarx are other names'

    assert redact_connection_details(text, PG_ENV) == (
        'database "…" does not exist; radar_app and radarx are other names'
    )
