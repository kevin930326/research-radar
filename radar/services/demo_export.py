"""示範站匯出（Story 3.7、ADR-0007、ADR-0061、AD-20）：把示範站要的資料匯出成靜態檔。

流程（`run_demo_export`）：

1. 一個短交易把 `demo_export` 那一筆改成 `running`（已經結束的不做）。
2. **一個唯讀、REPEATABLE READ 的交易**（`Database.read_only_snapshot_transaction`）裡讀完全部的
   資料並做完自我檢查：只用 `PublicViewer` 經 repo 讀（最新已發佈的快照、每一篇 `counted_paper`
   的書目與分類、`demo_full_paper` 的摘要與兩個 AI 區塊），組成白名單模型、轉成 JSON；manifest
   也先組好；再用同一個交易讀成員 email（系統身分，只拿來比對、不寫進任何檔，ADR-0061），
   `domain/demo_export.check_demo_files` 檢查全部的檔。沒過就丟 `DemoExportCheckError`，**什麼都
   不寫**。
3. 交易結束後才寫檔：資料檔先寫進暫存目錄、整個改名成 `preview/<匯出編號>/`（目錄已經存在就失敗、
   不覆寫）；`preview/manifest.json` 最後寫（先寫暫存檔再換名，換的那一下是原子的）。
4. 一個短交易把那一筆改成 `succeeded`；任何一步失敗改成 `failed`（記原因代碼）再往上丟。

`RADAR_DEMO_PUBLISH` 預設關；這個 epic 不開：開了的話不讀資料、直接記成失敗（正式發佈還沒做）。
這個模組只寫 `preview/`，沒有寫 `data/` 的路徑。
"""

import os
import shutil
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog

from radar.domain.ai_blocks import AiBlock, AiBlockSettings, AiProduct
from radar.domain.context_values import ContextValues
from radar.domain.contracts import (
    ClassificationBlock,
    FullTextLinks,
    PaperAbstract,
    PaperBiblio,
    ResearchModelBlock,
    SnapshotRef,
    ZhSummaryBlock,
    build_client_config,
    build_display_names,
    doi_url,
    openalex_url,
)
from radar.domain.demo_export import (
    MANIFEST_FILE,
    PAPERS_DIR,
    PAPERS_INDEX_FILE,
    PREVIEW_DIR,
    SHARD_SIZE,
    TAXONOMY_FILE,
    TITLES_FILE,
    DemoExportFailure,
    DemoExportRun,
    DemoFileEntry,
    DemoManifest,
    DemoPaper,
    DemoPaperBiblio,
    DemoPaperFacts,
    DemoPaperFull,
    DemoPaperShard,
    DemoPapersIndex,
    DemoSummary,
    DemoTaxonomy,
    DemoTitles,
    ForbiddenText,
    check_demo_files,
    data_file_path,
    dump_demo_file,
    index_columns,
    shard_file_name,
)
from radar.domain.domain_config import JournalsConfig
from radar.domain.fingerprints import demo_file_sha256
from radar.domain.paper_page import journal_key_for
from radar.domain.pricing import ModelsConfig
from radar.domain.taxonomy import Taxonomy
from radar.domain.viewer import PublicViewer, SystemViewer
from radar.repo.ai_blocks import find_ai_block
from radar.repo.db import Database, DbConnection
from radar.repo.demo_exports import (
    create_demo_export,
    update_demo_export_finished,
    update_demo_export_running,
)
from radar.repo.members import list_member_emails
from radar.repo.papers import (
    find_paper_llm_source,
    list_demo_full_paper_ids,
    list_demo_paper_facts,
)
from radar.repo.snapshots import find_latest_published_snapshot
from radar.services.ai_blocks import demo_ai_block_settings

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DemoExportSetup:
    """匯出要的設定（入口讀好設定檔組成；services 不讀設定）：模型（判定 AI 區塊「目前這組鍵」）、
    分類表、情境值清單、追蹤的期刊（ISSN-L → 期刊代號、顯示名稱）、自我檢查要找的金絲雀字串
    （測試環境放；正式環境是空的）、`RADAR_DEMO_PUBLISH`（預設關）。"""

    models: ModelsConfig
    taxonomy: Taxonomy
    context_values: ContextValues
    journals: JournalsConfig
    canaries: tuple[str, ...] = ()
    publish: bool = False


