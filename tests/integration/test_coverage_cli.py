"""`radar coverage` 對真的 PostgreSQL（`CliRunner`＋環境變數；Story 1.10、ADR-0027）。

1. 複製一份 `config/`，`journals.yaml` 只留三本（misq、isr、io）。
2. `FakePaperSource` 抓三本（自己造的；io 沒有論文），照正式的 `apply_weekly_update` 套用、發佈
   快照 1。另一條路（成員匯入）寫入的追蹤範圍外論文、被排除的論文都不算。
3. 執行指令，輸出和預期的每一行完全相同；指令的 SQL 沒碰 `paper` 表（`counted_paper`、
   `paper_abstract` 不算）。

另有：下一個快照發佈後數字是新的、沒有已發佈的快照、設定少了期刊、連不上資料庫。不連網路；
環境變數只用測試值。
"""

import logging
import re
import shutil
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event
from typer.testing import CliRunner, Result

import radar.entrypoints.cli as cli
from radar.adapters.fake import FakeHeartbeat, FakeObjectStore, FakePaperSource
from radar.adapters.fake.jobs import FakeJobQueue
from radar.config import load_domain_config
from radar.domain.domain_config import DomainConfig
from radar.domain.papers import PaperOrigin
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.raw_records import RawRecord, RawSource
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.papers import upsert_by_external_id
from radar.services.weekly_update import apply_weekly_update, run_weekly_update, weekly_update
from tests.database import ROOT, TestDatabase, migrate
from tests.openalex_cassette import ISSN_L, cassette_work, work_bytes

NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
SYSTEM = SystemViewer()
MISQ_ISSN = "0276-7783"
ISR_ISSN = "1047-7047"
OTHER_ISSN = "0960-085X"  # EJIS：這些測試的追蹤範圍外
TAIPEI = ZoneInfo("Asia/Taipei")
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
# 論文表本身（`counted_paper`、`paper_abstract` 前後接著底線，不算）。
PAPER_TABLE = re.compile(r"\bpaper\b", re.IGNORECASE)

runner = CliRunner()


