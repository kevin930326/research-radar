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

from radar.config import load_domain_config
from radar.domain.contracts import CubeRow, SnapshotRef, TrendParams
from radar.domain.demo_export import (
    CUBE_FILE,
    MANIFEST_FILE,
    DemoCube,
    check_demo_files,
    data_file_path,
    model_for_path,
)

ROOT = Path(__file__).resolve().parents[2]
PREVIEW = ROOT / "web" / "e2e" / "fixtures" / "demo-preview"
CUBE = data_file_path("2-1", CUBE_FILE)
INDEX = "2-1/papers-index.json"
# 設定檔的期刊代號：自我檢查只印這些（其他的寫第幾格）。
JOURNAL_KEYS = [
    journal.key
    for journal in load_domain_config({"RADAR_CONFIG_DIR": str(ROOT / "config")}).journals.journals
]
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


def _all_problems(files: dict[str, bytes]) -> list[str]:
    # 其他項（完整內容、示範站可用）的輸入給範例裡的全部論文，範例本來就通過。
    ids = [uuid.UUID(paper_id) for paper_id in json.loads(_files()[INDEX])["id"]]
    return check_demo_files(
        files, full_paper_ids=ids, forbidden=[], eligible_paper_ids=ids, journal_keys=JOURNAL_KEYS
    )


def _cube_problems(files: dict[str, bytes]) -> list[str]:
    # 只看計數方塊這一項。
    return [problem for problem in _all_problems(files) if problem.startswith(CUBE)]


def _manifest_problems(files: dict[str, bytes]) -> list[str]:
    # 只看 manifest 那兩項（列的檔、summary 的讀數）。
    return [problem for problem in _all_problems(files) if problem.startswith(MANIFEST_FILE)]


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
    # manifest 的 summary 也用索引重算過（第 9 項），範例的讀數對得上。
    assert _manifest_problems(files) == []
    summary = json.loads(files[MANIFEST_FILE])["summary"]
    assert (summary["paper_count"], summary["with_abstract_count"]) == (60, 51)


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


def test_a_known_journal_is_named_in_the_problem() -> None:
    broken = _edit(_files(), CUBE, _bump_first_total)
    first = _rows(_files())[0]

    assert any(
        problem.startswith(f"{CUBE}：{first['journal']} {first['year']} 的計數")
        for problem in _cube_problems(broken)
    )


def test_an_unknown_journal_value_is_never_echoed() -> None:
    # 合法的 JSON、但期刊欄位是白名單不收的值（例如混進了別的文字）：重算照樣擋，訊息只寫第幾格。
    secret = "Secret Member Note"

    def plant(data: dict[str, Any]) -> None:
        # 先記下第一格的鍵再改（直接拿第一列比，改了第一列之後同一格的 with_abstract 就比不到）。
        cell = (data["rows"][0]["journal"], data["rows"][0]["year"])
        for row in data["rows"]:
            if (row["journal"], row["year"]) == cell:
                row["journal"] = secret

    problems = _all_problems(_edit(_files(), CUBE, plant))

    assert any(problem.startswith(CUBE) and "（期刊不在設定檔）" in problem for problem in problems)
    assert all(secret not in problem for problem in problems)


@pytest.mark.parametrize("field_name", ["paper_count", "with_abstract_count", "full_content_count"])
def test_a_wrong_summary_count_in_the_manifest_fails(field_name: str) -> None:
    # 只改錯 manifest 的一個讀數（其他檔都不動、模型照樣通過）：用索引重算時擋下。
    def bump(data: dict[str, Any]) -> None:
        data["summary"][field_name] += 1

    files = _files()
    want = json.loads(files[MANIFEST_FILE])["summary"][field_name]

    problems = _manifest_problems(_edit(files, MANIFEST_FILE, bump))

    assert problems == [
        f"{MANIFEST_FILE}：summary.{field_name}（{want + 1}）和 papers-index.json 重算的"
        f"（{want}）不同"
    ]


def test_a_listed_file_that_is_missing_fails() -> None:
    # manifest 照樣列著 cube.json（模型通過），實際的檔裡卻沒有：第 8 項擋下。
    files = {path: content for path, content in _files().items() if path != CUBE}

    assert _manifest_problems(files) == [f"{MANIFEST_FILE}：列了 {CUBE}，實際沒有這個檔"]