@dataclass(frozen=True)
class DemoExportResult:
    """一次成功的匯出：匯出編號、論文篇數、有完整內容的篇數、manifest 的位置。"""

    export_id: str
    paper_count: int
    full_content_count: int
    manifest_path: Path


class DemoExportError(Exception):
    """匯出失敗（`failure` 是記進 `demo_export` 的原因代碼）。"""

    def __init__(self, failure: DemoExportFailure, message: str) -> None:
        self.failure = failure
        super().__init__(message)


class DemoExportCheckError(DemoExportError):
    """寫 manifest 之前的自我檢查沒過；`problems` 是每一條原因（只有檔名、論文 ID、問題種類）。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(DemoExportFailure.SELF_CHECK_FAILED, f"自我檢查沒過（{len(problems)} 項）")


class NoPublishedSnapshotError(Exception):
    """還沒有已發佈的快照，沒有東西可以匯出。"""


def queue_demo_export(actor: SystemViewer, database: Database) -> DemoExportRun:
    """為最新已發佈的快照新增一筆 `queued` 的匯出（一個短交易）。`actor` 是做這件事的身分（系統）。

    Raises:
        NoPublishedSnapshotError: 還沒有已發佈的快照。
        DemoExportBusyError: 這個快照已經有排隊中或執行中的匯出（部分唯一索引）。
    """
    with database.transaction() as connection:
        run = create_demo_export(connection)
    if run is None:
        raise NoPublishedSnapshotError("還沒有已發佈的快照")
    return run


def run_demo_export(
    actor: SystemViewer,
    database: Database,
    setup: DemoExportSetup,
    demo_export_id: uuid.UUID,
    output_root: Path | None,
    *,
    clock: Callable[[], datetime],
) -> DemoExportResult | None:
    """做 `demo_export_id` 那一次匯出，寫到 `output_root/preview/`（見模組說明）。那一筆已經結束或
    不存在時什麼都不做、回 None。

    `actor`（系統身分）只用在自我檢查讀成員 email（不寫進任何檔）；匯出的資料一律用
    `PublicViewer` 讀。

    Raises:
        DemoExportCheckError: 自我檢查沒過（什麼都沒寫，manifest 不換）。
        DemoExportError: 其他已知的失敗（原因代碼見 `failure`）。
        Exception: 讀資料庫或寫檔的錯誤原樣往上丟（那一筆已記成 `failed`）。
    """
    with database.transaction() as connection:
        run = update_demo_export_running(connection, demo_export_id)
    if run is None:
        log.info("這一次示範站匯出已經結束或不存在，沒有再做", demo_export_id=str(demo_export_id))
        return None
    try:
        if setup.publish:
            raise DemoExportError(
                DemoExportFailure.PUBLISH_NOT_READY,
                "RADAR_DEMO_PUBLISH 開著，但正式發佈還沒做；這個 epic 只寫 preview/",
            )
        if output_root is None:
            raise DemoExportError(DemoExportFailure.NO_OUTPUT_DIR, "沒有設定匯出的資料夾")
        files, result = _build_and_check(actor, database, setup, run, clock=clock)
        result = _write(output_root, run.export_id, files, result)
    except Exception as exc:
        failure = exc.failure if isinstance(exc, DemoExportError) else DemoExportFailure.ERROR
        with database.transaction() as connection:
            update_demo_export_finished(connection, run.id, failure=failure)
        log.error(
            "示範站匯出失敗",
            export_id=run.export_id,
            failure=failure.value,
            error_type=type(exc).__name__,
            problems=getattr(exc, "problems", None),
        )
        raise
    with database.transaction() as connection:
        update_demo_export_finished(connection, run.id, failure=None)
    log.info(
        "示範站匯出完成",
        export_id=result.export_id,
        paper_count=result.paper_count,
        full_content_count=result.full_content_count,
    )
    return result


# --- 讀資料、組檔、自我檢查（同一個唯讀 REPEATABLE READ 交易）-----------------------------


def _build_and_check(
    actor: SystemViewer,
    database: Database,
    setup: DemoExportSetup,
    run: DemoExportRun,
    *,
    clock: Callable[[], datetime],
) -> tuple[dict[str, bytes], DemoExportResult]:
    viewer = PublicViewer()
    settings = demo_ai_block_settings(setup.models, setup.taxonomy, setup.context_values)
    export_id = run.export_id
    with database.read_only_snapshot_transaction() as connection:
        snapshot = find_latest_published_snapshot(viewer, connection)
        if snapshot is None or snapshot.id != run.snapshot_id:
            raise DemoExportError(
                DemoExportFailure.SNAPSHOT_CHANGED, "排入之後最新的快照變了；照新的快照再匯出一次"
            )
        ref = SnapshotRef.from_snapshot(snapshot)
        facts = list_demo_paper_facts(viewer, connection)
        papers = [_paper(viewer, connection, setup, settings, item) for item in facts]
        files = _data_files(setup, ref, export_id, papers)
        manifest = DemoManifest(
            snapshot=ref,
            export_id=export_id,
            exported_at=clock(),
            files=tuple(
                DemoFileEntry(path=path, sha256=demo_file_sha256(content), size=len(content))
                for path, content in files.items()
            ),
            summary=_summary(papers),
            client_config=build_client_config(),
        )
        files[MANIFEST_FILE] = dump_demo_file(manifest)
        # 自我檢查另外讀一次 `demo_full_paper`（同一個交易），不拿組檔用的那份結果檢查自己。
        full_ids = list_demo_full_paper_ids(viewer, connection)
        # 成員 email 只拿來比對（系統身分讀；訪客身分看不到成員資料，讀到的會是空的）。
        forbidden = [ForbiddenText("金絲雀字串", canary) for canary in setup.canaries]
        forbidden += [
            ForbiddenText("成員的 email", email) for email in list_member_emails(actor, connection)
        ]
        problems = check_demo_files(files, full_paper_ids=full_ids, forbidden=forbidden)
    if problems:
        raise DemoExportCheckError(problems)
    return files, DemoExportResult(
        export_id=export_id,
        paper_count=len(papers),
        full_content_count=sum(1 for paper in papers if paper.full is not None),
        manifest_path=Path(PREVIEW_DIR) / MANIFEST_FILE,
    )


def _paper(
    viewer: PublicViewer,
    connection: DbConnection,
    setup: DemoExportSetup,
    settings: Mapping[AiProduct, AiBlockSettings],
    facts: DemoPaperFacts,
) -> DemoPaper:
    biblio = PaperBiblio(
        id=facts.paper_id,
        title=facts.title,
        authors=facts.authors,
        publication_year=facts.publication_year,
        journal=journal_key_for(setup.journals, facts.journal_issn_l),
        journal_issn_l=facts.journal_issn_l,
        volume=facts.volume,
        issue=facts.issue,
        first_page=facts.first_page,
        last_page=facts.last_page,
        doi=facts.doi,
        doi_url=None if facts.doi is None else doi_url(facts.doi),
        openalex_id=facts.openalex_id,
        openalex_url=None if facts.openalex_id is None else openalex_url(facts.openalex_id),
        cited_by_count=facts.cited_by_count,
        in_scope=facts.in_scope,
        origin=facts.origin,
        is_retracted=facts.is_retracted,
    )
    classification: ClassificationBlock | None = None
    if facts.labels_allowed:
        classification = ClassificationBlock.from_block(
            _block(viewer, connection, facts, AiProduct.CLASSIFICATION, settings)
        )
    full: DemoPaperFull | None = None
    if facts.in_demo_full_paper:
        source = find_paper_llm_source(viewer, connection, facts.paper_id, for_update=False)
        if source is None or source.abstract is None:
            # 同一個 REPEATABLE READ 交易裡不會發生：view 說有目前的摘要，這裡就讀得到。
            raise DemoExportError(DemoExportFailure.ERROR, "demo_full_paper 的論文讀不到摘要")
        full = DemoPaperFull(
            paper_id=facts.paper_id,
            abstract=PaperAbstract.from_current(source.abstract),
            zh_summary=ZhSummaryBlock.from_block(
                _block(viewer, connection, facts, AiProduct.ZH_SUMMARY, settings)
            ),
            research_model=ResearchModelBlock.from_block(
                _block(viewer, connection, facts, AiProduct.RESEARCH_MODEL, settings)
            ),
        )
    return DemoPaper(
        biblio=DemoPaperBiblio(
            biblio=biblio,
            links=FullTextLinks(doi_url=biblio.doi_url, open_access_url=None),
            classification=classification,
            has_abstract=facts.abstract_source is not None,
            has_full_content=facts.in_demo_full_paper,
            is_new_this_week=facts.is_new_this_week,
        ),
        full=full,
    )


def _block(
    viewer: PublicViewer,
    connection: DbConnection,
    facts: DemoPaperFacts,
    product: AiProduct,
    settings: Mapping[AiProduct, AiBlockSettings],
) -> AiBlock:
    block = find_ai_block(viewer, connection, facts.paper_id, product, settings[product])
    if block is None:
        # 同一個 REPEATABLE READ 交易裡不會發生：上一句才讀到這篇。
        raise DemoExportError(DemoExportFailure.ERROR, "論文在匯出途中變成看不到")
    return block


def _data_files(
    setup: DemoExportSetup, ref: SnapshotRef, export_id: str, papers: list[DemoPaper]
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    biblios = [paper.biblio for paper in papers]
    provenances, columns = index_columns(biblios)
    files[data_file_path(export_id, TAXONOMY_FILE)] = dump_demo_file(
        DemoTaxonomy(
            snapshot=ref,
            export_id=export_id,
            names=build_display_names(setup.journals, setup.taxonomy),
        )
    )
    files[data_file_path(export_id, PAPERS_INDEX_FILE)] = dump_demo_file(
        DemoPapersIndex(snapshot=ref, export_id=export_id, provenances=provenances, **columns)
    )
    files[data_file_path(export_id, TITLES_FILE)] = dump_demo_file(
        DemoTitles(
            snapshot=ref, export_id=export_id, titles=tuple(item.biblio.title for item in biblios)
        )
    )
    for shard, start in enumerate(range(0, len(papers), SHARD_SIZE)):
        name = f"{PAPERS_DIR}/{shard_file_name(shard)}"
        files[data_file_path(export_id, name)] = dump_demo_file(
            DemoPaperShard(
                snapshot=ref,
                export_id=export_id,
                shard=shard,
                papers=tuple(papers[start : start + SHARD_SIZE]),
            )
        )
    return files


def _summary(papers: list[DemoPaper]) -> DemoSummary:
    years = [
        paper.biblio.biblio.publication_year
        for paper in papers
        if paper.biblio.biblio.publication_year is not None
    ]
    return DemoSummary(
        paper_count=len(papers),
        with_abstract_count=sum(1 for paper in papers if paper.biblio.has_abstract),
        new_this_week_count=sum(1 for paper in papers if paper.biblio.is_new_this_week),
        full_content_count=sum(1 for paper in papers if paper.biblio.has_full_content),
        first_year=min(years) if years else None,
        last_year=max(years) if years else None,
    )


# --- 寫檔（自我檢查通過之後）------------------------------------------------------------------


def _write(
    output_root: Path, export_id: str, files: dict[str, bytes], result: DemoExportResult
) -> DemoExportResult:
    preview = output_root / PREVIEW_DIR
    target = preview / export_id
    if target.exists():
        raise DemoExportError(
            DemoExportFailure.OUTPUT_EXISTS, f"{export_id} 的目錄已經存在，不覆寫"
        )
    preview.mkdir(parents=True, exist_ok=True)
    staging = preview / f".{export_id}.tmp-{uuid.uuid4().hex}"
    try:
        for path, content in files.items():
            if path == MANIFEST_FILE:
                continue
            relative = path.removeprefix(f"{export_id}/")
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        # 同一個檔案系統裡改名是原子的；目標已經存在時 rename 失敗（不會蓋掉別的匯出）。
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    manifest = preview / MANIFEST_FILE
    pending = preview / f".{MANIFEST_FILE}.tmp-{uuid.uuid4().hex}"
    try:
        pending.write_bytes(files[MANIFEST_FILE])
        os.replace(pending, manifest)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    return DemoExportResult(
        export_id=result.export_id,
        paper_count=result.paper_count,
        full_content_count=result.full_content_count,
        manifest_path=manifest,
    )
