"""文獻庫的 domain 與共用形狀（Story 3.16）：不連資料庫。

- `AddedVia` 剛好三種值；`LibraryItem` 的時間要帶時區；`LibraryEntry` 的論文列要是那一篇。
- 下一頁的位置（cursor）：編碼後解得回來（時間轉成 UTC）；不是它產生的寫法（亂碼、改過、補了 `=`、
  太長、沒有時區、轉成 UTC 超出年份範圍）一律 `ValueError`。
- 文獻庫寫入的鎖鍵：同一組（成員, 論文）同一個鍵，換成員或論文就不同，和固定名稱的鎖不撞。
- `LibraryPage` 一頁最多 `LIBRARY_PAGE_SIZE` 篇；`client-config` 給的是同一個數字。
- 論文列（`PaperRowFacts`、`PaperRow`）：作者最多 3 位、總人數不少於列出的；`doi_url` 由 `doi`
  產生。
- 稽核：管理者記 `admin`、其他成員記 `member`；不是成員身分丟錯。
- 可見條件：文獻庫與封存都是成員只看自己的（UUID 常值）、系統全部、訪客沒有；別名照樣檢查。
資料全是測試自己編的。
"""

import base64
import dataclasses
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from radar.domain.audit import AuditActorRole, member_audit_event
from radar.domain.contracts import ClientConfig, PaperRow, build_client_config
from radar.domain.fingerprints import LLM_BATCH_BUDGET_LOCK_KEY, named_lock_key
from radar.domain.library import (
    LIBRARY_PAGE_SIZE,
    AddedVia,
    DeletedSource,
    LibraryCursor,
    LibraryEntry,
    LibraryItem,
    LibraryPage,
    decode_library_cursor,
    encode_library_cursor,
    library_item_lock_key,
)
from radar.domain.paper_rows import PAPER_ROW_AUTHOR_LIMIT, PaperRowFacts
from radar.domain.viewer import MemberViewer, PublicViewer, SystemViewer
from radar.repo.visibility import visible_deleted_records, visible_library_items

OWNER_ID = uuid.UUID("018f0000-0000-7000-8000-0000000000a1")
PAPER_ID = uuid.UUID("018f0000-0000-7000-8000-0000000000c3")
ITEM_ID = uuid.UUID("018f0000-0000-7000-8000-0000000000d4")
AT = datetime(2026, 11, 18, 9, 30, 15, 123456, tzinfo=UTC)
MEMBER = MemberViewer(member_id=OWNER_ID, is_admin=False)
ADMIN = MemberViewer(member_id=OWNER_ID, is_admin=True)

ITEM = LibraryItem(
    id=ITEM_ID, owner_id=OWNER_ID, paper_id=PAPER_ID, added_via=AddedVia.MANUAL, added_at=AT
)
FACTS = PaperRowFacts(
    paper_id=PAPER_ID,
    title="A study of team platforms",
    authors=("Chen, A.", "Lin, B.", "Wang, C."),
    author_count=5,
    publication_year=2025,
    journal_issn_l="0276-7783",
    doi="10.1/abc def",
    has_abstract=True,
    in_scope=True,
    is_retracted=False,
)


# --- 加入方式、一筆、一頁 -------------------------------------------------------------------------


def test_added_via_has_exactly_three_values() -> None:
    assert {value.value for value in AddedVia} == {"upload", "shared_list", "manual"}
    assert {value.value for value in DeletedSource} == {"library_item"}


def test_library_item_needs_an_aware_time_and_a_known_added_via() -> None:
    with pytest.raises(ValueError, match="時區"):
        dataclasses.replace(ITEM, added_at=datetime(2026, 11, 18, 9, 30))
    with pytest.raises(ValueError, match="bookmark"):
        dataclasses.replace(ITEM, added_via="bookmark")  # type: ignore[arg-type]
    assert dataclasses.replace(ITEM, added_via="upload").added_via is AddedVia.UPLOAD  # type: ignore[arg-type]


def test_entry_paper_must_be_the_item_paper() -> None:
    assert LibraryEntry(item=ITEM, paper=FACTS).paper is FACTS
    with pytest.raises(ValueError, match="那篇論文"):
        LibraryEntry(item=ITEM, paper=dataclasses.replace(FACTS, paper_id=uuid.uuid4()))


