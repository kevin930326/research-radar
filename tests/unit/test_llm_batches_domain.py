"""Batch 送出的 domain（Story 2.9、ADR-0041）：月初窗口、保守上限費用、設定、固定鎖鍵、工作與
sweep 登記，以及 `FakeLlmProvider.submit_batch` 替身。"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from radar.adapters.fake import FakeLlmProvider
from radar.adapters.fake.llm import FakeRepliesExhaustedError
from radar.config import ConfigError, load_llm_batch_config
from radar.domain.ai_outputs import InputKind, LlmTask, RunKey, RunStatus
from radar.domain.fingerprints import LLM_BATCH_BUDGET_LOCK_KEY, named_lock_key
from radar.domain.jobs import (
    JOBS,
    PLAN_LLM_BATCHES,
    SUBMIT_LLM_BATCH,
    Queue,
    is_id_parameter,
)
from radar.domain.llm import LlmBatchRequest, LlmRequest, LlmServerError, NotCloudSafeError
from radar.domain.llm_batches import (
    QUEUED_STUCK_AFTER,
    SUBMITTING_STALE_AFTER,
    BatchItemRun,
    BatchSubmissionStatus,
    BatchSubmitUncertainError,
    LlmBatchConfig,
    in_month_start_hold,
)
from radar.domain.policy import CloudPolicy, ContentSource, clear_for_cloud
from radar.domain.pricing import max_call_cost
from radar.domain.sweep import SWEEP_REGISTRY
from radar.repo.db import EMBEDDING_QUOTA_LOCK_KEY, INGEST_WRITE_LOCK_KEY
from tests.llm_support import models_config
from tests.prompt_support import template_text

ROOT = Path(__file__).resolve().parents[2]
TAIPEI = ZoneInfo("Asia/Taipei")


# --- 月初窗口 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "held"),
    [
        (datetime(2026, 12, 1, 0, 0, tzinfo=TAIPEI), True),
        (datetime(2026, 12, 1, 7, 59, 59, tzinfo=TAIPEI), True),
        (datetime(2026, 12, 1, 8, 0, tzinfo=TAIPEI), False),
        (datetime(2026, 12, 2, 3, 0, tzinfo=TAIPEI), False),
        (datetime(2026, 11, 30, 23, 59, tzinfo=TAIPEI), False),
        # UTC 換算：UTC 11/30 16:00＝台北 12/1 00:00；UTC 12/1 00:00＝台北 08:00。
        (datetime(2026, 11, 30, 15, 59, tzinfo=UTC), False),
        (datetime(2026, 11, 30, 16, 0, tzinfo=UTC), True),
        (datetime(2026, 11, 30, 23, 59, tzinfo=UTC), True),
        (datetime(2026, 12, 1, 0, 0, tzinfo=UTC), False),
    ],
)
def test_month_start_hold_uses_taipei_time(now: datetime, held: bool) -> None:
    assert in_month_start_hold(now, 8) is held


def test_month_start_hold_zero_hours_never_holds() -> None:
    assert in_month_start_hold(datetime(2026, 12, 1, 0, 0, tzinfo=TAIPEI), 0) is False
    assert in_month_start_hold(datetime(2026, 12, 1, 23, 59, tzinfo=TAIPEI), 24) is True


def test_month_start_hold_rejects_naive_time_and_bad_hours() -> None:
    with pytest.raises(ValueError, match="時區"):
        in_month_start_hold(datetime(2026, 12, 1, 3, 0), 8)
    for hours in (-1, 25, True, 8.0):
        with pytest.raises(ValueError, match="hours"):
            in_month_start_hold(datetime(2026, 12, 1, 3, 0, tzinfo=UTC), hours)  # type: ignore[arg-type]


# --- 保守上限費用 -------------------------------------------------------------------------------


def test_max_call_cost_is_input_estimate_plus_full_output_at_batch_half_price() -> None:
    models = models_config()
    # (1000 × 0.10 + 1024 × 0.50) / 1e6 = 0.000612；Batch 五折 = 0.000306。
    assert max_call_cost(models, "claude-haiku-5-5", 1000, 1024, is_batch=False) == Decimal(
        "0.000612"
    )
    assert max_call_cost(models, "claude-haiku-5-5", 1000, 1024, is_batch=True) == Decimal(
        "0.000306"
    )


def test_max_call_cost_switches_to_long_context_price_over_threshold() -> None:
    models = models_config()
    threshold = models.long_context_threshold_tokens
    at = max_call_cost(models, "claude-haiku-5-5", threshold, 1, is_batch=False)
    over = max_call_cost(models, "claude-haiku-5-5", threshold + 1, 1, is_batch=False)
    # 門檻（含）以內用一般價；超過兩邊都改長上下文價（輸入 0.50、輸出 2.50）。
    assert at == Decimal("0.010001")  # (100000 × 0.10 + 0.50) / 1e6，無條件進位
    assert over == Decimal("0.050003")  # (100001 × 0.50 + 2.50) / 1e6 = 0.0500030


def test_max_call_cost_rounds_up_and_validates() -> None:
    models = models_config()
    # (1 × 0.10 + 1 × 0.50) / 1e6 × 0.5 = 0.0000003 → 進位到 0.000001。
    assert max_call_cost(models, "claude-haiku-5-5", 1, 1, is_batch=True) == Decimal("0.000001")
    with pytest.raises(ValueError, match="estimated_input_tokens"):
        max_call_cost(models, "claude-haiku-5-5", -1, 1, is_batch=True)
    with pytest.raises(ValueError, match="max_tokens"):
        max_call_cost(models, "claude-haiku-5-5", 1, 0, is_batch=True)
    with pytest.raises(KeyError):
        max_call_cost(models, "not-a-model", 1, 1, is_batch=True)


# --- 設定 ---------------------------------------------------------------------------------------


def test_repo_config_loads_with_documented_defaults() -> None:
    config = load_llm_batch_config({"RADAR_CONFIG_DIR": str(ROOT / "config")})

    assert config == LlmBatchConfig(max_requests_per_batch=500, month_start_hold_hours=8)


@pytest.mark.parametrize(
    "data",
    [
        {"max_requests_per_batch": 0, "month_start_hold_hours": 8},
        {"max_requests_per_batch": 100_001, "month_start_hold_hours": 8},
        {"max_requests_per_batch": "500", "month_start_hold_hours": 8},
        {"max_requests_per_batch": 500.0, "month_start_hold_hours": 8},
        {"max_requests_per_batch": 500, "month_start_hold_hours": 25},
        {"max_requests_per_batch": 500, "month_start_hold_hours": True},
        {"max_requests_per_batch": 500},
        {"max_requests_per_batch": 500, "month_start_hold_hours": 8, "extra": 1},
    ],
)
def test_bad_config_values_are_rejected(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LlmBatchConfig.model_validate(data)


def test_bad_config_file_lists_the_field(tmp_path: Path) -> None:
    (tmp_path / "llm_batches.yaml").write_text(
        "max_requests_per_batch: 0\nmonth_start_hold_hours: 8\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError) as caught:
        load_llm_batch_config({"RADAR_CONFIG_DIR": str(tmp_path)})

    assert any(
        p.startswith("llm_batches.yaml：max_requests_per_batch：") for p in caught.value.problems
    )


def test_missing_config_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"llm_batches\.yaml：找不到檔案"):
        load_llm_batch_config({"RADAR_CONFIG_DIR": str(tmp_path)})


# --- 鎖鍵、工作、sweep ----------------------------------------------------------------------------


def test_budget_lock_key_is_fixed_and_distinct() -> None:
    assert named_lock_key("llm_batch_budget") == LLM_BATCH_BUDGET_LOCK_KEY
    assert named_lock_key("other") != LLM_BATCH_BUDGET_LOCK_KEY
    assert -(2**63) <= LLM_BATCH_BUDGET_LOCK_KEY < 2**63
    assert LLM_BATCH_BUDGET_LOCK_KEY not in {INGEST_WRITE_LOCK_KEY, EMBEDDING_QUOTA_LOCK_KEY}
    with pytest.raises(ValueError, match="鎖的名稱"):
        named_lock_key("")


def test_batch_jobs_are_registered() -> None:
    assert PLAN_LLM_BATCHES in JOBS
    assert SUBMIT_LLM_BATCH in JOBS
    assert (PLAN_LLM_BATCHES.queue, PLAN_LLM_BATCHES.lock, PLAN_LLM_BATCHES.schedule) == (
        Queue.AI,
        "plan_llm_batches",
        None,
    )
    assert (SUBMIT_LLM_BATCH.queue, SUBMIT_LLM_BATCH.lock, SUBMIT_LLM_BATCH.schedule) == (
        Queue.AI,
        "llm_batches",
        None,
    )


def test_sweep_registers_queued_and_submitting_batches() -> None:
    entries = {
        entry.start_status: entry for entry in SWEEP_REGISTRY if entry.table == "batch_submission"
    }

    assert set(entries) == {"queued", "submitting"}
    for entry in entries.values():
        assert (entry.status_column, entry.time_column, entry.job) == (
            "status",
            "updated_at",
            SUBMIT_LLM_BATCH,
        )
        assert entry.id_parameter == "batch_submission_id"
        assert is_id_parameter(entry.id_parameter)
    assert entries["queued"].stuck_after == QUEUED_STUCK_AFTER == timedelta(hours=1)
    assert entries["submitting"].stuck_after == SUBMITTING_STALE_AFTER == timedelta(minutes=30)


def test_status_values_match_adr_0004() -> None:
    assert [status.value for status in BatchSubmissionStatus] == [
        "queued",
        "submitting",
        "submitted",
        "ended",
        "imported",
        "failed",
        "needs_attention",
    ]


def test_item_custom_id_is_the_item_uuid_hex() -> None:
    item_id = uuid.uuid4()
    item = BatchItemRun(
        item_id=item_id,
        run_id=uuid.uuid4(),
        key=RunKey(uuid.uuid4(), LlmTask.ZH_SUMMARY, 1, "claude-haiku-5-5", "a" * 64),
        input_kind=InputKind.ABSTRACT,
        run_status=RunStatus.RUNNING,
        abstract_id=None,
        estimated_max_cost_usd=Decimal("0.001"),
    )

    assert item.custom_id == item_id.hex
    assert len(item.custom_id) == 32


def test_uncertain_error_message_has_only_id_and_type() -> None:
    batch_id = uuid.uuid4()
    error = BatchSubmitUncertainError(batch_id, "LlmTimeoutError")

    assert str(batch_id) in str(error)
    assert "LlmTimeoutError" in str(error)
    assert error.batch_submission_id == batch_id


# --- FakeLlmProvider.submit_batch ----------------------------------------------------------------


def _batch_request(custom_id: str) -> LlmBatchRequest:
    policy = CloudPolicy()
    return LlmBatchRequest(
        custom_id=custom_id,
        request=LlmRequest(
            model="claude-haiku-5-5",
            system=template_text("請整理摘要。"),
            content=(clear_for_cloud("（假）標題", ContentSource.TITLE, None, policy),),
            max_tokens=256,
        ),
    )


def test_fake_submit_batch_records_and_replies_in_order() -> None:
    error = LlmServerError("HTTP 500", status=500)
    fake = FakeLlmProvider(batch_replies=["msgbatch_one", error])
    first = [_batch_request("a" * 32), _batch_request("b" * 32)]

    assert fake.submit_batch(first) == "msgbatch_one"
    with pytest.raises(LlmServerError):
        fake.submit_batch([_batch_request("c" * 32)])
    with pytest.raises(FakeRepliesExhaustedError):
        fake.submit_batch([_batch_request("d" * 32)])

    assert [len(call) for call in fake.batch_requests] == [2, 1, 1]
    assert [item.custom_id for item in fake.batch_requests[0]] == ["a" * 32, "b" * 32]
    fake.add_batch("msgbatch_two")
    assert fake.submit_batch(first) == "msgbatch_two"


def test_fake_submit_batch_validates_like_the_adapter() -> None:
    fake = FakeLlmProvider(batch_replies=["msgbatch_one"])

    with pytest.raises(ValueError, match="至少"):
        fake.submit_batch([])
    with pytest.raises(ValueError, match="重複"):
        fake.submit_batch([_batch_request("a" * 32), _batch_request("a" * 32)])
    with pytest.raises(NotCloudSafeError):
        fake.submit_batch([object()])  # type: ignore[list-item]

    assert fake.batch_requests == []
