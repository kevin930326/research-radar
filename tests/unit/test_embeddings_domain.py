"""向量化的純函式（Story 2.8）：輸入組法、指紋、放行後接起來、估 token、額度、向量檢查、
halfvec 文字格式、設定檔、`Settings` 的 Cloudflare 欄位，以及回填指令的輸出。

標題、摘要都是測試自己編的。
"""

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from radar.config import ConfigError, load_embedding_config, load_settings
from radar.domain.ai_outputs import CurrentAbstract, InputKind, PaperLlmSource
from radar.domain.embeddings import (
    EMBED_SEPARATOR,
    EmbeddingApiCall,
    EmbeddingBacklog,
    EmbeddingConfig,
    EmbeddingQuota,
    EmbeddingResponseError,
    EmbeddingTokenEstimate,
    EmbedRunResult,
    EmbedStop,
    NoEmbeddingInputError,
    WorkersAiApi,
    build_embedding_input,
    check_embedding_vectors,
    days_needed,
    estimate_embedding_tokens,
    halfvec_literal,
    parse_halfvec,
    quota_allows,
    render_backfill_report,
    render_backlog_estimate,
)
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import embedding_input_sha256
from radar.domain.llm import NotCloudSafeError
from radar.domain.papers import AbstractSource
from radar.domain.policy import (
    CloudPolicy,
    ContentSource,
    clear_for_cloud,
    cloud_text,
    join_cloud_safe_texts,
)

ROOT = Path(__file__).resolve().parents[2]
PAPER_ID = uuid.UUID("01890000-0000-7000-8000-000000000001")
ABSTRACT_ID = uuid.UUID("01890000-0000-7000-8000-000000000002")
TITLE = "Collaboration platform adoption in remote teams"
ABSTRACT = "This study examines how remote teams adopt collaboration platforms."
POLICY = CloudPolicy()
DB_URL = "postgresql://radar_app@127.0.0.1:5432/radar"
FAKE_ACCOUNT = "0123456789abcdef0123456789abcdef"


def _source(
    title: str = TITLE,
    abstract: str | None = ABSTRACT,
    abstract_source: AbstractSource = AbstractSource.OPENALEX,
) -> PaperLlmSource:
    current = None if abstract is None else CurrentAbstract(ABSTRACT_ID, abstract_source, abstract)
    return PaperLlmSource(paper_id=PAPER_ID, title=title, abstract=current)


def _quota(**changes: Any) -> EmbeddingQuota:
    values: dict[str, Any] = {
        "daily_free_neurons": 10000,
        "neurons_per_million_input_tokens": 1075,
        "stop_at_ratio": "0.9",
        "verified_on": "2026-11-15",
        "source_url": "https://developers.cloudflare.com/workers-ai/platform/pricing/",
    }
    values.update(changes)
    return EmbeddingQuota.model_validate(values)


ESTIMATE = EmbeddingTokenEstimate(bytes_per_token=Decimal(3), overhead_tokens_per_text=4)


# --- 輸入 -------------------------------------------------------------------------------------


def test_abstract_input_is_title_blank_line_abstract() -> None:
    built = build_embedding_input(_source(f"  {TITLE} ", f"\n{ABSTRACT}  "), InputKind.ABSTRACT)

    assert built.text == f"{TITLE}\n\n{ABSTRACT}"
    assert built.parts == (
        (TITLE, ContentSource.TITLE),
        (ABSTRACT, ContentSource.OPENALEX_ABSTRACT),
    )
    assert built.abstract_id == ABSTRACT_ID
    assert EMBED_SEPARATOR == "\n\n"


def test_title_input_has_only_the_title() -> None:
    built = build_embedding_input(_source(), InputKind.TITLE)
    assert built.text == TITLE
    assert built.parts == ((TITLE, ContentSource.TITLE),)
    assert built.abstract_id is None


def test_abstract_source_comes_from_the_row() -> None:
    built = build_embedding_input(
        _source(abstract_source=AbstractSource.SEMANTIC_SCHOLAR), InputKind.ABSTRACT
    )
    assert built.parts[1][1] is ContentSource.SEMANTIC_SCHOLAR_ABSTRACT


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        (_source(title="   "), InputKind.TITLE),
        (_source(title=""), InputKind.ABSTRACT),
        (_source(abstract=None), InputKind.ABSTRACT),
        (_source(abstract=" \n "), InputKind.ABSTRACT),
        (_source(), InputKind.FULLTEXT),
    ],
)
def test_missing_content_raises(source: PaperLlmSource, kind: InputKind) -> None:
    with pytest.raises(NoEmbeddingInputError):
        build_embedding_input(source, kind)


