"""`is_in_scope` 的邊界（AD-5、PRD FR-8：2016 年起、含當年度、沒有上限）。"""

import pytest

from radar.domain.identifiers import InvalidIdentifierError
from radar.domain.scope import TRACKING_START_YEAR, ScopeSettings, is_in_scope

SETTINGS = ScopeSettings(frozenset({"0276-7783", "1047-7047"}))


def test_start_year_is_2016() -> None:
    assert TRACKING_START_YEAR == 2016
    assert SETTINGS.start_year == 2016


@pytest.mark.parametrize(
    ("journal", "year", "expected"),
    [
        ("0276-7783", 2016, True),
        ("0276-7783", 2015, False),
        ("1047-7047", 2025, True),
        # 出版年標成很後面的年份也算（期刊年底先出隔年那一期；沒有上限）。
        ("0276-7783", 2099, True),
        ("1234-5678", 2020, False),
        (None, 2020, False),
        ("0276-7783", None, False),
        (None, None, False),
        # 沒有連字號的寫法一樣認得。
        ("02767783", 2020, True),
    ],
)
def test_is_in_scope(journal: str | None, year: int | None, expected: bool) -> None:
    assert is_in_scope(journal, year, SETTINGS) is expected


def test_settings_normalize_journals() -> None:
    settings = ScopeSettings(frozenset({"ISSN 0276-778x", "10477047"}))

    assert settings.journals == frozenset({"0276-778X", "1047-7047"})


def test_settings_reject_unreadable_journal() -> None:
    with pytest.raises(InvalidIdentifierError):
        ScopeSettings(frozenset({"not an issn"}))


def test_custom_start_year() -> None:
    settings = ScopeSettings(frozenset({"0276-7783"}), start_year=2020)

    assert is_in_scope("0276-7783", 2019, settings) is False
    assert is_in_scope("0276-7783", 2020, settings) is True
