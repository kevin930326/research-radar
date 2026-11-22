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
- `demo_eligible` 由 `upsert_by_external_id` 依 `is_demo_eligible` 寫；另外只有
  `update_papers_not_demo_eligible`（`reparse` 結束時的重判）把 true 改成 false（ADR-0052）。
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

from radar.domain.ai_outputs import CurrentAbstract, PaperLlmSource
from radar.domain.annotations import AnnotationPaperFacts, AnnotationPaperText
from radar.domain.identifiers import ExternalId, IdentifierScheme
from radar.domain.llm_batches import PaperAiSource
from radar.domain.paper_page import PaperPageFacts
from radar.domain.papers import (
    ABSTRACT_SOURCE_PRIORITY,
    AbstractSource,
    KeywordSource,
    PaperOrigin,
    ParsedKeyword,
    ParsedPaper,
    SameSchemeMismatch,
    SplitIdentifiers,
    StoredDemoFacts,
    StoredPaper,
    UpsertOutcome,
    UpsertResult,
    check_storable_text,
)
from radar.domain.policy import is_demo_eligible
from radar.domain.processing import ProcessingFacts
from radar.domain.scope import ScopeSettings, is_in_scope
from radar.domain.trial_runs import TrialCandidate
from radar.domain.viewer import SystemViewer, Viewer
from radar.repo.db import Database
from radar.repo.models import Base
from radar.repo.visibility import visible_in_own_library, visible_papers, visible_to_system_only

log = structlog.get_logger(__name__)

# --- 表的定義（`alembic check` 拿來和資料庫比對；遷移 0003 建表、0005 加欄位與兩張表、0007 加
# `first_snapshot_id`、0009 加 `source_changed_at`、0021 加授權、出版社、`demo_eligible`） -----

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
    # 由遷移 0009 加（Story 1.11）：寫入時 `source_hash` 變了的最後時間；NULL＝寫入以來沒變過。
    # 只由 `_update_existing` 寫（ADR-0028）。
    Column("source_changed_at", DateTime(timezone=True)),
    # 由遷移 0021 加（Story 3.7a、ADR-0052）：出版社自己那一版（OpenAlex `primary_location`）的
    # 授權原字串、出版社 ID 與名稱（含 lineage），以及由 `domain/policy.is_demo_eligible` 算好的
    # 「示範站可用」。都由 `upsert_by_external_id` 寫；`demo_eligible` 另外只有
    # `update_papers_not_demo_eligible`（reparse 結束時的重判）會把 true 改成 false。都不算進
    # `source_hash`。
    Column("primary_license", Text),
    Column("publisher_ids", ARRAY(Text), nullable=False, server_default=_EMPTY_TEXT_ARRAY),
    Column("publisher_names", ARRAY(Text), nullable=False, server_default=_EMPTY_TEXT_ARRAY),
    Column("demo_eligible", Boolean, nullable=False, server_default=text("false")),
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
    CheckConstraint(
        "primary_license IS NULL OR primary_license <> ''", name="primary_license_not_empty"
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
    " last_page, work_type, is_retracted, referenced_works, cited_by_count, source_hash,"
    " source_changed_at"
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
        source_changed_at=row["source_changed_at"],
    )