# --- 指紋 -------------------------------------------------------------------------------------


def test_fingerprint_changes_with_text_and_kind_only() -> None:
    base = embedding_input_sha256("a\n\nb", "abstract")
    assert len(base) == 64
    assert base == embedding_input_sha256("a\n\nb", "abstract")
    assert base != embedding_input_sha256("a\n\nc", "abstract")
    assert base != embedding_input_sha256("a\n\nb", "title")


@pytest.mark.parametrize("text", ["", "  \n"])
def test_fingerprint_rejects_blank(text: str) -> None:
    with pytest.raises(ValueError, match="不是空白"):
        embedding_input_sha256(text, "title")


# --- 放行後接起來 -----------------------------------------------------------------------------


def test_join_keeps_cleared_parts_only() -> None:
    title = clear_for_cloud(TITLE, ContentSource.TITLE, None, POLICY)
    abstract = clear_for_cloud(ABSTRACT, ContentSource.OPENALEX_ABSTRACT, None, POLICY)

    joined = join_cloud_safe_texts([title, abstract], EMBED_SEPARATOR)

    assert cloud_text(joined) == f"{TITLE}\n\n{ABSTRACT}"


@pytest.mark.parametrize(
    "parts",
    ["plain", [], ["plain"], ("x",), {"a"}, None],
)
def test_join_rejects_uncleared_parts(parts: object) -> None:
    with pytest.raises(NotCloudSafeError):
        join_cloud_safe_texts(parts, EMBED_SEPARATOR)


def test_join_separator_must_be_whitespace() -> None:
    title = clear_for_cloud(TITLE, ContentSource.TITLE, None, POLICY)
    with pytest.raises(ValueError, match="空白"):
        join_cloud_safe_texts([title, title], "\n<secret>\n")
    with pytest.raises(TypeError):
        join_cloud_safe_texts([title], None)  # type: ignore[arg-type]


# --- 估 token 與額度 --------------------------------------------------------------------------


def test_token_estimate_is_ceil_bytes_over_three_plus_overhead() -> None:
    # "abcd"：4 位元組 → ceil(4/3)=2，＋4；「資管」：6 位元組 → 2，＋4。
    assert estimate_embedding_tokens(["abcd"], ESTIMATE) == 6
    assert estimate_embedding_tokens(["資管"], ESTIMATE) == 6
    assert estimate_embedding_tokens(["abcd", "資管", ""], ESTIMATE) == 16
    assert estimate_embedding_tokens([], ESTIMATE) == 0


@given(st.lists(st.text(max_size=200), max_size=10))
def test_token_estimate_is_never_below_a_quarter_of_the_bytes(texts: list[str]) -> None:
    # 保守：每 3 個位元組至少算一個 token（真實的 tokenizer 英文約 4 個位元組一個）。
    size = sum(len(text.encode("utf-8")) for text in texts)
    assert estimate_embedding_tokens(texts, ESTIMATE) >= size / 3


def test_quota_numbers() -> None:
    quota = _quota()
    assert quota.stop_at_neurons == Decimal(9000)
    assert quota.daily_free_tokens == 9_302_325
    assert quota.usable_tokens_per_day == 8_372_093
    assert quota.tokens_to_neurons(1_000_000) == Decimal(1075)


def test_quota_allows_up_to_the_ratio_inclusive() -> None:
    quota = _quota()
    limit = quota.usable_tokens_per_day
    assert quota_allows(limit - 100, 100, quota)
    assert not quota_allows(limit, 100, quota)
    assert not quota_allows(0, limit + 1, quota)
    with pytest.raises(ValueError, match="負"):
        quota_allows(-1, 0, quota)


def test_days_needed_rounds_up() -> None:
    quota = _quota()
    assert days_needed(0, quota) == Decimal(0)
    assert days_needed(1, quota) == Decimal("0.1")
    assert days_needed(quota.usable_tokens_per_day, quota) == Decimal("1.0")
    assert days_needed(quota.usable_tokens_per_day + 1, quota) == Decimal("1.1")


@pytest.mark.parametrize(
    "changes",
    [
        {"stop_at_ratio": "0"},
        {"stop_at_ratio": "1.1"},
        {"stop_at_ratio": "NaN"},
        {"daily_free_neurons": 0},
        {"source_url": "http://example.com"},
        {"verified_on": "yesterday"},
    ],
)
def test_bad_quota_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic 的 ValidationError 是 ValueError
        _quota(**changes)


