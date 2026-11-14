"""`radar weekly-run report` 對真的 PostgreSQL（`CliRunner`＋環境變數；Story 1.9、ADR-0026）。

1. 複製一份 `config/`，`journals.yaml` 只留三本（misq、isr、io），`import_baseline.yaml` 換成
   測試用的基準：misq 差 +2%、isr 差 +40%、io 沒有基準。
2. `FakePaperSource` 抓三本（io 是錄好的 I&O 三頁，CC0；其餘自己造），再照正式的
   `apply_weekly_update` 套用、發佈快照 1。
3. 執行指令，輸出和預期的每一行完全相同；報表的 SQL 沒碰論文四張表。

另有抓取中的輸出、找不到（沒有任何每週更新／`--id` 不存在／`--id` 不是 UUID）的退出碼、
設定錯誤與連不上資料庫。不連網路；環境變數只用測試值。
"""

import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from typer.testing import CliRunner

import radar.entrypoints.cli as cli
from radar.adapters.fake import FakeHeartbeat, FakeObjectStore, FakePaperSource
from radar.adapters.fake.jobs import FakeJobQueue
from radar.config import load_domain_config
from radar.domain.domain_config import DomainConfig
from radar.domain.raw_records import RawRecord, RawSource
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.services.ingest import JournalFetchError
from radar.services.weekly_update import apply_weekly_update, run_weekly_update, weekly_update
from tests.database import APP_ROLE, ROOT, TestDatabase, migrate
from tests.openalex_cassette import ISSN_L, cassette_pages, cassette_work, work_bytes

NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
SYSTEM = SystemViewer()
MISQ_ISSN = "0276-7783"
ISR_ISSN = "1047-7047"
JOURNALS_YAML = """\
start_year: 2016
journals:
  - key: misq
    name: MIS Quarterly
    issn_l: 0276-7783
  - key: isr
    name: Information Systems Research
    issn_l: 1047-7047
  - key: io
    name: Information and Organization
    issn_l: 1471-7727
"""
BASELINE_YAML = """\
year_from: 2016
year_to: 2025
journals:
  misq:
    2020: 50
  isr:
    2019: 5
"""
PAPER_TABLES = re.compile(r"\b(?:paper|external_id|paper_abstract|paper_keyword)\b", re.IGNORECASE)

runner = CliRunner()


