"""閘門的時間設定：`RADAR_ANTHROPIC_DEADLINE`（總時限）與
`RADAR_LLM_RUN_STALE_AFTER`（sweep 時限）。
"""

import pytest

from radar.config import ConfigError, load_settings

BASE = {"DATABASE_URL": "postgresql://radar_app@db:5432/radar"}


def test_defaults() -> None:
    settings = load_settings(BASE)

    assert settings.anthropic_deadline == 180
    assert settings.llm_run_stale_after == 900
    assert settings.llm_run_stale_after > (
        settings.anthropic_deadline
        + settings.anthropic_timeout
        + max(
            settings.anthropic_retry_wait_max + settings.anthropic_retry_wait_jitter,
            settings.anthropic_retry_after_max,
        )
    )


def test_values_from_environment() -> None:
    settings = load_settings(
        {**BASE, "RADAR_ANTHROPIC_DEADLINE": "30", "RADAR_LLM_RUN_STALE_AFTER": "151"}
    )

    # 30 + 60（timeout）+ 60（retry_after_max）= 150 < 151。
    assert (settings.anthropic_deadline, settings.llm_run_stale_after) == (30, 151)


@pytest.mark.parametrize(
    "env",
    [
        {"RADAR_LLM_RUN_STALE_AFTER": "300"},  # = 180 + 60 + 60（retry_after_max）
        # Retry-After 最長可以照等 600 秒：180 + 60 + 600 = 840。
        {"RADAR_ANTHROPIC_RETRY_AFTER_MAX": "600", "RADAR_LLM_RUN_STALE_AFTER": "840"},
        # 指數退避的上限＋抖動比 retry_after_max 長時看它：180 + 60 + (300 + 60) = 600。
        {
            "RADAR_ANTHROPIC_RETRY_WAIT_MAX": "300",
            "RADAR_ANTHROPIC_RETRY_WAIT_JITTER": "60",
            "RADAR_LLM_RUN_STALE_AFTER": "600",
        },
        {"RADAR_ANTHROPIC_DEADLINE": "600", "RADAR_LLM_RUN_STALE_AFTER": "600"},
        {"RADAR_ANTHROPIC_TIMEOUT": "300", "RADAR_LLM_RUN_STALE_AFTER": "400"},
    ],
)
def test_stale_after_must_exceed_the_worst_case_call_time(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="RADAR_LLM_RUN_STALE_AFTER") as caught:
        load_settings({**BASE, **env})

    assert "RADAR_ANTHROPIC_DEADLINE + RADAR_ANTHROPIC_TIMEOUT" in str(caught.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RADAR_ANTHROPIC_DEADLINE", "0"),
        ("RADAR_ANTHROPIC_DEADLINE", "3601"),
        ("RADAR_LLM_RUN_STALE_AFTER", "59"),
        ("RADAR_LLM_RUN_STALE_AFTER", "86401"),
    ],
)
def test_out_of_range(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings({**BASE, name: value})


def test_bad_deadline_is_reported_once() -> None:
    # deadline 自己不合法時，不再多報一條 stale_after 的錯。
    with pytest.raises(ConfigError) as caught:
        load_settings({**BASE, "RADAR_ANTHROPIC_DEADLINE": "abc"})

    assert caught.value.problems == [
        problem for problem in caught.value.problems if "RADAR_ANTHROPIC_DEADLINE" in problem
    ]
