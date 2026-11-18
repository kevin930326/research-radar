"""備份與還原的規則（Story 3.18、ADR-0051）：清單、備份檔開頭的格式、版本與筆數比對、連線字串、
age 公鑰、資料表歸類的形狀。不連資料庫、不跑外部工具。"""

import io
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from radar.adapters.backup_tools import _redact
from radar.config import ConfigError, load_settings
from radar.domain.backup import (
    MEMBER_TABLES,
    PAYLOAD_MAGIC,
    SHARED_TABLES,
    BackupCounts,
    BackupFormatError,
    BackupManifest,
    BackupMode,
    DatabaseVersions,
    DumpRequest,
    RestoreRequest,
    backup_object_key,
    check_age_recipient,
    count_differences,
    encode_payload_header,
    expect_mode_problems,
    libpq_environment,
    read_payload_header,
    restore_version_problems,
    unknown_revision_problems,
    version_text,
    version_tuple,
)
from radar.domain.viewer import SystemViewer
from radar.repo.backup import get_restore_request

RECIPIENT = "age1" + "q" * 58
CREATED = datetime(2026, 11, 18, 12, 0, 5, tzinfo=UTC)
COUNTS = BackupCounts(
    counted_paper=3,
    zh_summary_active=2,
    classification_active=2,
    research_model_active=1,
    embedding=3,
)


def _manifest(**changes: object) -> BackupManifest:
    values: dict[str, object] = {
        "mode": BackupMode.FULL,
        "created_at": CREATED,
        "postgres_version": "18.1",
        "postgres_version_num": 180001,
        "pgvector_version": "0.8.7",
        "pg_dump_version": "18.7",
        "alembic_revisions": ("0020",),
        "counts": COUNTS,
        "member_table_rows": {"member": 2, "library_item": 1},
    } | changes
    return BackupManifest.model_validate(values)


def _versions(**changes: object) -> DatabaseVersions:
    values: dict[str, object] = {
        "postgres_version": "18.1",
        "postgres_version_num": 180001,
        "pgvector_version": "0.8.7",
        "has_pg_trgm": True,
        "alembic_revisions": (),
    } | changes
    return DatabaseVersions(**values)  # type: ignore[arg-type]


# --- 資料表歸類 ---------------------------------------------------------------------------------


def test_every_table_is_in_exactly_one_list_with_a_reason() -> None:
    assert set(SHARED_TABLES).isdisjoint(MEMBER_TABLES)
    assert all(reason.strip() for reason in (*SHARED_TABLES.values(), *MEMBER_TABLES.values()))


def test_story_tables_are_classified() -> None:
    # 驗收條件點名的成員資料，加上 3.16 的兩張表；Alembic 的版本表兩種模式都要帶，所以是共用資料。
    for table in ("member", "allowed_email", "session", "judgment_event", "audit_event"):
        assert table in MEMBER_TABLES
    assert {"library_item", "deleted_record"} <= set(MEMBER_TABLES)
    assert "alembic_version" in SHARED_TABLES


# --- 清單與備份檔的格式 ---------------------------------------------------------------------------


def test_header_round_trips_and_leaves_the_dump_in_the_stream() -> None:
    manifest = _manifest()
    stream = io.BytesIO(encode_payload_header(manifest) + b"PGDMP-rest")

    assert read_payload_header(stream) == manifest
    assert stream.read() == b"PGDMP-rest"


def test_header_has_no_personal_data_fields() -> None:
    line = encode_payload_header(_manifest()).splitlines()[1].decode()

    assert "email" not in line
    assert "display_name" not in line


@pytest.mark.parametrize(
    "start",
    [b"age-encryption.org/v1\n-> X25519 abc\n", b"-----BEGIN AGE ENCRYPTED FILE-----\nYWdl\n"],
)
def test_still_encrypted_input_is_explained(start: bytes) -> None:
    with pytest.raises(BackupFormatError, match="age -d"):
        read_payload_header(io.BytesIO(start))


@pytest.mark.parametrize(
    "data",
    [
        b"PGDMP\x01\x0e",  # 只有 pg_dump 的輸出，沒有清單
        b"",
        PAYLOAD_MAGIC,  # 沒有清單那一行
        PAYLOAD_MAGIC + b"{not json}\n",
        PAYLOAD_MAGIC + b'{"format_version": 2}\n',
        PAYLOAD_MAGIC + b"x" * (64 * 1024 + 10) + b"\n",
    ],
)
def test_malformed_input_is_rejected(data: bytes) -> None:
    with pytest.raises(BackupFormatError):
        read_payload_header(io.BytesIO(data))


def test_manifest_rejects_unknown_fields_and_bad_tables() -> None:
    line = encode_payload_header(_manifest()).splitlines()[1]
    tampered = line.replace(b'"mode":"full"', b'"mode":"full","email":"x@example.com"')

    with pytest.raises(BackupFormatError):
        read_payload_header(io.BytesIO(PAYLOAD_MAGIC + tampered + b"\n"))
    with pytest.raises(ValueError, match="小寫識別字"):
        _manifest(member_table_rows={"Member; DROP": 1})


