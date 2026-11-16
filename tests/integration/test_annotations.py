"""`services/annotations.prepare_annotation_sample` 對真的 PostgreSQL（Story 2.13、ADR-0045）。

候選只有算數論文（範圍內、沒被排除、在已發佈的快照）而且有 DOI；摘要層級只從有目前摘要的抽；
各層名額依比例；興趣主題優先；人工修正優先於 AI 主題；同 seed 同結果；補抽排除既有試算表的論文。

論文經正式的 `upsert_by_external_id` 寫入；快照與 `first_snapshot_id` 用擁有者角色直接寫，AI 分類
直接用 SQL 造，人工修正經 `create_judgment_event`（都只在測試裡）。論文、DOI（`10.5555/` 測試前綴）
都是自己編的。
"""

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import Connection

import radar.services.annotations as annotation_service
from radar.domain.annotations import (
    AnnotationPaperText,
    AnnotationRow,
    AnnotationSampleResult,
    SampledWorkbook,
    SampleSet,
    build_annotator_a_workbook,
)
from radar.domain.eval_split import EvalSplit, split_for_doi
from radar.domain.papers import PaperOrigin, ParsedPaper
from radar.domain.scope import ScopeSettings
from radar.domain.taxonomy import TaxonomyField
from radar.domain.viewer import MemberViewer, SystemViewer
from radar.repo.db import Database
from radar.repo.papers import find_annotation_paper_texts, upsert_by_external_id
from radar.services.annotations import prepare_annotation_sample
from tests.annotation_support import TEST_CONTEXT, TEST_TAXONOMY
from tests.database import TestDatabase, migrate
from tests.integration.test_effective_views import fix, insert_classification
from tests.llm_support import execute
from tests.trial_support import doi_in

FETCHED_AT = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
AS_OF = datetime(2026, 11, 16, 2, 30, tzinfo=UTC)
JOURNAL_A = "0276-7783"
JOURNAL_B = "1047-7047"
OTHER_ISSN = "0960-085X"  # 追蹤範圍外
SCOPE = ScopeSettings(frozenset({JOURNAL_A, JOURNAL_B}), start_year=2016)


@pytest.fixture
def db(fresh_database: TestDatabase) -> TestDatabase:
    migrate(fresh_database)
    return fresh_database


@pytest.fixture
def database(db: TestDatabase) -> Database:
    return Database(db.app_url, connect_timeout=5, pool_size=2)


def snapshot(db: TestDatabase, number: int = 1, status: str = "published") -> uuid.UUID:
    [(run_id,)] = execute(db, "INSERT INTO weekly_run (status) VALUES ('failed') RETURNING id")
    [(snapshot_id,)] = execute(
        db,
        "INSERT INTO snapshot (number, weekly_run_id, status, as_of) VALUES (%s, %s, %s, %s)"
        " RETURNING id",
        number,
        run_id,
        status,
        AS_OF if status == "published" else None,
    )
    return uuid.UUID(str(snapshot_id))


def paper(
    database: Database,
    db: TestDatabase,
    number: int,
    *,
    snapshot_id: uuid.UUID | None,
    issn: str = JOURNAL_A,
    year: int = 2020,
    abstract: bool = True,
    doi: bool = True,
    excluded_reason: str | None = None,
) -> uuid.UUID:
    """一篇假論文（`10.5555/rr.<number>`，標題 `Annotation study <number>`）。"""
    fields: dict[str, object] = {
        "source": "openalex",
        "fetched_at": FETCHED_AT,
        "openalex_id": f"W9{number:08d}",
        "title": f"Annotation study {number}",
        "journal_issn_l": issn,
        "publication_year": year,
    }
    fields["doi"] = f"10.5555/rr.{number:04d}" if doi else None
    if abstract:
        fields |= {"abstract": f"Made-up abstract number {number}.", "abstract_source": "openalex"}
    with database.transaction() as connection:
        result = upsert_by_external_id(
            connection,
            ParsedPaper.model_validate(fields),
            origin=PaperOrigin.TRACKED if issn in SCOPE.journals else PaperOrigin.IMPORT,
            excluded_reason=excluded_reason,
            scope=SCOPE,
        )
    if snapshot_id is not None:
        execute(
            db,
            "UPDATE paper SET first_snapshot_id = %s WHERE id = %s",
            snapshot_id,
            result.paper_id,
        )
    return result.paper_id


