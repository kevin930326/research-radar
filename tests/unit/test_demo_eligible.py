"""示範站可用的判定（Story 3.7a、ADR-0052）：`openalex_license`、`is_demo_eligible`、
`config/demo_publishers.yaml`、解析出的授權與出版社、新欄位不進 `source_hash`。
"""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from radar.config import ConfigError, load_domain_config
from radar.domain.domain_config import DemoPublishersConfig
from radar.domain.identifiers import InvalidIdentifierError, normalize_openalex_id
from radar.domain.papers import ParsedPaper
from radar.domain.parsers.openalex import parse_openalex_work
from radar.domain.policy import (
    DEMO_ELIGIBLE_LICENSES,
    ContentLicense,
    PublisherExclusions,
    is_demo_eligible,
    normalize_publisher_name,
    openalex_license,
)
from tests.demo_works import (
    EXCLUDED_PARENT_NAME,
    EXCLUDED_PUBLISHER_ID,
    EXCLUDED_PUBLISHER_NAME,
    OPEN_PUBLISHER_ID,
    OPEN_PUBLISHER_NAME,
    TEST_EXCLUSIONS,
    demo_work,
    demo_work_bytes,
)

ROOT = Path(__file__).resolve().parents[2]
FETCHED_AT = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
ISSN = "1471-7727"


def _parse(**kwargs: object) -> ParsedPaper:
    return parse_openalex_work(
        demo_work_bytes(demo_work(1, issn_l=ISSN, **kwargs)),  # type: ignore[arg-type]
        fetched_at=FETCHED_AT,
    )


def _eligible(paper: ParsedPaper, excluded: PublisherExclusions | None = TEST_EXCLUSIONS) -> bool:
    return is_demo_eligible(
        paper.primary_license, paper.publisher_ids, paper.publisher_names, excluded
    )


# --- openalex_license ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("cc-by", ContentLicense.CC_BY),
        ("cc-by-sa", ContentLicense.CC_BY_SA),
        ("cc0", ContentLicense.CC0),
        ("public-domain", ContentLicense.CC0),
        ("cc-by-nc", ContentLicense.CC_BY_NC),
        ("cc-by-nd", ContentLicense.CC_BY_ND),
        ("cc-by-nc-sa", ContentLicense.CC_BY_NC_SA),
        ("cc-by-nc-nd", ContentLicense.CC_BY_NC_ND),
        ("other-oa", ContentLicense.OTHER_OA),
        ("publisher-specific-oa", ContentLicense.UNKNOWN),
        ("mit", ContentLicense.UNKNOWN),
        ("CC-BY", ContentLicense.UNKNOWN),
        ("paywalled", ContentLicense.UNKNOWN),
        ("unknown", ContentLicense.UNKNOWN),
        ("weird", ContentLicense.UNKNOWN),
        (None, ContentLicense.UNKNOWN),
    ],
)
def test_openalex_license_mapping(value: str | None, expected: ContentLicense) -> None:
    assert openalex_license(value) is expected


# --- is_demo_eligible -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("license", "expected"),
    [
        ("cc-by", True),
        ("cc-by-sa", True),
        ("public-domain", True),
        ("cc-by-nc", False),
        ("cc-by-nd", False),
        ("cc-by-nc-nd", False),
        ("other-oa", False),
        ("weird", False),
        (None, False),
    ],
)
def test_only_three_licenses_are_eligible(license: str | None, expected: bool) -> None:
    assert is_demo_eligible(license, ("P1",), ("Open",), TEST_EXCLUSIONS) is expected


def test_no_publisher_fields_is_not_eligible() -> None:
    assert is_demo_eligible("cc-by", (), (), TEST_EXCLUSIONS) is False


@pytest.mark.parametrize(
    ("ids", "names"),
    [
        # 只有看不懂的 ID：排除清單目前只靠名稱，比不到不代表不是被排除的出版社。
        (("not an id!",), ()),
        # 只有看得懂的 ID、不在排除清單裡：一樣沒有名稱可以比。
        (("P9000000001",), ()),
        (("https://openalex.org/P9000000001", "P9000000077"), ()),
        # 名稱全部壞掉（空字串、只有空白、全形空白、正規化後是空的）。
        (("P9000000001",), ("",)),
        (("P9000000001",), ("   ", "\t\n")),
        ((), ("\u3000", " \u00a0 ")),
    ],
)
def test_publisher_without_a_usable_name_is_not_eligible(
    ids: tuple[str, ...], names: tuple[str, ...]
) -> None:
    # ADR-0052 第 4 點：至少要有一個正規化後不是空的出版社名稱，才可能是 True。
    assert is_demo_eligible("cc-by", ids, names, TEST_EXCLUSIONS) is False


def test_one_usable_name_among_broken_ones_is_enough() -> None:
    assert is_demo_eligible("cc-by", ("not an id!",), ("  ", "Open"), TEST_EXCLUSIONS) is True


# 看不見的字元（Unicode 類別 Cf、Cc）：零寬空白、BOM、軟連字號、零寬連接符、控制字元。
_INVISIBLE = ["\u200b", "\ufeff", "\u00ad", "\u200d", "\u2060", "\x00", "\x7f"]


