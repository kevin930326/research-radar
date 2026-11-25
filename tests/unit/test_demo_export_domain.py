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
    DemoFileEntry,
    DemoManifest,
    DemoPaper,
    DemoPaperShard,
    DemoPapersIndex,
    DemoPublisherFacts,
    DemoTitles,
    ForbiddenText,
    check_demo_files,
    data_file_path,
    excluded_publisher_paper_ids,
    export_id_for,
    export_order,
    model_for_path,
    shard_file_name,
)
from radar.domain.jobs import RUN_DEMO_EXPORT
from radar.domain.policy import PublisherExclusions
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


# 範例裡示範站有完整內容的論文（標題的編號；`tests/demo_export_support.preview_specs` 算出來的）：
# 固定的 13 篇裡 CC BY、出版社可以放、有 OpenAlex 摘要的 7 篇，加上第 14 篇起「授權是 CC BY、
# 有摘要」的（第 n 篇的 i＝n − 14，i % 3 ≠ 0 而且 i % 7 ≠ 0）。寫死，不從範例自己的索引算（F16）。
FULL_CONTENT_NUMBERS = frozenset(
    {1, 4, 5, 6, 11, 12, 13}
    | {15, 16, 18, 19, 22, 24, 25, 27, 30, 31, 33, 34, 36, 37, 39, 40, 43, 45, 46, 48}
    | {51, 52, 54, 55, 57, 58, 60}
)


def _ids_by_number(files: dict[str, bytes]) -> dict[int, uuid.UUID]:
    """標題的編號 → 論文 ID（`titles.json` 與 `papers-index.json` 的列順序相同）。"""
    titles = json.loads(files["2-1/titles.json"])["titles"]
    ids = json.loads(files[INDEX])["id"]
    return {
        int(title.removeprefix("Fake Paper ").split(":", 1)[0]): uuid.UUID(paper_id)
        for title, paper_id in zip(titles, ids, strict=True)
    }


def _full_ids(files: dict[str, bytes]) -> set[uuid.UUID]:
    by_number = _ids_by_number(files)
    return {by_number[number] for number in FULL_CONTENT_NUMBERS}


def _forbidden() -> list[ForbiddenText]:
    return [ForbiddenText("金絲雀字串", CANARY), ForbiddenText("成員的 email", EMAIL)]


def _edit(files: dict[str, bytes], path: str, change: Any) -> dict[str, bytes]:
    data = json.loads(files[path])
    change(data)
    return {**files, path: json.dumps(data, ensure_ascii=False).encode("utf-8")}


def test_the_preview_example_passes_every_check() -> None:
    files = _files()
    index = json.loads(files[INDEX])
    flagged = {
        uuid.UUID(paper_id)
        for paper_id, full in zip(index["id"], index["has_full_content"], strict=True)
        if full
    }

    # 範例標成有完整內容的，正好是照測試資料算出來的那幾篇。
    assert flagged == _full_ids(files)
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


@pytest.mark.parametrize(
    "written",
    [
        EMAIL.replace("@", "\uff20"),  # 全形的 ＠
        "ｓｏｍｅｏｎｅ.member@example.org",  # 全形英文字母
        EMAIL.replace("@", "%40"),  # 百分比編碼
        EMAIL.replace("@", "%40").upper(),
    ],
    ids=["fullwidth-at", "fullwidth-letters", "percent-encoded", "percent-encoded-upper"],
)
def test_member_email_in_other_unicode_forms_fails(written: str) -> None:
    files = _files()

    def plant(data: dict[str, Any]) -> None:
        data["titles"][0] = f"Fake Paper {written}"

    problems = check_demo_files(
        _edit(files, "2-1/titles.json", plant),
        full_paper_ids=_full_ids(files),
        forbidden=_forbidden(),
    )

    assert problems == ["2-1/titles.json：出現成員的 email"]


def test_canary_with_combining_characters_fails() -> None:
    # 同一個字的兩種寫法（合成字元 é 與 e＋組合重音）：兩邊先正規化再比。
    files = _files()
    composed = "canary-jos\u00e9"
    decomposed = "canary-jose\u0301"

    def plant(data: dict[str, Any]) -> None:
        data["titles"][0] = f"Fake Paper {decomposed}"

    problems = check_demo_files(
        _edit(files, "2-1/titles.json", plant),
        full_paper_ids=_full_ids(files),
        forbidden=[ForbiddenText("金絲雀字串", composed)],
    )

    assert problems == ["2-1/titles.json：出現金絲雀字串"]


