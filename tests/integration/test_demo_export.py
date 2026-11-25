"""示範站匯出（Story 3.7、ADR-0007、ADR-0061、AD-20）對真的 PostgreSQL：直接呼叫 service（不經
`radar export-demo` CLI），寫到 pytest 的暫存資料夾。

測試資料全部是假論文（`tests/demo_export_support.py`），每個測試一個全新、已遷移的資料庫。
"""

import dataclasses
import errno
import fcntl
import hashlib
import json
import os
import shutil
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import event
from structlog.testing import capture_logs

import radar.entrypoints.worker.tasks as worker_tasks
from radar.config import ConfigError
from radar.domain.demo_export import (
    DEMO_LABEL_ABSTRACT_SOURCES,
    DEMO_PAPER_LIST_PAGE_SIZE,
    MANIFEST_FILE,
    PREVIEW_DIR,
    DemoExportBusyError,
    DemoExportFailure,
    DemoPaperFacts,
    dump_demo_file,
    model_for_path,
)
from radar.domain.jobs import RUN_DEMO_EXPORT
from radar.domain.judgments import ActorRole, classification_fix
from radar.domain.papers import AbstractSource
from radar.domain.settings import Settings
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import PublicViewer, SystemViewer
from radar.entrypoints.bootstrap import build_worker
from radar.repo.db import Database
from radar.repo.demo_exports import update_demo_export_finished, update_demo_export_if_owned
from radar.repo.papers import list_demo_paper_facts
from radar.services import demo_export as service
from radar.services.demo_export import (
    DemoExportCheckError,
    DemoExportError,
    DemoExportPublishedUnrecordedError,
    DemoExportSetup,
    DemoExportTakenOverError,
    NoPublishedSnapshotError,
    demo_output_lock,
    fail_demo_export,
    queue_demo_export,
    run_demo_export,
)
from radar.services.members import allow_member
from tests.database import ROOT, TestDatabase, migrate
from tests.demo_export_support import (
    EXPORTED_AT,
    MISQ,
    TEM,
    PaperSpec,
    demo_setup,
    preview_specs,
    publish_snapshots,
    seed_papers,
)
from tests.integration.test_effective_views import create_event
from tests.llm_support import execute
from tests.permissions.framework import CANARY

MEMBER_EMAIL = "demo.export.member@example.org"
# 成員版共用模型裡有、ADR-0007 白名單沒有的欄位（F1）：匯出的任何一個檔都不能有這些鍵。
NOT_WHITELISTED_KEYS = frozenset(
    {
        "fix_member_count",
        "members_disagree",
        "reported_fields",
        "in_scope",
        "origin",
        "is_retracted",
        "zh_summary_id",
        "research_model_id",
        "library_page_size",
    }
)
# Nature 的 ISSN-L：不在追蹤清單裡（追蹤範圍外）。
OUT_OF_SCOPE_ISSN = "0028-0836"


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


def _keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _all_text(output: Path) -> str:
    return b"".join(_files(output).values()).decode("utf-8")


# --- 正常的匯出 -------------------------------------------------------------------------------


def _special_specs() -> list[PaperSpec]:
    # 計入統計的 14 篇（固定的 13 篇加第 14 篇），加上三篇不計入統計的：被排除的非研究文章、
    # 追蹤範圍外、還沒算進任何已發佈的快照。
    return [
        *preview_specs(count=14),
        PaperSpec("excluded_not_counted", 90, excluded_reason="editors_comments"),
        PaperSpec("out_of_scope", 91, issn_l=OUT_OF_SCOPE_ISSN),
        PaperSpec("not_yet_published", 92, unpublished=True),
    ]


NOT_COUNTED = ("excluded_not_counted", "out_of_scope", "not_yet_published")


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
    # 只寫 preview/（沒有 data/，輸出資料夾底下也沒有鎖檔或別的檔，H1），沒有留下暫存檔；每個檔都是
    # 認得的資料檔、通過它的模型。
    assert sorted(path.name for path in tmp_path.iterdir()) == [PREVIEW_DIR]
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
    # 14 篇計入統計（不計入統計的三篇不算）。有摘要：第 2、10、14 篇以外的 11 篇；本週新增：第 1、
    # 14 篇；有完整內容（CC BY、出版社可以放、有 OpenAlex 的摘要）：第 1、4、5、6、11、12、13 篇。
    assert manifest["summary"] == {
        "paper_count": 14,
        "with_abstract_count": 11,
        "new_this_week_count": 2,
        "full_content_count": 7,
        "first_year": 2022,
        "last_year": 2025,
    }
    # 示範站一頁 50 篇（F18）：只在示範站的 client_config，沒有成員版文獻庫的篇數。
    assert manifest["client_config"] == {
        "timezone": "Asia/Taipei",
        "paper_list_page_size": DEMO_PAPER_LIST_PAGE_SIZE,
    }
    assert DEMO_PAPER_LIST_PAGE_SIZE == 50
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
    # 第 4 輪：demo_eligible（授權允許轉載）。授權允許但沒有摘要的是 true；授權不允許、IEEE 兩篇、
    # 沒有授權的是 false；有完整內容的都是 true；摘要來源不允許公開但授權允許的是 true。
    eligible = {name: paper["biblio"]["demo_eligible"] for name, paper in named.items() if paper}
    assert eligible == {
        "full_content": True,
        "eligible_without_abstract": True,
        "biblio_only_not_open": False,
        "classification_failed": True,
        "no_ai_output_yet": True,
        "full_content_ai_pending": True,
        "ieee_excluded_publisher": False,
        "ieee_journal_only": False,
        "other_source_abstract": True,
        "title_only_classification": False,
        "human_verified_topic": True,
        "human_verified_method": True,
        "full_content_ai_failed": True,
        "paper_14": False,
    }
    no_abstract = named["title_only_classification"]["biblio"]  # type: ignore[index]
    assert (no_abstract["demo_eligible"], no_abstract["has_abstract"]) == (False, False)
    assert named["eligible_without_abstract"]["biblio"]["has_abstract"] is False  # type: ignore[index]
    title_only = named["title_only_classification"]["biblio"]["classification"]  # type: ignore[index]
    assert title_only["provenance"]["input_kind"] == "title"

    # 成員修正過的主題、方法：輸出修正後的值、標成已核對；誰修正的、幾位成員修正過、意見是否不同都
    # 不在白名單裡（F1，ADR-0007 第 4、6 點）。
    verified = named["human_verified_topic"]["biblio"]["classification"]["value"]  # type: ignore[index]
    assert verified == {
        "topic": {"keys": ["it_strategy_digital_transformation"], "human_verified": True},
        "method": {"keys": ["survey_sem"], "human_verified": False},
    }
    method = named["human_verified_method"]["biblio"]["classification"]["value"]["method"]  # type: ignore[index]
    assert method == {"keys": ["experiment"], "human_verified": True}
    # 書目只有 ADR-0007 清單裡的欄位；成員版多的欄位（修正人數、回報、in_scope、origin 等）
    # 一個都沒有。
    assert set(full["biblio"]["biblio"]) == {
        "id",
        "title",
        "authors",
        "publication_year",
        "journal",
        "journal_issn_l",
        "volume",
        "issue",
        "first_page",
        "last_page",
        "doi",
        "doi_url",
        "openalex_id",
        "openalex_url",
        "cited_by_count",
    }
    for path, content in _files(tmp_path).items():
        assert NOT_WHITELISTED_KEYS.isdisjoint(_keys(json.loads(content))), path
    assert set(full["full"]["zh_summary"]["value"]) == {
        "research_question",
        "method",
        "theory",
        "findings",
    }

    # 摘要來源不允許公開：沒有完整內容，也沒有分類標籤。
    other = named["other_source_abstract"]
    assert other is not None
    assert other["full"] is None
    assert other["biblio"]["classification"] is None
    assert other["biblio"]["has_abstract"] is True

    # AI 沒有產生成功、還沒有產出（分類；完整內容層的中文重點）。
    assert named["classification_failed"]["biblio"]["classification"] == {  # type: ignore[index]
        "state": "failed",
        "value": None,
        "provenance": None,
    }
    assert named["no_ai_output_yet"]["biblio"]["classification"]["state"] == "pending_batch"  # type: ignore[index]
    ai_failed = named["full_content_ai_failed"]["full"]  # type: ignore[index]
    assert ai_failed["zh_summary"] == {"state": "failed", "value": None, "provenance": None}
    assert ai_failed["research_model"]["state"] == "ready"

    # 不計入統計的論文（被排除的非研究文章、追蹤範圍外、還沒進已發佈快照）完全不在匯出裡：分片、
    # 索引、標題，任何一個檔都沒有它的 ID 或標題。
    text = _all_text(tmp_path)
    for name, number in zip(NOT_COUNTED, (90, 91, 92), strict=True):
        assert named[name] is None, name
        assert str(ids[name]) not in text, name
        assert f"Fake Paper {number}:" not in text, name

    # 摘要原文只出現在有完整內容的論文：別的論文的摘要字串不在任何檔裡。
    assert "Fake abstract from another source" not in text
    assert full["full"]["abstract"] == {"content": "Made up abstract 1", "source": "openalex"}
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
            "INSERT INTO demo_export (snapshot_id, sequence, status, run_token)"
            " SELECT snapshot_id, 9, 'running', gen_random_uuid() FROM demo_export LIMIT 1",
        )