@pytest.mark.parametrize(
    "names",
    [
        ("\u200b",),
        ("\ufeff",),
        ("\u00ad",),
        ("\u200b\ufeff\u00ad",),
        (" \u200b ", "\u2060\x7f"),
    ],
)
def test_name_of_only_invisible_characters_is_not_eligible(names: tuple[str, ...]) -> None:
    # ADR-0052 第 4、5 點：只有看不見的字元的名稱正規化後是空的，不算有名稱。
    assert all(normalize_publisher_name(name) == "" for name in names)
    assert is_demo_eligible("cc-by", (), names, TEST_EXCLUSIONS) is False
    assert is_demo_eligible("cc-by", ("P9000000001",), names, TEST_EXCLUSIONS) is False


@pytest.mark.parametrize("mark", _INVISIBLE)
def test_excluded_name_with_invisible_characters_still_matches(mark: str) -> None:
    for name in (
        f"{EXCLUDED_PUBLISHER_NAME}{mark}",
        f"{mark}{EXCLUDED_PUBLISHER_NAME}",
        EXCLUDED_PUBLISHER_NAME.replace(" ", f" {mark}", 1),
    ):
        assert normalize_publisher_name(name) == normalize_publisher_name(EXCLUDED_PUBLISHER_NAME)
        assert is_demo_eligible("cc-by", (), ("Open", name), TEST_EXCLUSIONS) is False


def test_separators_between_words_are_ignored_so_glued_words_still_match() -> None:
    # PR #108 第 3 輪：比對鍵只留字母與數字，字與字之間用什麼隔開（或黏在一起）都得到同一個鍵。
    key = normalize_publisher_name("IEEE Computer Society")
    assert key == "ieeecomputersociety"
    assert normalize_publisher_name("IEEE\nComputer\tSociety") == key
    assert normalize_publisher_name("IEEE\u200bComputer\u200bSociety") == key
    assert normalize_publisher_name("IEEE-Computer Society") == key
    assert normalize_publisher_name("IEEEComputerSociety") == key
    assert normalize_publisher_name("IEEE\u200b") == "ieee"
    assert normalize_publisher_name("I.E.E.E.") == "ieee"


# 只有這些字元的名稱正規化後是空的：韓文填充字、異體字選擇符、組字連接符、點字空白、私用區、
# 標點與符號。
_NO_LETTERS_OR_DIGITS = [
    "\u3164",
    "\uffa0",
    "\u115f",
    "\u1160",
    "\ufe0f",
    "\ufe00",
    "\U000e0100",
    "\u034f",
    "\u2800",
    "\ue000",
    "\U000f0000",
    "\u17b4",
    "\u180b",
    "...",
    "- / &",
    "\u00a9\u00ae",
    "\u3164\u2800\ufe0f\u034f",
]


@pytest.mark.parametrize("name", _NO_LETTERS_OR_DIGITS)
def test_name_without_letters_or_digits_is_not_eligible(name: str) -> None:
    assert normalize_publisher_name(name) == ""
    assert is_demo_eligible("cc-by", (), (name,), TEST_EXCLUSIONS) is False
    assert is_demo_eligible("cc-by", ("P9000000001",), (name, " "), TEST_EXCLUSIONS) is False


# 夾在排除名稱前後或字中間也不影響比對的字元（字母與數字以外的都拿掉）。
_IGNORED_MARKS = [
    "\u3164",
    "\uffa0",
    "\u115f",
    "\u1160",
    "\ufe0f",
    "\U000e0100",
    "\u034f",
    "\u2800",
    "\ue000",
    "\u180e",
    "\x01",
    ".",
    "-",
]


@pytest.mark.parametrize("mark", _IGNORED_MARKS)
def test_excluded_name_with_ignored_characters_still_matches(mark: str) -> None:
    key = normalize_publisher_name(EXCLUDED_PUBLISHER_NAME)
    for name in (
        f"{EXCLUDED_PUBLISHER_NAME}{mark}",
        f"{mark}{EXCLUDED_PUBLISHER_NAME}",
        EXCLUDED_PUBLISHER_NAME.replace(" ", mark),
        EXCLUDED_PUBLISHER_NAME.replace("E", f"E{mark}", 1),
    ):
        assert normalize_publisher_name(name) == key
        assert is_demo_eligible("cc-by", (), ("Open", name), TEST_EXCLUSIONS) is False


def test_missing_exclusion_list_is_not_eligible() -> None:
    assert is_demo_eligible("cc-by", ("P1",), ("Open",), None) is False


@pytest.mark.parametrize(
    ("ids", "names"),
    [
        (("P9000000002",), ()),
        (("https://openalex.org/p9000000002",), ()),
        ((), ("Excluded Engineering Society",)),
        ((), ("  excluded   ENGINEERING society ",)),
        (("P1",), ("Open", EXCLUDED_PARENT_NAME)),
    ],
)
def test_any_excluded_id_or_name_excludes(ids: tuple[str, ...], names: tuple[str, ...]) -> None:
    assert is_demo_eligible("cc-by", ids, names, TEST_EXCLUSIONS) is False