def test_page_holds_at_most_one_page() -> None:
    entry = LibraryEntry(item=ITEM, paper=FACTS)
    assert LIBRARY_PAGE_SIZE == 50
    LibraryPage(entries=(entry,) * LIBRARY_PAGE_SIZE, next_cursor="x", total=51)
    with pytest.raises(ValueError, match="一頁最多"):
        LibraryPage(entries=(entry,) * (LIBRARY_PAGE_SIZE + 1), next_cursor=None, total=51)
    with pytest.raises(ValueError, match="total"):
        LibraryPage(entries=(), next_cursor=None, total=-1)


def test_client_config_gives_the_library_page_size() -> None:
    assert build_client_config() == ClientConfig(timezone="Asia/Taipei", library_page_size=50)
    with pytest.raises(ValidationError):
        ClientConfig(timezone="Asia/Taipei", library_page_size=0)


# --- 下一頁的位置 --------------------------------------------------------------------------------


def test_cursor_round_trips_in_utc() -> None:
    taipei = AT.astimezone(timezone(timedelta(hours=8)))
    encoded = encode_library_cursor(LibraryCursor(added_at=taipei, item_id=ITEM_ID))

    decoded = decode_library_cursor(encoded)

    assert decoded == LibraryCursor(added_at=AT, item_id=ITEM_ID)
    assert decoded.added_at.utcoffset() == timedelta(0)
    assert "=" not in encoded
    assert LibraryCursor.after(ITEM) == LibraryCursor(added_at=AT, item_id=ITEM_ID)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


GOOD = encode_library_cursor(LibraryCursor(added_at=AT, item_id=ITEM_ID))


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not a cursor",
        "!!!!",
        GOOD + "=",
        GOOD + "A",
        GOOD[:-1],
        "A" * 201,
        _b64(f"{AT.isoformat()}"),
        _b64(f"{AT.isoformat()},{ITEM_ID},extra"),
        _b64(f"2026-11-18T09:30:15,{ITEM_ID}"),
        _b64(f"{AT.isoformat()},not-a-uuid"),
        _b64(f"{AT.isoformat()},{str(ITEM_ID).upper()}"),
        _b64(f"{AT.astimezone(timezone(timedelta(hours=8))).isoformat()},{ITEM_ID}"),
        base64.urlsafe_b64encode(b"\xff\xfe,").decode(),
    ],
)
def test_cursor_rejects_anything_it_did_not_write(value: str) -> None:
    with pytest.raises(ValueError, match="下一頁的位置"):
        decode_library_cursor(value)


@pytest.mark.parametrize(
    "added_at",
    ["0001-01-01T00:00:00.000000+14:00", "9999-12-31T23:59:59.999999-14:00"],
)
def test_cursor_out_of_range_in_utc_is_a_value_error(added_at: str) -> None:
    # 解得出時間與 ID，但轉成 UTC 會超出年份範圍（OverflowError）：一樣當格式錯，api 回 422。
    with pytest.raises(ValueError, match="下一頁的位置"):
        decode_library_cursor(_b64(f"{added_at},{ITEM_ID}"))


def test_cursor_needs_an_aware_time() -> None:
    with pytest.raises(ValueError, match="時區"):
        LibraryCursor(added_at=datetime(2026, 11, 18), item_id=ITEM_ID)


# --- 寫入的鎖鍵 -----------------------------------------------------------------------------------


def test_lock_key_is_per_member_and_paper() -> None:
    key = library_item_lock_key(OWNER_ID, PAPER_ID)

    assert key == library_item_lock_key(OWNER_ID, PAPER_ID)
    assert -(2**63) <= key < 2**63
    assert key != library_item_lock_key(ITEM_ID, PAPER_ID)
    assert key != library_item_lock_key(OWNER_ID, ITEM_ID)
    # 有自己的命名空間：不是沒有前綴的同一串名稱，也不是既有的固定鍵。
    assert key != named_lock_key(f"{OWNER_ID}:{PAPER_ID}")
    assert key != LLM_BATCH_BUDGET_LOCK_KEY


# --- 論文列 ------------------------------------------------------------------------------------


