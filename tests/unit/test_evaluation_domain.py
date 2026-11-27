"""搜尋的評估題組的 domain（`domain/evaluation.py`，Story 4.4）：不連資料庫。

- 題目檔當使用者輸入驗：範例檔讀得過；編碼（非 UTF-8 明確失敗、BOM 可以有）、欄位少了／多了／重複、
  `set` 的值、空列跳過並計數、欄位數不對、重複列、DOI 正規化、`similar` 的 `query` 也是 DOI、每題
  正解超過 5 篇、長度上限、控制字元、CSV 格式錯；公式開頭的字原樣當文字存。問題訊息不含格子的內容。
- 匯入計畫：對不到的正解跳過、起點對不到或正解不剩的整題跳過。
- 相似度下限的參數。
- 三組排名從同一份結果拆開、命中名次、MRR、「找不到相關的論文」的題數；三組分開算。
- 存進 `eval_run` 的 JSON 與印出來的報告。

題目、DOI、論文 ID 都是測試自己編的。
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from radar.domain.evaluation import (
    MAX_FILE_BYTES,
    TOP_N,
    EvalFloorResult,
    EvalImportResult,
    EvalLeg,
    EvalQueriesFileError,
    EvalQuerySet,
    EvalQuestion,
    EvalRelevant,
    EvalSetResult,
    EvalSkip,
    EvalSkipReason,
    SearchEvalReport,
    eval_doi_targets,
    eval_results_json,
    first_hit_rank,
    leg_rankings,
    parse_eval_queries_csv,
    parse_min_similarities,
    plan_eval_import,
    question_outcome,
    render_eval_import,
    render_search_eval,
    summarize_leg,
)
from radar.domain.retrieval import RetrievalHit

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "tests" / "fixtures" / "eval" / "search_queries_sample.csv"
HEADER = "set,query_id,query,relevant_doi,note\n"
MAX_LENGTH = 500


def parse(text: str, *, header: str = HEADER) -> tuple[EvalQuestion, ...]:
    return parse_eval_queries_csv(
        (header + text).encode("utf-8"), query_max_length=MAX_LENGTH
    ).questions


def problems(data: str | bytes) -> tuple[str, ...]:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    with pytest.raises(EvalQueriesFileError) as caught:
        parse_eval_queries_csv(raw, query_max_length=MAX_LENGTH)
    return caught.value.problems


# --- 題目檔 -----------------------------------------------------------------------------------


def test_sample_file_is_made_up_and_parses() -> None:
    parsed = parse_eval_queries_csv(SAMPLE.read_bytes(), query_max_length=MAX_LENGTH)

    assert parsed.blank_lines == 1
    assert [(q.query_set, q.query_id, len(q.relevant)) for q in parsed.questions] == [
        (EvalQuerySet.SEARCH, "q01", 2),
        (EvalQuerySet.SEARCH, "q02", 1),
        (EvalQuerySet.SEARCH, "q03", 2),
        (EvalQuerySet.SEARCH, "q04", 1),
        (EvalQuerySet.SIMILAR, "s01", 1),
        (EvalQuerySet.SIMILAR, "s02", 1),
    ]
    # 只用假的 DOI（10.9999/fake.*）。
    every = eval_doi_targets(parsed.questions)
    assert every
    assert all(doi.startswith("10.9999/fake.") for doi in every)
    # 網址形式、大寫的 DOI 正規化成同一種寫法。
    assert [item.doi for item in parsed.questions[0].relevant] == [
        "10.9999/fake.0001",
        "10.9999/fake.0002",
    ]
    assert parsed.questions[0].relevant[0].note == "測試資料：自己編的題目"
    assert parsed.questions[0].relevant[1].note is None
    assert parsed.questions[4].query == "10.9999/fake.0001"


def test_utf8_bom_is_accepted() -> None:
    data = b"\xef\xbb\xbf" + (HEADER + "search,q1,平台,10.9999/fake.0001,\n").encode("utf-8")

    parsed = parse_eval_queries_csv(data, query_max_length=MAX_LENGTH)

    assert parsed.questions[0].query == "平台"


def test_non_utf8_file_fails_clearly() -> None:
    data = (HEADER + "search,q1,平台採用,10.9999/fake.0001,\n").encode("big5")

    found = problems(data)

    assert len(found) == 1
    assert found[0].startswith("題目檔不是 UTF-8")


def test_file_over_the_size_limit_is_refused() -> None:
    assert problems(b"x" * (MAX_FILE_BYTES + 1)) == (f"題目檔超過 {MAX_FILE_BYTES:,} 位元組",)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("set,query_id,query\n", ["第 1 列：少了欄位 relevant_doi"]),
        (
            "set,query_id,query,relevant_doi,score\n",
            ["第 1 列：第 5 欄的欄位名稱不認得"],
        ),
        ("set,query_id,query,relevant_doi,set\n", ["第 1 列：欄位 set 寫了兩次"]),
        ("\n", ["第 1 列要是欄位名稱"]),
    ],
    ids=["missing", "extra", "twice", "empty"],
)
def test_header_problems(header: str, expected: list[str]) -> None:
    found = problems(header + "search,q1,平台,10.9999/fake.0001,\n")

    assert len(found) == len(expected)
    for problem, start in zip(found, expected, strict=True):
        assert problem.startswith(start)


def test_columns_can_be_in_any_order_and_note_is_optional() -> None:
    questions = parse(
        "10.9999/fake.0001,平台,q1,search\n", header="relevant_doi,query,query_id,set\n"
    )

    assert questions == (
        EvalQuestion(
            EvalQuerySet.SEARCH, "q1", "平台", (EvalRelevant("10.9999/fake.0001", None, 2),)
        ),
    )


def test_blank_lines_are_skipped_and_counted() -> None:
    raw = (HEADER + "\n , , , ,\nsearch,q1,平台,10.9999/fake.0001,\n\n").encode("utf-8")

    parsed = parse_eval_queries_csv(raw, query_max_length=MAX_LENGTH)

    assert parsed.blank_lines == 3
    assert len(parsed.questions) == 1


def test_file_with_only_a_header_has_no_questions() -> None:
    assert problems(HEADER + "\n") == ("題目檔沒有任何題目",)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ("search,q1,平台,10.9999/fake.0001\n", "第 2 列：有 4 欄，欄位名稱是 5 欄"),
        ("Search,q1,平台,10.9999/fake.0001,\n", "第 2 列：set 只能是 search 或 similar"),
        ("other,q1,平台,10.9999/fake.0001,\n", "第 2 列：set 只能是 search 或 similar"),
        ("search,,平台,10.9999/fake.0001,\n", "第 2 列：query_id 是空的"),
        ("search," + "q" * 65 + ",平台,10.9999/fake.0001,\n", "第 2 列：query_id 超過 64 個字"),
        ("search,q​1,平台,10.9999/fake.0001,\n", "第 2 列：query_id 有控制字元"),
        ("search,q1,  ,10.9999/fake.0001,\n", "第 2 列：query 不收（query_empty）"),
        (
            "search,q1," + "字" * 501 + ",10.9999/fake.0001,\n",
            "第 2 列：query 不收（query_too_long）",
        ),
        (
            'search,q1,"平\t台",10.9999/fake.0001,\n',
            "第 2 列：query 不收（query_invalid_character）",
        ),
        ("search,q1,平‮台,10.9999/fake.0001,\n", "第 2 列：query 不收（query_invalid_character）"),
        (
            "similar,s1,not a doi,10.9999/fake.0001,\n",
            "第 2 列：similar 的 query 要是起點論文的 DOI",
        ),
        ("search,q1,平台,fake.0001,\n", "第 2 列：relevant_doi 看不懂"),
        ("search,q1,平台,10.9999/fake.0001," + "註" * 501 + "\n", "第 2 列：note 超過 500 個字"),
        ("search,q1,平台,10.9999/fake.0001,備\x07註\n", "第 2 列：note 有控制字元"),
        (
            "similar,s1,10.9999/fake.0001,https://doi.org/10.9999/FAKE.0001,\n",
            "第 2 列：找相似論文的正解不能是起點自己",
        ),
        ('search,q1,"平台,10.9999/fake.0001,\n', "第 2 列：CSV 格式不對"),
    ],
    ids=[
        "too-few-cells",
        "set-case",
        "set-unknown",
        "id-empty",
        "id-too-long",
        "id-zero-width",
        "query-blank",
        "query-too-long",
        "query-tab",
        "query-bidi",
        "similar-query-not-doi",
        "doi-not-doi",
        "note-too-long",
        "note-control",
        "similar-self",
        "broken-quote",
    ],
)
def test_bad_rows_are_refused_without_echoing_the_cell(row: str, expected: str) -> None:
    found = problems(HEADER + row)

    assert any(problem.startswith(expected) for problem in found), found
    # 問題訊息只有列號與原因，不印格子的內容（查詢、備註、DOI）。
    for problem in found:
        assert "平台" not in problem
        assert "fake.0001" not in problem
        assert "註註" not in problem


def test_every_problem_is_listed_with_its_line() -> None:
    found = problems(
        HEADER
        + "search,q1,平台,10.9999/fake.0001,\n"
        + "bad,q2,平台,10.9999/fake.0001,\n"
        + "search,q3,平台,nope,\n"
    )

    assert [problem.split("：")[0] for problem in found] == ["第 3 列", "第 4 列"]


def test_duplicate_rows_after_normalising_are_refused() -> None:
    found = problems(
        HEADER
        + "search,q1,平台,10.9999/fake.0001,\n"
        + "search,q1,平台,https://doi.org/10.9999/FAKE.0001,另一個備註\n"
    )

    assert found == ("第 3 列：和第 2 列重複（同一題、同一篇正解）",)


def test_same_question_must_keep_the_same_query() -> None:
    found = problems(
        HEADER + "search,q1,平台,10.9999/fake.0001,\nsearch,q1,平台採用,10.9999/fake.0002,\n"
    )

    assert found == ("第 3 列：同一題（search q1）的 query 和第 2 列不一樣",)


def test_same_query_id_in_both_sets_are_two_questions() -> None:
    questions = parse(
        "search,x1,平台,10.9999/fake.0001,\nsimilar,x1,10.9999/fake.0002,10.9999/fake.0001,\n"
    )

    assert [q.key for q in questions] == [
        (EvalQuerySet.SEARCH, "x1"),
        (EvalQuerySet.SIMILAR, "x1"),
    ]


def test_one_to_five_relevant_papers_per_question() -> None:
    five = "".join(f"search,q1,平台,10.9999/fake.000{n},\n" for n in range(1, 6))
    assert len(parse(five)[0].relevant) == 5

    six = five + "search,q1,平台,10.9999/fake.0006,\n"
    assert problems(HEADER + six) == ("第 2 列起的那一題（search q1）有 6 篇正解，最多 5 篇",)


def test_question_shape_is_checked() -> None:
    with pytest.raises(ValueError, match="1～5"):
        EvalQuestion(EvalQuerySet.SEARCH, "q1", "平台", ())
    with pytest.raises(ValueError, match="重複"):
        EvalQuestion(
            EvalQuerySet.SEARCH,
            "q1",
            "平台",
            (EvalRelevant("10.9/a", None), EvalRelevant("10.9/a", None)),
        )
    with pytest.raises(ValueError, match="起點自己"):
        EvalQuestion(EvalQuerySet.SIMILAR, "s1", "10.9/a", (EvalRelevant("10.9/a", None),))


def test_formula_like_cells_are_kept_as_plain_text() -> None:
    questions = parse(
        "search,=cmd|' /C calc'!A0,=HYPERLINK(\"x\"),10.9999/fake.0001,@SUM(1+1)\n"
        "search,+q2,-平台,10.9999/fake.0002,\n"
    )

    assert [(q.query_id, q.query, q.relevant[0].note) for q in questions] == [
        ("=cmd|' /C calc'!A0", '=HYPERLINK("x")', "@SUM(1+1)"),
        ("+q2", "-平台", None),
    ]


# --- 匯入計畫 ---------------------------------------------------------------------------------


def test_import_plan_skips_missing_dois_and_empty_questions() -> None:
    questions = parse_eval_queries_csv(SAMPLE.read_bytes(), query_max_length=MAX_LENGTH).questions
    found = frozenset(f"10.9999/fake.000{n}" for n in (1, 2, 3))

    kept, skips = plan_eval_import(questions, found)

    assert [(q.query_set.value, q.query_id, [r.doi for r in q.relevant]) for q in kept] == [
        ("search", "q01", ["10.9999/fake.0001", "10.9999/fake.0002"]),
        ("search", "q02", ["10.9999/fake.0002"]),
        ("search", "q03", ["10.9999/fake.0003"]),
        ("search", "q04", ["10.9999/fake.0001"]),
        ("similar", "s01", ["10.9999/fake.0002"]),
    ]
    assert skips == (
        EvalSkip(
            EvalQuerySet.SEARCH, "q03", "10.9999/fake.0099", EvalSkipReason.RELEVANT_NOT_FOUND, 7
        ),
        EvalSkip(
            EvalQuerySet.SIMILAR, "s02", "10.9999/fake.0098", EvalSkipReason.ANCHOR_NOT_FOUND, 10
        ),
    )


def test_question_whose_relevant_papers_are_all_missing_is_skipped() -> None:
    questions = parse("search,q1,平台,10.9999/fake.0001,\nsearch,q1,平台,10.9999/fake.0002,\n")

    kept, skips = plan_eval_import(questions, frozenset())

    assert kept == ()
    assert [skip.reason for skip in skips] == [
        EvalSkipReason.RELEVANT_NOT_FOUND,
        EvalSkipReason.RELEVANT_NOT_FOUND,
        EvalSkipReason.NO_RELEVANT_LEFT,
    ]


# --- 相似度下限 ---------------------------------------------------------------------------------


def test_min_similarities_are_parsed_deduplicated_and_sorted() -> None:
    assert parse_min_similarities(["0.5", " 0.2", "0.50", "0"]) == (
        Decimal(0),
        Decimal("0.2"),
        Decimal("0.5"),
    )


@pytest.mark.parametrize("value", ["1", "1.0", "-0.1", "abc", "NaN", "Infinity", ""])
def test_bad_min_similarity_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="--min-similarity"):
        parse_min_similarities([value])


def test_too_many_min_similarities_are_refused() -> None:
    with pytest.raises(ValueError, match="最多"):
        parse_min_similarities([f"0.{n}" for n in range(11)])


# --- 三組排名、命中名次、MRR -------------------------------------------------------------------


def ids(count: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=n + 1) for n in range(count)]


def test_leg_rankings_split_one_result_into_three() -> None:
    a, b, c, d = ids(4)
    hits = [
        RetrievalHit(paper_id=a, rank=1, vector_rank=2, text_rank=1),
        RetrievalHit(paper_id=b, rank=2, vector_rank=1, text_rank=None),
        RetrievalHit(paper_id=c, rank=3, vector_rank=None, text_rank=2),
        RetrievalHit(paper_id=d, rank=4, vector_rank=3, text_rank=None),
    ]

    rankings = leg_rankings(list(reversed(hits)))

    assert rankings == {
        EvalLeg.FUSED: (a, b, c, d),
        EvalLeg.VECTOR: (b, a, d),
        EvalLeg.TEXT: (a, c),
    }


def test_rankings_and_hits_only_look_at_the_top_10() -> None:
    papers = ids(12)
    hits = [
        RetrievalHit(paper_id=paper, rank=n + 1, vector_rank=n + 1, text_rank=None)
        for n, paper in enumerate(papers)
    ]

    rankings = leg_rankings(hits)

    assert rankings[EvalLeg.FUSED] == tuple(papers[:TOP_N])
    assert rankings[EvalLeg.TEXT] == ()
    assert first_hit_rank(rankings[EvalLeg.FUSED], [papers[9]]) == 10
    assert first_hit_rank(rankings[EvalLeg.FUSED], [papers[10]]) is None
    assert first_hit_rank(rankings[EvalLeg.FUSED], [papers[10], papers[3]]) == 4


def test_three_groups_are_summarised_separately() -> None:
    a, b, c, d = ids(4)
    one = question_outcome(
        "q1",
        [
            RetrievalHit(paper_id=a, rank=1, vector_rank=1, text_rank=None),
            RetrievalHit(paper_id=b, rank=2, vector_rank=2, text_rank=None),
        ],
        [b],
        (EvalLeg.TEXT, EvalLeg.VECTOR, EvalLeg.FUSED),
    )
    two = question_outcome(
        "q2",
        [
            RetrievalHit(paper_id=c, rank=1, vector_rank=None, text_rank=1),
            RetrievalHit(paper_id=d, rank=2, vector_rank=1, text_rank=None),
        ],
        [c],
        (EvalLeg.TEXT, EvalLeg.VECTOR, EvalLeg.FUSED),
    )

    assert [(leg, o.rank, o.has_results) for leg, o in one.legs.items()] == [
        (EvalLeg.TEXT, None, False),
        (EvalLeg.VECTOR, 2, True),
        (EvalLeg.FUSED, 2, True),
    ]
    text = summarize_leg([one, two], EvalLeg.TEXT)
    vector = summarize_leg([one, two], EvalLeg.VECTOR)
    fused = summarize_leg([one, two], EvalLeg.FUSED)
    assert (text.hits, text.hit_rate, text.mrr, text.no_results) == (
        1,
        Fraction(1, 2),
        Fraction(1, 2),
        1,
    )
    assert (vector.hits, vector.mrr, vector.no_results) == (1, Fraction(1, 4), 0)
    assert (fused.hits, fused.hit_rate, fused.mrr, fused.no_results) == (
        2,
        Fraction(1),
        Fraction(3, 4),
        0,
    )


def test_summary_of_no_questions_is_zero() -> None:
    summary = summarize_leg([], EvalLeg.FUSED)

    assert (summary.questions, summary.hits) == (0, 0)
    assert summary.hit_rate == Fraction(0)
    assert summary.mrr == Fraction(0)


# --- 存進 eval_run 的 JSON 與報告 ---------------------------------------------------------------


def _report() -> SearchEvalReport:
    a, b = ids(2)
    hits = [RetrievalHit(paper_id=a, rank=1, vector_rank=None, text_rank=1)]
    legs = (EvalLeg.TEXT, EvalLeg.VECTOR, EvalLeg.FUSED)
    search = EvalSetResult(
        EvalQuerySet.SEARCH,
        (question_outcome("q1", hits, [a], legs), question_outcome("q2", [], [b], legs)),
    )
    similar = EvalSetResult(
        EvalQuerySet.SIMILAR,
        (
            question_outcome(
                "s1",
                [RetrievalHit(paper_id=a, rank=1, vector_rank=1, text_rank=None)],
                [b],
                (EvalLeg.VECTOR,),
            ),
        ),
    )
    return SearchEvalReport(
        eval_run_id=uuid.UUID(int=7),
        model_id="fake-embedding",
        snapshot_id=uuid.UUID(int=9),
        snapshot_number=3,
        snapshot_changed=False,
        query_count=3,
        floors=(EvalFloorResult(Decimal("0.2"), (search, similar)),),
        skips=(
            EvalSkip(
                EvalQuerySet.SEARCH, "q9", "10.9999/fake.0099", EvalSkipReason.RELEVANT_NOT_FOUND
            ),
        ),
        started_at=datetime(2026, 11, 28, 1, 0, tzinfo=UTC),
        finished_at=datetime(2026, 11, 28, 1, 1, tzinfo=UTC),
    )


def test_results_json_keeps_every_group_and_question_without_query_text() -> None:
    results = eval_results_json(_report())

    floor = results["floors"][0]  # type: ignore[index]
    assert floor["min_similarity"] == "0.2"
    assert floor["sets"]["search"]["summary"] == {
        "text": {"questions": 2, "hits": 1, "hit_rate": "0.5000", "mrr": "0.5000", "no_results": 1},
        "vector": {
            "questions": 2,
            "hits": 0,
            "hit_rate": "0.0000",
            "mrr": "0.0000",
            "no_results": 2,
        },
        "fused": {
            "questions": 2,
            "hits": 1,
            "hit_rate": "0.5000",
            "mrr": "0.5000",
            "no_results": 1,
        },
    }
    assert floor["sets"]["search"]["questions"][0] == {
        "query_id": "q1",
        "ranks": {"text": 1, "vector": None, "fused": 1},
        "has_results": {"text": True, "vector": False, "fused": True},
    }
    assert list(floor["sets"]["similar"]["summary"]) == ["vector"]
    assert results["skips"] == [
        {
            "set": "search",
            "query_id": "q9",
            "doi": "10.9999/fake.0099",
            "reason": "relevant_not_found",
        }
    ]


def test_report_prints_settings_three_groups_and_each_question() -> None:
    lines = render_search_eval(_report())

    assert lines[:5] == [
        f"搜尋評估（eval_run {uuid.UUID(int=7)}）",
        "向量模型：fake-embedding",
        "快照：第 3 號",
        "相似度下限：0.2",
        "題數：3（範圍 tracked；看前 10 名）",
    ]
    assert "  只用全文檢索腿：命中 1/2（50.0%），MRR 0.500，找不到相關的論文 1 題" in lines
    assert "  只用向量腿：命中 0/2（0.0%），MRR 0.000，找不到相關的論文 2 題" in lines
    assert "  融合：命中 1/2（50.0%），MRR 0.500，找不到相關的論文 1 題" in lines
    assert "    q1：只用全文檢索腿 第 1 名；只用向量腿 沒有結果；融合 第 1 名" in lines
    assert "    s1：只用向量腿 沒命中" in lines
    assert "  search q9 10.9999/fake.0099：正解的 DOI 對不到論文，這一篇跳過" in lines


def test_report_marks_a_changed_snapshot_and_escapes_data_text() -> None:
    from dataclasses import replace

    report = replace(_report(), snapshot_changed=True, snapshot_number=None)

    lines = render_search_eval(report, label=lambda value: f"<{value}>")

    assert "快照：還沒有已發佈的快照" in lines
    assert "注意：跑到一半換了快照，這次的數字混了兩個快照，請重跑" in lines
    assert any(line.startswith("    <q1>：") for line in lines)


def test_import_result_lines() -> None:
    result = EvalImportResult(
        imported={EvalQuerySet.SEARCH: 4, EvalQuerySet.SIMILAR: 1},
        relevant=6,
        replaced=2,
        skips=(
            EvalSkip(
                EvalQuerySet.SIMILAR,
                "s02",
                "10.9999/fake.0098",
                EvalSkipReason.ANCHOR_NOT_FOUND,
                10,
            ),
        ),
        blank_lines=1,
    )

    assert render_eval_import(result) == [
        "已匯入評估題目：search 4 題、similar 1 題，正解共 6 篇（整份取代原本的 2 題）",
        "跳過空列 1 列",
        "跳過的（1 筆）：",
        "  第 10 列 similar s02 10.9999/fake.0098：起點論文的 DOI 對不到論文，整題跳過",
    ]
