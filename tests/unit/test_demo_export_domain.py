"""示範站匯出的白名單模型與自我檢查（Story 3.7、ADR-0007「寫出之前的自我檢查」、ADR-0061）。

拿給前端的範例資料（`web/e2e/fixtures/demo-preview/`，全部是假論文）當作一份正常的匯出，逐項改壞，
確認每一種失敗都擋得下來。
"""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from radar.domain.demo_export import (
    MANIFEST_FILE,
    DemoPaper,
    DemoPaperShard,
    DemoPapersIndex,
    DemoTitles,
    ForbiddenText,
    check_demo_files,
    data_file_path,
    export_id_for,
    model_for_path,
    shard_file_name,
)
from radar.domain.jobs import RUN_DEMO_EXPORT
from radar.domain.sweep import SWEEP_REGISTRY
from tests.permissions.framework import CANARY

ROOT = Path(__file__).resolve().parents[2]
PREVIEW = ROOT / "web" / "e2e" / "fixtures" / "demo-preview"
SHARD = "2-1/papers/000.json"
INDEX = "2-1/papers-index.json"
EMAIL = "someone.member@example.org"


def _files() -> dict[str, bytes]:
    return {
        path.relative_to(PREVIEW).as_posix(): path.read_bytes()
        for path in sorted(PREVIEW.rglob("*"))
        if path.is_file()
    }


def _full_ids(files: dict[str, bytes]) -> set[uuid.UUID]:
    index = json.loads(files[INDEX])
    return {
        uuid.UUID(paper_id)
        for paper_id, full in zip(index["id"], index["has_full_content"], strict=True)
        if full
    }


def _forbidden() -> list[ForbiddenText]:
    return [ForbiddenText("金絲雀字串", CANARY), ForbiddenText("成員的 email", EMAIL)]


def _edit(files: dict[str, bytes], path: str, change: Any) -> dict[str, bytes]:
    data = json.loads(files[path])
    change(data)
    return {**files, path: json.dumps(data, ensure_ascii=False).encode("utf-8")}


def test_the_preview_example_passes_every_check() -> None:
    files = _files()

    assert check_demo_files(files, full_paper_ids=_full_ids(files), forbidden=_forbidden()) == []


# --- 四種失敗各一條（AC：多餘欄位、不該有完整內容的論文帶了完整內容、金絲雀字串、成員 email）---


def test_a_field_outside_the_whitelist_fails() -> None:
    files = _files()

    def add_field(data: dict[str, Any]) -> None:
        data["papers"][0]["biblio"]["biblio"]["member_email_hint"] = "x"

    broken = _edit(files, SHARD, add_field)
    problems = check_demo_files(broken, full_paper_ids=_full_ids(files), forbidden=[])

    assert len(problems) == 1
    assert problems[0].startswith(f"{SHARD}：不符合白名單模型 DemoPaperShard")
    assert "extra_forbidden" in problems[0]


def test_full_content_for_a_paper_outside_demo_full_paper_fails() -> None:
    files = _files()
    full_ids = _full_ids(files)
    papers = json.loads(files[SHARD])["papers"]
    victim = next(paper for paper in papers if paper["full"] is not None)
    victim_id = uuid.UUID(victim["biblio"]["biblio"]["id"])

    # 檔案本身沒變、形狀也對，但這篇不在 view `demo_full_paper` 裡（例如被改成 IEEE）。
    problems = check_demo_files(files, full_paper_ids=full_ids - {victim_id}, forbidden=[])

    assert problems == [
        f"{INDEX}：不在 demo_full_paper 的論文 {victim_id} 標成有完整內容",
        f"{SHARD}：不在 demo_full_paper 的論文 {victim_id} 帶了完整內容",
    ]


def test_full_content_check_does_not_rely_on_the_model() -> None:
    # 模型壞掉（多一個欄位）時，完整內容那一項照樣檢查得到。
    files = _files()
    full_ids = _full_ids(files)
    target = next(
        paper for paper in json.loads(files[SHARD])["papers"] if paper["full"] is not None
    )
    target_id = uuid.UUID(target["biblio"]["biblio"]["id"])

    def break_model(data: dict[str, Any]) -> None:
        data["unexpected"] = True

    problems = check_demo_files(
        _edit(files, SHARD, break_model), full_paper_ids=full_ids - {target_id}, forbidden=[]
    )

    assert f"{SHARD}：不在 demo_full_paper 的論文 {target_id} 帶了完整內容" in problems


