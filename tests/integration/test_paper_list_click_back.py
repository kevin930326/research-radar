"""「點得回去」（Story 3.13、AD-19、ADR-0011「點數字列出論文」）：計數方塊的每一格，用它的條件查
找論文清單（`scope=tracked`），清單的 `total` 一定等於那一格的 `n`。

計數方塊由成員身分讀 `repo/stats.list_cube_counts`＋`list_cube_key_counts`，經
`domain/cube.build_cube_rows` 得到每一格；每一格打 `GET /api/v1/papers?scope=tracked&journal=<格>
&year=<格>`，`with_abstract` 加 `has_abstract=true`，`topic`／`method` 加 `topic=`／`method=<key>`。

測試資料涵蓋：沒有摘要（含只有不是目前的摘要）、被排除、快照建立中、`first_snapshot_id` 是空的範圍內
論文、範圍外但全實驗室看得到的、AI 分類被人修正成不同 key（主題一篇、方法一篇）、同一篇多個主題、
有效主題 `unclassified`、有方法與沒有方法、出版年比快照截至年晚一年；兩本期刊、五個年份。被排除、
範圍外、還沒算數的論文也有分類，確定它們不會讓格子和清單其中一邊多算。資料全是測試自己編的。

比對是雙向的（Story 3.13 審查 T1、T2）：計數方塊的每一格要和手算的期望字典一模一樣；反過來，
每個（期刊, 年份）× 種子用到的每個 key 都查一次清單，有篇數的格子也要剛好是那份期望字典（方塊漏算
的格子、清單多出來的格子都會被抓到）。每一次查清單都檢查 `items` 的篇數等於 `total`。
"""

import uuid

import pytest

from radar.domain.contracts import CubeDim, CubeRow
from radar.domain.cube import build_cube_rows
from radar.domain.papers import PaperOrigin
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import MemberViewer
from radar.repo.db import Database
from radar.repo.stats import list_cube_counts, list_cube_key_counts
from tests.database import TestDatabase, migrate
from tests.integration.test_effective_views import fix, insert_classification
from tests.integration.test_paper_list_api import (
    ISR,
    MISQ,
    OUTSIDE,
    Api,
    make_paper,
    make_snapshot,
)
from tests.llm_support import execute, paper_page_setup


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


@pytest.fixture
def api(database: Database) -> Api:
    return Api(database)


def _seed(db: TestDatabase, database: Database) -> None:
    published = make_snapshot(db, 1)  # 截至 2026 年
    building = make_snapshot(db, 2, "building")

    def counted(number: int, issn_l: str, year: int, *, abstract: bool = True) -> uuid.UUID:
        return make_paper(
            database, db, number, snapshot_id=published, issn_l=issn_l, year=year, abstract=abstract
        )

    # misq 2020：兩個主題＋兩個方法；人把 erp 改成 health_it（方法照 AI）；沒有方法；沒有摘要。
    multi = counted(1, MISQ, 2020)
    insert_classification(db, multi, ["e_commerce", "erp"], ["experiment", "survey_sem"])
    corrected = counted(2, MISQ, 2020)
    insert_classification(db, corrected, ["erp"], ["survey_sem"])
    fix(database, corrected, TaxonomyField.TOPIC, ["health_it"])
    no_method = counted(3, MISQ, 2020, abstract=False)
    insert_classification(db, no_method, ["erp"])
    # misq 2021：有效主題 unclassified；只有不是目前的摘要；還沒分類。
    unclassified = counted(4, MISQ, 2021, abstract=False)
    insert_classification(db, unclassified, ["unclassified"], ["design_science"])
    execute(
        db,
        "INSERT INTO paper_abstract (paper_id, source, content, is_current)"
        " VALUES (%s, 'semantic_scholar', 'An older made-up abstract.', false)",
        unclassified,
    )
    counted(5, MISQ, 2021)
    # isr 2019 與出版年比截至年晚一年的 isr 2027。
    isr_2019 = counted(6, ISR, 2019)
    insert_classification(db, isr_2019, ["erp", "health_it"], ["survey_sem"])
    isr_2027 = counted(7, ISR, 2027)
    insert_classification(db, isr_2027, ["health_it"])
    # isr 2020：人把方法 experiment 改成 design_science（主題照 AI）。
    method_fixed = counted(8, ISR, 2020)
    insert_classification(db, method_fixed, ["e_commerce"], ["experiment"])
    fix(database, method_fixed, TaxonomyField.METHOD, ["design_science"])
    # 不算數的：被排除、快照建立中、還沒有快照、範圍外但全實驗室看得到的（都有分類）。
    not_counted = [
        make_paper(
            database, db, 11, snapshot_id=published, year=2020, excluded_reason="editors_comments"
        ),
        make_paper(database, db, 12, snapshot_id=building, year=2020),
        make_paper(database, db, 13, snapshot_id=None, year=2021),
        make_paper(
            database,
            db,
            14,
            snapshot_id=published,
            issn_l=OUTSIDE,
            year=2020,
            origin=PaperOrigin.EXPANSION,
        ),
    ]
    for paper_id in not_counted:
        insert_classification(db, paper_id, ["erp"], ["survey_sem"])