# 判定為 True 時授權只可能是這幾個字面值（寫死，不拿實作的 `openalex_license` 當答案）。
# `cc0` 不是 OpenAlex 會給的值（官方清單是 `public-domain`），但 `openalex_license` 認得它是 CC0。
_ALLOWED_LICENSE_STRINGS = frozenset({"cc-by", "cc-by-sa", "public-domain", "cc0"})
# 和允許值很像、但一律不算的寫法：大小寫、版本號、NC／ND、前後空白、底線。
_NEAR_LICENSES = [
    "cc-by-4.0",
    "CC-BY",
    "Cc-By",
    "CC0",
    "cc-by-nc",
    "cc-by-nd",
    "cc-by-nc-sa",
    "cc-by-nc-nd",
    " cc-by",
    "cc-by ",
    " cc-by-sa ",
    "cc_by",
    "public domain",
    "public-domain ",
    "other-oa",
    "publisher-specific-oa",
]
_LICENSES = st.one_of(
    st.none(),
    st.sampled_from(sorted(_ALLOWED_LICENSE_STRINGS)),
    st.sampled_from(_NEAR_LICENSES),
    st.text(max_size=20),
)
_IDS = st.lists(
    st.one_of(st.sampled_from(["P1", "P9000000002", "W5"]), st.text(min_size=1, max_size=12)),
    max_size=4,
).map(tuple)
_NAMES = st.lists(
    st.one_of(
        st.sampled_from(["Open", EXCLUDED_PUBLISHER_NAME, EXCLUDED_PARENT_NAME.upper()]),
        st.text(min_size=1, max_size=20),
    ),
    max_size=4,
).map(tuple)


@given(license=_LICENSES, ids=_IDS, names=_NAMES, excluded_id=st.booleans())
def test_property_excluded_publisher_is_never_eligible(
    license: str | None, ids: tuple[str, ...], names: tuple[str, ...], excluded_id: bool
) -> None:
    # 不管授權與其他出版社值怎麼組合：只要摻進一個排除清單裡的 ID 或名稱，一定是 False。
    if excluded_id:
        ids = (*ids, "P9000000002")
    else:
        names = (*names, EXCLUDED_PUBLISHER_NAME)
    assert is_demo_eligible(license, ids, names, TEST_EXCLUSIONS) is False


# 排除清單裡的值的各種寫法：URL、小寫、大小寫與空白不同的名稱、上層出版社名稱。
_EXCLUDED_ID_FORMS = [
    "P9000000002",
    "p9000000002",
    EXCLUDED_PUBLISHER_ID,
    "https://openalex.org/p9000000002",
]
_EXCLUDED_NAME_FORMS = [
    EXCLUDED_PUBLISHER_NAME,
    EXCLUDED_PUBLISHER_NAME.upper(),
    f"  {EXCLUDED_PUBLISHER_NAME.lower()}  ",
    EXCLUDED_PUBLISHER_NAME.replace(" ", "   "),
    EXCLUDED_PARENT_NAME,
    EXCLUDED_PARENT_NAME.swapcase(),
    # 夾雜看不見的字元（零寬空白、BOM、軟連字號、控制字元）。
    f"{EXCLUDED_PUBLISHER_NAME}\u200b",
    f"\ufeff{EXCLUDED_PUBLISHER_NAME}",
    EXCLUDED_PUBLISHER_NAME.replace(" ", " \u00ad"),
    f"\u200d{EXCLUDED_PARENT_NAME.upper()}\x00",
    # 字母與數字以外的都不影響：異體字選擇符、韓文填充字、點字空白、私用區、標點、黏在一起。
    f"{EXCLUDED_PUBLISHER_NAME}\ufe0f",
    f"\u3164{EXCLUDED_PUBLISHER_NAME}\u2800",
    EXCLUDED_PUBLISHER_NAME.replace(" ", "\u200b"),
    EXCLUDED_PUBLISHER_NAME.replace(" ", ""),
    EXCLUDED_PUBLISHER_NAME.replace(" ", "-"),
    ".".join(EXCLUDED_PARENT_NAME.replace(" ", "")),
    f"\ue000{EXCLUDED_PARENT_NAME}\u034f",
]


@given(
    license=_LICENSES,
    ids=_IDS,
    names=_NAMES,
    excluded_id=st.one_of(st.none(), st.sampled_from(_EXCLUDED_ID_FORMS)),
    excluded_name=st.one_of(st.none(), st.sampled_from(_EXCLUDED_NAME_FORMS)),
    id_position=st.integers(min_value=0, max_value=4),
    name_position=st.integers(min_value=0, max_value=4),
)
def test_property_excluded_value_in_any_form_and_position_is_never_eligible(
    license: str | None,
    ids: tuple[str, ...],
    names: tuple[str, ...],
    excluded_id: str | None,
    excluded_name: str | None,
    id_position: int,
    name_position: int,
) -> None:
    # 排除值用非標準寫法、放在直屬（第一個）或 lineage 的任何位置，一樣一定是 False。
    if excluded_id is None and excluded_name is None:
        excluded_name = EXCLUDED_PUBLISHER_NAME
    if excluded_id is not None:
        ids = (*ids[:id_position], excluded_id, *ids[id_position:])
    if excluded_name is not None:
        names = (*names[:name_position], excluded_name, *names[name_position:])
    assert is_demo_eligible(license, ids, names, TEST_EXCLUSIONS) is False


@given(license=_LICENSES, ids=_IDS)
def test_property_ids_without_names_are_never_eligible(
    license: str | None, ids: tuple[str, ...]
) -> None:
    # 只有出版社 ID、沒有名稱：不管 ID 看不看得懂、在不在排除清單，一定是 False
    # （ADR-0052 第 4 點）。
    assert is_demo_eligible(license, ids, (), TEST_EXCLUSIONS) is False


@given(license=_LICENSES, ids=_IDS, names=_NAMES)
def test_property_license_outside_the_three_is_never_eligible(
    license: str | None, ids: tuple[str, ...], names: tuple[str, ...]
) -> None:
    result = is_demo_eligible(license, ids, names, TEST_EXCLUSIONS)
    if license not in _ALLOWED_LICENSE_STRINGS:
        assert result is False