def test_shared_only_manifest_must_have_zero_member_rows() -> None:
    assert _manifest(mode=BackupMode.SHARED_ONLY, member_table_rows={"member": 0}).mode is (
        BackupMode.SHARED_ONLY
    )
    with pytest.raises(ValueError, match="0"):
        _manifest(mode=BackupMode.SHARED_ONLY, member_table_rows={"member": 1})


def test_created_at_is_normalised_to_utc_and_needs_a_zone() -> None:
    taipei = datetime(2026, 11, 18, 20, 0, 5, tzinfo=timezone(timedelta(hours=8)))

    assert _manifest(created_at=taipei).created_at == CREATED
    with pytest.raises(ValueError, match="時區"):
        _manifest(created_at=datetime(2026, 11, 18, 12, 0))


def test_object_key_names_time_and_mode() -> None:
    assert backup_object_key(CREATED, BackupMode.FULL) == (
        "backup/20261118T120005Z-full.radar-backup.age"
    )
    assert backup_object_key(CREATED, BackupMode.SHARED_ONLY) == (
        "backup/20261118T120005Z-shared-only.radar-backup.age"
    )


# --- 版本 -------------------------------------------------------------------------------------


def test_version_text_takes_the_leading_number() -> None:
    assert version_text("18.1 (Debian 18.1-1.pgdg13+2)") == "18.1"
    assert version_tuple("0.8.10") > version_tuple("0.8.7")
    with pytest.raises(ValueError, match="版本號"):
        version_text("beta")


def test_same_or_newer_target_passes() -> None:
    assert restore_version_problems(_manifest(), _versions(), 18) == []
    newer = _versions(
        postgres_version="18.3", postgres_version_num=180003, pgvector_version="0.9.0"
    )
    assert restore_version_problems(_manifest(), newer, 19) == []


def test_older_target_lists_both_versions() -> None:
    old = _versions(postgres_version="18.0", postgres_version_num=180000, pgvector_version="0.8.0")

    problems = restore_version_problems(_manifest(), old, 18)

    assert problems == [
        "目標的 PostgreSQL 是 18.0，比備份時的 18.1 舊",
        "目標的 pgvector 是 0.8.0，比備份時的 0.8.7 舊",
    ]


def test_missing_extensions_and_old_pg_restore_are_problems() -> None:
    problems = restore_version_problems(
        _manifest(), _versions(pgvector_version=None, has_pg_trgm=False), 17
    )

    assert len(problems) == 3
    assert "沒有 pgvector" in problems[0]
    assert "沒有 pg_trgm" in problems[1]
    assert "pg_restore 是 17 版" in problems[2]


# --- 筆數比對 ----------------------------------------------------------------------------------


def test_matching_counts_have_no_differences() -> None:
    assert count_differences(_manifest(), COUNTS, {"member": 2, "library_item": 1}) == []


def test_differences_name_the_item_and_by_how_much() -> None:
    restored = COUNTS.model_copy(update={"counted_paper": 2, "embedding": 4})

    differences = count_differences(_manifest(), restored, {"member": 2})

    assert differences == [
        "算數的論文：清單 3，還原後 2，差 -1",
        "向量：清單 3，還原後 4，差 +1",
        "成員資料表 library_item：清單 1 列，還原後沒有這張表",
    ]


# --- 連線字串 → libpq 的環境變數 ----------------------------------------------------------------


def test_libpq_environment_puts_the_password_only_in_pgpassword() -> None:
    env = libpq_environment(
        "postgresql+psycopg://radar_app:p%40ss%3Aword@db.example:5433/radar?sslmode=require",
        connect_timeout=3,
        application_name="radar-backup",
    )

    assert env == {
        "PGHOST": "db.example",
        "PGPORT": "5433",
        "PGUSER": "radar_app",
        "PGPASSWORD": "p@ss:word",
        "PGDATABASE": "radar",
        "PGSSLMODE": "require",
        "PGCONNECT_TIMEOUT": "3",
        "PGAPPNAME": "radar-backup",
    }


@pytest.mark.parametrize(
    "url",
    [
        "mysql://radar:s3cret-PW@db/radar",
        "postgresql://radar:s3cret-PW@db/radar?evil=s3cret-PW",
        "postgresql://radar:s3cret-PW@db1,db2/radar",
        "postgresql://radar:s3cret-PW@/radar",
    ],
)
def test_libpq_environment_errors_do_not_echo_the_url(url: str) -> None:
    with pytest.raises(ValueError, match="連線字串") as caught:
        libpq_environment(url, connect_timeout=3, application_name="x")

    assert "s3cret" not in str(caught.value)


def test_socket_host_from_query_is_accepted() -> None:
    env = libpq_environment(
        "postgresql://radar_owner@/radar?host=/var/run/postgresql",
        connect_timeout=3,
        application_name="x",
    )

    assert env["PGHOST"] == "/var/run/postgresql"


