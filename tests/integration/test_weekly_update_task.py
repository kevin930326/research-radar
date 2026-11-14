"""worker 登記的 `run_weekly_update` 工作（`register_tasks` 裡的那個函式，照正式的 bootstrap
建 adapter）對真的 PostgreSQL：缺 OpenAlex 金鑰、物件儲存設定不齊、領域設定壞掉時，
`weekly_run.updated_at` 有更新、例外照樣往上丟；已經抓完的那一筆被補排時不讀設定、不建 adapter
（對抗式審查第 1 條）。

不跑 Procrastinate worker、不連網路（建不起來的 adapter 在連線之前就丟錯）；環境變數只用測試值。
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

import radar.entrypoints.worker.tasks as worker_tasks
from radar.config import ConfigError
from radar.domain.jobs import RUN_WEEKLY_UPDATE
from radar.domain.settings import Settings
from radar.entrypoints.bootstrap import build_worker
from tests.database import APP_ROLE, OWNER_ROLE, ROOT, TestDatabase, migrate

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


def test_finished_run_builds_no_adapter_and_reads_no_config(
    weekly_db: TestDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 設定資料夾不存在、也沒有任何金鑰：只要讀了設定或建了 adapter 就會丟錯。
    task = _task(weekly_db, monkeypatch, tmp_path / "missing-config")
    built: list[str] = []
    monkeypatch.setattr(worker_tasks, "build_paper_source", lambda *_: built.append("source"))
    monkeypatch.setattr(worker_tasks, "build_object_store", lambda *_: built.append("store"))
    run_id, before = _fetching_run(weekly_db, finished=True)

    with capture_logs() as logs:
        task(weekly_run_id=str(run_id))

    assert built == []
    # 已經抓完：只更新 updated_at（sweep 2 小時後才再補排）。
    assert _updated_at(weekly_db, run_id) > before
    assert [log["event"] for log in logs] == ["每週更新已經抓完，不再抓"]


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
