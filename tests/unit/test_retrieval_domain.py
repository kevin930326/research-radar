"""檢索的 domain 形狀（Story 4.1、ADR-0005）：不連資料庫。

- 查詢字串當使用者輸入驗（`clean_search_query`）：每種不收的原因、訊息不含查詢內容；
  日誌只記長度與有沒有中文（`query_log_facts`）。
- `config/retrieval.yaml` 讀得過、值是寫好的初始值；每種錯誤在讀取時擋下
  （「檔名：欄位路徑：原因」）。
- 清單範圍換成檢索範圍；結果頁（`RetrievalPage`）與融合結果（`FusedMatch`）的不變條件。
- 查詢向量的結果代碼（`embedding_call_outcome`）與 `quota_exhausted`、`not_sent` 的紀錄條件。

資料全是測試自己編的。
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from radar.config import RETRIEVAL_FILE, ConfigError, load_retrieval_config
from radar.domain.embeddings import (
    EMBED_QUERY_OPERATION,
    EmbeddingApiCall,
    EmbeddingCallOutcome,
    embedding_call_outcome,
)
from radar.domain.errors import ErrorCode
from radar.domain.paper_list import PaperListScope
from radar.domain.retrieval import (
    FusedMatch,
    InvalidSearchQueryError,
    RetrievalAnchorNotFoundError,
    RetrievalConfig,
    RetrievalHit,
    RetrievalPage,
    RetrievalScope,
    SearchEmbeddingUnavailableError,
    SearchQueryProblem,
    SearchQuotaExhaustedError,
    clean_search_query,
    query_log_facts,
    retrieval_scope_for,
)
from tests.llm_support import ROOT

REAL_ENV = {"RADAR_CONFIG_DIR": str(ROOT / "config")}
GOOD = """vector_min_similarity: "0.2"
leg_limit: 100
rrf_k: 60
page_size: 20
qa_paper_count: 12
query_max_length: 500
"""
NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)

# --- 查詢字串 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("trust", "trust"),
        ("  區塊鏈 信任  ", "區塊鏈 信任"),
        ("👨‍👩‍👧 family", "👨‍👩‍👧 family"),
        ("x" * 20, "x" * 20),
        ("  " + "x" * 20 + "  ", "x" * 20),  # 長度上限去掉頭尾空白之後算
    ],
)
def test_clean_search_query_keeps_the_trimmed_text(value: str, expected: str) -> None:
    assert clean_search_query(value, max_length=20) == expected


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        (None, SearchQueryProblem.NOT_TEXT),
        (b"trust", SearchQueryProblem.NOT_TEXT),
        (["trust"], SearchQueryProblem.NOT_TEXT),
        ("", SearchQueryProblem.EMPTY),
        ("   ", SearchQueryProblem.EMPTY),
        ("　", SearchQueryProblem.EMPTY),  # 全形空白
        ("‍", SearchQueryProblem.EMPTY),  # 只有零寬連接
        (" ‍ ‍ ", SearchQueryProblem.EMPTY),
        ("x" * 21, SearchQueryProblem.TOO_LONG),
        ("\ttrust\n", SearchQueryProblem.INVALID_CHARACTER),  # tab、換行是控制字元
        ("trust\x00", SearchQueryProblem.INVALID_CHARACTER),
        ("\x07trust", SearchQueryProblem.INVALID_CHARACTER),  # 頭尾的控制字元也不收
        ("tr​ust", SearchQueryProblem.INVALID_CHARACTER),  # 零寬空格
        ("tr‮ust", SearchQueryProblem.INVALID_CHARACTER),  # 方向覆寫
        ("tr\ud800ust", SearchQueryProblem.INVALID_CHARACTER),  # 落單的代理字元
        ("\x00" * 30, SearchQueryProblem.INVALID_CHARACTER),  # 先看字元、再看長度
    ],
)
def test_clean_search_query_rejects_each_problem_without_echoing_the_query(
    value: object, problem: SearchQueryProblem
) -> None:
    with pytest.raises(InvalidSearchQueryError) as caught:
        clean_search_query(value, max_length=20)

    assert caught.value.problem is problem
    assert str(caught.value) == problem.value


def test_query_log_facts_are_length_and_cjk_only() -> None:
    assert query_log_facts("trust") == {"query_length": 5, "query_has_cjk": False}
    assert query_log_facts("區塊鏈 trust") == {"query_length": 9, "query_has_cjk": True}
    assert query_log_facts("𠀀") == {"query_length": 1, "query_has_cjk": True}  # 擴充 B
    assert query_log_facts("トラスト") == {"query_length": 4, "query_has_cjk": False}  # 片假名不算


# --- 設定 -------------------------------------------------------------------------------------


def _load(tmp_path: Path, text: str) -> RetrievalConfig:
    (tmp_path / RETRIEVAL_FILE).write_text(text, encoding="utf-8")
    return load_retrieval_config({"RADAR_CONFIG_DIR": str(tmp_path)})


def _problems(tmp_path: Path, text: str) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, text)
    problems = caught.value.problems
    assert problems
    assert all(problem.startswith(f"{RETRIEVAL_FILE}：") for problem in problems)
    return problems


def test_real_config_has_the_initial_values() -> None:
    config = load_retrieval_config(REAL_ENV)

    assert config == RetrievalConfig(
        vector_min_similarity=Decimal("0.2"),
        leg_limit=100,
        rrf_k=60,
        page_size=20,
        qa_paper_count=12,
        query_max_length=500,
    )
    assert config.max_results == 200
    assert config.vector_max_distance == Decimal("0.8")


def test_good_config_loads(tmp_path: Path) -> None:
    assert _load(tmp_path, GOOD).leg_limit == 100


@pytest.mark.parametrize(
    ("line", "replacement", "field"),
    [
        ('vector_min_similarity: "0.2"', 'vector_min_similarity: "1"', "vector_min_similarity"),
        ('vector_min_similarity: "0.2"', 'vector_min_similarity: "-0.1"', "vector_min_similarity"),
        ('vector_min_similarity: "0.2"', 'vector_min_similarity: "NaN"', "vector_min_similarity"),
        ("leg_limit: 100", "leg_limit: 0", "leg_limit"),
        ("leg_limit: 100", "leg_limit: 1001", "leg_limit"),  # hnsw.ef_search 上限 1000
        ("leg_limit: 100", 'leg_limit: "100"', "leg_limit"),  # 整數不收字串
        ("rrf_k: 60", "rrf_k: 0", "rrf_k"),
        ("page_size: 20", "page_size: 101", "page_size"),
        ("qa_paper_count: 12", "qa_paper_count: 0", "qa_paper_count"),
        ("query_max_length: 500", "query_max_length: 2001", "query_max_length"),
        ("query_max_length: 500", "", "query_max_length"),  # 缺欄位
    ],
)
def test_each_bad_value_is_rejected_at_load(
    tmp_path: Path, line: str, replacement: str, field: str
) -> None:
    problems = _problems(tmp_path, GOOD.replace(line, replacement))

    assert any(problem.startswith(f"{RETRIEVAL_FILE}：{field}") for problem in problems), problems


def test_extra_key_missing_file_and_duplicate_key_are_rejected(tmp_path: Path) -> None:
    assert _problems(tmp_path, GOOD + "fallback_to_keywords: true\n")
    assert _problems(tmp_path, GOOD + "rrf_k: 61\n")
    (tmp_path / RETRIEVAL_FILE).unlink()
    with pytest.raises(ConfigError) as caught:
        load_retrieval_config({"RADAR_CONFIG_DIR": str(tmp_path)})
    assert all(problem.startswith(f"{RETRIEVAL_FILE}：") for problem in caught.value.problems)


# --- 範圍與結果 -------------------------------------------------------------------------------


def test_list_scopes_map_to_retrieval_scopes_with_the_same_name() -> None:
    assert {scope: retrieval_scope_for(scope) for scope in PaperListScope} == {
        PaperListScope.PERSONAL: RetrievalScope.PERSONAL,
        PaperListScope.LAB: RetrievalScope.LAB,
        PaperListScope.TRACKED: RetrievalScope.TRACKED,
    }
    assert "demo" not in {scope.value for scope in PaperListScope}


def _hit(rank: int) -> RetrievalHit:
    return RetrievalHit(paper_id=uuid.uuid4(), rank=rank, vector_rank=rank, text_rank=None)


def test_page_accepts_consecutive_ranks_from_offset_plus_one() -> None:
    page = RetrievalPage(hits=(_hit(21), _hit(22)), total=22, offset=20, k=20)
    assert page.total == 22
    assert RetrievalPage(hits=(), total=5, offset=40, k=20).hits == ()  # 翻過頭是空的一頁


@pytest.mark.parametrize(
    ("hits", "total", "offset", "k"),
    [
        ((_hit(2), _hit(3)), 3, 0, 20),  # 沒有從 offset + 1 開始
        ((_hit(1), _hit(3)), 3, 0, 20),  # 不連續
        ((_hit(1), _hit(2)), 2, 0, 1),  # 超過 k
        ((_hit(1), _hit(2)), 1, 0, 20),  # total 比翻得到的少
        ((), -1, 0, 20),
    ],
)
def test_page_rejects_broken_invariants(
    hits: tuple[RetrievalHit, ...], total: int, offset: int, k: int
) -> None:
    with pytest.raises(ValueError, match=r"total|k|名次"):
        RetrievalPage(hits=hits, total=total, offset=offset, k=k)


@pytest.mark.parametrize(
    ("vector_rank", "text_rank"), [(None, None), (0, None), (None, -1), (True, None)]
)
def test_fused_match_needs_at_least_one_positive_rank(
    vector_rank: int | None, text_rank: int | None
) -> None:
    with pytest.raises(ValueError, match=r"腿|名次"):
        FusedMatch(paper_id=uuid.uuid4(), vector_rank=vector_rank, text_rank=text_rank)


def test_errors_carry_only_their_codes() -> None:
    assert str(SearchQuotaExhaustedError()) == ErrorCode.SEARCH_QUOTA_EXHAUSTED.value
    assert SearchQuotaExhaustedError.code is ErrorCode.SEARCH_QUOTA_EXHAUSTED
    assert str(SearchEmbeddingUnavailableError()) == ErrorCode.SEARCH_EMBEDDING_UNAVAILABLE.value
    assert SearchEmbeddingUnavailableError.code is ErrorCode.SEARCH_EMBEDDING_UNAVAILABLE
    assert len({SearchQuotaExhaustedError.code, SearchEmbeddingUnavailableError.code}) == 2
    assert str(RetrievalAnchorNotFoundError()) == "找不到這篇論文"


# --- 查詢向量的結果代碼 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (None, EmbeddingCallOutcome.UNREACHABLE),
        (408, EmbeddingCallOutcome.UNREACHABLE),
        (429, EmbeddingCallOutcome.UNREACHABLE),
        (500, EmbeddingCallOutcome.UNREACHABLE),
        (503, EmbeddingCallOutcome.UNREACHABLE),
        (200, EmbeddingCallOutcome.SUCCEEDED),
        (204, EmbeddingCallOutcome.SUCCEEDED),
        (400, EmbeddingCallOutcome.REJECTED),
        (401, EmbeddingCallOutcome.REJECTED),
        (404, EmbeddingCallOutcome.REJECTED),
    ],
)
def test_embedding_call_outcome(status: int | None, outcome: EmbeddingCallOutcome) -> None:
    assert embedding_call_outcome(status) is outcome


def test_quota_exhausted_record_has_no_status_and_no_tokens() -> None:
    record = EmbeddingApiCall(
        operation=EMBED_QUERY_OPERATION,
        http_status=None,
        estimated_input_tokens=0,
        called_at=NOW,
        outcome=EmbeddingCallOutcome.QUOTA_EXHAUSTED,
    )
    assert record.outcome is EmbeddingCallOutcome.QUOTA_EXHAUSTED

    for status, tokens in [(200, 0), (None, 6)]:
        with pytest.raises(ValueError, match="quota_exhausted"):
            EmbeddingApiCall(
                operation=EMBED_QUERY_OPERATION,
                http_status=status,
                estimated_input_tokens=tokens,
                called_at=NOW,
                outcome=EmbeddingCallOutcome.QUOTA_EXHAUSTED,
            )


def test_outcome_string_is_normalized_and_unknown_is_rejected() -> None:
    record = EmbeddingApiCall(
        operation=EMBED_QUERY_OPERATION,
        http_status=200,
        estimated_input_tokens=6,
        called_at=NOW,
        outcome="succeeded",  # type: ignore[arg-type]
    )
    assert record.outcome is EmbeddingCallOutcome.SUCCEEDED
    with pytest.raises(ValueError, match="timeout"):
        EmbeddingApiCall(
            operation=EMBED_QUERY_OPERATION,
            http_status=200,
            estimated_input_tokens=6,
            called_at=NOW,
            outcome="timeout",  # type: ignore[arg-type]
        )


def test_not_sent_record_has_no_status_and_no_tokens() -> None:
    # 審查 H2：沒有送出的其他失敗（等額度鎖逾時等）也記一列，條件和 quota_exhausted 一樣。
    record = EmbeddingApiCall(
        operation=EMBED_QUERY_OPERATION,
        http_status=None,
        estimated_input_tokens=0,
        called_at=NOW,
        outcome=EmbeddingCallOutcome.NOT_SENT,
    )
    assert record.outcome is EmbeddingCallOutcome.NOT_SENT

    for status, tokens in [(200, 0), (None, 6)]:
        with pytest.raises(ValueError, match="not_sent"):
            EmbeddingApiCall(
                operation=EMBED_QUERY_OPERATION,
                http_status=status,
                estimated_input_tokens=tokens,
                called_at=NOW,
                outcome=EmbeddingCallOutcome.NOT_SENT,
            )