# --- 向量與 halfvec ---------------------------------------------------------------------------


def test_check_vectors_accepts_exact_shape() -> None:
    assert check_embedding_vectors([[1, 0.5], [-1, 0]], count=2, dimensions=2) == [
        [1.0, 0.5],
        [-1.0, 0.0],
    ]


@pytest.mark.parametrize(
    ("raw", "count", "dimensions"),
    [
        (None, 1, 2),
        ("[[1, 2]]", 1, 2),
        ([[1, 2]], 2, 2),
        ([[1, 2], [1]], 2, 2),
        ([[1, 2, 3]], 1, 2),
        ([(1, 2)], 1, 2),
        ([[1, "2"]], 1, 2),
        ([[1, True]], 1, 2),
        ([[1, float("nan")]], 1, 2),
        ([[1, float("inf")]], 1, 2),
        ([[1, 70000]], 1, 2),
    ],
)
def test_check_vectors_rejects_anything_else(raw: object, count: int, dimensions: int) -> None:
    with pytest.raises(EmbeddingResponseError) as caught:
        check_embedding_vectors(raw, count=count, dimensions=dimensions)
    assert caught.value.code is ErrorCode.EMBED_RESPONSE_INVALID


@given(
    st.lists(
        st.floats(min_value=-65504, max_value=65504, allow_nan=False, width=16),
        min_size=1,
        max_size=16,
    )
)
def test_halfvec_literal_round_trips(vector: list[float]) -> None:
    assert parse_halfvec(halfvec_literal(vector)) == tuple(vector)


@pytest.mark.parametrize("vector", [[], [float("nan")], [1e6]])
def test_halfvec_literal_rejects_bad_vectors(vector: list[float]) -> None:
    with pytest.raises(ValueError, match="向量"):
        halfvec_literal(vector)


def test_api_call_validation() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 11, 15, tzinfo=UTC)
    EmbeddingApiCall("embed_documents", None, 0, now)
    with pytest.raises(ValueError, match="操作"):
        EmbeddingApiCall("delete", 200, 1, now)
    with pytest.raises(ValueError, match="estimated_input_tokens"):
        EmbeddingApiCall("embed_documents", 200, -1, now)
    with pytest.raises(ValueError, match="時區"):
        EmbeddingApiCall("embed_documents", 200, 1, datetime(2026, 11, 15))


# --- 設定 -------------------------------------------------------------------------------------


def test_repo_config_file_is_valid_and_matches_the_registered_model() -> None:
    config = load_embedding_config({"RADAR_CONFIG_DIR": str(ROOT / "config")})

    assert config.model_id == "@cf/qwen/qwen3-embedding-0.6b"
    assert config.dimensions == 1024
    assert config.batch_size == 32
    assert config.query_instruction is None
    assert config.api.base_url == "https://api.cloudflare.com/client/v4"
    assert config.quota.daily_free_neurons == 10000
    assert config.quota.neurons_per_million_input_tokens == 1075
    assert config.quota.stop_at_ratio == Decimal("0.9")
    assert config.quota.verified_on == date(2026, 11, 15)
    assert config.quota.source_url == (
        "https://developers.cloudflare.com/workers-ai/platform/pricing/"
    )


