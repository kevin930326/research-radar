"""示範站匯出（Story 3.7、ADR-0007、ADR-0061、AD-20）：把示範站要的資料匯出成靜態檔。

流程（`run_demo_export`）：

1. 一個短交易接手那一筆 `demo_export`（`update_demo_export_claimed`：`queued`，或卡在 `running` 超過
   `DEMO_EXPORT_RUNNING_STUCK` 的），拿到這一次的 `run_token`；已經結束或正由別的行程在做的不做。
2. 先看 `preview/manifest.json` 是不是已經指向這一次（上一次換好 manifest、卻沒記上成功就中斷）：是
   而且檔案的雜湊都對，就補記成功、結束（不重寫）。
3. **一個唯讀、REPEATABLE READ 的交易**（`Database.read_only_snapshot_transaction`）裡讀完全部的
   資料並做完自我檢查：匯出的資料只用 `PublicViewer` 經 repo 讀（最新已發佈的快照、每一篇
   `counted_paper` 的書目、出版社與分類、`demo_full_paper` 的摘要與兩個 AI 區塊），逐欄組成白名單
   模型、轉成 JSON；manifest 也先組好。出版社或期刊在排除清單裡的論文，就算舊的 `demo_eligible`
   還是 true，也不給完整內容（ADR-0007 第 6 項）。自我檢查另外讀 `demo_full_paper` 名單、出版社
   欄位與成員 email（**成員 email 是唯一用系統身分讀的東西**，只拿來比對、不寫進任何檔，ADR-0061），
   `domain/demo_export.check_demo_files` 檢查全部的檔。沒過就丟 `DemoExportCheckError`，**什麼都
   不寫**。
4. 交易結束後，在一個短交易裡拿發佈鎖（CLI 與 worker 共用）、確認這一筆仍是自己接手的、現在的
   manifest 沒有指向比這次新的匯出，才寫檔：資料檔先寫進暫存目錄（每個檔 fsync）、整個改名成
   `preview/<匯出編號>/`（目錄已經存在就失敗、不覆寫），再 fsync `preview/`；`preview/manifest.json`
   最後寫（暫存檔 fsync 後換名，換的那一下是原子的，再 fsync `preview/`）；同一個交易把那一筆改成
   `succeeded`。
5. 任何一步失敗把那一筆改成 `failed`（記原因代碼）再往上丟；被別的行程接手了就什麼都不改。

`RADAR_DEMO_PUBLISH` 預設關；這個 epic 不開：開了的話不讀資料、直接記成失敗（正式發佈還沒做）。
這個模組只寫 `<輸出資料夾>/preview/`，沒有寫 `data/` 的路徑；`preview` 是符號連結或跑出輸出資料夾時
拒絕。
"""

import errno
import os
import re
import shutil
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog
from pydantic import ValidationError

from radar.domain.ai_blocks import AiBlock, AiBlockSettings, AiProduct
from radar.domain.context_values import ContextValues
from radar.domain.contracts import (
    FullTextLinks,
    PaperAbstract,
    SnapshotRef,
    build_display_names,
    doi_url,
    openalex_url,
)
from radar.domain.demo_export import (
    DEMO_FILE_PATH_PATTERN,
    MANIFEST_FILE,
    PAPERS_DIR,
    PAPERS_INDEX_FILE,
    PREVIEW_DIR,
    SHARD_SIZE,
    TAXONOMY_FILE,
    TITLES_FILE,
    DemoBiblio,
    DemoClassificationBlock,
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
    DemoResearchModelBlock,
    DemoSummary,
    DemoTaxonomy,
    DemoTitles,
    DemoZhSummaryBlock,
    ForbiddenText,
    build_demo_client_config,
    check_demo_files,
    data_file_path,
    dump_demo_file,
    excluded_publisher_paper_ids,
    export_order,
    index_columns,
    shard_file_name,
)
from radar.domain.domain_config import JournalsConfig
from radar.domain.fingerprints import demo_file_sha256
from radar.domain.paper_page import journal_key_for
from radar.domain.policy import PublisherExclusions
from radar.domain.pricing import ModelsConfig
from radar.domain.sweep import DEMO_EXPORT_RUNNING_STUCK
from radar.domain.taxonomy import Taxonomy
from radar.domain.viewer import PublicViewer, SystemViewer
from radar.repo.ai_blocks import find_ai_block
from radar.repo.db import Database, DbConnection
from radar.repo.demo_exports import (
    DEMO_PUBLISH_LOCK_KEY,
    create_demo_export,
    update_demo_export_claimed,
    update_demo_export_finished,
    update_demo_export_if_owned,
)
from radar.repo.members import list_member_emails
from radar.repo.papers import (
    find_paper_llm_source,
    list_demo_full_paper_ids,
    list_demo_paper_facts,
    list_demo_publisher_facts,
)
from radar.repo.snapshots import find_latest_published_snapshot
from radar.services.ai_blocks import demo_ai_block_settings

