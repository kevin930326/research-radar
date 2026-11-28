"""備份與還原的規則（Story 3.18、AD-28、ADR-0051）：資料表歸類、清單、備份檔的格式、
版本與筆數的比對。

- **資料表歸類**：每張表剛好在 `SHARED_TABLES`（共用資料）或 `MEMBER_TABLES`（成員資料）其中一份。
  schema 體檢（`tests/arch/test_schema.py`）檢查遷移後的資料庫：沒歸類的表、兩份都有的表、清單裡
  卻不存在的表都算失敗；有 `owner_id` 或成員欄位（`member_id`、`*_member_id`）的表一定是成員資料；
  共用資料表不能有外鍵指到成員資料表（只有共用資料的備份還原時會違反外鍵）。新增資料表的 Story
  要在這裡加一列並寫理由。view 不是表，不歸類（資料在它讀的表裡）。
- **備份檔**（加密前的內容，`age` 加密的就是這一整串）：第一行是 `PAYLOAD_MAGIC`，第二行是一行
  JSON 的清單（`BackupManifest`），之後是 `pg_dump --format=custom` 的輸出。清單包在同一個加密檔
  裡（不另外放一份沒加密的），所以經 SSH 用標準輸入還原時只要傳一個串流。
- 清單只有模式、筆數、版本與時間，不放 email、名稱或任何成員的個人資料。
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import IO, Annotated, Literal
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

# --- 資料表歸類 ---------------------------------------------------------------------------------

# 成員資料：屬於某位成員、或記下哪位成員做了什麼的表。`radar backup --shared-only` 只帶它們的
# 結構、不帶任何一列。歸類拿不準的表先放這裡（寧可不帶出去），理由寫「拿不準」，列給 Kevin 決定
# （ADR-0051）。
_UNSURE_QUEUE = (
    "拿不準，先歸成員資料（ADR-0051 待 Kevin 決定）：背景工作佇列（Procrastinate）的表；"
    "工作參數之後可能帶成員資料的 ID（例如上傳辨認），試用主機也不該接手 Mac 上排著的工作"
)
MEMBER_TABLES: Mapping[str, str] = {
    "member": "成員本人（顯示名稱、Google 的 sub、是不是管理者）",
    "allowed_email": "允許名單：成員的 email",
    "session": "成員的登入 session",
    "audit_event": "稽核事件：記下哪位成員做了什麼",
    "judgment_event": "人工判斷事件（修正分類、回報中文重點）：記下是哪位成員",
    "library_item": "成員的個人文獻庫（owner_id，Story 3.16）",
    "deleted_record": "移出文獻庫前的封存（owner_id，Story 3.16）",
    "procrastinate_jobs": _UNSURE_QUEUE,
    "procrastinate_events": _UNSURE_QUEUE,
    "procrastinate_periodic_defers": _UNSURE_QUEUE,
    "procrastinate_workers": _UNSURE_QUEUE,
}

# 共用資料：論文、AI 產出、向量與系統自己的運作紀錄，兩種模式都整份帶走。
SHARED_TABLES: Mapping[str, str] = {
    "alembic_version": (
        "Alembic 的遷移版本：還原後的資料庫要知道自己的結構是哪一版，兩種模式都要帶"
    ),
    "paper": "論文書目",
    "paper_abstract": "論文摘要",
    "paper_keyword": "論文關鍵字",
    "paper_search_document": "論文的全文檢索資料（觸發器從標題與目前的摘要算出，Story 4.1）",
    "external_id": "論文的外部識別碼（DOI、OpenAlex ID）",
    "snapshot": "資料快照",
    "weekly_run": "每週更新的執行紀錄",
    "weekly_run_journal": "每週更新各期刊的進度",
    "raw_fetch": "原始資料的抓取紀錄（物件在物件儲存的 raw/，不在備份裡）",
    "ingest_staging": "匯入暫存表",
    "derivation_run": "AI 工作的執行紀錄（沒有成員欄位）",
    "zh_summary": "AI 產出：中文重點",
    "classification": "AI 產出：分類",
    "classification_label": "AI 產出：分類的標籤",
    "research_model": "AI 產出：研究模型",
    "hypothesis": "AI 產出：研究模型的假說",
    "construct": "構面",
    "construct_mention": "AI 產出：論文裡提到的構面",
    "embedding": "向量",
    "embedding_model": "向量模型登錄",
    "embedding_quota_hold": "向量化的額度占用（系統運作紀錄）",
    "llm_call": "LLM 呼叫帳本（費用與回應，沒有成員欄位）",
    "api_call": "外部 API 呼叫帳本（費用與額度）",
    "batch_submission": "AI 工作的 Batch 送出紀錄",
    "batch_item": "Batch 裡的每一項",
    "demo_export": "示範站匯出的狀態紀錄（快照與第幾次，沒有成員欄位）",
    # 評估（Story 4.4）：Kevin 以管理者身分匯入的題目與量測紀錄，是系統資料、沒有成員欄位、不記誰
    # 匯入；正解存 DOI（換資料庫照樣能用），搬到試用主機時要跟著走（規格寫「共用資料」）。
    "eval_query": "搜尋的評估題目（題組、題目編號、查詢或起點 DOI；沒有成員欄位，Story 4.4）",
    "eval_query_relevant": "評估題目的正解（DOI 與備註；沒有成員欄位，Story 4.4）",
    "eval_run": "評估的執行紀錄（下限、模型、快照、命中率；沒有成員欄位，Story 4.4）",
}


class BackupMode(StrEnum):
    """`full`：整個資料庫（含成員資料）；`shared_only`：成員資料表只有結構、沒有任何一列。"""

    FULL = "full"
    SHARED_ONLY = "shared_only"

    @property
    def label(self) -> str:
        return "完整（含成員資料）" if self is BackupMode.FULL else "只有共用資料"


# --- 清單 -------------------------------------------------------------------------------------

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,62}")
_VERSION = r"[0-9]+(\.[0-9]+){0,3}"
type VersionText = Annotated[str, StringConstraints(pattern=rf"^{_VERSION}$", max_length=40)]
type Count = Annotated[int, Field(ge=0, strict=True)]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("備份時間要帶時區")
    return value.astimezone(UTC)


class BackupCounts(BaseModel):
    """還原後要對得上的筆數：算數的論文、三種 AI 產出（`active`）、向量。

    `counted_paper` 是統計模組 `repo/stats.list_cube_counts` 各格篇數的加總（AD-5：分母只由它數），
    不是 `paper` 表的全部列數：追蹤範圍外、被排除、還沒進已發佈快照的論文不算（ADR-0051）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    counted_paper: Count
    zh_summary_active: Count
    classification_active: Count
    research_model_active: Count
    embedding: Count


