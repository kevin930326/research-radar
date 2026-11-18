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