@given(license=_LICENSES, ids=_IDS, names=_NAMES)
def test_property_true_needs_allowed_license_and_publisher_not_excluded(
    license: str | None, ids: tuple[str, ...], names: tuple[str, ...]
) -> None:
    # True 的必要條件：三種授權之一（寫死的字面值）、有出版社欄位、至少一個不是空白的名稱、
    # 沒有任何值在排除清單裡；而且拿掉排除清單（None）一律 False。
    if is_demo_eligible(license, ids, names, TEST_EXCLUSIONS):
        assert license in _ALLOWED_LICENSE_STRINGS
        assert openalex_license(license) in DEMO_ELIGIBLE_LICENSES
        assert ids or names
        assert any(name.strip() for name in names)
        assert any(normalize_publisher_name(name) for name in names)
        assert not TEST_EXCLUSIONS.matches(ids, names)
    assert is_demo_eligible(license, ids, names, None) is False


# --- 解析 ------------------------------------------------------------------------------------


def test_parser_reads_primary_license_and_publisher() -> None:
    paper = _parse(lineage=[OPEN_PUBLISHER_ID, "https://openalex.org/P9000000077"])

    assert paper.primary_license == "cc-by"
    assert paper.publisher_ids == ("P9000000001", "P9000000077")
    assert paper.publisher_names == (OPEN_PUBLISHER_NAME,)
    assert _eligible(paper) is True


def test_parser_ignores_best_oa_and_other_locations() -> None:
    paper = _parse(license=None, best_oa_license="cc-by", other_location_licenses=("cc-by",))

    assert paper.primary_license is None
    assert _eligible(paper) is False


def test_parser_keeps_unknown_license_strings() -> None:
    paper = _parse(license="  publisher-specific-oa ")

    assert paper.primary_license == "publisher-specific-oa"
    assert _eligible(paper) is False


@pytest.mark.parametrize("bad", [5, True, ["cc-by"], {"x": 1}, "", "   ", "a\x00b"])
def test_parser_is_lenient_with_license_shapes(bad: object) -> None:
    assert _parse(license=bad).primary_license is None


def test_parser_is_lenient_with_publisher_shapes() -> None:
    paper = _parse(publisher_id=5, publisher_name="  ", lineage="P1", lineage_names=[3, "Group"])

    assert paper.publisher_ids == ()
    assert paper.publisher_names == ("Group",)


def test_parser_keeps_unreadable_publisher_ids_as_text() -> None:
    assert _parse(publisher_id="not an id!", lineage=[]).publisher_ids == ("not an id!",)


def test_parser_without_primary_location_has_no_license_or_publisher() -> None:
    paper = _parse(primary=False, best_oa_license="cc-by")

    assert paper.primary_license is None
    assert (paper.publisher_ids, paper.publisher_names) == ((), ())
    assert _eligible(paper) is False


def test_parser_valid_publisher_id_without_name_is_not_eligible() -> None:
    paper = _parse(publisher_name=None, lineage_names=[])

    assert paper.publisher_ids == ("P9000000001",)
    assert paper.publisher_names == ()
    assert _eligible(paper) is False


@pytest.mark.parametrize(
    "invisible", ["\u200b", "\ufeff", "\u00ad", " \u200b\u00ad ", "\u3164", "\ufe0f\u2800", "..."]
)
def test_parser_name_of_only_invisible_characters_is_not_eligible(invisible: str) -> None:
    # 解析器照存（去頭尾空白後不是空的），判定時正規化成空字串，一樣不可用。
    paper = _parse(publisher_id=None, publisher_name=invisible, lineage_names=[invisible])

    assert paper.primary_license == "cc-by"
    assert paper.publisher_ids == ()
    assert _eligible(paper) is False


def test_parser_without_source_has_no_publisher() -> None:
    paper = _parse(source=False)

    assert (paper.publisher_ids, paper.publisher_names) == ((), ())
    assert _eligible(paper) is False


def test_parser_excluded_publisher_via_lineage_name() -> None:
    paper = _parse(
        publisher_id="https://openalex.org/P9000000055",
        publisher_name="Some Imprint",
        lineage_names=["Some Imprint", EXCLUDED_PARENT_NAME],
    )

    assert _eligible(paper) is False


def test_parser_excluded_publisher_by_id_or_name() -> None:
    assert _eligible(_parse(publisher_id=EXCLUDED_PUBLISHER_ID, publisher_name="X")) is False
    assert _eligible(_parse(publisher_id=None, publisher_name=EXCLUDED_PUBLISHER_NAME)) is False


def test_license_and_publisher_are_not_in_source_hash() -> None:
    base = _parse()
    changed = _parse(license="cc-by-nc", publisher_id=EXCLUDED_PUBLISHER_ID, publisher_name="Other")

    assert (base.primary_license, base.publisher_ids) != (
        changed.primary_license,
        changed.publisher_ids,
    )
    assert base.source_hash == changed.source_hash


def test_new_fields_default_to_nothing() -> None:
    paper = ParsedPaper(
        source="openalex",
        fetched_at=FETCHED_AT,
        doi="10.1/a",
        openalex_id=None,
        title="t",
        journal_issn_l=None,
        publication_year=None,
    )

    assert (paper.primary_license, paper.publisher_ids, paper.publisher_names) == (None, (), ())


def test_new_fields_survive_the_staging_json_roundtrip() -> None:
    paper = _parse()

    assert ParsedPaper.model_validate_json(paper.model_dump_json()) == paper


# --- 設定檔 ----------------------------------------------------------------------------------


