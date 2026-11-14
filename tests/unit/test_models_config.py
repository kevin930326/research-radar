"""`config/models.yaml` 與 `load_models_config`：價格寫成字串、模型在白名單、六個工作各一個模型。"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from radar.config import MODELS_FILE, ConfigError, load_models_config
from radar.domain.ai_outputs import LlmTask
from radar.domain.llm import ALLOWED_MODELS
from tests.llm_support import ROOT

REAL = (ROOT / "config" / MODELS_FILE).read_text(encoding="utf-8")


def _load(tmp_path: Path, text: str) -> object:
    (tmp_path / MODELS_FILE).write_text(text, encoding="utf-8")
    return load_models_config({"RADAR_CONFIG_DIR": str(tmp_path)})


def test_real_config_matches_adr_0009() -> None:
    config = load_models_config({"RADAR_CONFIG_DIR": str(ROOT / "config")})
    price = config.models["claude-haiku-5-5"]

    assert config.verified_on == date(2026, 11, 12)
    assert config.cross_checked_on == date(2026, 11, 14)
    assert config.source_url == "https://platform.claude.com/docs/en/about-claude/pricing"
    assert price.retires_not_before == date(2027, 10, 7)
    assert (price.input_usd_per_mtok, price.output_usd_per_mtok) == (
        Decimal("0.10"),
        Decimal("0.50"),
    )
    assert (price.long_context_input_usd_per_mtok, price.long_context_output_usd_per_mtok) == (
        Decimal("0.50"),
        Decimal("2.50"),
    )
    assert config.long_context_threshold_tokens == 100_000
    assert config.max_input_tokens == 100_000
    assert config.cache_read_multiplier == Decimal("0.1")
    assert config.cache_write_multiplier == Decimal("2")
    assert config.batch_multiplier == Decimal("0.5")
    assert set(config.tasks) == set(LlmTask)
    assert {choice.model for choice in config.tasks.values()} <= ALLOWED_MODELS
    assert not any(choice.allow_over_input_limit for choice in config.tasks.values())


def test_real_config_documents_the_conservative_estimate_and_retirement() -> None:
    assert "保守估計" in REAL
    assert "2027-10-07" in REAL
    assert "2026-11-14" in REAL


def test_float_price_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"models\.claude-haiku-5-5\.input_usd_per_mtok"):
        _load(tmp_path, REAL.replace('input_usd_per_mtok: "0.10"', "input_usd_per_mtok: 0.10"))


def test_float_multiplier_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="batch_multiplier"):
        _load(tmp_path, REAL.replace('batch_multiplier: "0.5"', "batch_multiplier: 0.5"))


@pytest.mark.parametrize("bad", ['"-0.1"', '"abc"', '"NaN"', '"Infinity"'])
def test_bad_price_strings_are_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match="input_usd_per_mtok"):
        _load(tmp_path, REAL.replace('input_usd_per_mtok: "0.10"', f"input_usd_per_mtok: {bad}"))


def test_model_outside_allowed_models_is_rejected(tmp_path: Path) -> None:
    text = REAL.replace("  claude-haiku-5-5:\n    # 官方", "  claude-opus-9:\n    # 官方")

    with pytest.raises(ConfigError, match="ALLOWED_MODELS"):
        _load(tmp_path, text)


def test_task_model_without_price_is_rejected(tmp_path: Path) -> None:
    text = REAL.replace(
        "  zh_summary:\n    model: claude-haiku-5-5", "  zh_summary:\n    model: claude-x"
    )

    with pytest.raises(ConfigError, match=r"tasks\.zh_summary\.model"):
        _load(tmp_path, text)


def test_missing_task_is_rejected(tmp_path: Path) -> None:
    text = REAL.replace(
        "  eval_judge:\n    model: claude-haiku-5-5\n    allow_over_input_limit: false\n", ""
    )

    with pytest.raises(ConfigError, match="eval_judge"):
        _load(tmp_path, text)


def test_unknown_task_and_unknown_key_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, REAL + "  translate:\n    model: claude-haiku-5-5\n")
    with pytest.raises(ConfigError, match="temperature"):
        _load(tmp_path, REAL + "temperature: 0\n")


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="找不到檔案"):
        load_models_config({"RADAR_CONFIG_DIR": str(tmp_path)})
