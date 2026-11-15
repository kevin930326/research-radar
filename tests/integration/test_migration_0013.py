"""遷移 0013（研究模型的內容）對真的 PostgreSQL：降版、升版、CHECK、唯一約束與外鍵。

- 降到 0012 後 `construct`、`construct_mention`、`hypothesis` 與表頭的內容欄位都不在（0012 已有的
  研究模型表頭照樣在），再升回來都在、舊列的內容與計數是 NULL。
- 表頭：情境 `key` 的寫法、樣本數 ≥ 1、文字不是空白、計數不是負數、`rejected` 沒有內容。
- `construct`：`normalized_name` 唯一、不是空白；`canonical_id` 不可為空、要指到存在的構面。
- `construct_mention`：同一篇同一個構面只有一列；文字、題數、α／CR／AVE 的範圍；外鍵。
- `hypothesis`：`ordinal`、原句雜湊的格式、證據類型與支持與否的值與對應、因 ≠ 果、β 有限、p 在
  0～1、N ≥ 1；同一篇同一個 `ordinal`、同一句同一條關係都只有一列；外鍵。
- 應用程式角色可以新增、讀取這三張表（0001 的預設權限）。
"""

import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from radar.repo.db import Database
from tests.database import APP_ROLE, ROOT, TestDatabase, migrate
from tests.llm_support import create_paper, execute

BIN = Path(sys.executable).parent
SHA = "a" * 64
CONTENT_COLUMNS = {
    "is_review_or_meta_analysis",
    "country",
    "industry",
    "research_design",
    "unit_of_analysis",
    "sample_size",
    "limitations",
    "future_research",
    "dropped_context_value_count",
    "unsupported_context_value_count",
    "corrected_support_count",
    "cleared_value_count",
}
NEW_TABLES = {"construct", "construct_mention", "hypothesis"}


def _alembic(database: TestDatabase, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BIN / "alembic"), *args],
        env={"PYTHONUNBUFFERED": "1", "RADAR_MIGRATION_DATABASE_URL": database.owner_url},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