def test_all_reads_happen_in_one_read_only_repeatable_read_transaction(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = publish_snapshots(db)
    ids = seed_papers(db, database, preview_specs(count=3), snapshots=snapshots)
    seen: list[tuple[str, str, str, int, Any, Any]] = []

    def spy(name: str, real: Any) -> Any:
        def wrapper(viewer: Any, connection: Any, *args: Any, **kwargs: Any) -> Any:
            isolation = connection.exec_driver_sql("SHOW transaction_isolation").scalar()
            read_only = connection.exec_driver_sql("SHOW transaction_read_only").scalar()
            pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar()
            started = connection.exec_driver_sql("SELECT transaction_timestamp()").scalar()
            seen.append((name, isolation, read_only, pid, started, viewer))
            result = real(viewer, connection, *args, **kwargs)
            if name == "find_latest_published_snapshot":
                # 第一句讀完之後，另一條連線改一篇論文的標題並 commit：後面的讀取仍要看到原本的值。
                execute(
                    db,
                    "UPDATE paper SET title = 'Fake Paper changed mid export' WHERE id = %s",
                    ids["full_content"],
                )
            return result

        return wrapper

    names = (
        "find_latest_published_snapshot",
        "list_demo_paper_facts",
        "find_paper_llm_source",
        "find_ai_block",
        "list_demo_full_paper_ids",
        "list_demo_publisher_facts",
        "list_member_emails",
    )
    for name in names:
        monkeypatch.setattr(service, name, spy(name, getattr(service, name)))

    _export(database, tmp_path)

    assert {name for name, *_ in seen} == set(names)
    assert {(isolation, read_only) for _, isolation, read_only, *_ in seen} == {
        ("repeatable read", "on")
    }
    # 同一條連線、同一個交易（交易開始的時間都一樣）。
    assert len({pid for *_, pid, _, _ in seen}) == 1
    assert len({started for *_, started, _ in seen}) == 1
    # 中途 commit 的改動看不到：匯出的是原本的標題。
    titles = json.loads(_files(tmp_path)["2-1/titles.json"])["titles"]
    assert "Fake Paper 1: full content" in titles
    assert "Fake Paper changed mid export" not in titles
    # 匯出的資料只用 PublicViewer 讀；成員 email（只拿來比對）是唯一用系統身分讀的，只讀一次。
    for name, *_, viewer in seen:
        expected = SystemViewer if name == "list_member_emails" else PublicViewer
        assert type(viewer) is expected, name
    assert [name for name, *_ in seen].count("list_member_emails") == 1


def test_stale_demo_eligible_for_an_excluded_journal_gets_no_full_content(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    # 排除清單改了、還沒 reparse：IEEE 期刊（0018-9391）的論文資料庫裡還留著舊的 demo_eligible =
    # true，view demo_full_paper 也算它（F2）。匯出時直接用排除清單判，不給完整內容。
    snapshots = publish_snapshots(db)
    ids = seed_papers(
        db,
        database,
        [
            PaperSpec(
                "stale_ieee",
                1,
                issn_l=TEM,
                publisher_id="https://openalex.org/P9000000077",
                publisher_name="The Engineering Management Press",
                zh_summary=True,
            )
        ],
        snapshots=snapshots,
    )
    execute(db, "UPDATE paper SET demo_eligible = true WHERE id = %s", ids["stale_ieee"])
    in_view = {uuid.UUID(str(row[0])) for row in execute(db, "SELECT id FROM demo_full_paper")}
    assert ids["stale_ieee"] in in_view

    _export(database, tmp_path)

    [paper] = _papers(tmp_path).values()
    assert paper["full"] is None
    assert paper["biblio"]["has_full_content"] is False
    # 第 4 輪：demo_eligible 是排除清單擋完之後的結果，和不給完整內容一致。
    assert paper["biblio"]["demo_eligible"] is False
    assert json.loads(_files(tmp_path)["2-1/papers-index.json"])["has_full_content"] == [False]
    text = _all_text(tmp_path)
    assert "Made up abstract 1" not in text
    assert "（假資料）" not in text


def test_self_check_catches_an_excluded_journal_with_full_content(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 組檔那一段讀到的出版社與期刊欄位是空的（假裝漏了）：自我檢查第 6 項另外直接讀出版社欄位，
    # 擋下。
    snapshots = publish_snapshots(db)
    ids = seed_papers(db, database, [PaperSpec("stale_ieee", 1, issn_l=TEM)], snapshots=snapshots)
    execute(db, "UPDATE paper SET demo_eligible = true WHERE id = %s", ids["stale_ieee"])
    _export(database, tmp_path)  # 先有一份正常的匯出（2-1）
    before = _files(tmp_path)
    real = list_demo_paper_facts

    def blind(viewer: Any, connection: Any) -> list[DemoPaperFacts]:
        return [
            DemoPaperFacts(
                **{
                    **_fields(item),
                    "publisher_ids": (),
                    "publisher_names": (),
                    "journal_issn_l": None,
                }
            )
            for item in real(viewer, connection)
        ]

    monkeypatch.setattr(service, "list_demo_paper_facts", blind)

    problems = _check_fails(database, tmp_path)

    paper_id = ids["stale_ieee"]
    assert f"排除清單裡的出版社或期刊的論文 {paper_id} 帶了完整內容" in problems
    assert f"排除清單裡的出版社或期刊的論文 {paper_id} 標成有完整內容" in problems
    assert f"排除清單裡的出版社或期刊的論文 {paper_id} 標成 demo_eligible" in problems
    _assert_unchanged(db, tmp_path, before)


# --- 自我檢查沒過：不換 manifest、什麼都不寫、那一筆記成失敗 ------------------------------------


def _assert_unchanged(
    db: TestDatabase,
    output: Path,
    before: dict[str, bytes],
    failure: DemoExportFailure = DemoExportFailure.SELF_CHECK_FAILED,
) -> None:
    """失敗的那一次（2-2）之後：既有的 manifest 位元組不變、仍指向 2-1，2-1 的檔一個不少，沒有 2-2
    的目錄，沒有留下暫存目錄或暫存的 manifest；那一筆記成 `failure`。"""
    assert _files(output) == before
    assert json.loads(before[MANIFEST_FILE])["export_id"] == "2-1"
    assert sorted(path.name for path in (output / PREVIEW_DIR).iterdir()) == ["2-1", MANIFEST_FILE]
    assert _rows(db)[-1][1:] == ("failed", failure.value)


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
    _export(database, tmp_path)  # 沒設金絲雀：先有一份正常的匯出（2-1）
    before = _files(tmp_path)

    problems = _check_fails(database, tmp_path, demo_setup(canaries=[CANARY]))

    assert "出現金絲雀字串" in problems
    assert CANARY not in problems
    _assert_unchanged(db, tmp_path, before)


def test_member_email_in_the_output_fails_the_check(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    snapshots = publish_snapshots(db)
    # 大小寫不同也算。
    seed_papers(
        db,
        database,
        [PaperSpec("email", 1, title=f"Fake Paper by {MEMBER_EMAIL.upper()}")],
        snapshots=snapshots,
    )
    _export(database, tmp_path)  # 還不是成員的 email：先有一份正常的匯出（2-1）
    before = _files(tmp_path)
    allow_member(SystemViewer(), database, email=MEMBER_EMAIL, display_name="測試成員")

    problems = _check_fails(database, tmp_path)

    assert "出現成員的 email" in problems
    assert MEMBER_EMAIL not in problems.casefold()
    _assert_unchanged(db, tmp_path, before)


def test_full_content_outside_demo_full_paper_fails_the_check(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 組檔那一段誤以為一篇授權不允許的論文有完整內容：自我檢查另外讀 view，擋下來。
    snapshots = publish_snapshots(db)
    ids = seed_papers(
        db, database, [PaperSpec("not_open", 1, license="cc-by-nc")], snapshots=snapshots
    )
    _export(database, tmp_path)
    before = _files(tmp_path)
    real = list_demo_paper_facts

    def wrong(viewer: Any, connection: Any) -> list[DemoPaperFacts]:
        return [
            DemoPaperFacts(**{**_fields(item), "in_demo_full_paper": True, "demo_eligible": True})
            for item in real(viewer, connection)
        ]

    monkeypatch.setattr(service, "list_demo_paper_facts", wrong)

    problems = _check_fails(database, tmp_path)

    assert f"不在 demo_full_paper 的論文 {ids['not_open']} 帶了完整內容" in problems
    assert f"不在 demo_full_paper 的論文 {ids['not_open']} 標成有完整內容" in problems
    # 第 4 輪：自我檢查第 7 項另外讀資料庫存的 demo_eligible，也擋下。
    assert f"授權不允許轉載的論文 {ids['not_open']} 標成 demo_eligible" in problems
    _assert_unchanged(db, tmp_path, before)


def _fields(facts: DemoPaperFacts) -> dict[str, Any]:
    return {name: getattr(facts, name) for name in DemoPaperFacts.__slots__}


def test_field_outside_the_whitelist_fails_the_check(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 組檔那一段多寫了一個欄位：自我檢查用白名單模型讀回來，擋下來。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1, issn_l=MISQ)], snapshots=snapshots)
    _export(database, tmp_path)
    before = _files(tmp_path)
    real = dump_demo_file

    def leaky(model: Any) -> bytes:
        data = json.loads(real(model))
        data["member_note"] = "extra"
        return json.dumps(data).encode()

    monkeypatch.setattr(service, "dump_demo_file", leaky)

    problems = _check_fails(database, tmp_path)

    assert "2-2/papers-index.json：不符合白名單模型 DemoPapersIndex" in problems
    assert "extra_forbidden" in problems
    # 多出來的鍵名不印進原因（只印 `?`）。
    assert "member_note" not in problems
    _assert_unchanged(db, tmp_path, before)


def test_member_only_data_with_a_canary_never_reaches_the_files(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    # FR-7：金絲雀放進成員專屬的地方（修正分類的理由、成員的顯示名稱、email），匯出成功、任何一個檔
    # 都找不到它（F16）。
    snapshots = publish_snapshots(db)
    ids = seed_papers(db, database, [PaperSpec("fixed", 1)], snapshots=snapshots)
    canary_email = f"{CANARY}@example.org"
    member = allow_member(
        SystemViewer(), database, email=canary_email, display_name=f"成員 {CANARY}"
    ).member_id
    event = create_event(
        database,
        classification_fix(
            member_id=member,
            actor_role=ActorRole.MEMBER,
            paper_id=ids["fixed"],
            field=TaxonomyField.TOPIC,
            keys=("health_it",),
            reason=f"修正理由 {CANARY}",
        ),
    )
    assert event is not None

    _export(database, tmp_path, demo_setup(canaries=[CANARY]))

    text = _all_text(tmp_path).casefold()
    assert CANARY not in text
    assert canary_email not in text
    [paper] = _papers(tmp_path).values()
    assert paper["biblio"]["classification"]["value"]["topic"] == {
        "keys": ["health_it"],
        "human_verified": True,
    }


# --- 寫到一半失敗、並行、接手 ------------------------------------------------------------------


def _first_export(db: TestDatabase, database: Database, output: Path) -> dict[str, bytes]:
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    _export(database, output)
    return _files(output)


def _fail_on(monkeypatch: pytest.MonkeyPatch, name: str, error: BaseException) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(service, name, broken)


@pytest.mark.parametrize("step", ["_write_file", "_rename_dir"])
def test_failure_while_writing_the_directory_keeps_the_old_manifest(
    db: TestDatabase,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
) -> None:
    before = _first_export(db, database, tmp_path)
    _fail_on(monkeypatch, step, OSError(errno.EIO, "disk"))
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(OSError, match="disk"):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    _assert_unchanged(db, tmp_path, before, DemoExportFailure.ERROR)


def test_failure_while_replacing_the_manifest_keeps_the_old_one(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _first_export(db, database, tmp_path)
    _fail_on(monkeypatch, "_replace_file", OSError(errno.EIO, "disk"))
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(OSError, match="disk"):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    # 第 4 輪：manifest 沒換成，這次寫好的 2-2 目錄也刪掉（不留孤兒目錄）；manifest 的位元組沒變、
    # 沒有留下暫存的 manifest。
    _assert_unchanged(db, tmp_path, before, DemoExportFailure.ERROR)


def test_interrupted_export_is_recorded(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _first_export(db, database, tmp_path)
    _fail_on(monkeypatch, "_write_file", KeyboardInterrupt())
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(KeyboardInterrupt):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    # 中斷也移出 running（不會讓下一次 radar export-demo 被擋 6 小時）。
    _assert_unchanged(db, tmp_path, before, DemoExportFailure.INTERRUPTED)


def _lose_the_success_update(monkeypatch: pytest.MonkeyPatch) -> None:
    real = update_demo_export_finished

    def lost(connection: Any, demo_export_id: Any, **kwargs: Any) -> bool:
        if kwargs["failure"] is None:
            raise RuntimeError("connection lost")
        return bool(real(connection, demo_export_id, **kwargs))

    monkeypatch.setattr(service, "update_demo_export_finished", lost)


def _make_stale(db: TestDatabase) -> None:
    """執行中的那一筆假裝已經超過 6 小時（sweep 補排、別的行程可以接手）。"""
    execute(
        db,
        "UPDATE demo_export SET updated_at = now() - interval '7 hours' WHERE status = 'running'",
    )


def test_manifest_already_switched_is_recorded_as_succeeded_on_the_rerun(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 換好 manifest、記成功的那一句失敗（F5）：回報「已發佈、狀態待補記」，那一筆留在 running；
    # sweep 補排後重跑時，發現 manifest 已經指向這一次、檔案都完整，就補記成功，不重寫。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    _lose_the_success_update(monkeypatch)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportPublishedUnrecordedError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert (caught.value.export_id, caught.value.error_type) == ("2-1", "RuntimeError")

    written = _files(tmp_path)
    assert json.loads(written[MANIFEST_FILE])["export_id"] == "2-1"
    assert _rows(db) == [(1, "running", None)]
    monkeypatch.undo()
    # 還沒超過 6 小時：別的行程不能接手。
    assert run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock) is None
    _make_stale(db)

    result = run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert result is not None
    assert (result.export_id, result.paper_count, result.full_content_count) == ("2-1", 1, 1)
    assert _files(tmp_path) == written
    assert _rows(db) == [(1, "succeeded", None)]


def test_older_export_does_not_overwrite_a_newer_manifest(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 快照 2 的匯出讀完資料後，快照 3 發佈、它的匯出先寫完（F3）。快照 2 那一次在發佈鎖裡看到
    # manifest 已經指向 3-1，不蓋回去。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    real = service._build_and_check
    newer: list[service.DemoExportResult] = []
    started: list[bool] = []

    def slow(*args: Any, **kwargs: Any) -> Any:
        built = real(*args, **kwargs)
        if not started:
            started.append(True)  # 快照 3 那一次也會經過這裡，只插一次
            [(run,)] = execute(
                db, "INSERT INTO weekly_run (status) VALUES ('published') RETURNING id"
            )
            execute(
                db,
                "INSERT INTO snapshot (number, weekly_run_id, status, as_of)"
                " VALUES (3, %s, 'published', now())",
                run,
            )
            newer.append(_export(database, tmp_path))
        return built

    monkeypatch.setattr(service, "_build_and_check", slow)
    older = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), older.id, tmp_path, clock=_clock)

    assert caught.value.failure is DemoExportFailure.SUPERSEDED
    assert newer[0].export_id == "3-1"
    assert json.loads(_files(tmp_path)[MANIFEST_FILE])["export_id"] == "3-1"
    assert sorted(path.name for path in (tmp_path / PREVIEW_DIR).iterdir()) == [
        "3-1",
        MANIFEST_FILE,
    ]
    assert _rows(db) == [(1, "failed", "superseded"), (1, "succeeded", None)]


def test_export_taken_over_by_another_process_writes_nothing(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 組檔途中另一個行程接手了同一筆（run_token 換掉，F4）：這個行程不寫檔、不改那一筆的狀態。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    real = service._build_and_check

    def taken_over(*args: Any, **kwargs: Any) -> Any:
        built = real(*args, **kwargs)
        execute(db, "UPDATE demo_export SET run_token = gen_random_uuid()")
        return built

    monkeypatch.setattr(service, "_build_and_check", taken_over)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportTakenOverError):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert _files(tmp_path) == {}
    assert _rows(db) == [(1, "running", None)]


def test_preview_symlink_is_refused(db: TestDatabase, database: Database, tmp_path: Path) -> None:
    # preview 是指到別處（例如正式的 data/）的符號連結：不沿著連結寫（F7）。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    elsewhere = tmp_path / "data"
    elsewhere.mkdir()
    output = tmp_path / "out"
    output.mkdir()
    (output / PREVIEW_DIR).symlink_to(elsewhere, target_is_directory=True)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), run.id, output, clock=_clock)

    assert caught.value.failure is DemoExportFailure.OUTPUT_UNSAFE
    assert list(elsewhere.iterdir()) == []
    assert _rows(db) == [(1, "failed", "output_unsafe")]


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


def test_missing_output_dir_is_recorded_and_nothing_is_written(
    db: TestDatabase, database: Database, tmp_path: Path
) -> None:
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), run.id, None, clock=_clock)

    assert caught.value.failure is DemoExportFailure.NO_OUTPUT_DIR
    assert list(tmp_path.iterdir()) == []
    assert _rows(db) == [(1, "failed", "no_output_dir")]


def test_config_error_before_the_export_is_recorded(db: TestDatabase, database: Database) -> None:
    # 還沒開始就失敗時記成 failed 的那一步（worker 入口的測試在下面）：只記一次。
    publish_snapshots(db)
    run = queue_demo_export(SYSTEM, database)

    assert fail_demo_export(SYSTEM, database, run.id, DemoExportFailure.CONFIG_ERROR) is True
    assert fail_demo_export(SYSTEM, database, run.id, DemoExportFailure.CONFIG_ERROR) is False
    assert _rows(db) == [(1, "failed", "config_error")]


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
    assert _rows(db) == [(1, "failed", "output_exists")]


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


# --- 第 2 輪審查（G1～G10）------------------------------------------------------------------------


def _lock_is_held(output: Path) -> bool:
    """另外打開 `preview/` 試著拿它的檔案鎖（不等）：拿不到就是有人拿著。"""
    descriptor = os.open(output / PREVIEW_DIR, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def test_export_dir_and_manifest_are_written_while_holding_the_output_lock(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G1、H1：改名成匯出目錄、換 manifest 的時候都拿著 preview/ 的檔案鎖；做完就放掉。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    held: list[tuple[str, bool]] = []
    real_rename, real_replace = service._rename_dir, service._replace_file

    def rename(*args: Any) -> None:
        held.append(("rename", _lock_is_held(tmp_path)))
        real_rename(*args)

    def replace(*args: Any) -> None:
        held.append(("replace", _lock_is_held(tmp_path)))
        real_replace(*args)

    monkeypatch.setattr(service, "_rename_dir", rename)
    monkeypatch.setattr(service, "_replace_file", replace)

    _export(database, tmp_path)

    assert held == [("rename", True), ("replace", True)]
    assert not _lock_is_held(tmp_path)


def test_export_waits_for_another_holder_of_the_output_lock(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G1、H7（真的並行）：別的行程（另一個匯出）拿著 preview/ 的檔案鎖時，這次匯出等它放掉才寫。
    # 用替身記下「試著拿鎖、沒拿到」，確定匯出真的走到搶鎖那一步才檢查（不靠等幾秒）。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    run = queue_demo_export(SYSTEM, database)
    outcome: list[object] = []
    waiting = threading.Event()
    real_try = service._try_lock

    def try_lock(descriptor: int) -> bool:
        got = real_try(descriptor)
        if not got:
            waiting.set()
        return got

    monkeypatch.setattr(service, "_try_lock", try_lock)
    (tmp_path / PREVIEW_DIR).mkdir()

    def export() -> None:
        try:
            outcome.append(
                run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)
            )
        except BaseException as exc:  # 交給主執行緒斷言
            outcome.append(exc)

    with demo_output_lock(tmp_path / PREVIEW_DIR):
        worker = threading.Thread(target=export)
        worker.start()
        assert waiting.wait(timeout=60)
        assert worker.is_alive()
        assert not (tmp_path / PREVIEW_DIR / MANIFEST_FILE).exists()
        assert not (tmp_path / PREVIEW_DIR / "2-1").exists()
    worker.join(timeout=60)

    assert not worker.is_alive()
    [result] = outcome
    assert isinstance(result, service.DemoExportResult), result
    assert json.loads(_files(tmp_path)[MANIFEST_FILE])["export_id"] == "2-1"
    assert _rows(db) == [(1, "succeeded", None)]


def test_newer_manifest_written_after_the_directory_is_checked_again_before_the_switch(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G1、H7：寫好匯出目錄之後、換 manifest 之前，manifest 變成指向比較新的 3-1（這裡是同一個行程用
    # 替身直接寫進去，只驗「換之前再比一次」這一步；沒有另一個行程、也沒有模擬資料庫斷線）。第二次比
    # 新舊擋下來，不蓋回去。
    before = _first_export(db, database, tmp_path)
    data = json.loads(before[MANIFEST_FILE])
    newer = json.dumps(
        {
            **data,
            "export_id": "3-1",
            "files": [
                {**entry, "path": entry["path"].replace("2-1/", "3-1/", 1)}
                for entry in data["files"]
            ],
        }
    ).encode()
    real = service._write_export_dir

    def then_newer(*args: Any) -> None:
        real(*args)
        (tmp_path / PREVIEW_DIR / MANIFEST_FILE).write_bytes(newer)

    monkeypatch.setattr(service, "_write_export_dir", then_newer)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert caught.value.failure is DemoExportFailure.SUPERSEDED
    assert (tmp_path / PREVIEW_DIR / MANIFEST_FILE).read_bytes() == newer
    # 第 4 輪：換 manifest 前停下，這次寫好的 2-2 目錄刪掉。
    assert sorted(path.name for path in (tmp_path / PREVIEW_DIR).iterdir()) == [
        "2-1",
        MANIFEST_FILE,
    ]
    assert _rows(db)[-1] == (2, "failed", "superseded")


@pytest.mark.parametrize("kind", ["broken_json", "older_format"])
def test_unreadable_manifest_is_refused_instead_of_treated_as_missing(
    db: TestDatabase, database: Database, tmp_path: Path, kind: str
) -> None:
    # G2：現有的 manifest 讀不懂（JSON 壞掉、別的版本寫的、多了欄位）時不知道它指向的是不是比較新
    # 的匯出：拒絕、什麼都不寫，不當成沒有 manifest。
    first = _first_export(db, database, tmp_path)
    if kind == "broken_json":
        content = first[MANIFEST_FILE][:-5]
    else:
        data = json.loads(first[MANIFEST_FILE])
        data["client_config"]["library_page_size"] = 20
        content = json.dumps(data).encode()
    (tmp_path / PREVIEW_DIR / MANIFEST_FILE).write_bytes(content)
    before = _files(tmp_path)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert caught.value.failure is DemoExportFailure.MANIFEST_UNREADABLE
    assert _files(tmp_path) == before
    assert sorted(path.name for path in (tmp_path / PREVIEW_DIR).iterdir()) == [
        "2-1",
        MANIFEST_FILE,
    ]
    assert _rows(db)[-1] == (2, "failed", "manifest_unreadable")


def test_preview_is_fsynced_before_and_after_the_manifest_switch(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G3：匯出目錄改名之後、換 manifest 之前 fsync preview/；換完再 fsync 一次。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    events: list[tuple[str, int]] = []
    real_fsync, real_rename, real_replace = (
        service._fsync_dir,
        service._rename_dir,
        service._replace_file,
    )

    def fsync(descriptor: int) -> None:
        events.append(("fsync", os.fstat(descriptor).st_ino))
        real_fsync(descriptor)

    def rename(*args: Any) -> None:
        events.append(("rename", 0))
        real_rename(*args)

    def replace(*args: Any) -> None:
        events.append(("replace", 0))
        real_replace(*args)

    monkeypatch.setattr(service, "_fsync_dir", fsync)
    monkeypatch.setattr(service, "_rename_dir", rename)
    monkeypatch.setattr(service, "_replace_file", replace)

    _export(database, tmp_path)

    preview = (tmp_path / PREVIEW_DIR).stat().st_ino
    renamed, replaced = events.index(("rename", 0)), events.index(("replace", 0))
    assert renamed < replaced
    assert events[renamed + 1 : replaced] == [("fsync", preview)]
    assert events[replaced + 1 :] == [("fsync", preview)]


def test_fsync_failure_after_the_switch_is_published_and_recovered_later(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G3：換好 manifest 之後 fsync preview/ 失敗：讀者已經看得到新的一批，回報「已發佈、狀態待
    # 補記」、不記失敗；接手時先再 fsync 一次 preview/，才補記成功。
    _first_export(db, database, tmp_path)
    switched: list[bool] = []
    real_fsync, real_replace = service._fsync_dir, service._replace_file

    def replace(*args: Any) -> None:
        real_replace(*args)
        switched.append(True)

    def fsync(descriptor: int) -> None:
        if switched:
            raise OSError(errno.EIO, "disk")
        real_fsync(descriptor)

    monkeypatch.setattr(service, "_replace_file", replace)
    monkeypatch.setattr(service, "_fsync_dir", fsync)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportPublishedUnrecordedError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert (caught.value.export_id, caught.value.error_type) == ("2-2", "OSError")
    written = _files(tmp_path)
    assert json.loads(written[MANIFEST_FILE])["export_id"] == "2-2"
    assert _rows(db)[-1] == (2, "running", None)
    monkeypatch.undo()
    _make_stale(db)
    order: list[str] = []
    real_finished = update_demo_export_finished

    def spy_fsync(descriptor: int) -> None:
        order.append("fsync")
        real_fsync(descriptor)

    def spy_finished(connection: Any, demo_export_id: Any, **kwargs: Any) -> bool:
        order.append("succeeded" if kwargs["failure"] is None else "failed")
        return bool(real_finished(connection, demo_export_id, **kwargs))

    monkeypatch.setattr(service, "_fsync_dir", spy_fsync)
    monkeypatch.setattr(service, "update_demo_export_finished", spy_finished)

    result = run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert result is not None
    assert result.export_id == "2-2"
    assert order == ["fsync", "succeeded"]
    assert _files(tmp_path) == written
    assert _rows(db)[-1] == (2, "succeeded", None)


def test_commit_failure_after_the_switch_is_published_and_recovered_later(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G4、H7：記成功的 UPDATE 已經執行、commit 本身失敗（SQLAlchemy 的 commit 事件丟錯，真的 commit
    # 沒有送出，交易退回）：回報「已發佈、狀態待補記」，那一筆留在 running；接手時補記成功、不重寫。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    recorded: list[bool] = []
    commits_refused: list[bool] = []
    real_finished = update_demo_export_finished

    def finished(connection: Any, demo_export_id: Any, **kwargs: Any) -> bool:
        updated = bool(real_finished(connection, demo_export_id, **kwargs))
        if kwargs["failure"] is None:
            recorded.append(updated)
        return updated

    def refuse_commit(connection: Any) -> None:
        # 記成功的那個交易 commit 時丟錯（只丟一次）。
        if recorded and not commits_refused:
            commits_refused.append(True)
            raise psycopg.OperationalError("commit failed")

    monkeypatch.setattr(service, "update_demo_export_finished", finished)
    engine = database._engine
    event.listen(engine, "commit", refuse_commit)
    run = queue_demo_export(SYSTEM, database)

    try:
        with pytest.raises(DemoExportPublishedUnrecordedError) as caught:
            run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)
    finally:
        event.remove(engine, "commit", refuse_commit)

    assert recorded == [True]
    assert commits_refused == [True]
    assert (caught.value.export_id, caught.value.error_type) == ("2-1", "OperationalError")
    written = _files(tmp_path)
    assert json.loads(written[MANIFEST_FILE])["export_id"] == "2-1"
    assert _rows(db) == [(1, "running", None)]
    monkeypatch.undo()
    _make_stale(db)

    result = run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert result is not None
    assert result.export_id == "2-1"
    assert _files(tmp_path) == written
    assert _rows(db) == [(1, "succeeded", None)]


def test_preview_swapped_for_a_symlink_mid_export_is_not_followed(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G5、H4：檢查過 preview/ 之後，有人把它搬走、換成指到別處（例如正式的 data/）的符號連結：不沿著
    # 連結寫；打開的目錄已經不是 <輸出資料夾>/preview，也不寫進搬走的資料夾、不回報成功
    # （output_unsafe）。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    elsewhere = tmp_path / "data"
    elsewhere.mkdir()
    output = tmp_path / "out"
    real = service._build_and_check

    def swap(*args: Any, **kwargs: Any) -> Any:
        built = real(*args, **kwargs)
        (output / PREVIEW_DIR).rename(output / "moved")
        (output / PREVIEW_DIR).symlink_to(elsewhere, target_is_directory=True)
        return built

    monkeypatch.setattr(service, "_build_and_check", swap)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), run.id, output, clock=_clock)

    assert caught.value.failure is DemoExportFailure.OUTPUT_UNSAFE
    assert list(elsewhere.iterdir()) == []
    assert list((output / "moved").iterdir()) == []
    assert _rows(db) == [(1, "failed", "output_unsafe")]


def test_manifest_listing_this_export_with_missing_files_is_recorded_as_failed(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G10（L1）：manifest 已經指向這一次、資料檔卻少了一個：補記不了；重跑時目錄已經存在，記成
    # failed（不要一直停在 running、每 6 小時重排一次）。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    _lose_the_success_update(monkeypatch)
    run = queue_demo_export(SYSTEM, database)
    with pytest.raises(DemoExportPublishedUnrecordedError):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)
    monkeypatch.undo()
    (tmp_path / PREVIEW_DIR / "2-1" / "titles.json").unlink()
    _make_stale(db)

    with pytest.raises(DemoExportError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert caught.value.failure is DemoExportFailure.OUTPUT_EXISTS
    assert _rows(db) == [(1, "failed", "output_exists")]


def test_config_error_in_the_worker_job_is_recorded(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G8：走 worker 登記的工作（register_tasks 裡的 run_demo_export_job）：設定檔壞掉時那一筆記成
    # failed（config_error，sweep 不再補排），ConfigError 原樣往上丟，什麼都不寫。
    publish_snapshots(db)
    run = queue_demo_export(SYSTEM, database)
    config_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", config_dir)
    (config_dir / "models.yaml").write_text("models: [\n", encoding="utf-8")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(config_dir))
    output = tmp_path / "out"
    settings = Settings.model_validate({"database_url": db.app_url, "demo_export_dir": str(output)})
    parts = build_worker(settings, queue_pool_size=1, app_pool_size=1, heartbeat_urls={})
    worker_tasks.register_tasks(parts.job_app.app, parts, settings)
    task = parts.job_app.app.tasks[RUN_DEMO_EXPORT.name].func

    with pytest.raises(ConfigError):
        task(demo_export_id=run.id)

    assert _rows(db) == [(1, "failed", "config_error")]
    assert not output.exists()


# --- 第 3 輪審查（H1～H8）------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lost_at", "written"),
    [
        # 接手代號的確認：_recover 的拿鎖前（1）與拿到檔案鎖後（2）、_publish 的拿鎖前（3）、
        # 拿到檔案鎖後寫目錄前（4）、換 manifest 前（5）。第 4 輪：換 manifest 前發現時，
        # 這次寫好的 2-1 目錄刪掉（原本留著）。
        (4, []),
        (5, []),
    ],
)
def test_takeover_noticed_after_getting_the_output_lock_does_not_switch_the_manifest(
    db: TestDatabase,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lost_at: int,
    written: list[str],
) -> None:
    # H2：等檔案鎖的期間被別的行程接手（這裡用替身讓第 lost_at 次確認接手代號回「不是自己的」）：
    # 拿到檔案鎖之後再確認一次，寫目錄前發現就什麼都不寫，換 manifest 前發現就不換、刪掉剛寫好的
    # 目錄；狀態不改。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    real = update_demo_export_if_owned
    calls: list[int] = []

    def owned(connection: Any, demo_export_id: Any, token: Any) -> bool:
        calls.append(1)
        if len(calls) == lost_at:
            return False
        return bool(real(connection, demo_export_id, token))

    monkeypatch.setattr(service, "update_demo_export_if_owned", owned)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportTakenOverError):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert len(calls) == lost_at
    assert sorted(path.name for path in (tmp_path / PREVIEW_DIR).iterdir()) == written
    assert _rows(db) == [(1, "running", None)]


def test_published_export_superseded_before_it_was_recorded_is_recorded_as_superseded(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H3：2-1 換好 manifest、卻沒記上成功（留在 running）；之後快照 3 發佈、3-1 先發佈。sweep 補排
    # 2-1 時，manifest 已經指向比較新的 3-1：直接記成 superseded（不讀資料、不寫檔），不會因為快照
    # 變了記成 snapshot_changed，也不會一直停在 running。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    _lose_the_success_update(monkeypatch)
    older = queue_demo_export(SYSTEM, database)
    with pytest.raises(DemoExportPublishedUnrecordedError):
        run_demo_export(SYSTEM, database, demo_setup(), older.id, tmp_path, clock=_clock)
    monkeypatch.undo()
    [(weekly,)] = execute(db, "INSERT INTO weekly_run (status) VALUES ('published') RETURNING id")
    execute(
        db,
        "INSERT INTO snapshot (number, weekly_run_id, status, as_of)"
        " VALUES (3, %s, 'published', now())",
        weekly,
    )
    assert _export(database, tmp_path).export_id == "3-1"
    before = _files(tmp_path)
    _make_stale(db)
    built: list[bool] = []
    real_build = service._build_and_check

    def build(*args: Any, **kwargs: Any) -> Any:
        built.append(True)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(service, "_build_and_check", build)

    with pytest.raises(DemoExportError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), older.id, tmp_path, clock=_clock)

    assert caught.value.failure is DemoExportFailure.SUPERSEDED
    assert built == []
    assert _files(tmp_path) == before
    assert _rows(db) == [(1, "failed", "superseded"), (1, "succeeded", None)]


def test_preview_moved_while_the_manifest_is_switched_is_not_reported_as_success(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H4、第 4 輪（Codex 第 3 次複查新高 1）：換 manifest 的那一下 preview/ 被搬走。manifest 一換好
    # 就算已發佈：換完的位置檢查失敗不回報成功，也不記成失敗（留在 running、已發佈待補記）；搬回來
    # 之後補排時補記成功。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    output = tmp_path / "out"
    real_replace = service._replace_file

    def replace_then_move(*args: Any) -> None:
        real_replace(*args)
        (output / PREVIEW_DIR).rename(output / "moved")

    monkeypatch.setattr(service, "_replace_file", replace_then_move)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(DemoExportPublishedUnrecordedError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), run.id, output, clock=_clock)

    assert (caught.value.export_id, caught.value.error_type) == ("2-1", "DemoExportError")
    assert not (output / PREVIEW_DIR).exists()
    moved = json.loads((output / "moved" / MANIFEST_FILE).read_bytes())
    assert moved["export_id"] == "2-1"
    assert _rows(db) == [(1, "running", None)]
    monkeypatch.undo()
    (output / "moved").rename(output / PREVIEW_DIR)
    _make_stale(db)

    result = run_demo_export(SYSTEM, database, demo_setup(), run.id, output, clock=_clock)

    assert result is not None
    assert result.export_id == "2-1"
    assert _rows(db) == [(1, "succeeded", None)]


@pytest.mark.parametrize(
    "error", [PermissionError(errno.EACCES, "denied"), KeyboardInterrupt()], ids=type
)
def test_failure_right_after_the_switch_is_never_recorded_as_unpublished(
    db: TestDatabase,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    # 第 4 輪（Codex 第 3 次複查新高 1）：manifest 換好之後，位置檢查一直讀不到路徑（權限錯誤；
    # 失敗處理再查一次也讀不到）或收到中斷：已發佈，不記成 failed，留在 running；之後補排時補記
    # 成功、不重寫。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    switched: list[bool] = []
    real_replace, real_check = service._replace_file, service._check_in_place

    def replace(*args: Any) -> None:
        real_replace(*args)
        switched.append(True)

    def check(out: Any) -> None:
        if switched:
            raise error
        real_check(out)

    monkeypatch.setattr(service, "_replace_file", replace)
    monkeypatch.setattr(service, "_check_in_place", check)
    run = queue_demo_export(SYSTEM, database)

    expected = (
        DemoExportPublishedUnrecordedError if isinstance(error, Exception) else KeyboardInterrupt
    )
    with pytest.raises(expected):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    written = _files(tmp_path)
    assert json.loads(written[MANIFEST_FILE])["export_id"] == "2-1"
    assert _rows(db) == [(1, "running", None)]
    monkeypatch.undo()
    _make_stale(db)

    result = run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert result is not None
    assert result.export_id == "2-1"
    assert _files(tmp_path) == written
    assert _rows(db) == [(1, "succeeded", None)]


def test_same_export_id_redone_after_a_takeover_before_the_switch_succeeds(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 第 4 輪（Opus 第 3 輪確認的中）：寫好匯出目錄之後、換 manifest 之前發現被接手，刪掉剛寫好的
    # 目錄；之後接手的行程用同一個匯出編號重做，不會碰到 output_exists。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    real = update_demo_export_if_owned
    calls: list[int] = []

    def owned(connection: Any, demo_export_id: Any, token: Any) -> bool:
        calls.append(1)
        return False if len(calls) == 5 else bool(real(connection, demo_export_id, token))

    monkeypatch.setattr(service, "update_demo_export_if_owned", owned)
    run = queue_demo_export(SYSTEM, database)
    with pytest.raises(DemoExportTakenOverError):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)
    monkeypatch.undo()
    assert list((tmp_path / PREVIEW_DIR).iterdir()) == []
    _make_stale(db)

    result = run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert result is not None
    assert result.export_id == "2-1"
    assert sorted(path.name for path in (tmp_path / PREVIEW_DIR).iterdir()) == [
        "2-1",
        MANIFEST_FILE,
    ]
    assert _rows(db) == [(1, "succeeded", None)]


def test_ownership_check_raising_before_the_switch_removes_the_written_directory(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 第 4 輪（第 5 輪改名，Codex 第 4 次複查中 3）：換 manifest 前那次確認接手代號時，替身直接丟
    # psycopg.OperationalError（只驗這個例外路徑；沒有真的斷開資料庫連線，也不驗資料庫放鎖）：
    # manifest 不換、剛寫好的目錄刪掉，那一筆記成 error。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    real = update_demo_export_if_owned
    calls: list[int] = []

    def owned(connection: Any, demo_export_id: Any, token: Any) -> bool:
        calls.append(1)
        if len(calls) == 5:
            raise psycopg.OperationalError("connection lost")
        return bool(real(connection, demo_export_id, token))

    monkeypatch.setattr(service, "update_demo_export_if_owned", owned)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(psycopg.OperationalError):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert list((tmp_path / PREVIEW_DIR).iterdir()) == []
    assert _rows(db) == [(1, "failed", "error")]


def test_interrupt_right_after_the_manifest_rename_keeps_the_directory_and_recovers(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 第 5 輪（Codex 第 4 次複查新高 1、Opus 新高 1）：manifest 換名已經成功、還沒設「已發佈」就收到
    # Ctrl-C（替身在換名之後丟 KeyboardInterrupt）：清理前重讀 manifest，已經指向這一次就不刪目錄、
    # 不記失敗，留在 running；之後補排時補記成功、不重寫。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    real_replace = service._replace_file

    def replace_then_interrupt(*args: Any) -> None:
        real_replace(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(service, "_replace_file", replace_then_interrupt)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(KeyboardInterrupt):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    written = _files(tmp_path)
    assert json.loads(written[MANIFEST_FILE])["export_id"] == "2-1"
    assert sorted(path.name for path in (tmp_path / PREVIEW_DIR).iterdir()) == [
        "2-1",
        MANIFEST_FILE,
    ]
    assert _rows(db) == [(1, "running", None)]
    monkeypatch.undo()
    _make_stale(db)

    result = run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert result is not None
    assert result.export_id == "2-1"
    assert _files(tmp_path) == written
    assert _rows(db) == [(1, "succeeded", None)]


def test_cleanup_leaves_a_directory_swapped_in_under_the_same_name(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 第 5 輪（Codex 第 4 次複查新高 2）：寫好 2-1 之後，有人把它搬走、放進同名的別的目錄，接著換
    # manifest 前發現被接手：清理比對身分不一樣，不刪換進來的目錄（也不動搬走的那個）。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    preview = tmp_path / PREVIEW_DIR
    moved = tmp_path / "moved-2-1"
    real_write = service._write_export_dir

    def write_then_swap(*args: Any) -> None:
        real_write(*args)
        (preview / "2-1").rename(moved)
        (preview / "2-1").mkdir()
        (preview / "2-1" / "keep.txt").write_text("someone else's", encoding="utf-8")

    real_owned = update_demo_export_if_owned
    calls: list[int] = []

    def owned(connection: Any, demo_export_id: Any, token: Any) -> bool:
        calls.append(1)
        return False if len(calls) == 5 else bool(real_owned(connection, demo_export_id, token))

    monkeypatch.setattr(service, "_write_export_dir", write_then_swap)
    monkeypatch.setattr(service, "update_demo_export_if_owned", owned)
    run = queue_demo_export(SYSTEM, database)

    with capture_logs() as logs, pytest.raises(DemoExportTakenOverError):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert (preview / "2-1" / "keep.txt").read_text(encoding="utf-8") == "someone else's"
    assert any(moved.iterdir())
    events = [entry["event"] for entry in logs]
    assert any("已經不是這次寫的" in event for event in events)
    assert any("沒有刪掉（見前一筆警告）" in event for event in events)
    assert _rows(db) == [(1, "running", None)]


@pytest.mark.parametrize(
    ("lost_at", "says"), [(1, "這次還沒寫匯出目錄"), (5, "這次寫好的匯出目錄已刪掉")]
)
def test_takeover_log_says_what_happened_to_the_directory(
    db: TestDatabase,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lost_at: int,
    says: str,
) -> None:
    # 第 5 輪（Codex 中 1／#4、Opus 低 1）：被接手時的日誌照實寫匯出目錄的下場。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    real = update_demo_export_if_owned
    calls: list[int] = []

    def owned(connection: Any, demo_export_id: Any, token: Any) -> bool:
        calls.append(1)
        return False if len(calls) == lost_at else bool(real(connection, demo_export_id, token))

    monkeypatch.setattr(service, "update_demo_export_if_owned", owned)
    run = queue_demo_export(SYSTEM, database)

    with capture_logs() as logs, pytest.raises(DemoExportTakenOverError):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    (taken_over,) = [entry["event"] for entry in logs if "被別的行程接手" in entry["event"]]
    assert says in taken_over


def test_fsync_failure_right_after_the_directory_rename_removes_the_directory(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 第 5 輪（Codex 第 4 次複查新中 1）：匯出目錄改名之後 fsync preview/ 失敗：manifest 不換、這次
    # 寫好的目錄照樣刪掉（不留下讓同編號重做碰到 output_exists 的目錄），記成 error。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    renamed: list[bool] = []
    real_rename, real_fsync = service._rename_dir, service._fsync_dir

    def rename(*args: Any) -> None:
        real_rename(*args)
        renamed.append(True)

    def fsync(descriptor: int) -> None:
        if renamed:
            raise OSError(errno.EIO, "disk")
        real_fsync(descriptor)

    monkeypatch.setattr(service, "_rename_dir", rename)
    monkeypatch.setattr(service, "_fsync_dir", fsync)
    run = queue_demo_export(SYSTEM, database)

    with pytest.raises(OSError, match="disk"):
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert renamed == [True]
    assert list((tmp_path / PREVIEW_DIR).iterdir()) == []
    assert _rows(db) == [(1, "failed", "error")]


def test_waiting_too_long_for_the_output_lock_writes_nothing(
    db: TestDatabase, database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H8：拿著檔案鎖的一方卡住時，不會無限等：超過時限記成 output_busy，什麼都不寫。
    snapshots = publish_snapshots(db)
    seed_papers(db, database, [PaperSpec("a", 1)], snapshots=snapshots)
    monkeypatch.setattr(service, "DEMO_OUTPUT_LOCK_TIMEOUT_SECONDS", 0.2)
    (tmp_path / PREVIEW_DIR).mkdir()
    run = queue_demo_export(SYSTEM, database)

    with demo_output_lock(tmp_path / PREVIEW_DIR), pytest.raises(DemoExportError) as caught:
        run_demo_export(SYSTEM, database, demo_setup(), run.id, tmp_path, clock=_clock)

    assert caught.value.failure is DemoExportFailure.OUTPUT_BUSY
    assert list((tmp_path / PREVIEW_DIR).iterdir()) == []
    assert _rows(db) == [(1, "failed", "output_busy")]
