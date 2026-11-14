"""FR-2 的示範：解析函式多了欄位之後，`radar reparse` 只靠已存的原始資料就把欄位補上。

1. 全新資料庫＋moto 的 bucket（在行程裡模擬 S3，不連網路）。
2. `fetch_journal`＋`FakePaperSource`（頁內容取自錄好的 OpenAlex 回應，CC0）＋真的
   `S3ObjectStore` 存原始資料。
3. 用「舊的解析函式」（真的解析結果去掉 `referenced_works` 與 `cited_by_count`，模擬還沒加這兩個
   欄位）經 `upsert_by_external_id` 寫進論文，確認欄位是空的。
4. 以 `CliRunner` 執行 `radar reparse`：環境變數指到測試資料庫與 moto，
   `bootstrap.build_paper_source` 換成一呼叫就失敗的函式，pytest-socket 只放行本機。
5. 退出碼 0；每篇的 `referenced_works`／`cited_by_count` 等於錄好的值，摘要與關鍵字也寫好。
"""

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from moto import mock_aws
from typer.testing import CliRunner

import radar.entrypoints.bootstrap as bootstrap
import radar.entrypoints.cli as cli
from radar.adapters.fake import FakePaperSource
from radar.adapters.object_store import S3ObjectStore, create_s3_client
from radar.domain.domain_config import Journal, OpenAlexQuota
from radar.domain.papers import PaperOrigin, ParsedPaper
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.raw_records import RawRecord, RawSource
from radar.domain.scope import ScopeSettings
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.papers import upsert_by_external_id
from radar.services.ingest import fetch_journal
from tests.database import APP_ROLE, TestDatabase, migrate
from tests.openalex_cassette import ISSN_L, cassette_pages, work_bytes

ROOT = Path(__file__).resolve().parents[2]
FETCHED_AT = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)
BUCKET = "radar-reparse-test"
ENDPOINT = "https://s3.us-east-1.amazonaws.com"
REGION = "us-east-1"
ACCESS_KEY = "test-access-key"
SECRET_KEY = "test-secret-key"
JOURNAL = Journal(key="io", name="Information and Organization", issn_l=ISSN_L)
QUOTA = OpenAlexQuota(
    daily_budget_usd=Decimal("1.0"),
    stop_at_ratio=Decimal("0.8"),
    default_request_cost_usd=Decimal("0.0001"),
)

runner = CliRunner()


class _SourceCalledError(Exception):
    """reparse 不該建立或呼叫任何外部來源。"""


def _no_paper_source(*_args: Any, **_kwargs: Any) -> Any:
    raise _SourceCalledError


@pytest.fixture
def s3() -> Iterator[Any]:
    with mock_aws():
        client = create_s3_client(
            endpoint_url=ENDPOINT,
            region=REGION,
            access_key_id=ACCESS_KEY,
            secret_access_key=SECRET_KEY,
            timeout=5,
        )
        client.create_bucket(Bucket=BUCKET)
        yield client


