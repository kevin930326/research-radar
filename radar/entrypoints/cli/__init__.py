"""`radar` 指令：專案唯一的 CLI 入口（NFR-11）。typer 只能出現在這個套件。"""

import itertools
import re
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated, NoReturn

import structlog
import typer

from radar.config import (
    ConfigError,
    database_pool_size,
    load_budget_config,
    load_context_values,
    load_domain_config,
    load_embedding_config,
    load_import_baseline,
    load_llm_batch_config,
    load_migration_settings,
    load_models_config,
    load_settings,
    load_taxonomy,
    load_zh_summary_rules,
)
from radar.domain.admin_runs import (
    ADMIN_TASKS,
    render_paper_ai_report,
    render_removal_blocks,
    render_retry_plan,
    render_retry_report,
    render_revision_estimate,
    render_source_removal_preview,
    render_source_removal_result,
)
from radar.domain.ai_outputs import CallMode, LlmTask
from radar.domain.budget import format_usd, render_cost_report
from radar.domain.coverage import render_coverage_report
from radar.domain.cube import UnknownJournalError
from radar.domain.database_requirements import DatabaseRequirementsError
from radar.domain.embeddings import (
    EmbeddingConfigurationError,
    render_backfill_report,
    render_backlog_estimate,
)
from radar.domain.import_report import render_import_report
from radar.domain.jobs import UnknownJobError, find_scheduled_job
from radar.domain.llm_batches import (
    PROVIDER_BATCH_ID_PATTERN,
    BatchSubmission,
    BatchSubmissionStatus,
    BatchSubmitUncertainError,
    LlmBatchPlanResult,
    LlmBatchSubmitResult,
    PlanStop,
    ResolveOutcome,
    SubmitOutcome,
    is_finished,
    render_batch_estimate,
    render_poll_row,
    render_poll_summary,
    render_queued_batches,
    render_wait_progress,
)
from radar.domain.papers import AbstractSource
from radar.domain.run_reports import RunScope, parse_ids_file, parse_since, render_run_report
from radar.domain.settings import Settings
from radar.domain.trial_runs import (
    MAX_TRIAL_PAPERS,
    TRIAL_TASKS,
    TrialArgumentError,
    TrialItem,
    TrialTargets,
    check_sample_size,
    format_trial_ids,
    parse_trial_ids_file,
    parse_trial_targets,
    render_trial_estimate,
    render_trial_item,
    render_trial_selection,
    render_trial_summary,
)
from radar.domain.viewer import SystemViewer
from radar.entrypoints.bootstrap import (
    MigrationsNotFoundError,
    build_database,
    build_embedding_provider,
    build_gateway_config,
    build_job_enqueuer,
    build_llm_provider,
    build_migration_runner,
    build_object_store,
    code_version,
)
from radar.entrypoints.logging_config import configure_logging
from radar.entrypoints.startup import check_ai_config
from radar.ports import LlmProvider
from radar.services.admin_runs import (
    get_paper_ai_report,
    preview_source_removal,
    remove_source_ai_outputs,
)
from radar.services.budget import get_budget_status
from radar.services.coverage import get_coverage
from radar.services.embeddings import (
    EmbeddingModelMismatchError,
    check_embedding_model,
    embed_papers,
    estimate_embedding_backlog,
)
from radar.services.import_report import get_import_report
from radar.services.jobs import enqueue_scheduled_job
from radar.services.llm_batches import (
    REVISION_TASKS,
    LlmBatchSetup,
    count_llm_batch_running_runs,
    estimate_llm_batches,
    estimate_revision_reruns,
    list_llm_batch_pending_papers,
    list_llm_batches,
    plan_llm_batches,
    poll_llm_batches,
    resolve_llm_batch,
    submit_llm_batch,
)
from radar.services.migrations import migrate_database
from radar.services.reparse import (
    AdvisoryLockLostError,
    ReparseBusyError,
    WeeklyUpdateInProgressError,
    reparse_papers,
)
from radar.services.run_reports import get_run_report
from radar.services.run_retry import plan_retry, run_retry
from radar.services.trial_runs import (
    TrialPapersNotFoundError,
    TrialSetup,
    estimate_trial,
    resolve_trial_papers,
    run_trial,
    sample_trial_papers,
)

