"""`config/import_baseline.yaml` 與 `radar.config.load_import_baseline`（Story 1.9、ADR-0026）。

repo 裡的基準檔載得起來、合計 9,643、期刊代號和 `journals.yaml` 相同、和 OQ-02 的
`spikes/oq-02/output/results.json` 一致（檔不在就跳過）；壞的檔各有一條錯誤。
"""

import json
from pathlib import Path

import pytest

from radar.config import (
    IMPORT_BASELINE_FILE,
    ConfigError,
    load_domain_config,
    load_import_baseline,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
OQ02_RESULTS = ROOT / "spikes" / "oq-02" / "output" / "results.json"


def _env(directory: Path) -> dict[str, str]:
    return {"RADAR_CONFIG_DIR": str(directory)}


def test_repo_baseline_loads_and_totals_9643() -> None:
    baseline = load_import_baseline(_env(CONFIG))

    assert (baseline.year_from, baseline.year_to) == (2016, 2025)
    assert baseline.total() == 9643
    for years in baseline.journals.values():
        assert sorted(years) == list(range(2016, 2026))


def test_repo_baseline_uses_the_journal_keys_of_journals_yaml() -> None:
    baseline = load_import_baseline(_env(CONFIG))
    journals = load_domain_config(_env(CONFIG)).journals

    assert list(baseline.journals) == [journal.key for journal in journals.journals]


def test_repo_baseline_matches_oq02_results() -> None:
    if not OQ02_RESULTS.is_file():
        pytest.skip("沒有 spikes/oq-02/output/results.json")
    results = json.loads(OQ02_RESULTS.read_text(encoding="utf-8"))
    key_of = {
        journal.issn_l: journal.key
        for journal in load_domain_config(_env(CONFIG)).journals.journals
    }
    expected = {
        key_of[entry["source"]["issn_l"]]: {
            int(year): count for year, count in entry["counts"].items()
        }
        for entry in results
    }

    assert load_import_baseline(_env(CONFIG)).journals == expected


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / IMPORT_BASELINE_FILE).write_text(text, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (
            "year_from: 2016\nyear_to: 2025\njournals:\n  misq:\n    2015: 3\n",
            "import_baseline.yaml：（最上層）：journals.misq.2015：年份不在 2016–2025 裡",
        ),
        (
            "year_from: 2016\nyear_to: 2025\njournals:\n  misq:\n    2016: -1\n",
            "import_baseline.yaml：journals.misq.2016：Input should be greater than or equal to 0",
        ),
        (
            "year_from: 2016\nyear_to: 2025\njournals:\n  MISQ:\n    2016: 1\n",
            "import_baseline.yaml：journals.MISQ.[key]：String should match pattern",
        ),
        (
            "year_from: 2016\nyear_to: 2025\njournals:\n  misq:\n    2016: 1\n    2016: 2\n",
            "import_baseline.yaml：journals.misq.2016（第 6 行）：同一層的鍵寫了兩次",
        ),
        ("journals: [\n", "import_baseline.yaml：不是合法的 YAML"),
    ],
    ids=["year-out-of-range", "negative", "key-format", "duplicate-key", "bad-yaml"],
)
def test_broken_baseline_reports_one_problem(tmp_path: Path, text: str, problem: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_import_baseline(_env(_write(tmp_path, text)))

    [reported] = caught.value.problems
    assert reported.startswith(problem), reported


def test_missing_baseline_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_import_baseline(_env(tmp_path))

    [reported] = caught.value.problems
    assert reported.startswith("import_baseline.yaml：找不到檔案")
