"""示範站匯出（Story 3.7、ADR-0007、ADR-0061、AD-20）：把示範站要的資料匯出成靜態檔。

流程（`run_demo_export`）：

1. 一個短交易接手那一筆 `demo_export`（`update_demo_export_claimed`：`queued`，或卡在 `running` 超過
   `DEMO_EXPORT_RUNNING_STUCK` 的），拿到這一次的 `run_token`；已經結束或正由別的行程在做的不做。
2. 先看 `preview/manifest.json` 是不是已經指向這一次（上一次換好 manifest、卻沒記上成功就中斷）：是
   而且檔案的雜湊都對，就再 fsync 一次 `preview/`、補記成功、結束（不重寫）。已經指向比這次新的匯出
   時直接記成 `superseded`（不讀資料、不寫檔；這一次就算之前發佈過，也已經被新的一批取代）。現有的
   manifest 讀不懂（格式不合、別的版本寫的）時拒絕（`manifest_unreadable`），不當成沒有。
3. **一個唯讀、REPEATABLE READ 的交易**（`Database.read_only_snapshot_transaction`）裡讀完全部的
   資料並做完自我檢查：匯出的資料只用 `PublicViewer` 經 repo 讀（最新已發佈的快照、每一篇
   `counted_paper` 的書目、出版社與分類、`demo_full_paper` 的摘要與兩個 AI 區塊），逐欄組成白名單
   模型、轉成 JSON；manifest 也先組好。出版社或期刊在排除清單裡的論文，就算舊的 `demo_eligible`
   還是 true，也不給完整內容（ADR-0007 第 6 項）。自我檢查另外讀 `demo_full_paper` 名單、出版社
   欄位與成員 email（**成員 email 是唯一用系統身分讀的東西**，只拿來比對、不寫進任何檔，ADR-0061），
   `domain/demo_export.check_demo_files` 檢查全部的檔。沒過就丟 `DemoExportCheckError`，**什麼都
   不寫**。
4. 交易結束後，在一個短交易裡拿資料庫的發佈鎖（CLI 與 worker 共用）、確認這一筆仍是自己接手的，再拿
   `preview/` 這個資料夾本身的**檔案鎖**（`fcntl.flock` 鎖打開的目錄 fd，不另外建鎖檔；資料庫斷線
   也不會放掉；等太久記成 `output_busy`）。拿到檔案鎖之後、寫目錄前與換 manifest 前，各再確認一次
   接手代號（資料庫斷線時這一句會失敗，什麼都不寫）與 `preview/` 還在原位（打開的目錄 fd 和
   `<輸出資料夾>/preview` 是同一個資料夾）。現在的 manifest 沒有指向比這次
   新的匯出，才寫檔：資料檔先寫進暫存目錄（每個檔 fsync）、整個改名成 `preview/<匯出編號>/`（目錄
   已經存在就失敗、不覆寫），再 fsync `preview/`；換 manifest 前在檔案鎖裡再比一次新舊；
   `preview/manifest.json` 最後寫（暫存檔 fsync 後換名，換的那一下是原子的，再 fsync `preview/`）；
   同一個資料庫交易把那一筆改成 `succeeded`。**manifest 換名一成功就算已發佈**：之後的步驟（位置
   檢查、fsync、記成功、commit）失敗時丟 `DemoExportPublishedUnrecordedError`（已發佈、狀態待
   補記），中斷原樣往上丟，那一筆都留在 `running`、不記失敗。寫好匯出目錄之後、換 manifest 之前
   停下（被接手、檢查失敗、中斷）時，刪掉這次寫好的匯出目錄（manifest 沒指向它，沒有讀者讀得到）。
5. 其他任何一步失敗把那一筆改成 `failed`（記原因代碼）再往上丟；被別的行程接手了就不改狀態。

輸出位置的讀寫一律相對於打開的目錄 fd（`O_DIRECTORY`、`O_NOFOLLOW`、`dir_fd`）：檢查之後有人把
`preview` 換成符號連結，也寫不到別處。
`RADAR_DEMO_PUBLISH` 預設關；這個 epic 不開：開了的話不讀資料、直接記成失敗（正式發佈還沒做）。
這個模組只寫 `<輸出資料夾>/preview/`（輸出資料夾本身沒有就建），沒有寫 `data/` 的路徑；`preview`
是符號連結時拒絕。
"""

import contextlib
import errno
import fcntl
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

import structlog
from pydantic import ValidationError