def test_real_config_excludes_ieee_names() -> None:
    config = load_domain_config({"RADAR_CONFIG_DIR": str(ROOT / "config")})
    exclusions = config.demo_publishers.exclusions()

    assert normalize_publisher_name("Institute of Electrical and Electronics Engineers") in (
        exclusions.names
    )
    assert normalize_publisher_name("IEEE") in exclusions.names
    assert is_demo_eligible("cc-by", (), ("IEEE\u200b",), exclusions) is False
    assert is_demo_eligible("cc-by", (), ("\ufeffI\u00adEEE",), exclusions) is False
    for name in (
        "IEEE\ufe0f",
        "IEEE\u3164",
        "IEEE\u2800",
        "IEEE\ue000",
        "I.E.E.E.",
        "IEEE\u200bComputer\u200bSociety",
        "IEEE-Computer Society",
        "Institute\u2060of Electrical and Electronics Engineers",
        "IEEE\x01Computer Society",
        "IEEE\u180eComputer Society",
    ):
        assert is_demo_eligible("cc-by", (), (name,), exclusions) is False, name
    assert config.scope_settings().demo_publishers == exclusions
    assert config.journals.scope_settings().demo_publishers is None
    assert (
        is_demo_eligible(
            "cc-by", (), ("Institute of Electrical and Electronics Engineers",), exclusions
        )
        is False
    )


@pytest.mark.parametrize(
    "data",
    [
        {"excluded": []},
        {"excluded": [{"id": None, "name": " ", "note": "x"}]},
        {"excluded": [{"id": None, "name": "...", "note": "x"}]},
        {"excluded": [{"id": None, "name": "\u200b\u3164", "note": "x"}]},
        {"excluded": [{"id": "W123", "name": "A", "note": "x"}]},
        {"excluded": [{"id": "not an id!", "name": "A", "note": "x"}]},
        {"excluded": [{"id": None, "name": "A"}]},
        {"excluded": [{"id": None, "name": "A", "note": "x", "extra": 1}]},
    ],
)
def test_demo_publishers_config_rejects_bad_entries(data: object) -> None:
    with pytest.raises(ValidationError):
        DemoPublishersConfig.model_validate(data)


def test_demo_publishers_config_normalizes_ids() -> None:
    config = DemoPublishersConfig.model_validate(
        {"excluded": [{"id": "https://openalex.org/p42", "name": "A", "note": "x"}]}
    )

    assert config.exclusions() == PublisherExclusions(
        ids=frozenset({"P42"}), names=frozenset({"a"})
    )


def test_demo_publishers_names_with_the_same_key_are_not_an_error() -> None:
    config = DemoPublishersConfig.model_validate(
        {
            "excluded": [
                {"id": None, "name": "IEEE Computer Society", "note": "x"},
                {"id": None, "name": "IEEE-Computer\u200bSociety", "note": "x"},
                {"id": None, "name": "I.E.E.E.", "note": "x"},
                {"id": None, "name": "IEEE", "note": "x"},
            ]
        }
    )

    assert config.exclusions().names == frozenset({"ieeecomputersociety", "ieee"})


