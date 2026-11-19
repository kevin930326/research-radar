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


_LICENSES = st.one_of(
    st.none(),
    st.sampled_from(["cc-by", "cc-by-sa", "public-domain", "cc0", "cc-by-nc", "other-oa"]),
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


@given(license=_LICENSES, ids=_IDS, names=_NAMES)
def test_property_license_outside_the_three_is_never_eligible(
    license: str | None, ids: tuple[str, ...], names: tuple[str, ...]
) -> None:
    result = is_demo_eligible(license, ids, names, TEST_EXCLUSIONS)
    if openalex_license(license) not in DEMO_ELIGIBLE_LICENSES:
        assert result is False


@given(license=_LICENSES, ids=_IDS, names=_NAMES)
def test_property_true_needs_allowed_license_and_publisher_not_excluded(
    license: str | None, ids: tuple[str, ...], names: tuple[str, ...]
) -> None:
    # True 的必要條件：三種授權之一、有出版社欄位、沒有任何值在排除清單裡；而且拿掉排除清單
    # （None）一律 False。
    if is_demo_eligible(license, ids, names, TEST_EXCLUSIONS):
        assert openalex_license(license) in DEMO_ELIGIBLE_LICENSES
        assert ids or names
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


def test_broken_demo_publishers_file_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    (target / "demo_publishers.yaml").write_text("excluded: []\n", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load_domain_config({"RADAR_CONFIG_DIR": str(target)})

    [problem] = caught.value.problems
    assert problem.startswith("demo_publishers.yaml：excluded：")
