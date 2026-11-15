"""`radar runs trial` 對真的 PostgreSQL（`CliRunner`＋環境變數；Story 2.10）。

指定 ID 與 DOI、抽樣、找不到、不確認、`--verbose`／`--show-output`、熔斷、金鑰錯、缺金鑰、看不懂的
參數。模型用依 schema 回覆的替身（換掉 `build_llm_provider`），不連網路；環境變數只用測試值；論文與
回覆都是自己編的。
"""

import logging
import shutil
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest
import typer
from typer.testing import CliRunner, Result

import radar.entrypoints.cli as cli
from radar.domain.ai_outputs import LlmTask
from radar.domain.eval_split import EvalSplit
from radar.domain.llm import LlmConfigurationError, LlmServerError
from radar.repo.db import Database
from tests.batch_support import NOW
from tests.database import ROOT, TestDatabase, migrate
from tests.llm_support import execute, insert_ledger, table_counts
from tests.trial_support import (
    BANKING_ABSTRACT,
    Reply,
    SchemaReplyProvider,
    banking_paper,
    doi_in,
    good_replies,
    reply,
)

runner = CliRunner()
FAKE_KEY = "sk-ant-FAKE-KEY-for-cli-tests"


class _ProviderContext:
    """`build_llm_provider` 的替身：`with` 進去拿到依 schema 回覆的替身。"""

    def __init__(self, provider: SchemaReplyProvider) -> None:
        self.provider = provider
        self.closed = False

    def __enter__(self) -> SchemaReplyProvider:
        return self.provider

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    custom = tmp_path / "config"
    shutil.copytree(ROOT / "config", custom)
    return custom