def _rows(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(sql, params).fetchall()


def _old_parse(content: bytes) -> ParsedPaper:
    """舊的解析函式：還沒有 `referenced_works` 與 `cited_by_count`。"""
    parsed = parse_openalex_work(content, fetched_at=FETCHED_AT)
    return ParsedPaper.model_validate(
        {**parsed.model_dump(), "referenced_works": (), "cited_by_count": None}
    )


def test_reparse_fills_new_fields_from_stored_raw_data(
    fresh_database: TestDatabase, s3: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    migrate(fresh_database)
    database = Database(fresh_database.app_url, connect_timeout=5, pool_size=2)
    store = S3ObjectStore(
        s3,
        BUCKET,
        attempts=1,
        wait_initial=0.1,
        wait_max=0.1,
        wait_jitter=0,
        retry_after_max=0,
    )

    # 1–2. 抓（替身）→ 存原始資料（真的 S3ObjectStore 對 moto）。
    pages = cassette_pages()
    source = FakePaperSource(
        {
            ISSN_L: [
                [
                    RawRecord(
                        RawSource.OPENALEX,
                        work["id"].rsplit("/", 1)[-1],
                        work_bytes(work),
                        FETCHED_AT,
                    )
                    for work in page
                ]
                for page in pages
            ]
        },
        clock=lambda: FETCHED_AT,
    )
    fetched = fetch_journal(
        SystemViewer(),
        database,
        source,
        store,
        JOURNAL,
        start_year=2016,
        quota=QUOTA,
        clock=lambda: FETCHED_AT,
    )
    works = [work for page in pages for work in page]
    assert fetched.records == len(works) == 248

    # 3. 舊的解析函式寫進論文：兩個欄位是空的。
    scope = ScopeSettings(frozenset({ISSN_L}))
    with database.transaction() as connection:
        for work in works:
            upsert_by_external_id(
                connection,
                _old_parse(work_bytes(work)),
                origin=PaperOrigin.TRACKED,
                excluded_reason=None,
                scope=scope,
            )
    assert _rows(
        fresh_database,
        "SELECT count(*) FROM paper WHERE referenced_works = '{}' AND cited_by_count IS NULL",
    ) == [(248,)]
    assert _rows(fresh_database, "SELECT count(*) FROM paper_keyword") == [(0,)]

    # 4. radar reparse：只連測試資料庫與 moto，建立 PaperSource 就失敗。
    monkeypatch.setattr(bootstrap, "build_paper_source", _no_paper_source)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DATABASE_URL", fresh_database.app_url)
    monkeypatch.setenv("RADAR_OBJECT_STORE_ENDPOINT_URL", ENDPOINT)
    monkeypatch.setenv("RADAR_OBJECT_STORE_REGION", REGION)
    monkeypatch.setenv("RADAR_OBJECT_STORE_BUCKET", BUCKET)
    monkeypatch.setenv("RADAR_OBJECT_STORE_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("RADAR_OBJECT_STORE_SECRET_ACCESS_KEY", SECRET_KEY)
    monkeypatch.delenv("RADAR_OPENALEX_API_KEY", raising=False)

    result = runner.invoke(cli.app, ["reparse"])

    # 5. 欄位都補上了，摘要與關鍵字也寫好；沒有新增論文、暫存表是空的。
    assert result.exit_code == 0, result.output
    assert "更新：248" in result.stdout
    assert _rows(fresh_database, "SELECT count(*) FROM paper") == [(248,)]
    stored = {
        key: (tuple(referenced_works), cited_by_count)
        for key, referenced_works, cited_by_count in _rows(
            fresh_database,
            "SELECT e.value_norm, p.referenced_works, p.cited_by_count FROM paper p"
            " JOIN external_id e ON e.paper_id = p.id AND e.scheme = 'openalex'",
        )
    }
    for work in works:
        key = work["id"].rsplit("/", 1)[-1]
        expected_refs = tuple(
            dict.fromkeys(reference.rsplit("/", 1)[-1] for reference in work["referenced_works"])
        )
        assert stored[key] == (expected_refs, work["cited_by_count"]), key

    with_abstract = sum(1 for work in works if work["abstract_inverted_index"])
    assert _rows(
        fresh_database,
        "SELECT count(*) FROM paper_abstract WHERE is_current AND source = 'openalex'",
    ) == [(with_abstract,)]
    keyword_count = sum(len({k["id"] for k in work["keywords"]}) for work in works)
    assert _rows(
        fresh_database, "SELECT count(*) FROM paper_keyword WHERE source = 'openalex'"
    ) == [(keyword_count,)]
    assert _rows(fresh_database, "SELECT count(*) FROM ingest_staging") == [(0,)]

    # 6. 規則增減只改設定檔、不改程式：複製一份 config/，只在 exclusion_rules.yaml 加一條，
    #    指過去再跑一次；改回原本的設定再跑，結果跟著回來。
    def reasons() -> dict[str | None, int]:
        rows = _rows(fresh_database, "SELECT excluded_reason, count(*) FROM paper GROUP BY 1")
        return {reason: count for reason, count in rows}

    baseline = reasons()
    assert "editorial_board" not in baseline
    custom = tmp_path / "config"
    shutil.copytree(ROOT / "config", custom)
    rules_file = custom / "exclusion_rules.yaml"
    rules_file.write_text(
        rules_file.read_text(encoding="utf-8")
        + "  - reason: editorial_board\n    title_equals:\n      - Editorial Board\n",
        encoding="utf-8",
    )
    editorial_boards = sum(1 for work in works if work["title"] == "Editorial Board")
    assert editorial_boards > 0

    monkeypatch.setenv("RADAR_CONFIG_DIR", str(custom))
    changed = runner.invoke(cli.app, ["reparse"])

    assert changed.exit_code == 0, changed.output
    assert reasons().get("editorial_board") == editorial_boards
    assert _rows(fresh_database, "SELECT count(*) FROM paper") == [(248,)]

    monkeypatch.setenv("RADAR_CONFIG_DIR", str(ROOT / "config"))
    restored = runner.invoke(cli.app, ["reparse"])

    assert restored.exit_code == 0, restored.output
    assert reasons() == baseline
    assert _rows(fresh_database, "SELECT count(*) FROM paper") == [(248,)]
