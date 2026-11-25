"""示範站匯出（Story 3.7、ADR-0007、ADR-0061、AD-20）對真的 PostgreSQL：直接呼叫 service（不經
`radar export-demo` CLI），寫到 pytest 的暫存資料夾。

測試資料全部是假論文（`tests/demo_export_support.py`），每個測試一個全新、已遷移的資料庫。
"""

import dataclasses
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from radar.domain.demo_export import (
    DEMO_LABEL_ABSTRACT_SOURCES,
    MANIFEST_FILE,
    PREVIEW_DIR,
    DemoExportBusyError,
    DemoExportFailure,
    DemoPaperFacts,
    dump_demo_file,
    model_for_path,
)
from radar.domain.papers import AbstractSource
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.papers import list_demo_paper_facts
from radar.services import demo_export as service
from radar.services.demo_export import (
    DemoExportCheckError,
    DemoExportError,
    DemoExportSetup,
    NoPublishedSnapshotError,
    queue_demo_export,
    run_demo_export,
)
from radar.services.members import allow_member
from tests.database import TestDatabase, migrate
from tests.demo_export_support import (
    EXPORTED_AT,
    MISQ,
    PaperSpec,
    demo_setup,
    preview_specs,
    publish_snapshots,
    seed_papers,
)
from tests.llm_support import execute
from tests.permissions.framework import CANARY

MEMBER_EMAIL = "demo.export.member@example.org"


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


SYSTEM = SystemViewer()


def _clock() -> Any:
    return EXPORTED_AT


def _export(
    database: Database, output: Path, setup: DemoExportSetup | None = None
) -> service.DemoExportResult:
    run = queue_demo_export(SYSTEM, database)
    result = run_demo_export(SYSTEM, database, setup or demo_setup(), run.id, output, clock=_clock)
    assert result is not None
    return result


def _files(output: Path) -> dict[str, bytes]:
    preview = output / PREVIEW_DIR
    return {
        path.relative_to(preview).as_posix(): path.read_bytes()
        for path in sorted(preview.rglob("*"))
        if path.is_file()
    }


def _papers(output: Path) -> dict[str, dict[str, Any]]:
    """匯出的分片檔裡每一篇（標題 → 那一篇的 JSON）。"""
    found: dict[str, dict[str, Any]] = {}
    for path, content in _files(output).items():
        if "/papers/" in path:
            for paper in json.loads(content)["papers"]:
                found[paper["biblio"]["biblio"]["title"]] = paper
    return found


def _rows(db: TestDatabase) -> list[tuple[Any, ...]]:
    return execute(
        db, "SELECT sequence, status, failure_reason FROM demo_export ORDER BY created_at, id"
    )


# --- 正常的匯出 -------------------------------------------------------------------------------


def _special_specs() -> list[PaperSpec]:
    return [
        *preview_specs(count=12),
        PaperSpec("excluded_not_counted", 90, excluded_reason="editors_comments"),
    ]