def test_paper_row_facts_checks_authors() -> None:
    with pytest.raises(ValueError, match="最多"):
        dataclasses.replace(FACTS, authors=("a", "b", "c", "d"), author_count=4)
    with pytest.raises(ValueError, match="author_count"):
        dataclasses.replace(FACTS, author_count=2)
    with pytest.raises(ValueError, match="tuple"):
        dataclasses.replace(FACTS, authors=["a"])  # type: ignore[arg-type]
    assert PAPER_ROW_AUTHOR_LIMIT == 3


def test_paper_row_from_facts() -> None:
    row = PaperRow.from_facts(FACTS, "misq")

    assert row.model_dump(mode="json") == {
        "id": str(PAPER_ID),
        "title": "A study of team platforms",
        "authors": ["Chen, A.", "Lin, B.", "Wang, C."],
        "author_count": 5,
        "publication_year": 2025,
        "journal": "misq",
        "journal_issn_l": "0276-7783",
        "doi": "10.1/abc def",
        "doi_url": "https://doi.org/10.1/abc%20def",
        "has_abstract": True,
        "in_scope": True,
        "is_retracted": False,
    }
    assert PaperRow.model_config.get("extra") == "forbid"


def test_paper_row_without_doi_has_no_link() -> None:
    row = PaperRow.from_facts(
        dataclasses.replace(FACTS, doi=None, authors=(), author_count=0), None
    )

    assert (row.doi, row.doi_url, row.journal, row.authors) == (None, None, None, ())


@pytest.mark.parametrize(
    "changes",
    [
        {"doi_url": "https://example.com/elsewhere"},
        {"doi": None},
        {"author_count": 2},
        {"authors": ["a", "b", "c", "d"], "author_count": 4},
        {"has_abstract": "yes"},
        {"unexpected": 1},
    ],
)
def test_paper_row_rejects_inconsistent_values(changes: dict[str, object]) -> None:
    data = PaperRow.from_facts(FACTS, "misq").model_dump(mode="json") | changes
    with pytest.raises(ValidationError):
        PaperRow.model_validate(data)


# --- 稽核 --------------------------------------------------------------------------------------


def test_member_audit_event_records_member_or_admin() -> None:
    member = member_audit_event(
        MEMBER, action="add_library_item", target_type="library_item", target_id=ITEM_ID
    )
    admin = member_audit_event(
        ADMIN,
        action="add_library_item",
        target_type="library_item",
        target_id=ITEM_ID,
        detail={"paper_id": str(PAPER_ID)},
    )

    assert (member.actor_role, member.actor_member_id) == (AuditActorRole.MEMBER, OWNER_ID)
    assert (admin.actor_role, admin.actor_member_id) == (AuditActorRole.ADMIN, OWNER_ID)
    assert dict(member.detail) == {}
    assert dict(admin.detail) == {"paper_id": str(PAPER_ID)}


@pytest.mark.parametrize("actor", [SystemViewer(), PublicViewer()])
def test_member_audit_event_needs_a_member(actor: object) -> None:
    with pytest.raises(TypeError):
        member_audit_event(actor, action="x", target_type="y", target_id=None)  # type: ignore[arg-type]


# --- 可見條件 ----------------------------------------------------------------------------------


@pytest.mark.parametrize("visible", [visible_library_items, visible_deleted_records])
def test_members_see_only_their_own_rows(visible: Callable[..., str]) -> None:
    condition = f"\"t\".owner_id = '{OWNER_ID}'::uuid"
    assert visible(MEMBER, alias="t") == condition
    # 管理者也一樣：文獻庫是私人的。
    assert visible(ADMIN, alias="t") == condition
    assert visible(SystemViewer(), alias="t") == "TRUE"
    assert visible(PublicViewer(), alias="t") == "FALSE"
    with pytest.raises(ValueError, match="別名"):
        visible(MEMBER, alias="t; DROP TABLE library_item")
    with pytest.raises(TypeError):
        visible(object())


def test_default_aliases_are_the_table_names() -> None:
    assert visible_library_items(MEMBER).startswith('"library_item".owner_id')
    assert visible_deleted_records(MEMBER).startswith('"deleted_record".owner_id')