# 印給 Kevin 看的名稱（比對結果、備份完成時）。
COUNT_LABELS: Mapping[str, str] = {
    "counted_paper": "算數的論文",
    "zh_summary_active": "中文重點（active）",
    "classification_active": "分類（active）",
    "research_model_active": "研究模型（active）",
    "embedding": "向量",
}


class BackupManifest(BaseModel):
    """一份備份的清單：模式、筆數、版本與時間。不放任何成員的個人資料。

    `member_table_rows` 是備份裡每張成員資料表的列數；只有共用資料的備份每一張都是 0。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    format_version: Literal[1] = 1
    mode: BackupMode
    created_at: Annotated[datetime, AfterValidator(_utc)]
    postgres_version: VersionText
    postgres_version_num: Annotated[int, Field(ge=100000, strict=True)]
    pgvector_version: VersionText
    pg_dump_version: VersionText
    alembic_revisions: tuple[Annotated[str, StringConstraints(max_length=64)], ...]
    counts: BackupCounts
    member_table_rows: dict[str, Count]

    @model_validator(mode="after")
    def _check_member_tables(self) -> BackupManifest:
        for table in self.member_table_rows:
            if not _IDENTIFIER.fullmatch(table):
                raise ValueError("成員資料表的名稱只能是小寫識別字")
        if self.mode is BackupMode.SHARED_ONLY and any(self.member_table_rows.values()):
            raise ValueError("只有共用資料的備份，成員資料表的列數都要是 0")
        return self

    @property
    def postgres_major(self) -> int:
        return self.postgres_version_num // 10000


def manifest_from_json(text: bytes) -> BackupManifest:
    """從 JSON 讀清單（JSON 模式：時間是字串、模式是字串）。

    Raises:
        BackupFormatError: 不是合法的清單。
    """
    try:
        return BackupManifest.model_validate_json(text, strict=False)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "?" for e in exc.errors()})
        raise BackupFormatError(f"清單的格式不對（{'、'.join(fields[:5])}）") from None


# --- 備份檔的格式 -------------------------------------------------------------------------------

PAYLOAD_MAGIC = b"RADAR-BACKUP 1\n"
MAX_MANIFEST_BYTES = 64 * 1024
# 還沒解密的 age 檔的開頭（二進位與 ASCII armor 兩種）。
_AGE_PREFIXES = (b"age-encryption.org/", b"-----BEGIN AGE ENCRYPTED FILE-----")
# 物件儲存裡的 Content-Type：age 加密後的位元組。
BACKUP_CONTENT_TYPE = "application/octet-stream"


class BackupError(Exception):
    """備份或還原沒有完成。訊息是給 Kevin 看的中文，不含連線字串、密碼或資料內容。"""


class BackupFormatError(BackupError):
    """還原的輸入不是解密後的 radar 備份檔（或清單壞了）。訊息不含檔案內容。"""


def encode_payload_header(manifest: BackupManifest) -> bytes:
    """備份檔開頭的兩行：格式標記與一行 JSON 的清單。"""
    line = manifest.model_dump_json().encode("utf-8")
    if len(line) > MAX_MANIFEST_BYTES or b"\n" in line:
        raise ValueError("清單太長")
    return PAYLOAD_MAGIC + line + b"\n"


def read_payload_header(stream: IO[bytes]) -> BackupManifest:
    """讀掉備份檔開頭的兩行、回清單；之後 `stream` 剩下的就是 `pg_dump` 的輸出。

    Raises:
        BackupFormatError: 開頭不是 radar 的備份檔（包括還沒解密的 age 檔）、或清單壞了。
    """
    first = stream.readline(len(PAYLOAD_MAGIC))
    if first != PAYLOAD_MAGIC:
        if any(first.startswith(p[: len(first)]) and first for p in _AGE_PREFIXES):
            raise BackupFormatError(
                "這是還沒解密的備份檔：先在有私鑰的電腦用 age -d 解密，再用管線傳給"
                " radar restore -（主機上不放私鑰）"
            )
        raise BackupFormatError("這不是 radar 的備份檔（開頭的格式標記不對）")
    line = stream.readline(MAX_MANIFEST_BYTES + 1)
    if not line.endswith(b"\n") or len(line) > MAX_MANIFEST_BYTES:
        raise BackupFormatError("清單太長或不完整")
    return manifest_from_json(line[:-1])


# --- 物件的鍵 ---------------------------------------------------------------------------------


def backup_object_key(created_at: datetime, mode: BackupMode) -> str:
    """備份在物件儲存的鍵：`backup/<UTC 時間>-<模式>.radar-backup.age`。

    例如 `backup/20261118T120000Z-shared-only.radar-backup.age`。
    """
    stamp = _utc(created_at).strftime("%Y%m%dT%H%M%SZ")
    return f"backup/{stamp}-{mode.value.replace('_', '-')}.radar-backup.age"


# --- 版本 -------------------------------------------------------------------------------------

_LEADING_VERSION = re.compile(_VERSION)


def version_text(raw: str) -> str:
    """從 `18.1 (Debian 18.1-1.pgdg13+2)` 這類字串取出開頭的數字版本 `18.1`。

    Raises:
        ValueError: 開頭不是數字版本。
    """
    match = _LEADING_VERSION.match(raw.strip())
    if match is None:
        raise ValueError("認不出版本號")
    return match.group(0)


def version_tuple(text: str) -> tuple[int, ...]:
    """`0.8.7` → `(0, 8, 7)`，比大小用。"""
    return tuple(int(part) for part in version_text(text).split("."))


@dataclass(frozen=True)
class DatabaseVersions:
    """一個資料庫的版本：PostgreSQL（`server_version_num` 與文字）、pgvector、有沒有 pg_trgm、
    Alembic。"""

    postgres_version: str
    postgres_version_num: int
    pgvector_version: str | None
    has_pg_trgm: bool
    alembic_revisions: tuple[str, ...]

    @property
    def postgres_major(self) -> int:
        return self.postgres_version_num // 10000


def unknown_revision_problems(manifest: BackupManifest, known: frozenset[str]) -> list[str]:
    """備份的 Alembic 版本有這個映像不認得的（備份比程式新）時，每個版本一條；沒有問題時是空清單。

    `known` 是這個映像的遷移檔認得的全部版本（不連資料庫）。
    """
    return [
        f"備份的遷移版本 {revision} 這個映像不認得（備份比程式新）；先換成新的映像再還原"
        for revision in manifest.alembic_revisions
        if revision not in known
    ]


def expect_mode_problems(manifest: BackupManifest, expected: BackupMode | None) -> list[str]:
    """`radar restore --expect-mode` 有給、而備份的模式不同時一條；沒給或相同時是空清單。"""
    if expected is None or manifest.mode is expected:
        return []
    return [f"這份備份是「{manifest.mode.label}」，和 --expect-mode 指定的「{expected.label}」不同"]


def restore_version_problems(
    manifest: BackupManifest, target: DatabaseVersions, pg_restore_major: int
) -> list[str]:
    """還原前的版本檢查：目標的 PostgreSQL、pgvector 不能比備份時舊（pgvector、pg_trgm 要先由管理者
    建好：備份不帶 `CREATE EXTENSION`，見 ADR-0051）；`pg_restore` 的大版本不能比做備份的 `pg_dump`
    舊。沒有問題時是空清單；版本不符的每一條都印出兩邊的版本。"""
    problems: list[str] = []
    if target.postgres_version_num < manifest.postgres_version_num:
        problems.append(
            f"目標的 PostgreSQL 是 {target.postgres_version}，比備份時的"
            f" {manifest.postgres_version} 舊"
        )
    if target.pgvector_version is None:
        problems.append(
            f"目標資料庫沒有 pgvector（備份時是 {manifest.pgvector_version}）；"
            "先由管理者建好擴充套件"
        )
    elif version_tuple(target.pgvector_version) < version_tuple(manifest.pgvector_version):
        problems.append(
            f"目標的 pgvector 是 {target.pgvector_version}，比備份時的"
            f" {manifest.pgvector_version} 舊"
        )
    if not target.has_pg_trgm:
        problems.append("目標資料庫沒有 pg_trgm；先由管理者建好擴充套件")
    dump_major = version_tuple(manifest.pg_dump_version)[0]
    if pg_restore_major < dump_major:
        problems.append(
            f"這裡的 pg_restore 是 {pg_restore_major} 版，比做備份的 pg_dump {dump_major} 版舊"
        )
    return problems


# --- 筆數比對 ----------------------------------------------------------------------------------


def count_differences(
    manifest: BackupManifest, counts: BackupCounts, member_table_rows: Mapping[str, int]
) -> list[str]:
    """還原後的筆數和清單比對；一致時是空清單，否則每一項印出清單、還原後、差多少。

    `member_table_rows` 裡沒有的表（還原後的資料庫沒有這張表）也算不一致。
    """
    expected = manifest.counts.model_dump()
    actual = counts.model_dump()
    differences = [
        _difference(COUNT_LABELS[name], expected[name], actual[name])
        for name in expected
        if expected[name] != actual[name]
    ]
    for table, rows in sorted(manifest.member_table_rows.items()):
        restored = member_table_rows.get(table)
        if restored is None:
            differences.append(f"成員資料表 {table}：清單 {rows} 列，還原後沒有這張表")
        elif restored != rows:
            differences.append(_difference(f"成員資料表 {table}", rows, restored))
    return differences


def _difference(label: str, expected: int, actual: int) -> str:
    return f"{label}：清單 {expected}，還原後 {actual}，差 {actual - expected:+d}"


# --- 子程序的請求 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class DumpRequest:
    """一次加密傾印：`pg_dump` 用 `snapshot_id` 這個快照（和清單的筆數同一個時間點），
    `exclude_table_data` 的表只傾印結構；加密前先寫 `header`（格式標記與清單）。"""

    snapshot_id: str
    header: bytes
    exclude_table_data: tuple[str, ...]
    recipient: str

    def __post_init__(self) -> None:
        for table in self.exclude_table_data:
            if not _IDENTIFIER.fullmatch(table):
                raise ValueError("表名只能是小寫識別字")
        check_age_recipient(self.recipient)


@dataclass(frozen=True)
class RestoreRequest:
    """一次還原在同一個交易裡要多送的兩段檢查（`repo/backup.get_restore_request` 組的 SQL）。

    - `before_restore_sql`：`BEGIN` 之後、備份的 SQL 之前送：目標還是空的（審查 C2）。
    - `before_commit_sql`：`COMMIT` 之前送：所有使用者的 relation 都是這個交易建的、沒有大型物件；
      只有共用資料的備份另外檢查成員資料表沒有任何一列、沒有成員上傳的 PDF 摘要；最後確認沒有
      別的用戶端連著目標資料庫。

    任何一項不符就用 `RESTORE_GUARD_SQLSTATES` 裡的代碼丟錯，`psql` 停下、不送 `COMMIT`，整個交易
    退回。
    """

    before_restore_sql: str
    before_commit_sql: str

    def __post_init__(self) -> None:
        for sql in (self.before_restore_sql, self.before_commit_sql):
            if not sql.endswith(";\n"):
                raise ValueError("檢查的 SQL 要是完整的一句（以分號與換行結尾）")


def check_table_name(table: str) -> str:
    """表名不是小寫識別字就丟 `ValueError`（組進 SQL 之前檢查）。"""
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError("表名只能是小寫識別字")
    return table


# --- 還原時在交易裡的檢查（SQLSTATE）--------------------------------------------------------------

# 還原的交易裡，檢查不過時 `RAISE` 用的自訂 SQLSTATE；`psql` 用 `VERBOSITY=sqlstate`，錯誤只印代碼。
MEMBER_ROWS_SQLSTATE = "RR001"  # 只有共用資料的備份帶了成員資料（成員表有列、成員上傳的 PDF 摘要）
TARGET_NOT_EMPTY_SQLSTATE = "RR002"  # BEGIN 之後再檢查一次：目標已經不是空的
FOREIGN_RELATION_SQLSTATE = "RR003"  # COMMIT 前：有不是這個交易建的 relation
LARGE_OBJECT_SQLSTATE = "RR004"  # COMMIT 前：目標有大型物件
OTHER_CLIENT_SQLSTATE = "RR005"  # COMMIT 前：還有別的用戶端連著目標資料庫（審查第 2 輪 R2）
RESTORE_GUARD_SQLSTATES = (
    MEMBER_ROWS_SQLSTATE,
    TARGET_NOT_EMPTY_SQLSTATE,
    FOREIGN_RELATION_SQLSTATE,
    LARGE_OBJECT_SQLSTATE,
    OTHER_CLIENT_SQLSTATE,
)


# --- 逾時（審查 C4、S6；數值與理由見 ADR-0051 決定第 16 點）----------------------------------

# `pg_dump --lock-wait-timeout`：每張表等鎖最多這麼久，等不到就失敗，不無限期排在別人後面。
PG_DUMP_LOCK_WAIT_TIMEOUT_MS = 60_000
# 等 `pg_dump` 開始輸出（已經匯入快照、鎖好表）最多這麼久；逾時就停掉兩個子程序，沒有備份。
DUMP_START_TIMEOUT_SECONDS = 300
# 匯出快照的交易在等 `pg_dump` 時是閒置的；伺服器端的最後保險，比上一個長，讓程式的逾時先發生。
SNAPSHOT_IDLE_TIMEOUT_MS = 360_000
# 還原時輸入（檔案或標準輸入）多久沒有新資料就放棄：停掉 `psql`、不送 `COMMIT`。
RESTORE_INPUT_IDLE_TIMEOUT_SECONDS = 300
# 還原 commit 之後重數筆數的連線用的 `statement_timeout`（平常的連線是幾秒）。
RECOUNT_STATEMENT_TIMEOUT_MS = 600_000


# --- age 的公鑰 --------------------------------------------------------------------------------

# age 的 X25519 公鑰：`age1` 加 58 個 bech32 字元（小寫）。只收公鑰；私鑰（AGE-SECRET-KEY-1…）
# 不會出現在任何設定裡。
_AGE_RECIPIENT = re.compile(r"age1[02-9ac-hj-np-z]{58}")


def check_age_recipient(value: str) -> str:
    """age 的公鑰格式不對就丟 `ValueError`（訊息不帶值）。"""
    if not _AGE_RECIPIENT.fullmatch(value):
        raise ValueError("要是 age 的公鑰（age1 開頭、共 62 個字元）")
    return value


# --- 連線字串 → libpq 的環境變數 --------------------------------------------------------------

# 連線字串的查詢參數對應的 libpq 環境變數；其他參數不認得就擋下（不默默丟掉，例如 sslmode）。
_LIBPQ_QUERY_ENV: Mapping[str, str] = {
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslcrl": "PGSSLCRL",
    "sslnegotiation": "PGSSLNEGOTIATION",
    "channel_binding": "PGCHANNELBINDING",
    "gssencmode": "PGGSSENCMODE",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "passfile": "PGPASSFILE",
}


def libpq_environment(url: str, *, connect_timeout: int, application_name: str) -> dict[str, str]:
    """把 `postgresql://user:password@host:port/db?sslmode=…` 換成給 `pg_dump`、`psql` 的環境變數。

    密碼放 `PGPASSWORD`，只放在子程序的環境裡，不出現在命令列參數（`ps` 看得到）與日誌。
    錯誤訊息是固定句子，不帶連線字串的任何一段。

    Raises:
        ValueError: 解析不了、不是 postgresql、有多個主機、或有不認得的查詢參數。
    """
    try:
        parts = urlsplit(url)
        scheme, netloc, path, query = parts.scheme, parts.netloc, parts.path, parts.query
        username, password, hostname, port = (
            parts.username,
            parts.password,
            parts.hostname,
            parts.port,
        )
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query))
    except Exception:
        raise ValueError("連線字串無法解析") from None
    if scheme not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("連線字串開頭必須是 postgresql://")
    if "," in netloc:
        raise ValueError("連線字串不支援多個主機")
    env: dict[str, str] = {}
    if hostname:
        env["PGHOST"] = unquote(hostname)
    if port is not None:
        env["PGPORT"] = str(port)
    if username:
        env["PGUSER"] = unquote(username)
    if password:
        env["PGPASSWORD"] = unquote(password)
    database = unquote(path.removeprefix("/"))
    if database:
        env["PGDATABASE"] = database
    for key, value in pairs:
        name = _LIBPQ_QUERY_ENV.get(key)
        if name is None:
            raise ValueError("連線字串有不認得的參數（只收 libpq 的連線參數）")
        env[name] = value
    if "PGHOST" not in env and "PGHOSTADDR" not in env:
        raise ValueError("連線字串缺少主機名稱")
    env.setdefault("PGCONNECT_TIMEOUT", str(connect_timeout))
    env["PGAPPNAME"] = application_name
    return env


# --- 錯誤 -------------------------------------------------------------------------------------


class PgDumpTooOldError(BackupError):
    def __init__(self, pg_dump_major: int, database_major: int) -> None:
        super().__init__(
            f"這裡的 pg_dump 是 {pg_dump_major} 版，比資料庫的 PostgreSQL {database_major} 版舊；"
            "沒有備份"
        )


class RestoreRefusedError(BackupError):
    """還原前的檢查沒過（目標不是空的、版本太舊），什麼都沒有寫。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("沒有還原：" + "；".join(problems))
        self.problems = problems