def test_problems_never_echo_unknown_keys_or_unreadable_ids() -> None:
    # 被擋的字串出現在多出來的鍵名、或看不懂的論文 ID 裡：失敗原因只印已知欄位名、位置、
    # 錯誤代碼（F12）。
    files = _files()

    def as_key(data: dict[str, Any]) -> None:
        data["papers"][0]["biblio"][EMAIL] = True

    def as_id(data: dict[str, Any]) -> None:
        data["id"][0] = EMAIL
        data["has_full_content"][0] = True

    broken = _edit(_edit(files, SHARD, as_key), INDEX, as_id)
    problems = check_demo_files(broken, full_paper_ids=_full_ids(files), forbidden=_forbidden())

    assert f"{SHARD}：出現成員的 email" in problems
    assert any(
        problem.startswith(f"{SHARD}：不符合白名單模型 DemoPaperShard") for problem in problems
    )
    assert any("第 0 列（ID 看不懂）的論文 標成有完整內容" in problem for problem in problems)
    assert all(EMAIL not in problem.casefold() for problem in problems)
    assert all("someone" not in problem for problem in problems)


def test_excluded_publisher_paper_with_full_content_fails() -> None:
    # 自我檢查第 6 項：出版社或期刊在排除清單裡的論文，就算在 demo_full_paper 名單裡也不能有
    # 完整內容。
    files = _files()
    full_ids = _full_ids(files)
    victim = _ids_by_number(files)[1]

    problems = check_demo_files(
        files, full_paper_ids=full_ids, forbidden=[], excluded_paper_ids={victim}
    )

    assert problems == [
        f"{INDEX}：排除清單裡的出版社或期刊的論文 {victim} 標成有完整內容",
        f"{SHARD}：排除清單裡的出版社或期刊的論文 {victim} 帶了完整內容",
    ]


def test_excluded_publisher_ids_use_the_exclusion_list_directly() -> None:
    exclusions = PublisherExclusions(
        ids=frozenset({"P4310319808"}),
        names=frozenset({"Institute of Electrical and Electronics Engineers"}),
        journal_issn_ls=frozenset({"0018-9391"}),
    )
    papers = [
        DemoPublisherFacts(uuid.UUID(int=1), ("https://openalex.org/P4310319808",), (), None),
        DemoPublisherFacts(
            uuid.UUID(int=2), (), ("IEEE-Institute of Electrical and Electronics Engineers",), None
        ),
        DemoPublisherFacts(
            uuid.UUID(int=3), (), ("INSTITUTE OF ELECTRICAL AND ELECTRONICS ENGINEERS",), None
        ),
        DemoPublisherFacts(
            uuid.UUID(int=4), ("P9000000077",), ("The Engineering Press",), "0018-9391"
        ),
        DemoPublisherFacts(
            uuid.UUID(int=5), ("P9000000001",), ("Example Open Press",), "0276-7783"
        ),
    ]

    assert excluded_publisher_paper_ids(papers, exclusions) == {
        uuid.UUID(int=1),
        uuid.UUID(int=3),
        uuid.UUID(int=4),
    }


def test_full_content_must_belong_to_its_own_paper() -> None:
    # 一篇可以公開的論文底下掛著另一篇論文的完整內容：模型與自我檢查都擋（F16）。
    files = _files()
    by_number = _ids_by_number(files)
    papers = json.loads(files[SHARD])["papers"]
    host = next(paper for paper in papers if paper["biblio"]["biblio"]["id"] == str(by_number[1]))
    swapped = {**host, "full": {**host["full"], "paper_id": str(by_number[3])}}

    with pytest.raises(ValidationError, match="full 的 paper_id 要和書目的 id 一樣"):
        DemoPaper.model_validate(swapped)

    def swap(data: dict[str, Any]) -> None:
        for paper in data["papers"]:
            if paper["biblio"]["biblio"]["id"] == str(by_number[1]):
                paper["full"]["paper_id"] = str(by_number[3])

    problems = check_demo_files(
        _edit(files, SHARD, swap), full_paper_ids=_full_ids(files), forbidden=[]
    )

    assert f"{SHARD}：論文 {by_number[1]} 的完整內容不是它自己的（paper_id 對不上）" in problems
    assert any(
        problem.startswith(f"{SHARD}：不符合白名單模型 DemoPaperShard") for problem in problems
    )


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


def test_manifest_lists_only_known_files_of_its_own_export() -> None:
    manifest = json.loads(_files()[MANIFEST_FILE])
    entry = manifest["files"][0]
    for bad in (
        "2-1/../../x.json",
        "2-1/papers/../titles.json",
        "2-1/members.json",
        "2-1/papers/7.json",
    ):
        with pytest.raises(ValidationError):
            DemoFileEntry.model_validate({**entry, "path": bad})
    other = {**manifest, "files": [{**entry, "path": "3-1/titles.json"}]}
    with pytest.raises(ValidationError, match="自己這次的匯出目錄"):
        DemoManifest.model_validate(other)
    twice = {**manifest, "files": [entry, entry]}
    with pytest.raises(ValidationError, match="不能重複"):
        DemoManifest.model_validate(twice)


def test_paths_and_ids() -> None:
    assert export_id_for(12, 2) == "12-2"
    assert export_order("12-2") == (12, 2)
    assert export_order("3-10") > export_order("3-9") > export_order("2-99")
    with pytest.raises(ValueError, match="匯出編號"):
        export_order("../x")
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
