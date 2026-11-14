"""排除規則：正規化、第一條符合的勝出、空清單；真的 `config/exclusion_rules.yaml` 的四條。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from radar.config import load_domain_config
from radar.domain.domain_config import ExclusionRule, ExclusionRulesConfig
from radar.domain.exclusion import exclusion_reason, normalize_title
from radar.domain.papers import ParsedPaper

ROOT = Path(__file__).resolve().parents[2]
RULES = load_domain_config({"RADAR_CONFIG_DIR": str(ROOT / "config")}).exclusion_rules


def _paper(title: str) -> ParsedPaper:
    return ParsedPaper(
        source="openalex",
        fetched_at=datetime(2026, 11, 14, tzinfo=UTC),
        doi="10.1/a",
        openalex_id="W1",
        title=title,
        journal_issn_l=None,
        publication_year=2020,
    )


def test_real_config_has_the_four_prd_rules() -> None:
    assert [rule.reason for rule in RULES.rules] == [
        "editors_comments",
        "research_spotlights",
        "about_our_authors",
        "erratum",
    ]


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("Editor’s Comments: Next-Generation Digital Platforms", "editors_comments"),
        ("EDITOR'S COMMENTS", "editors_comments"),
        ("  editor’s   comments ", "editors_comments"),
        ("Ｅｄｉｔｏｒ's Comments", "editors_comments"),  # 全形字經 NFKC 變半形
        ("Research Spotlights", "research_spotlights"),
        ("About Our Authors", "about_our_authors"),
        ("Erratum to “A study of X”", "erratum"),
        ("Erratum: A study of X", "erratum"),
        ("Erratum", "erratum"),
        ("About Our Authorship Practices in IS Research", None),
        ("Erratumology", None),
        ("Digital innovation and transformation", None),
        ("Comments from the editor", None),
    ],
)
def test_real_rules(title: str, reason: str | None) -> None:
    assert exclusion_reason(_paper(title), RULES) == reason


def test_empty_rules_exclude_nothing() -> None:
    empty = ExclusionRulesConfig(rules=())

    assert exclusion_reason(_paper("Editor’s Comments"), empty) is None


def test_first_matching_rule_wins_and_equals_is_exact() -> None:
    rules = ExclusionRulesConfig(
        rules=(
            ExclusionRule(reason="board", title_equals=("Editorial Board",)),
            ExclusionRule(reason="editorial", title_starts_with=("Editorial",)),
            ExclusionRule(reason="late", title_starts_with=("Editorial Board",)),
        )
    )

    assert exclusion_reason(_paper("editorial  board"), rules) == "board"
    assert exclusion_reason(_paper("Editorial Board Members"), rules) == "editorial"
    assert exclusion_reason(_paper("Editor"), rules) is None


def test_normalize_title() -> None:
    assert normalize_title("  Editor’s\tComments\n ") == "editor's comments"
    assert normalize_title("‘A’ ‛B‚ Cʼs") == "'a' 'b' c's"
    assert normalize_title("STRASSE") == normalize_title("straße")  # casefold
