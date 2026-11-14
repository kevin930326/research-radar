"""`paper`、`external_id`、`paper_abstract`、`paper_keyword` 四張表：表的定義與唯一的寫入者
（AD-4、AD-6、AD-7、AD-31、ADR-0014、ADR-0016、ADR-0020、ADR-0022）。

- 論文只能經 `upsert_by_external_id` 建立（只收 `ParsedPaper`）；沒有識別碼的手填書目之後經
  `create_manual`（還沒有呼叫端，不建）。
- 摘要和論文欄位一起在 `upsert_by_external_id` 裡寫（同一個「舊的不蓋掉新的」保護）；同一篇
  最多一筆 `is_current`，由這裡依 `ABSTRACT_SOURCE_PRIORITY` 決定。
- `first_snapshot_id` 只由 `update_papers_first_snapshot` 寫（每週更新的套用交易呼叫，AD-19、
  ADR-0011「快照」第 3 點）；`upsert_by_external_id` 不碰這個欄位。
- 關鍵字由 `update_paper_keywords` 整批覆寫（每週套用與 `reparse` 在不是 STALE 時呼叫）。
  這個模組只寫 `paper_keyword`、不讀它（讀的查詢只放之後的 `repo/keyword_stats.py`，AD-31）。
- `lab_visible` 只由這個模組寫：追蹤範圍內的論文、引用擴展（之後加上傳批次勾了分享）設為真，
  設為真之後不改回去。
- 寫入一律用 `text()` SQL（架構測試掃字串裡的 `INSERT INTO` 等，確認一張表一個寫入者）。
- 函式收呼叫端開好的交易連線（`Database.transaction()`），不 commit、不 rollback。
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

import structlog
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY

from radar.domain.identifiers import ExternalId, IdentifierScheme
from radar.domain.papers import (
    ABSTRACT_SOURCE_PRIORITY,
    AbstractSource,
    KeywordSource,
    PaperOrigin,
    ParsedKeyword,
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

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0003 建表、0005 加欄位與兩張表、0007 加
# `first_snapshot_id`） ----------------------------------------------------------------------

_EMPTY_TEXT_ARRAY = text("'{}'::text[]")

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
    # 以下由遷移 0005 加（Story 1.7）。
    Column("work_type", Text),
    Column("is_retracted", Boolean, nullable=False, server_default=text("false")),
    Column("volume", Text),
    Column("issue", Text),
    Column("first_page", Text),
    Column("last_page", Text),
    Column("authors", ARRAY(Text), nullable=False, server_default=_EMPTY_TEXT_ARRAY),
    Column("referenced_works", ARRAY(Text), nullable=False, server_default=_EMPTY_TEXT_ARRAY),
    Column("cited_by_count", Integer),
    # 目前這份資料的指紋（AD-25，`domain/fingerprints.py`）。
    Column("source_hash", Text),
    # 由遷移 0007 加（Story 1.9）：第一次被算進哪一個快照；只由 `update_papers_first_snapshot` 寫。
    Column("first_snapshot_id", Uuid, ForeignKey("snapshot.id"), index=True),
    CheckConstraint(
        "origin IN ('tracked', 'expansion', 'import', 'manual')",
        name="origin",
    ),
    CheckConstraint(
        "excluded_reason IS NULL OR excluded_reason <> ''",
        name="excluded_reason_not_empty",
    ),
    CheckConstraint("NOT in_scope OR lab_visible", name="in_scope_implies_lab_visible"),
    CheckConstraint(
        "cited_by_count IS NULL OR cited_by_count >= 0", name="cited_by_count_not_negative"
    ),
    CheckConstraint(
        "source_hash IS NULL OR source_hash ~ '^[0-9a-f]{64}$'", name="source_hash_format"
    ),
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

# 一篇論文可以有多個來源的摘要，最多一筆 `is_current`（部分唯一索引保證）。外鍵的索引由
# 唯一約束（開頭是 paper_id）提供。
paper_abstract_table = Table(
    "paper_abstract",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("paper_id", Uuid, ForeignKey("paper.id"), nullable=False),
    Column("source", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("is_current", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("paper_id", "source"),
    Index(
        "uq_paper_abstract_current",
        "paper_id",
        unique=True,
        postgresql_where=text("is_current"),
    ),
    CheckConstraint(
        "source IN ('openalex', 'semantic_scholar', 'uploaded_pdf')",
        name="source",
    ),
    CheckConstraint("content <> ''", name="content_not_empty"),
    CheckConstraint(
        "NOT (is_current AND source = 'uploaded_pdf')", name="uploaded_pdf_not_current"
    ),
)

# （論文, 來源, key）唯一；來源只有 `KeywordSource` 那一份清單（測試比對 CHECK 的值）。
paper_keyword_table = Table(
    "paper_keyword",
    Base.metadata,
    Column("id", Uuid, primary_key=True, server_default=text("uuidv7()")),
    Column("paper_id", Uuid, ForeignKey("paper.id"), nullable=False),
    Column("source", Text, nullable=False),
    Column("key", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("paper_id", "source", "key"),
    CheckConstraint("source IN ('openalex')", name="source"),
    CheckConstraint(
        "key ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$' AND char_length(key) <= 200", name="key_format"
    ),
    CheckConstraint("display_name <> ''", name="display_name_not_empty"),
)

# `upsert_by_external_id` 收的 origin；`manual` 只能經之後的 `create_manual`。
UPSERT_ORIGINS = frozenset({PaperOrigin.TRACKED, PaperOrigin.EXPANSION, PaperOrigin.IMPORT})

_PAPER_COLUMNS = (
    "id, origin, title, publication_year, journal_issn_l, in_scope, excluded_reason,"
    " openalex_fetched_at, created_at, updated_at, authors, volume, issue, first_page,"
    " last_page, work_type, is_retracted, referenced_works, cited_by_count, source_hash"
)

# 每個資料來源寫的是哪一種摘要來源（沒有摘要時刪掉的就是這一列）。
_ABSTRACT_SOURCE_OF = {"openalex": AbstractSource.OPENALEX}


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
        authors=tuple(row["authors"]),
        volume=row["volume"],
        issue=row["issue"],
        first_page=row["first_page"],
        last_page=row["last_page"],
        work_type=row["work_type"],
        is_retracted=row["is_retracted"],
        referenced_works=tuple(row["referenced_works"]),
        cited_by_count=row["cited_by_count"],
        source_hash=row["source_hash"],
    )


def find_known_external_ids(
    viewer: Viewer, connection: Connection, ids: Iterable[ExternalId]
) -> frozenset[ExternalId]:
    """`ids` 裡已經掛在 `viewer` 看得到的論文上的那些識別碼。"""
    visible = visible_papers(viewer, alias="paper")
    wanted = sorted(set(ids), key=lambda external_id: (external_id.scheme, external_id.value_norm))
    if not wanted:
        return frozenset()
    rows = connection.execute(
        text(
            "SELECT external_id.scheme, external_id.value_norm FROM external_id"
            " JOIN paper ON paper.id = external_id.paper_id"
            " JOIN unnest(CAST(:schemes AS text[]), CAST(:values AS text[]))"
            " AS wanted(scheme, value_norm)"
            " ON wanted.scheme = external_id.scheme AND wanted.value_norm = external_id.value_norm"
            f" WHERE {visible}"
        ),
        {
            "schemes": [external_id.scheme.value for external_id in wanted],
            "values": [external_id.value_norm for external_id in wanted],
        },
    )
    return frozenset(
        ExternalId(IdentifierScheme(scheme), value_norm) for scheme, value_norm in rows
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

    CREATED／UPDATED 時一併寫摘要（`_write_abstract`）：有摘要就「有就更新、沒有才新增」這個
    來源的那一列，沒有摘要就刪掉這個來源的那一列，接著重新決定這篇的 `is_current`。
    STALE 時論文、識別碼、摘要都不寫。關鍵字不在這裡寫（`update_paper_keywords`）。

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
        "work_type": paper.work_type,
        "is_retracted": paper.is_retracted,
        "volume": paper.volume,
        "issue": paper.issue,
        "first_page": paper.first_page,
        "last_page": paper.last_page,
        "authors": list(paper.authors),
        "referenced_works": list(paper.referenced_works),
        "cited_by_count": paper.cited_by_count,
        "source_hash": paper.source_hash,
    }

    owners = _find_owners(connection, ids)
    target = _first_owner(ids, owners)
    if target is None:
        created = _insert_new(connection, ids, values, origin)
        if created is not None:
            _write_abstract(connection, created.paper_id, paper)
            return created
        # 並行的另一個交易剛建好同一篇（已經 commit，所以現在讀得到）。
        owners = _find_owners(connection, ids)
        target = _first_owner(ids, owners)
        if target is None:
            raise RuntimeError("識別碼認領失敗，卻找不到擁有它的論文")
    result = _update_existing(connection, target, ids, owners, values, paper.fetched_at)
    if result.outcome is UpsertOutcome.UPDATED:
        _write_abstract(connection, result.paper_id, paper)
    return result


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
                    " in_scope, excluded_reason, lab_visible, openalex_fetched_at,"
                    " work_type, is_retracted, volume, issue, first_page, last_page,"
                    " authors, referenced_works, cited_by_count, source_hash)"
                    " VALUES (:origin, :title, :publication_year, :journal_issn_l, :in_scope,"
                    " :excluded_reason, :shares_with_lab, :fetched_at,"
                    " :work_type, :is_retracted, :volume, :issue, :first_page, :last_page,"
                    " CAST(:authors AS text[]), CAST(:referenced_works AS text[]),"
                    " :cited_by_count, :source_hash)"
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
            " work_type = :work_type,"
            " is_retracted = :is_retracted,"
            " volume = :volume,"
            " issue = :issue,"
            " first_page = :first_page,"
            " last_page = :last_page,"
            " authors = CAST(:authors AS text[]),"
            " referenced_works = CAST(:referenced_works AS text[]),"
            " cited_by_count = :cited_by_count,"
            " source_hash = :source_hash,"
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


def _write_abstract(connection: Connection, paper_id: uuid.UUID, paper: ParsedPaper) -> None:
    """寫這篇論文、這個來源的摘要，再重新決定 `is_current`。

    - 有摘要：這個來源（`paper.abstract_source`）的那一列「有就更新、沒有才新增」。
    - 沒有摘要：刪掉這個資料來源對應的那一列（例如 OpenAlex 的資料 → `openalex` 那一列）。
      原始資料都還在，需要時可以 reparse 找回（ADR-0022）。
    - 接著依 `ABSTRACT_SOURCE_PRIORITY` 選出目前的那一筆：先把其他列的 `is_current` 清掉、
      再設這一筆（部分唯一索引不允許同時有兩筆）；`uploaded_pdf` 永遠不是目前的。
    """
    if paper.abstract is not None and paper.abstract_source is not None:
        connection.execute(
            text(
                "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
                " VALUES (:paper_id, :source, :content, false)"
                " ON CONFLICT (paper_id, source) DO UPDATE"
                " SET content = EXCLUDED.content, updated_at = now()"
                " WHERE paper_abstract.content IS DISTINCT FROM EXCLUDED.content"
            ),
            {
                "paper_id": paper_id,
                "source": paper.abstract_source.value,
                "content": paper.abstract,
            },
        )
    else:
        connection.execute(
            text("DELETE FROM paper_abstract WHERE paper_id = :paper_id AND source = :source"),
            {"paper_id": paper_id, "source": _ABSTRACT_SOURCE_OF[paper.source].value},
        )
    _choose_current_abstract(connection, paper_id)


def _choose_current_abstract(connection: Connection, paper_id: uuid.UUID) -> None:
    priority = [source.value for source in ABSTRACT_SOURCE_PRIORITY]
    current: uuid.UUID | None = connection.execute(
        text(
            "SELECT id FROM paper_abstract"
            " WHERE paper_id = :paper_id AND source = ANY(CAST(:priority AS text[]))"
            " ORDER BY array_position(CAST(:priority AS text[]), source)"
            " LIMIT 1"
        ),
        {"paper_id": paper_id, "priority": priority},
    ).scalar_one_or_none()
    connection.execute(
        text(
            "UPDATE paper_abstract SET is_current = false, updated_at = now()"
            " WHERE paper_id = :paper_id AND is_current"
            " AND id IS DISTINCT FROM CAST(:current AS uuid)"
        ),
        {"paper_id": paper_id, "current": current},
    )
    if current is not None:
        connection.execute(
            text(
                "UPDATE paper_abstract SET is_current = true, updated_at = now()"
                " WHERE id = :id AND NOT is_current"
            ),
            {"id": current},
        )


def update_paper_keywords(
    connection: Connection,
    paper_id: uuid.UUID,
    source: KeywordSource,
    keywords: Sequence[ParsedKeyword],
) -> None:
    """整批覆寫這篇論文、這個來源的關鍵字：先刪掉、再新增（ADR-0014 決定第 5 項、ADR-0016）。

    在呼叫端開好的交易裡做，不 commit。每週套用與 `reparse` 在寫入結果不是 STALE 時呼叫；
    單篇查詢不寫 OpenAlex 的關鍵字。

    Raises:
        ValueError: `source` 不是 `KeywordSource`、`keywords` 有不是 `ParsedKeyword` 的、
            或同一個 key 出現兩次（不碰資料庫）。
    """
    source = KeywordSource(source)
    if not all(isinstance(keyword, ParsedKeyword) for keyword in keywords):
        raise ValueError("keywords 只能放 ParsedKeyword")
    keys = [keyword.key for keyword in keywords]
    if len(set(keys)) != len(keys):
        raise ValueError("同一個關鍵字 key 只能出現一次")
    connection.execute(
        text("DELETE FROM paper_keyword WHERE paper_id = :paper_id AND source = :source"),
        {"paper_id": paper_id, "source": source.value},
    )
    if not keywords:
        return
    connection.execute(
        text(
            "INSERT INTO paper_keyword (paper_id, source, key, display_name)"
            " SELECT :paper_id, :source, key, display_name"
            " FROM unnest(CAST(:keys AS text[]), CAST(:names AS text[])) AS k(key, display_name)"
        ),
        {
            "paper_id": paper_id,
            "source": source.value,
            "keys": keys,
            "names": [keyword.display_name for keyword in keywords],
        },
    )


def update_papers_first_snapshot(connection: Connection, snapshot_id: uuid.UUID) -> int:
    """把還沒有著落的追蹤範圍內論文一律指到這個快照；回傳指了幾篇（AD-19、ADR-0011 快照第 3 點）。

    「還沒有著落」：`in_scope` 為真，而 `first_snapshot_id` 是 NULL 或指向狀態不是 `published` 的
    快照（例如上一次失敗的）。已經指到已發佈快照的論文不動，範圍外的論文不指。

    只有每週更新的套用交易呼叫（架構測試守著）；在呼叫端開好的交易裡做，不 commit。
    """
    rows = connection.execute(
        text(
            "UPDATE paper SET first_snapshot_id = :snapshot_id, updated_at = now()"
            " WHERE in_scope"
            " AND first_snapshot_id IS DISTINCT FROM :snapshot_id"
            " AND (first_snapshot_id IS NULL OR first_snapshot_id IN"
            " (SELECT id FROM snapshot WHERE status <> 'published'))"
        ),
        {"snapshot_id": snapshot_id},
    )
    return rows.rowcount


def _warn_conflict(paper_id: uuid.UUID, external_id: ExternalId, owner: uuid.UUID) -> None:
    log.warning(
        "識別碼已經屬於另一篇論文，沒有搬動也沒有合併",
        paper_id=str(paper_id),
        scheme=external_id.scheme.value,
        value_norm=external_id.value_norm,
        owner_paper_id=str(owner),
    )
