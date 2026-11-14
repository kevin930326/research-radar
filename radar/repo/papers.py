"""`paper` 與 `external_id` 兩張表：表的定義與唯一的寫入者（AD-4、AD-6、ADR-0014、ADR-0020）。

- 論文只能經 `upsert_by_external_id` 建立（只收 `ParsedPaper`）；沒有識別碼的手填書目之後經
  `create_manual`（還沒有呼叫端，不建）。
- `lab_visible` 只由這個模組寫：追蹤範圍內的論文、引用擴展（之後加上傳批次勾了分享）設為真，
  設為真之後不改回去。
- 寫入一律用 `text()` SQL（架構測試掃字串裡的 `INSERT INTO` 等，確認一張表一個寫入者）。
- 函式收呼叫端開好的交易連線（`Database.transaction()`），不 commit、不 rollback。
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

import structlog
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from radar.domain.identifiers import ExternalId, IdentifierScheme
from radar.domain.papers import (
    PaperOrigin,
    ParsedPaper,
    StoredPaper,
    UpsertOutcome,
    UpsertResult,
    check_storable_text,
)
from radar.domain.scope import ScopeSettings, is_in_scope
from radar.domain.viewer import Viewer
from radar.repo.db import Database
from radar.repo.models import Base
from radar.repo.visibility import visible_papers

log = structlog.get_logger(__name__)

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0003 建表） ---------------------------

paper_table = Table(
    "paper",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("origin", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("publication_year", Integer),
    Column("journal_issn_l", Text),
    Column("in_scope", Boolean, nullable=False),
    Column("excluded_reason", Text),
    Column("lab_visible", Boolean, nullable=False),
    # 每個來源一欄抓取時間；NULL＝沒有這個來源的資料。「舊的不蓋掉新的」只和同一個來源比。
    Column("openalex_fetched_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "origin IN ('tracked', 'expansion', 'import', 'manual')",
        name="origin",
    ),
    CheckConstraint(
        "excluded_reason IS NULL OR excluded_reason <> ''",
        name="excluded_reason_not_empty",
    ),
    CheckConstraint("NOT in_scope OR lab_visible", name="in_scope_implies_lab_visible"),
)

external_id_table = Table(
    "external_id",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("paper_id", Uuid, ForeignKey("paper.id"), nullable=False, index=True),
    Column("scheme", Text, nullable=False),
    Column("value_norm", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("scheme", "value_norm"),
    CheckConstraint("scheme IN ('doi', 'openalex')", name="scheme"),
    CheckConstraint("value_norm <> ''", name="value_norm_not_empty"),
)

# `upsert_by_external_id` 收的 origin；`manual` 只能經之後的 `create_manual`。
UPSERT_ORIGINS = frozenset({PaperOrigin.TRACKED, PaperOrigin.EXPANSION, PaperOrigin.IMPORT})

_PAPER_COLUMNS = (
    "id, origin, title, publication_year, journal_issn_l, in_scope, excluded_reason,"
    " openalex_fetched_at, created_at, updated_at"
)


# --- 讀取 ---------------------------------------------------------------------------------


def get_paper(viewer: Viewer, connection: Connection, paper_id: uuid.UUID) -> StoredPaper | None:
    """`viewer` 看得到的那篇論文；不存在或看不到都回 None（不透露資料存在）。"""
    condition = visible_papers(viewer, alias="paper")
    row = (
        connection.execute(
            text(f"SELECT {_PAPER_COLUMNS} FROM paper WHERE paper.id = :id AND {condition}"),
            {"id": paper_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return StoredPaper(
        id=row["id"],
        origin=PaperOrigin(row["origin"]),
        title=row["title"],
        publication_year=row["publication_year"],
        journal_issn_l=row["journal_issn_l"],
        in_scope=row["in_scope"],
        excluded_reason=row["excluded_reason"],
        openalex_fetched_at=row["openalex_fetched_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# --- 寫入 ---------------------------------------------------------------------------------


def upsert_by_external_id(
    connection: Connection,
    paper: ParsedPaper,
    *,
    origin: PaperOrigin,
    excluded_reason: str | None,
    scope: ScopeSettings,
) -> UpsertResult:
    """依識別碼「有就更新、沒有才新增」一篇論文，在呼叫端開好的交易裡做，不 commit。

    1. 依優先序（DOI、再 OpenAlex ID）找既有的論文。
    2. 有：`SELECT ... FOR UPDATE` 鎖住它，比 `openalex_fetched_at`：要寫的比較舊就什麼都不寫、
       回 `stale`；一樣新或比較新才寫，並把還沒有主人的識別碼掛上來。
    3. 沒有：在保存點裡新增論文，再用 `ON CONFLICT DO NOTHING` 依優先序認領識別碼。任何一個
       認領不到（開始找的時候都沒有主人，所以只可能是並行的另一個交易剛建好同一篇；會等那個交易
       結束才知道），就退回保存點：剛新增的論文與剛認領的識別碼一起放掉，再改走第 2 步。結果和
       兩個交易依序執行時一樣。
    4. 識別碼已經屬於另一篇論文時不搬、不合併：放在結果的 `conflicts`，並記警告日誌。
       判成 `stale` 時不掛任何識別碼，也不回報衝突。

    鎖的順序（ADR-0020）：先鎖論文、再佔識別碼，識別碼依優先序佔；第 3 步改走第 2 步之前先退回
    保存點，手上不留任何識別碼，才去等論文的鎖。所以每個交易只寫一篇時，不論幾個交易同時寫、
    識別碼怎麼重疊，都不會互相等成死結。一個交易寫很多篇（每週套用、`reparse`）時，跨論文的
    順序固定不了，和其他交易（含只寫一篇的）仍可能死結；PostgreSQL 會中止其中一個交易，
    那個交易要整個重跑。

    `in_scope` 由 `is_in_scope` 算；`lab_visible`＝原本的值 OR `in_scope` OR `origin` 是
    `expansion`；`origin` 只在新增時寫；`excluded_reason` 由呼叫端明確決定、整欄覆寫。

    交易隔離層級要是 READ COMMITTED（PostgreSQL 預設）：第 3 步「退回再重查」靠每個語句重新拿
    快照才讀得到並行交易剛 commit 的論文；改成 REPEATABLE READ／SERIALIZABLE 會失效。

    Raises:
        TypeError: `paper` 不是 `ParsedPaper`（不碰資料庫）。
        ValueError: `origin` 不是 `tracked`／`expansion`／`import`，或 `excluded_reason`
            是空字串、含 NUL 字元或無法編成 UTF-8（不碰資料庫）。
    """
    if not isinstance(paper, ParsedPaper):
        raise TypeError(f"只收 ParsedPaper（現在是 {type(paper).__name__}）")
    origin = PaperOrigin(origin)
    if origin not in UPSERT_ORIGINS:
        raise ValueError(f"upsert_by_external_id 不收 origin={origin}")
    if excluded_reason is not None:
        if not isinstance(excluded_reason, str) or excluded_reason == "":
            raise ValueError("excluded_reason 要是 None 或非空字串")
        check_storable_text(excluded_reason, "excluded_reason")

    ids = paper.external_ids()
    in_scope = is_in_scope(paper.journal_issn_l, paper.publication_year, scope)
    values: dict[str, object] = {
        "title": paper.title,
        "publication_year": paper.publication_year,
        "journal_issn_l": paper.journal_issn_l,
        "in_scope": in_scope,
        "excluded_reason": excluded_reason,
        "shares_with_lab": in_scope or origin is PaperOrigin.EXPANSION,
        "fetched_at": paper.fetched_at,
    }

    owners = _find_owners(connection, ids)
    target = _first_owner(ids, owners)
    if target is None:
        created = _insert_new(connection, ids, values, origin)
        if created is not None:
            return created
        # 並行的另一個交易剛建好同一篇（已經 commit，所以現在讀得到）。
        owners = _find_owners(connection, ids)
        target = _first_owner(ids, owners)
        if target is None:
            raise RuntimeError("識別碼認領失敗，卻找不到擁有它的論文")
    return _update_existing(connection, target, ids, owners, values, paper.fetched_at)


def _first_owner(
    ids: Sequence[ExternalId], owners: Mapping[ExternalId, uuid.UUID]
) -> uuid.UUID | None:
    """依優先序第一個有主人的識別碼所屬的論文。"""
    return next((owners[external_id] for external_id in ids if external_id in owners), None)


def _find_owners(connection: Connection, ids: Sequence[ExternalId]) -> dict[ExternalId, uuid.UUID]:
    """這些識別碼目前各屬於哪一篇論文（沒有主人的不在結果裡）。"""
    if not ids:
        return {}
    clauses = " OR ".join(
        f"(scheme = :scheme_{index} AND value_norm = :value_{index})" for index in range(len(ids))
    )
    parameters: dict[str, object] = {}
    for index, external_id in enumerate(ids):
        parameters[f"scheme_{index}"] = external_id.scheme.value
        parameters[f"value_{index}"] = external_id.value_norm
    rows = connection.execute(
        text(f"SELECT scheme, value_norm, paper_id FROM external_id WHERE {clauses}"),
        parameters,
    )
    return {
        ExternalId(IdentifierScheme(scheme), value_norm): paper_id
        for scheme, value_norm, paper_id in rows
    }


def _claim(connection: Connection, paper_id: uuid.UUID, external_id: ExternalId) -> bool:
    """把識別碼掛到 `paper_id`；已經有主人（含並行交易剛寫、等它結束後確定的）回 False。"""
    row = connection.execute(
        text(
            "INSERT INTO external_id (paper_id, scheme, value_norm)"
            " VALUES (:paper_id, :scheme, :value_norm)"
            " ON CONFLICT (scheme, value_norm) DO NOTHING"
            " RETURNING id"
        ),
        {
            "paper_id": paper_id,
            "scheme": external_id.scheme.value,
            "value_norm": external_id.value_norm,
        },
    ).one_or_none()
    return row is not None


def _owner_of(connection: Connection, external_id: ExternalId) -> uuid.UUID:
    owner: uuid.UUID = connection.execute(
        text("SELECT paper_id FROM external_id WHERE scheme = :scheme AND value_norm = :value"),
        {"scheme": external_id.scheme.value, "value": external_id.value_norm},
    ).scalar_one()
    return owner


class _IdentifierTakenError(Exception):
    """新增時有識別碼認領不到：用來退回保存點（只在這個模組裡丟、接）。"""


def _insert_new(
    connection: Connection,
    ids: Sequence[ExternalId],
    values: Mapping[str, object],
    origin: PaperOrigin,
) -> UpsertResult | None:
    """在保存點裡新增論文並依優先序認領識別碼；任何一個認領不到就退回保存點、回 None。

    退回保存點會放掉剛新增的論文與剛認領的識別碼（都還沒 commit，別人看不到），正在等這些
    識別碼的交易也會立刻繼續；呼叫端接著才去等既有論文的鎖，手上不留任何識別碼（ADR-0020）。
    """
    try:
        with Database.savepoint(connection):
            paper_id: uuid.UUID = connection.execute(
                text(
                    "INSERT INTO paper (origin, title, publication_year, journal_issn_l,"
                    " in_scope, excluded_reason, lab_visible, openalex_fetched_at)"
                    " VALUES (:origin, :title, :publication_year, :journal_issn_l, :in_scope,"
                    " :excluded_reason, :shares_with_lab, :fetched_at)"
                    " RETURNING id"
                ),
                {**values, "origin": origin.value},
            ).scalar_one()
            for external_id in ids:
                if not _claim(connection, paper_id, external_id):
                    raise _IdentifierTakenError
    except _IdentifierTakenError:
        return None
    return UpsertResult(paper_id, UpsertOutcome.CREATED)


def _update_existing(
    connection: Connection,
    paper_id: uuid.UUID,
    ids: Sequence[ExternalId],
    owners: Mapping[ExternalId, uuid.UUID],
    values: Mapping[str, object],
    fetched_at: datetime,
) -> UpsertResult:
    """先鎖住論文再比抓取時間；比較舊就不寫（`stale`，不掛識別碼、不回報衝突），
    否則更新並掛上還沒有主人的識別碼。
    """
    stored: datetime | None = connection.execute(
        text("SELECT openalex_fetched_at FROM paper WHERE id = :id FOR UPDATE"),
        {"id": paper_id},
    ).scalar_one()
    if stored is not None and fetched_at < stored:
        return UpsertResult(paper_id, UpsertOutcome.STALE)

    connection.execute(
        text(
            "UPDATE paper SET"
            " title = :title,"
            " publication_year = :publication_year,"
            " journal_issn_l = :journal_issn_l,"
            " in_scope = :in_scope,"
            " excluded_reason = :excluded_reason,"
            " lab_visible = lab_visible OR :shares_with_lab,"
            " openalex_fetched_at = :fetched_at,"
            " updated_at = now()"
            " WHERE id = :id"
        ),
        {**values, "id": paper_id},
    )

    conflicts: list[ExternalId] = []
    for external_id in ids:
        owner = owners.get(external_id)
        if owner is None:
            if _claim(connection, paper_id, external_id):
                continue
            owner = _owner_of(connection, external_id)
        if owner != paper_id:
            _warn_conflict(paper_id, external_id, owner)
            conflicts.append(external_id)
    return UpsertResult(paper_id, UpsertOutcome.UPDATED, tuple(conflicts))


def _warn_conflict(paper_id: uuid.UUID, external_id: ExternalId, owner: uuid.UUID) -> None:
    log.warning(
        "識別碼已經屬於另一篇論文，沒有搬動也沒有合併",
        paper_id=str(paper_id),
        scheme=external_id.scheme.value,
        value_norm=external_id.value_norm,
        owner_paper_id=str(owner),
    )