def test_canary_anywhere_fails() -> None:
    files = _files()

    def plant(data: dict[str, Any]) -> None:
        data["titles"][3] = f"Fake Paper {CANARY.upper()}"

    problems = check_demo_files(
        _edit(files, "2-1/titles.json", plant),
        full_paper_ids=_full_ids(files),
        forbidden=_forbidden(),
    )

    assert problems == ["2-1/titles.json：出現金絲雀字串"]


@pytest.mark.parametrize(
    "written",
    [EMAIL, EMAIL.upper(), EMAIL.replace("@", "\\u0040")],
    ids=["as-is", "upper", "json-escaped"],
)
def test_member_email_anywhere_fails(written: str) -> None:
    files = _files()
    raw = files[MANIFEST_FILE].decode("utf-8")
    # 直接改文字（不經 json.dumps），模擬各種寫進 JSON 的樣子；跳脫過的寫法也要找得到。
    planted = raw.replace('"export_id":"2-1"', f'"export_id":"2-1","x":"{written}"', 1)

    problems = check_demo_files(
        {**files, MANIFEST_FILE: planted.encode("utf-8")},
        full_paper_ids=_full_ids(files),
        forbidden=_forbidden(),
    )

    assert f"{MANIFEST_FILE}：出現成員的 email" in problems
    assert all(EMAIL not in problem.casefold() for problem in problems)


def test_unknown_file_fails() -> None:
    files = {**_files(), "2-1/members.json": b"{}"}

    problems = check_demo_files(files, full_paper_ids=_full_ids(files), forbidden=[])

    assert problems == ["2-1/members.json：不是認得的資料檔"]


# --- 模型本身 ---------------------------------------------------------------------------------


def test_paper_full_layer_only_with_full_content() -> None:
    papers = json.loads(_files()[SHARD])["papers"]
    with_full = next(paper for paper in papers if paper["full"] is not None)
    without = next(paper for paper in papers if paper["full"] is None)

    with pytest.raises(ValidationError, match="只有 has_full_content 的論文有 full"):
        DemoPaper.model_validate({**without, "full": with_full["full"]})
    flipped = {**with_full, "biblio": {**with_full["biblio"], "has_full_content": False}}
    with pytest.raises(ValidationError, match="只有 has_full_content 的論文有 full"):
        DemoPaper.model_validate(flipped)


def test_index_columns_must_line_up() -> None:
    data = json.loads(_files()[INDEX])
    data["doi"] = data["doi"][:-1]

    with pytest.raises(ValidationError, match="每一欄的長度要一樣"):
        DemoPapersIndex.model_validate(data)


def test_shard_holds_at_most_two_hundred_papers() -> None:
    data = json.loads(_files()[SHARD])
    data["papers"] = data["papers"] * 4  # 60 × 4 = 240

    with pytest.raises(ValidationError, match="at most 200"):
        DemoPaperShard.model_validate(data)


def test_titles_cannot_be_empty_strings() -> None:
    data = json.loads(_files()["2-1/titles.json"])
    data["titles"][0] = ""

    with pytest.raises(ValidationError):
        DemoTitles.model_validate(data)


def test_paths_and_ids() -> None:
    assert export_id_for(12, 2) == "12-2"
    assert data_file_path("12-2", "papers/000.json") == "12-2/papers/000.json"
    assert shard_file_name(0) == "000.json"
    assert shard_file_name(49) == "049.json"
    for bad in (0, -1, True):
        with pytest.raises(ValueError, match="正整數"):
            export_id_for(bad, 1)
    with pytest.raises(ValueError, match="分片編號"):
        shard_file_name(1000)
    with pytest.raises(ValueError, match="匯出編號"):
        data_file_path("../x", "titles.json")
    assert model_for_path("manifest.json") is not None
    assert model_for_path("12-2/papers/007.json") is DemoPaperShard
    for unknown in ("12-2/manifest.json", "x/titles.json", "12-2/papers/7.json", "titles.json"):
        assert model_for_path(unknown) is None, unknown


def test_sweep_watches_queued_and_running_exports() -> None:
    entries = {
        entry.start_status: entry for entry in SWEEP_REGISTRY if entry.table == "demo_export"
    }

    assert set(entries) == {"queued", "running"}
    assert {entry.job for entry in entries.values()} == {RUN_DEMO_EXPORT}
    assert {entry.time_column for entry in entries.values()} == {"updated_at"}