class RecordingDatabase(Database):
    """記下執行過的每一句 SQL。"""

    def __init__(self, url: str, **kwargs: Any) -> None:
        super().__init__(url, connect_timeout=5, pool_size=1)
        self.statements: list[str] = []
        event.listen(self._engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        self.statements.append(statement)


@pytest.fixture
def report_db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    custom = tmp_path / "config"
    shutil.copytree(ROOT / "config", custom)
    (custom / "journals.yaml").write_text(JOURNALS_YAML, encoding="utf-8")
    (custom / "import_baseline.yaml").write_text(BASELINE_YAML, encoding="utf-8")
    return custom


@pytest.fixture
def recorded(
    report_db: TestDatabase, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> list[RecordingDatabase]:
    """指令要用的環境變數；`build_database` 換成會記 SQL 的版本（連的仍是測試資料庫）。"""
    monkeypatch.setenv("DATABASE_URL", report_db.app_url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))
    built: list[RecordingDatabase] = []

    def build(settings: Any, *, pool_size: int) -> RecordingDatabase:
        database = RecordingDatabase(settings.database_url.get_secret_value())
        built.append(database)
        return database

    monkeypatch.setattr(cli, "build_database", build)
    return built


# --- 資料 ---------------------------------------------------------------------------------


def _key(work: dict[str, Any]) -> str:
    return str(work["id"]).rsplit("/", 1)[-1]


def _records(works: list[dict[str, Any]]) -> list[RawRecord]:
    return [RawRecord(RawSource.OPENALEX, _key(work), work_bytes(work), T1) for work in works]


def _work(
    number: int, issn_l: str, year: int, *, work_type: str = "article", title: str | None = None
) -> dict[str, Any]:
    work = cassette_work()
    work["id"] = f"https://openalex.org/W8{number:08d}"
    work["doi"] = f"https://doi.org/10.9999/radar-report.{number}"
    work["type"] = work_type
    work["publication_year"] = year
    work["primary_location"]["source"]["issn_l"] = issn_l
    work["title"] = work["display_name"] = title or f"Report study {number}"
    return work


def _misq_pages() -> list[list[RawRecord]]:
    """MISQ 2020 年 51 篇：50 篇 article（一篇命中排除規則）＋1 篇 paratext（收錄範圍外）。"""
    works = [_work(n, MISQ_ISSN, 2020) for n in range(1, 50)]
    works.append(_work(50, MISQ_ISSN, 2020, title="Editor's Comments: On Baselines"))
    works.append(_work(51, MISQ_ISSN, 2020, work_type="paratext"))
    return [_records(works[:30]), _records(works[30:])]


def _isr_pages() -> list[list[RawRecord]]:
    """ISR 2019 年 7 篇，另有 1 篇解析不了（沒有標題）。"""
    works = [_work(n, ISR_ISSN, 2019) for n in range(101, 108)]
    broken = _work(108, ISR_ISSN, 2019)
    broken["title"] = broken["display_name"] = None
    return [_records(works[:4]), _records([*works[4:], broken])]


def _io_pages() -> list[list[RawRecord]]:
    return [_records(list(page)) for page in cassette_pages()]


def _domain(config_dir: Path) -> DomainConfig:
    return load_domain_config({"RADAR_CONFIG_DIR": str(config_dir)})


def _fetch(
    database: Database, config_dir: Path, source: FakePaperSource, weekly_run_id: uuid.UUID
) -> Any:
    domain = _domain(config_dir)
    return run_weekly_update(
        SYSTEM,
        database,
        source,
        FakeObjectStore(),
        weekly_run_id,
        journals=domain.journals,
        exclusion_rules=domain.exclusion_rules,
        quota=domain.openalex.quota,
        clock=lambda: NOW,
    )


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") + "（UTC）"


def _stdout_lines(output: str) -> list[str]:
    # 日誌是 JSON（以 { 開頭）；報表本身不是。
    return [line for line in output.splitlines() if not line.startswith("{")]


def _no_paper_tables(built: list[RecordingDatabase]) -> None:
    statements = [statement for database in built for statement in database.statements]
    assert any("weekly_run_journal" in statement for statement in statements)
    assert not [statement for statement in statements if PAPER_TABLES.search(statement)]


# --- 套用後的完整輸出 -------------------------------------------------------------------------


def test_report_after_apply(
    report_db: TestDatabase, config_dir: Path, recorded: list[RecordingDatabase]
) -> None:
    database = Database(report_db.app_url, connect_timeout=5, pool_size=2)
    run_id = weekly_update(SYSTEM, database, FakeJobQueue().enqueue).weekly_run_id
    source = FakePaperSource(
        {MISQ_ISSN: _misq_pages(), ISR_ISSN: _isr_pages(), ISSN_L: _io_pages()},
        clock=lambda: NOW,
    )
    assert _fetch(database, config_dir, source, run_id).fetch_finished
    applied = apply_weekly_update(
        SYSTEM,
        database,
        run_id,
        scope=_domain(config_dir).journals.scope_settings(),
        send_heartbeat=FakeHeartbeat(),
    )
    assert applied.snapshot_number == 1
    with report_db.connect(APP_ROLE) as connection:
        [(finished_at,)] = connection.execute("SELECT fetch_finished_at FROM weekly_run").fetchall()
        [(as_of, created_at)] = connection.execute(
            "SELECT as_of, created_at FROM snapshot"
        ).fetchall()
    seconds = (as_of - created_at).total_seconds()

    result = runner.invoke(cli.app, ["weekly-run", "report"])

    assert result.exit_code == 0, result.output
    assert _stdout_lines(result.stdout) == [
        f"每週更新 {run_id}",
        "狀態：已發佈",
        f"抓完時間：{_utc(finished_at)}",
        f"快照 1：截至 {_utc(as_of)}；套用 {seconds:.1f} 秒",
        "",
        "每本期刊每年的過濾前篇數",
        "期刊  2016  2017  2018  2019  2020  2021  2022  2023  2024  2025  2026  合計",
        "misq     0     0     0     0    51     0     0     0     0     0     0    51",
        "isr      0     0     0     7     0     0     0     0     0     0     0     7",
        "io      14    20    22    23    15    26    21    25    27    32    23   248",
        "合計    14    20    22    30    66    26    21    25    27    32    23   306",
        "",
        "依期刊的排除篇數",
        "期刊  排除  收錄範圍外  解析失敗",
        "misq     1           1         0",
        "isr      0           0         1",
        "io       0          48         0",
        "合計     1          49         1",
        "",
        "和 OQ-02 基準比對（2016–2025 過濾前篇數）",
        "期刊          本次  基準      差異",
        "misq            51    50     +2.0%",
        "isr              7     5    +40.0%  超過 5%",
        "io             225     -  沒有基準",
        "有基準的合計    58    55     +5.5%  超過 5%",
        "超過 5% 的期刊：isr",
    ]
    _no_paper_tables(recorded)

    # 指定 --id 是同一份。
    by_id = runner.invoke(cli.app, ["weekly-run", "report", "--id", str(run_id)])
    assert by_id.exit_code == 0, by_id.output
    assert _stdout_lines(by_id.stdout) == _stdout_lines(result.stdout)


def test_report_while_fetching(
    report_db: TestDatabase,
    config_dir: Path,
    recorded: list[RecordingDatabase],
) -> None:
    # 只有兩本：misq 抓完，isr 抓到第 2 頁失敗（還在抓）。
    (config_dir / "journals.yaml").write_text(
        JOURNALS_YAML.split("  - key: io")[0], encoding="utf-8"
    )
    database = Database(report_db.app_url, connect_timeout=5, pool_size=2)
    run_id = weekly_update(SYSTEM, database, FakeJobQueue().enqueue).weekly_run_id
    source = FakePaperSource({MISQ_ISSN: _misq_pages(), ISR_ISSN: _isr_pages()}, clock=lambda: NOW)
    source.fail_once(ISR_ISSN, 2)
    with pytest.raises(JournalFetchError):
        _fetch(database, config_dir, source, run_id)

    result = runner.invoke(cli.app, ["weekly-run", "report"])

    assert result.exit_code == 0, result.output
    assert _stdout_lines(result.stdout) == [
        f"每週更新 {run_id}",
        "狀態：抓取中 1／2 本抓完",
        "抓完時間：還沒抓完",
        "",
        "每本期刊每年的過濾前篇數",
        "期刊  2019  2020  合計",
        "misq     0    51    51",
        "isr      4     0     4",
        "合計     4    51    55",
        "",
        "依期刊的排除篇數",
        "期刊  排除  收錄範圍外  解析失敗",
        "misq     1           1         0",
        "isr      0           0         0",
        "合計     1           1         0",
        "",
        "和 OQ-02 基準比對（2016–2025 過濾前篇數）",
        "期刊          本次  基準      差異",
        "misq            51    50     +2.0%",
        "isr              -     5  還沒抓完",
        "有基準的合計     -    55  還沒抓完",
        "超過 5% 的期刊：沒有",
    ]
    _no_paper_tables(recorded)


# --- 找不到、設定錯誤、連不上 -----------------------------------------------------------------


def test_no_weekly_run_at_all_exits_1(recorded: list[RecordingDatabase]) -> None:
    result = runner.invoke(cli.app, ["weekly-run", "report"])

    assert result.exit_code == 1
    assert "還沒有任何每週更新" in result.stderr
    assert result.stdout == ""


def test_unknown_id_exits_1(recorded: list[RecordingDatabase]) -> None:
    missing = uuid.uuid7()

    result = runner.invoke(cli.app, ["weekly-run", "report", "--id", str(missing)])

    assert result.exit_code == 1
    assert f"找不到這次每週更新：{missing}" in result.stderr


def test_id_that_is_not_a_uuid_exits_2(recorded: list[RecordingDatabase]) -> None:
    result = runner.invoke(cli.app, ["weekly-run", "report", "--id", "not-a-uuid"])

    assert result.exit_code == 2
    assert recorded == []  # 沒有連資料庫


def test_missing_baseline_file_exits_1(recorded: list[RecordingDatabase], config_dir: Path) -> None:
    (config_dir / "import_baseline.yaml").unlink()

    result = runner.invoke(cli.app, ["weekly-run", "report"])

    assert result.exit_code == 1
    assert "設定錯誤，沒有產生報表" in result.stderr
    assert "import_baseline.yaml：找不到檔案" in result.stderr
    assert recorded == []


def test_unreachable_database_prints_only_the_error_type(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "postgresql://radar_app:secret-password@127.0.0.1:9/radar"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))

    result = runner.invoke(cli.app, ["weekly-run", "report"])

    assert result.exit_code == 1
    assert "讀取匯入報表失敗（" in result.stderr
    assert "secret-password" not in result.output
    assert "127.0.0.1:9" not in result.output
