"""標註試算表的抽樣與要印的文字（Story 2.13、ADR-0045）：`annotation_sample_sha256`、最大餘數法、
興趣主題優先、排除、不足、兩組不重疊、標註者 B 的抽樣、補抽後的列順序、主題計數。"""

import uuid
from collections import Counter

import pytest
from hypothesis import given
from hypothesis import strategies as st

from radar.domain.annotations import (
    AnnotationCandidate,
    AnnotationPaperFacts,
    AnnotationProblem,
    AnnotationRow,
    AnnotationSampleResult,
    SampleSet,
    allocate_strata,
    check_interest,
    count_topics,
    draw_annotation_sample,
    merge_annotation_rows,
    render_annotation_problems,
    render_annotation_sample,
    render_second_annotator,
    render_topic_counts,
    select_second_annotator,
)
from radar.domain.eval_split import EvalSplit, split_for_dois
from radar.domain.fingerprints import annotation_sample_sha256
from radar.domain.identifiers import InvalidIdentifierError
from tests.annotation_support import TEST_TAXONOMY

# --- annotation_sample_sha256 ---------------------------------------------------------------


def test_sha256_is_stable_and_depends_on_every_part() -> None:
    base = annotation_sample_sha256(7, "rank", "10.5555/rr.0001")
    assert base == annotation_sample_sha256(7, "rank", "10.5555/rr.0001")
    assert len(base) == 64
    assert base == base.lower()
    others = {
        annotation_sample_sha256(8, "rank", "10.5555/rr.0001"),
        annotation_sample_sha256(7, "second_annotator", "10.5555/rr.0001"),
        annotation_sample_sha256(7, "rank", "10.5555/rr.0002"),
        annotation_sample_sha256(7, "stratum", "0276-7783:2020"),
    }
    assert base not in others
    assert len(others) == 4


@pytest.mark.parametrize("seed", [True, 1.0, "7"])
def test_sha256_rejects_non_int_seed(seed: object) -> None:
    with pytest.raises(ValueError, match="seed"):
        annotation_sample_sha256(seed, "rank", "10.5555/rr.0001")  # type: ignore[arg-type]


def test_sha256_rejects_unknown_purpose_and_unnormalized_doi() -> None:
    with pytest.raises(ValueError, match="purpose"):
        annotation_sample_sha256(1, "trial_sample", "10.5555/rr.0001")
    with pytest.raises(ValueError, match="正規化"):
        annotation_sample_sha256(1, "rank", "10.5555/RR.0001")
    with pytest.raises(InvalidIdentifierError):
        annotation_sample_sha256(1, "second_annotator", "not a doi")


# --- allocate_strata ------------------------------------------------------------------------


def test_allocate_proportional_with_largest_remainder() -> None:
    # 60:30:10，要 7 篇 → 4.2、2.1、0.7 → 先給 4、2、0，剩 1 給餘數最大的 c。
    assert allocate_strata({"a": 60, "b": 30, "c": 10}, 7, seed=1) == {"a": 4, "b": 2, "c": 1}


def test_allocate_takes_everything_when_total_is_large() -> None:
    assert allocate_strata({"a": 3, "b": 1}, 10, seed=1) == {"a": 3, "b": 1}
    assert allocate_strata({}, 5, seed=1) == {}


def test_allocate_ties_are_decided_by_seed_hash() -> None:
    sizes = {f"s{index}": 1 for index in range(6)}
    picks = {
        seed: frozenset(key for key, quota in allocate_strata(sizes, 2, seed).items() if quota)
        for seed in range(20)
    }
    assert all(len(chosen) == 2 for chosen in picks.values())
    assert len(set(picks.values())) > 1  # 不同 seed 會選到不同的層
    assert allocate_strata(sizes, 2, 3) == allocate_strata(dict(reversed(sizes.items())), 2, 3)


def test_allocate_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="total"):
        allocate_strata({"a": 1}, -1, seed=1)
    with pytest.raises(ValueError, match="每層"):
        allocate_strata({"a": -1}, 1, seed=1)
    with pytest.raises(ValueError, match="seed"):
        allocate_strata({"a": 1}, 1, seed=True)


@given(
    sizes=st.dictionaries(st.text(min_size=1, max_size=8), st.integers(0, 60), max_size=12),
    total=st.integers(0, 400),
    seed=st.integers(-1000, 1000),
)
def test_allocate_properties(sizes: dict[str, int], total: int, seed: int) -> None:
    quotas = allocate_strata(sizes, total, seed)
    assert set(quotas) == set(sizes)
    assert sum(quotas.values()) == min(total, sum(sizes.values()))
    assert all(0 <= quotas[key] <= sizes[key] for key in sizes)
    assert quotas == allocate_strata(sizes, total, seed)


# --- draw_annotation_sample -----------------------------------------------------------------