class RecordingDatabase(Database):
    """記下執行過的每一句 SQL。"""

    def __init__(self, url: str) -> None:
        super().__init__(url, connect_timeout=5, pool_size=1)
        self.statements: list[str] = []
        event.listen(self._engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        self.statements.append(statement)


@pytest.fixture
def coverage_db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(coverage_db: TestDatabase) -> Database:
    return Database(coverage_db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    custom = tmp_path / "config"
    shutil.copytree(ROOT / "config", custom)
    (custom / "journals.yaml").write_text(JOURNALS_YAML, encoding="utf-8")
    return custom


@pytest.fixture
def recorded(
    coverage_db: TestDatabase, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> list[RecordingDatabase]:
    """指令要用的環境變數；`build_database` 換成會記 SQL 的版本（連的仍是測試資料庫）。"""
    monkeypatch.setenv("DATABASE_URL", coverage_db.app_url)
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
    number: int,
    issn_l: str,
    year: int,
    *,
    abstract: bool = True,
    work_type: str = "article",
    title: str | None = None,
) -> dict[str, Any]:
    work = cassette_work()  # 錄好的這一篇有摘要
    work["id"] = f"https://openalex.org/W6{number:08d}"
    work["doi"] = f"https://doi.org/10.9999/radar-coverage.{number}"
    work["type"] = work_type
    work["publication_year"] = year
    work["primary_location"]["source"]["issn_l"] = issn_l
    work["title"] = work["display_name"] = title or f"Coverage study {number}"
    if not abstract:
        work["abstract_inverted_index"] = None
    return work


def _misq_pages() -> list[list[RawRecord]]:
    """MISQ：2020 年 4 篇（3 篇有摘要）、2021 年 1 篇；另有被排除的 1 篇與收錄範圍外的 1 篇。"""
    works = [
        _work(1, MISQ_ISSN, 2020),
        _work(2, MISQ_ISSN, 2020),
        _work(3, MISQ_ISSN, 2020),
        _work(4, MISQ_ISSN, 2020, abstract=False),
        _work(5, MISQ_ISSN, 2021),
        _work(6, MISQ_ISSN, 2020, title="Editor's Comments: On Coverage"),
        _work(7, MISQ_ISSN, 2020, work_type="paratext"),
    ]
    return [_records(works[:4]), _records(works[4:])]


def _isr_pages() -> list[list[RawRecord]]:
    """ISR：2019 年 2 篇（1 篇有摘要）。"""
    return [_records([_work(11, ISR_ISSN, 2019), _work(12, ISR_ISSN, 2019, abstract=False)])]


def _source() -> FakePaperSource:
    return FakePaperSource(
        {MISQ_ISSN: _misq_pages(), ISR_ISSN: _isr_pages(), ISSN_L: [[]]}, clock=lambda: NOW
    )


def _domain(config_dir: Path) -> DomainConfig:
    return load_domain_config({"RADAR_CONFIG_DIR": str(config_dir)})


def _weekly_update(database: Database, config_dir: Path) -> int:
    """一次完整的每週更新（抓三本、套用），回傳快照編號。"""
    domain = _domain(config_dir)
    run_id = weekly_update(SYSTEM, database, FakeJobQueue().enqueue).weekly_run_id
    fetched = run_weekly_update(
        SYSTEM,
        database,
        _source(),
        FakeObjectStore(),
        run_id,
        journals=domain.journals,
        exclusion_rules=domain.exclusion_rules,
        quota=domain.openalex.quota,
        clock=lambda: NOW,
    )
    assert fetched.fetch_finished
    applied = apply_weekly_update(
        SYSTEM,
        database,
        run_id,
        scope=domain.journals.scope_settings(),
        send_heartbeat=FakeHeartbeat(),
    )
    assert applied.snapshot_number is not None
    return applied.snapshot_number


def _insert_paper(database: Database, config_dir: Path, work: dict[str, Any]) -> uuid.UUID:
    """另一條路（成員匯入）寫入的一篇，不經每週更新。"""
    parsed = parse_openalex_work(work_bytes(work), fetched_at=T1)
    with database.transaction() as connection:
        result = upsert_by_external_id(
            connection,
            parsed,
            origin=PaperOrigin.IMPORT,
            excluded_reason=None,
            scope=_domain(config_dir).journals.scope_settings(),
        )
    return result.paper_id


def _cutoff(coverage_db: TestDatabase, number: int) -> date:
    with coverage_db.connect("radar_app") as connection:
        row = connection.execute(
            "SELECT as_of FROM snapshot WHERE number = %s", (number,)
        ).fetchone()
    assert row is not None
    as_of: datetime = row[0]
    return as_of.astimezone(TAIPEI).date()


def _coverage() -> Result:
    """執行 `radar coverage`；結束後把 root logger 的 handler 換回原本的。

    指令裡的 `configure_logging()` 會把 handler 換成寫到 CliRunner 暫時的 stdout，指令結束後那個
    stream 就關了；同一個測試之後再跑每週更新時，日誌會寫到關掉的 stream 而印出 logging 錯誤。
    """
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        return runner.invoke(cli.app, ["coverage"])
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)


def _stdout_lines(output: str) -> list[str]:
    # 日誌是 JSON（以 { 開頭）；報表本身不是。
    return [line for line in output.splitlines() if not line.startswith("{")]


def _no_paper_table(built: list[RecordingDatabase]) -> None:
    statements = [statement for database in built for statement in database.statements]
    assert any("counted_paper" in statement for statement in statements)
    assert not [statement for statement in statements if PAPER_TABLE.search(statement)]


def _expected(number: int, cutoff: date, *, misq_2021: int = 1) -> list[str]:
    misq_total = 4 + misq_2021
    return [
        f"快照 {number}：截至 {cutoff.isoformat()}（台北時間）",
        "",
        "收錄篇數（total）",
        "期刊  2019  2020  2021  合計",
        f"misq     0     4     {misq_2021}     {misq_total}",
        "isr      2     0     0     2",
        "io       0     0     0     0",
        f"合計     2     4     {misq_2021}     {misq_total + 2}",
        "",
        "有摘要的篇數（with_abstract）",
        "期刊  2019  2020  2021  合計",
        f"misq     0     3     {misq_2021}     {misq_total - 1}",
        "isr      1     0     0     1",
        "io       0     0     0     0",
        f"合計     1     3     {misq_2021}     {misq_total}",
        "",
        "依摘要來源分的篇數（目前的摘要）",
        "期刊  openalex  semantic_scholar  沒有摘要  合計",
        f"misq         {misq_total - 1}                 0         1     {misq_total}",
        "isr          1                 0         1     2",
        "io           0                 0         0     0",
        f"合計         {misq_total}                 0         2     {misq_total + 2}",
    ]


# --- 快照 1 之後的完整輸出 ----------------------------------------------------------------------


def test_coverage_after_snapshot_1(
    coverage_db: TestDatabase,
    database: Database,
    config_dir: Path,
    recorded: list[RecordingDatabase],
) -> None:
    assert _weekly_update(database, config_dir) == 1
    # 另一條路寫入的追蹤範圍外論文（有摘要）：不算。
    _insert_paper(database, config_dir, _work(90, OTHER_ISSN, 2020))

    result = _coverage()

    assert result.exit_code == 0, result.output
    assert _stdout_lines(result.stdout) == _expected(1, _cutoff(coverage_db, 1))
    _no_paper_table(recorded)


def test_next_snapshot_brings_new_numbers(
    coverage_db: TestDatabase,
    database: Database,
    config_dir: Path,
    recorded: list[RecordingDatabase],
) -> None:
    assert _weekly_update(database, config_dir) == 1
    # 快照 1 之後另一條路寫入一篇範圍內論文：還沒算進任何快照，不算數。
    _insert_paper(database, config_dir, _work(91, MISQ_ISSN, 2021))

    first = _coverage()

    assert first.exit_code == 0, first.output
    assert _stdout_lines(first.stdout) == _expected(1, _cutoff(coverage_db, 1))

    # 下一次每週更新把它收進快照 2。
    assert _weekly_update(database, config_dir) == 2

    second = _coverage()

    assert second.exit_code == 0, second.output
    assert _stdout_lines(second.stdout) == _expected(2, _cutoff(coverage_db, 2), misq_2021=2)
    _no_paper_table(recorded)


# --- 沒有快照、設定少了期刊、連不上 -----------------------------------------------------------


def test_no_weekly_run_at_all_exits_1(recorded: list[RecordingDatabase]) -> None:
    result = _coverage()

    assert result.exit_code == 1
    assert "還沒有已發佈的快照" in result.stderr
    assert _stdout_lines(result.stdout) == []


def test_only_fetching_weekly_run_exits_1(
    database: Database, recorded: list[RecordingDatabase]
) -> None:
    weekly_update(SYSTEM, database, FakeJobQueue().enqueue)

    result = _coverage()

    assert result.exit_code == 1
    assert "還沒有已發佈的快照" in result.stderr
    assert _stdout_lines(result.stdout) == []


def test_journal_removed_from_config_exits_1(
    database: Database, config_dir: Path, recorded: list[RecordingDatabase]
) -> None:
    assert _weekly_update(database, config_dir) == 1
    # 套用後把 isr 從 journals.yaml 拿掉。
    (config_dir / "journals.yaml").write_text(
        JOURNALS_YAML.replace(
            "  - key: isr\n    name: Information Systems Research\n    issn_l: 1047-7047\n", ""
        ),
        encoding="utf-8",
    )

    result = _coverage()

    assert result.exit_code == 1
    assert f"計數裡有設定沒有的期刊（ISSN-L：{ISR_ISSN}）" in result.stderr
    assert "config/journals.yaml 改過但論文的追蹤範圍還沒重算" in result.stderr
    assert "把那本期刊加回設定" in result.stderr
    assert _stdout_lines(result.stdout) == []


def test_settings_error_exits_1_without_touching_the_database(
    recorded: list[RecordingDatabase], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL")

    result = _coverage()

    assert result.exit_code == 1
    assert "設定錯誤，沒有讀取覆蓋率" in result.stderr
    assert "DATABASE_URL" in result.stderr
    assert _stdout_lines(result.stdout) == []
    assert recorded == []


def test_unreachable_database_prints_only_the_error_type(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "postgresql://radar_app:secret-password@127.0.0.1:9/radar"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))

    result = _coverage()

    assert result.exit_code == 1
    assert "讀取覆蓋率失敗（" in result.stderr
    assert "secret-password" not in result.output
    assert "127.0.0.1:9" not in result.output