def test_a_data_file_the_manifest_does_not_list_fails() -> None:
    def unlist_cube(data: dict[str, Any]) -> None:
        data["files"] = [entry for entry in data["files"] if entry["path"] != CUBE]

    problems = _all_problems(_edit(_files(), MANIFEST_FILE, unlist_cube))

    assert f"{CUBE}：manifest.json 沒有列" in problems


def test_an_odd_listed_path_is_not_echoed() -> None:
    secret = "../secret-member.json"

    def odd_path(data: dict[str, Any]) -> None:
        data["files"][0]["path"] = secret

    problems = _all_problems(_edit(_files(), MANIFEST_FILE, odd_path))

    assert f"{MANIFEST_FILE}：列了 第 1 個檔，實際沒有這個檔" in problems
    assert all(secret not in problem for problem in problems)


# --- 第 2 輪：獨立重算不靠模型也擋得下（布林、結構、manifest 缺檔、summary 型別）---


def _first_cell_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    cell = (data["rows"][0]["journal"], data["rows"][0]["year"])
    return [row for row in data["rows"] if (row["journal"], row["year"]) == cell]


def _cube_check_only(files: dict[str, bytes]) -> list[str]:
    # 只看重算那一項（排除白名單模型那一句），證明這一項自己就擋得下。
    return [problem for problem in _cube_problems(files) if "白名單模型" not in problem]


def _set_one_total_to_true(data: dict[str, Any]) -> None:
    row = next(row for row in data["rows"] if row["dim"] == "total" and row["n"] == 1)
    row["n"] = True


def _with_a_zero_abstract_cell() -> dict[str, bytes]:
    """範例裡沒有「有摘要 0 篇」的格子：把只有 1 篇的那一格（索引與 cube.json 一起）改成那篇沒有
    摘要，得到一份兩邊仍然對得上的檔。"""
    files = _files()
    cell = next(
        (row["journal"], row["year"])
        for row in _rows(files)
        if row["dim"] == "total" and row["n"] == 1
    )

    def no_abstract_in_index(data: dict[str, Any]) -> None:
        for position, key in enumerate(zip(data["journal"], data["year"], strict=True)):
            if key == cell:
                data["has_abstract"][position] = False

    def no_abstract_in_cube(data: dict[str, Any]) -> None:
        for row in data["rows"]:
            if (row["journal"], row["year"]) == cell and row["dim"] == "with_abstract":
                row["n"] = 0

    return _edit(_edit(files, INDEX, no_abstract_in_index), CUBE, no_abstract_in_cube)


def _set_one_abstract_to_false(data: dict[str, Any]) -> None:
    row = next(row for row in data["rows"] if row["dim"] == "with_abstract" and row["n"] == 0)
    row["n"] = False


def test_the_bool_tests_start_from_matching_files() -> None:
    # 前提：有 1 篇的格子、改出來的「有摘要 0 篇」那份兩邊仍然對得上（不然下面改不到東西）。
    assert any(row["dim"] == "total" and row["n"] == 1 for row in _rows(_files()))
    files = _with_a_zero_abstract_cell()
    assert any(row["dim"] == "with_abstract" and row["n"] == 0 for row in _rows(files))
    assert _cube_problems(files) == []


@pytest.mark.parametrize(
    "change",
    [_set_one_total_to_true, _set_one_abstract_to_false],
    ids=["n 是 true（剛好等於 1）", "n 是 false（剛好等於 0）"],
)
def test_a_boolean_count_fails_both_the_model_and_the_recount(
    change: Callable[[Any], None],
) -> None:
    broken = _edit(_with_a_zero_abstract_cell(), CUBE, change)

    assert any("不符合白名單模型 DemoCube" in problem for problem in _cube_problems(broken))
    assert _cube_check_only(broken) == [
        f"{CUBE}：無法和 papers-index.json 對照重算（有一列的期刊、年份或篇數形狀不對）"
    ]


def test_a_boolean_year_fails_the_recount() -> None:
    def year_true(data: dict[str, Any]) -> None:
        data["rows"][0]["year"] = True

    broken = _edit(_files(), CUBE, year_true)

    assert _cube_check_only(broken) == [
        f"{CUBE}：無法和 papers-index.json 對照重算（有一列的期刊、年份或篇數形狀不對）"
    ]


