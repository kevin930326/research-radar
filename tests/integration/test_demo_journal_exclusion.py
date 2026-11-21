"""示範站依期刊 ISSN-L 排除（C3、ADR-0052 更新 2026-11-22）與 reparse 重判跨多頁（G2），對真的
PostgreSQL。

- `upsert_by_external_id` 用正式 `config/` 的設定寫入「CC BY、出版社名稱是清單外的寫法、ID 不在
  清單、期刊是 IEEE TEM（0018-9391）」的論文：`demo_eligible` 是 false、不在 view
  `demo_full_paper`；同樣的論文換成別的期刊是 true、在 view 裡。
- reparse 結束時的重判讀存下的 `journal_issn_l`：存下的期刊對到排除清單、原本 true 的改成 false。
- 重判依論文 id 分頁（`batch_size=2`），跨好幾頁時每一篇該改的都改了、不該改的沒動，兩個數字正確。

測試資料全部自己編（`tests/demo_works.py`），不用真的論文。每個測試一個全新、已遷移的資料庫。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from radar.adapters.fake import FakeObjectStore
from radar.config import load_domain_config
from radar.domain.domain_config import ExclusionRulesConfig
from radar.domain.papers import PaperOrigin
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.policy import PublisherExclusions
from radar.domain.scope import ScopeSettings
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.papers import upsert_by_external_id
from radar.services.reparse import ReparseResult, reparse_papers
from tests.database import APP_ROLE, OWNER_ROLE, ROOT, TestDatabase, migrate
from tests.demo_works import TEST_EXCLUSIONS, demo_work, demo_work_bytes

IEEE_TEM_ISSN_L = "0018-9391"
OTHER_ISSN_L = "1471-7727"  # Information and Organization（正式 journals.yaml 裡的另一本）
# 不在排除清單裡的 IEEE 名稱寫法與一個編的、不在清單裡的出版社 ID。
UNLISTED_IEEE_NAME = "The Institute of Electrical and Electronics Engineers"
UNLISTED_ID = "https://openalex.org/P9000000001"
T1 = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
NO_RULES = ExclusionRulesConfig(rules=())

# 重判用：寫入時沒有期刊清單（舊），重判時多一本 IEEE TEM（新）。出版社那一道兩邊一樣。
OLD_SCOPE = ScopeSettings(
    frozenset({OTHER_ISSN_L, IEEE_TEM_ISSN_L}), demo_publishers=TEST_EXCLUSIONS
)
NEW_SCOPE = ScopeSettings(
    frozenset({OTHER_ISSN_L, IEEE_TEM_ISSN_L}),
    demo_publishers=PublisherExclusions(
        ids=TEST_EXCLUSIONS.ids,
        names=TEST_EXCLUSIONS.names,
        journal_issn_ls=frozenset({IEEE_TEM_ISSN_L}),
    ),
)


@pytest.fixture
def demo_db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(demo_db: TestDatabase) -> Database:
    return Database(demo_db.app_url, connect_timeout=5, pool_size=2)


def _owner(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(OWNER_ROLE) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def _rows(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with db.connect(APP_ROLE) as connection:
        return connection.execute(sql, params).fetchall()


def _write(database: Database, work: dict[str, Any], scope: ScopeSettings) -> uuid.UUID:
    paper = parse_openalex_work(demo_work_bytes(work), fetched_at=T1)
    with database.transaction() as connection:
        return upsert_by_external_id(
            connection, paper, origin=PaperOrigin.TRACKED, excluded_reason=None, scope=scope
        ).paper_id


def _published_snapshot(db: TestDatabase) -> uuid.UUID:
    [(run_id,)] = _owner(db, "INSERT INTO weekly_run (status) VALUES ('published') RETURNING id")
    [(snapshot_id,)] = _owner(
        db,
        "INSERT INTO snapshot (number, weekly_run_id, status, as_of)"
        " VALUES (1, %s, 'published', %s) RETURNING id",
        run_id,
        T1,
    )
    return uuid.UUID(str(snapshot_id))


def _eligible_by_id(db: TestDatabase) -> dict[uuid.UUID, bool]:
    return dict(_rows(db, "SELECT id, demo_eligible FROM paper"))


def _reparse(database: Database, scope: ScopeSettings, batch_size: int = 500) -> ReparseResult:
    return reparse_papers(
        SystemViewer(),
        database,
        FakeObjectStore(),
        scope=scope,
        exclusion_rules=NO_RULES,
        batch_size=batch_size,
    )


def test_upsert_with_the_real_config_excludes_ieee_tem_by_journal(
    demo_db: TestDatabase, database: Database
) -> None:
    scope = load_domain_config({"RADAR_CONFIG_DIR": str(ROOT / "config")}).scope_settings()
    snapshot = _published_snapshot(demo_db)
    tem = _write(
        database,
        demo_work(
            1, issn_l=IEEE_TEM_ISSN_L, publisher_id=UNLISTED_ID, publisher_name=UNLISTED_IEEE_NAME
        ),
        scope,
    )
    # 對照：同樣的出版社欄位、別的期刊。
    other = _write(
        database,
        demo_work(
            2, issn_l=OTHER_ISSN_L, publisher_id=UNLISTED_ID, publisher_name=UNLISTED_IEEE_NAME
        ),
        scope,
    )
    _owner(
        demo_db,
        "UPDATE paper SET first_snapshot_id = %s WHERE id = ANY(%s)",
        snapshot,
        [tem, other],
    )

    rows = _rows(
        demo_db,
        "SELECT id, journal_issn_l, primary_license, publisher_names, in_scope, demo_eligible"
        " FROM paper",
    )
    assert sorted(rows, key=lambda row: row[1]) == [
        (tem, IEEE_TEM_ISSN_L, "cc-by", [UNLISTED_IEEE_NAME], True, False),
        (other, OTHER_ISSN_L, "cc-by", [UNLISTED_IEEE_NAME], True, True),
    ]
    assert [row[0] for row in _rows(demo_db, "SELECT id FROM demo_full_paper")] == [other]


def test_reparse_recheck_turns_a_stored_ieee_tem_paper_false(
    demo_db: TestDatabase, database: Database
) -> None:
    # 沒有原始資料紀錄＝reparse 沒重寫到，只經結束時的重判；判定讀存下的 journal_issn_l。
    tem = _write(database, demo_work(1, issn_l=IEEE_TEM_ISSN_L), OLD_SCOPE)
    other = _write(database, demo_work(2, issn_l=OTHER_ISSN_L), OLD_SCOPE)
    assert _eligible_by_id(demo_db) == {tem: True, other: True}

    result = _reparse(database, NEW_SCOPE)

    assert _eligible_by_id(demo_db) == {tem: False, other: True}
    assert (result.updated, result.demo_rechecked, result.demo_revoked) == (0, 2, 1)


def test_reparse_recheck_across_several_pages(demo_db: TestDatabase, database: Database) -> None:
    # G2：7 篇 true（另 1 篇原本就是 false），batch_size=2 → 重判分 4 頁讀（2、2、2、1）。
    # 依論文 id 排序後第 0、1 篇（整個第一頁）與第 4 篇改成 IEEE TEM，重判要把這 3 篇改成 false。
    eligible = [_write(database, demo_work(n, issn_l=OTHER_ISSN_L), OLD_SCOPE) for n in range(1, 8)]
    already_false = _write(
        database, demo_work(8, issn_l=IEEE_TEM_ISSN_L), ScopeSettings(frozenset({OTHER_ISSN_L}))
    )
    ordered = sorted(eligible)
    revoke = {ordered[0], ordered[1], ordered[4]}
    # 只改存下的期刊（測試資料），demo_eligible 仍是 true：模擬「寫入時期刊不在清單裡」。
    _owner(
        demo_db,
        "UPDATE paper SET journal_issn_l = %s WHERE id = ANY(%s)",
        IEEE_TEM_ISSN_L,
        list(revoke),
    )
    before = _eligible_by_id(demo_db)
    assert before == {**dict.fromkeys(eligible, True), already_false: False}

    result = _reparse(database, NEW_SCOPE, batch_size=2)

    after = _eligible_by_id(demo_db)
    assert after == {
        **{paper_id: paper_id not in revoke for paper_id in eligible},
        already_false: False,
    }
    # 重判了 7 篇（原本 true、這次沒重寫到的全部），改了 3 篇；原本 false 的不算、不動。
    assert (result.updated, result.demo_rechecked, result.demo_revoked) == (0, 7, 3)