def find_paper_llm_source(
    viewer: Viewer, connection: Connection, paper_id: uuid.UUID, *, for_update: bool
) -> PaperLlmSource | None:
    """組 LLM 輸入要用的資料列：論文標題與目前的摘要（`is_current` 的那一筆，沒有時是 None）。

    `for_update` 為真時先鎖住論文那一列（`FOR UPDATE`），再讀標題與摘要：`services/ai_results.py`
    寫成 `active` 之前先鎖住論文，核對與切換才不會被插隊（ADR-0009 第 8 步）。不存在或 `viewer`
    看不到都回 None。
    """
    visible = visible_papers(viewer, alias="paper")
    if for_update:
        # 先單獨鎖論文列，再另一句讀摘要：鎖和 JOIN 寫在同一句時，等鎖結束後 PostgreSQL 只重新檢查
        # 被鎖的論文列，JOIN 進來的摘要仍是等鎖前的快照，核對會拿到舊摘要。分開寫，第二句在
        # READ COMMITTED 下會看到等鎖期間別人 commit 的摘要。
        locked = connection.execute(
            text(f"SELECT paper.id FROM paper WHERE {visible} AND paper.id = :id FOR UPDATE"),
            {"id": paper_id},
        ).one_or_none()
        if locked is None:
            return None
    row = (
        connection.execute(
            text(
                "SELECT paper.id, paper.title, a.id AS abstract_id, a.source AS abstract_source,"
                " a.content AS abstract_content"
                " FROM paper LEFT JOIN paper_abstract AS a"
                " ON a.paper_id = paper.id AND a.is_current"
                f" WHERE {visible} AND paper.id = :id"
            ),
            {"id": paper_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    abstract = None
    if row["abstract_id"] is not None:
        abstract = CurrentAbstract(
            id=row["abstract_id"],
            source=AbstractSource(row["abstract_source"]),
            content=row["abstract_content"],
        )
    return PaperLlmSource(paper_id=row["id"], title=row["title"], abstract=abstract)


def find_paper_processing_facts(
    viewer: Viewer, connection: Connection, paper_id: uuid.UUID
) -> ProcessingFacts | None:
    """`domain/processing.plan_for` 要看的三件事：`origin`、`excluded_reason`、目前的摘要內容
    （`is_current` 的那一筆，沒有時是 None）。不存在或 `viewer` 看不到都回 None。"""
    visible = visible_papers(viewer, alias="paper")
    row = (
        connection.execute(
            text(
                "SELECT paper.origin, paper.excluded_reason, a.content AS abstract_content"
                " FROM paper LEFT JOIN paper_abstract AS a"
                " ON a.paper_id = paper.id AND a.is_current"
                f" WHERE {visible} AND paper.id = :id"
            ),
            {"id": paper_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return ProcessingFacts(
        origin=PaperOrigin(row["origin"]),
        excluded_reason=row["excluded_reason"],
        abstract=row["abstract_content"],
    )


def find_paper_page_facts(
    viewer: Viewer, connection: Connection, paper_id: uuid.UUID
) -> PaperPageFacts | None:
    """論文頁要的論文欄位（Story 3.6）：書目、狀態標記、最早記上的 DOI 與 OpenAlex ID
    （`external_id.created_at, id` 最前面的一個）、目前的摘要（`is_current` 那一筆）。

    一句帶可見條件的查詢：不存在、`viewer` 看不到、被排除的非研究文章（`excluded_reason` 有值，
    ADR-0015「被排除的非研究文章沒有論文頁」）都回 None，不先查存在或排除（ADR-0002）。
    `in_my_library` 是同一句查詢裡的一欄（`visibility.visible_in_own_library`，Story 3.17）。
    """
    visible = visible_papers(viewer, alias="paper")
    mine = visible_in_own_library(viewer, alias="paper")
    row = (
        connection.execute(
            text(
                "SELECT paper.id, paper.origin, paper.title, paper.authors,"
                " paper.publication_year, paper.journal_issn_l, paper.volume, paper.issue,"
                " paper.first_page, paper.last_page, paper.cited_by_count, paper.in_scope,"
                " paper.is_retracted,"
                " (SELECT d.value_norm FROM external_id AS d"
                "  WHERE d.paper_id = paper.id AND d.scheme = :doi"
                "  ORDER BY d.created_at, d.id LIMIT 1) AS doi,"
                " (SELECT o.value_norm FROM external_id AS o"
                "  WHERE o.paper_id = paper.id AND o.scheme = :openalex"
                "  ORDER BY o.created_at, o.id LIMIT 1) AS openalex_id,"
                " a.id AS abstract_id, a.source AS abstract_source,"
                " a.content AS abstract_content,"
                f" {mine} AS in_my_library"
                " FROM paper LEFT JOIN paper_abstract AS a"
                " ON a.paper_id = paper.id AND a.is_current"
                f" WHERE {visible} AND paper.excluded_reason IS NULL AND paper.id = :id"
            ),
            {
                "id": paper_id,
                "doi": IdentifierScheme.DOI.value,
                "openalex": IdentifierScheme.OPENALEX.value,
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    abstract = None
    if row["abstract_id"] is not None:
        abstract = CurrentAbstract(
            id=row["abstract_id"],
            source=AbstractSource(row["abstract_source"]),
            content=row["abstract_content"],
        )
    return PaperPageFacts(
        paper_id=row["id"],
        origin=PaperOrigin(row["origin"]),
        title=row["title"],
        authors=tuple(row["authors"]),
        publication_year=row["publication_year"],
        journal_issn_l=row["journal_issn_l"],
        volume=row["volume"],
        issue=row["issue"],
        first_page=row["first_page"],
        last_page=row["last_page"],
        cited_by_count=row["cited_by_count"],
        in_scope=row["in_scope"],
        is_retracted=row["is_retracted"],
        doi=row["doi"],
        openalex_id=row["openalex_id"],
        abstract=abstract,
        in_my_library=row["in_my_library"],
    )


def list_paper_ai_sources(
    viewer: SystemViewer, connection: Connection, *, after_id: uuid.UUID | None, limit: int
) -> list[PaperAiSource]:
    """依論文 id 順序讀一頁論文（`after_id` 之後、最多 `limit` 篇）：處理事實（`plan_for` 用）、
    標題與目前的摘要（組 LLM 輸入用）。全部論文都讀（要不要做由 `plan_for` 判斷，這裡不篩）；
    Batch 規劃用（Story 2.9）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: `limit` 不是正整數。
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("limit 要是正整數")
    visible_to_system_only(viewer)
    visible = visible_papers(viewer, alias="paper")
    after = "" if after_id is None else " AND paper.id > :after_id"
    rows = (
        connection.execute(
            text(
                "SELECT paper.id, paper.title, paper.origin, paper.excluded_reason,"
                " a.id AS abstract_id, a.source AS abstract_source,"
                " a.content AS abstract_content"
                " FROM paper LEFT JOIN paper_abstract AS a"
                " ON a.paper_id = paper.id AND a.is_current"
                f" WHERE {visible}{after} ORDER BY paper.id LIMIT :limit"
            ),
            {"after_id": after_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    sources: list[PaperAiSource] = []
    for row in rows:
        abstract = None
        if row["abstract_id"] is not None:
            abstract = CurrentAbstract(
                id=row["abstract_id"],
                source=AbstractSource(row["abstract_source"]),
                content=row["abstract_content"],
            )
        sources.append(
            PaperAiSource(
                facts=ProcessingFacts(
                    origin=PaperOrigin(row["origin"]),
                    excluded_reason=row["excluded_reason"],
                    abstract=row["abstract_content"],
                ),
                source=PaperLlmSource(paper_id=row["id"], title=row["title"], abstract=abstract),
            )
        )
    return sources


def find_paper_ids_by_dois(
    viewer: Viewer, connection: Connection, dois: Iterable[str]
) -> dict[str, uuid.UUID]:
    """正規化後的 DOI → 掛著它的論文 id（`viewer` 看得到的）；找不到的不在結果裡。
    `radar runs trial` 用 DOI 指定論文時用（Story 2.10）。"""
    visible = visible_papers(viewer, alias="paper")
    wanted = sorted(set(dois))
    if not wanted:
        return {}
    rows = connection.execute(
        text(
            "SELECT external_id.value_norm, external_id.paper_id FROM external_id"
            " JOIN paper ON paper.id = external_id.paper_id"
            " WHERE external_id.scheme = :scheme"
            " AND external_id.value_norm = ANY(CAST(:values AS text[]))"
            f" AND {visible}"
        ),
        {"scheme": IdentifierScheme.DOI.value, "values": wanted},
    )
    return {value_norm: uuid.UUID(str(paper_id)) for value_norm, paper_id in rows}


def list_paper_dois(
    viewer: Viewer, connection: Connection, paper_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, ...]]:
    """這些論文（`viewer` 看得到、存在的）各自的 DOI（正規化後、排好序；沒有 DOI 是空的）；
    不存在的論文不在結果裡。調整組／測試組只看 DOI（Story 2.10）。"""
    visible = visible_papers(viewer, alias="paper")
    wanted = sorted(set(paper_ids))
    if not wanted:
        return {}
    rows = connection.execute(
        text(
            "SELECT paper.id, COALESCE(array_agg(external_id.value_norm"
            " ORDER BY external_id.value_norm)"
            " FILTER (WHERE external_id.value_norm IS NOT NULL), '{}') AS dois"
            " FROM paper LEFT JOIN external_id"
            " ON external_id.paper_id = paper.id AND external_id.scheme = :scheme"
            f" WHERE paper.id = ANY(CAST(:ids AS uuid[])) AND {visible}"
            " GROUP BY paper.id"
        ),
        {"scheme": IdentifierScheme.DOI.value, "ids": wanted},
    )
    return {uuid.UUID(str(paper_id)): tuple(dois) for paper_id, dois in rows}


def count_abstracts_from_source(
    viewer: SystemViewer, connection: Connection, source: AbstractSource
) -> int:
    """來源是 `source` 的摘要有幾筆（不管是不是目前的摘要；不接 `paper`，不是統計用的篇數）。

    `radar backup --shared-only` 的擋下條件（Story 3.18 審查 S3）：成員上傳的 PDF 摘要
    （`uploaded_pdf`，只有上傳者看得到、永遠不是目前的）不能帶到只有共用資料的備份。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    value = connection.execute(
        text(f"SELECT count(*) FROM paper_abstract WHERE {visible} AND source = :source"),
        {"source": AbstractSource(source).value},
    ).scalar_one()
    return int(value)


def count_papers_with_current_abstract_source(
    viewer: SystemViewer, connection: Connection, source: AbstractSource
) -> int:
    """目前的摘要（`is_current`）是 `source` 的論文有幾篇（每篇最多一筆 `is_current`，所以數摘要列
    就是篇數；不接 `paper`，不是統計用的篇數）。`radar runs remove-by-source` 的擋下條件：這些論文
    下次規劃會用同一份摘要重新產生（Story 2.12）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible = visible_to_system_only(viewer)
    value = connection.execute(
        text(
            f"SELECT count(*) FROM paper_abstract WHERE {visible}"
            " AND is_current AND source = :source"
        ),
        {"source": AbstractSource(source).value},
    ).scalar_one()
    return int(value)


def list_trial_candidates(
    viewer: SystemViewer, connection: Connection, *, after_id: uuid.UUID | None, limit: int
) -> list[TrialCandidate]:
    """依論文 id 順序讀一頁抽樣候選（`after_id` 之後、最多 `limit` 篇）：id、DOI 與
    `plan_for` 要看的處理事實（組成 `TrialCandidate` 後就不留摘要內容）。全部論文都讀，要不要抽由
    domain 判斷；`radar runs trial --sample` 用（Story 2.10）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: `limit` 不是正整數。
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("limit 要是正整數")
    visible_to_system_only(viewer)
    visible = visible_papers(viewer, alias="paper")
    after = "" if after_id is None else " AND paper.id > :after_id"
    rows = (
        connection.execute(
            text(
                "SELECT paper.id, paper.origin, paper.excluded_reason,"
                " a.content AS abstract_content,"
                " ARRAY(SELECT e.value_norm FROM external_id AS e"
                " WHERE e.paper_id = paper.id AND e.scheme = :scheme"
                " ORDER BY e.value_norm) AS dois"
                " FROM paper LEFT JOIN paper_abstract AS a"
                " ON a.paper_id = paper.id AND a.is_current"
                f" WHERE {visible}{after} ORDER BY paper.id LIMIT :limit"
            ),
            {"scheme": IdentifierScheme.DOI.value, "after_id": after_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [
        TrialCandidate.from_facts(
            row["id"],
            row["dois"],
            ProcessingFacts(
                origin=PaperOrigin(row["origin"]),
                excluded_reason=row["excluded_reason"],
                abstract=row["abstract_content"],
            ),
        )
        for row in rows
    ]


def list_annotation_candidates(
    viewer: SystemViewer, connection: Connection, *, after_id: uuid.UUID | None, limit: int
) -> list[AnnotationPaperFacts]:
    """依論文 id 順序讀一頁標註抽樣的候選（`after_id` 之後、最多 `limit` 篇）：只讀 view
    `counted_paper` 裡的論文（追蹤範圍內、沒被排除、在已發佈的快照），每篇的 ISSN-L、出版年、
    有沒有目前的摘要、DOI（正規化後、排好序）。不讀標題與摘要內容、不做彙總（篇數在 domain 算）；
    `radar annotations template` 用（Story 2.13）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: `limit` 不是正整數。
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("limit 要是正整數")
    visible_to_system_only(viewer)
    visible = visible_papers(viewer, alias="c")
    after = "" if after_id is None else " AND c.id > :after_id"
    rows = connection.execute(
        text(
            "SELECT c.id, c.journal_issn_l, c.publication_year,"
            " EXISTS (SELECT 1 FROM paper_abstract AS a"
            " WHERE a.paper_id = c.id AND a.is_current) AS has_abstract,"
            " ARRAY(SELECT e.value_norm FROM external_id AS e"
            " WHERE e.paper_id = c.id AND e.scheme = :scheme"
            " ORDER BY e.value_norm) AS dois"
            f" FROM counted_paper AS c WHERE {visible}{after} ORDER BY c.id LIMIT :limit"
        ),
        {"scheme": IdentifierScheme.DOI.value, "after_id": after_id, "limit": limit},
    )
    return [
        AnnotationPaperFacts(
            paper_id=uuid.UUID(str(paper_id)),
            journal_issn_l=issn_l,
            publication_year=year,
            has_abstract=bool(has_abstract),
            dois=tuple(dois),
        )
        for paper_id, issn_l, year, has_abstract, dois in rows
    ]


def find_annotation_paper_texts(
    viewer: SystemViewer, connection: Connection, paper_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, AnnotationPaperText]:
    """這些論文的標題、目前的摘要（沒有是 None）與 DOI（正規化後、排好序）；不存在的論文不在結果
    裡。產生標註試算表用（Story 2.13）。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
    """
    visible_to_system_only(viewer)
    visible = visible_papers(viewer, alias="paper")
    wanted = sorted(set(paper_ids))
    if not wanted:
        return {}
    rows = connection.execute(
        text(
            "SELECT paper.id, paper.title, a.content,"
            " ARRAY(SELECT e.value_norm FROM external_id AS e"
            " WHERE e.paper_id = paper.id AND e.scheme = :scheme"
            " ORDER BY e.value_norm) AS dois"
            " FROM paper LEFT JOIN paper_abstract AS a"
            " ON a.paper_id = paper.id AND a.is_current"
            f" WHERE paper.id = ANY(CAST(:ids AS uuid[])) AND {visible}"
        ),
        {"scheme": IdentifierScheme.DOI.value, "ids": wanted},
    )
    return {
        uuid.UUID(str(paper_id)): AnnotationPaperText(title, abstract, tuple(dois))
        for paper_id, title, abstract, dois in rows
    }


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
       回 `stale`；一樣新或比較新：先把還沒有主人的識別碼掛上來，再寫論文（順序見第 5 點）。
    3. 沒有：在保存點裡新增論文，再用 `ON CONFLICT DO NOTHING` 依優先序認領識別碼。任何一個
       認領不到（開始找的時候都沒有主人，所以只可能是並行的另一個交易剛建好同一篇；會等那個交易
       結束才知道），就退回保存點：剛新增的論文與剛認領的識別碼一起放掉，再改走第 2 步。結果和
       兩個交易依序執行時一樣。
    4. 識別碼已經屬於另一篇論文時不搬、不合併。只有 DOI 與 OpenAlex ID 兩種識別碼，而且至少
       一個屬於目標論文，所以這種情況一定是第 5 點的「識別碼分屬兩篇」；判成 `stale` 時不掛任何
       識別碼。原本放這種識別碼的結果欄位 `conflicts` 已經拿掉（ADR-0020 更新 2026-11-22）。
    5. DOI 與 OpenAlex ID 屬於兩篇不同的既有論文（「識別碼分屬兩篇」，ADR-0020 更新 2026-11-22）：
       兩篇都什麼都不寫（欄位、識別碼、抓取時間、`demo_eligible`、`lab_visible`、摘要），回
       `identifiers_split`（`split` 帶兩個識別碼與各自的論文），記一行警告日誌（只帶 id 與識別碼）。
       三個地方會發現：第 1 步找的時候（在比新舊之前，所以比較舊的資料也回這個、不回 `stale`）；
       第 3 步退回保存點後重查的時候；第 2 步認領還沒有主人的識別碼、卻被並行的交易搶先掛到別篇
       的時候（這一步在寫論文之前做，所以那時也還沒寫任何東西；已經判成 `stale` 的不會走到這裡）。
       因為只有 DOI 與 OpenAlex ID 兩種識別碼，第 2 步至少有一個識別碼屬於目標論文，最多只認領
       一個，認領失敗時手上沒有掛上任何識別碼。
    6. 用一個識別碼找到論文 P、帶來的另一種識別碼沒有主人，而 P 已經有同一種、正規化後不同值的
       識別碼（「同一種識別碼對不上」，ADR-0020 更新 2026-11-22 第二節）：什麼都不寫（同第 5 點），
       回 `same_scheme_mismatch`（`mismatch` 帶 P 的 id 與同一種識別碼的兩個值），記一行警告日誌
       （只帶 id、識別碼的種類與兩個值）。只在第 2 步鎖住 P 之後判斷一次（第 1 步與第 3 步重查
       走到第 2 步都經過這裡），在比新舊與認領之前：要替 P 掛識別碼的交易都得先鎖 P，所以鎖到之後
       讀到的 P 的識別碼到交易結束都不會再多；並行的另一個交易先替 P 掛上別的值、已 commit 的，
       鎖到之後這一句也讀得到，兩個交易最多只有一個掛上。這時 P 鎖到交易結束但沒有寫。
       「分屬兩篇」在找的時候（鎖之前）就判斷，所以鎖之前就看得到兩篇的，回 `identifiers_split`。
       找完到鎖住 P 之間，並行的另一個交易才把這筆帶來的另一種識別碼掛到別篇 Q（並 commit）時：
       只有 P 鎖住後已經有同一種、不同值的識別碼，這一筆才會回 `same_scheme_mismatch`（警告只帶
       P、不帶 Q），不回 `identifiers_split`；兩篇都一樣什麼都不寫，只差在結果與日誌的分類。P 沒有
       那一種識別碼時，這一筆會通過這個判斷，認領時查到主人是 Q，照舊回 `identifiers_split`
       （第 5 點；ADR-0020 更新 2026-11-22 第二節第 3 點）。

    鎖的順序（ADR-0020）：先鎖論文、再佔識別碼，識別碼依優先序佔；第 3 步改走第 2 步之前先退回
    保存點，手上不留任何識別碼，才去等論文的鎖。所以每個交易只寫一篇時，不論幾個交易同時寫、
    識別碼怎麼重疊，都不會互相等成死結。一個交易寫很多篇（每週套用、`reparse`）時，跨論文的
    順序固定不了，和其他交易（含只寫一篇的）仍可能死結；PostgreSQL 會中止其中一個交易，
    那個交易要整個重跑。

    `in_scope` 由 `is_in_scope` 算；`lab_visible`＝原本的值 OR `in_scope` OR `origin` 是
    `expansion`；`origin` 只在新增時寫；`excluded_reason` 由呼叫端明確決定、整欄覆寫。
    `demo_eligible` 由 `is_demo_eligible` 算（授權、出版社、期刊 ISSN-L、`scope.demo_publishers`；
    ADR-0052），
    和授權、出版社欄位在 CREATED／UPDATED 時一起整欄覆寫；STALE 時都不寫。

    CREATED／UPDATED 時一併寫摘要（`_write_abstract`）：有摘要就「有就更新、沒有才新增」這個
    來源的那一列，沒有摘要就刪掉這個來源的那一列，接著重新決定這篇的 `is_current`。
    STALE 時論文、識別碼、摘要都不寫。關鍵字不在這裡寫（`update_paper_keywords`）。

    UPDATED 時比對舊的 `source_hash`：變了（含原本是 NULL）就寫 `source_changed_at`（拿到論文
    列鎖之後的 `clock_timestamp()`）、結果的 `source_changed` 為真（每週更新與 `reparse` 都經
    這裡，所以都會記；ADR-0028）。
    CREATED 時 `source_changed_at` 留 NULL、`source_changed` 為假。

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
    demo_eligible = is_demo_eligible(
        paper.primary_license,
        paper.publisher_ids,
        paper.publisher_names,
        scope.demo_publishers,
        journal_issn_l=paper.journal_issn_l,
    )
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
        "primary_license": paper.primary_license,
        "publisher_ids": list(paper.publisher_ids),
        "publisher_names": list(paper.publisher_names),
        "demo_eligible": demo_eligible,
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
    split = _split_result(owners)
    if split is not None:
        return split
    result = _update_existing(connection, target, ids, owners, values, paper.fetched_at)
    if result.outcome is UpsertOutcome.UPDATED:
        _write_abstract(connection, result.paper_id, paper)
    return result


def _split_result(owners: Mapping[ExternalId, uuid.UUID]) -> UpsertResult | None:
    """DOI 與 OpenAlex ID 都有主人、而且是兩篇不同的論文時，記警告並回「識別碼分屬兩篇」。

    日誌只帶兩篇論文的 id 與兩個識別碼（不帶標題、摘要等內容）。
    """
    doi = next((i for i in owners if i.scheme is IdentifierScheme.DOI), None)
    openalex = next((i for i in owners if i.scheme is IdentifierScheme.OPENALEX), None)
    if doi is None or openalex is None or owners[doi] == owners[openalex]:
        return None
    split = SplitIdentifiers(
        doi=doi,
        doi_paper_id=owners[doi],
        openalex_id=openalex,
        openalex_paper_id=owners[openalex],
    )
    log.warning(
        "DOI 與 OpenAlex ID 屬於兩篇不同的論文，這一筆略過不寫",
        doi=doi.value_norm,
        doi_paper_id=str(split.doi_paper_id),
        openalex_id=openalex.value_norm,
        openalex_paper_id=str(split.openalex_paper_id),
    )
    return UpsertResult(split.doi_paper_id, UpsertOutcome.IDENTIFIERS_SPLIT, split=split)


def _same_scheme_mismatch(
    connection: Connection, paper_id: uuid.UUID, ids: Sequence[ExternalId]
) -> UpsertResult | None:
    """這篇已經有和這筆資料同一種、正規化後不同值的識別碼時，記警告並回「同一種識別碼對不上」。

    要在鎖住論文之後呼叫。依優先序看這筆資料的每個識別碼：這篇有同一種識別碼、但沒有一個等於
    帶來的值，就是對不上（找到這篇的那個識別碼一定屬於它，所以實際上只會是另一種）。這篇同一種
    有好幾個值時，日誌與結果帶排序後的第一個。日誌只帶論文 id、識別碼的種類與兩個值。
    """
    rows = connection.execute(
        text(
            "SELECT scheme, value_norm FROM external_id WHERE paper_id = :paper_id"
            " ORDER BY scheme, value_norm"
        ),
        {"paper_id": paper_id},
    )
    stored_by_scheme: dict[IdentifierScheme, list[str]] = {}
    for scheme, value_norm in rows:
        stored_by_scheme.setdefault(IdentifierScheme(scheme), []).append(value_norm)
    for external_id in ids:
        stored_values = stored_by_scheme.get(external_id.scheme, [])
        if not stored_values or external_id.value_norm in stored_values:
            continue
        mismatch = SameSchemeMismatch(
            paper_id=paper_id,
            stored=ExternalId(external_id.scheme, stored_values[0]),
            incoming=external_id,
        )
        log.warning(
            "論文已經有同一種、不同值的識別碼，這一筆略過不寫",
            paper_id=str(paper_id),
            scheme=external_id.scheme.value,
            stored=mismatch.stored.value_norm,
            incoming=external_id.value_norm,
        )
        return UpsertResult(paper_id, UpsertOutcome.SAME_SCHEME_MISMATCH, mismatch=mismatch)
    return None


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
                    " authors, referenced_works, cited_by_count, source_hash,"
                    " primary_license, publisher_ids, publisher_names, demo_eligible)"
                    " VALUES (:origin, :title, :publication_year, :journal_issn_l, :in_scope,"
                    " :excluded_reason, :shares_with_lab, :fetched_at,"
                    " :work_type, :is_retracted, :volume, :issue, :first_page, :last_page,"
                    " CAST(:authors AS text[]), CAST(:referenced_works AS text[]),"
                    " :cited_by_count, :source_hash,"
                    " :primary_license, CAST(:publisher_ids AS text[]),"
                    " CAST(:publisher_names AS text[]), :demo_eligible)"
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
    """先鎖住論文，看同一種識別碼對不對得上，再比抓取時間；對不上就不寫（`same_scheme_mismatch`），
    比較舊就不寫（`stale`，不掛識別碼），否則先掛上還沒有主人的識別碼、再更新論文。

    掛識別碼時被並行的交易搶先掛到另一篇（認領失敗、主人不是這篇）：DOI 與 OpenAlex ID 分屬兩篇，
    什麼都不寫、回 `identifiers_split`（ADR-0020 更新 2026-11-22）。所以先掛識別碼、後寫論文：
    寫論文之前就知道要不要略過。鎖的順序不變（先鎖論文、再依優先序佔識別碼）。

    鎖論文時一起讀舊的 `source_hash`：要寫的不同（含舊的是 NULL）就把 `source_changed_at` 設成
    `clock_timestamp()`、結果的 `source_changed` 為真；相同就保留原值（ADR-0028）。被引用次數、
    關鍵字、授權、出版社、`demo_eligible` 不在指紋裡，只改它們不算變動（照樣更新，不讓 AI 重做）。

    用 `clock_timestamp()`（這一句執行的時間）不用 `now()`（交易開始的時間）：這一句在拿到論文的
    列鎖之後才執行。等著每週套用放鎖的寫入，記下的時間會晚於那次快照的 `as_of`（也是
    `clock_timestamp()`，在同一個套用交易的最後），不會因為交易開得早而落在快照之前被漏掉
    （Story 1.11 Codex C3）。
    """
    stored, stored_hash = connection.execute(
        text("SELECT openalex_fetched_at, source_hash FROM paper WHERE id = :id FOR UPDATE"),
        {"id": paper_id},
    ).one()
    # 拿到鎖之後才讀這篇已經有的識別碼：要替這篇掛識別碼的交易都得先鎖它，所以從這裡到交易結束
    # 不會有人再替它掛上新的；並行的另一個交易剛掛上、已 commit 的，這一句也讀得到（READ
    # COMMITTED）。比新舊之前判斷，和「分屬兩篇」一樣（ADR-0020 更新 2026-11-22 第二節）。
    # 等鎖期間另一種識別碼被別篇掛走時：只有 P 這時已經有同一種、不同值的識別碼，這裡才回「同一種
    # 識別碼對不上」、不回「分屬兩篇」（兩種都不寫，只差分類）；P 沒有那一種的，會通過這裡，認領時
    # 查到主人是別篇，照舊回「分屬兩篇」（見 `upsert_by_external_id` 第 6 點）。
    mismatch = _same_scheme_mismatch(connection, paper_id, ids)
    if mismatch is not None:
        return mismatch
    if stored is not None and fetched_at < stored:
        return UpsertResult(paper_id, UpsertOutcome.STALE)
    source_changed = stored_hash != values["source_hash"]

    # 只有 DOI 與 OpenAlex ID 兩種識別碼，而且至少一個屬於這篇（找到它的那一個），所以這裡最多
    # 認領一個；認領失敗時手上沒有掛上任何識別碼，直接回「分屬兩篇」就什麼都沒寫。
    current = dict(owners)
    for external_id in ids:
        if external_id in current:
            continue
        if _claim(connection, paper_id, external_id):
            current[external_id] = paper_id
            continue
        current[external_id] = _owner_of(connection, external_id)
        split = _split_result(current)
        if split is not None:
            return split

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
            " source_changed_at = CASE WHEN :source_changed THEN clock_timestamp()"
            " ELSE source_changed_at END,"
            " primary_license = :primary_license,"
            " publisher_ids = CAST(:publisher_ids AS text[]),"
            " publisher_names = CAST(:publisher_names AS text[]),"
            " demo_eligible = :demo_eligible,"
            " updated_at = now()"
            " WHERE id = :id"
        ),
        {**values, "id": paper_id, "source_changed": source_changed},
    )

    # 識別碼屬於別篇的情況都在上面判成「分屬兩篇」了，走到這裡的識別碼都屬於這篇。
    return UpsertResult(paper_id, UpsertOutcome.UPDATED, source_changed=source_changed)


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


def list_demo_eligible_facts(
    viewer: SystemViewer, connection: Connection, *, after_id: uuid.UUID | None, limit: int
) -> list[StoredDemoFacts]:
    """依論文 id 順序讀一頁 `demo_eligible` 為真的論文（`after_id` 之後、最多 `limit` 篇）存下的
    授權、出版社與期刊 ISSN-L 欄位。`reparse` 結束時重判用（ADR-0052 第 9 點）；判定由呼叫端經
    `is_demo_eligible` 算，這裡不判斷。

    Raises:
        TypeError: `viewer` 不是 `SystemViewer`。
        ValueError: `limit` 不是正整數。
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("limit 要是正整數")
    visible_to_system_only(viewer)
    visible = visible_papers(viewer, alias="paper")
    after = "" if after_id is None else " AND paper.id > :after_id"
    rows = connection.execute(
        text(
            "SELECT paper.id, paper.primary_license, paper.publisher_ids, paper.publisher_names,"
            " paper.journal_issn_l"
            f" FROM paper WHERE {visible} AND paper.demo_eligible{after}"
            " ORDER BY paper.id LIMIT :limit"
        ),
        {"after_id": after_id, "limit": limit},
    )
    return [
        StoredDemoFacts(
            paper_id=paper_id,
            primary_license=primary_license,
            publisher_ids=tuple(publisher_ids),
            publisher_names=tuple(publisher_names),
            journal_issn_l=journal_issn_l,
        )
        for paper_id, primary_license, publisher_ids, publisher_names, journal_issn_l in rows
    ]


def update_papers_not_demo_eligible(connection: Connection, paper_ids: Sequence[uuid.UUID]) -> int:
    """把這幾篇的 `demo_eligible` 從 true 改成 false；回傳改了幾篇（ADR-0052 第 6、9 點）。

    只有 `reparse` 結束時的重判呼叫：哪幾篇要改由呼叫端經 `is_demo_eligible` 算好，這裡不判斷、
    不寫任何授權清單或名稱比對。`WHERE demo_eligible`：只會把 true 改成 false，原本就是 false 的
    不動。只寫 `demo_eligible`，`updated_at` 也不動（ADR-0052 第 9 點只准重判寫這一欄）。在呼叫端
    開好的交易裡做，不 commit。
    """
    if not paper_ids:
        return 0
    rows = connection.execute(
        text(
            "UPDATE paper SET demo_eligible = false"
            " WHERE id = ANY(CAST(:ids AS uuid[])) AND demo_eligible"
        ),
        {"ids": list(paper_ids)},
    )
    return rows.rowcount


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