@dataclass(frozen=True)
class Library:
    abstract_2020: list[uuid.UUID]  # 期刊 A、2020、有摘要（6 篇）
    abstract_2021: list[uuid.UUID]  # 期刊 A、2021、有摘要（3 篇）
    no_abstract: list[uuid.UUID]  # 期刊 B、2020、沒有摘要（4 篇）
    excluded: uuid.UUID
    out_of_scope: uuid.UUID
    unassigned: uuid.UUID
    on_building: uuid.UUID
    no_doi: uuid.UUID

    @property
    def candidates(self) -> set[uuid.UUID]:
        return {*self.abstract_2020, *self.abstract_2021, *self.no_abstract}


@pytest.fixture
def library(db: TestDatabase, database: Database) -> Library:
    published = snapshot(db, 1)
    building = snapshot(db, 2, "building")
    return Library(
        abstract_2020=[paper(database, db, n, snapshot_id=published) for n in range(1, 7)],
        abstract_2021=[
            paper(database, db, n, snapshot_id=published, year=2021) for n in range(7, 10)
        ],
        no_abstract=[
            paper(database, db, n, snapshot_id=published, issn=JOURNAL_B, abstract=False)
            for n in range(10, 14)
        ],
        excluded=paper(database, db, 20, snapshot_id=published, excluded_reason="editors_comments"),
        out_of_scope=paper(database, db, 21, snapshot_id=published, issn=OTHER_ISSN),
        unassigned=paper(database, db, 22, snapshot_id=None),
        on_building=paper(database, db, 23, snapshot_id=building),
        no_doi=paper(database, db, 24, snapshot_id=published, doi=False),
    )


def _prepare(
    database: Database,
    *,
    seed: int = 7,
    interest: tuple[str, ...] = (),
    abstract: int = 50,
    classification: int = 100,
    existing: SampledWorkbook | None = None,
) -> AnnotationSampleResult:
    return prepare_annotation_sample(
        SystemViewer(),
        database,
        TEST_TAXONOMY,
        seed=seed,
        interest=interest,
        abstract_count=abstract,
        classification_count=classification,
        existing=existing,
    )


def _ids(result: AnnotationSampleResult, sample_set: SampleSet | None = None) -> list[uuid.UUID]:
    return [
        row.paper_id for row in result.rows if sample_set is None or row.sample_set is sample_set
    ]


def test_only_counted_papers_with_doi_are_drawn(database: Database, library: Library) -> None:
    result = _prepare(database)

    assert set(_ids(result)) == library.candidates
    assert result.skipped_without_doi == 1
    assert result.pool == {SampleSet.ABSTRACT: 9, SampleSet.CLASSIFICATION_ONLY: 4}
    assert result.drawn == {SampleSet.ABSTRACT: 9, SampleSet.CLASSIFICATION_ONLY: 4}
    abstract_rows = [row for row in result.rows if row.sample_set is SampleSet.ABSTRACT]
    assert {row.paper_id for row in abstract_rows} == {
        *library.abstract_2020,
        *library.abstract_2021,
    }
    assert all(row.abstract is not None for row in abstract_rows)
    # 摘要層級在前、分類專用在後
    assert [row.sample_set for row in result.rows] == [SampleSet.ABSTRACT] * 9 + [
        SampleSet.CLASSIFICATION_ONLY
    ] * 4
    first = next(row for row in result.rows if row.paper_id == library.abstract_2020[0])
    assert first.dois == ("10.5555/rr.0001",)
    assert first.title == "Annotation study 1"