# 例外時不印區域變數：裡面可能有連線字串。
app = typer.Typer(
    help="Research Radar 的指令列工具。",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
log = structlog.get_logger(__name__)

# 資源指令：`radar weekly-run <動詞>`（Conventions「CLI」：`radar <名詞> <動詞>`）。
weekly_run_app = typer.Typer(help="每週更新（weekly_run）。", no_args_is_help=True)
app.add_typer(weekly_run_app, name="weekly-run")
embeddings_app = typer.Typer(help="向量（embedding）。", no_args_is_help=True)
app.add_typer(embeddings_app, name="embeddings")
runs_app = typer.Typer(help="AI 工作的執行（derivation_run）。", no_args_is_help=True)
app.add_typer(runs_app, name="runs")
batches_app = typer.Typer(help="AI 工作的 Batch（batch_submission）。", no_args_is_help=True)
app.add_typer(batches_app, name="batches")
# Ctrl-C 中斷時的退出碼（128 + SIGINT）。
INTERRUPTED_EXIT_CODE = 130


@app.callback()
def _root() -> None:
    """Research Radar 的指令列工具。"""
    # 每個指令執行前先驗證領域設定（config/*.yaml，AD-18）；錯了就不執行、退出碼 1。
    try:
        load_domain_config()
    except ConfigError as exc:
        _fail("領域設定錯誤，沒有執行：", exc.problems)
    # AI 設定（prompt 登記、models、budget、zh_summary、分類表、情境值清單、llm_batches）錯了也
    # 不執行（Story 2.9）。
    try:
        check_ai_config()
    except ConfigError as exc:
        _fail("AI 設定錯誤，沒有執行：", exc.problems)


@app.command()
def version() -> None:
    """印出 radar 的版本。"""
    typer.echo(package_version("radar"))


@app.command()
def migrate() -> None:
    """用擁有者角色（RADAR_MIGRATION_DATABASE_URL）把資料庫遷移到最新版本。

    失敗時退出碼 1，原因印在 stderr；連線字串與密碼不會出現在輸出裡。
    """
    configure_logging()
    try:
        settings = load_migration_settings()
    except ConfigError as exc:
        log.error("設定錯誤，無法遷移", problems=exc.problems)
        _fail("設定錯誤，沒有遷移：", exc.problems)
    try:
        migrate_database(build_migration_runner(settings))
    except DatabaseRequirementsError as exc:
        log.error("資料庫不符合需求，遷移已整個退回", problems=exc.problems)
        _fail("資料庫不符合需求，遷移已整個退回：", exc.problems)
    except MigrationsNotFoundError:
        log.error("找不到遷移檔（工作目錄下的 migrations/）")
        _fail("找不到遷移檔：請在有 migrations/ 的目錄執行", [])
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("遷移失敗，已整個退回", error_type=type(exc).__name__)
        _fail(f"遷移失敗，已整個退回（{type(exc).__name__}）；細節見日誌", [])


@app.command()
def enqueue(
    job: str = typer.Argument(help="排程名稱，例如 sweep"),
) -> None:
    """把一個排程工作排進它的佇列（cron 呼叫；排程表在 deploy/crontab）。

    已經在排隊時不會多排一筆，退出碼一樣是 0。不認得的工作退出碼 2；
    設定錯誤或連不上資料庫退出碼 1，只印錯誤類別，不印連線字串。
    """
    configure_logging()
    try:
        find_scheduled_job(job)
    except UnknownJobError as exc:
        typer.echo(f"不認得的工作：{job[:64]!r}", err=True)
        typer.echo(f"可用的工作：{'、'.join(exc.available)}", err=True)
        raise typer.Exit(2) from None
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("設定錯誤，無法排入工作", problems=exc.problems)
        _fail("設定錯誤，沒有排入：", exc.problems)
    try:
        with build_job_enqueuer(
            settings, max_connections=database_pool_size(settings, "cli")
        ) as enqueue_job:
            queued = enqueue_scheduled_job(job, enqueue_job)
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("排入工作失敗", schedule=job, error_type=type(exc).__name__)
        _fail(f"排入失敗（{type(exc).__name__}）；細節見日誌", [])
    typer.echo(f"已排入 {job}" if queued else f"{job} 已經在排隊，沒有再排一筆")


@app.command()
def reparse() -> None:
    """只用已存的原始資料重新解析已經匯入的論文（不連 OpenAlex 或任何外部來源）。

    排除規則改了、或解析多了欄位時跑這個。解析結果先放暫存表、最後一個交易套用；
    不新增論文、不建立快照。每週更新正在抓取、或另一個 reparse 正在跑時拒絕執行、不寫任何東西，
    退出碼 1。
    持有寫入鎖的連線在兩個交易之間斷了（例如資料庫重啟）時中止、退出碼 1，印出原因與「直接重跑即可」
    （已經 commit 的不受影響）；斷在交易進行中時印一般的失敗訊息，同樣不會 commit。
    設定錯誤或執行失敗退出碼 1，只印錯誤類別，不印連線字串。
    """
    configure_logging()
    try:
        settings = load_settings()
        domain_config = load_domain_config()
    except ConfigError as exc:
        log.error("設定錯誤，無法重新解析", problems=exc.problems)
        _fail("設定錯誤，沒有重新解析：", exc.problems)
    try:
        # 建立物件儲存的 client 也放在這裡：botocore 初始化失敗的訊息會帶整個端點網址。
        store = build_object_store(settings)
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        result = reparse_papers(
            SystemViewer(),
            database,
            store,
            scope=domain_config.journals.scope_settings(),
            exclusion_rules=domain_config.exclusion_rules,
        )
    except WeeklyUpdateInProgressError as exc:
        log.error("每週更新正在進行，沒有重新解析", weekly_run_id=str(exc.weekly_run_id))
        _fail(str(exc), [])
    except ReparseBusyError as exc:
        log.error("另一個 reparse 或每週更新正在寫入，沒有重新解析")
        _fail(str(exc), [])
    except AdvisoryLockLostError as exc:
        # 訊息與原因只帶錯誤類別，不帶連線字串（radar/repo/db.py）。
        log.error("寫入鎖不見了，重新解析中止", reason=exc.reason)
        _fail(f"{exc}；直接重跑 radar reparse 即可", [])
    except ConfigError as exc:
        # build_object_store 缺設定時丟的；訊息只列環境變數名稱。
        log.error("設定錯誤，無法重新解析", problems=exc.problems)
        _fail("設定錯誤，沒有重新解析：", exc.problems)
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("重新解析失敗", error_type=type(exc).__name__)
        _fail(f"重新解析失敗（{type(exc).__name__}）；細節見日誌", [])
    typer.echo("重新解析完成")
    typer.echo(f"讀到的論文（外部鍵）：{result.raw_keys}")
    typer.echo(f"不在資料庫、略過：{result.not_in_database}")
    typer.echo(f"物件不見：{result.missing_object}")
    typer.echo(f"內容指紋對不上：{result.hash_mismatch}")
    typer.echo(f"解析失敗：{result.parse_error}")
    typer.echo(f"ID 對不上：{result.id_mismatch}")
    typer.echo(f"放進暫存表：{result.staged}")
    typer.echo(f"更新：{result.updated}")
    typer.echo(f"資料較舊、沒寫：{result.stale}")
    typer.echo(f"識別碼衝突：{result.conflicts}")


@weekly_run_app.command("report")
def weekly_run_report(
    weekly_run_id: Annotated[
        uuid.UUID | None, typer.Option("--id", help="每週更新的 id；不給就是最新的一筆")
    ] = None,
) -> None:
    """印出一次每週更新的匯入報表（只讀，不改任何資料）。

    每本期刊每年的過濾前篇數、依期刊的排除篇數、和 OQ-02 基準（config/import_baseline.yaml）
    的差異。沒有任何每週更新或找不到這個 id：退出碼 1；--id 不是 UUID：退出碼 2；
    設定錯誤或讀不到資料庫：退出碼 1，只印錯誤類別，不印連線字串。
    """
    configure_logging()
    try:
        settings = load_settings()
        domain_config = load_domain_config()
        baseline = load_import_baseline()
    except ConfigError as exc:
        log.error("設定錯誤，無法產生匯入報表", problems=exc.problems)
        _fail("設定錯誤，沒有產生報表：", exc.problems)
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        report = get_import_report(
            SystemViewer(),
            database,
            weekly_run_id,
            journals=domain_config.journals,
            baseline=baseline,
        )
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("讀取匯入報表失敗", error_type=type(exc).__name__)
        _fail(f"讀取匯入報表失敗（{type(exc).__name__}）；細節見日誌", [])
    if report is None:
        if weekly_run_id is None:
            _fail("還沒有任何每週更新", [])
        _fail(f"找不到這次每週更新：{weekly_run_id}", [])
    for line in render_import_report(report):
        typer.echo(line)


@app.command("coverage")
def coverage() -> None:
    """印出最新已發佈快照的覆蓋率（只讀，不改任何資料）。

    快照編號與截至日期（台北時間）、每本期刊每年的收錄篇數（total）與有摘要的篇數
    （with_abstract）、依目前的摘要來源分的篇數。只算 counted_paper 裡的論文（追蹤範圍內、
    沒有被排除、已算進已發佈的快照）。還沒有已發佈的快照、計數裡有設定沒有的期刊、設定錯誤或
    讀不到資料庫：退出碼 1；資料庫的錯誤只印類別，不印連線字串。
    """
    configure_logging()
    try:
        settings = load_settings()
        domain_config = load_domain_config()
    except ConfigError as exc:
        log.error("設定錯誤，無法讀取覆蓋率", problems=exc.problems)
        _fail("設定錯誤，沒有讀取覆蓋率：", exc.problems)
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        report = get_coverage(SystemViewer(), database, journals=domain_config.journals)
    except UnknownJournalError as exc:
        log.error("計數裡有設定沒有的期刊", issn_ls=list(exc.issn_ls))
        _fail(
            f"計數裡有設定沒有的期刊（ISSN-L：{'、'.join(exc.issn_ls)}）；"
            "config/journals.yaml 改過但論文的追蹤範圍還沒重算",
            ["重算追蹤範圍的 radar rescope 還沒做；在那之前先把那本期刊加回設定"],
        )
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("讀取覆蓋率失敗", error_type=type(exc).__name__)
        _fail(f"讀取覆蓋率失敗（{type(exc).__name__}）；細節見日誌", [])
    if report is None:
        _fail("還沒有已發佈的快照", [])
    for line in render_coverage_report(report):
        typer.echo(line)


def _utc_now() -> datetime:
    """`radar costs` 的時鐘（測試換掉它來固定「本月」）。"""
    return datetime.now(UTC)


@app.command("costs")
def costs() -> None:
    """印出本月（台北時間）的 LLM 費用（只讀，不改任何資料）。

    依工作代碼分的費用、合計、熔斷線、離熔斷線還有多少、是否「只檢索」；金額單位美元、
    4 位小數。設定錯誤或讀不到資料庫：退出碼 1；資料庫的錯誤只印類別，不印連線字串。
    """
    configure_logging()
    try:
        settings = load_settings()
        budget = load_budget_config()
    except ConfigError as exc:
        log.error("設定錯誤，無法讀取費用", problems=exc.problems)
        _fail("設定錯誤，沒有讀取費用：", exc.problems)
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        status = get_budget_status(SystemViewer(), database, budget, now=_utc_now())
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("讀取費用失敗", error_type=type(exc).__name__)
        _fail(f"讀取費用失敗（{type(exc).__name__}）；細節見日誌", [])
    for line in render_cost_report(status):
        typer.echo(line)


@embeddings_app.command("backfill")
def embeddings_backfill(
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="這次最多送幾篇；不給就做到全部做完或今天的額度比例"),
    ] = None,
    estimate: Annotated[
        bool,
        typer.Option("--estimate", help="只用本地資料估還要幾篇、幾個 token、幾天；不送任何請求"),
    ] = False,
) -> None:
    """把還沒有向量（或標題、摘要變了）的論文向量化（Cloudflare Workers AI）。

    照 config/embeddings.yaml 的模型、每批篇數與每日額度；到今天的額度比例就停下（不算失敗），
    再跑一次會從沒做完的接著做，已經做過、輸入沒變的不重送。印出這次做了幾篇、估計用了多少
    token，並照這次的平均推算全庫還要多少 token、幾天。

    --estimate 只讀資料庫，不需要 Cloudflare 的 token。設定錯誤、讀不到資料庫、token 或帳號
    不對、這次有一批失敗或有任何一篇標成失敗（其他論文照做）：退出碼 1；錯誤只印類別或狀態碼，
    不印 token、帳號或連線字串。
    """
    configure_logging()
    if estimate and limit is not None:
        _fail("--estimate 只估算、不送請求，不能和 --limit 一起用", [])
    try:
        settings = load_settings()
        config = load_embedding_config()
    except ConfigError as exc:
        log.error("設定錯誤，無法向量化", problems=exc.problems)
        _fail("設定錯誤，沒有向量化：", exc.problems)
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        if estimate:
            check_embedding_model(database, config)
            backlog = estimate_embedding_backlog(
                SystemViewer(), database, config, settings.cloud_policy
            )
            for line in render_backlog_estimate(backlog, config.quota, config.model_id):
                typer.echo(line)
            return
        with build_embedding_provider(settings, config) as provider:
            result = embed_papers(
                SystemViewer(),
                database,
                provider,
                config,
                settings.cloud_policy,
                code_version=code_version(),
                clock=_utc_now,
                limit=limit,
            )
    except ConfigError as exc:
        log.error("設定錯誤，無法向量化", problems=exc.problems)
        _fail("設定錯誤，沒有向量化：", exc.problems)
    except EmbeddingModelMismatchError as exc:
        log.error("向量模型的設定對不上", reason=str(exc))
        _fail("向量模型的設定對不上，沒有送任何請求：", [str(exc)])
    except EmbeddingConfigurationError as exc:
        log.error("Cloudflare 拒絕請求", http_status=exc.http_status)
        _fail(
            f"Cloudflare 拒絕請求（HTTP {exc.http_status}），這一批沒有寫入：",
            [
                "確認 RADAR_CLOUDFLARE_ACCOUNT_ID 是 Cloudflare 的 account id",
                "確認 RADAR_CLOUDFLARE_API_TOKEN 有 Workers AI 的 Read 與 Edit 權限、沒有過期",
            ],
        )
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("向量化失敗", error_type=type(exc).__name__)
        _fail(f"向量化失敗（{type(exc).__name__}）；細節見日誌", [])
    try:
        backlog = estimate_embedding_backlog(
            SystemViewer(), database, config, settings.cloud_policy
        )
    except Exception as exc:
        # 向量已經寫進去了；只是推算剩下的失敗，照樣印出這次做了多少。
        log.error("推算剩下的篇數失敗", error_type=type(exc).__name__)
        typer.echo(f"這次寫入向量：{result.embedded:,} 篇（送出 {result.requests:,} 次請求）")
        _fail(f"推算剩下的篇數失敗（{type(exc).__name__}）；細節見日誌", [])
    for line in render_backfill_report(result, backlog, config.quota, config.model_id):
        typer.echo(line)
    if result.has_failures:
        raise typer.Exit(1)