def test_export_writes_a_new_preview_directory_and_the_manifest_last(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    snapshots = publish_snapshots(db)
    seed_papers(db, database, _special_specs(), snapshots=snapshots)

    result = _export(database, tmp_path)

    files = _files(tmp_path)
    manifest = json.loads(files[MANIFEST_FILE])
    assert result.export_id == manifest["export_id"] == "2-1"
    assert result.manifest_path == tmp_path / PREVIEW_DIR / MANIFEST_FILE
    # 只寫 preview/（沒有 data/），沒有留下暫存檔；每個檔都是認得的資料檔、通過它的模型。
    assert [path.name for path in tmp_path.iterdir()] == [PREVIEW_DIR]
    assert sorted(files) == sorted(
        [
            MANIFEST_FILE,
            "2-1/taxonomy.json",
            "2-1/papers-index.json",
            "2-1/titles.json",
            "2-1/papers/000.json",
        ]
    )
    for path, content in files.items():
        model = model_for_path(path)
        assert model is not None, path
        model.model_validate_json(content)
    # manifest 列的檔與雜湊和寫出來的一樣；每個檔最外層都有快照與匯出編號。
    assert {entry["path"]: entry["sha256"] for entry in manifest["files"]} == {
        path: hashlib.sha256(content).hexdigest()
        for path, content in files.items()
        if path != MANIFEST_FILE
    }
    for path, content in files.items():
        data = json.loads(content)
        assert data["snapshot"] == {"number": 2, "as_of": "2026-11-16T03:00:00Z"}, path
        assert data["export_id"] == "2-1", path
    assert manifest["exported_at"] == "2026-11-16T06:00:00Z"
    # 12 篇計入統計（被排除的那篇不算）。有摘要：第 2、10、12 篇以外的 9 篇；本週新增：第 1、12 篇；
    # 有完整內容（CC BY、出版社可以放、有 OpenAlex 的摘要）：第 1、4、5、6、11 篇。
    assert manifest["summary"] == {
        "paper_count": 12,
        "with_abstract_count": 9,
        "new_this_week_count": 2,
        "full_content_count": 5,
        "first_year": 2022,
        "last_year": 2025,
    }
    assert _rows(db) == [(1, "succeeded", None)]


def test_each_kind_of_paper_has_only_what_it_may_show(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    snapshots = publish_snapshots(db)
    ids = seed_papers(db, database, _special_specs(), snapshots=snapshots)

    _export(database, tmp_path)

    papers = _papers(tmp_path)
    by_name = {paper["biblio"]["biblio"]["id"]: paper for paper in papers.values()}
    named = {name: by_name.get(str(paper_id)) for name, paper_id in ids.items()}

    # 有完整內容：摘要、中文重點、研究模型都在第二層；第一層標示有完整內容。
    full = named["full_content"]
    assert full is not None
    assert full["biblio"]["has_full_content"] is True
    assert full["full"]["abstract"]["source"] == "openalex"
    assert full["full"]["zh_summary"]["state"] == "ready"
    assert full["full"]["research_model"]["state"] == "ready"
    assert full["biblio"]["classification"]["value"]["topic"]["keys"] == ["erp"]
    assert full["biblio"]["is_new_this_week"] is True
    # 有完整內容、AI 還沒產生：第二層照成員版的狀態（預算不讀，見 ADR-0061）。
    pending = named["full_content_ai_pending"]
    assert pending is not None
    assert pending["full"] is not None
    assert pending["full"]["zh_summary"]["state"] == "pending_batch"
    assert pending["full"]["zh_summary"]["value"] is None

    # 只有書目與分類標籤：IEEE（出版社對上排除清單；出版社沒對上但期刊是 0018-9391）、授權不允許、
    # 授權允許但沒有摘要、只有標題分類的。
    for name in (
        "ieee_excluded_publisher",
        "ieee_journal_only",
        "biblio_only_not_open",
        "eligible_without_abstract",
        "title_only_classification",
    ):
        paper = named[name]
        assert paper is not None, name
        assert paper["full"] is None, name
        assert paper["biblio"]["has_full_content"] is False, name
        assert paper["biblio"]["classification"]["state"] == "ready", name
    assert named["ieee_journal_only"]["biblio"]["biblio"]["journal"] == "tem"  # type: ignore[index]
    assert named["eligible_without_abstract"]["biblio"]["has_abstract"] is False  # type: ignore[index]
    title_only = named["title_only_classification"]["biblio"]["classification"]  # type: ignore[index]
    assert title_only["provenance"]["input_kind"] == "title"

    # 成員修正過的主題：輸出修正後的值、標成已核對；誰修正的不在白名單裡。
    verified = named["human_verified_topic"]["biblio"]["classification"]["value"]  # type: ignore[index]
    assert verified["topic"] == {
        "keys": ["it_strategy_digital_transformation"],
        "human_verified": True,
        "fix_member_count": 1,
        "members_disagree": False,
    }

    # 摘要來源不允許公開：沒有完整內容，也沒有分類標籤。
    other = named["other_source_abstract"]
    assert other is not None
    assert other["full"] is None
    assert other["biblio"]["classification"] is None
    assert other["biblio"]["has_abstract"] is True

    # AI 沒有產生成功、還沒有產出。
    assert named["classification_failed"]["biblio"]["classification"] == {  # type: ignore[index]
        "state": "failed",
        "value": None,
        "provenance": None,
    }
    assert named["no_ai_output_yet"]["biblio"]["classification"]["state"] == "pending_batch"  # type: ignore[index]

    # 被排除、不計入統計的論文完全不在匯出裡。
    assert named["excluded_not_counted"] is None

    # 摘要原文只出現在有完整內容的論文：別的論文的摘要字串不在任何檔裡。
    text = b"".join(_files(tmp_path).values()).decode("utf-8")
    assert "Fake abstract from another source" not in text
    assert "Made up abstract 1" in text  # 有完整內容的第 1 篇
    for number in (3, 7, 8):  # 授權不允許、IEEE 兩篇
        assert f"Made up abstract {number}" not in text
    # 中文重點只在有完整內容的論文出現（三篇只有書目的論文也有中文重點，但不輸出）。
    assert text.count("（假資料）團隊怎麼採用協作工具？") == 1


def test_index_titles_and_shards_share_the_row_order(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    snapshots = publish_snapshots(db)
    seed_papers(db, database, preview_specs(count=205), snapshots=snapshots)

    _export(database, tmp_path)

    files = _files(tmp_path)
    index = json.loads(files["2-1/papers-index.json"])
    titles = json.loads(files["2-1/titles.json"])["titles"]
    shards = [json.loads(files[f"2-1/papers/{n:03d}.json"]) for n in (0, 1)]
    assert "2-1/papers/002.json" not in files
    assert [len(shard["papers"]) for shard in shards] == [200, 5]
    rows = [paper for shard in shards for paper in shard["papers"]]
    assert [paper["biblio"]["biblio"]["id"] for paper in rows] == index["id"]
    assert [paper["biblio"]["biblio"]["title"] for paper in rows] == titles
    assert [paper["biblio"]["has_full_content"] for paper in rows] == index["has_full_content"]
    # 出版年新到舊。
    years = index["year"]
    assert years == sorted(years, reverse=True)


def test_label_sources_match_the_view(db: TestDatabase, database: Database) -> None:
    # 分類標籤可公開的摘要來源（Python 的清單）和 view `demo_full_paper` 的「摘要來源允許公開」
    # 一致：示範站可用、有目前的摘要的論文，摘要來源在清單裡 ⇔ 在 view 裡。
    snapshots = publish_snapshots(db)
    ids = seed_papers(
        db,
        database,
        [
            PaperSpec("openalex", 1),
            PaperSpec("semantic_scholar", 2, abstract=False, s2_abstract=True),
        ],
        snapshots=snapshots,
    )
    in_view = {uuid.UUID(str(row[0])) for row in execute(db, "SELECT id FROM demo_full_paper")}
    current_sources = {AbstractSource.OPENALEX, AbstractSource.SEMANTIC_SCHOLAR}
    for source in current_sources:
        assert (ids[source.value] in in_view) == (source in DEMO_LABEL_ABSTRACT_SOURCES)


def test_second_export_of_the_same_snapshot_gets_a_new_directory(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)

    first = _export(database, tmp_path)
    old = _files(tmp_path)
    second = _export(database, tmp_path)

    assert (first.export_id, second.export_id) == ("2-1", "2-2")
    files = _files(tmp_path)
    # 舊的目錄原封不動，manifest 換成指向新的。
    assert {path: content for path, content in files.items() if path.startswith("2-1/")} == {
        path: content for path, content in old.items() if path.startswith("2-1/")
    }
    assert json.loads(files[MANIFEST_FILE])["export_id"] == "2-2"
    assert _rows(db) == [(1, "succeeded", None), (2, "succeeded", None)]


def test_one_export_in_progress_per_snapshot(db: TestDatabase, database: Database) -> None:
    with pytest.raises(NoPublishedSnapshotError):
        queue_demo_export(SYSTEM, database)
    publish_snapshots(db)
    queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportBusyError):
        queue_demo_export(SYSTEM, database)
    # 部分唯一索引本身也擋（不經 service）。
    with pytest.raises(psycopg.errors.UniqueViolation):
        execute(
            db,
            "INSERT INTO demo_export (snapshot_id, sequence, status)"
            " SELECT snapshot_id, 9, 'running' FROM demo_export LIMIT 1",
        )


def test_all_reads_happen_in_one_read_only_repeatable_read_transaction(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = publish_snapshots(db)
    seed_papers(db, database, preview_specs(count=3), snapshots=snapshots)
    seen: list[tuple[str, str, str, int]] = []

    def spy(name: str, real: Any) -> Any:
        def wrapper(viewer: Any, connection: Any, *args: Any, **kwargs: Any) -> Any:
            isolation = connection.exec_driver_sql("SHOW transaction_isolation").scalar()
            read_only = connection.exec_driver_sql("SHOW transaction_read_only").scalar()
            pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar()
            seen.append((name, isolation, read_only, pid))
            return real(viewer, connection, *args, **kwargs)

        return wrapper

    for name in (
        "find_latest_published_snapshot",
        "list_demo_paper_facts",
        "find_paper_llm_source",
        "find_ai_block",
        "list_demo_full_paper_ids",
        "list_member_emails",
    ):
        monkeypatch.setattr(service, name, spy(name, getattr(service, name)))

    _export(database, tmp_path)

    assert {name for name, *_ in seen} == {
        "find_latest_published_snapshot",
        "list_demo_paper_facts",
        "find_paper_llm_source",
        "find_ai_block",
        "list_demo_full_paper_ids",
        "list_member_emails",
    }
    assert {(isolation, read_only) for _, isolation, read_only, _ in seen} == {
        ("repeatable read", "on")
    }
    # 同一條連線（同一個交易）。
    assert len({pid for *_, pid in seen}) == 1


# --- 自我檢查沒過：不換 manifest、什麼都不寫、那一筆記成失敗 ------------------------------------


def _assert_nothing_written(db: TestDatabase, output: Path) -> None:
    assert not (output / PREVIEW_DIR / MANIFEST_FILE).exists()
    assert not any(path.is_file() for path in output.rglob("*"))
    assert _rows(db)[-1][1:] == ("failed", DemoExportFailure.SELF_CHECK_FAILED.value)


def _check_fails(database: Database, output: Path, setup: DemoExportSetup | None = None) -> str:
    run = queue_demo_export(SYSTEM, database)
    with pytest.raises(DemoExportCheckError) as caught:
        run_demo_export(SYSTEM, database, setup or demo_setup(), run.id, output, clock=_clock)
    return "\n".join(caught.value.problems)


def test_canary_in_the_output_fails_the_check(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    snapshots = publish_snapshots(db)
    seed_papers(
        db, database, [PaperSpec("canary", 1, title=f"Fake Paper {CANARY}")], snapshots=snapshots
    )

    problems = _check_fails(database, tmp_path, demo_setup(canaries=[CANARY]))

    assert "出現金絲雀字串" in problems
    assert CANARY not in problems
    _assert_nothing_written(db, tmp_path)


def test_member_email_in_the_output_fails_the_check(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    snapshots = publish_snapshots(db)
    allow_member(SystemViewer(), database, email=MEMBER_EMAIL, display_name="測試成員")
    # 大小寫不同也算。
    seed_papers(
        db,
        database,
        [PaperSpec("email", 1, title=f"Fake Paper by {MEMBER_EMAIL.upper()}")],
        snapshots=snapshots,
    )

    problems = _check_fails(database, tmp_path)

    assert "出現成員的 email" in problems
    assert MEMBER_EMAIL not in problems.casefold()
    _assert_nothing_written(db, tmp_path)


def test_full_content_outside_demo_full_paper_fails_the_check(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 組檔那一段誤以為一篇授權不允許的論文有完整內容：自我檢查另外讀 view，擋下來。
    snapshots = publish_snapshots(db)
    ids = seed_papers(
        db, database, [PaperSpec("not_open", 1, license="cc-by-nc")], snapshots=snapshots
    )
    real = list_demo_paper_facts

    def wrong(viewer: Any, connection: Any) -> list[DemoPaperFacts]:
        return [
            DemoPaperFacts(**{**_fields(item), "in_demo_full_paper": True})
            for item in real(viewer, connection)
        ]

    monkeypatch.setattr(service, "list_demo_paper_facts", wrong)

    problems = _check_fails(database, tmp_path)

    assert f"不在 demo_full_paper 的論文 {ids['not_open']} 帶了完整內容" in problems
    assert f"不在 demo_full_paper 的論文 {ids['not_open']} 標成有完整內容" in problems
    _assert_nothing_written(db, tmp_path)


def _fields(facts: DemoPaperFacts) -> dict[str, Any]:
    return {name: getattr(facts, name) for name in DemoPaperFacts.__slots__}


def test_field_outside_the_whitelist_fails_the_check(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 組檔那一段多寫了一個欄位：自我檢查用白名單模型讀回來，擋下來。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1, issn_l=MISQ)], snapshots=snapshots)
    real = dump_demo_file

    def leaky(model: Any) -> bytes:
        data = json.loads(real(model))
        data["member_note"] = "extra"
        return json.dumps(data).encode()

    monkeypatch.setattr(service, "dump_demo_file", leaky)

    problems = _check_fails(database, tmp_path)

    assert "2-1/papers-index.json：不符合白名單模型 DemoPapersIndex" in problems
    assert "extra_forbidden" in problems
    _assert_nothing_written(db, tmp_path)


# --- 其他失敗 ---------------------------------------------------------------------------------


def test_publish_switch_is_refused_and_nothing_is_written(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    setup = demo_setup()
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportError) as caught:
        run_demo_export(
            SYSTEM,
            database,
            dataclasses.replace(setup, publish=True),
            run.id,
            tmp_path,
            clock=_clock,
        )

    assert caught.value.failure is DemoExportFailure.PUBLISH_NOT_READY
    assert list(tmp_path.iterdir()) == []
    assert _rows(db) == [(1, "failed", "publish_not_ready")]


def test_existing_directory_is_not_overwritten(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    (tmp_path / PREVIEW_DIR / "2-1").mkdir(parents=True)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert caught.value.failure is DemoExportFailure.OUTPUT_EXISTS
    assert not (tmp_path / PREVIEW_DIR / MANIFEST_FILE).exists()
    assert list((tmp_path / PREVIEW_DIR / "2-1").iterdir()) == []


def test_finished_export_is_not_run_again(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    run = queue_demo_export(SYSTEM, database)
    assert (
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock) is not None
    )

    assert run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock) is None
    assert (
        run_demo_export(SYSTEM, database, demo_setup(), uuid.uuid4(), tmp_path, clock=_clock)
        is None
    )