log = structlog.get_logger(__name__)

_DATA_FILE = re.compile(DEMO_FILE_PATH_PATTERN)


@dataclass(frozen=True)
class DemoExportSetup:
    """匯出要的設定（入口讀好設定檔組成；services 不讀設定）：模型（判定 AI 區塊「目前這組鍵」）、
    分類表、情境值清單、追蹤的期刊（ISSN-L → 期刊代號、顯示名稱）、示範站不放完整內容的出版社與
    期刊（`config/demo_publishers.yaml`）、自我檢查要找的金絲雀字串（測試環境放；正式環境是空的）、
    `RADAR_DEMO_PUBLISH`（預設關）。"""

    models: ModelsConfig
    taxonomy: Taxonomy
    context_values: ContextValues
    journals: JournalsConfig
    exclusions: PublisherExclusions
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
    """寫 manifest 之前的自我檢查沒過；`problems` 是每一條原因（只有檔名、已知欄位名、論文 ID、
    問題種類）。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(DemoExportFailure.SELF_CHECK_FAILED, f"自我檢查沒過（{len(problems)} 項）")


class DemoExportTakenOverError(Exception):
    """這一筆已經被別的行程接手（或已經結束）：這個行程什麼都沒寫，也不改那一筆的狀態。"""


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


def fail_demo_export(
    actor: SystemViewer,
    database: Database,
    demo_export_id: uuid.UUID,
    failure: DemoExportFailure,
) -> bool:
    """還沒開始匯出就失敗（例如 worker 讀設定檔失敗）：照同一個接手規則接手那一筆，直接記成
    `failed`（移出起始狀態，sweep 不再補排）。回傳有沒有記到（已經結束或正由別的行程在做時不改）。
    `actor` 是做這件事的身分（系統）。"""
    with database.transaction() as connection:
        run = update_demo_export_claimed(
            connection, demo_export_id, running_stale_after=DEMO_EXPORT_RUNNING_STUCK
        )
        if run is None or run.run_token is None:
            return False
        return update_demo_export_finished(
            connection, run.id, run_token=run.run_token, failure=failure
        )


def run_demo_export(
    actor: SystemViewer,
    database: Database,
    setup: DemoExportSetup,
    demo_export_id: uuid.UUID,
    output_root: Path | None,
    *,
    clock: Callable[[], datetime],
) -> DemoExportResult | None:
    """做 `demo_export_id` 那一次匯出，寫到 `output_root/preview/`（見模組說明）。那一筆已經結束、
    不存在或正由別的行程在做時什麼都不做、回 None。

    `actor`（系統身分）只用在自我檢查讀成員 email（不寫進任何檔）；匯出的資料一律用
    `PublicViewer` 讀。

    Raises:
        DemoExportCheckError: 自我檢查沒過（什麼都沒寫，manifest 不換）。
        DemoExportError: 其他已知的失敗（原因代碼見 `failure`）。
        DemoExportTakenOverError: 做到一半被別的行程接手（什麼都沒寫，狀態不改）。
        BaseException: 讀資料庫或寫檔的錯誤、中斷原樣往上丟（那一筆已記成 `failed`）。
    """
    with database.transaction() as connection:
        run = update_demo_export_claimed(
            connection, demo_export_id, running_stale_after=DEMO_EXPORT_RUNNING_STUCK
        )
    if run is None or run.run_token is None:
        log.info(
            "這一次示範站匯出已經結束、不存在或正由別的行程在做，沒有再做",
            demo_export_id=str(demo_export_id),
        )
        return None
    token = run.run_token
    preview: Path | None = None
    try:
        if setup.publish:
            raise DemoExportError(
                DemoExportFailure.PUBLISH_NOT_READY,
                "RADAR_DEMO_PUBLISH 開著，但正式發佈還沒做；這個 epic 只寫 preview/",
            )
        if output_root is None:
            raise DemoExportError(DemoExportFailure.NO_OUTPUT_DIR, "沒有設定匯出的資料夾")
        preview = _preview_dir(output_root)
        recovered = _recover(database, preview, run, token)
        if recovered is not None:
            log.info("示範站匯出已經換好 manifest，補記成功", export_id=run.export_id)
            return recovered
        files, result = _build_and_check(actor, database, setup, run, clock=clock)
        result = _publish(database, preview, run, token, files, result)
    except DemoExportTakenOverError:
        log.warning("示範站匯出被別的行程接手，這個行程沒有寫檔", export_id=run.export_id)
        raise
    except BaseException as exc:
        _record_failure(database, preview, run, token, exc)
        raise
    log.info(
        "示範站匯出完成",
        export_id=result.export_id,
        paper_count=result.paper_count,
        full_content_count=result.full_content_count,
    )
    return result


def _record_failure(
    database: Database,
    preview: Path | None,
    run: DemoExportRun,
    token: uuid.UUID,
    exc: BaseException,
) -> None:
    """把那一筆記成 `failed`。manifest 已經換成這一次時不記（換好之後才失敗：留在 `running`，sweep
    補排時 `_recover` 補記成功）；記不上（資料庫斷線等）只記日誌，原本的例外照樣往上丟。"""
    if isinstance(exc, DemoExportError):
        failure = exc.failure
    elif isinstance(exc, Exception):
        failure = DemoExportFailure.ERROR
    else:
        failure = DemoExportFailure.INTERRUPTED
    problems = exc.problems if isinstance(exc, DemoExportCheckError) else None
    if preview is not None and _manifest_export_id(preview) == run.export_id:
        log.error(
            "示範站匯出已經換好 manifest，但沒有記上成功；sweep 補排時會補記",
            export_id=run.export_id,
            error_type=type(exc).__name__,
        )
        return
    updated = False
    try:
        with database.transaction() as connection:
            updated = update_demo_export_finished(
                connection, run.id, run_token=token, failure=failure
            )
    except Exception as record_exc:
        log.warning(
            "示範站匯出的失敗沒有記上",
            export_id=run.export_id,
            error_type=type(record_exc).__name__,
        )
    else:
        if not updated:
            log.warning("示範站匯出的狀態沒有更新：已經被別的行程接手", export_id=run.export_id)
    log.error(
        "示範站匯出失敗",
        export_id=run.export_id,
        failure=failure.value,
        error_type=type(exc).__name__,
        problems=problems,
    )


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
        stale = sum(
            1
            for item, paper in zip(facts, papers, strict=True)
            if item.in_demo_full_paper and paper.full is None
        )
        if stale:
            # 舊的 demo_eligible 還是 true、但出版社或期刊在排除清單裡（清單改了還沒 reparse）。
            log.warning("示範站可用的舊判定和排除清單不一致，這幾篇不給完整內容", count=stale)
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
            client_config=build_demo_client_config(),
        )
        files[MANIFEST_FILE] = dump_demo_file(manifest)
        # 自我檢查另外讀（同一個交易），不拿組檔用的那份結果檢查自己。
        full_ids = list_demo_full_paper_ids(viewer, connection)
        excluded = excluded_publisher_paper_ids(
            list_demo_publisher_facts(viewer, connection), setup.exclusions
        )
        forbidden = [ForbiddenText("金絲雀字串", canary) for canary in setup.canaries]
        forbidden += _member_email_checks(actor, connection)
        problems = check_demo_files(
            files, full_paper_ids=full_ids, forbidden=forbidden, excluded_paper_ids=excluded
        )
    if problems:
        raise DemoExportCheckError(problems)
    return files, DemoExportResult(
        export_id=export_id,
        paper_count=len(papers),
        full_content_count=sum(1 for paper in papers if paper.full is not None),
        manifest_path=Path(PREVIEW_DIR) / MANIFEST_FILE,
    )


def _member_email_checks(actor: SystemViewer, connection: DbConnection) -> list[ForbiddenText]:
    """自我檢查要找的成員 email：AD-20「只用 PublicViewer 讀」唯一的例外（ADR-0061）。訪客身分看不到
    成員資料，用訪客身分讀會是空的、檢查等於沒做，所以用系統身分讀；只拿來比對，不寫進任何檔或日誌，
    組檔的函式拿不到這個身分。"""
    return [ForbiddenText("成員的 email", email) for email in list_member_emails(actor, connection)]


def _paper(
    viewer: PublicViewer,
    connection: DbConnection,
    setup: DemoExportSetup,
    settings: Mapping[AiProduct, AiBlockSettings],
    facts: DemoPaperFacts,
) -> DemoPaper:
    biblio = DemoBiblio(
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
    )
    classification: DemoClassificationBlock | None = None
    if facts.labels_allowed:
        classification = DemoClassificationBlock.from_block(
            _block(viewer, connection, facts, AiProduct.CLASSIFICATION, settings)
        )
    # 完整內容：在 view `demo_full_paper` 裡，而且出版社與期刊這時直接對排除清單也沒有對上（不只信
    # 資料庫存的 `demo_eligible`，ADR-0007 第 6 項、ADR-0052）。
    excluded = setup.exclusions.matches(
        facts.publisher_ids, facts.publisher_names, journal_issn_l=facts.journal_issn_l
    )
    full: DemoPaperFull | None = None
    if facts.in_demo_full_paper and not excluded:
        source = find_paper_llm_source(viewer, connection, facts.paper_id, for_update=False)
        if source is None or source.abstract is None:
            # 同一個 REPEATABLE READ 交易裡不會發生：view 說有目前的摘要，這裡就讀得到。
            raise DemoExportError(DemoExportFailure.ERROR, "demo_full_paper 的論文讀不到摘要")
        full = DemoPaperFull(
            paper_id=facts.paper_id,
            abstract=PaperAbstract.from_current(source.abstract),
            zh_summary=DemoZhSummaryBlock.from_block(
                _block(viewer, connection, facts, AiProduct.ZH_SUMMARY, settings)
            ),
            research_model=DemoResearchModelBlock.from_block(
                _block(viewer, connection, facts, AiProduct.RESEARCH_MODEL, settings)
            ),
        )
    return DemoPaper(
        biblio=DemoPaperBiblio(
            biblio=biblio,
            links=FullTextLinks(doi_url=biblio.doi_url, open_access_url=None),
            classification=classification,
            has_abstract=facts.abstract_source is not None,
            has_full_content=full is not None,
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


# --- 輸出位置、補記成功、寫檔（自我檢查通過之後）---------------------------------------------


def _unsafe(message: str) -> DemoExportError:
    return DemoExportError(DemoExportFailure.OUTPUT_UNSAFE, message)


def _preview_dir(output_root: Path) -> Path:
    """`output_root/preview/`（沒有就建）。`preview` 是符號連結、或實際位置不在輸出資料夾底下時
    拒絕：`RADAR_DEMO_PUBLISH` 關著時不能沿著連結寫到別的地方（例如正式的 `data/`）。"""
    output_root.mkdir(parents=True, exist_ok=True)
    root = output_root.resolve(strict=True)
    preview = output_root / PREVIEW_DIR
    if preview.is_symlink():
        raise _unsafe("preview 是符號連結，不寫")
    preview.mkdir(exist_ok=True)
    if preview.is_symlink() or preview.resolve(strict=True) != root / PREVIEW_DIR:
        raise _unsafe("preview 不在輸出資料夾底下，不寫")
    return preview


def _read_manifest(preview: Path) -> DemoManifest | None:
    """現在的 `preview/manifest.json`；沒有、或讀不懂（不符合模型）時回 None。

    Raises:
        DemoExportError: manifest 是符號連結（`output_unsafe`）。
    """
    path = preview / MANIFEST_FILE
    if path.is_symlink():
        raise _unsafe("manifest.json 是符號連結，不寫")
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return DemoManifest.model_validate_json(content)
    except ValidationError:
        log.warning("現在的 manifest.json 讀不懂，當作沒有")
        return None


def _manifest_export_id(preview: Path) -> str | None:
    """現在的 manifest 指向哪一次匯出；沒有、讀不懂或讀的時候出錯時回 None（只給失敗處理
    判斷用）。"""
    try:
        manifest = _read_manifest(preview)
    except Exception:
        return None
    return None if manifest is None else manifest.export_id


def _files_intact(preview: Path, manifest: DemoManifest) -> bool:
    """manifest 列的每個檔都在、不是符號連結、雜湊與大小都對。"""
    for entry in manifest.files:
        path = preview / entry.path
        if path.is_symlink() or not path.is_file():
            return False
        content = path.read_bytes()
        if len(content) != entry.size or demo_file_sha256(content) != entry.sha256:
            return False
    return True


def _recover(
    database: Database, preview: Path, run: DemoExportRun, token: uuid.UUID
) -> DemoExportResult | None:
    """manifest 已經指向這一次、檔案都完整（上一次換好 manifest、卻沒記上成功就中斷）：補記成功、回
    這一次的結果；不是就回 None（照常匯出）。在發佈鎖裡看，免得和正在寫的行程互相看到一半。

    Raises:
        DemoExportTakenOverError: 這一筆已經被別的行程接手。
    """
    with database.transaction() as connection:
        Database.transaction_lock(connection, DEMO_PUBLISH_LOCK_KEY)
        if not update_demo_export_if_owned(connection, run.id, token):
            raise DemoExportTakenOverError(run.export_id)
        manifest = _read_manifest(preview)
        if manifest is None or manifest.export_id != run.export_id:
            return None
        if not _files_intact(preview, manifest):
            return None
        update_demo_export_finished(connection, run.id, run_token=token, failure=None)
    return DemoExportResult(
        export_id=run.export_id,
        paper_count=manifest.summary.paper_count,
        full_content_count=manifest.summary.full_content_count,
        manifest_path=preview / MANIFEST_FILE,
    )


def _publish(
    database: Database,
    preview: Path,
    run: DemoExportRun,
    token: uuid.UUID,
    files: dict[str, bytes],
    result: DemoExportResult,
) -> DemoExportResult:
    """拿發佈鎖、確認還是自己接手的、manifest 沒有指向比這次新的匯出，才寫檔換 manifest，並在同一個
    交易記成功（換好 manifest 後交易沒 commit 成：那一筆留在 `running`，之後由 `_recover` 補記）。

    Raises:
        DemoExportTakenOverError: 這一筆已經被別的行程接手（什麼都沒寫）。
        DemoExportError: manifest 已經指向比這次新的匯出（`superseded`）、目錄已經存在、輸出位置
            不安全。
    """
    with database.transaction() as connection:
        Database.transaction_lock(connection, DEMO_PUBLISH_LOCK_KEY)
        if not update_demo_export_if_owned(connection, run.id, token):
            raise DemoExportTakenOverError(run.export_id)
        current = _read_manifest(preview)
        if current is not None and export_order(current.export_id) > export_order(run.export_id):
            raise DemoExportError(
                DemoExportFailure.SUPERSEDED,
                f"manifest 已經指向比 {run.export_id} 新的匯出，不蓋回舊的",
            )
        manifest_path = _write(preview, run.export_id, files)
        update_demo_export_finished(connection, run.id, run_token=token, failure=None)
    return DemoExportResult(
        export_id=result.export_id,
        paper_count=result.paper_count,
        full_content_count=result.full_content_count,
        manifest_path=manifest_path,
    )


def _write_file(path: Path, content: bytes) -> None:
    """寫一個新檔（已經存在就失敗）並 fsync，確定內容先落到磁碟才換名。"""
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    """fsync 一個目錄（讓裡面的新增、換名落到磁碟）。"""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_dir(source: Path, target: Path) -> None:
    """暫存目錄改名成匯出目錄。同一個檔案系統裡改名是原子的；目標已經存在、而且不是空目錄時
    失敗（目標是空目錄時 POSIX 會直接取代它，所以呼叫前先確認目標不存在）。"""
    os.rename(source, target)


def _replace_file(source: Path, target: Path) -> None:
    """暫存的 manifest 換成正式的（原子的換名；目標是符號連結時換掉的是連結本身，不會跟過去）。"""
    os.replace(source, target)


def _write(preview: Path, export_id: str, files: dict[str, bytes]) -> Path:
    """資料檔寫進暫存目錄、改名成 `preview/<匯出編號>/`，最後換 `preview/manifest.json`；回 manifest
    的位置。每一步都先 fsync 再往下（主機突然斷電時，不會有指向缺檔的新 manifest）。

    Raises:
        DemoExportError: 目錄已經存在（`output_exists`）、路徑不是認得的資料檔（`output_unsafe`）。
    """
    target = preview / export_id
    if os.path.lexists(target):
        raise DemoExportError(
            DemoExportFailure.OUTPUT_EXISTS, f"{export_id} 的目錄已經存在，不覆寫"
        )
    manifest = preview / MANIFEST_FILE
    if manifest.is_symlink():
        raise _unsafe("manifest.json 是符號連結，不寫")
    data_files = {path: content for path, content in files.items() if path != MANIFEST_FILE}
    for path in data_files:
        if not _DATA_FILE.fullmatch(path) or not path.startswith(f"{export_id}/"):
            raise _unsafe("資料檔的路徑不是認得的資料檔，不寫")
    staging = preview / f".{export_id}.tmp-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        directories = {staging}
        for path, content in data_files.items():
            destination = staging / path.removeprefix(f"{export_id}/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            directories.add(destination.parent)
            _write_file(destination, content)
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            _fsync_dir(directory)
        try:
            _rename_dir(staging, target)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                raise DemoExportError(
                    DemoExportFailure.OUTPUT_EXISTS, f"{export_id} 的目錄已經存在，不覆寫"
                ) from None
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _fsync_dir(preview)
    pending = preview / f".{MANIFEST_FILE}.tmp-{uuid.uuid4().hex}"
    try:
        _write_file(pending, files[MANIFEST_FILE])
        _replace_file(pending, manifest)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    _fsync_dir(preview)
    return manifest
