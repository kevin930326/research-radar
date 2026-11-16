"""標註試算表的抽樣（`radar annotations template`，Story 2.13、PRD FR-41、ADR-0045）。

`prepare_annotation_sample`：

1. 逐頁讀算數論文（`repo/papers.list_annotation_candidates`，依 id、每頁一個短的只讀交易），同一個
   交易裡讀這一頁的有效主題（`repo/ai_blocks.list_effective_topic_keys`，一次最多 `PAGE_SIZE` 個
   ID）；沒有 DOI 的不抽、只計數。每頁讀完只留抽樣要的事實，不留內容。
2. `domain/annotations.draw_annotation_sample` 抽兩組（補抽時排除既有試算表的論文 ID 與 DOI）。
3. 讀新論文的標題與摘要，組成列：既有列照原順序在前，新列依雜湊排序接在同一組後面，摘要層級在前。
4. 再讀全部列的有效主題算主題計數（人工修正優先）。

info 日誌只記 seed、興趣主題與各數量，不記標題、摘要與論文清單。這裡不寫任何資料。
"""

import uuid
from collections.abc import Iterable, Iterator, Sequence
from typing import Final

import structlog

from radar.domain.annotations import (
    AnnotationCandidate,
    AnnotationPaperText,
    AnnotationRow,
    AnnotationSampleResult,
    SampledWorkbook,
    SampleSet,
    annotation_row,
    check_interest,
    count_topics,
    draw_annotation_sample,
    merge_annotation_rows,
)
from radar.domain.taxonomy import Taxonomy
from radar.domain.viewer import SystemViewer
from radar.repo.ai_blocks import list_effective_topic_keys
from radar.repo.db import Database
from radar.repo.papers import find_annotation_paper_texts, list_annotation_candidates

log = structlog.get_logger(__name__)

# 依論文 id 分頁，一頁讀幾篇；有效主題與論文文字也一次最多讀這麼多個 ID（≤ 1,000）。
PAGE_SIZE: Final = 500


def _batches(ids: Sequence[uuid.UUID]) -> Iterator[Sequence[uuid.UUID]]:
    for start in range(0, len(ids), PAGE_SIZE):
        yield ids[start : start + PAGE_SIZE]


class _Candidates:
    """逐頁讀候選；`skipped_without_doi` 在讀完之後才是總數。"""

    def __init__(self, viewer: SystemViewer, database: Database) -> None:
        self._viewer = viewer
        self._database = database
        self.skipped_without_doi = 0

    def __iter__(self) -> Iterator[AnnotationCandidate]:
        after: uuid.UUID | None = None
        while True:
            with self._database.transaction() as connection:
                page = list_annotation_candidates(
                    self._viewer, connection, after_id=after, limit=PAGE_SIZE
                )
                topics = list_effective_topic_keys(
                    self._viewer, connection, [facts.paper_id for facts in page]
                )
            if not page:
                return
            after = page[-1].paper_id
            for facts in page:
                candidate = AnnotationCandidate.from_facts(facts, topics.get(facts.paper_id, ()))
                if candidate is None:
                    self.skipped_without_doi += 1
                else:
                    yield candidate


def _read_topics(
    viewer: SystemViewer, database: Database, paper_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, ...]]:
    found: dict[uuid.UUID, tuple[str, ...]] = {}
    for batch in _batches(paper_ids):
        with database.transaction() as connection:
            found |= list_effective_topic_keys(viewer, connection, batch)
    return found


def _new_rows(
    viewer: SystemViewer,
    database: Database,
    picked: Iterable[tuple[AnnotationCandidate, SampleSet]],
) -> list[AnnotationRow]:
    ordered = list(picked)
    texts: dict[uuid.UUID, AnnotationPaperText] = {}
    for batch in _batches([candidate.paper_id for candidate, _ in ordered]):
        with database.transaction() as connection:
            texts |= find_annotation_paper_texts(viewer, connection, batch)
    rows = [
        annotation_row(candidate, texts[candidate.paper_id], sample_set)
        for candidate, sample_set in ordered
        if candidate.paper_id in texts
    ]
    if len(rows) < len(ordered):
        # 抽完到讀文字之間論文被刪：不放進試算表，只記篇數。
        log.warning("標註抽樣：有論文在讀文字前不見了", missing=len(ordered) - len(rows))
    return rows


def prepare_annotation_sample(
    actor: SystemViewer,
    database: Database,
    taxonomy: Taxonomy,
    *,
    seed: int,
    interest: Iterable[str],
    abstract_count: int,
    classification_count: int,
    existing: SampledWorkbook | None,
) -> AnnotationSampleResult:
    """抽樣並組好 A 試算表的列（見模組說明）。`existing` 是補抽時既有試算表讀出的列。

    Raises:
        TypeError: `actor` 不是 `SystemViewer`（repo 擋下）。
        ValueError: `interest` 有不是分類表主題 `key` 的值、`seed` 不是整數，或數量不是 0 以上的
            整數（不碰資料庫）。
    """
    wanted = check_interest(interest, taxonomy)
    if type(seed) is not int:
        raise ValueError("seed 要是整數")
    for name, value in (
        ("abstract_count", abstract_count),
        ("classification_count", classification_count),
    ):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} 要是 0 以上的整數")
    existing_rows = existing.rows if existing is not None else ()
    candidates = _Candidates(actor, database)
    draw = draw_annotation_sample(
        candidates,
        seed=seed,
        interest=wanted,
        abstract_count=abstract_count,
        classification_count=classification_count,
        exclude_ids=[row.paper_id for row in existing_rows],
        exclude_dois=[doi for row in existing_rows for doi in row.dois],
    )
    new = _new_rows(
        actor,
        database,
        [
            *((candidate, SampleSet.ABSTRACT) for candidate in draw.abstract),
            *((candidate, SampleSet.CLASSIFICATION_ONLY) for candidate in draw.classification),
        ],
    )
    rows = merge_annotation_rows(existing_rows, new)
    topics = _read_topics(actor, database, [row.paper_id for row in rows])
    drawn = {
        sample_set: sum(row.sample_set is sample_set for row in new) for sample_set in SampleSet
    }
    result = AnnotationSampleResult(
        seed=seed,
        interest=wanted,
        rows=rows,
        existing_count=len(existing_rows),
        requested={
            SampleSet.ABSTRACT: abstract_count,
            SampleSet.CLASSIFICATION_ONLY: classification_count,
        },
        drawn=drawn,
        pool={
            SampleSet.ABSTRACT: draw.abstract_pool,
            SampleSet.CLASSIFICATION_ONLY: draw.classification_pool,
        },
        skipped_without_doi=candidates.skipped_without_doi,
        topic_counts=count_topics(rows, topics),
    )
    log.info(
        "標註抽樣",
        seed=seed,
        interest=list(wanted),
        existing=len(existing_rows),
        abstract_requested=abstract_count,
        abstract_drawn=drawn[SampleSet.ABSTRACT],
        abstract_pool=draw.abstract_pool,
        classification_requested=classification_count,
        classification_drawn=drawn[SampleSet.CLASSIFICATION_ONLY],
        classification_pool=draw.classification_pool,
        skipped_without_doi=candidates.skipped_without_doi,
    )
    return result