def _candidate(
    number: int,
    *,
    issn: str = "0276-7783",
    year: int = 2020,
    abstract: bool = True,
    topics: tuple[str, ...] = (),
) -> AnnotationCandidate:
    candidate = AnnotationCandidate.from_facts(
        AnnotationPaperFacts(
            paper_id=uuid.UUID(int=number),
            journal_issn_l=issn,
            publication_year=year,
            has_abstract=abstract,
            dois=(f"10.5555/RR.{number:04d}",),
        ),
        topics,
    )
    assert candidate is not None
    return candidate


def test_candidate_without_doi_is_none_and_dois_are_normalized() -> None:
    facts = AnnotationPaperFacts(uuid.UUID(int=1), "0276-7783", 2020, True, ())
    assert AnnotationCandidate.from_facts(facts) is None
    candidate = _candidate(1, topics=("erp", "erp", "e_commerce"))
    assert candidate.doi == "10.5555/rr.0001"
    assert candidate.topics == ("e_commerce", "erp")
    assert candidate.stratum == "0276-7783:2020"


def _ids(candidates: tuple[AnnotationCandidate, ...]) -> list[int]:
    return [candidate.paper_id.int for candidate in candidates]


def test_interest_topic_is_preferred_within_a_stratum() -> None:
    # 一層 3 篇、名額 1：有興趣主題的那篇一定被抽到。
    pool = [_candidate(1), _candidate(2, topics=("erp",)), _candidate(3)]
    for seed in range(10):
        draw = draw_annotation_sample(
            pool, seed=seed, interest={"erp"}, abstract_count=1, classification_count=0
        )
        assert _ids(draw.abstract) == [2]


def test_without_interest_match_order_follows_hash() -> None:
    pool = [_candidate(1), _candidate(2), _candidate(3)]
    expected = min(pool, key=lambda c: annotation_sample_sha256(5, "rank", c.doi))
    draw = draw_annotation_sample(
        pool, seed=5, interest={"supply_chain"}, abstract_count=1, classification_count=0
    )
    assert draw.abstract == (expected,)


def test_strata_get_proportional_quotas() -> None:
    pool = [_candidate(n, year=2020) for n in range(1, 31)]
    pool += [_candidate(n, year=2021) for n in range(31, 41)]
    draw = draw_annotation_sample(
        pool, seed=1, interest=(), abstract_count=8, classification_count=0
    )
    years = Counter(candidate.year for candidate in draw.abstract)
    assert years == {2020: 6, 2021: 2}


def test_abstract_only_from_papers_with_abstract_and_sets_do_not_overlap() -> None:
    pool = [_candidate(n, abstract=n % 2 == 0) for n in range(1, 21)]
    draw = draw_annotation_sample(
        pool, seed=3, interest=(), abstract_count=5, classification_count=8
    )
    assert all(candidate.has_abstract for candidate in draw.abstract)
    assert len(draw.abstract) == 5
    assert len(draw.classification) == 8
    assert not set(_ids(draw.abstract)) & set(_ids(draw.classification))
    assert draw.abstract_pool == 10
    assert draw.classification_pool == 15
    # 分類專用含沒有摘要的
    assert any(not candidate.has_abstract for candidate in draw.classification)


def test_shortage_takes_everything() -> None:
    pool = [_candidate(n, abstract=n <= 3) for n in range(1, 6)]
    draw = draw_annotation_sample(
        pool, seed=1, interest=(), abstract_count=50, classification_count=100
    )
    assert len(draw.abstract) == 3
    assert len(draw.classification) == 2


def test_excluded_ids_and_dois_are_never_drawn() -> None:
    pool = [_candidate(n) for n in range(1, 11)]
    draw = draw_annotation_sample(
        pool,
        seed=1,
        interest=(),
        abstract_count=10,
        classification_count=10,
        exclude_ids=[uuid.UUID(int=1)],
        exclude_dois=["10.5555/rr.0002"],
    )
    drawn = set(_ids(draw.abstract)) | set(_ids(draw.classification))
    assert drawn == set(range(3, 11))


def test_draw_is_independent_of_input_order_and_sorted_by_hash() -> None:
    pool = [
        _candidate(n, year=2019 + n % 3, topics=("erp",) if n % 4 == 0 else ())
        for n in range(1, 40)
    ]
    first = draw_annotation_sample(
        pool, seed=9, interest={"erp"}, abstract_count=10, classification_count=10
    )
    second = draw_annotation_sample(
        list(reversed(pool)), seed=9, interest={"erp"}, abstract_count=10, classification_count=10
    )
    assert first == second
    hashes = [annotation_sample_sha256(9, "rank", candidate.doi) for candidate in first.abstract]
    assert hashes == sorted(hashes)  # 列的順序不依「是否興趣主題」


def test_draw_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="abstract_count"):
        draw_annotation_sample([], seed=1, interest=(), abstract_count=-1, classification_count=0)


def test_check_interest() -> None:
    assert check_interest(["erp", "e_commerce", "erp"], TEST_TAXONOMY) == ("e_commerce", "erp")
    with pytest.raises(ValueError, match="主題 key"):
        check_interest(["survey_sem"], TEST_TAXONOMY)  # 方法不是主題


# --- 列、標註者 B、主題計數 ------------------------------------------------------------------