def test_broken_demo_publishers_file_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    (target / "demo_publishers.yaml").write_text("excluded: []\n", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load_domain_config({"RADAR_CONFIG_DIR": str(target)})

    [problem] = caught.value.problems
    assert problem.startswith("demo_publishers.yaml：excluded：")


# --- IEEE 的出版社 ID（2026-11-21：Kevin 用唯讀查詢查到 P4310319808） --------------------------

IEEE_PUBLISHER_ID = "P4310319808"


def test_real_config_has_the_verified_ieee_id() -> None:
    config = load_domain_config({"RADAR_CONFIG_DIR": str(ROOT / "config")})
    entries = {entry.name: entry.id for entry in config.demo_publishers.excluded}

    assert entries == {
        "Institute of Electrical and Electronics Engineers": IEEE_PUBLISHER_ID,
        "IEEE": None,
        "IEEE Computer Society": None,
    }
    assert config.demo_publishers.exclusions().ids == frozenset({IEEE_PUBLISHER_ID})


@pytest.mark.parametrize(
    "ids",
    [
        (IEEE_PUBLISHER_ID,),
        (f"https://openalex.org/{IEEE_PUBLISHER_ID}",),
        ("https://openalex.org/p4310319808",),
        ("P1", IEEE_PUBLISHER_ID),
    ],
)
def test_ieee_id_with_another_name_is_not_eligible(ids: tuple[str, ...]) -> None:
    exclusions = load_domain_config(
        {"RADAR_CONFIG_DIR": str(ROOT / "config")}
    ).demo_publishers.exclusions()

    # 名稱是別的寫法（不在排除清單）：靠 ID 排除。
    assert is_demo_eligible("cc-by", ids, ("Some Other Press",), exclusions) is False
    # 只有 ID、沒有名稱：名稱是必要條件，一樣不可用（ADR-0052 第 4 點）。
    assert is_demo_eligible("cc-by", ids, (), exclusions) is False
    # 對照：同樣的名稱、不是 IEEE 的 ID，是可用的。
    assert is_demo_eligible("cc-by", ("P1",), ("Some Other Press",), exclusions) is True


# --- PublisherExclusions 建立時自己正規化 ---------------------------------------------------


def test_exclusions_normalize_names_and_ids_when_built() -> None:
    built = PublisherExclusions(
        ids=frozenset({"https://openalex.org/p42", " p7 ", " not an id! "}),
        names=frozenset({"IEEE-Computer​Society", "  Excluded ENGINEERING society "}),
    )

    assert built.ids == frozenset({"P42", "P7", "NOT AN ID!"})
    assert built.names == frozenset({"ieeecomputersociety", "excludedengineeringsociety"})
    assert built == PublisherExclusions(
        ids=frozenset({"P42", "P7", "NOT AN ID!"}),
        names=frozenset({"ieeecomputersociety", "excludedengineeringsociety"}),
    )
    # 直接用原字串建的清單，比對結果和設定檔載入的一樣。
    assert built.matches(("P42",), ()) is True
    assert built.matches(("not an id!",), ()) is True
    assert built.matches((), ("IEEE Computer Society",)) is True
    assert is_demo_eligible("cc-by", (), ("Excluded Engineering Society",), built) is False


def test_exclusions_reject_names_or_ids_that_normalize_to_nothing() -> None:
    with pytest.raises(ValueError, match="名稱"):
        PublisherExclusions(ids=frozenset(), names=frozenset({"A", "...​"}))
    with pytest.raises(ValueError, match="ID"):
        PublisherExclusions(ids=frozenset({"  "}), names=frozenset({"A"}))


def test_config_and_direct_construction_give_the_same_exclusions() -> None:
    config = DemoPublishersConfig.model_validate(
        {
            "excluded": [
                {"id": "https://openalex.org/p42", "name": "IEEE-Computer Society", "note": "x"},
                {"id": None, "name": "I.E.E.E.", "note": "x"},
            ]
        }
    )

    assert config.exclusions() == PublisherExclusions(
        ids=frozenset({"https://openalex.org/p42"}),
        names=frozenset({"IEEE-Computer Society", "I.E.E.E."}),
    )
    assert config.exclusions() == PublisherExclusions(
        ids=frozenset({"P42"}), names=frozenset({"ieeecomputersociety", "ieee"})
    )


_RAW_NAMES = st.lists(
    st.one_of(
        st.sampled_from(
            [EXCLUDED_PUBLISHER_NAME, EXCLUDED_PARENT_NAME.swapcase(), "I.E.E.E.", "IEEE​"]
        ),
        st.text(min_size=1, max_size=20),
    ),
    max_size=5,
).filter(lambda names: all(normalize_publisher_name(name) for name in names))
_RAW_IDS = st.lists(
    st.one_of(
        st.sampled_from(["P42", "p42", "https://openalex.org/P9000000002", " w5 "]),
        st.text(min_size=1, max_size=12),
    ),
    max_size=5,
).filter(lambda ids: all(value.strip() for value in ids))


@given(ids=_RAW_IDS, names=_RAW_NAMES)
def test_property_building_twice_is_stable(ids: list[str], names: list[str]) -> None:
    first = PublisherExclusions(ids=frozenset(ids), names=frozenset(names))
    again = PublisherExclusions(ids=frozenset(ids), names=frozenset(names))
    rebuilt = PublisherExclusions(ids=first.ids, names=first.names)

    assert first == again
    # 用正規化後的字串再建一次（等於原字串與正規化後的字串建出來相等）。
    assert rebuilt == first


@given(ids=_RAW_IDS, names=_RAW_NAMES, paper_names=_NAMES)
def test_property_raw_and_normalized_exclusions_match_the_same_papers(
    ids: list[str], names: list[str], paper_names: tuple[str, ...]
) -> None:
    raw = PublisherExclusions(ids=frozenset(ids), names=frozenset(names))
    normalized_names = frozenset(normalize_publisher_name(name) for name in names)

    assert raw.names == normalized_names
    assert raw.matches((), paper_names) == any(
        normalize_publisher_name(name) in normalized_names for name in paper_names
    )
    for name in names:
        assert raw.matches((), (name,)) is True
    # ID 也一樣（#118 審查 G4 補的斷言）：建立時正規化成和比對論文 ID 時同一個鍵，原字串對得上。
    assert raw.ids == frozenset(_expected_id_key(value) for value in ids)
    for value in ids:
        assert raw.matches((value,), ()) is True


def _expected_id_key(value: str) -> str:
    """測試自己照 ADR-0052 寫一份 ID 的比對鍵（不呼叫 policy 的私有函式）。"""
    try:
        return normalize_openalex_id(value)
    except InvalidIdentifierError:
        return value.strip().upper()


# Codex 審查 PR #118 的反例：拿掉分隔字元後 NFKC 會把韓文字母組成音節，只做一輪時要兩次才穩定。
# 韓文字母在畫面上和音節幾乎分不出來，所以用 \u 寫法（#118 審查 L3；值和原本的字面一樣）。
_RECOMPOSING_NAME = "\u1100-\u1161"  # U+1100 初聲 ㄱ（KIYEOK）、連字號、U+1161 中聲 ㅏ（A）
_SYLLABLE = "\uac00"  # U+AC00 音節「가」（上面兩個字母組起來的結果）


def test_name_key_that_recomposes_is_stable_in_one_call() -> None:
    # 字面確實是這幾個 code point（改成 \u 寫法後值沒變）。
    assert [ord(ch) for ch in _RECOMPOSING_NAME] == [0x1100, 0x2D, 0x1161]
    assert [ord(ch) for ch in _SYLLABLE] == [0xAC00]
    assert normalize_publisher_name(_RECOMPOSING_NAME) == normalize_publisher_name(_SYLLABLE)
    assert normalize_publisher_name(_RECOMPOSING_NAME) == _SYLLABLE
    key = normalize_publisher_name(_RECOMPOSING_NAME)
    assert normalize_publisher_name(key) == key


def test_exclusion_written_with_separator_matches_the_composed_name() -> None:
    exclusions = PublisherExclusions(ids=frozenset(), names=frozenset({_RECOMPOSING_NAME}))

    assert exclusions.names == frozenset({_SYLLABLE})
    assert exclusions.matches((), (_SYLLABLE,)) is True
    assert PublisherExclusions(ids=frozenset(), names=exclusions.names) == exclusions


@given(name=st.text(max_size=20))
def test_property_name_key_is_idempotent(name: str) -> None:
    key = normalize_publisher_name(name)

    assert normalize_publisher_name(key) == key


# --- 依期刊 ISSN-L 排除（C3、ADR-0052 更新 2026-11-22） ---------------------------------

IEEE_TEM_ISSN_L = "0018-9391"
# 不在排除清單裡的 IEEE 名稱寫法（前面多一個 The），出版社 ID 也是編的、不在清單裡。
UNLISTED_IEEE_NAME = "The Institute of Electrical and Electronics Engineers"


def test_real_config_excludes_ieee_tem_by_journal_even_with_an_unlisted_name() -> None:
    exclusions = load_domain_config(
        {"RADAR_CONFIG_DIR": str(ROOT / "config")}
    ).demo_publishers.exclusions()

    assert (
        is_demo_eligible(
            "cc-by",
            ("P9000000001",),
            (UNLISTED_IEEE_NAME,),
            exclusions,
            journal_issn_l=IEEE_TEM_ISSN_L,
        )
        is False
    )
    # 對照：同樣的論文換成別的期刊就可用（證明期刊這一道沒有多擋）。
    assert (
        is_demo_eligible(
            "cc-by",
            ("P9000000001",),
            (UNLISTED_IEEE_NAME,),
            exclusions,
            journal_issn_l=ISSN,
        )
        is True
    )


def test_real_config_lists_ieee_tem_as_an_excluded_journal() -> None:
    config = load_domain_config({"RADAR_CONFIG_DIR": str(ROOT / "config")})

    assert [entry.issn_l for entry in config.demo_publishers.excluded_journals] == [IEEE_TEM_ISSN_L]
    assert config.demo_publishers.exclusions().journal_issn_ls == frozenset({IEEE_TEM_ISSN_L})
    assert config.scope_settings().demo_publishers == config.demo_publishers.exclusions()


# 測試用的排除清單多一本編的期刊（出版社與名稱和 TEST_EXCLUSIONS 一樣）。
EXCLUDED_JOURNAL_ISSN_L = "9999-999X"
JOURNAL_EXCLUSIONS = PublisherExclusions(
    ids=TEST_EXCLUSIONS.ids,
    names=TEST_EXCLUSIONS.names,
    journal_issn_ls=frozenset({EXCLUDED_JOURNAL_ISSN_L}),
)


def test_paper_in_an_excluded_journal_is_not_eligible() -> None:
    assert (
        is_demo_eligible(
            "cc-by",
            (OPEN_PUBLISHER_ID,),
            (OPEN_PUBLISHER_NAME,),
            JOURNAL_EXCLUSIONS,
            journal_issn_l=EXCLUDED_JOURNAL_ISSN_L,
        )
        is False
    )
    assert JOURNAL_EXCLUSIONS.matches((), (), journal_issn_l=EXCLUDED_JOURNAL_ISSN_L) is True


@pytest.mark.parametrize("issn_l", [None, ISSN])
@pytest.mark.parametrize(
    ("license", "ids", "names", "expected"),
    [
        ("cc-by", (OPEN_PUBLISHER_ID,), (OPEN_PUBLISHER_NAME,), True),
        ("cc-by", (EXCLUDED_PUBLISHER_ID,), (OPEN_PUBLISHER_NAME,), False),
        ("cc-by", (), (EXCLUDED_PUBLISHER_NAME,), False),
        ("cc-by", (OPEN_PUBLISHER_ID,), (), False),
        ("cc-by-nc", (OPEN_PUBLISHER_ID,), (OPEN_PUBLISHER_NAME,), False),
        (None, (OPEN_PUBLISHER_ID,), (OPEN_PUBLISHER_NAME,), False),
    ],
)
def test_journal_that_is_missing_or_not_listed_keeps_the_original_result(
    issn_l: str | None,
    license: str | None,
    ids: tuple[str, ...],
    names: tuple[str, ...],
    expected: bool,
) -> None:
    # 論文沒有期刊 ISSN-L、或期刊不在清單裡：期刊這一道不算對上，結果和沒有期刊清單時一樣。
    assert (
        is_demo_eligible(license, ids, names, JOURNAL_EXCLUSIONS, journal_issn_l=issn_l) is expected
    )
    assert is_demo_eligible(license, ids, names, TEST_EXCLUSIONS) is expected


@pytest.mark.parametrize("journals", [{}, {"excluded_journals": []}], ids=["omitted", "empty"])
def test_without_a_journal_list_the_journal_is_ignored(journals: dict[str, object]) -> None:
    # 設定檔沒有 excluded_journals（省略鍵或明寫空清單 `[]`，兩種要一樣）：行為和加這一道之前
    # 完全一樣（期刊是什麼都不影響）。
    config = DemoPublishersConfig.model_validate(
        {"excluded": [{"id": None, "name": EXCLUDED_PUBLISHER_NAME, "note": "x"}], **journals}
    )
    exclusions = config.exclusions()

    assert config.excluded_journals == ()
    assert exclusions.journal_issn_ls == frozenset()
    assert exclusions == PublisherExclusions(
        ids=frozenset(), names=frozenset({EXCLUDED_PUBLISHER_NAME})
    )
    for issn_l in (None, ISSN, IEEE_TEM_ISSN_L, EXCLUDED_JOURNAL_ISSN_L):
        assert (
            is_demo_eligible("cc-by", (), (OPEN_PUBLISHER_NAME,), exclusions, journal_issn_l=issn_l)
            is True
        )
        assert (
            is_demo_eligible(
                "cc-by", (), (EXCLUDED_PUBLISHER_NAME,), exclusions, journal_issn_l=issn_l
            )
            is False
        )


@pytest.mark.parametrize("written", ["0018-9391", "00189391", " 0018-9391 ", "ISSN 0018-9391"])
@pytest.mark.parametrize("paper", ["0018-9391", "00189391"])
def test_issn_l_spellings_match(written: str, paper: str) -> None:
    config = DemoPublishersConfig.model_validate(
        {
            "excluded": [{"id": None, "name": "A", "note": "x"}],
            "excluded_journals": [{"issn_l": written, "name": "J", "note": "x"}],
        }
    )

    assert config.excluded_journals[0].issn_l == "0018-9391"
    assert config.exclusions().matches((), (), journal_issn_l=paper) is True


@pytest.mark.parametrize("written", ["0960-085x", "0960085x", "0960-085X", "0960085X"])
@pytest.mark.parametrize("paper", ["0960-085X", "0960-085x", "0960085x"])
def test_issn_l_lowercase_x_matches(written: str, paper: str) -> None:
    exclusions = PublisherExclusions(
        ids=frozenset(), names=frozenset({"A"}), journal_issn_ls=frozenset({written})
    )

    assert exclusions.journal_issn_ls == frozenset({"0960-085X"})
    assert exclusions.matches((), (), journal_issn_l=paper) is True
    assert (
        is_demo_eligible("cc-by", (), (OPEN_PUBLISHER_NAME,), exclusions, journal_issn_l=paper)
        is False
    )


@pytest.mark.parametrize(
    "data",
    [
        {"excluded_journals": [{"issn_l": "not an issn", "name": "J", "note": "x"}]},
        {"excluded_journals": [{"issn_l": "0018-939", "name": "J", "note": "x"}]},
        {"excluded_journals": [{"issn_l": 189391, "name": "J", "note": "x"}]},
        {"excluded_journals": [{"issn_l": "0018-9391", "name": " ", "note": "x"}]},
        {"excluded_journals": [{"issn_l": "0018-9391", "name": "J"}]},
        {"excluded_journals": [{"issn_l": "0018-9391", "name": "J", "note": "x", "extra": 1}]},
    ],
)
def test_excluded_journals_reject_bad_entries(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DemoPublishersConfig.model_validate(
            {"excluded": [{"id": None, "name": "A", "note": "x"}], **data}
        )


def test_excluded_journals_with_the_same_issn_l_are_not_an_error() -> None:
    config = DemoPublishersConfig.model_validate(
        {
            "excluded": [{"id": None, "name": "A", "note": "x"}],
            "excluded_journals": [
                {"issn_l": "0018-9391", "name": "J", "note": "x"},
                {"issn_l": "00189391", "name": "J again", "note": "x"},
            ],
        }
    )

    assert config.exclusions().journal_issn_ls == frozenset({"0018-9391"})


def test_exclusions_reject_issn_l_that_cannot_be_read() -> None:
    with pytest.raises(ValueError, match="ISSN-L"):
        PublisherExclusions(
            ids=frozenset(), names=frozenset({"A"}), journal_issn_ls=frozenset({"nope"})
        )


def test_broken_excluded_journal_in_the_config_file_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    file = target / "demo_publishers.yaml"
    file.write_text(
        file.read_text(encoding="utf-8").replace("issn_l: 0018-9391", "issn_l: 0018-93X1"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as caught:
        load_domain_config({"RADAR_CONFIG_DIR": str(target)})

    [problem] = caught.value.problems
    assert problem.startswith("demo_publishers.yaml：excluded_journals.0.issn_l：")


_RAW_ISSN_LS = st.lists(
    st.builds(
        lambda digits, check, hyphen, prefix: f"{prefix}{digits[:4]}{hyphen}{digits[4:]}{check}",
        st.text(alphabet="0123456789", min_size=7, max_size=7),
        st.sampled_from([*"0123456789", "x", "X"]),
        st.sampled_from(["", "-"]),
        st.sampled_from(["", " ", "ISSN ", "issn-l: "]),
    ),
    max_size=5,
)


@given(ids=_RAW_IDS, names=_RAW_NAMES, issn_ls=_RAW_ISSN_LS)
def test_property_journal_list_built_twice_is_stable(
    ids: list[str], names: list[str], issn_ls: list[str]
) -> None:
    first = PublisherExclusions(
        ids=frozenset(ids), names=frozenset(names), journal_issn_ls=frozenset(issn_ls)
    )
    again = PublisherExclusions(
        ids=frozenset(ids), names=frozenset(names), journal_issn_ls=frozenset(issn_ls)
    )
    rebuilt = PublisherExclusions(
        ids=first.ids, names=first.names, journal_issn_ls=first.journal_issn_ls
    )

    assert first == again
    assert rebuilt == first
    assert first.journal_issn_ls == again.journal_issn_ls == rebuilt.journal_issn_ls
    for issn_l in issn_ls:
        assert first.matches((), (), journal_issn_l=issn_l) is True