class SharedOnlyBackupHasMemberRowsError(BackupError):
    def __init__(self) -> None:
        super().__init__(
            "清單說這份備份只有共用資料，但備份裡有成員資料（成員資料表有資料，或有成員上傳的 PDF"
            " 摘要）；沒有還原（整個交易已退回，目標資料庫還是空的）"
        )


class SharedOnlyBackupRefusedError(BackupError):
    """`--shared-only` 備份前的檢查沒過：共用表裡有成員來源的內容。沒有備份。"""

    def __init__(self, uploaded_abstracts: int) -> None:
        super().__init__(
            f"有 {uploaded_abstracts} 筆成員上傳的 PDF 摘要（只有上傳者看得到）；只有共用資料的備份"
            "不能帶它們，沒有備份"
        )


class RestoreArchiveUnsafeError(BackupError):
    """備份裡有會自己 commit 的內容（交易控制語句、大型物件），不能在一個交易裡還原。

    已經停掉 `psql`、沒有送 `COMMIT`，交易退回，目標沒有被寫入。
    """

    def __init__(self, what: str) -> None:
        super().__init__(
            f"這份備份含有會自己 commit 的內容（{what}），不能在同一個交易裡還原；沒有送 COMMIT，"
            "交易已退回，目標資料庫沒有被寫入"
        )


class RestoreVerificationError(BackupError):
    """已經 COMMIT（資料已還原），但之後重數筆數時出錯，沒比對成功。資料留著，不自動刪除。"""

    def __init__(self, error_type: str) -> None:
        super().__init__(
            f"資料已還原，但還原後重數筆數時出錯（{error_type}），筆數沒比對成功；還原的資料留著"
            "沒有刪除，要不要刪掉重來由 Kevin 決定"
        )


class RestoreCountMismatchError(BackupError):
    """還原完成了，但筆數和清單不一致。還原的資料留著，不自動刪除。"""

    def __init__(self, differences: list[str]) -> None:
        super().__init__(
            "還原後的筆數和清單不一致；還原的資料留著沒有刪除，要不要刪掉重來由 Kevin 決定："
            + "；".join(differences)
        )
        self.differences = differences


class BackupToolError(BackupError):
    """外部工具（pg_dump、age、pg_restore、psql）失敗或找不到。`detail` 已經去掉連線資訊。"""

    def __init__(self, tool: str, reason: str, detail: str = "") -> None:
        message = f"{tool} {reason}"
        if detail:
            message += f"：{detail}"
        super().__init__(message)
        self.tool = tool