def _tables(db: TestDatabase) -> set[str]:
    rows = execute(
        db, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    return {row[0] for row in rows}


def _research_model_columns(db: TestDatabase) -> set[str]:
    rows = execute(
        db,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = 'research_model'",
    )
    return {row[0] for row in rows}


def _app(db: TestDatabase, sql: str, *params: object) -> list[tuple[Any, ...]]:
    """用應用程式角色執行一句 SQL，回所有列。"""
    with db.connect(APP_ROLE) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description is not None else []


def _seed_run(db: TestDatabase) -> uuid.UUID:
    """用應用程式角色新增一篇論文（每次都是新的一篇）與一筆做完的 extract_model run，回 run id。"""
    suffix = uuid.uuid4().hex[:8]
    paper_id = create_paper(
        Database(db.app_url, connect_timeout=5, pool_size=1),
        doi=f"10.1/{suffix}",
        openalex_id=f"W{int(suffix, 16)}",
    )
    (row,) = _app(
        db,
        "INSERT INTO derivation_run (paper_id, task, prompt_version, prompt_sha256, model,"
        " input_sha256, input_kind, params, code_version, call_mode, context_values_version,"
        " status, finished_at)"
        " VALUES (%s, 'extract_model', 1, %s, 'claude-haiku-5-5', %s, 'abstract', '{}',"
        " 'test', 'interactive', '1', 'succeeded', now()) RETURNING id",
        paper_id,
        SHA,
        SHA,
    )
    return uuid.UUID(str(row[0]))


def _header(
    db: TestDatabase,
    status: str = "active",
    reject_reason: str | None = None,
    **content: object,
) -> uuid.UUID:
    """用應用程式角色新增一列研究模型表頭，回它的 id。沒給的內容欄位：計數 0、其餘 NULL；
    `rejected` 時全部 NULL。"""
    values: dict[str, object] = (
        {}
        if status == "rejected"
        else {
            "dropped_context_value_count": 0,
            "unsupported_context_value_count": 0,
            "corrected_support_count": 0,
            "cleared_value_count": 0,
        }
    )
    values.update(content)
    run_id = _seed_run(db)
    columns = "".join(f", {name}" for name in values)
    placeholders = "".join(", %s" for _ in values)
    (row,) = _app(
        db,
        f"INSERT INTO research_model (paper_id, run_id, input_kind, input_sha256, lang, status,"
        f" reject_reason{columns})"
        f" SELECT paper_id, id, 'abstract', input_sha256, 'en', %s, %s{placeholders}"
        " FROM derivation_run WHERE id = %s RETURNING id",
        status,
        reject_reason,
        *values.values(),
        run_id,
    )
    return uuid.UUID(str(row[0]))


def _construct(db: TestDatabase, name: str, canonical_id: uuid.UUID | None = None) -> uuid.UUID:
    """新增一列構面；沒給 `canonical_id` 就指向自己。"""
    new_id = uuid.uuid4()
    _app(
        db,
        "INSERT INTO construct (id, normalized_name, canonical_id) VALUES (%s, %s, %s)",
        new_id,
        name,
        canonical_id or new_id,
    )
    return new_id


def _mention(
    db: TestDatabase, header_id: uuid.UUID, construct_id: uuid.UUID, **changes: object
) -> None:
    values: dict[str, object] = {"name_en": "Trust", "label_zh": "信任"} | changes
    columns = ", ".join(values)
    placeholders = ", ".join("%s" for _ in values)
    _app(
        db,
        f"INSERT INTO construct_mention (research_model_id, construct_id, {columns})"
        f" VALUES (%s, %s, {placeholders})",
        header_id,
        construct_id,
        *values.values(),
    )


def _hypothesis(
    db: TestDatabase,
    header_id: uuid.UUID,
    cause_id: uuid.UUID,
    effect_id: uuid.UUID,
    **changes: object,
) -> None:
    values: dict[str, object] = {
        "ordinal": 1,
        "sentence": "Trust increases loyalty.",
        "sentence_sha256": SHA,
        "evidence_type": "empirical_test",
        "support": "supported",
    } | changes
    # 判定了支持與否就要有結果原文（沒特別給時照支持與否補上）。
    if "result_quote" not in values:
        decided = values["support"] in ("supported", "not_supported", "partially_supported")
        values["result_quote"] = "Trust increases loyalty (p = .01)." if decided else None
    columns = ", ".join(values)
    placeholders = ", ".join("%s" for _ in values)
    _app(
        db,
        "INSERT INTO hypothesis (research_model_id, cause_construct_id, effect_construct_id,"
        f" {columns}) VALUES (%s, %s, %s, {placeholders})",
        header_id,
        cause_id,
        effect_id,
        *values.values(),
    )


@pytest.fixture
def pair(db: TestDatabase) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """（表頭 id, 構面 trust 的 id, 構面 loyalty 的 id）。"""
    return _header(db), _construct(db, "trust"), _construct(db, "loyalty")


# --- 降版、升版 ---------------------------------------------------------------------------------


def test_downgrade_to_0012_drops_content_and_upgrade_restores_it(
    db: TestDatabase, pair: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    header_id, trust, loyalty = pair
    assert _tables(db) >= NEW_TABLES
    assert _research_model_columns(db) >= CONTENT_COLUMNS
    _mention(db, header_id, trust)
    _mention(db, header_id, loyalty, name_en="Loyalty", label_zh="忠誠度")
    _hypothesis(db, header_id, trust, loyalty)

    down = _alembic(db, "downgrade", "0012")

    assert down.returncode == 0, down.stdout + down.stderr
    assert db.revisions() == {"0012"}
    assert not NEW_TABLES & _tables(db)
    assert not CONTENT_COLUMNS & _research_model_columns(db)
    assert execute(db, "SELECT status FROM research_model") == [("active",)]

    migrate(db)

    assert db.revisions() == {"0015"}
    assert _tables(db) >= NEW_TABLES
    # 降版丟掉的內容不會回來；舊列的內容與計數是 NULL，CHECK 都成立。
    assert execute(
        db, "SELECT country, sample_size, corrected_support_count FROM research_model"
    ) == [(None, None, None)]
    for table in NEW_TABLES:
        assert execute(db, f"SELECT count(*) FROM {table}") == [(0,)]


def test_app_role_can_insert_and_read_every_new_table(
    db: TestDatabase, pair: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    header_id, trust, loyalty = pair
    _mention(
        db,
        header_id,
        trust,
        definition="the belief that the vendor is honest",
        scale_source="Gefen (2003)",
        item_location="Appendix A",
        item_count=4,
        cronbach_alpha="0.91",
        composite_reliability="0.93",
        ave="0.78",
    )
    _hypothesis(db, header_id, trust, loyalty, beta="-0.42", p_value="0.01", sample_size=200)

    assert _app(db, "SELECT normalized_name FROM construct ORDER BY normalized_name") == [
        ("loyalty",),
        ("trust",),
    ]
    assert _app(db, "SELECT item_count, cronbach_alpha::text FROM construct_mention") == [
        (4, "0.91")
    ]
    assert _app(db, "SELECT beta::text, p_value::text, sample_size FROM hypothesis") == [
        ("-0.42", "0.01", 200)
    ]


# --- research_model 表頭 ------------------------------------------------------------------------


def test_header_accepts_full_content(db: TestDatabase) -> None:
    _header(
        db,
        is_review_or_meta_analysis=False,
        country="tw",
        industry="financial_services",
        research_design="survey",
        unit_of_analysis="individual",
        sample_size=200,
        limitations="One limitation is the sample.",
        future_research="Future research should test other countries.",
        dropped_context_value_count=1,
        unsupported_context_value_count=2,
        corrected_support_count=3,
        cleared_value_count=4,
    )
    # 2.2 留下的表頭沒有內容：全部 NULL。
    _header(
        db,
        dropped_context_value_count=None,
        unsupported_context_value_count=None,
        corrected_support_count=None,
        cleared_value_count=None,
    )


@pytest.mark.parametrize(
    "content",
    [
        {"country": "TW"},
        {"industry": "financial-services"},
        {"research_design": ""},
        {"unit_of_analysis": "1st"},
        {"sample_size": 0},
        {"limitations": "  "},
        {"future_research": ""},
        {"dropped_context_value_count": -1},
        {"unsupported_context_value_count": -1},
        {"corrected_support_count": -1},
        {"cleared_value_count": -1},
    ],
)
def test_header_checks(db: TestDatabase, content: dict[str, object]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _header(db, "active", None, **content)


@pytest.mark.parametrize(
    "content",
    [
        {"is_review_or_meta_analysis": False},
        {"country": "tw"},
        {"sample_size": 10},
        {"limitations": "x"},
        {"dropped_context_value_count": 0},
        {"corrected_support_count": 0},
        {"cleared_value_count": 0},
    ],
)
def test_rejected_header_has_no_content(db: TestDatabase, content: dict[str, object]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _header(db, status="rejected", reject_reason="llm_output_invalid", **content)


def test_rejected_header_without_content_is_accepted(db: TestDatabase) -> None:
    _header(db, status="rejected", reject_reason="research_model_quote_not_in_input")


# --- construct ----------------------------------------------------------------------------------


def test_construct_name_is_unique(db: TestDatabase) -> None:
    _construct(db, "trust")

    with pytest.raises(psycopg.errors.UniqueViolation):
        _construct(db, "trust")


@pytest.mark.parametrize("name", ["", "   "])
def test_construct_name_cannot_be_blank(db: TestDatabase, name: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _construct(db, name)


def test_construct_canonical_must_exist_and_cannot_be_null(db: TestDatabase) -> None:
    trust = _construct(db, "trust")
    # 指到別的構面可以（合併之後的樣子）。
    _construct(db, "trust in vendor", canonical_id=trust)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _construct(db, "loyalty", canonical_id=uuid.uuid4())
    with pytest.raises(psycopg.errors.NotNullViolation):
        _app(
            db,
            "INSERT INTO construct (normalized_name, canonical_id) VALUES ('loyalty', NULL)",
        )


def test_construct_id_defaults_to_uuidv7(db: TestDatabase) -> None:
    (row,) = execute(
        db,
        "WITH new AS (SELECT uuidv7() AS id)"
        " INSERT INTO construct (id, normalized_name, canonical_id)"
        " SELECT id, 'trust', id FROM new RETURNING id, canonical_id, created_at",
    )

    assert row[0] == row[1]
    assert uuid.UUID(str(row[0])).version == 7
    assert row[2] is not None


# --- construct_mention --------------------------------------------------------------------------


def test_same_construct_twice_in_one_model_is_refused(
    db: TestDatabase, pair: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    header_id, trust, _ = pair
    _mention(db, header_id, trust)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _mention(db, header_id, trust)

    # 另一篇可以提到同一個構面。
    _mention(db, _header(db), trust)


@pytest.mark.parametrize(
    "changes",
    [
        {"name_en": " "},
        {"label_zh": ""},
        {"definition": "  "},
        {"scale_source": ""},
        {"item_location": " "},
        {"item_count": 0},
        {"cronbach_alpha": "1.01"},
        {"composite_reliability": "-0.1"},
        {"ave": "2"},
    ],
)
def test_construct_mention_checks(
    db: TestDatabase, pair: tuple[uuid.UUID, uuid.UUID, uuid.UUID], changes: dict[str, object]
) -> None:
    header_id, trust, _ = pair

    with pytest.raises(psycopg.errors.CheckViolation):
        _mention(db, header_id, trust, **changes)


def test_construct_mention_foreign_keys(
    db: TestDatabase, pair: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    header_id, trust, _ = pair

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _mention(db, uuid.uuid4(), trust)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _mention(db, header_id, uuid.uuid4())


# --- hypothesis ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"ordinal": 0},
        {"sentence": "  "},
        {"effect_type": ""},
        {"sentence_sha256": "A" * 64},
        {"sentence_sha256": "a" * 63},
        {"evidence_type": "survey"},
        {"support": "proven"},
        # 只有實證檢定判定支持與否。
        {"evidence_type": "empirical_test", "support": "not_determined"},
        {"evidence_type": "qualitative", "support": "supported"},
        {"evidence_type": "conceptual", "support": "not_stated"},
        {"beta": "Infinity"},
        {"beta": "-Infinity"},
        {"beta": "NaN"},
        {"p_value": "1.5"},
        {"p_value": "-0.01"},
        {"sample_size": 0},
        # 判定了支持與否就一定有結果原文，沒判定就沒有（FR-3 擷取規則 1）。
        {"result_quote": None},
        {"support": "not_stated", "result_quote": "Trust increases loyalty."},
        {"evidence_type": "qualitative", "support": "not_determined", "result_quote": "x"},
        {"result_quote": "  "},
    ],
)
def test_hypothesis_checks(
    db: TestDatabase, pair: tuple[uuid.UUID, uuid.UUID, uuid.UUID], changes: dict[str, object]
) -> None:
    header_id, trust, loyalty = pair

    with pytest.raises(psycopg.errors.CheckViolation):
        _hypothesis(db, header_id, trust, loyalty, **changes)


def test_hypothesis_accepts_each_consistent_support(
    db: TestDatabase, pair: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    header_id, trust, loyalty = pair
    cases = [
        ("empirical_test", "supported"),
        ("empirical_test", "not_supported"),
        ("empirical_test", "partially_supported"),
        ("empirical_test", "not_stated"),
        ("analytical", "not_determined"),
        ("simulation", "not_determined"),
        ("qualitative", "not_determined"),
        ("conceptual", "not_determined"),
    ]
    for ordinal, (evidence_type, support) in enumerate(cases, start=1):
        _hypothesis(
            db,
            header_id,
            trust,
            loyalty,
            ordinal=ordinal,
            sentence_sha256=f"{ordinal:064x}",
            evidence_type=evidence_type,
            support=support,
        )

    assert execute(db, "SELECT count(*) FROM hypothesis") == [(len(cases),)]


def test_hypothesis_cause_must_differ_from_effect(
    db: TestDatabase, pair: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    header_id, trust, _ = pair

    with pytest.raises(psycopg.errors.CheckViolation):
        _hypothesis(db, header_id, trust, trust)


def test_hypothesis_unique_ordinal_and_relation(
    db: TestDatabase, pair: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    header_id, trust, loyalty = pair
    _hypothesis(db, header_id, trust, loyalty)

    # 同一篇同一個 ordinal。
    with pytest.raises(psycopg.errors.UniqueViolation):
        _hypothesis(db, header_id, loyalty, trust, sentence_sha256="b" * 64)
    # 同一句同一條關係。
    with pytest.raises(psycopg.errors.UniqueViolation):
        _hypothesis(db, header_id, trust, loyalty, ordinal=2)

    # 同一句、反方向的關係可以；另一篇同一句同一條關係也可以。
    _hypothesis(db, header_id, loyalty, trust, ordinal=2)
    _hypothesis(db, _header(db), trust, loyalty)


def test_hypothesis_foreign_keys(
    db: TestDatabase, pair: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    header_id, trust, loyalty = pair

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _hypothesis(db, uuid.uuid4(), trust, loyalty)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _hypothesis(db, header_id, uuid.uuid4(), loyalty)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _hypothesis(db, header_id, trust, uuid.uuid4())
