"""`radar` 指令：專案唯一的 CLI 入口（NFR-11）。typer 只能出現在這個套件。"""

import itertools
import uuid
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
    load_migration_settings,
    load_models_config,
    load_settings,
    load_taxonomy,
    load_zh_summary_rules,
)
from radar.domain.budget import render_cost_report
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
from radar.services.migrations import migrate_database
from radar.services.reparse import (
    AdvisoryLockLostError,
    ReparseBusyError,
    WeeklyUpdateInProgressError,
    reparse_papers,
)
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


def _fail(headline: str, problems: list[str]) -> NoReturn:
    typer.echo(headline, err=True)
    for problem in problems:
        typer.echo(f"- {problem}", err=True)
    raise typer.Exit(1)


def main() -> None:
    app()