# --- age 的公鑰 --------------------------------------------------------------------------------


def test_only_age_public_keys_are_accepted() -> None:
    assert check_age_recipient(RECIPIENT) == RECIPIENT
    for bad in ("AGE-SECRET-KEY-1" + "Q" * 58, "age1short", "ssh-ed25519 AAAA", RECIPIENT + " "):
        with pytest.raises(ValueError, match="age 的公鑰"):
            check_age_recipient(bad)


def test_setting_rejects_a_private_key_without_echoing_it() -> None:
    secret = "AGE-SECRET-KEY-1" + "Q" * 58
    with pytest.raises(ConfigError) as caught:
        load_settings(
            {
                "DATABASE_URL": "postgresql://radar_app@db/radar",
                "RADAR_BACKUP_AGE_RECIPIENT": secret,
            }
        )

    assert "RADAR_BACKUP_AGE_RECIPIENT" in str(caught.value)
    assert secret not in str(caught.value)


def test_setting_is_optional() -> None:
    settings = load_settings({"DATABASE_URL": "postgresql://radar_app@db/radar"})

    assert settings.backup_age_recipient is None


# --- 子程序的請求與工具 -------------------------------------------------------------------------


def test_requests_only_take_lowercase_table_names() -> None:
    with pytest.raises(ValueError, match="小寫識別字"):
        DumpRequest("snap", b"h", ("member; DROP TABLE paper",), RECIPIENT)
    with pytest.raises(ValueError, match="age 的公鑰"):
        DumpRequest("snap", b"h", ("member",), "AGE-SECRET-KEY-1" + "Q" * 58)
    with pytest.raises(ValueError, match="小寫識別字"):
        get_restore_request(SystemViewer(), mode=BackupMode.SHARED_ONLY, member_tables=["Member"])
    with pytest.raises(ValueError, match="完整的一句"):
        RestoreRequest("SELECT 1", "SELECT 1;\n")


def test_member_rows_guard_names_every_table_and_raises_its_own_sqlstate() -> None:
    sql = get_restore_request(
        SystemViewer(), mode=BackupMode.SHARED_ONLY, member_tables=("member", "library_item")
    ).before_commit_sql

    assert "ARRAY['library_item', 'member']::text[]" in sql
    assert "ERRCODE = 'RR001'" in sql
    assert "public.%I" in sql


def test_restore_guards_check_emptiness_and_relations_in_both_modes() -> None:
    """審查 C2、C3、S3：BEGIN 之後再查一次空的；COMMIT 前查 relation 都是這個交易建的、沒有大型
    物件；只有共用資料時另外查成員資料表與成員上傳的 PDF 摘要。"""
    full = get_restore_request(SystemViewer(), mode=BackupMode.FULL, member_tables=("member",))
    shared = get_restore_request(
        SystemViewer(), mode=BackupMode.SHARED_ONLY, member_tables=("member",)
    )

    for request in (full, shared):
        assert "ERRCODE = 'RR002'" in request.before_restore_sql
        assert "pg_current_xact_id()" in request.before_commit_sql
        assert "ERRCODE = 'RR003'" in request.before_commit_sql
        assert "pg_largeobject_metadata" in request.before_commit_sql
        assert "ERRCODE = 'RR004'" in request.before_commit_sql
    assert "RR001" not in full.before_commit_sql
    assert "uploaded_pdf" not in full.before_commit_sql
    assert "public.paper_abstract" in shared.before_commit_sql
    assert "''uploaded_pdf''" in shared.before_commit_sql


def test_restore_guards_are_only_for_the_system_viewer() -> None:
    with pytest.raises(TypeError):
        get_restore_request(cast(SystemViewer, object()), mode=BackupMode.FULL, member_tables=())


# --- --expect-mode 與遷移版本（審查 S8）--------------------------------------------------------


def test_expect_mode_only_complains_when_given_and_different() -> None:
    shared = _manifest(mode=BackupMode.SHARED_ONLY, member_table_rows={"member": 0})

    assert expect_mode_problems(shared, None) == []
    assert expect_mode_problems(shared, BackupMode.SHARED_ONLY) == []
    assert expect_mode_problems(_manifest(), BackupMode.SHARED_ONLY) == [
        "這份備份是「完整（含成員資料）」，和 --expect-mode 指定的「只有共用資料」不同"
    ]


def test_unknown_alembic_revisions_are_problems() -> None:
    manifest = _manifest(alembic_revisions=("0020", "0099_future"))

    assert unknown_revision_problems(manifest, frozenset({"0020", "0099_future"})) == []
    assert unknown_revision_problems(manifest, frozenset({"0019", "0020"})) == [
        "備份的遷移版本 0099_future 這個映像不認得（備份比程式新）；先換成新的映像再還原"
    ]


def test_redact_removes_connection_values() -> None:
    text = 'connection to server at "db.example", port 5433 failed: user "radar_app"'

    assert _redact(text, ["db.example", "radar_app", ""]) == (
        'connection to server at "…", port 5433 failed: user "…"'
    )