def _row(number: int, sample_set: SampleSet = SampleSet.ABSTRACT) -> AnnotationRow:
    dois = (f"10.5555/rr.{number:04d}",)
    return AnnotationRow(
        uuid.UUID(int=number), dois, f"T{number}", None, split_for_dois(dois), sample_set
    )


def test_select_second_annotator_only_from_abstract_set() -> None:
    rows = [_row(n) for n in range(1, 21)] + [
        _row(n, SampleSet.CLASSIFICATION_ONLY) for n in range(21, 40)
    ]
    selected = select_second_annotator(rows, 15, seed=3)
    assert len(selected) == 15
    assert all(row.sample_set is SampleSet.ABSTRACT for row in selected)
    hashes = [annotation_sample_sha256(3, "second_annotator", row.doi) for row in selected]
    assert hashes == sorted(hashes)
    assert selected == select_second_annotator(list(reversed(rows)), 15, seed=3)
    assert len(select_second_annotator(rows[:4], 15, seed=3)) == 4
    with pytest.raises(ValueError, match="count"):
        select_second_annotator(rows, 0, seed=3)


def test_merge_rows_keeps_existing_order_and_groups_sets() -> None:
    existing = [_row(1), _row(2), _row(3, SampleSet.CLASSIFICATION_ONLY)]
    new = [_row(4), _row(5, SampleSet.CLASSIFICATION_ONLY)]
    merged = merge_annotation_rows(existing, new)
    assert [row.paper_id.int for row in merged] == [1, 2, 4, 3, 5]


def test_count_topics_unclassified_and_pending() -> None:
    rows = [_row(1), _row(2), _row(3), _row(4, SampleSet.CLASSIFICATION_ONLY)]
    counts = count_topics(
        rows,
        {
            uuid.UUID(int=1): ("e_commerce", "erp"),
            uuid.UUID(int=2): ("unclassified",),
            uuid.UUID(int=4): ("erp",),
        },
    )
    assert counts.by_set[SampleSet.ABSTRACT] == {"e_commerce": 1, "erp": 1, "unclassified": 1}
    assert counts.by_set[SampleSet.CLASSIFICATION_ONLY] == {"erp": 1}
    assert counts.pending == {SampleSet.ABSTRACT: 1, SampleSet.CLASSIFICATION_ONLY: 0}

    lines = render_topic_counts(counts, TEST_TAXONOMY)
    assert any(line.split()[:4] == ["erp", "ERP", "1", "1"] for line in lines)
    assert any(line.split()[:2] == ["unclassified", "沒有主題"] for line in lines)
    assert any("還沒有分類結果" in line for line in lines)
    assert lines[-1] == "一篇可以有多個主題，各主題加起來會多於篇數。"


def test_render_sample_shows_counts_but_no_titles() -> None:
    rows = (_row(1), _row(2), _row(3, SampleSet.CLASSIFICATION_ONLY))
    result = AnnotationSampleResult(
        seed=7,
        interest=("erp",),
        rows=rows,
        existing_count=0,
        requested={SampleSet.ABSTRACT: 50, SampleSet.CLASSIFICATION_ONLY: 1},
        drawn={SampleSet.ABSTRACT: 2, SampleSet.CLASSIFICATION_ONLY: 1},
        pool={SampleSet.ABSTRACT: 2, SampleSet.CLASSIFICATION_ONLY: 1},
        skipped_without_doi=4,
        topic_counts=count_topics(rows, {}),
    )
    lines = render_annotation_sample(result, truncated_cells=1)
    assert lines[0] == "seed 7；興趣主題：erp"
    splits = Counter(row.split for row in rows[:2])
    assert lines[1] == (
        f"摘要層級：2 篇（調整組 {splits[EvalSplit.TUNING]}、測試組 {splits[EvalSplit.TEST]}）；"
        "這次新抽 2 篇，要 50 篇，候選 2 篇"
    )
    assert "摘要層級只有 2 篇候選，少於要的 50 篇，全部放進來" in lines
    assert "沒有 DOI、沒有抽的算數論文：4 篇" in lines
    assert "有 1 格超過 Excel 一格的上限，已截斷" in lines
    assert not any("T1" in line for line in lines)


def test_render_second_annotator_and_problems() -> None:
    selected = (_row(1), _row(2))
    lines = render_second_annotator(3, selected, available=2, requested=15)
    assert lines[0].startswith("seed 3：從摘要層級 2 篇抽 2 篇給標註者 B")
    assert lines[1] == "摘要層級只有 2 篇，少於要的 15 篇，全部放進來"

    problems = [
        AnnotationProblem("論文", n, "status（I）", "不在下拉清單裡") for n in range(2, 205)
    ]
    problems.append(AnnotationProblem(None, None, None, "不是 xlsx"))
    rendered = render_annotation_problems(problems)
    assert rendered[0] == "標註試算表有 204 個問題："
    assert rendered[1] == "- 論文 第 2 列 status（I）：不在下拉清單裡"
    assert len(rendered) == 1 + 200 + 1
    assert rendered[-1] == "還有 4 條"
    assert render_annotation_problems(problems[-1:])[1] == "- 不是 xlsx"
