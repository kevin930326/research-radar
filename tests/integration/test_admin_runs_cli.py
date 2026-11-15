"""`radar runs show／retry／remove-by-source／estimate-revision` 與改版上限下的 `radar batches
create` 對真的 PostgreSQL（`CliRunner`＋環境變數；Story 2.12、ADR-0044）：退出碼 0／1／2／130、
印出的內容、沒確認或被擋下時一筆都不動。

模型用依 schema 回覆的替身（換掉 `build_llm_provider`；假回覆不是真的錄的），不連網路；環境變數
只用測試值；論文與回覆都是自己編的。
"""

import logging
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner, Result

import radar.entrypoints.cli as cli
from radar.domain.ai_outputs import InputKind, LlmTask
from radar.domain.eval_split import EvalSplit
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.services.llm_batches import llm_job
from radar.services.llm_gateway import GatewayOutcome, run_llm_job
from tests.batch_support import NOW
from tests.database import ROOT, TestDatabase, migrate
from tests.integration.test_trial_runs_cli import FAKE_KEY, Models
from tests.llm_support import execute, table_counts
from tests.trial_support import (
    SchemaReplyProvider,
    banking_paper,
    doi_in,
    good_replies,
    reply,
    trial_setup,
)

runner = CliRunner()
SYSTEM = SystemViewer()
# 不是 JSON：拒收。假回覆，不是真的錄的。
BAD = reply("研究問題：行動銀行的採用")


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def env(db: TestDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", config_dir)
    monkeypatch.setenv("DATABASE_URL", db.app_url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("RADAR_ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    return config_dir


@pytest.fixture
def models(env: Path, monkeypatch: pytest.MonkeyPatch) -> Models:
    return Models(monkeypatch).use(good_replies())


def _invoke(*args: str, stdin: str | None = None) -> Result:
    """執行指令；結束後把 root logger 的 handler 換回原本的（見 test_costs_cli）。"""
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        return runner.invoke(cli.app, list(args), input=stdin)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)


def _stdout(result: Result) -> str:
    # 日誌是 JSON（以 { 開頭）；指令自己印的不是。
    return "\n".join(line for line in result.stdout.splitlines() if not line.startswith("{"))


def _gateway(
    database: Database, replies: dict[LlmTask, Any], paper_id: uuid.UUID, task: LlmTask
) -> GatewayOutcome:
    setup = trial_setup()
    return run_llm_job(
        SYSTEM,
        database,
        SchemaReplyProvider(replies),
        setup.gateway,
        llm_job(setup, paper_id, task, InputKind.ABSTRACT),
        budget_allows=lambda _task: True,
    ).outcome


def _rejected_paper(database: Database) -> tuple[uuid.UUID, str]:
    doi = doi_in(EvalSplit.TUNING, "1")
    paper_id = banking_paper(database, doi, 1)
    assert _gateway(database, {LlmTask.CLASSIFY: BAD}, paper_id, LlmTask.CLASSIFY) is (
        GatewayOutcome.REJECTED
    )
    return paper_id, doi


def _all_rows(db: TestDatabase) -> dict[str, int]:
    tables = (
        "derivation_run",
        "llm_call",
        "zh_summary",
        "classification",
        "classification_label",
        "research_model",
        "construct_mention",
        "hypothesis",
        "audit_event",
        "batch_submission",
        "batch_item",
    )
    return {table: int(execute(db, f"SELECT count(*) FROM {table}")[0][0]) for table in tables}


# --- show -------------------------------------------------------------------------------------


def test_show_prints_blocks_and_history(db: TestDatabase, database: Database, env: Path) -> None:
    paper_id, doi = _rejected_paper(database)
    assert _gateway(database, good_replies(), paper_id, LlmTask.ZH_SUMMARY) is (
        GatewayOutcome.ACTIVE
    )
    before = _all_rows(db)

    result = _invoke("runs", "show", doi)

    assert result.exit_code == 0, result.output
    shown = _stdout(result)
    assert f"論文 {paper_id}（{doi}）" in shown
    assert "中文重點：ready（有結果）" in shown
    assert "分類：failed" in shown
    assert "llm_malformed_response" in shown
    assert "openalex" in shown
    assert _all_rows(db) == before


@pytest.mark.parametrize(("target", "code"), [("not a doi", 2), ("10.1/nowhere", 1)])
def test_show_bad_or_missing_target(
    db: TestDatabase, database: Database, env: Path, target: str, code: int
) -> None:
    result = _invoke("runs", "show", target)

    assert result.exit_code == code
    if code == 1:
        assert "找不到這篇論文" in result.stderr
    else:
        assert "參數看不懂" in result.stderr


# --- retry ------------------------------------------------------------------------------------


def test_retry_confirmed_runs_the_rejected_task(
    db: TestDatabase, database: Database, models: Models
) -> None:
    _, doi = _rejected_paper(database)

    result = _invoke("runs", "retry", doi, stdin="y\n")

    assert result.exit_code == 0, result.output
    shown = _stdout(result)
    assert "classify：會重跑（被拒收：llm_malformed_response" in shown
    assert "zh_summary：不能重跑" in shown
    assert "預估：重跑 1 項" in shown
    assert "classify：active" in shown
    assert models.calls == 1
    assert execute(db, "SELECT status FROM classification ORDER BY created_at, id") == [
        ("rejected",),
        ("active",),
    ]


@pytest.mark.parametrize("answer", ["n\n", ""])
def test_retry_without_confirmation_does_nothing(
    db: TestDatabase, database: Database, models: Models, answer: str
) -> None:
    _, doi = _rejected_paper(database)
    before = table_counts(db)

    result = _invoke("runs", "retry", doi, "--task", "classify", stdin=answer)

    assert result.exit_code == 1
    assert "沒有執行" in result.stderr
    assert models.calls == 0
    assert table_counts(db) == before


def test_retry_with_nothing_to_retry_exits_1(
    db: TestDatabase, database: Database, models: Models
) -> None:
    doi = doi_in(EvalSplit.TUNING, "1")
    banking_paper(database, doi, 1)

    result = _invoke("runs", "retry", doi, "--yes")

    assert result.exit_code == 1
    assert "沒有可以重跑的工作" in result.stderr
    assert models.calls == 0


def test_retry_rejected_again_still_exits_0(
    db: TestDatabase, database: Database, models: Models
) -> None:
    _, doi = _rejected_paper(database)
    models.use({LlmTask.CLASSIFY: BAD})

    result = _invoke("runs", "retry", doi, "--task", "classify", "--yes")

    assert result.exit_code == 0, result.output
    assert "classify：rejected（llm_malformed_response）" in _stdout(result)


@pytest.mark.parametrize(
    "args",
    [
        ("not a doi",),
        ("10.1/x", "--task", "merge_judge"),
        ("10.1/x", "--task", "nonsense"),
    ],
)
def test_retry_bad_arguments_exit_2(
    db: TestDatabase, models: Models, args: tuple[str, ...]
) -> None:
    result = _invoke("runs", "retry", *args, "--yes")

    assert result.exit_code == 2
    assert models.calls == 0


def test_retry_missing_paper_exits_1(db: TestDatabase, models: Models) -> None:
    result = _invoke("runs", "retry", str(uuid.uuid4()), "--yes")

    assert result.exit_code == 1
    assert "找不到這篇論文" in result.stderr


def _abort_from_ctrl_c(*_args: object, **_kwargs: object) -> Any:
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        raise typer.Abort from None


def test_retry_ctrl_c_at_the_prompt_exits_130(
    db: TestDatabase, database: Database, models: Models, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, doi = _rejected_paper(database)
    before = table_counts(db)
    monkeypatch.setattr(typer, "confirm", _abort_from_ctrl_c)

    result = _invoke("runs", "retry", doi)

    assert result.exit_code == 130
    assert "中斷了，沒有執行" in result.stderr
    assert table_counts(db) == before


def test_retry_ctrl_c_while_running_exits_130_and_can_run_again(
    db: TestDatabase, database: Database, models: Models
) -> None:
    _, doi = _rejected_paper(database)
    models.use({LlmTask.CLASSIFY: KeyboardInterrupt()})

    interrupted = _invoke("runs", "retry", doi, "--task", "classify", "--yes")

    assert interrupted.exit_code == 130
    assert "再跑一次 radar runs retry" in interrupted.stderr
    models.use(good_replies())
    again = _invoke("runs", "retry", doi, "--task", "classify", "--yes")
    assert again.exit_code == 0, again.output
    assert "classify：active" in _stdout(again)


# --- remove-by-source -------------------------------------------------------------------------


def test_remove_by_source_without_execute_only_prints(
    db: TestDatabase, database: Database, env: Path
) -> None:
    _rejected_paper(database)
    before = _all_rows(db)

    result = _invoke("runs", "remove-by-source", "openalex")

    assert result.exit_code == 0, result.output
    shown = _stdout(result)
    assert "摘要來源 openalex 衍生的 AI 產出" in shown
    assert "會刪：表頭 1 列" in shown
    assert "目前摘要仍是這個來源的論文：1" in shown
    assert "只印不做" in shown
    assert _all_rows(db) == before


def test_remove_by_source_execute_is_blocked_by_current_abstracts(
    db: TestDatabase, database: Database, env: Path
) -> None:
    _rejected_paper(database)
    before = _all_rows(db)

    result = _invoke("runs", "remove-by-source", "openalex", "--execute", "--yes")

    assert result.exit_code == 1
    assert "沒有刪任何一列" in result.stderr
    assert "先停用這個來源" in result.stderr
    assert _all_rows(db) == before


def test_remove_by_source_execute_after_confirmation(
    db: TestDatabase, database: Database, env: Path
) -> None:
    _rejected_paper(database)
    execute(db, "DELETE FROM paper_abstract WHERE source = 'openalex'")

    declined = _invoke("runs", "remove-by-source", "openalex", "--execute", stdin="n\n")
    assert declined.exit_code == 1
    assert execute(db, "SELECT count(*) FROM classification") == [(1,)]

    result = _invoke("runs", "remove-by-source", "openalex", "--execute", stdin="y\n")

    assert result.exit_code == 0, result.output
    shown = _stdout(result)
    assert "已刪除摘要來源 openalex 衍生的 AI 產出：zh_summary 0、classification 1" in shown
    assert "稽核事件：" in shown
    assert execute(db, "SELECT count(*) FROM classification") == [(0,)]
    assert execute(db, "SELECT count(*) FROM audit_event") == [(1,)]
    again = _invoke("runs", "remove-by-source", "openalex", "--execute", "--yes")
    assert again.exit_code == 0
    assert "沒有可刪的 AI 產出" in _stdout(again)
    assert execute(db, "SELECT count(*) FROM audit_event") == [(1,)]


@pytest.mark.parametrize("source", ["crossref", "OPENALEX", ""])
def test_remove_by_source_bad_source_exits_2(db: TestDatabase, env: Path, source: str) -> None:
    result = _invoke("runs", "remove-by-source", source)

    assert result.exit_code == 2


def test_remove_by_source_ctrl_c_at_the_prompt_exits_130(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rejected_paper(database)
    execute(db, "DELETE FROM paper_abstract WHERE source = 'openalex'")
    before = _all_rows(db)
    monkeypatch.setattr(typer, "confirm", _abort_from_ctrl_c)

    result = _invoke("runs", "remove-by-source", "openalex", "--execute")

    assert result.exit_code == 130
    assert _all_rows(db) == before


# --- estimate-revision 與規劃上限 -------------------------------------------------------------


def _done(database: Database) -> None:
    paper_id = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    for task in (LlmTask.CLASSIFY, LlmTask.EXTRACT_MODEL):
        assert _gateway(database, good_replies(), paper_id, task) is GatewayOutcome.ACTIVE


def _set_cap(config_dir: Path, cap: str) -> None:
    path = config_dir / "llm_batches.yaml"
    text = path.read_text(encoding="utf-8").replace(
        'revision_rerun_max_usd: "5"', f'revision_rerun_max_usd: "{cap}"'
    )
    assert f'revision_rerun_max_usd: "{cap}"' in text
    path.write_text(text, encoding="utf-8")


def test_estimate_revision_prints_counts(db: TestDatabase, database: Database, env: Path) -> None:
    _done(database)
    banking_paper(database, doi_in(EvalSplit.TUNING, "2"), 2)
    before = _all_rows(db)

    result = _invoke("runs", "estimate-revision", "--taxonomy", "--context-values")

    assert result.exit_code == 0, result.output
    shown = _stdout(result)
    assert "分類表改版 → classify" in shown
    assert "情境值清單改版 → extract_model" in shown
    assert "上限 revision_rerun_max_usd：5.0000 美元" in shown
    assert _all_rows(db) == before


def test_estimate_revision_over_the_cap_exits_1(
    db: TestDatabase, database: Database, env: Path
) -> None:
    _done(database)
    _set_cap(env, "0")

    result = _invoke("runs", "estimate-revision", "--taxonomy")

    assert result.exit_code == 1
    assert "超過上限" in _stdout(result)


def test_estimate_revision_ctrl_c_exits_130(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _done(database)
    before = _all_rows(db)

    def interrupt(*_args: object, **_kwargs: object) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "estimate_revision_reruns", interrupt)

    result = _invoke("runs", "estimate-revision", "--taxonomy")

    assert result.exit_code == 130
    assert "中斷了（沒有寫任何資料）" in result.stderr
    assert _all_rows(db) == before


def test_estimate_revision_needs_a_flag(db: TestDatabase, env: Path) -> None:
    result = _invoke("runs", "estimate-revision")

    assert result.exit_code == 2
    assert "--taxonomy、--context-values 至少給一個" in result.stderr


def test_estimate_revision_bad_cap_in_config_exits_1(db: TestDatabase, env: Path) -> None:
    _set_cap(env, "51")

    result = _invoke("runs", "estimate-revision", "--taxonomy")

    assert result.exit_code == 1
    assert "revision_rerun_max_usd" in result.stderr


def test_batches_create_stops_over_the_rerun_cap(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _done(database)
    banking_paper(database, doi_in(EvalSplit.TUNING, "2"), 2)
    # 模擬分類表已經改版：現行分類的 run 記的是舊版本，鍵也和現在不同（版本算進輸入指紋）。
    execute(
        db,
        "UPDATE derivation_run SET input_sha256 = %s, taxonomy_version = 'old-version'"
        " WHERE id IN (SELECT run_id FROM classification WHERE status = 'active')",
        "b" * 64,
    )
    execute(db, "UPDATE classification SET input_sha256 = %s WHERE status = 'active'", "b" * 64)
    _set_cap(env, "0")
    built: list[object] = []

    def build(settings: object) -> Callable[..., Any]:
        built.append(settings)
        raise AssertionError("不該建 provider")

    monkeypatch.setattr(cli, "build_llm_provider", build)
    before = _all_rows(db)

    result = _invoke("batches", "create", stdin="y\n")

    assert result.exit_code == 1
    assert "規劃整次不排" in _stdout(result)
    assert "radar runs estimate-revision" in _stdout(result)
    assert _all_rows(db) == before
    assert built == []
