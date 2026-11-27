"""`radar eval`：搜尋的評估題組（Story 4.4、FR-26、FR-27、FR-41）。

- `import-queries`：從標準輸入（預設）或 `--file` 讀題目檔（CSV、UTF-8；格式見
  `domain/evaluation.py`），整份取代資料庫裡的評估題目。題目檔有任何問題就整份不收、什麼都不寫（列出
  每個問題的列號與原因，不印格子的內容）；DOI 對不到論文的列印出來並跳過，不中斷。經 SSH 串流匯入時
  主機上不留檔（`docs/deploy/migrate-mac-to-trial.md`）。
- `search`：跑全部題目（`tracked` 範圍、系統的 viewer），印每題前 10 名有沒有命中、命中名次，以及
  「只用全文檢索腿／只用向量腿／融合」三組各自的命中率、MRR、「找不到相關的論文」的題數；可以帶多個
  `--min-similarity` 一次比多個相似度下限（每題的查詢向量只算一次，多個下限共用；Workers AI 的用量是
  `search` 的題數次）。印出相似度下限、向量模型、快照編號、題數，結果存一筆 `eval_run`。要
  Cloudflare 的金鑰（`RADAR_CLOUDFLARE_ACCOUNT_ID`、`RADAR_CLOUDFLARE_API_TOKEN`）。

誰能跑：只限能登入主機、拿得到資料庫設定的維運者（Kevin）。評估不是 pytest，不在 CI 用真的服務跑
（Conventions「評估」）。題目、查詢內容不寫進日誌。
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import structlog
import typer

from radar.config import (
    ConfigError,
    database_pool_size,
    load_embedding_config,
    load_retrieval_config,
    load_settings,
)
from radar.domain.admin_runs import terminal_text
from radar.domain.evaluation import (
    MAX_FILE_BYTES,
    MAX_PROBLEMS_SHOWN,
    EvalQueriesFileError,
    parse_eval_queries_csv,
    parse_min_similarities,
    render_eval_import,
    render_search_eval,
)
from radar.domain.retrieval import SearchEmbeddingUnavailableError, SearchQuotaExhaustedError
from radar.domain.viewer import SystemViewer
from radar.entrypoints.bootstrap import build_database, build_embedding_provider
from radar.entrypoints.logging_config import configure_logging
from radar.services.embeddings import EmbeddingModelMismatchError
from radar.services.evaluation import (
    EvalQueriesMissingError,
    import_eval_queries,
    run_search_eval,
)
from radar.services.retrieval import RetrievalSetup

log = structlog.get_logger(__name__)

eval_app = typer.Typer(help="搜尋的評估題組（eval_query、eval_run）。", no_args_is_help=True)


def _utc_now() -> datetime:
    """評估的時鐘（額度的 UTC 日期、記帳與 `eval_run` 的時間；測試換掉它）。"""
    return datetime.now(UTC)


def _fail(headline: str, problems: list[str] | None = None) -> NoReturn:
    typer.echo(headline, err=True)
    for problem in problems or []:
        typer.echo(f"- {problem}", err=True)
    raise typer.Exit(1)


def _read_input(path: Path | None) -> bytes:
    """讀題目檔（多讀一個位元組，超過上限時 domain 會擋）。"""
    if path is not None:
        try:
            with path.open("rb") as handle:
                return handle.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            _fail(f"讀不到題目檔（{type(exc).__name__}）")
    if sys.stdin is not None and sys.stdin.isatty():
        _fail("沒有給題目檔：用 --file <檔案>，或從標準輸入串流進來（< 題目.csv）")
    return typer.get_binary_stream("stdin").read(MAX_FILE_BYTES + 1)


@eval_app.command("import-queries")
def import_queries(
    file: Annotated[
        Path | None,
        typer.Option("--file", help="題目檔（CSV、UTF-8）；不給就從標準輸入讀"),
    ] = None,
) -> None:
    """匯入評估題目（整份取代資料庫裡原本的題目）。

    欄位：set（search／similar）、query_id、query（search 是查詢，similar 是起點論文的 DOI）、
    relevant_doi、note（選填）；一列一個「查詢＋一篇正解」，每題 1～5 篇正解。題目檔有問題：列出
    問題、什麼都不寫、退出碼 1。DOI 對不到論文的列印出來並跳過。
    """
    configure_logging()
    data = _read_input(file)
    try:
        settings = load_settings()
        retrieval = load_retrieval_config()
    except ConfigError as exc:
        log.error("設定錯誤，無法匯入評估題目", problems=exc.problems)
        _fail("設定錯誤，沒有匯入：", exc.problems)
    try:
        parsed = parse_eval_queries_csv(data, query_max_length=retrieval.query_max_length)
    except EvalQueriesFileError as exc:
        shown = list(exc.problems[:MAX_PROBLEMS_SHOWN])
        if len(exc.problems) > MAX_PROBLEMS_SHOWN:
            shown.append(f"……還有 {len(exc.problems) - MAX_PROBLEMS_SHOWN} 個問題沒有列出")
        _fail(f"題目檔有 {len(exc.problems)} 個問題，什麼都沒有寫：", shown)
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        result = import_eval_queries(SystemViewer(), database, parsed)
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("匯入評估題目失敗", error_type=type(exc).__name__)
        _fail(f"匯入評估題目失敗（{type(exc).__name__}）；細節見日誌")
    for line in render_eval_import(result, label=terminal_text):
        typer.echo(line)


@eval_app.command("search")
def search(
    min_similarity: Annotated[
        list[str] | None,
        typer.Option(
            "--min-similarity",
            help="相似度下限（0 到 1，可以給多次一起比）；不給就用 config/retrieval.yaml 的值",
        ),
    ] = None,
) -> None:
    """跑全部評估題目，印命中率與 MRR（三組分開），存一筆 eval_run。

    還沒有題目、設定錯誤、缺 Cloudflare 金鑰、Workers AI 額度用完或連不上：退出碼 1，不存
    eval_run。錯誤只印類別或代碼，不印金鑰、帳號或連線字串。
    """
    configure_logging()
    try:
        settings = load_settings()
        retrieval = load_retrieval_config()
        embedding = load_embedding_config()
    except ConfigError as exc:
        log.error("設定錯誤，無法評估", problems=exc.problems)
        _fail("設定錯誤，沒有評估：", exc.problems)
    try:
        floors = parse_min_similarities(min_similarity or [str(retrieval.vector_min_similarity)])
    except ValueError as exc:
        _fail(str(exc))
    try:
        database = build_database(settings, pool_size=database_pool_size(settings, "cli"))
        with build_embedding_provider(settings, embedding) as provider:
            setup = RetrievalSetup(
                embedding=embedding,
                retrieval=retrieval,
                cloud_policy=settings.cloud_policy,
                provider=provider,
                clock=_utc_now,
            )
            report = run_search_eval(SystemViewer(), database, setup, min_similarities=floors)
    except ConfigError as exc:
        log.error("設定錯誤，無法評估", problems=exc.problems)
        _fail("設定錯誤，沒有評估：", exc.problems)
    except EvalQueriesMissingError:
        _fail("還沒有評估題目：先用 radar eval import-queries 匯入")
    except SearchQuotaExhaustedError:
        _fail("Workers AI 今天的額度用完了，沒有存這次的評估（明天再跑）")
    except SearchEmbeddingUnavailableError:
        _fail("查詢沒能轉成向量（Workers AI 連不上或回了錯），沒有存這次的評估；細節見日誌")
    except EmbeddingModelMismatchError as exc:
        _fail("向量模型的設定對不上，沒有送任何請求：", [str(exc)])
    except Exception as exc:
        # 例外訊息可能含主機與帳號，只記類別名稱。
        log.error("評估失敗", error_type=type(exc).__name__)
        _fail(f"評估失敗（{type(exc).__name__}）；細節見日誌")
    for line in render_search_eval(report, label=terminal_text):
        typer.echo(line)