def test_config_rejects_bigger_batches_and_unknown_keys(tmp_path: Path) -> None:
    text = (ROOT / "config" / "embeddings.yaml").read_text(encoding="utf-8")
    (tmp_path / "embeddings.yaml").write_text(
        text.replace("batch_size: 32", "batch_size: 33") + "\nextra_key: 1\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as caught:
        load_embedding_config({"RADAR_CONFIG_DIR": str(tmp_path)})
    assert any(
        problem.startswith("embeddings.yaml：batch_size") for problem in caught.value.problems
    )
    assert any("extra_key" in problem for problem in caught.value.problems)


def test_config_base_url_must_be_https() -> None:
    values = EmbeddingConfig.model_json_schema()
    assert "api" in values["properties"]
    config = load_embedding_config({"RADAR_CONFIG_DIR": str(ROOT / "config")})
    data = config.model_dump(mode="json")
    data["api"]["base_url"] = "http://api.cloudflare.com/client/v4"
    with pytest.raises(ValueError, match="https"):
        EmbeddingConfig.model_validate(data)


def test_cloudflare_settings_are_secret_and_validated() -> None:
    settings = load_settings(
        {
            "DATABASE_URL": DB_URL,
            "RADAR_CLOUDFLARE_ACCOUNT_ID": FAKE_ACCOUNT,
            "RADAR_CLOUDFLARE_API_TOKEN": "fake-token-for-tests",
        }
    )
    assert settings.cloudflare_account_id is not None
    assert settings.cloudflare_account_id.get_secret_value() == FAKE_ACCOUNT
    assert FAKE_ACCOUNT not in repr(settings)
    assert "fake-token-for-tests" not in repr(settings)

    unset = load_settings({"DATABASE_URL": DB_URL})
    assert unset.cloudflare_account_id is None
    assert unset.cloudflare_api_token is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RADAR_CLOUDFLARE_ACCOUNT_ID", "not-hex-but-has-32-characters-xx"),
        ("RADAR_CLOUDFLARE_ACCOUNT_ID", "0123456789abcdef/../../evil?x=1"),
        ("RADAR_CLOUDFLARE_ACCOUNT_ID", "0123456789abcdef"),
        ("RADAR_CLOUDFLARE_API_TOKEN", "has space-in-it"),
        ("RADAR_CLOUDFLARE_API_TOKEN", "line\nbreak"),
        ("RADAR_CLOUDFLARE_API_TOKEN", "非ASCII"),
    ],
)
def test_bad_cloudflare_settings_never_echo_the_value(name: str, value: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings({"DATABASE_URL": DB_URL, name: value})
    assert caught.value.problems[0].startswith(f"環境變數 {name} 格式不對")
    assert value not in str(caught.value)


# --- 回填指令的輸出 ---------------------------------------------------------------------------


def test_backlog_estimate_lines() -> None:
    backlog = EmbeddingBacklog(papers=40_000, estimated_tokens=16_000_000, blocked=3)
    lines = render_backlog_estimate(backlog, _quota(), "@cf/qwen/qwen3-embedding-0.6b")
    text = "\n".join(lines)

    assert "沒有送任何請求" in lines[0]
    assert "40,000 篇" in text
    assert "16,000,000 token" in text
    assert "平均每篇約 400 token" in text
    assert "10,000 neurons" in text
    assert "2026-11-15 查證" in text
    assert "推算要 2.0 天" in text
    assert "放行擋下、不會送出的論文：3 篇" in text


def test_backfill_report_projects_with_this_run_average() -> None:
    result = EmbedRunResult(
        stop=EmbedStop.LIMIT_REACHED, embedded=20, estimated_tokens=6_000, requests=1, unchanged=5
    )
    backlog = EmbeddingBacklog(papers=30_000, estimated_tokens=1, blocked=0)
    text = "\n".join(render_backfill_report(result, backlog, _quota(), "m"))

    assert "到了這次指定的篇數" in text
    assert "這次寫入向量：20 篇（送出 1 次請求）" in text
    assert "這次估計用了：6,000 token" in text
    assert "平均每篇約 300 token" in text
    # 300 × 30,000 = 9,000,000 token；一天可用 8,372,093 → 1.1 天。
    assert "照這次的平均推算還要：9,000,000 token" in text
    assert "推算還要 1.1 天" in text
    assert "沒變 5 篇" in text


def test_backfill_report_without_writes_uses_local_estimate() -> None:
    result = EmbedRunResult(stop=EmbedStop.QUOTA_REACHED)
    backlog = EmbeddingBacklog(papers=10, estimated_tokens=4_000, blocked=0)
    text = "\n".join(render_backfill_report(result, backlog, _quota(), "m"))

    assert "台北 08:00" in text
    assert "照本地估計還要：4,000 token" in text


def test_backfill_report_names_the_failure_reason() -> None:
    result = EmbedRunResult(
        stop=EmbedStop.FAILED, failed=3, failure_reason=ErrorCode.EMBED_TRANSIENT
    )
    lines = render_backfill_report(result, EmbeddingBacklog(3, 300, 0), _quota(), "m")
    assert lines[0].endswith("失敗（embed_transient）")


@pytest.mark.parametrize(("ratio", "shown"), [("0.9", "90%"), ("0.95", "95%"), ("1", "100%")])
def test_quota_ratio_is_shown_without_trailing_zeros(ratio: str, shown: str) -> None:
    lines = render_backlog_estimate(EmbeddingBacklog(0, 0, 0), _quota(stop_at_ratio=ratio), "m")
    assert f"到 {shown} 就停" in "\n".join(lines)


def test_quota_that_leaves_no_usable_tokens_is_rejected() -> None:
    # 推算天數要除以每天可用的 token；換算後是 0 的設定在讀檔時就擋下。
    with pytest.raises(ValueError, match="不到 1"):
        _quota(daily_free_neurons=1, stop_at_ratio="0.0001", neurons_per_million_input_tokens=1075)


def test_request_limit_is_the_total_timeout_plus_two_phase_timeouts() -> None:
    # 審查後的修正（Codex 中 3）：總時限涵蓋所有重試與等待，嘗試次數與重試等待不再加進去。
    api = WorkersAiApi.model_validate(
        {
            "base_url": "https://api.cloudflare.com/client/v4",
            "timeout_seconds": 60,
            "total_timeout_seconds": 480,
            "retry": {
                "attempts": 5,
                "wait_initial_seconds": 1,
                "wait_max_seconds": 30,
                "wait_jitter_seconds": 1,
                "retry_after_max_seconds": 120,
            },
        }
    )
    assert api.request_limit_seconds == 480 + 2 * 60
    more_attempts = api.model_copy(update={"retry": api.retry.model_copy(update={"attempts": 10})})
    assert more_attempts.request_limit_seconds == 600


# --- 審查後的修正（Codex 中 3、4、6、7） -------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        [0.0, 0.0, 0.0],
        [0, 0, 0],
        [1e-9, -1e-9, 0.0],  # 轉成半精度會下溢成 0
        [2.9e-8, 0.0, -2.9e-8],
    ],
)
def test_vector_that_is_all_zero_at_halfvec_precision_is_rejected(row: list[float]) -> None:
    with pytest.raises(EmbeddingResponseError, match="全是 0"):
        check_embedding_vectors([[0.1, 0.2, 0.3], row], count=2, dimensions=3)


