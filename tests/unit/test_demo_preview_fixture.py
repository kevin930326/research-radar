"""給前端的範例資料 `web/e2e/fixtures/demo-preview/`（Story 3.7、ADR-0061）：結構和 `preview/`
相同、通過 `demo-openapi.json` 的每個模型、涵蓋前端要測的每一種情況。

這份範例由 `tests/demo_preview.py` 用測試用的資料庫產生（全部是假論文），怎麼重新產生寫在那個檔。
"""

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from radar.domain.demo_export import (
    DEMO_FILE_MODELS,
    MANIFEST_FILE,
    PAPERS_INDEX_FILE,
    SHARD_SIZE,
    TITLES_FILE,
    build_demo_openapi,
    model_for_path,
)
from radar.domain.paper_list import PAPER_LIST_PAGE_SIZE

ROOT = Path(__file__).resolve().parents[2]
PREVIEW = ROOT / "web" / "e2e" / "fixtures" / "demo-preview"
# 示範站論文清單一次 50 篇（epics Story 3.14）：範例要多於一頁。
DEMO_PAGE_SIZE = 50


def _files() -> dict[str, bytes]:
    return {
        path.relative_to(PREVIEW).as_posix(): path.read_bytes()
        for path in sorted(PREVIEW.rglob("*"))
        if path.is_file()
    }


def _load(path: str) -> Any:
    return json.loads(_files()[path])


def _export_id() -> str:
    return str(_load(MANIFEST_FILE)["export_id"])


def _papers() -> list[dict[str, Any]]:
    export_id = _export_id()
    return [
        paper
        for path, content in sorted(_files().items())
        if path.startswith(f"{export_id}/papers/")
        for paper in json.loads(content)["papers"]
    ]


def test_layout_is_the_same_as_preview() -> None:
    files = _files()
    export_id = _export_id()
    manifest = _load(MANIFEST_FILE)

    # 最上層只有 manifest 與一個匯出目錄；manifest 列的檔就是目錄裡的每一個檔、雜湊對得上。
    assert sorted(path.name for path in PREVIEW.iterdir()) == sorted([MANIFEST_FILE, export_id])
    assert {entry["path"]: entry["sha256"] for entry in manifest["files"]} == {
        path: hashlib.sha256(content).hexdigest()
        for path, content in files.items()
        if path != MANIFEST_FILE
    }
    for path, content in files.items():
        data = json.loads(content)
        assert data["snapshot"] == manifest["snapshot"], path
        assert data["export_id"] == export_id, path


def _model_by_ref() -> dict[str, type[BaseModel]]:
    return {model.__name__: model for _, model in DEMO_FILE_MODELS}


def test_every_file_passes_its_demo_openapi_model() -> None:
    document = build_demo_openapi()
    by_ref = _model_by_ref()
    used: set[str] = set()
    for path, content in _files().items():
        model = model_for_path(path)
        assert model is not None, path
        model.model_validate_json(content)
        used.add(model.__name__)
    # demo-openapi.json 列的每一種資料檔，範例裡都有。
    listed = {
        operations["get"]["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ].rsplit("/", 1)[1]
        for operations in document["paths"].values()
    }
    assert listed == used == set(by_ref)


def _count(values: Iterable[object]) -> dict[object, int]:
    counts: dict[object, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def test_enough_papers_for_more_than_one_page() -> None:
    index = _load(f"{_export_id()}/{PAPERS_INDEX_FILE}")
    titles = _load(f"{_export_id()}/{TITLES_FILE}")["titles"]

    assert len(index["id"]) > DEMO_PAGE_SIZE > PAPER_LIST_PAGE_SIZE
    assert len(index["id"]) <= SHARD_SIZE
    assert len(titles) == len(index["id"])
    assert all(title.startswith("Fake Paper ") for title in titles)


def test_every_paper_state_is_covered() -> None:
    papers = _papers()
    by_title = {paper["biblio"]["biblio"]["title"]: paper for paper in papers}

    def named(part: str) -> dict[str, Any]:
        [paper] = [paper for title, paper in by_title.items() if title.split(": ", 1)[1] == part]
        return paper

    full = named("full content")
    assert full["full"]["zh_summary"]["state"] == "ready"
    assert full["full"]["research_model"]["state"] == "ready"
    assert full["full"]["abstract"]["content"]
    # 授權允許但沒有摘要、只有書目（授權不允許）、AI 沒有產生成功、還沒有產出、只看標題分類、
    # 成員核對過、摘要來源不允許公開（沒有標籤）。
    no_abstract = named("eligible without abstract")
    assert (no_abstract["biblio"]["has_abstract"], no_abstract["full"]) == (False, None)
    biblio_only = named("biblio only not open")
    assert (biblio_only["biblio"]["has_abstract"], biblio_only["full"]) == (True, None)
    assert named("classification failed")["biblio"]["classification"]["state"] == "failed"
    assert named("no ai output yet")["biblio"]["classification"]["state"] == "pending_batch"
    pending = named("full content ai pending")["full"]
    assert pending["zh_summary"]["state"] == "pending_batch"
    title_only = named("title only classification")["biblio"]["classification"]
    assert title_only["provenance"]["input_kind"] == "title"
    verified = named("human verified topic")["biblio"]["classification"]["value"]["topic"]
    assert verified["human_verified"] is True
    assert named("other source abstract")["biblio"]["classification"] is None
    assert named("ieee excluded publisher")["full"] is None
    assert named("ieee journal only")["full"] is None


def test_every_filter_has_both_sides() -> None:
    index = _load(f"{_export_id()}/{PAPERS_INDEX_FILE}")

    # 期刊、年份、主題、方法各有好幾個值；有沒有摘要、有沒有完整內容、本週新增、已核對兩邊都有。
    assert len(_count(index["journal"])) >= 3
    assert len(_count(index["year"])) >= 3
    topics = _count(key for keys in index["topics"] if keys for key in keys)
    methods = _count(key for keys in index["methods"] if keys for key in keys)
    assert len(topics) >= 5
    assert "unclassified" in topics
    assert len(methods) >= 4
    assert any(keys is not None and len(keys) > 1 for keys in index["topics"])
    for column in ("has_abstract", "has_full_content", "is_new_this_week"):
        assert set(index[column]) == {True, False}, column
    assert {True, False} <= set(index["topics_verified"])
    assert {"ready", "failed", "pending_batch", None} <= set(index["classification_state"])