def test_classification_set_includes_papers_with_abstract_left_over(
    database: Database, library: Library
) -> None:
    result = _prepare(database, abstract=3, classification=100)

    assert len(_ids(result, SampleSet.ABSTRACT)) == 3
    leftover = set(_ids(result, SampleSet.CLASSIFICATION_ONLY))
    assert leftover == library.candidates - set(_ids(result, SampleSet.ABSTRACT))
    assert result.pool[SampleSet.CLASSIFICATION_ONLY] == 10


def test_strata_get_proportional_quotas(database: Database, library: Library) -> None:
    # 有摘要的候選：2020 有 6 篇、2021 有 3 篇 → 抽 3 篇是 2:1。
    result = _prepare(database, abstract=3, classification=0)
    picked = set(_ids(result, SampleSet.ABSTRACT))
    assert len(picked & set(library.abstract_2020)) == 2
    assert len(picked & set(library.abstract_2021)) == 1


def test_same_seed_gives_same_rows_and_bytes(database: Database, library: Library) -> None:
    first = _prepare(database, abstract=4, classification=3)
    second = _prepare(database, abstract=4, classification=3)

    assert first.rows == second.rows
    assert build_annotator_a_workbook(first.rows, TEST_TAXONOMY, TEST_CONTEXT) == (
        build_annotator_a_workbook(second.rows, TEST_TAXONOMY, TEST_CONTEXT)
    )
    other = [_prepare(database, seed=seed, abstract=4, classification=3).rows for seed in range(8)]
    assert len({tuple(row.paper_id for row in rows) for rows in other}) > 1


def test_existing_rows_are_kept_and_excluded(database: Database, library: Library) -> None:
    first = _prepare(database, abstract=4, classification=2)
    existing = SampledWorkbook(first.rows, annotated=False)

    extended = _prepare(database, abstract=2, classification=1, existing=existing)

    old_ids = set(_ids(first))
    new_rows = [row for row in extended.rows if row.paper_id not in old_ids]
    assert len(new_rows) == 3
    old_dois = {doi for row in first.rows for doi in row.dois}
    assert not old_dois & {doi for row in new_rows for doi in row.dois}
    assert extended.existing_count == 6
    # 既有的摘要層級列照原順序在前，新的摘要層級接在後面，再來是分類專用。
    abstract_ids = _ids(extended, SampleSet.ABSTRACT)
    assert abstract_ids[:4] == _ids(first, SampleSet.ABSTRACT)
    assert len(abstract_ids) == 6
    classification_ids = _ids(extended, SampleSet.CLASSIFICATION_ONLY)
    assert classification_ids[:2] == _ids(first, SampleSet.CLASSIFICATION_ONLY)
    assert extended.drawn == {SampleSet.ABSTRACT: 2, SampleSet.CLASSIFICATION_ONLY: 1}


@pytest.fixture
def three_papers(db: TestDatabase, database: Database) -> list[uuid.UUID]:
    """同一層 3 篇有摘要的論文：第 1 篇 AI 主題 erp、人工修正成 supply_chain；第 2 篇 AI 主題 erp；
    第 3 篇還沒有分類結果。"""
    published = snapshot(db, 1)
    papers = [paper(database, db, n, snapshot_id=published) for n in (101, 102, 103)]
    insert_classification(db, papers[0], ["erp"], ["survey_sem"])
    fix(database, papers[0], TaxonomyField.TOPIC, ["supply_chain"])
    insert_classification(db, papers[1], ["erp"])
    return papers


def test_interest_topic_is_preferred_and_human_fix_wins(
    database: Database, three_papers: list[uuid.UUID]
) -> None:
    for seed in range(5):
        erp = _prepare(database, seed=seed, interest=("erp",), abstract=1, classification=0)
        assert _ids(erp) == [three_papers[1]]
        chain = _prepare(
            database, seed=seed, interest=("supply_chain",), abstract=1, classification=0
        )
        assert _ids(chain) == [three_papers[0]]


