"""搜尋的評估（Story 4.4、FR-26、FR-27、FR-41、ADR-0005）：匯入題目、跑題、存 `eval_run`。規則與
算法在 `domain/evaluation.py`，SQL 在 `repo/evaluation.py`，指令是 `radar eval`。

- `import_eval_queries`：驗好的題目（`domain/evaluation.parse_eval_queries_csv`）→ 同一個交易裡
  查 DOI 對得到哪些論文（系統的 viewer，看得到全部）→ 對不到的正解跳過、起點對不到或正解一篇不剩的
  題目整題跳過（`plan_eval_import`）→ **整份取代**資料庫裡原本的全部題目（刪掉舊的、寫新的）。題目檔
  是唯一的來源：同一個 `query_id` 重匯就是換成這次的寫法與正解，題目檔裡拿掉的題目資料庫也不再有。
  跳過的列印出來，不中斷。
- `run_search_eval`：讀全部題目 → DOI 換成論文 ID（這時才對不到的照樣跳過並印出）→ 每題跑
  `services/retrieval.retrieve`（唯一入口；`tracked` 範圍、`actor`（系統的 viewer）的可見範圍；`k`
  給到 `max_results`，拿到融合後的全部結果）→ 每個相似度下限、每組排名算命中名次 → 存一筆
  `eval_run`。
  - `search` 題：每題的查詢向量只算一次（`embed_retrieval_query`，記帳、額度和搜尋一樣），多個下限
    都用同一個向量重跑 `retrieve`（`query_vector`），所以 Workers AI 的用量是「題數」次，和下限的
    個數無關。
  - `similar` 題：起點 DOI 換成論文 ID 後用 `retrieve(paper_id=…)`（只有向量腿、用存好的向量、不呼叫
    Workers AI、不用額度）。起點被排除或看不到：整題跳過。
  - 額度用完或連不上：丟出原本的錯（不降級、不存半套的 `eval_run`）。
- 日誌只記題數與下限，不記查詢內容。
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

import structlog

from radar.domain.evaluation import (
    SET_LEGS,
    EvalFloorResult,
    EvalImportResult,
    EvalQuerySet,
    EvalQuestion,
    EvalQuestionOutcome,
    EvalRunKind,
    EvalSetResult,
    EvalSkip,
    EvalSkipReason,
    ParsedEvalQueries,
    SearchEvalReport,
    eval_doi_targets,
    eval_results_json,
    plan_eval_import,
    question_outcome,
)
from radar.domain.paper_list import PaperListFilters
from radar.domain.retrieval import RetrievalAnchorNotFoundError, RetrievalConfig, RetrievalScope
from radar.domain.snapshot import Snapshot
from radar.domain.viewer import SystemViewer
from radar.repo.db import Database
from radar.repo.evaluation import (
    create_eval_queries,
    create_eval_run,
    delete_eval_queries,
    list_eval_queries,
)
from radar.repo.papers import find_paper_ids_by_dois
from radar.services.retrieval import RetrievalSetup, embed_retrieval_query, retrieve

log = structlog.get_logger(__name__)


class EvalQueriesMissingError(LookupError):
    """資料庫裡還沒有任何評估題目（先 `radar eval import-queries`）。"""

    def __init__(self) -> None:
        super().__init__("還沒有評估題目")


def import_eval_queries(
    actor: SystemViewer, database: Database, parsed: ParsedEvalQueries
) -> EvalImportResult:
    """把驗好的題目整份取代資料庫裡的題目（見模組說明），回匯入的結果（含跳過的）。"""
    with database.transaction() as connection:
        found = find_paper_ids_by_dois(actor, connection, eval_doi_targets(parsed.questions))
        kept, skips = plan_eval_import(parsed.questions, frozenset(found))
        replaced = delete_eval_queries(connection)
        create_eval_queries(connection, kept)
    imported = {
        query_set: sum(1 for question in kept if question.query_set is query_set)
        for query_set in EvalQuerySet
    }
    log.info(
        "評估題目已匯入",
        search=imported[EvalQuerySet.SEARCH],
        similar=imported[EvalQuerySet.SIMILAR],
        skipped=len(skips),
        replaced=replaced,
    )
    return EvalImportResult(
        imported=imported,
        relevant=sum(len(question.relevant) for question in kept),
        replaced=replaced,
        skips=skips,
        blank_lines=parsed.blank_lines,
    )


@dataclass(frozen=True, slots=True)
class _Prepared:
    question: EvalQuestion
    relevant_ids: frozenset[uuid.UUID]
    anchor_id: uuid.UUID | None


def _prepare(actor: SystemViewer, database: Database) -> tuple[list[_Prepared], list[EvalSkip]]:
    with database.read_only_snapshot_transaction() as connection:
        questions = list_eval_queries(actor, connection)
        if not questions:
            raise EvalQueriesMissingError()
        found = find_paper_ids_by_dois(actor, connection, eval_doi_targets(questions))
    prepared: list[_Prepared] = []
    skips: list[EvalSkip] = []
    for question in questions:
        anchor_id = None
        if question.query_set is EvalQuerySet.SIMILAR:
            anchor_id = found.get(question.query)
            if anchor_id is None:
                skips.append(
                    EvalSkip(
                        question.query_set,
                        question.query_id,
                        question.query,
                        EvalSkipReason.ANCHOR_NOT_FOUND,
                    )
                )
                continue
        relevant_ids = set()
        for item in question.relevant:
            paper_id = found.get(item.doi)
            if paper_id is None:
                skips.append(
                    EvalSkip(
                        question.query_set,
                        question.query_id,
                        item.doi,
                        EvalSkipReason.RELEVANT_NOT_FOUND,
                    )
                )
            else:
                relevant_ids.add(paper_id)
        if not relevant_ids:
            skips.append(
                EvalSkip(
                    question.query_set, question.query_id, None, EvalSkipReason.NO_RELEVANT_LEFT
                )
            )
            continue
        prepared.append(_Prepared(question, frozenset(relevant_ids), anchor_id))
    return prepared, skips


def _setup_for(setup: RetrievalSetup, min_similarity: Decimal) -> RetrievalSetup:
    """同一份設定、換一個相似度下限（照 `RetrievalConfig` 的規則重新驗）。"""
    values = setup.retrieval.model_dump()
    values["vector_min_similarity"] = min_similarity
    return replace(setup, retrieval=RetrievalConfig.model_validate(values))


def run_search_eval(
    actor: SystemViewer,
    database: Database,
    setup: RetrievalSetup,
    *,
    min_similarities: Sequence[Decimal],
) -> SearchEvalReport:
    """跑全部的評估題目、存一筆 `eval_run`，回這次的結果（見模組說明）。`min_similarities` 是要比的
    相似度下限（至少一個；由小到大、不重複，`domain/evaluation.parse_min_similarities` 產生）。

    Raises:
        TypeError: `actor` 不是 `SystemViewer`。
        ValueError: `min_similarities` 是空的、有重複，或有不合法的值。
        EvalQueriesMissingError: 還沒有任何評估題目。
        SearchQuotaExhaustedError: Workers AI 今天的額度用完了（沒有存 `eval_run`）。
        SearchEmbeddingUnavailableError: 查詢沒能轉成向量（沒有存 `eval_run`）。
    """
    if not isinstance(actor, SystemViewer):
        raise TypeError("評估只用系統的 viewer")
    floors = tuple(min_similarities)
    if not floors or len(set(floors)) != len(floors):
        raise ValueError("min_similarities 要至少一個、不重複")
    setups = {floor: _setup_for(setup, floor) for floor in floors}
    started_at = setup.clock()
    prepared, skips = _prepare(actor, database)
    log.info("評估：開始", questions=len(prepared), floors=[str(floor) for floor in floors])

    outcomes: dict[Decimal, dict[EvalQuerySet, list[EvalQuestionOutcome]]] = {
        floor: {query_set: [] for query_set in EvalQuerySet} for floor in floors
    }
    snapshots: list[Snapshot | None] = []
    filters = PaperListFilters()  # 預設的範圍就是 tracked，其他條件不限
    ran = 0
    for item in prepared:
        question = item.question
        legs = SET_LEGS[question.query_set]
        if question.query_set is EvalQuerySet.SEARCH:
            # 查詢向量只算一次，每個下限共用（不重複用 Workers AI 的額度）。
            vector = embed_retrieval_query(actor, database, setup, question.query)
            pages = [
                retrieve(
                    actor,
                    database,
                    setups[floor],
                    query=question.query,
                    query_vector=vector,
                    scope=RetrievalScope.TRACKED,
                    filters=filters,
                    k=setups[floor].retrieval.max_results,
                )
                for floor in floors
            ]
        else:
            assert item.anchor_id is not None
            try:
                pages = [
                    retrieve(
                        actor,
                        database,
                        setups[floor],
                        paper_id=item.anchor_id,
                        scope=RetrievalScope.TRACKED,
                        filters=filters,
                        k=setups[floor].retrieval.max_results,
                    )
                    for floor in floors
                ]
            except RetrievalAnchorNotFoundError:
                skips.append(
                    EvalSkip(
                        question.query_set,
                        question.query_id,
                        question.query,
                        EvalSkipReason.ANCHOR_UNAVAILABLE,
                    )
                )
                continue
        ran += 1
        for floor, page in zip(floors, pages, strict=True):
            snapshots.append(page.snapshot)
            outcomes[floor][question.query_set].append(
                question_outcome(question.query_id, page.hits, item.relevant_ids, legs)
            )

    first = snapshots[0] if snapshots else None
    snapshot_changed = len({None if s is None else s.id for s in snapshots}) > 1
    floor_results = tuple(
        EvalFloorResult(
            min_similarity=floor,
            sets=tuple(
                EvalSetResult(query_set, tuple(outcomes[floor][query_set]))
                for query_set in EvalQuerySet
                if outcomes[floor][query_set]
            ),
        )
        for floor in floors
    )
    # 跑了哪幾組題目：資料庫裡有題目的組（整題跳過的也算，紀錄看得出那一組這次沒有可跑的題）。
    present = {item.question.query_set for item in prepared} | {s.query_set for s in skips}
    query_sets = [query_set for query_set in EvalQuerySet if query_set in present]
    finished_at = setup.clock()
    draft = SearchEvalReport(
        eval_run_id=uuid.UUID(int=0),
        model_id=setup.embedding.model_id,
        snapshot_id=None if first is None else first.id,
        snapshot_number=None if first is None else first.number,
        snapshot_changed=snapshot_changed,
        query_count=ran,
        floors=floor_results,
        skips=tuple(skips),
        started_at=started_at,
        finished_at=finished_at,
    )
    with database.transaction() as connection:
        run_id = create_eval_run(
            connection,
            kind=EvalRunKind.SEARCH,
            query_sets=query_sets,
            min_similarities=floors,
            embedding_model_id=setup.embedding.model_id,
            snapshot_id=draft.snapshot_id,
            query_count=ran,
            results=eval_results_json(draft),
            started_at=started_at,
            finished_at=finished_at,
        )
    log.info("評估：結束", eval_run_id=str(run_id), questions=ran, skipped=len(skips))
    return replace(draft, eval_run_id=run_id)