def _cube(database: Database, viewer: MemberViewer) -> tuple[CubeRow, ...]:
    with database.transaction() as connection:
        cells = list_cube_counts(viewer, connection)
        key_cells = list_cube_key_counts(viewer, connection)
    return build_cube_rows(cells, paper_page_setup().journals, key_cells=key_cells)


type Cell = tuple[str, int, CubeDim, str | None]

# 手算的期望：上面種子資料的每一格（只有算數的論文；被修正掉的 AI 值不算）。
EXPECTED: dict[Cell, int] = {
    ("misq", 2020, CubeDim.TOTAL, None): 3,
    ("misq", 2020, CubeDim.WITH_ABSTRACT, None): 2,
    ("misq", 2020, CubeDim.TOPIC, "e_commerce"): 1,
    ("misq", 2020, CubeDim.TOPIC, "erp"): 2,
    ("misq", 2020, CubeDim.TOPIC, "health_it"): 1,
    ("misq", 2020, CubeDim.METHOD, "experiment"): 1,
    ("misq", 2020, CubeDim.METHOD, "survey_sem"): 2,
    ("misq", 2021, CubeDim.TOTAL, None): 2,
    ("misq", 2021, CubeDim.WITH_ABSTRACT, None): 1,
    ("misq", 2021, CubeDim.TOPIC, "unclassified"): 1,
    ("misq", 2021, CubeDim.METHOD, "design_science"): 1,
    ("isr", 2019, CubeDim.TOTAL, None): 1,
    ("isr", 2019, CubeDim.WITH_ABSTRACT, None): 1,
    ("isr", 2019, CubeDim.TOPIC, "erp"): 1,
    ("isr", 2019, CubeDim.TOPIC, "health_it"): 1,
    ("isr", 2019, CubeDim.METHOD, "survey_sem"): 1,
    ("isr", 2020, CubeDim.TOTAL, None): 1,
    ("isr", 2020, CubeDim.WITH_ABSTRACT, None): 1,
    ("isr", 2020, CubeDim.TOPIC, "e_commerce"): 1,
    ("isr", 2020, CubeDim.METHOD, "design_science"): 1,
    ("isr", 2027, CubeDim.TOTAL, None): 1,
    ("isr", 2027, CubeDim.WITH_ABSTRACT, None): 1,
    ("isr", 2027, CubeDim.TOPIC, "health_it"): 1,
}
# 反向查清單用：種子裡出現過的期刊、年份與 key（含只出現在不算數論文、或被修正掉的）。
JOURNALS = ("misq", "isr")
YEARS = (2019, 2020, 2021, 2027)
TOPICS = ("e_commerce", "erp", "health_it", "unclassified")
METHODS = ("design_science", "experiment", "survey_sem")


def _cell_params(cell: Cell) -> list[tuple[str, str]]:
    journal, year, dim, key = cell
    params = [("scope", "tracked"), ("journal", journal), ("year", str(year))]
    match dim:
        case CubeDim.WITH_ABSTRACT:
            params.append(("has_abstract", "true"))
        case CubeDim.TOPIC:
            params.append(("topic", str(key)))
        case CubeDim.METHOD:
            params.append(("method", str(key)))
    return params


def _listed(api: Api, cell: Cell) -> int:
    """用這一格的條件查清單，回 `total`；每格都在一頁之內，`items` 的篇數要等於 `total`。"""
    body = api.ok(_cell_params(cell))
    assert body["next_cursor"] is None, cell
    assert len(body["items"]) == body["total"], cell
    assert len({item["id"] for item in body["items"]}) == body["total"], cell
    return int(body["total"])


def test_every_cube_cell_clicks_back_to_the_same_count(
    db: TestDatabase, database: Database, api: Api
) -> None:
    _seed(db, database)
    rows = _cube(database, MemberViewer(member_id=api.owner_id, is_admin=False))

    # 方塊這一邊：每一格和手算的一模一樣（不多不少）。
    cube = {(row.journal, row.year, row.dim, row.key_a): row.n for row in rows}
    assert len(cube) == len(rows)
    assert cube == EXPECTED

    # 從方塊的每一格點回清單：篇數一樣（items 與 total 都是）。
    mismatched = [
        (*cell, n, listed) for cell, n in cube.items() if (listed := _listed(api, cell)) != n
    ]
    assert mismatched == []

    # 反向：每個（期刊, 年份）× 每個 key 都查一次清單，有篇數的格子剛好是期望字典。
    candidates: list[Cell] = [
        (journal, year, dim, key)
        for journal in JOURNALS
        for year in YEARS
        for dim, keys in (
            (CubeDim.TOTAL, (None,)),
            (CubeDim.WITH_ABSTRACT, (None,)),
            (CubeDim.TOPIC, TOPICS),
            (CubeDim.METHOD, METHODS),
        )
        for key in keys
    ]
    assert set(EXPECTED) <= set(candidates)
    found = {cell: n for cell in candidates if (n := _listed(api, cell)) > 0}
    assert found == EXPECTED
