"""示範站的計數方塊 `cube.json`（Story 3.15、ADR-0007 自我檢查第 4 項、ADR-0062）。

拿前端的範例資料（`web/e2e/fixtures/demo-preview/`，全部是假論文，由 `tests/demo_preview.py`
用正式的匯出產生）當作一份正常的匯出：先確認範例本身的計數，再逐項改壞 `cube.json`，確認自我檢查
用 `papers-index.json` 重算時每一種不一致都擋得下來。
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from radar.domain.contracts import CubeRow, SnapshotRef, TrendParams
from radar.domain.demo_export import (
    CUBE_FILE,
    DemoCube,
    check_demo_files,
    data_file_path,
    model_for_path,
)

ROOT = Path(__file__).resolve().parents[2]
PREVIEW = ROOT / "web" / "e2e" / "fixtures" / "demo-preview"
CUBE = data_file_path("2-1", CUBE_FILE)
INDEX = "2-1/papers-index.json"
PARAMS = {
    "timezone": "Asia/Taipei",
    "tracking_start_year": 2016,
    "current_year_cutoff": "07-01",
    "growth_window_years": 3,
    "baseline_years": 3,
    "low_coverage_threshold": 0.5,
}
SNAPSHOT = {"number": 2, "as_of": "2026-11-16T03:00:00Z"}


def _files() -> dict[str, bytes]:
    return {
        path.relative_to(PREVIEW).as_posix(): path.read_bytes()
        for path in sorted(PREVIEW.rglob("*"))
        if path.is_file()
    }


def _cube_problems(files: dict[str, bytes]) -> list[str]:
    # 只看計數方塊這一項：其他項（完整內容、示範站可用）的輸入給範例裡的全部論文，範例本來就通過。
    ids = [uuid.UUID(paper_id) for paper_id in json.loads(_files()[INDEX])["id"]]
    problems = check_demo_files(files, full_paper_ids=ids, forbidden=[], eligible_paper_ids=ids)
    return [problem for problem in problems if problem.startswith(CUBE)]


def _edit(files: dict[str, bytes], path: str, change: Callable[[Any], None]) -> dict[str, bytes]:
    data = json.loads(files[path])
    change(data)
    return {**files, path: json.dumps(data, ensure_ascii=False).encode("utf-8")}


def _rows(files: dict[str, bytes]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(files[CUBE])["rows"]
    return rows


def test_cube_is_a_known_data_file() -> None:
    assert model_for_path(CUBE) is DemoCube


def test_the_preview_cube_matches_the_index_and_the_summary() -> None:
    files = _files()
    rows = _rows(files)
    cube = json.loads(files[CUBE])

    assert _cube_problems(files) == []
    # 範例 60 篇、其中 51 篇有摘要（manifest 的 summary，寫死在 test_demo_preview_fixture）。
    assert sum(row["n"] for row in rows if row["dim"] == "total") == 60
    assert sum(row["n"] for row in rows if row["dim"] == "with_abstract") == 51
    assert {row["dim"] for row in rows} == {"total", "with_abstract"}
    assert cube["params"] == PARAMS
    assert cube["snapshot"] == json.loads(files[INDEX])["snapshot"]


def _bump_first_total(data: dict[str, Any]) -> None:
    first = next(row for row in data["rows"] if row["dim"] == "total")
    first["n"] += 1


def _drop_first_abstract(data: dict[str, Any]) -> None:
    first = next(row for row in data["rows"] if row["dim"] == "with_abstract" and row["n"] > 0)
    first["n"] -= 1


def _drop_one_cell(data: dict[str, Any]) -> None:
    journal, year = data["rows"][0]["journal"], data["rows"][0]["year"]
    data["rows"] = [row for row in data["rows"] if (row["journal"], row["year"]) != (journal, year)]


def _add_a_cell(data: dict[str, Any]) -> None:
    data["rows"] += [
        {"journal": "jit", "year": 2016, "dim": "total", "key_a": None, "key_b": None, "n": 1},
        {
            "journal": "jit",
            "year": 2016,
            "dim": "with_abstract",
            "key_a": None,
            "key_b": None,
            "n": 0,
        },
    ]


def _move_a_paper_to_another_year(data: dict[str, Any]) -> None:
    # 總數不變，只是年份錯了：重算逐格比，一樣擋下。
    totals = [row for row in data["rows"] if row["dim"] == "total" and row["n"] >= 2]
    source = totals[0]
    target = next(row for row in data["rows"] if row["dim"] == "total" and row is not source)
    source["n"] -= 1
    target["n"] += 1


@pytest.mark.parametrize(
    "change",
    [
        _bump_first_total,
        _drop_first_abstract,
        _drop_one_cell,
        _add_a_cell,
        _move_a_paper_to_another_year,
    ],
    ids=["total 多一篇", "with_abstract 少一篇", "少一格", "多一格", "一篇算到別的年份"],
)
def test_counts_that_do_not_match_the_index_fail(change: Callable[[Any], None]) -> None:
    broken = _edit(_files(), CUBE, change)

    # 擋下的是重算那一項（白名單模型照樣通過：改的只是篇數）。
    assert any("重算的不同" in problem for problem in _cube_problems(broken))


def test_index_changes_are_caught_too() -> None:
    # 反過來：索引的一篇改成沒有摘要，cube.json 不變，也對不上。
    def drop_abstract(data: dict[str, Any]) -> None:
        position = data["has_abstract"].index(True)
        data["has_abstract"][position] = False

    broken = _edit(_files(), INDEX, drop_abstract)

    assert any("with_abstract" in problem for problem in _cube_problems(broken))


def test_a_different_snapshot_or_export_fails() -> None:
    def other_snapshot(data: dict[str, Any]) -> None:
        data["snapshot"]["number"] = 1

    def other_export(data: dict[str, Any]) -> None:
        data["export_id"] = "2-2"

    for change, key in ((other_snapshot, "snapshot"), (other_export, "export_id")):
        problems = _cube_problems(_edit(_files(), CUBE, change))
        assert f"{CUBE}：{key} 和 papers-index.json 不同" in problems


def test_cube_without_its_index_fails() -> None:
    files = {path: content for path, content in _files().items() if path != INDEX}

    assert any("無法重算" in problem for problem in _cube_problems(files))


def test_broken_cube_json_is_reported_without_echoing_it() -> None:
    files = {**_files(), CUBE: b'{"rows": "secret-text"'}

    problems = _cube_problems(files)

    assert problems
    assert all("secret-text" not in problem for problem in problems)


def _cube(rows: list[dict[str, Any]]) -> DemoCube:
    return DemoCube(
        snapshot=SnapshotRef.model_validate(SNAPSHOT),
        export_id="2-1",
        params=TrendParams.model_validate(PARAMS),
        rows=tuple(CubeRow.model_validate(row) for row in rows),
    )


def _row(dim: str, n: int, *, journal: str = "misq", year: int = 2024) -> dict[str, Any]:
    return {"journal": journal, "year": year, "dim": dim, "n": n}


def test_demo_cube_model_accepts_matching_pairs() -> None:
    cube = _cube([_row("total", 4), _row("with_abstract", 0)])

    assert len(cube.rows) == 2


@pytest.mark.parametrize(
    "rows",
    [
        [_row("total", 4)],  # 少了 with_abstract
        [_row("total", 4), _row("with_abstract", 5)],  # 有摘要比總數多
        [_row("total", 0), _row("with_abstract", 0)],  # 沒有論文的格子不列
        [_row("total", 4), _row("with_abstract", 1), _row("total", 4)],  # 重複
        [_row("total", 4), _row("with_abstract", 1), {**_row("topic", 1), "key_a": "ai"}],
    ],
    ids=["缺 with_abstract", "有摘要比總數多", "0 篇的格子", "重複的列", "其他種類的列"],
)
def test_demo_cube_model_rejects_bad_rows(rows: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        _cube(rows)


def test_demo_cube_model_forbids_extra_fields() -> None:
    data = json.loads(_files()[CUBE])
    data["ratio"] = 0.5

    with pytest.raises(ValidationError):
        DemoCube.model_validate(data)