def _read_ids_file(path: Path) -> TrialTargets:
    """讀 `--ids-file`（UTF-8；開頭有 BOM 也收，有些編輯器或試算表匯出時會加）。

    Raises:
        TrialArgumentError: 讀不到、不是 UTF-8，或內容看不懂（見 `parse_trial_ids_file`）。
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise TrialArgumentError(f"讀不到名單檔（{type(exc).__name__}）") from None
    return parse_trial_ids_file(text)


def _trial_request(
    targets: list[str],
    sample: int | None,
    seed: int | None,
    ids_file: Path | None = None,
    save_ids: Path | None = None,
) -> TrialTargets | tuple[int, int]:
    """`radar runs trial` 的參數：指定清單或名單檔（`TrialTargets`），或抽樣（N, seed）。

    Raises:
        TrialArgumentError: 清單、--ids-file、--sample 給了不只一種、--sample 沒給 --seed（或反過
            來）、都沒給、N 不在範圍內、清單或名單檔看不懂、超過上限，或 --save-ids 的檔案已經存在。
    """
    if save_ids is not None and save_ids.exists():
        raise TrialArgumentError(f"--save-ids 的檔案已經存在，不覆寫：{save_ids}")
    if sum((bool(targets), sample is not None, ids_file is not None)) > 1:
        raise TrialArgumentError("論文清單、--ids-file、--sample 只能擇一")
    if ids_file is not None:
        if seed is not None:
            raise TrialArgumentError("--seed 只能和 --sample 一起用")
        return _read_ids_file(ids_file)
    if sample is None:
        if seed is not None:
            raise TrialArgumentError("--seed 只能和 --sample 一起用")
        if not targets:
            raise TrialArgumentError("要指定論文 ID 或 DOI，或用 --sample N --seed S 抽樣")
        return parse_trial_targets(targets)
    if seed is None:
        raise TrialArgumentError(
            "--sample 要和 --seed 一起用（同一個 seed、同一時間點的候選才抽得到同一批）"
        )
    check_sample_size(sample)
    return sample, seed


@runs_app.command("trial")
def runs_trial(
    targets: Annotated[
        list[str] | None,
        typer.Argument(help="論文 ID（UUID）或 DOI，可以給多個", show_default=False),
    ] = None,
    sample: Annotated[
        int | None,
        typer.Option(
            "--sample", help=f"從調整組抽幾篇（1～{MAX_TRIAL_PAPERS}），要和 --seed 一起用"
        ),
    ] = None,
    seed: Annotated[
        int | None,
        typer.Option(
            "--seed",
            help="抽樣的 seed（候選沒變時同一個 seed 抽到同一批；要固定名單用 --save-ids）",
        ),
    ] = None,
    ids_file: Annotated[
        Path | None,
        typer.Option(
            "--ids-file",
            help="從名單檔讀論文（一行一個 ID 或 DOI，# 開頭與空行略過）；重跑同一份名單用",
        ),
    ] = None,
    save_ids: Annotated[
        Path | None,
        typer.Option(
            "--save-ids", help="把選到的論文 ID 存成名單檔（不覆寫；確認前就寫，按 n 也會留下）"
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="不問確認，直接執行")] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose", help="每項另印耗時、tokens、回應形狀與送出字元數（不印論文內容與輸出）"
        ),
    ] = False,
    show_output: Annotated[
        bool, typer.Option("--show-output", help="印驗證過的輸出；被拒收時印模型原文")
    ] = False,
) -> None:
    """只對指定的論文，用目前的 prompt 跑一次 AI（中文重點、分類、研究模型）。

    給論文 ID／DOI（最多 100 篇）、用 --ids-file 讀名單檔，或用 --sample N --seed S 從調整組抽
    N 篇（--save-ids 把選到的存成名單檔）。先印選到的論文、含重試的最壞費用與本月預算，確認後才跑
    （--yes 不問）；只跑確認過的項目，確認後輸入變了的標 input_changed、不呼叫。每項都經同一個
    閘門（互動呼叫，照常略過已有結果的、記帳、受熔斷限制）。每做完一項印一行，最後印每篇每種工作
    的狀態、拒收統計、這次有沒有呼叫模型與這次的費用。

    參數看不懂、清單／--ids-file／--sample 給了不只一種、--sample 沒給 --seed、超過 100 篇、名單檔
    讀不到、--save-ids 的檔案已經存在：退出碼 2（不碰資料庫）。有論文找不到、名單檔寫不進去、沒有
    確認、設定錯、有暫時性失敗、出錯或輸入在確認後變了的項目：退出碼 1。Ctrl-C：退出碼 130，重跑
    同一個指令只做沒做完的。錯誤只印類別或狀態碼，不印金鑰與連線字串。
    """
    configure_logging()
    try:
        request = _trial_request(targets or [], sample, seed, ids_file, save_ids)
    except TrialArgumentError as exc:
        typer.echo(f"參數看不懂，沒有執行：{exc}", err=True)
        raise typer.Exit(2) from None
    try:
        settings = load_settings()
        setup = TrialSetup(
            gateway=build_gateway_config(settings, load_models_config()),
            budget=load_budget_config(),
            zh_summary_rules=load_zh_summary_rules(),
            taxonomy=load_taxonomy(),
            context_values=load_context_values(),
        )
        provider_context = build_llm_provider(settings)
    except ConfigError as exc:
        log.error("設定錯誤，無法試跑", problems=exc.problems)
        _fail("設定錯誤，沒有執行：", exc.problems)
    with provider_context as provider:
        # 執行前（選論文、預估、確認）按 Ctrl-C：什麼都還沒寫，退出碼 130。
        try:
            try:
                database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
                if isinstance(request, TrialTargets):
                    selection = resolve_trial_papers(SystemViewer(), database, request)
                else:
                    selection = sample_trial_papers(SystemViewer(), database, *request)
            except TrialPapersNotFoundError as exc:
                log.error("找不到指定的論文", missing=list(exc.missing))
                _fail("找不到這些論文，一篇都沒有跑：", list(exc.missing))
            except Exception as exc:
                # 例外訊息可能含主機與帳號，只記類別名稱。
                log.error("讀取論文失敗", error_type=type(exc).__name__)
                _fail(f"讀取論文失敗（{type(exc).__name__}）；細節見日誌", [])
            for line in render_trial_selection(selection):
                typer.echo(line)
            if not selection.papers:
                typer.echo("沒有可以跑的論文，沒有執行")
                return
            if save_ids is not None:
                try:
                    with save_ids.open("x", encoding="utf-8") as file:
                        file.write(format_trial_ids(selection.papers))
                except OSError as exc:
                    log.error("名單檔寫不進去", error_type=type(exc).__name__)
                    _fail(f"名單檔寫不進去（{type(exc).__name__}），沒有執行", [])
                log.info("試跑名單已存檔", papers=len(selection.papers))
                typer.echo(f"名單已存到 {save_ids}（{len(selection.papers)} 篇）")
            # 預估 → 確認 → 執行 → 總表。最壞花費照 adapter 實際的重試次數算。
            try:
                estimate = estimate_trial(
                    SystemViewer(), database, setup, selection, attempts=provider.attempts
                )
                status = get_budget_status(SystemViewer(), database, setup.budget, now=_utc_now())
            except Exception as exc:
                log.error("試跑的預估失敗", error_type=type(exc).__name__)
                _fail(f"預估失敗（{type(exc).__name__}），沒有執行；細節見日誌", [])
            for line in render_trial_estimate(estimate, status):
                typer.echo(line)
            if not yes:
                try:
                    confirmed = typer.confirm("要執行嗎？", default=False)
                except typer.Abort as exc:
                    # click 把 Ctrl-C 與 EOF 都換成 Abort；原本的例外留在 __context__。
                    if isinstance(exc.__context__, KeyboardInterrupt):
                        raise KeyboardInterrupt from None
                    confirmed = False
                if not confirmed:
                    typer.echo("沒有執行", err=True)
                    raise typer.Exit(1)
        except KeyboardInterrupt:
            log.warning("試跑在執行前被中斷")
            typer.echo("中斷了，沒有執行", err=True)
            raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
        total = len(selection.papers) * len(TRIAL_TASKS)
        counter = itertools.count(1)

        def print_item(item: TrialItem) -> None:
            for line in render_trial_item(
                next(counter), total, item, verbose=verbose, show_output=show_output
            ):
                typer.echo(line)

        try:
            report = run_trial(
                SystemViewer(),
                database,
                provider,
                setup,
                selection,
                clock=_utc_now,
                estimate=estimate,
                diagnostics=verbose,
                show_output=show_output,
                on_item=print_item,
            )
        except KeyboardInterrupt:
            log.warning("試跑被中斷")
            typer.echo("中斷了：做完的已經寫好，重跑同一個指令只做沒做完的（不重付）", err=True)
            raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
        except Exception as exc:
            log.error("試跑失敗", error_type=type(exc).__name__)
            _fail(f"試跑失敗（{type(exc).__name__}）；做完的已經寫好，細節見日誌", [])
        for line in render_trial_summary(report, verbose=verbose):
            typer.echo(line)
        if report.stopped_by_configuration:
            statuses = sorted(
                {str(item.http_status) for item in report.items if item.http_status is not None}
            )
            shown = f"HTTP {'、'.join(statuses)}" if statuses else "沒有狀態碼"
            typer.echo(
                f"金鑰、權限或模型設定錯誤（{shown}），停下；確認 RADAR_ANTHROPIC_API_KEY 與"
                " config/models.yaml",
                err=True,
            )
        if report.exit_code:
            raise typer.Exit(report.exit_code)


# --- Batch（Story 2.11、ADR-0043） -----------------------------------------------------------

# `radar batches wait` 兩次檢查之間的等待（測試換掉它，不真的睡）。
_sleep = time.sleep
_BATCH_ID = re.compile(PROVIDER_BATCH_ID_PATTERN)
_ALL_BATCH_STATUSES = frozenset(BatchSubmissionStatus)
# `radar batches wait` 連續幾次查詢都有錯誤（例如金鑰失效）就停下，不無限重試。
_WAIT_MAX_ERROR_ROUNDS = 3


def _load_batch_setup(action: str) -> tuple[Settings, LlmBatchSetup]:
    """讀 Batch 要的設定；設定錯就印出來、退出碼 1。"""
    try:
        settings = load_settings()
        setup = LlmBatchSetup(
            gateway=build_gateway_config(settings, load_models_config(), call_mode=CallMode.BATCH),
            budget=load_budget_config(),
            batches=load_llm_batch_config(),
            zh_summary_rules=load_zh_summary_rules(),
            taxonomy=load_taxonomy(),
            context_values=load_context_values(),
        )
    except ConfigError as exc:
        log.error(f"設定錯誤，無法{action}", problems=exc.problems)
        _fail(f"設定錯誤，沒有{action}：", exc.problems)
    return settings, setup


def _lazy_provider(settings: Settings, stack: ExitStack) -> Callable[[], LlmProvider]:
    """第一次真的要查或送時才建 provider（缺金鑰時只碰資料庫的路照樣做得完），之後重用同一個；
    由 `stack` 關掉。"""
    built: list[LlmProvider] = []

    def open_provider() -> LlmProvider:
        if not built:
            built.append(stack.enter_context(build_llm_provider(settings)))
        return built[0]

    return open_provider


def _confirm(question: str) -> bool:
    """問 y/N。Ctrl-C 照樣丟 `KeyboardInterrupt`；EOF 當成沒有確認。"""
    try:
        return typer.confirm(question, default=False)
    except typer.Abort as exc:
        # click 把 Ctrl-C 與 EOF 都換成 Abort；原本的例外留在 __context__。
        if isinstance(exc.__context__, KeyboardInterrupt):
            raise KeyboardInterrupt from None
        return False


def _render_plan(result: LlmBatchPlanResult) -> list[str]:
    lines = [
        f"排好 {len(result.submission_ids)} 批、共 {result.planned:,} 筆"
        f"（保守上限 {format_usd(result.estimated_max_cost_usd)} 美元）",
    ]
    if result.deferred_budget:
        lines.append(f"預算放不下、這次不排：{result.deferred_budget:,} 筆")
    if result.stop is PlanStop.HELD_MONTH_START:
        lines.append("月初窗口（台北時間每月 1 日早上），規劃延後；過了再跑一次")
    elif result.stop is PlanStop.BUDGET_TRIPPED:
        lines.append("已熔斷，規劃不排任何批次")
    elif result.stop is PlanStop.RERUN_CAP_EXCEEDED:
        lines.append(
            "分類表或情境值清單改版後要重跑的超過 revision_rerun_max_usd，規劃整次不排；"
            "用 radar runs estimate-revision 看篇數與金額，決定後再調 config/llm_batches.yaml"
        )
    return lines


_NOT_SENT = frozenset(
    {
        SubmitOutcome.HELD,
        SubmitOutcome.FAILED,
        SubmitOutcome.NEEDS_ATTENTION,
        SubmitOutcome.REQUEUED,
    }
)


def _render_submit(batch_submission_id: uuid.UUID, result: LlmBatchSubmitResult) -> str:
    line = f"{batch_submission_id}  {result.outcome.value}  {result.status.value}"
    if result.outcome is SubmitOutcome.SUBMITTED:
        line += f"  {result.sent:,} 筆 → {result.provider_batch_id}"
    if result.input_changed:
        line += f"  輸入變了、沒送 {result.input_changed:,} 筆"
    if result.reason is not None:
        line += f"  原因：{result.reason.value}"
    return line


@batches_app.command("create")
def batches_create(
    yes: Annotated[bool, typer.Option("--yes", help="不問確認，直接規劃與送出")] = False,
    estimate_only: Annotated[
        bool, typer.Option("--estimate", help="只印預估，不寫任何資料、不送出")
    ] = False,
) -> None:
    """手動規劃全庫的 AI 工作（中文重點、分類、研究模型）並送出 Batch。

    先印預估（各工作筆數、Batch 價的保守上限、本月預算剩餘），以及已經排好、還沒送出的 queued
    批次（id、筆數、上限）與兩者合計的上限；確認後（--yes 不問）規劃（不看
    weekly_planning_enabled；月初窗口、熔斷照樣看），再在這個行程裡只送出確認過的批次（確認前列出的
    queued 批次＋這次規劃建的），印每批結果與下一步（radar batches wait）。確認之後才出現的其他
    queued 批次（例如同時進行的每週規劃）不送，列出來讓你另外確認。--estimate 只估不寫。

    設定錯、讀不到資料庫、沒有確認、有批次沒送成（延後、失敗、不確定）：退出碼 1。Ctrl-C：退出碼
    130。錯誤只印類別或狀態碼，不印金鑰與連線字串。
    """
    configure_logging()
    settings, setup = _load_batch_setup("建 Batch")
    try:
        try:
            database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
            estimate = estimate_llm_batches(SystemViewer(), database, setup)
            status = get_budget_status(SystemViewer(), database, setup.budget, now=_utc_now())
            # 確認的範圍：預估（新排的）＋現在已經排好、還沒送出的批次（這次一起送）。
            confirmed = list_llm_batches(SystemViewer(), database, {BatchSubmissionStatus.QUEUED})
        except Exception as exc:
            # 例外訊息可能含主機與帳號，只記類別名稱。
            log.error("Batch 預估失敗", error_type=type(exc).__name__)
            _fail(f"預估失敗（{type(exc).__name__}），沒有執行；細節見日誌", [])
        for line in render_batch_estimate(estimate, status):
            typer.echo(line)
        for line in render_queued_batches(confirmed, estimate):
            typer.echo(line)
        if estimate_only:
            return
        if not yes and not _confirm("要規劃並送出嗎？"):
            typer.echo("沒有執行", err=True)
            raise typer.Exit(1)
    except KeyboardInterrupt:
        log.warning("建 Batch 在執行前被中斷")
        typer.echo("中斷了，沒有執行", err=True)
        raise typer.Exit(INTERRUPTED_EXIT_CODE) from None

    not_sent = False
    try:
        try:
            plan = plan_llm_batches(
                SystemViewer(),
                database,
                setup,
                weekly_run_id=None,
                clock=_utc_now,
                enqueue=lambda *_: False,  # 這個行程自己送，不排入 worker
                manual=True,
            )
        except Exception as exc:
            log.error("Batch 規劃失敗", error_type=type(exc).__name__)
            _fail(f"規劃失敗（{type(exc).__name__}）；已排好的批次留著，細節見日誌", [])
        for line in _render_plan(plan):
            typer.echo(line)
        if plan.stop is not PlanStop.COMPLETED:
            not_sent = True
        # 只送確認過的：確認前列出的 queued 批次＋這次規劃建的（之後才出現的另行確認）。
        to_send = list(dict.fromkeys([*(row.id for row in confirmed), *plan.submission_ids]))
        with ExitStack() as stack:
            open_provider = _lazy_provider(settings, stack)
            for submission_id in to_send:
                try:
                    result = submit_llm_batch(
                        SystemViewer(),
                        database,
                        open_provider,
                        setup,
                        submission_id,
                        clock=_utc_now,
                    )
                except BatchSubmitUncertainError as exc:
                    log.error("Batch 送出結果不確定", error_type=exc.error_type)
                    typer.echo(
                        f"{submission_id}  needs_attention  不確定有沒有送出（{exc.error_type}），"
                        "停下；用 radar batches resolve 確認",
                        err=True,
                    )
                    raise typer.Exit(1) from None
                typer.echo(_render_submit(submission_id, result))
                if result.outcome in _NOT_SENT:
                    not_sent = True
        others = [
            row.id
            for row in list_llm_batches(SystemViewer(), database, {BatchSubmissionStatus.QUEUED})
            if row.id not in to_send
        ]
        if others:
            typer.echo(
                f"另有 {len(others)} 批 queued 不在這次確認的範圍，沒有送"
                "（worker 會照排程送；要現在送就再跑一次 radar batches create）：",
                err=True,
            )
            for other in others:
                typer.echo(f"  {other}", err=True)
    except ConfigError as exc:
        log.error("設定錯誤，無法送出 Batch", problems=exc.problems)
        _fail("設定錯誤，沒有送出（批次留在 queued）：", exc.problems)
    except KeyboardInterrupt:
        log.warning("建 Batch 被中斷")
        typer.echo(
            "中斷了：已送出的照常處理，沒送的留在 queued（worker 的 sweep 會補送，或再跑一次）",
            err=True,
        )
        raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
    except typer.Exit:
        raise
    except Exception as exc:
        log.error("Batch 送出失敗", error_type=type(exc).__name__)
        _fail(f"送出失敗（{type(exc).__name__}）；細節見日誌", [])
    typer.echo("下一步：radar batches wait（等結果回來並匯入）")
    if not_sent:
        typer.echo("有批次沒送成（延後、失敗或要人工確認），見上面每批的結果", err=True)
        raise typer.Exit(1)


@batches_app.command("poll")
def batches_poll(
    ids: Annotated[
        list[uuid.UUID] | None,
        typer.Option("--id", help="只查這幾批（batch_submission id，可重複）", show_default=False),
    ] = None,
) -> None:
    """查一次 submitted／ended 的批次，匯入已結束的（和 worker 的 poll-batches 同一段程式）。

    每批印一行（批次 id、狀態、供應商狀態、這次匯入的各結果計數、逾期），最後印 needs_attention 數
    與進 ai_done 的每週更新。設定錯、讀不到資料庫、有錯誤、有 needs_attention：退出碼 1。Ctrl-C：
    退出碼 130（做完的項目已經寫好，再跑一次接著做）。
    """
    configure_logging()
    settings, setup = _load_batch_setup("查 Batch")
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        with ExitStack() as stack:
            result = poll_llm_batches(
                SystemViewer(),
                database,
                _lazy_provider(settings, stack),
                setup,
                clock=_utc_now,
                ids=ids or None,
            )
    except ConfigError as exc:
        log.error("設定錯誤，無法查 Batch", problems=exc.problems)
        _fail("設定錯誤，沒有查 Batch：", exc.problems)
    except KeyboardInterrupt:
        log.warning("查 Batch 被中斷")
        typer.echo("中斷了：做完的項目已經寫好，再跑一次接著做（不重複匯入）", err=True)
        raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
    except Exception as exc:
        log.error("查 Batch 失敗", error_type=type(exc).__name__)
        _fail(f"查 Batch 失敗（{type(exc).__name__}）；細節見日誌", [])
    for row in result.rows:
        typer.echo(render_poll_row(row))
    for line in render_poll_summary(result):
        typer.echo(line)
    if not result.healthy:
        raise typer.Exit(1)


@batches_app.command("wait")
def batches_wait(
    ids: Annotated[
        list[uuid.UUID] | None,
        typer.Option("--id", help="只等這幾批（batch_submission id，可重複）", show_default=False),
    ] = None,
    interval_minutes: Annotated[
        int | None,
        typer.Option(
            "--interval-minutes",
            min=1,
            max=1440,
            help="每隔幾分鐘查一次（預設照 config/llm_batches.yaml 的 poll_interval_minutes）",
        ),
    ] = None,
) -> None:
    """一直查到目標批次都結束（imported／failed）或出現 needs_attention 為止，邊查邊匯入。

    目標是開始時 submitted／ended 的批次，或 --id 指定的。每次檢查印一行「已結束 X 批、還在跑
    Y 批」。設定錯、讀不到資料庫、最後一次有錯誤、有 needs_attention（沒有目標時也看全庫的）、
    目標批次裡還有寫結果失敗等接續的項目（llm_result_pending，印論文 ID 與接續方式；沒有目標時看
    全部的批次）：退出碼 1。
    Ctrl-C：退出碼 130，重跑接著收、不重複匯入。
    """
    configure_logging()
    settings, setup = _load_batch_setup("等 Batch")
    interval = interval_minutes or setup.batches.poll_interval_minutes
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        if ids:
            targets = list(dict.fromkeys(ids))
            _check_wait_targets(
                targets, list_llm_batches(SystemViewer(), database, _ALL_BATCH_STATUSES, targets)
            )
        else:
            targets = [
                row.id
                for row in list_llm_batches(
                    SystemViewer(),
                    database,
                    {BatchSubmissionStatus.SUBMITTED, BatchSubmissionStatus.ENDED},
                )
            ]
        if not targets:
            # 上次的 wait 收完了才結束、重跑時沒有目標：上次留下的 needs_attention 與等接續的
            # 結果（全部的批次）都還要看，不能當成成功（確認審查 D3）。
            stuck = list_llm_batches(
                SystemViewer(), database, {BatchSubmissionStatus.NEEDS_ATTENTION}
            )
            left_pending = list_llm_batch_pending_papers(SystemViewer(), database, None)
            if not stuck and not left_pending:
                typer.echo("沒有要等的批次（submitted／ended）")
                return
            if stuck:
                typer.echo("沒有要等的批次（submitted／ended），但有批次要人工確認：", err=True)
                for stuck_row in stuck:
                    typer.echo(f"  {stuck_row.id}", err=True)
                typer.echo(
                    "radar batches resolve <批次 id> --provider-batch-id <ID> 或 --fail", err=True
                )
            if left_pending:
                if not stuck:
                    typer.echo("沒有要等的批次（submitted／ended）", err=True)
                _echo_pending(left_pending)
            raise typer.Exit(1)
        with ExitStack() as stack:
            open_provider = _lazy_provider(settings, stack)
            error_rounds = 0
            while True:
                result = poll_llm_batches(
                    SystemViewer(), database, open_provider, setup, clock=_utc_now, ids=targets
                )
                for row in result.rows:
                    typer.echo(render_poll_row(row))
                error_rounds = error_rounds + 1 if any(row.error for row in result.rows) else 0
                rows = list_llm_batches(SystemViewer(), database, _ALL_BATCH_STATUSES, targets)
                finished = sum(is_finished(row.status) for row in rows)
                attention = [
                    row.id for row in rows if row.status is BatchSubmissionStatus.NEEDS_ATTENTION
                ]
                typer.echo(render_wait_progress(finished, len(rows) - finished))
                if attention or finished == len(rows):
                    break
                if error_rounds >= _WAIT_MAX_ERROR_ROUNDS:
                    typer.echo(
                        f"連續 {error_rounds} 次查詢都有錯誤，先停下；看日誌排除後重跑"
                        " radar batches wait",
                        err=True,
                    )
                    break
                _sleep(interval * 60)
            pending = list_llm_batch_pending_papers(SystemViewer(), database, targets)
    except ConfigError as exc:
        log.error("設定錯誤，無法等 Batch", problems=exc.problems)
        _fail("設定錯誤，沒有等 Batch：", exc.problems)
    except KeyboardInterrupt:
        log.warning("等 Batch 被中斷")
        typer.echo(
            "中斷了：匯入過的已經寫好，重跑 radar batches wait 接著收（不重複匯入）", err=True
        )
        raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
    except typer.Exit:
        raise
    except Exception as exc:
        log.error("等 Batch 失敗", error_type=type(exc).__name__)
        _fail(f"等 Batch 失敗（{type(exc).__name__}）；細節見日誌", [])
    for line in render_poll_summary(result):
        typer.echo(line)
    if attention:
        typer.echo(
            f"有 {len(attention)} 批要人工確認：radar batches resolve <批次 id>"
            " --provider-batch-id <ID> 或 --fail",
            err=True,
        )
    if pending:
        _echo_pending(pending)
    if attention or pending or not result.healthy:
        raise typer.Exit(1)


def _echo_pending(pending: list[uuid.UUID]) -> None:
    """等接續的論文 ID 與接續方式。帳記了、輸出存著，只差寫結果：Batch 不會重排，要用互動接續
    （那幾項不再呼叫模型、不再記帳）。"""
    typer.echo(
        f"有 {len(pending)} 篇論文的結果還沒寫好（寫入失敗、等接續）：用 radar runs trial"
        " <論文 ID>… 接續（會先印預估、要確認；等接續的那幾項用存下的輸出，不再呼叫模型；"
        "同一篇其他沒有結果的工作會照常呼叫）",
        err=True,
    )
    for paper_id in pending:
        typer.echo(f"  {paper_id}", err=True)


def _check_wait_targets(targets: list[uuid.UUID], rows: list[BatchSubmission]) -> None:
    """`--id` 指定的批次要存在，而且已經送出（queued／submitting 不會自己往下走，等不到）。"""
    found = {row.id: row.status for row in rows}
    unknown = [str(target) for target in targets if target not in found]
    if unknown:
        _fail("找不到這些批次，沒有開始等：", unknown)
    unsent = [
        f"{target}（{found[target].value}）"
        for target in targets
        if found[target] in (BatchSubmissionStatus.QUEUED, BatchSubmissionStatus.SUBMITTING)
    ]
    if unsent:
        _fail(
            "wait 只等已經送出的批次，這些還沒送出（先跑 radar batches create 或等 worker 送）：",
            unsent,
        )


@batches_app.command("resolve")
def batches_resolve(
    batch_submission_id: Annotated[
        uuid.UUID, typer.Argument(help="要確認的批次（batch_submission id）", show_default=False)
    ],
    provider_batch_id: Annotated[
        str | None,
        typer.Option(
            "--provider-batch-id",
            help="在 Anthropic Console 查到的 batch ID（msgbatch_…）：記下來、改回 submitted",
            show_default=False,
        ),
    ] = None,
    fail: Annotated[
        bool,
        typer.Option(
            "--fail",
            help="標失敗：這批還在跑的 run 改成失敗，下一次 radar batches create 會再送"
            "（每週規劃開啟後也會）",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="--fail 不問確認")] = False,
) -> None:
    """人工確認一個 needs_attention 的批次（不呼叫供應商）。

    --provider-batch-id 與 --fail 擇一。--fail 先印會放掉幾筆 run 並提醒先到 Console 取消，確認後
    （--yes 不問）才改。兩個都給或都沒給、batch ID 格式不對：退出碼 2。設定錯、讀不到資料庫、找不到
    批次、批次不是 needs_attention、batch ID 已經是別批的、沒有確認：退出碼 1。
    """
    configure_logging()
    if (provider_batch_id is None) == (not fail):
        typer.echo("參數看不懂，沒有執行：--provider-batch-id 與 --fail 要剛好給一個", err=True)
        raise typer.Exit(2)
    if provider_batch_id is not None and not _BATCH_ID.fullmatch(provider_batch_id):
        typer.echo(
            "參數看不懂，沒有執行：batch ID 只能有英數字、底線與連字號（例如 msgbatch_…）",
            err=True,
        )
        raise typer.Exit(2)
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("設定錯誤，無法確認 Batch", problems=exc.problems)
        _fail("設定錯誤，沒有執行：", exc.problems)
    try:
        try:
            database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
            found = list_llm_batches(
                SystemViewer(), database, _ALL_BATCH_STATUSES, [batch_submission_id]
            )
            running = count_llm_batch_running_runs(SystemViewer(), database, batch_submission_id)
        except Exception as exc:
            log.error("讀取批次失敗", error_type=type(exc).__name__)
            _fail(f"讀取批次失敗（{type(exc).__name__}）；細節見日誌", [])
        if not found:
            _fail(f"找不到批次 {batch_submission_id}", [])
        if found[0].status is not BatchSubmissionStatus.NEEDS_ATTENTION:
            _fail(f"這批是 {found[0].status.value}，不是 needs_attention，沒有改", [])
        if fail:
            typer.echo(f"會把這批還在跑的 {running:,} 筆 run 改成失敗（batch_resolved_failed）")
            typer.echo("供應商那邊若真的有這批，先到 Anthropic Console 取消（結果回來也不會匯入）")
            if not yes and not _confirm("要標失敗嗎？"):
                typer.echo("沒有執行", err=True)
                raise typer.Exit(1)
    except KeyboardInterrupt:
        typer.echo("中斷了，沒有執行", err=True)
        raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
    try:
        result = resolve_llm_batch(
            SystemViewer(),
            database,
            batch_submission_id,
            provider_batch_id=provider_batch_id,
            fail=fail,
        )
    except Exception as exc:
        log.error("確認 Batch 失敗", error_type=type(exc).__name__)
        _fail(f"確認失敗（{type(exc).__name__}）；細節見日誌", [])
    if result.outcome is ResolveOutcome.RESUMED:
        typer.echo("已改回 submitted；下一次 poll 會去查這個 batch ID")
    elif result.outcome is ResolveOutcome.FAILED:
        typer.echo(
            f"已標失敗；{result.released_runs:,} 筆 run 改成失敗，"
            "下一次 radar batches create 會再送"
        )
    elif result.outcome is ResolveOutcome.ID_IN_USE:
        _fail("這個 batch ID 已經記在別的批次，沒有改", [])
    else:
        _fail(f"這批現在是 {result.status.value}，不是 needs_attention，沒有改", [])


@runs_app.command("report")
def runs_report(
    batch: Annotated[
        list[uuid.UUID] | None,
        typer.Option("--batch", help="這幾批的 run（batch_submission id，可重複）"),
    ] = None,
    ids_file: Annotated[
        Path | None,
        typer.Option(
            "--ids-file",
            exists=True,
            dir_okay=False,
            readable=True,
            help="論文 ID 清單：每行一個 UUID，空行與 # 開頭的行略過",
        ),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="這個時間以後建的 run（ISO 8601；沒寫時區當台北時間）"),
    ] = None,
) -> None:
    """拒收報表：範圍內每篇論文每個 AI 工作最新那一筆 run 的結果（只讀）。

    依工作印篇數、各結果（有效、已取代、拒收、暫時失敗、還在跑），以及拒收、暫時失敗依原因代碼的
    篇數。--batch、--ids-file、--since 至少給一個（給幾個就同時滿足幾個），都沒給或看不懂：退出碼
    2。設定錯、讀不到資料庫：退出碼 1。
    """
    configure_logging()
    try:
        paper_ids = None if ids_file is None else parse_ids_file(ids_file.read_text("utf-8-sig"))
        since_at = None if since is None else parse_since(since)
        scope = RunScope(tuple(batch or ()), paper_ids, since_at)
    except ValueError as exc:
        typer.echo(f"參數看不懂，沒有執行：{exc}", err=True)
        raise typer.Exit(2) from None
    except OSError as exc:
        typer.echo(f"讀不到 --ids-file（{type(exc).__name__}），沒有執行", err=True)
        raise typer.Exit(2) from None
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("設定錯誤，無法印報表", problems=exc.problems)
        _fail("設定錯誤，沒有執行：", exc.problems)
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        rows = get_run_report(SystemViewer(), database, scope)
    except Exception as exc:
        log.error("讀取報表失敗", error_type=type(exc).__name__)
        _fail(f"讀取報表失敗（{type(exc).__name__}）；細節見日誌", [])
    for line in render_run_report(rows):
        typer.echo(line)


# --- 管理者的 AI 指令（Story 2.12、ADR-0044） ------------------------------------------------


def _parse_paper_target(target: str) -> uuid.UUID | str:
    """一個論文 ID 或 DOI；看不懂印出來、退出碼 2（不碰資料庫）。"""
    try:
        return parse_trial_targets([target]).targets[0]
    except TrialArgumentError as exc:
        typer.echo(f"參數看不懂，沒有執行：{exc}", err=True)
        raise typer.Exit(2) from None


def _load_trial_setup(action: str) -> tuple[Settings, TrialSetup]:
    """讀互動呼叫要的設定（和 `radar runs trial` 一樣）；設定錯就印出來、退出碼 1。"""
    try:
        settings = load_settings()
        setup = TrialSetup(
            gateway=build_gateway_config(settings, load_models_config()),
            budget=load_budget_config(),
            zh_summary_rules=load_zh_summary_rules(),
            taxonomy=load_taxonomy(),
            context_values=load_context_values(),
        )
    except ConfigError as exc:
        log.error(f"設定錯誤，無法{action}", problems=exc.problems)
        _fail(f"設定錯誤，沒有{action}：", exc.problems)
    return settings, setup


@runs_app.command("show")
def runs_show(
    target: Annotated[str, typer.Argument(help="論文 ID（UUID）或 DOI", show_default=False)],
) -> None:
    """查一篇論文的 AI 結果（只讀）：中文重點、分類、研究模型三個區塊的狀態、有效值與來歷
    （模型、prompt 版本、產生時間、輸入種類、人工核對），以及歷次 run（工作、結果、原因代碼、模型、
    prompt 版本、輸入種類、摘要來源、時間；評估另外標出）。

    ID／DOI 看不懂：退出碼 2。找不到論文、設定錯、讀不到資料庫：退出碼 1。Ctrl-C：退出碼 130。
    錯誤只印類別。
    """
    configure_logging()
    paper = _parse_paper_target(target)
    settings, setup = _load_trial_setup("查詢")
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        report = get_paper_ai_report(SystemViewer(), database, setup, paper, now=_utc_now())
    except KeyboardInterrupt:
        typer.echo("中斷了（沒有寫任何資料）", err=True)
        raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("查詢 AI 結果失敗", error_type=type(exc).__name__)
        _fail(f"查詢失敗（{type(exc).__name__}）；細節見日誌", [])
    if report is None:
        _fail(f"找不到這篇論文：{paper}", [])
    for line in render_paper_ai_report(report):
        typer.echo(line)


@runs_app.command("retry")
def runs_retry(
    target: Annotated[str, typer.Argument(help="論文 ID（UUID）或 DOI", show_default=False)],
    tasks: Annotated[
        list[LlmTask] | None,
        typer.Option(
            "--task",
            help="只重跑這幾個工作（zh_summary、classify、extract_model，可重複）；不給＝三個裡"
            "可以重跑的",
            show_default=False,
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="不問確認，直接重跑")] = False,
) -> None:
    """重跑一篇論文被拒收的 AI 結果（只有目前輸入最新的結果是被拒收、沒有有效結果的工作）。

    先印每個工作能不能重跑（不能的印原因）、含重試的最壞費用與本月預算，確認後（--yes 不問）每項經
    同一個閘門互動呼叫一次，結果照常寫（有效或再一次被拒收）。確認後輸入變了的標 input_changed、不
    呼叫；同時有另一個重跑在做的標 skipped、不再付錢。

    ID／DOI 或 --task 看不懂：退出碼 2。找不到論文、沒有可以重跑的、沒有確認、設定錯、讀不到資料庫、
    有暫時性失敗、出錯、預算不准或輸入在確認後變了的項目：退出碼 1。Ctrl-C：退出碼 130。錯誤只印
    類別或狀態碼，不印金鑰與連線字串。
    """
    configure_logging()
    paper = _parse_paper_target(target)
    wanted = tuple(dict.fromkeys(tasks or ADMIN_TASKS))
    if any(task not in ADMIN_TASKS for task in wanted):
        typer.echo(
            "參數看不懂，沒有執行：--task 只能是 zh_summary、classify、extract_model", err=True
        )
        raise typer.Exit(2)
    settings, setup = _load_trial_setup("重跑")
    try:
        provider_context = build_llm_provider(settings)
    except ConfigError as exc:
        log.error("設定錯誤，無法重跑", problems=exc.problems)
        _fail("設定錯誤，沒有重跑：", exc.problems)
    with provider_context as provider:
        try:
            try:
                database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
                selection = resolve_trial_papers(SystemViewer(), database, TrialTargets((paper,)))
                plan = plan_retry(
                    SystemViewer(),
                    database,
                    setup,
                    selection.papers[0].paper_id,
                    wanted,
                    attempts=provider.attempts,
                )
                status = get_budget_status(SystemViewer(), database, setup.budget, now=_utc_now())
            except TrialPapersNotFoundError:
                _fail(f"找不到這篇論文，沒有重跑：{paper}", [])
            except Exception as exc:
                log.error("重跑的預估失敗", error_type=type(exc).__name__)
                _fail(f"預估失敗（{type(exc).__name__}），沒有重跑；細節見日誌", [])
            for line in render_retry_plan(plan, status):
                typer.echo(line)
            if not plan.retryable:
                _fail("沒有可以重跑的工作，沒有呼叫模型", [])
            if not yes and not _confirm("要重跑嗎？"):
                typer.echo("沒有執行", err=True)
                raise typer.Exit(1)
        except KeyboardInterrupt:
            log.warning("重跑在執行前被中斷")
            typer.echo("中斷了，沒有執行", err=True)
            raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
        try:
            report = run_retry(SystemViewer(), database, provider, setup, plan, clock=_utc_now)
        except KeyboardInterrupt:
            log.warning("重跑被中斷")
            typer.echo(
                "中斷了：做完的已經寫好；中斷的那一項再跑一次 radar runs retry 即可（有存下的輸出"
                "會接續、不重付）",
                err=True,
            )
            raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
        except Exception as exc:
            log.error("重跑失敗", error_type=type(exc).__name__)
            _fail(f"重跑失敗（{type(exc).__name__}）；做完的已經寫好，細節見日誌", [])
    for line in render_retry_report(report):
        typer.echo(line)
    if report.exit_code:
        raise typer.Exit(report.exit_code)


@runs_app.command("remove-by-source")
def runs_remove_by_source(
    source: Annotated[
        AbstractSource,
        typer.Argument(
            help="摘要來源（openalex、semantic_scholar、uploaded_pdf）", show_default=False
        ),
    ],
    execute: Annotated[
        bool, typer.Option("--execute", help="真的刪（不給就只印會刪幾筆，資料庫一筆都不動）")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="--execute 不問確認")] = False,
) -> None:
    """依摘要來源移除衍生的 AI 產出（摘要來源授權改變時用；FR-11）。

    不加 --execute：只印各表各狀態會刪幾筆、細項幾筆、還在跑的 run 數、目前摘要仍是這個來源的
    論文數、來源不明的表頭數，資料庫一筆都不動。加 --execute：確認後（--yes 不問）在一個交易裡
    直接刪掉送出時用了這個來源摘要的表頭與細項（不複製），寫一筆稽核（只記來源與計數）。還有這個
    來源的 run 在跑、或有論文的目前摘要仍是這個來源時不刪：先停用來源、移除摘要，再跑這個指令。

    來源看不懂：退出碼 2。設定錯、讀不到資料庫、被擋下、沒有確認：退出碼 1。Ctrl-C：退出碼 130。
    """
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("設定錯誤，無法依來源移除", problems=exc.problems)
        _fail("設定錯誤，沒有執行：", exc.problems)
    try:
        try:
            database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
            preview = preview_source_removal(SystemViewer(), database, source)
        except Exception as exc:
            log.error("讀取要移除的 AI 產出失敗", error_type=type(exc).__name__)
            _fail(f"讀取失敗（{type(exc).__name__}）；細節見日誌", [])
        for line in render_source_removal_preview(preview):
            typer.echo(line)
        if not execute:
            typer.echo("只印不做：加 --execute 才會刪")
            return
        if preview.blocks:
            for line in render_removal_blocks(source, preview.blocks):
                typer.echo(line, err=True)
            raise typer.Exit(1)
        if not yes and not _confirm("要刪除嗎？（不能復原）"):
            typer.echo("沒有執行", err=True)
            raise typer.Exit(1)
    except KeyboardInterrupt:
        typer.echo("中斷了，沒有執行", err=True)
        raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
    try:
        result = remove_source_ai_outputs(SystemViewer(), database, source)
    except KeyboardInterrupt:
        typer.echo("中斷了：整個交易退回，一筆都沒有刪", err=True)
        raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
    except Exception as exc:
        log.error("依來源移除失敗", error_type=type(exc).__name__)
        _fail(f"移除失敗（{type(exc).__name__}），整個交易退回；細節見日誌", [])
    if result.blocks:
        for line in render_removal_blocks(source, result.blocks):
            typer.echo(line, err=True)
        raise typer.Exit(1)
    for line in render_source_removal_result(result):
        typer.echo(line)


@runs_app.command("estimate-revision")
def runs_estimate_revision(
    taxonomy: Annotated[
        bool, typer.Option("--taxonomy", help="分類表要改版：估分類（classify）會重跑多少")
    ] = False,
    context_values: Annotated[
        bool,
        typer.Option(
            "--context-values", help="情境值清單要改版：估研究模型（extract_model）會重跑多少"
        ),
    ] = False,
) -> None:
    """改分類表或情境值清單之前，估下一次 Batch 規劃會重跑幾篇、花多少（只讀）。

    印各工作「有現行 AI 結果、改版後會重跑」的篇數與 Batch 價保守上限、「還沒有結果」的篇數，以及
    config/llm_batches.yaml 的 revision_rerun_max_usd。兩個旗標可以一起給。上限只看分類表與情境值
    清單的版本：prompt 升版或換模型也會讓全部重跑，但不受這個上限管。

    兩個旗標都沒給：退出碼 2。設定錯、讀不到資料庫、超過上限（改版後規劃會整次不排）：退出碼 1。
    Ctrl-C：退出碼 130。
    """
    configure_logging()
    tasks = tuple(
        task
        for task, wanted in zip(REVISION_TASKS, (taxonomy, context_values), strict=True)
        if wanted
    )
    if not tasks:
        typer.echo("參數看不懂，沒有執行：--taxonomy、--context-values 至少給一個", err=True)
        raise typer.Exit(2)
    settings, setup = _load_batch_setup("估改版")
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        estimate = estimate_revision_reruns(
            SystemViewer(), database, setup, tasks=tasks, assume_changed=True
        )
    except KeyboardInterrupt:
        typer.echo("中斷了（沒有寫任何資料）", err=True)
        raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
    except Exception as exc:
        log.error("改版預估失敗", error_type=type(exc).__name__)
        _fail(f"預估失敗（{type(exc).__name__}）；細節見日誌", [])
    for line in render_revision_estimate(estimate):
        typer.echo(line)
    if estimate.exceeds_cap:
        raise typer.Exit(1)


def _fail(headline: str, problems: list[str]) -> NoReturn:
    typer.echo(headline, err=True)
    for problem in problems:
        typer.echo(f"- {problem}", err=True)
    raise typer.Exit(1)


def main() -> None:
    app()
