"""worker 登記的 `run_weekly_update` 工作（`register_tasks` 裡的那個函式，照正式的 bootstrap
建 adapter）對真的 PostgreSQL：缺 OpenAlex 金鑰、物件儲存設定不齊、領域設定壞掉時，
`weekly_run.updated_at` 有更新、例外照樣往上丟；已經抓完的那一筆被補排時不建 adapter，只讀追蹤範圍
接著套用（對抗式審查第 1 條、Story 1.9）；抓完後 `journals.yaml` 壞掉時準備套用失敗；
在 `journals.yaml` 多加一本期刊、不改程式，那本的論文就進論文表並指向快照 1（NFR-9）。

不跑 Procrastinate worker、不連網路（建不起來的 adapter 在連線之前就丟錯；NFR-9 那一條把
`build_paper_source`／`build_object_store` 換成替身）；環境變數只用測試值。
"""

import contextlib
import shutil
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

import radar.entrypoints.worker.tasks as worker_tasks
from radar.adapters.fake import FakeObjectStore, FakePaperSource
from radar.config import ConfigError
from radar.domain.jobs import RUN_WEEKLY_UPDATE
from radar.domain.raw_records import RawRecord, RawSource
from radar.domain.settings import Settings
from radar.entrypoints.bootstrap import build_worker
from tests.database import APP_ROLE, OWNER_ROLE, ROOT, TestDatabase, migrate
from tests.demo_works import demo_work, demo_work_bytes
from tests.openalex_cassette import cassette_work, work_bytes

OBJECT_STORE = {
    "object_store_endpoint_url": "http://127.0.0.1:9",
    "object_store_region": "us-east-1",
    "object_store_bucket": "radar-test",
    "object_store_access_key_id": "test-access-key",
    "object_store_secret_access_key": "test-secret-key",
}


@pytest.fixture
def weekly_db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _task(
    db: TestDatabase,
    monkeypatch: pytest.MonkeyPatch,
    config_dir: Path,
    **settings: Any,
) -> Callable[..., None]:
    """照正式的 `register_tasks` 登記，回傳 `run_weekly_update` 工作的函式。"""
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))
    worker_settings = Settings.model_validate({"database_url": db.app_url, **settings})
    parts = build_worker(worker_settings, queue_pool_size=1, app_pool_size=1, heartbeat_urls={})
    worker_tasks.register_tasks(parts.job_app.app, parts, worker_settings)
    func: Callable[..., None] = parts.job_app.app.tasks[RUN_WEEKLY_UPDATE.name].func
    return func


def _fetching_run(db: TestDatabase, *, finished: bool = False) -> tuple[uuid.UUID, datetime]:
    """寫一筆 `fetching` 的 `weekly_run`，`updated_at` 改到一天前；回傳 id 與改好的時間。"""
    with db.connect(APP_ROLE) as connection:
        row = connection.execute(
            "INSERT INTO weekly_run (status) VALUES ('fetching') RETURNING id"
        ).fetchone()
    assert row is not None
    run_id: uuid.UUID = row[0]
    with db.connect(OWNER_ROLE) as connection:
        connection.execute(
            "UPDATE weekly_run SET updated_at = now() - interval '1 day',"
            " fetch_finished_at = CASE WHEN %s THEN now() END WHERE id = %s",
            (finished, run_id),
        )
    return run_id, _updated_at(db, run_id)


def _updated_at(db: TestDatabase, run_id: uuid.UUID) -> datetime:
    with db.connect(APP_ROLE) as connection:
        row = connection.execute(
            "SELECT updated_at FROM weekly_run WHERE id = %s", (run_id,)
        ).fetchone()
    assert row is not None
    updated_at: datetime = row[0]
    return updated_at


def _progress_rows(db: TestDatabase) -> int:
    with db.connect(APP_ROLE) as connection:
        row = connection.execute("SELECT count(*) FROM weekly_run_journal").fetchone()
    assert row is not None
    count: int = row[0]
    return count