@pytest.mark.parametrize("field_name", ["year", "n"])
def test_cube_row_rejects_booleans(field_name: str) -> None:
    row = {**_row("total", 1), field_name: True}

    with pytest.raises(ValidationError):
        CubeRow.model_validate(row)
    with pytest.raises(ValidationError):
        CubeRow.model_validate_json(json.dumps(row))


def _add_an_empty_cell(data: dict[str, Any]) -> None:
    # 索引沒有的格子、兩列都是 0 篇：以前會和「沒有」的預設值 (0, 0) 相等而漏掉。
    data["rows"] += [
        {"journal": "jit", "year": 2016, "dim": dim, "key_a": None, "key_b": None, "n": 0}
        for dim in ("total", "with_abstract")
    ]


def test_an_extra_empty_cell_fails_the_recount() -> None:
    broken = _edit(_files(), CUBE, _add_an_empty_cell)

    assert _cube_check_only(broken) == [
        f"{CUBE}：jit 2016 的計數和 papers-index.json 重算的不同"
        "（papers-index.json 沒有這一格，cube.json 列了 total 0）"
    ]


def _split_a_total_into_two_rows(data: dict[str, Any]) -> None:
    # 重複的 total 列、加起來剛好等於原本的篇數。
    row = next(row for row in data["rows"] if row["dim"] == "total" and row["n"] >= 2)
    row["n"] -= 1
    data["rows"].append({**row, "n": 1})


def _drop_a_zero_abstract_row(data: dict[str, Any]) -> None:
    # 有摘要 0 篇的那一列漏掉：以前補成 0 而對得上。
    position = next(
        position
        for position, row in enumerate(data["rows"])
        if row["dim"] == "with_abstract" and row["n"] == 0
    )
    del data["rows"][position]


def _add_an_other_kind_of_row(data: dict[str, Any]) -> None:
    first = data["rows"][0]
    data["rows"].append({**first, "dim": "topic", "key_a": "ai", "n": 1})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_split_a_total_into_two_rows, "同一格的同一種列重複"),
        (_drop_a_zero_abstract_row, "有一格缺 total 或 with_abstract 的列"),
        (_add_an_other_kind_of_row, "有 total、with_abstract 以外的列"),
    ],
    ids=["重複的列加起來剛好", "漏了 0 篇的 with_abstract", "其他種類的列"],
)
def test_structural_gaps_fail_the_recount(change: Callable[[Any], None], reason: str) -> None:
    broken = _edit(_with_a_zero_abstract_cell(), CUBE, change)

    assert _cube_check_only(broken) == [f"{CUBE}：無法和 papers-index.json 對照重算（{reason}）"]


def test_a_non_boolean_has_abstract_in_the_index_fails() -> None:
    # 索引的 has_abstract 混進不是布林的值：不默默當成沒有摘要。
    def plant(data: dict[str, Any]) -> None:
        data["has_abstract"][data["has_abstract"].index(False)] = 0

    problems = _all_problems(_edit(_files(), INDEX, plant))

    assert f"{CUBE}：無法和 papers-index.json 對照重算（欄位的形狀不對）" in problems
    assert (
        f"{MANIFEST_FILE}：papers-index.json 的形狀不對，無法重算 with_abstract_count" in problems
    )


def test_a_missing_manifest_fails() -> None:
    files = {path: content for path, content in _files().items() if path != MANIFEST_FILE}

    assert _manifest_problems(files) == [f"{MANIFEST_FILE}：沒有這個檔，無法核對列的檔與讀數"]


@pytest.mark.parametrize(
    "value",
    [60.0, True, "60"],
    ids=["60.0", "true", "字串"],
)
def test_summary_count_must_be_a_json_integer(value: object) -> None:
    files = _files()
    assert json.loads(files[MANIFEST_FILE])["summary"]["paper_count"] == 60

    def plant(data: dict[str, Any]) -> None:
        data["summary"]["paper_count"] = value

    problems = _manifest_problems(_edit(files, MANIFEST_FILE, plant))

    assert f"{MANIFEST_FILE}：summary.paper_count（?）和 papers-index.json 重算的（60）不同" in (
        problems
    )


def test_a_non_boolean_full_content_flag_in_the_index_fails_the_summary() -> None:
    def plant(data: dict[str, Any]) -> None:
        data["has_full_content"][0] = "yes"

    problems = _manifest_problems(_edit(_files(), INDEX, plant))

    assert f"{MANIFEST_FILE}：papers-index.json 的形狀不對，無法重算 full_content_count" in problems