from radar.domain.ai_blocks import AiBlock, AiBlockSettings, AiProduct
from radar.domain.context_values import ContextValues
from radar.domain.contracts import (
    FullTextLinks,
    PaperAbstract,
    SnapshotRef,
    TrendParams,
    build_display_names,
    doi_url,
    openalex_url,
)
from radar.domain.cube import build_cube_rows
from radar.domain.demo_export import (
    CUBE_FILE,
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
    DemoCube,
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
    demo_eligible_for,
    demo_eligible_paper_ids,
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
from radar.repo.stats import list_cube_counts
from radar.services.ai_blocks import demo_ai_block_settings

log = structlog.get_logger(__name__)

_DATA_FILE = re.compile(DEMO_FILE_PATH_PATTERN)


@dataclass(frozen=True)
class DemoExportSetup:
    """匯出要的設定（入口讀好設定檔組成；services 不讀設定）：模型（判定 AI 區塊「目前這組鍵」）、
    分類表、情境值清單、追蹤的期刊（ISSN-L → 期刊代號、顯示名稱）、示範站不放完整內容的出版社與
    期刊（`config/demo_publishers.yaml`）、自我檢查要找的金絲雀字串（測試環境放；正式環境是空的）、
    `RADAR_DEMO_PUBLISH`（預設關）。`params` 是 `cube.json` 的規則設定值
    （`contracts.build_trend_params` 組好，Story 3.15）。"""

    models: ModelsConfig
    taxonomy: Taxonomy
    context_values: ContextValues
    journals: JournalsConfig
    exclusions: PublisherExclusions
    params: TrendParams
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


class DemoExportPublishedUnrecordedError(Exception):
    """manifest 已經換成這一次（讀者已經看得到新的一批，**已發佈**），但之後的步驟
    （fsync `preview/`、記成功、commit）失敗：那一筆留在 `running`，之後接手（sweep 補排）時
    補記成功、不重寫。`error_type` 是原本的錯誤類別（不串原例外：訊息可能帶主機或帳號）。"""

    def __init__(self, export_id: str, error_type: str) -> None:
        self.export_id = export_id
        self.error_type = error_type
        super().__init__(f"{export_id} 已發佈、狀態待補記（{error_type}）")


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
        DemoExportTakenOverError: 做到一半被別的行程接手（狀態不改、manifest 不換；已經寫好的匯出
            目錄刪掉，刪不掉時日誌另外寫）。
        DemoExportPublishedUnrecordedError: manifest 已經換成這一次，但狀態沒記上（留在 `running`，
            之後接手時補記成功）。換好 manifest 之後的中斷（`KeyboardInterrupt` 等）原樣往上丟，
            一樣不記失敗。
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
    out: _Output | None = None
    progress = _Progress()
    with ExitStack() as stack:
        try:
            if setup.publish:
                raise DemoExportError(
                    DemoExportFailure.PUBLISH_NOT_READY,
                    "RADAR_DEMO_PUBLISH 開著，但正式發佈還沒做；這個 epic 只寫 preview/",
                )
            if output_root is None:
                raise DemoExportError(DemoExportFailure.NO_OUTPUT_DIR, "沒有設定匯出的資料夾")
            out = stack.enter_context(_open_output(output_root))
            recovered = _recover(database, out, run, token)
            if recovered is not None:
                log.info("示範站匯出已經換好 manifest，補記成功", export_id=run.export_id)
                return recovered
            files, result = _build_and_check(actor, database, setup, run, clock=clock)
            result = _publish(database, out, run, token, files, result, progress)
        except DemoExportTakenOverError:
            log.warning(
                "示範站匯出被別的行程接手：manifest 沒換、狀態不改；"
                + _TAKEN_OVER_DIR[progress.dir],
                export_id=run.export_id,
            )
            raise
        except BaseException as exc:
            _record_failure(database, out, run, token, exc, published=progress.switched)
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
    out: _Output | None,
    run: DemoExportRun,
    token: uuid.UUID,
    exc: BaseException,
    *,
    published: bool = False,
) -> None:
    """把那一筆記成 `failed`。`published`（這個行程已經換好 manifest）時一律不記：manifest 一換好
    就算已發佈，之後的檢查失敗或中斷都留在 `running`，sweep 補排時 `_recover` 補記（ADR-0061）。
    沒換過但 manifest 已經指向這一次、檔案都完整時也不記（被接手前的同一筆換好的）；manifest 指向
    這一次、檔案卻不完整時照樣記失敗（補記不了，不要每 6 小時重排一次）。記不上（資料庫斷線等）
    只記日誌，原本的例外照樣往上丟。"""
    if isinstance(exc, DemoExportError):
        failure = exc.failure
    elif isinstance(exc, Exception):
        failure = DemoExportFailure.ERROR
    else:
        failure = DemoExportFailure.INTERRUPTED
    problems = exc.problems if isinstance(exc, DemoExportCheckError) else None
    if (
        published
        or isinstance(exc, DemoExportPublishedUnrecordedError)
        or (out is not None and _published_here(out, run))
    ):
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
        # 計數方塊（Story 3.15）：同一個交易、同一個訪客身分經 repo 數（AD-5、AD-19），和成員版的
        # 計數方塊同一個查詢；自我檢查再用 papers-index.json 重算每一格。
        cube = DemoCube(
            snapshot=ref,
            export_id=export_id,
            params=setup.params,
            rows=build_cube_rows(list_cube_counts(viewer, connection), setup.journals),
        )
        files[data_file_path(export_id, CUBE_FILE)] = dump_demo_file(cube)
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
        publisher_facts = list_demo_publisher_facts(viewer, connection)
        excluded = excluded_publisher_paper_ids(publisher_facts, setup.exclusions)
        eligible_ids = demo_eligible_paper_ids(publisher_facts, setup.exclusions)
        forbidden = [ForbiddenText("金絲雀字串", canary) for canary in setup.canaries]
        forbidden += _member_email_checks(actor, connection)
        problems = check_demo_files(
            files,
            full_paper_ids=full_ids,
            forbidden=forbidden,
            eligible_paper_ids=eligible_ids,
            excluded_paper_ids=excluded,
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
    # 示範站可用：資料庫存的 `demo_eligible`（view `demo_full_paper` 用的同一欄），而且出版社與期刊
    # 這時直接對排除清單也沒有對上（不只信存的值，ADR-0007 第 6 項、ADR-0052）。完整內容只給在
    # view 裡、而且這一次判定也可以的論文，所以有完整內容的一定 demo_eligible。
    eligible = demo_eligible_for(
        facts.demo_eligible,
        facts.publisher_ids,
        facts.publisher_names,
        facts.journal_issn_l,
        setup.exclusions,
    )
    full: DemoPaperFull | None = None
    if facts.in_demo_full_paper and eligible:
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
            demo_eligible=eligible,
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


# --- 輸出位置、檔案鎖、補記成功、寫檔（自我檢查通過之後）---------------------------------------

# 發佈用的檔案鎖：鎖 `preview/` 這個資料夾本身（`fcntl.flock` 鎖打開的目錄 fd，不另外建
# 鎖檔）。寫匯出目錄到換 manifest 這一段，同一台主機、同一個資料夾的 CLI 與 worker 都拿這一
# 把。資料庫的發佈鎖在連線斷掉時會被放掉，這把只有放鎖或行程結束才放。範例產生器鎖的是
# `web/e2e/fixtures/`，是另一把。
# 等超過這個秒數就放棄（`output_busy`）：拿著的一方只在寫目錄到換 manifest 那幾秒拿著，等
# 這麼久代表它卡住了；等的期間拿著資料庫交易與發佈鎖，不要無限等。
DEMO_OUTPUT_LOCK_TIMEOUT_SECONDS: Final = 300.0
_DEMO_OUTPUT_LOCK_POLL_SECONDS: Final = 0.05

_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


_TAKEN_OVER_DIR: Final = {
    "none": "這次還沒寫匯出目錄",
    "removed": "這次寫好的匯出目錄已刪掉",
    "kept": "這次寫好的匯出目錄沒有刪掉（見前一筆警告）",
}


@dataclass
class _Progress:
    """這一次匯出做到哪。`run_demo_export` 建，失敗處理看它（中斷也一樣看得到）：

    - `switched`：manifest 已經換成這一次（已發佈）。`_swap_manifest` 換名成功時設；換名之後、設好
      之前就停下時，由 `_publish` 的清理重讀 manifest 補設。
    - `replacing`：已經開始換 manifest 的名（`_swap_manifest` 換名前設）；清理只在這之後才需要重讀
      manifest 看換了沒有。
    - `written`：這次寫的匯出目錄的 (`st_dev`, `st_ino`)（取自改名前的暫存目錄，改名不變）。改名前
      就設、改名確定失敗才清掉；清理只刪身分相同的目錄。
    - `dir`：被接手時日誌怎麼寫匯出目錄（`none` 沒寫過、`removed` 刪掉了、`kept` 沒刪掉）。
    """

    switched: bool = False
    replacing: bool = False
    written: tuple[int, int] | None = None
    dir: Literal["none", "removed", "kept"] = "none"


@dataclass(frozen=True)
class _Output:
    """打開的輸出位置：輸出資料夾與 `preview/` 的目錄 fd。輸出資料夾本身用一般路徑打開（它和上層
    可以是符號連結）；`preview/` 起往下的讀寫一律相對於目錄 fd、不跟符號連結，途中有人把 `preview`
    換成符號連結也寫不到別處。`preview_path` 只用來回報位置。"""

    root_fd: int
    preview_fd: int
    preview_path: Path


def _unsafe(message: str) -> DemoExportError:
    return DemoExportError(DemoExportFailure.OUTPUT_UNSAFE, message)


def _open_dir(name: str, directory_fd: int) -> int:
    """相對於 `directory_fd` 打開一個子目錄（不跟符號連結）。"""
    return os.open(name, _DIR_FLAGS, dir_fd=directory_fd)


@contextmanager
def _open_output(output_root: Path) -> Iterator[_Output]:
    """打開 `output_root` 與它底下的 `preview/`（沒有就建）。`preview` 是符號連結或不是資料夾時
    拒絕：`RADAR_DEMO_PUBLISH` 關著時不能沿著連結寫到別的地方（例如正式的 `data/`）。

    Raises:
        DemoExportError: `preview` 不安全（`output_unsafe`）。
    """
    output_root.mkdir(parents=True, exist_ok=True)
    root_fd = os.open(output_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(PREVIEW_DIR, 0o755, dir_fd=root_fd)
        try:
            preview_fd = _open_dir(PREVIEW_DIR, root_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise _unsafe("preview 是符號連結或不是資料夾，不寫") from None
            raise
        try:
            yield _Output(root_fd, preview_fd, output_root / PREVIEW_DIR)
        finally:
            os.close(preview_fd)
    finally:
        os.close(root_fd)


def _try_lock(directory_fd: int) -> bool:
    """試著拿 `directory_fd` 這個資料夾的排他檔案鎖（不等）；拿到回 True，別人拿著回 False。"""
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextmanager
def _locked(directory_fd: int) -> Iterator[None]:
    """拿 `directory_fd` 這個資料夾本身的發佈檔案鎖（排他；別人拿著就每 0.05 秒再試，最多等
    `DEMO_OUTPUT_LOCK_TIMEOUT_SECONDS`）；離開時放掉。不建任何檔。`flock` 以「打開的檔案」為單位，
    所以要另外打開一個 fd 來鎖（同一個行程的兩次匯出也會互斥）。

    Raises:
        DemoExportError: 等超過時限（`output_busy`）。
    """
    descriptor = os.open(".", _DIR_FLAGS, dir_fd=directory_fd)
    try:
        deadline = time.monotonic() + DEMO_OUTPUT_LOCK_TIMEOUT_SECONDS
        while not _try_lock(descriptor):
            if time.monotonic() >= deadline:
                raise DemoExportError(
                    DemoExportFailure.OUTPUT_BUSY,
                    f"等發佈的檔案鎖超過 {DEMO_OUTPUT_LOCK_TIMEOUT_SECONDS:.0f} 秒，沒有寫",
                )
            time.sleep(_DEMO_OUTPUT_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def demo_output_lock(directory: Path) -> Iterator[None]:
    """拿 `directory` 這個資料夾本身的檔案鎖（機制和匯出鎖 `preview/` 一樣；鎖的資料夾不同就是不同的
    鎖）。範例產生器替換範例資料夾時鎖 `web/e2e/fixtures/`（`tests/demo_preview.py`）；測試鎖輸出的
    `preview/` 來模擬別的行程拿著。"""
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        with _locked(directory_fd):
            yield
    finally:
        os.close(directory_fd)


def _read_at(directory_fd: int, path: str) -> bytes | None:
    """相對於 `directory_fd` 讀一個檔（`a/b.json`），每一層都不跟符號連結；不存在回 None。

    Raises:
        OSError: 某一層是符號連結（`ELOOP`）、不是一般檔案（`EINVAL`）或其他讀檔錯誤。
    """
    *parents, name = path.split("/")
    opened: list[int] = []
    try:
        current = directory_fd
        for part in parents:
            current = _open_dir(part, current)
            opened.append(current)
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=current
        )
    except FileNotFoundError:
        return None
    finally:
        for item in opened:
            os.close(item)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(errno.EINVAL, "不是一般檔案")
        return handle.read()


def _read_manifest(out: _Output) -> DemoManifest | None:
    """現在的 `preview/manifest.json`；沒有時回 None。

    Raises:
        DemoExportError: manifest 是符號連結或不是一般檔案（`output_unsafe`）；讀不懂（JSON 壞掉、
            不符合模型、別的版本寫的；`manifest_unreadable`）時拒絕，不當成沒有：不知道它指向的是
            不是比這次新的匯出，不能蓋。
    """
    try:
        content = _read_at(out.preview_fd, MANIFEST_FILE)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EINVAL):
            raise _unsafe("manifest.json 是符號連結或不是一般檔案，不寫") from None
        raise
    if content is None:
        return None
    try:
        return DemoManifest.model_validate_json(content)
    except ValidationError:
        raise DemoExportError(
            DemoExportFailure.MANIFEST_UNREADABLE,
            "現在的 manifest.json 讀不懂（格式不合或是別的版本寫的），不知道新舊、不蓋；"
            "請人工確認後移走再匯出",
        ) from None


def _files_intact(out: _Output, manifest: DemoManifest) -> bool:
    """manifest 列的每個檔都在、不是符號連結、雜湊與大小都對（列的檔齊不齊由 `DemoManifest`
    檢查）。"""
    for entry in manifest.files:
        try:
            content = _read_at(out.preview_fd, entry.path)
        except OSError:
            return False
        if content is None or len(content) != entry.size:
            return False
        if demo_file_sha256(content) != entry.sha256:
            return False
    return True


def _is_complete_here(out: _Output, manifest: DemoManifest | None, run: DemoExportRun) -> bool:
    """manifest 指向這一次，而且列的檔都完整（這一次已經發佈好了）。"""
    return (
        manifest is not None
        and manifest.export_id == run.export_id
        and _files_intact(out, manifest)
    )


def _published_here(out: _Output, run: DemoExportRun) -> bool:
    """失敗處理用：`preview/` 還在原位，而且 manifest 已經指向這一次、檔案都完整；讀的時候
    出錯算不是。"""
    try:
        _check_in_place(out)
        return _is_complete_here(out, _read_manifest(out), run)
    except Exception:
        return False


def _check_in_place(out: _Output) -> None:
    """打開的 `preview/` 目錄 fd 還是 `<輸出資料夾>/preview` 那個資料夾（`st_dev`／`st_ino` 一樣）：
    途中被搬走或換掉時，寫進去的東西讀者看不到，不能回報成功。

    Raises:
        DemoExportError: 不是同一個資料夾或已經不在（`output_unsafe`）。
    """
    opened = os.fstat(out.preview_fd)
    try:
        current = os.stat(out.preview_path, follow_symlinks=False)
    except FileNotFoundError:
        raise _unsafe("preview 在匯出途中被搬走了，不寫") from None
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise _unsafe("preview 在匯出途中被搬走或換掉了，不寫")


def _hold_publish(connection: DbConnection, run: DemoExportRun, token: uuid.UUID) -> None:
    """拿資料庫的發佈鎖（CLI 與 worker 共用）、確認這一筆還是自己接手的。

    Raises:
        DemoExportTakenOverError: 已經被別的行程接手或已經結束。
    """
    Database.transaction_lock(connection, DEMO_PUBLISH_LOCK_KEY)
    _check_owned(connection, run, token)


def _check_owned(connection: DbConnection, run: DemoExportRun, token: uuid.UUID) -> None:
    """這一筆還是自己接手的（同時鎖住那一列到交易結束）。拿到檔案鎖之後、每次寫檔前各確認一次：等
    檔案鎖的期間資料庫連線可能斷掉、被別的行程接手；連線斷了時這一句會丟錯，一樣什麼都不寫。

    Raises:
        DemoExportTakenOverError: 已經被別的行程接手或已經結束。
    """
    if not update_demo_export_if_owned(connection, run.id, token):
        raise DemoExportTakenOverError(run.export_id)


def _record_recovered(
    connection: DbConnection,
    out: _Output,
    run: DemoExportRun,
    token: uuid.UUID,
    manifest: DemoManifest,
) -> DemoExportResult:
    """manifest 已經指向這一次、檔案都完整：先再 fsync 一次 `preview/`（上一次可能換好 manifest、
    還沒 fsync 就中斷），才補記成功。`preview/` 不在原位時不補記。"""
    _check_in_place(out)
    _fsync_dir(out.preview_fd)
    update_demo_export_finished(connection, run.id, run_token=token, failure=None)
    return DemoExportResult(
        export_id=run.export_id,
        paper_count=manifest.summary.paper_count,
        full_content_count=manifest.summary.full_content_count,
        manifest_path=out.preview_path / MANIFEST_FILE,
    )


def _recover(
    database: Database, out: _Output, run: DemoExportRun, token: uuid.UUID
) -> DemoExportResult | None:
    """manifest 已經指向這一次、檔案都完整（上一次換好 manifest、卻沒記上成功就中斷）：補記成功、回
    這一次的結果；不是就回 None（照常匯出）。拿著兩把發佈鎖看，免得和正在寫的行程互相看到一半。

    manifest 已經指向比這一次新的匯出時直接丟 `superseded`、不讀資料：這一次之前就算換好過
    manifest（已發佈、沒記上成功），讀者現在看到的也是新的一批；沒有這一次的 manifest，檔案也
    無從核對，所以不記成功（ADR-0061）。

    Raises:
        DemoExportTakenOverError: 這一筆已經被別的行程接手。
        DemoExportError: manifest 讀不懂或不安全（`manifest_unreadable`、`output_unsafe`）、已經指向
            比這次新的匯出（`superseded`）、等檔案鎖太久（`output_busy`）。
    """
    with database.transaction() as connection:
        _hold_publish(connection, run, token)
        with _locked(out.preview_fd):
            _check_owned(connection, run, token)
            current = _read_manifest(out)
            _check_not_superseded(current, run)
            if current is None or not _is_complete_here(out, current, run):
                return None
            return _record_recovered(connection, out, run, token, current)


def _check_not_superseded(current: DemoManifest | None, run: DemoExportRun) -> None:
    if current is not None and export_order(current.export_id) > export_order(run.export_id):
        raise DemoExportError(
            DemoExportFailure.SUPERSEDED,
            f"manifest 已經指向比 {run.export_id} 新的匯出，不蓋回舊的",
        )


def _publish(
    database: Database,
    out: _Output,
    run: DemoExportRun,
    token: uuid.UUID,
    files: dict[str, bytes],
    result: DemoExportResult,
    progress: _Progress,
) -> DemoExportResult:
    """拿資料庫的發佈鎖、確認還是自己接手的，再拿檔案鎖：manifest 沒有指向比這次新的匯出才寫匯出
    目錄，換 manifest 前在檔案鎖裡再比一次；換好後 fsync `preview/`，同一個資料庫交易記成功。
    寫目錄前、換 manifest 前各再確認一次接手代號與 `preview/` 還在原位；寫目錄（含改名後的 fsync）
    到換 manifest 之間停下（被接手、檢查失敗、中斷）時由 `_clean_up_before_switch` 收拾：manifest
    已經指向這一次就算已發佈、不刪；確定沒換才刪這次寫的目錄（比對身分）。manifest 換名一成功就記
    `progress.switched`（已發佈）：之後的位置檢查、fsync、記成功、commit 失敗都算已發佈待補記。

    Raises:
        DemoExportTakenOverError: 這一筆已經被別的行程接手（manifest 不換、寫好的匯出目錄刪掉）。
        DemoExportError: manifest 已經指向比這次新的匯出（`superseded`）、讀不懂
            （`manifest_unreadable`）、目錄已經存在、輸出位置不安全或途中被搬走（`output_unsafe`）、
            等檔案鎖太久（`output_busy`）。
        DemoExportPublishedUnrecordedError: manifest 已經換成這一次，之後的位置檢查、fsync、記成功
            或 commit 失敗（那一筆留在 `running`，之後接手時補記）。
    """
    try:
        with database.transaction() as connection:
            _hold_publish(connection, run, token)
            with _locked(out.preview_fd):
                current = _read_manifest(out)
                if current is not None and _is_complete_here(out, current, run):
                    # 別的行程（被接手前的同一筆）已經發佈好這一次：補記成功，不重寫。
                    return _record_recovered(connection, out, run, token, current)
                _check_not_superseded(current, run)
                _check_owned(connection, run, token)
                _check_in_place(out)
                try:
                    _write_export_dir(out, run.export_id, files, progress)
                    _check_not_superseded(_read_manifest(out), run)
                    _check_owned(connection, run, token)
                    _check_in_place(out)
                    _swap_manifest(out, files[MANIFEST_FILE], progress)
                except BaseException:
                    _clean_up_before_switch(out, run, progress)
                    raise
                # manifest 一換好就算已發佈（ADR-0061）：之後的位置檢查、fsync、記成功、
                # commit 失敗都不能記成沒發佈過，留在 running 等補記。換完才發現 preview/
                # 被搬走時日誌另外寫。
                _check_in_place_after_switch(out, run)
                _fsync_dir(out.preview_fd)
            update_demo_export_finished(connection, run.id, run_token=token, failure=None)
    except Exception as exc:
        if progress.switched:
            raise DemoExportPublishedUnrecordedError(run.export_id, type(exc).__name__) from None
        raise
    return DemoExportResult(
        export_id=result.export_id,
        paper_count=result.paper_count,
        full_content_count=result.full_content_count,
        manifest_path=out.preview_path / MANIFEST_FILE,
    )


def _write_file(directory_fd: int, name: str, content: bytes) -> None:
    """在 `directory_fd` 底下寫一個新檔（已經存在、或是符號連結就失敗）並 fsync，確定內容先落到磁碟
    才換名。"""
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o644,
        dir_fd=directory_fd,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(directory_fd: int) -> None:
    """fsync 一個打開的目錄（讓裡面的新增、換名落到磁碟）。"""
    os.fsync(directory_fd)


def _rename_dir(directory_fd: int, source: str, target: str) -> None:
    """`directory_fd` 底下的暫存目錄改名成匯出目錄。同一個檔案系統裡改名是原子的；目標已經存在、
    而且不是空目錄時失敗（目標是空目錄時 POSIX 會直接取代它，所以呼叫前先確認目標不存在）。"""
    os.rename(source, target, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)


def _replace_file(directory_fd: int, source: str, target: str) -> None:
    """暫存的 manifest 換成正式的（原子的換名；目標是符號連結時換掉的是連結本身，不會跟過去）。"""
    os.replace(source, target, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)


def _exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _fill_staging(
    preview_fd: int, staging: str, export_id: str, data_files: Mapping[str, bytes]
) -> None:
    """把資料檔寫進暫存目錄（每個檔 fsync），再由深到淺 fsync 各層目錄。"""
    staging_fd = _open_dir(staging, preview_fd)
    opened: dict[str, int] = {"": staging_fd}
    try:
        for path, content in data_files.items():
            parent, _, name = path.removeprefix(f"{export_id}/").rpartition("/")
            if parent not in opened:
                os.mkdir(parent, 0o755, dir_fd=staging_fd)
                opened[parent] = _open_dir(parent, staging_fd)
            _write_file(opened[parent], name, content)
        for parent in sorted(opened, key=lambda item: item.count("/") + bool(item), reverse=True):
            _fsync_dir(opened[parent])
    finally:
        for descriptor in opened.values():
            os.close(descriptor)


def _write_export_dir(
    out: _Output, export_id: str, files: Mapping[str, bytes], progress: _Progress
) -> None:
    """資料檔寫進暫存目錄、改名成 `preview/<匯出編號>/`，再 fsync `preview/`（新目錄的名字先落到
    磁碟，才換 manifest：主機突然斷電時，不會有指向缺檔的新 manifest）。改名前把暫存目錄的身分記
    進 `progress.written`，改名確定失敗才清掉：改名之後的任何失敗或中斷（含 fsync）都由
    `_publish` 的清理刪掉這個目錄。

    Raises:
        DemoExportError: 目錄已經存在（`output_exists`）、路徑不是認得的資料檔（`output_unsafe`）。
    """
    data_files = {path: content for path, content in files.items() if path != MANIFEST_FILE}
    for path in data_files:
        if not _DATA_FILE.fullmatch(path) or not path.startswith(f"{export_id}/"):
            raise _unsafe("資料檔的路徑不是認得的資料檔，不寫")
    if _exists(out.preview_fd, export_id):
        raise DemoExportError(
            DemoExportFailure.OUTPUT_EXISTS, f"{export_id} 的目錄已經存在，不覆寫"
        )
    staging = f".{export_id}.tmp-{uuid.uuid4().hex}"
    os.mkdir(staging, 0o755, dir_fd=out.preview_fd)
    try:
        _fill_staging(out.preview_fd, staging, export_id, data_files)
        made = os.stat(staging, dir_fd=out.preview_fd, follow_symlinks=False)
        progress.written = (made.st_dev, made.st_ino)
        try:
            _rename_dir(out.preview_fd, staging, export_id)
        except OSError as exc:
            progress.written = None
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                raise DemoExportError(
                    DemoExportFailure.OUTPUT_EXISTS, f"{export_id} 的目錄已經存在，不覆寫"
                ) from None
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True, dir_fd=out.preview_fd)
        raise
    _fsync_dir(out.preview_fd)


def _manifest_points_here(out: _Output, run: DemoExportRun) -> bool | None:
    """現在的 manifest 是不是指向這一次（只看匯出編號）；讀的時候出錯回 None（不知道）。"""
    try:
        current = _read_manifest(out)
    except Exception:
        return None
    return current is not None and current.export_id == run.export_id


def _clean_up_before_switch(out: _Output, run: DemoExportRun, progress: _Progress) -> None:
    """`_publish` 寫目錄到換 manifest 這一段停下時的清理（還拿著檔案鎖）。已經開始換 manifest 的名
    （`replacing`）時先重讀 manifest：換名已經成功、只是還沒設 `switched` 就停下（例如換名那一下收到
    Ctrl-C）時，manifest 已經指向這一次，補設 `switched`、不刪目錄（已發佈、待補記，ADR-0061）；讀不
    出 manifest 時不知道讀者看不看得到，目錄留著。確定沒換時才刪這次寫的目錄
    （`_discard_export_dir`）。"""
    if progress.switched:
        return
    points_here = _manifest_points_here(out, run) if progress.replacing else False
    if points_here:
        progress.switched = True
        log.error(
            "換 manifest 的那一下停下：manifest 已經指向這一次，匯出目錄留著，算已發佈待補記",
            export_id=run.export_id,
        )
        return
    if progress.written is None:
        return
    if points_here is None:
        progress.dir = "kept"
        log.warning(
            "換 manifest 之前停下，但讀不出現在的 manifest，這次寫好的匯出目錄留著沒刪；"
            "同一個匯出編號重做會碰到 output_exists",
            export_id=run.export_id,
        )
        return
    progress.dir = _discard_export_dir(out, run.export_id, progress.written)


def _discard_export_dir(
    out: _Output, export_id: str, written: tuple[int, int]
) -> Literal["none", "removed", "kept"]:
    """刪掉這次寫的 `preview/<匯出編號>/`：先相對於 `preview/` 的目錄 fd 打開它（不跟符號連結）、用
    `st_dev`／`st_ino` 比對是不是這次寫的那一個，不是（被搬走後換成別的目錄）就不刪；是的話相對於
    它的目錄 fd 刪掉裡面的東西，最後再刪掉這個空目錄。manifest 沒有指向它，沒有讀者讀得到。回
    `removed`（刪掉了）、`none`（沒有這個目錄：改名沒成功就停下）或 `kept`（不是這次寫的、刪不掉，
    記警告）；原本的例外由呼叫端照樣往上丟。"""
    try:
        export_fd = _open_dir(export_id, out.preview_fd)
    except FileNotFoundError:
        log.info("換 manifest 之前停下，沒有留下這次的匯出目錄", export_id=export_id)
        return "none"
    except Exception as exc:
        log.warning(
            "換 manifest 之前停下，這次寫好的匯出目錄打不開，沒有刪掉；"
            "同一個匯出編號重做會碰到 output_exists",
            export_id=export_id,
            error_type=type(exc).__name__,
        )
        return "kept"
    try:
        opened = os.fstat(export_fd)
        if (opened.st_dev, opened.st_ino) != written:
            log.warning(
                "換 manifest 之前停下，但 preview/ 底下同名的目錄已經不是這次寫的，沒有刪",
                export_id=export_id,
            )
            return "kept"
        for name in os.listdir(export_fd):
            if stat.S_ISDIR(os.stat(name, dir_fd=export_fd, follow_symlinks=False).st_mode):
                shutil.rmtree(name, dir_fd=export_fd)
            else:
                os.unlink(name, dir_fd=export_fd)
        # 名字指向的若已不是這個目錄，rmdir 不會刪掉非空的別的目錄；空的被換進來的目錄才會被刪。
        os.rmdir(export_id, dir_fd=out.preview_fd)
    except Exception as exc:
        log.warning(
            "換 manifest 之前停下，這次寫好的匯出目錄沒有刪掉；"
            "同一個匯出編號重做會碰到 output_exists",
            export_id=export_id,
            error_type=type(exc).__name__,
        )
        return "kept"
    finally:
        os.close(export_fd)
    log.info("換 manifest 之前停下，已刪掉這次寫好的匯出目錄", export_id=export_id)
    return "removed"


def _check_in_place_after_switch(out: _Output, run: DemoExportRun) -> None:
    """換好 manifest 之後確認 `preview/` 還在原位。不在原位時丟錯（那一筆留在 `running`、算已發佈待
    補記，不記成功也不記失敗），日誌寫明新的 manifest 寫進了被搬走的那個資料夾。

    Raises:
        DemoExportError: `preview/` 被搬走或換掉（`output_unsafe`）。
    """
    try:
        _check_in_place(out)
    except DemoExportError:
        log.error(
            "換 manifest 的途中 preview/ 被搬走或換掉：新的 manifest 寫進了被搬走的那個資料夾",
            export_id=run.export_id,
        )
        raise


def _swap_manifest(out: _Output, content: bytes, progress: _Progress) -> None:
    """manifest 先寫成暫存檔（fsync），再原子地換成 `preview/manifest.json`；換名一成功就記
    `progress.switched`（已發佈）。換名前設 `progress.replacing`（從這裡起 manifest 可能已經換了）。
    失敗時刪掉暫存檔。"""
    pending = f".{MANIFEST_FILE}.tmp-{uuid.uuid4().hex}"
    try:
        _write_file(out.preview_fd, pending, content)
        progress.replacing = True
        _replace_file(out.preview_fd, pending, MANIFEST_FILE)
        progress.switched = True
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(pending, dir_fd=out.preview_fd)
        raise
