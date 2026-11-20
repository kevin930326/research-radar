"""「這篇在不在我的文獻庫」的 SQL 運算式（Story 3.17、ADR-0053）：
`visibility.visible_in_own_library`。

- 成員：只看 `owner_id` 是自己的那一筆（管理者也一樣）；成員 ID 重新組成標準寫法的 UUID 常值。
- 系統、訪客：一律 `FALSE`（不能沿用 `visible_library_items` 的「系統看得到全部」，否則
  「任何人加過」會變成「在我的文獻庫」）。
- 別名：只收小寫識別字，不能和子查詢的別名相同。
- 欄位預設假：`PaperRowFacts`、`PaperPageFacts` 沒有算的地方不會說成「在」；回應模型一定要給、
  只收布林。
- 算出來是真的時候，文獻庫一筆與論文頁的回應照樣寫真（審查 F20：既有單元測試的假資料沒有給，
  只測到預設的假）。
"""

import dataclasses
import uuid

import pytest
from pydantic import ValidationError

from radar.domain.contracts import PaperRow
from radar.domain.paper_rows import PaperRowFacts
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer
from radar.entrypoints.api.library import library_item_response
from radar.entrypoints.api.papers import paper_page_response
from radar.repo.visibility import visible_in_own_library
from tests.unit.test_api_library import ENTRY
from tests.unit.test_api_papers import PAGE

OWNER_ID = uuid.UUID("018f0000-0000-7000-8000-0000000000a1")
FACTS = PaperRowFacts(
    paper_id=uuid.UUID("018f0000-0000-7000-8000-0000000000c3"),
    title="A study of team platforms",
    authors=("Chen, A.",),
    author_count=1,
    publication_year=2025,
    journal_issn_l="0276-7783",
    doi=None,
    has_abstract=True,
    in_scope=True,
    is_retracted=False,
)


@pytest.mark.parametrize("is_admin", [False, True])
def test_a_member_only_counts_their_own_library(is_admin: bool) -> None:
    viewer = MemberViewer(member_id=OWNER_ID, is_admin=is_admin)

    assert visible_in_own_library(viewer, alias="p") == (
        "EXISTS (SELECT 1 FROM library_item AS visible_library"
        ' WHERE visible_library.paper_id = "p".id'
        f" AND visible_library.owner_id = '{OWNER_ID}'::uuid)"
    )


def test_system_and_public_have_no_library_of_their_own() -> None:
    assert visible_in_own_library(SystemViewer()) == "FALSE"
    assert visible_in_own_library(PublicViewer()) == "FALSE"


def test_default_alias_is_the_paper_table() -> None:
    assert '"paper".id' in visible_in_own_library(MemberViewer(member_id=OWNER_ID, is_admin=False))


@pytest.mark.parametrize("alias", ["p; DROP TABLE paper", "Paper", "visible_library"])
def test_rejects_bad_aliases(alias: str) -> None:
    with pytest.raises(ValueError, match="別名"):
        visible_in_own_library(MemberViewer(member_id=OWNER_ID, is_admin=False), alias=alias)


def test_rejects_unknown_viewers() -> None:
    with pytest.raises(TypeError):
        visible_in_own_library(object())  # type: ignore[arg-type]


def test_facts_default_to_not_in_the_library_and_the_row_carries_it() -> None:
    assert FACTS.in_my_library is False
    assert PaperRow.from_facts(FACTS, "misq").in_my_library is False
    mine = dataclasses.replace(FACTS, in_my_library=True)
    assert PaperRow.from_facts(mine, "misq").in_my_library is True


@pytest.mark.parametrize("value", ["true", 1, None])
def test_the_row_only_takes_a_boolean(value: object) -> None:
    data = PaperRow.from_facts(FACTS, "misq").model_dump(mode="json") | {"in_my_library": value}
    with pytest.raises(ValidationError):
        PaperRow.model_validate(data)


def test_the_row_must_say_it() -> None:
    data = PaperRow.from_facts(FACTS, "misq").model_dump(mode="json")
    del data["in_my_library"]
    with pytest.raises(ValidationError):
        PaperRow.model_validate(data)


def test_true_facts_come_out_true_in_the_library_item_and_paper_page_responses() -> None:
    entry = dataclasses.replace(ENTRY, paper=dataclasses.replace(ENTRY.paper, in_my_library=True))
    page = dataclasses.replace(PAGE, facts=dataclasses.replace(PAGE.facts, in_my_library=True))

    item = library_item_response(entry, lambda _issn: "misq").model_dump(mode="json")
    paper_page = paper_page_response(page).model_dump(mode="json")

    assert item["paper"]["in_my_library"] is True
    assert paper_page["in_my_library"] is True
    # 對照：同樣的資料，facts 是假時回應是假（不是固定寫真）。
    assert paper_page_response(PAGE).model_dump(mode="json")["in_my_library"] is False
