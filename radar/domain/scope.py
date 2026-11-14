"""追蹤範圍的判定：`paper.in_scope` 只由這裡的 `is_in_scope` 算（AD-5）。"""

from dataclasses import dataclass

from radar.domain.identifiers import normalize_issn_l

# 追蹤範圍的起始年（含）。起點固定，不是「往回推 N 年」；沒有上限（PRD FR-8）。
TRACKING_START_YEAR = 2016


@dataclass(frozen=True)
class ScopeSettings:
    """追蹤範圍的設定：追蹤期刊的 ISSN-L（建立時正規化）與起始年。"""

    journals: frozenset[str]
    start_year: int = TRACKING_START_YEAR

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "journals", frozenset(normalize_issn_l(issn) for issn in self.journals)
        )


def is_in_scope(
    journal_issn_l: str | None, publication_year: int | None, settings: ScopeSettings
) -> bool:
    """期刊在設定的 ISSN-L 集合裡、而且出版年 ≥ 起始年（沒有上限）。任一個是 None 就是 False。

    Raises:
        InvalidIdentifierError: `journal_issn_l` 不是看得懂的 ISSN-L。
    """
    if journal_issn_l is None or publication_year is None:
        return False
    return (
        normalize_issn_l(journal_issn_l) in settings.journals
        and publication_year >= settings.start_year
    )