def _broken_config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    for name in ("journals.yaml", "openalex.yaml", "exclusion_rules.yaml", "keyword_sources.yaml"):
        (directory / name).write_text((ROOT / "config" / name).read_text(encoding="utf-8"))
    (directory / "journals.yaml").write_text("journals: [\n", encoding="utf-8")
    return directory


@pytest.mark.parametrize(
    ("settings", "broken_config", "missing"),
    [
        (OBJECT_STORE, False, "RADAR_OPENALEX_API_KEY"),
        ({"openalex_api_key": "test-openalex-key"}, False, "RADAR_OBJECT_STORE_ENDPOINT_URL"),
        ({"openalex_api_key": "test-openalex-key", **OBJECT_STORE}, True, "journals.yaml"),
    ],
    ids=["no-openalex-key", "no-object-store", "broken-journals-yaml"],
)
def test_adapter_setup_failure_touches_the_run_and_raises(
    weekly_db: TestDatabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    settings: dict[str, str],
    broken_config: bool,
    missing: str,
) -> None:
    config_dir = _broken_config_dir(tmp_path) if broken_config else ROOT / "config"
    task = _task(weekly_db, monkeypatch, config_dir, **settings)
    run_id, before = _fetching_run(weekly_db)

    with capture_logs() as logs, pytest.raises(ConfigError) as caught:
        task(weekly_run_id=str(run_id))

    assert missing in str(caught.value)
    # 建 adapter 失敗也更新 updated_at：sweep 照 2 小時的時限補排，不會每 15 分鐘就補排一次。
    assert _updated_at(weekly_db, run_id) > before
    assert _progress_rows(weekly_db) == 0
    [error] = [log for log in logs if log["log_level"] == "error"]
    assert error["event"] == "每週更新準備抓取失敗"
    assert (error["weekly_run_id"], error["error"]) == (str(run_id), "ConfigError")
    assert "test-secret-key" not in str(logs)


def _failure_count(db: TestDatabase, run_id: uuid.UUID) -> int:
    with db.connect(APP_ROLE) as connection:
        row = connection.execute(
            "SELECT failure_count FROM weekly_run WHERE id = %s", (run_id,)
        ).fetchone()
    assert row is not None
    count: int = row[0]
    return count