def test_vector_with_one_component_that_survives_halfvec_is_accepted() -> None:
    # 1e-7 轉成半精度是最小的次正規數（約 1.19e-7），不是 0。
    assert check_embedding_vectors([[0.0, 1e-7, 1e-9]], count=1, dimensions=3) == [
        [0.0, 1e-7, 1e-9]
    ]


def test_has_failures_covers_single_failed_papers_without_a_failed_stop() -> None:
    assert not EmbedRunResult(stop=EmbedStop.COMPLETED).has_failures
    assert not EmbedRunResult(stop=EmbedStop.QUOTA_REACHED).has_failures
    assert EmbedRunResult(stop=EmbedStop.COMPLETED, failed=1).has_failures
    assert EmbedRunResult(stop=EmbedStop.LIMIT_REACHED, failed=2).has_failures
    assert EmbedRunResult(stop=EmbedStop.FAILED).has_failures


def test_backfill_report_names_single_failures_even_when_completed() -> None:
    result = EmbedRunResult(
        stop=EmbedStop.COMPLETED,
        embedded=2,
        estimated_tokens=60,
        requests=4,
        failed=1,
        failure_reason=ErrorCode.EMBED_REQUEST_INVALID,
    )
    lines = render_backfill_report(result, EmbeddingBacklog(1, 30, 0), _quota(), "m")
    assert lines[0] == "向量化結束（模型 m）：要做的論文都做完了"
    assert lines[1] == "這次有 1 篇失敗（embed_request_invalid），結束碼 1"


def test_report_texts_no_longer_promise_an_overestimate() -> None:
    result = EmbedRunResult(stop=EmbedStop.COMPLETED, embedded=1, estimated_tokens=30)
    backlog = EmbeddingBacklog(1, 30, 0)
    text = "\n".join(
        render_backfill_report(result, backlog, _quota(), "m")
        + render_backlog_estimate(backlog, _quota(), "m")
    )
    assert "偏高" not in text
    assert "保證" not in text
    assert "估計值" in text


def test_shipped_config_bounds_a_run_below_the_default_sweep_limit() -> None:
    config = load_embedding_config({"RADAR_CONFIG_DIR": str(ROOT / "config")})
    assert config.api.total_timeout_seconds == 480
    assert config.lock_timeout_seconds == 2
    assert config.lock_timeout_ms == 2000
    # 480＋2 × 60＋(2 × 32＋2) × 2
    assert config.api.request_limit_seconds == 600
    assert config.run_limit_seconds == 732
    assert config.run_limit_seconds < load_settings({"DATABASE_URL": DB_URL}).llm_run_stale_after
    assert config.token_estimate.bytes_per_token == Decimal(3)


def test_shipped_config_explains_the_divisor_and_its_calibration() -> None:
    text = (ROOT / "config" / "embeddings.yaml").read_text(encoding="utf-8")
    assert "經驗值" in text
    assert "20 篇試跑" in text
    assert "neurons" in text
    assert "偏高" not in text
    assert "保守）" not in text