@pytest.fixture
def env(db: TestDatabase, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATABASE_URL", db.app_url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("RADAR_ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    return config_dir


class Models:
    """換掉 `build_llm_provider`，記下用到的替身。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.contexts: list[_ProviderContext] = []
        self._monkeypatch = monkeypatch

    def use(self, replies: Mapping[LlmTask, Reply]) -> Self:
        def build(_settings: object) -> _ProviderContext:
            context = _ProviderContext(SchemaReplyProvider(replies))
            self.contexts.append(context)
            return context

        self._monkeypatch.setattr(cli, "build_llm_provider", build)
        return self

    @property
    def calls(self) -> int:
        return sum(context.provider.total_calls for context in self.contexts)


@pytest.fixture
def models(env: Path, monkeypatch: pytest.MonkeyPatch) -> Models:
    return Models(monkeypatch).use(good_replies())


def _trial(*args: str, stdin: str | None = None) -> Result:
    """執行 `radar runs trial`；結束後把 root logger 的 handler 換回原本的（見 test_costs_cli）。"""
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        return runner.invoke(cli.app, ["runs", "trial", *args], input=stdin)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)


def _stdout(result: Result) -> list[str]:
    # 日誌是 JSON（以 { 開頭）；指令自己印的不是。
    return [line for line in result.stdout.splitlines() if not line.startswith("{")]


def _logs(result: Result) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.startswith("{")]


def test_ids_and_dois_run_every_task(db: TestDatabase, database: Database, models: Models) -> None:
    tuning = doi_in(EvalSplit.TUNING, "1")
    test = doi_in(EvalSplit.TEST, "1")
    first = banking_paper(database, tuning, 1)
    second = banking_paper(database, test, 2)

    result = _trial(str(first), f"https://doi.org/{test.upper()}", tuning, "--yes")

    assert result.exit_code == 0, result.output
    lines = _stdout(result)
    assert lines[0] == "選到的論文（2 篇）："
    assert any(str(first) in line and tuning in line and "調整組" in line for line in lines)
    assert any(str(second) in line and test in line and "測試組" in line for line in lines)
    assert f"警告：{second} 是測試組，結果不要拿來改提示詞" in lines
    assert any(line.startswith("預估：會交給閘門 6 項，費用上限 ") for line in lines)
    assert any(line.startswith("本月（2026-11，台北時間）：已花 0.0000") for line in lines)
    item_lines = [line for line in lines if line.startswith("[")]
    assert len(item_lines) == 6
    assert item_lines[0].startswith(f"[1/6] {first} zh_summary active ")
    assert item_lines[5].startswith(f"[6/6] {second} extract_model active ")
    assert "結果（2 篇 × 3 種工作）：" in lines
    assert any(
        line.split()[:5] == [str(first), "調整組", "active", "active", "active"] for line in lines
    )
    assert "拒收：沒有" in lines
    assert any(line.startswith("這次費用：0.00") for line in lines)
    assert table_counts(db)["llm_call"] == 6
    assert models.contexts[0].closed
    # 日誌與輸出不帶論文內容與模型輸出、金鑰。
    assert BANKING_ABSTRACT not in result.output
    assert "知覺有用性" not in result.output
    assert FAKE_KEY not in result.output

    rerun = _trial(str(first), str(second), "--yes")

    assert rerun.exit_code == 0, rerun.output
    assert sum(" skipped" in line for line in _stdout(rerun) if line.startswith("[")) == 6
    assert any(line == "這次費用：0.000000 美元（接續與略過是 0）" for line in _stdout(rerun))
    assert table_counts(db)["llm_call"] == 6


def test_sample_prints_and_logs_seed(db: TestDatabase, database: Database, models: Models) -> None:
    tuning = [banking_paper(database, doi_in(EvalSplit.TUNING, "1", skip=n), n) for n in range(3)]
    for n in range(3):
        banking_paper(database, doi_in(EvalSplit.TEST, "1", skip=n), 10 + n)

    result = _trial("--sample", "2", "--seed", "7", "--yes")

    assert result.exit_code == 0, result.output
    lines = _stdout(result)
    assert lines[0] == "抽樣：seed 7，從調整組符合條件的 3 篇抽 2 篇"
    picked = [paper for paper in tuning if any(str(paper) in line for line in lines[:6])]
    assert len(picked) == 2
    [log_line] = [line for line in _logs(result) if "試跑抽樣" in line]
    assert '"seed": 7' in log_line
    assert all(str(paper) in log_line for paper in picked)
    assert table_counts(db)["llm_call"] == 6

    short = _trial("--sample", "5", "--seed", "7", "--yes")
    assert "調整組符合條件的只有 3 篇，少於 5 篇，全部抽出" in _stdout(short)


def test_missing_paper_runs_nothing(db: TestDatabase, database: Database, models: Models) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    missing = uuid.UUID("01900000-0000-7000-8000-00000000abcd")
    before = table_counts(db)

    result = _trial(str(paper), str(missing), "10.1/nowhere", "--yes")

    assert result.exit_code == 1
    assert "找不到這些論文，一篇都沒有跑：" in result.stderr
    assert f"- {missing}" in result.stderr
    assert "- 10.1/nowhere" in result.stderr
    assert table_counts(db) == before
    assert models.calls == 0


@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_not_confirmed_writes_nothing(
    db: TestDatabase, database: Database, models: Models, answer: str
) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    before = table_counts(db)

    result = _trial(str(paper), stdin=answer)

    assert result.exit_code == 1
    assert "要執行嗎？" in result.stdout
    assert "沒有執行" in result.stderr
    assert table_counts(db) == before
    assert models.calls == 0


def test_confirmed_with_y_runs(db: TestDatabase, database: Database, models: Models) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)

    result = _trial(str(paper), stdin="y\n")

    assert result.exit_code == 0, result.output
    assert table_counts(db)["llm_call"] == 3


def test_verbose_and_show_output(db: TestDatabase, database: Database, models: Models) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    replies = good_replies()
    replies[LlmTask.EXTRACT_MODEL] = reply('{\n"not":\t"a research model"}')
    models.use(replies)

    verbose = _trial(str(paper), "--yes", "--verbose")

    assert verbose.exit_code == 0, verbose.output
    lines = _stdout(verbose)
    assert any(
        line.startswith("    耗時 ")
        and "輸入 900／估計上限 " in line
        and "輸出 120／max_tokens" in line
        for line in lines
    )
    assert any(line.startswith("    stop_reason end_turn；price_tier standard；") for line in lines)
    assert any(
        line.startswith("    回應形狀：頂層鍵 [content, id, model,") and "[thinking, text]" in line
        for line in lines
    )
    assert any(line.startswith("    送出字元：系統提示 ") for line in lines)
    assert any(line.startswith("zh_summary：最長耗時 ") for line in lines)
    assert "拒收：llm_output_invalid 1 項" in lines
    assert "知覺有用性" not in verbose.output
    assert "a research model" not in verbose.output
    assert BANKING_ABSTRACT not in verbose.output

    # 已經有結果：重跑都略過，沒有輸出可印；換一篇看 --show-output。
    other = banking_paper(database, doi_in(EvalSplit.TUNING, "1", skip=1), 2)
    shown = _trial(str(other), "--yes", "--show-output")

    assert shown.exit_code == 0, shown.output
    output_lines = [line for line in _stdout(shown) if line.startswith("    ")]
    assert any(line.startswith("    輸出：{") and "知覺有用性" in line for line in output_lines)
    assert '    模型原文（拒收）："{\\n\\"not\\":\\t\\"a research model\\"}"' in output_lines
    assert all("a research model" not in line for line in _logs(shown))
    assert all("知覺有用性" not in line for line in _logs(shown))


def test_breaker_defers_without_calling(
    db: TestDatabase, database: Database, models: Models
) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    insert_ledger(db, paper, LlmTask.QA_ANSWER, "50", NOW)
    before = table_counts(db)

    result = _trial(str(paper), "--yes")

    assert result.exit_code == 0, result.output
    lines = _stdout(result)
    assert any(line.startswith("已熔斷") for line in lines)
    assert sum("deferred_budget" in line for line in lines if line.startswith("[")) == 3
    assert table_counts(db) == before
    assert models.calls == 0


def test_configuration_error_stops_with_status(
    db: TestDatabase, database: Database, models: Models
) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    replies = good_replies()
    replies[LlmTask.ZH_SUMMARY] = LlmConfigurationError("HTTP 401", status=401)
    models.use(replies)

    result = _trial(str(paper), "--yes")

    assert result.exit_code == 1
    lines = _stdout(result)
    assert any("zh_summary error （llm_configuration） HTTP 401" in line for line in lines)
    assert sum(" not_run" in line for line in lines if line.startswith("[")) == 2
    assert "金鑰、權限或模型設定錯誤（HTTP 401），停下" in result.stderr
    assert FAKE_KEY not in result.output


def test_transient_failure_exits_1_with_hint(
    db: TestDatabase, database: Database, models: Models
) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    replies = good_replies()
    replies[LlmTask.CLASSIFY] = LlmServerError("HTTP 529", status=529)
    models.use(replies)

    result = _trial(str(paper), "--yes")

    assert result.exit_code == 1
    assert "有暫時性失敗：直接重跑同一個指令，做完的不會重付" in _stdout(result)


def test_ctrl_c_exits_130(db: TestDatabase, database: Database, models: Models) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    replies = good_replies()
    replies[LlmTask.CLASSIFY] = KeyboardInterrupt()
    models.use(replies)

    result = _trial(str(paper), "--yes")

    assert result.exit_code == 130
    assert "重跑同一個指令只做沒做完的" in result.stderr
    assert table_counts(db)["llm_call"] == 1


def _interrupt(*_args: object, **_kwargs: object) -> Any:
    raise KeyboardInterrupt


def _abort_from_ctrl_c(*_args: object, **_kwargs: object) -> Any:
    # click 的 confirm 把 Ctrl-C 轉成 Abort（`from None`，原本的例外留在 __context__）。
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        raise typer.Abort from None


@pytest.mark.parametrize(
    ("target", "stub", "args"),
    [
        ("resolve_trial_papers", _interrupt, ("--yes",)),
        ("estimate_trial", _interrupt, ("--yes",)),
        ("typer.confirm", _abort_from_ctrl_c, ()),
    ],
)
def test_ctrl_c_before_running_exits_130_and_writes_nothing(
    db: TestDatabase,
    database: Database,
    models: Models,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    stub: Callable[..., Any],
    args: tuple[str, ...],
) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    before = table_counts(db)
    if target == "typer.confirm":
        monkeypatch.setattr(typer, "confirm", stub)
    else:
        monkeypatch.setattr(cli, target, stub)

    result = _trial(str(paper), *args)

    assert result.exit_code == 130
    assert "中斷了，沒有執行" in result.stderr
    assert table_counts(db) == before
    assert models.calls == 0


def test_missing_api_key_exits_1(
    db: TestDatabase, database: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RADAR_ANTHROPIC_API_KEY")
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)

    result = _trial(str(paper), "--yes")

    assert result.exit_code == 1
    assert "缺少環境變數 RADAR_ANTHROPIC_API_KEY" in result.stderr
    assert table_counts(db)["derivation_run"] == 0


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["not-a-doi"], "看不懂的論文 ID 或 DOI"),
        (["10.1/a", "--sample", "3", "--seed", "1"], "只能擇一"),
        (["--sample", "3"], "--sample 要和 --seed 一起用"),
        (["--seed", "3", "10.1/a"], "--seed 只能和 --sample 一起用"),
        (["--sample", "101", "--seed", "1"], "1～100"),
        (["--sample", "0", "--seed", "1"], "1～100"),
        ([f"10.1/p{n}" for n in range(101)], "最多 100"),
        ([], "要指定論文"),
    ],
)
def test_unreadable_arguments_exit_2_without_touching_the_database(
    env: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], message: str
) -> None:
    built: list[object] = []
    record: Callable[..., Any] = lambda *a, **k: built.append(a)  # noqa: E731
    monkeypatch.setattr(cli, "build_database", record)
    monkeypatch.setattr(cli, "build_llm_provider", record)

    result = _trial(*args)

    assert result.exit_code == 2
    assert message in result.stderr
    assert built == []


# --- 審查後的修正（Codex 主審查第 1、3、4 條） ---------------------------------------------


def _worst_case(lines: list[str]) -> tuple[str, str]:
    """預估那一行的（金額, 每項最多送幾次）。"""
    [line] = [line for line in lines if line.startswith("預估：")]
    amount = line.split("費用上限 ")[1].split(" 美元")[0]
    attempts = line.split("每項最多送 ")[1].split(" 次")[0]
    return amount, attempts


@pytest.mark.parametrize("attempts", [1, 4])
def test_worst_case_uses_the_provider_attempts(
    db: TestDatabase,
    database: Database,
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    attempts: int,
) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)

    def build(_settings: object) -> _ProviderContext:
        return _ProviderContext(SchemaReplyProvider(good_replies(), attempts=attempts))

    monkeypatch.setattr(cli, "build_llm_provider", build)
    single = _trial(str(paper), "--verbose", stdin="n\n")
    assert single.exit_code == 1

    amount, shown = _worst_case(_stdout(single))
    assert shown == str(attempts)
    assert "含重試的最壞情況" in single.stdout

    result = _trial(str(paper), "--yes", "--verbose")

    assert result.exit_code == 0, result.output
    cost_lines = [line for line in _stdout(result) if line.startswith("    費用上限：單次 ")]
    assert len(cost_lines) == 3
    singles = [Decimal(line.split("單次 ")[1].split(" 美元")[0]) for line in cost_lines]
    worsts = [Decimal(line.split("最壞情況 ")[1].split(" 美元")[0]) for line in cost_lines]
    assert all(f"（最多送 {attempts} 次）" in line for line in cost_lines)
    assert Decimal(amount) == sum(worsts)
    for one, worst in zip(singles, worsts, strict=True):
        assert abs(worst - one * attempts) <= Decimal("0.000001") * attempts


def test_save_ids_then_ids_file_reruns_the_same_papers(
    db: TestDatabase, database: Database, models: Models, tmp_path: Path
) -> None:
    for n in range(4):
        banking_paper(database, doi_in(EvalSplit.TUNING, "1", skip=n), n)
    saved = tmp_path / "trial-ids.txt"

    drawn = _trial("--sample", "2", "--seed", "7", "--save-ids", str(saved), stdin="n\n")

    # 按 n：什麼都沒跑，但名單已經存好。
    assert drawn.exit_code == 1
    assert models.calls == 0
    assert table_counts(db)["derivation_run"] == 0
    picked = [uuid.UUID(line) for line in saved.read_text(encoding="utf-8").splitlines()]
    assert len(picked) == 2
    assert f"名單已存到 {saved}（2 篇）" in _stdout(drawn)
    # 之後又進了幾篇新論文（同一個 seed 可能抽到別篇），名單檔照樣跑原本那 2 篇。
    for n in range(4, 30):
        banking_paper(database, doi_in(EvalSplit.TUNING, "1", skip=n), n)

    result = _trial("--ids-file", str(saved), "--yes")

    assert result.exit_code == 0, result.output
    lines = _stdout(result)
    assert lines[0] == "選到的論文（2 篇）："
    item_lines = [line for line in lines if line.startswith("[")]
    assert [uuid.UUID(line.split()[1]) for line in item_lines[::3]] == picked
    runs = execute(db, "SELECT DISTINCT paper_id FROM derivation_run")
    assert {row[0] for row in runs} == set(picked)
    assert "這次呼叫模型：6 項（其中拒收 0 項）；沒有呼叫模型：0 項" in lines

    rerun = _trial("--ids-file", str(saved), "--yes")
    assert "這次呼叫模型：0 項（其中拒收 0 項）；沒有呼叫模型：6 項（skipped 6）" in _stdout(rerun)


def test_save_ids_never_overwrites(
    db: TestDatabase, database: Database, models: Models, tmp_path: Path
) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)
    saved = tmp_path / "keep.txt"
    saved.write_text("# 之前存的名單\n", encoding="utf-8")

    result = _trial(str(paper), "--save-ids", str(saved), "--yes")

    assert result.exit_code == 2
    assert "--save-ids 的檔案已經存在，不覆寫" in result.stderr
    assert saved.read_text(encoding="utf-8") == "# 之前存的名單\n"
    assert models.calls == 0


def test_save_ids_write_failure_runs_nothing(
    db: TestDatabase, database: Database, models: Models, tmp_path: Path
) -> None:
    paper = banking_paper(database, doi_in(EvalSplit.TUNING, "1"), 1)

    result = _trial(str(paper), "--save-ids", str(tmp_path / "no-such-dir" / "ids.txt"), "--yes")

    assert result.exit_code == 1
    assert "名單檔寫不進去（FileNotFoundError），沒有執行" in result.stderr
    assert models.calls == 0
    assert table_counts(db)["derivation_run"] == 0


@pytest.mark.parametrize(
    ("make_args", "message"),
    [
        (lambda p: ["--ids-file", str(p / "missing.txt")], "讀不到名單檔（FileNotFoundError）"),
        (lambda p: ["--ids-file", str(p / "empty.txt")], "名單檔裡沒有論文"),
        (lambda p: ["--ids-file", str(p / "bad.txt")], "看不懂"),
        (lambda p: ["--ids-file", str(p / "latin1.txt")], "讀不到名單檔（UnicodeDecodeError）"),
        (lambda p: ["--ids-file", str(p / "empty.txt"), "10.1/x"], "只能擇一"),
        (
            lambda p: ["--ids-file", str(p / "empty.txt"), "--sample", "1", "--seed", "1"],
            "只能擇一",
        ),
        (lambda p: ["--ids-file", str(p / "empty.txt"), "--seed", "1"], "--seed 只能和"),
    ],
)
def test_ids_file_problems_exit_2_without_touching_the_database(
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_args: Callable[[Path], list[str]],
    message: str,
) -> None:
    (tmp_path / "empty.txt").write_text("# 沒有論文\n", encoding="utf-8")
    (tmp_path / "bad.txt").write_text("not-a-paper\n", encoding="utf-8")
    (tmp_path / "latin1.txt").write_bytes(b"\xff\xfe10.1/x\n")
    built: list[object] = []
    record: Callable[..., Any] = lambda *a, **k: built.append(a)  # noqa: E731
    monkeypatch.setattr(cli, "build_database", record)
    monkeypatch.setattr(cli, "build_llm_provider", record)

    result = _trial(*make_args(tmp_path))

    assert result.exit_code == 2
    assert message in result.stderr
    assert built == []