def test_setup_failures_up_to_the_configured_limit_mark_the_run_failed(
    weekly_db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Story 1.11：照正式的 register_tasks 登記、設定上限 2；缺 OpenAlex 金鑰讓準備抓取每次都失敗。
    task = _task(
        weekly_db, monkeypatch, ROOT / "config", weekly_update_max_failures=2, **OBJECT_STORE
    )
    run_id, _before = _fetching_run(weekly_db)

    with capture_logs() as logs, pytest.raises(ConfigError):
        task(weekly_run_id=str(run_id))

    assert (_status(weekly_db, run_id), _failure_count(weekly_db, run_id)) == ("fetching", 1)
    [warning] = [log for log in logs if log["event"] == "每週更新失敗，已記一次"]
    assert (warning["failures"], warning["max_failures"]) == (1, 2)

    with capture_logs() as logs, pytest.raises(ConfigError):
        task(weekly_run_id=str(run_id))

    assert (_status(weekly_db, run_id), _failure_count(weekly_db, run_id)) == ("failed", 2)
    [error] = [log for log in logs if log["event"] == "每週更新重試到上限，標成失敗"]
    assert (error["log_level"], error["failures"], error["max_failures"]) == ("error", 2, 2)
    assert _snapshots(weekly_db) == []

    # 之後的工作重試或 sweep 補排：不是 fetching，略過、正常結束（不丟錯、不再加次數）。
    with capture_logs() as logs:
        task(weekly_run_id=str(run_id))

    assert [log["event"] for log in logs] == ["每週更新不是抓取中，略過"]
    assert _failure_count(weekly_db, run_id) == 2


def _no_adapters(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把建 adapter 的兩個函式換成「被呼叫就記下來」的版本；回傳記錄。"""
    built: list[str] = []
    monkeypatch.setattr(worker_tasks, "build_paper_source", lambda *_: built.append("source"))
    monkeypatch.setattr(worker_tasks, "build_object_store", lambda *_: built.append("store"))
    return built


def _status(db: TestDatabase, run_id: uuid.UUID) -> str:
    with db.connect(APP_ROLE) as connection:
        row = connection.execute(
            "SELECT status FROM weekly_run WHERE id = %s", (run_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def _snapshots(db: TestDatabase) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(
            "SELECT id, number, weekly_run_id, status FROM snapshot ORDER BY number"
        ).fetchall()


def test_finished_run_builds_no_adapter_and_applies(
    weekly_db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 沒有任何金鑰：只要建了 adapter 就會丟錯。已經抓完的那一筆只讀追蹤範圍（正式的 config/），
    # 接著套用：沒有暫存列也照樣發佈快照、送心跳（沒設心跳網址，只記警告）。
    task = _task(weekly_db, monkeypatch, ROOT / "config")
    built = _no_adapters(monkeypatch)
    run_id, before = _fetching_run(weekly_db, finished=True)

    with capture_logs() as logs:
        task(weekly_run_id=str(run_id))

    assert built == []
    assert _updated_at(weekly_db, run_id) > before
    assert _status(weekly_db, run_id) == "published"
    assert [(number, run, status) for _id, number, run, status in _snapshots(weekly_db)] == [
        (1, run_id, "published")
    ]
    # 這一筆暫存是 0 列，所以有「沒有任何暫存論文」的警告（1.9 審查第 6 條）。
    # Story 2.8：心跳之後排入向量化；這裡沒有開 Procrastinate 的 app，排入失敗只記警告、
    # 快照照樣發佈（排入成功的情形見 tests/integration/test_embed_task.py）。
    assert [log["event"] for log in logs] == [
        "每週更新已經抓完，不再抓",
        "這個排程沒有設定心跳網址，不送",
        "每週更新發佈後排入向量化失敗",
        # Story 2.9：接著排入 Batch 規劃，一樣排不進去、只記警告。
        "每週更新發佈後排入 Batch 規劃失敗",
        "這次每週更新沒有任何暫存論文",
        "每週更新已套用並發佈快照",
    ]
    assert logs[1]["check"] == "weekly-update"
    assert logs[4]["log_level"] == "warning"


def test_broken_journals_yaml_after_fetch_fails_to_prepare_the_apply(
    weekly_db: TestDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 抓完之後 journals.yaml 壞掉：讀追蹤範圍失敗，updated_at 更新、例外照樣往上丟、沒有快照。
    task = _task(weekly_db, monkeypatch, _broken_config_dir(tmp_path))
    built = _no_adapters(monkeypatch)
    run_id, before = _fetching_run(weekly_db, finished=True)

    with capture_logs() as logs, pytest.raises(ConfigError, match=r"journals\.yaml"):
        task(weekly_run_id=str(run_id))

    assert built == []
    assert _updated_at(weekly_db, run_id) > before
    assert _status(weekly_db, run_id) == "fetching"
    assert _snapshots(weekly_db) == []
    [error] = [log for log in logs if log["log_level"] == "error"]
    assert error["event"] == "每週更新準備套用失敗"
    assert (error["weekly_run_id"], error["error"]) == (str(run_id), "ConfigError")


NEW_ISSN = "2345-6780"
NEW_JOURNAL_YAML = """  - key: newj
    name: Journal Added By Config Only
    issn_l: 2345-6780
"""


def test_a_journal_added_only_in_journals_yaml_is_imported(
    weekly_db: TestDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # NFR-9：複製 config/，只在 journals.yaml 加一本期刊；照正式的 register_tasks 跑工作
    # （OpenAlex 與物件儲存換成替身）→ 那本的論文進論文表、在追蹤範圍內、指向快照 1。
    custom = tmp_path / "config"
    shutil.copytree(ROOT / "config", custom)
    journals_file = custom / "journals.yaml"
    journals_file.write_text(
        journals_file.read_text(encoding="utf-8") + NEW_JOURNAL_YAML, encoding="utf-8"
    )
    fetched_at = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
    records = []
    for number in range(1, 4):
        work = cassette_work()
        work["id"] = f"https://openalex.org/W7{number:08d}"
        work["doi"] = f"https://doi.org/10.9999/radar-new-journal.{number}"
        work["publication_year"] = 2024
        work["primary_location"]["source"]["issn_l"] = NEW_ISSN
        work["title"] = work["display_name"] = f"New journal study {number}"
        records.append(
            RawRecord(RawSource.OPENALEX, f"W7{number:08d}", work_bytes(work), fetched_at)
        )
    # 其他 14 本各回一頁空結果。
    source = FakePaperSource({NEW_ISSN: [records]})
    task = _task(weekly_db, monkeypatch, custom)
    monkeypatch.setattr(
        worker_tasks, "build_paper_source", lambda *_: contextlib.nullcontext(source)
    )
    monkeypatch.setattr(worker_tasks, "build_object_store", lambda *_: FakeObjectStore())
    run_id, _before = _fetching_run(weekly_db)

    task(weekly_run_id=str(run_id))

    assert {request.query.issn_l for request in source.requests} >= {NEW_ISSN}
    assert len(source.requests) == 15
    [(snapshot_id, number, _run, status)] = _snapshots(weekly_db)
    assert (number, status) == (1, "published")
    with weekly_db.connect(APP_ROLE) as connection:
        papers = connection.execute(
            "SELECT title, journal_issn_l, in_scope, first_snapshot_id FROM paper ORDER BY title"
        ).fetchall()
    assert papers == [
        (f"New journal study {number}", NEW_ISSN, True, snapshot_id) for number in range(1, 4)
    ]
    assert _status(weekly_db, run_id) == "published"


def test_weekly_update_marks_demo_eligible_with_the_real_exclusion_list(
    weekly_db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Story 3.7a：照正式的 register_tasks 跑（第一次匯入也是這條路），讀正式的
    # config/demo_publishers.yaml：CC BY 的編的出版社算可用；名稱是 IEEE 的 CC BY 不算。
    issn = "1471-7727"
    fetched_at = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
    works = [
        demo_work(1, issn_l=issn),
        demo_work(2, issn_l=issn, publisher_id=None, publisher_name="IEEE"),
    ]
    records = [
        RawRecord(RawSource.OPENALEX, f"W8{n:08d}", demo_work_bytes(work), fetched_at)
        for n, work in enumerate(works, start=1)
    ]
    source = FakePaperSource({issn: [records]})
    task = _task(weekly_db, monkeypatch, ROOT / "config")
    monkeypatch.setattr(
        worker_tasks, "build_paper_source", lambda *_: contextlib.nullcontext(source)
    )
    monkeypatch.setattr(worker_tasks, "build_object_store", lambda *_: FakeObjectStore())
    run_id, _before = _fetching_run(weekly_db)

    task(weekly_run_id=str(run_id))

    with weekly_db.connect(APP_ROLE) as connection:
        papers = connection.execute(
            "SELECT title, primary_license, demo_eligible FROM paper ORDER BY title"
        ).fetchall()
    assert papers == [
        ("Made up demo study 1", "cc-by", True),
        ("Made up demo study 2", "cc-by", False),
    ]


def test_weekly_update_excludes_ieee_tem_by_journal_with_the_real_config(
    weekly_db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C3（ADR-0052 更新 2026-11-22）：照正式的 register_tasks 跑、讀正式
    # config/demo_publishers.yaml 的期刊清單。IEEE TEM（0018-9391）的 CC BY 論文，出版社名稱
    # 是清單外的寫法、ID 不在清單裡，也不算可用；同樣的出版社欄位在別的期刊算可用。
    tem, other = "0018-9391", "1471-7727"
    unlisted_id = "https://openalex.org/P9000000001"
    unlisted_name = "The Institute of Electrical and Electronics Engineers"
    fetched_at = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
    tem_record = RawRecord(
        RawSource.OPENALEX,
        "W800000001",
        demo_work_bytes(
            demo_work(1, issn_l=tem, publisher_id=unlisted_id, publisher_name=unlisted_name)
        ),
        fetched_at,
    )
    other_record = RawRecord(
        RawSource.OPENALEX,
        "W800000002",
        demo_work_bytes(
            demo_work(2, issn_l=other, publisher_id=unlisted_id, publisher_name=unlisted_name)
        ),
        fetched_at,
    )
    source = FakePaperSource({tem: [[tem_record]], other: [[other_record]]})
    task = _task(weekly_db, monkeypatch, ROOT / "config")
    monkeypatch.setattr(
        worker_tasks, "build_paper_source", lambda *_: contextlib.nullcontext(source)
    )
    monkeypatch.setattr(worker_tasks, "build_object_store", lambda *_: FakeObjectStore())
    run_id, _before = _fetching_run(weekly_db)

    task(weekly_run_id=str(run_id))

    with weekly_db.connect(APP_ROLE) as connection:
        papers = connection.execute(
            "SELECT title, journal_issn_l, primary_license, demo_eligible FROM paper ORDER BY title"
        ).fetchall()
    assert papers == [
        ("Made up demo study 1", tem, "cc-by", False),
        ("Made up demo study 2", other, "cc-by", True),
    ]


BAD_HOST = "bad host!.abc123account.r2.example"


def test_adapter_setup_error_does_not_carry_the_endpoint(
    weekly_db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # botocore 對這個網址丟 `ValueError: Invalid endpoint: <整個網址>`；Procrastinate 記失敗時
    # 會印例外與它串著的例外，所以往上丟的例外與 __cause__／__context__ 都不能帶主機
    # （確認審查第 8 條）。
    settings = {
        "openalex_api_key": "test-openalex-key",
        **OBJECT_STORE,
        "object_store_endpoint_url": f"http://{BAD_HOST}:9000",
    }
    task = _task(weekly_db, monkeypatch, ROOT / "config", **settings)
    run_id, before = _fetching_run(weekly_db)

    with capture_logs() as logs, pytest.raises(worker_tasks.AdapterSetupError) as caught:
        task(weekly_run_id=str(run_id))

    error = caught.value
    assert error.error_type == "ValueError"
    assert error.__cause__ is None
    assert error.__context__ is None
    for text in (str(error), repr(error), *map(str, error.args)):
        assert BAD_HOST not in text
    assert BAD_HOST not in str(logs)
    assert _updated_at(weekly_db, run_id) > before
    assert _progress_rows(weekly_db) == 0
    [logged] = [log for log in logs if log["log_level"] == "error"]
    assert (logged["weekly_run_id"], logged["error"]) == (str(run_id), "AdapterSetupError")


def test_adapter_setup_error_log_carries_the_cause_class_but_not_the_endpoint(
    weekly_db: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 日誌除了包裝類別 `AdapterSetupError`，也帶原本的錯誤類別（`cause`），才看得出是哪一種
    # 錯誤；但仍然只有類別名稱，不帶網址或例外訊息（K7）。
    settings = {
        "openalex_api_key": "test-openalex-key",
        **OBJECT_STORE,
        "object_store_endpoint_url": f"http://{BAD_HOST}:9000",
    }
    task = _task(weekly_db, monkeypatch, ROOT / "config", **settings)
    run_id, _before = _fetching_run(weekly_db)

    with capture_logs() as logs, pytest.raises(worker_tasks.AdapterSetupError):
        task(weekly_run_id=str(run_id))

    [logged] = [log for log in logs if log["log_level"] == "error"]
    assert logged["event"] == "每週更新準備抓取失敗"
    assert (logged["error"], logged["cause"]) == ("AdapterSetupError", "ValueError")
    assert BAD_HOST not in str(logs)
    assert "abc123account" not in str(logs)