def test_topic_counts_use_effective_topics(
    database: Database, three_papers: list[uuid.UUID]
) -> None:
    result = _prepare(database, abstract=3, classification=0)

    counts = result.topic_counts
    assert counts.by_set[SampleSet.ABSTRACT] == {"erp": 1, "supply_chain": 1}
    assert counts.pending[SampleSet.ABSTRACT] == 1


def test_unclassified_topic_is_counted(db: TestDatabase, database: Database) -> None:
    published = snapshot(db, 1)
    only = paper(database, db, 201, snapshot_id=published)
    insert_classification(db, only, ["unclassified"], ["experiment"])

    result = _prepare(database)

    assert result.topic_counts.by_set[SampleSet.ABSTRACT] == {"unclassified": 1}
    assert Counter(row.sample_set for row in result.rows) == {SampleSet.ABSTRACT: 1}


def test_bad_arguments_and_viewer(database: Database) -> None:
    with pytest.raises(ValueError, match="主題 key"):
        _prepare(database, interest=("blockchain",))
    with pytest.raises(ValueError, match="abstract_count"):
        _prepare(database, abstract=-1)
    member = MemberViewer(member_id=uuid.UUID(int=5), is_admin=True)
    with pytest.raises(TypeError):
        prepare_annotation_sample(
            member,  # type: ignore[arg-type]
            database,
            TEST_TAXONOMY,
            seed=1,
            interest=(),
            abstract_count=1,
            classification_count=0,
            existing=None,
        )


def test_multi_doi_paper_keeps_all_dois_and_test_split(
    db: TestDatabase, database: Database
) -> None:
    published = snapshot(db, 1)
    multi = paper(database, db, 301, snapshot_id=published)
    own = "10.5555/rr.0301"
    other = doi_in(
        EvalSplit.TUNING if split_for_doi(own) is EvalSplit.TEST else EvalSplit.TEST, "5555"
    )
    execute(
        db,
        "INSERT INTO external_id (paper_id, scheme, value_norm) VALUES (%s, 'doi', %s)",
        multi,
        other,
    )

    (row,) = _prepare(database).rows

    assert row.paper_id == multi
    assert row.dois == tuple(sorted((own, other)))
    assert row.split is EvalSplit.TEST


def test_existing_dois_are_excluded_even_with_unknown_paper_id(
    database: Database, library: Library
) -> None:
    taken = library.abstract_2020[0]
    dois = ("10.5555/rr.0001",)  # 第 1 篇的 DOI，但 paper_id 不在資料庫
    existing = SampledWorkbook(
        (
            AnnotationRow(
                paper_id=uuid.UUID(int=999),
                dois=dois,
                title="Gone",
                abstract=None,
                split=split_for_doi(dois[0]),
                sample_set=SampleSet.ABSTRACT,
            ),
        ),
        annotated=False,
    )

    result = _prepare(database, abstract=50, classification=100, existing=existing)

    assert taken not in _ids(result)
    assert set(_ids(result)) == (library.candidates - {taken}) | {uuid.UUID(int=999)}


def test_paper_missing_when_reading_texts_is_dropped(
    database: Database, library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = library.abstract_2020[0]

    def without_one(
        viewer: SystemViewer, connection: Connection, paper_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, AnnotationPaperText]:
        found = find_annotation_paper_texts(viewer, connection, paper_ids)
        found.pop(gone, None)
        return found

    monkeypatch.setattr(annotation_service, "find_annotation_paper_texts", without_one)

    result = _prepare(database)

    assert gone not in _ids(result)
    assert len(result.rows) == len(library.candidates) - 1
    assert result.drawn[SampleSet.ABSTRACT] == 8
