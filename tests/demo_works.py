"""示範站可用（Story 3.7a）的測試資料：自己編的 OpenAlex work（不是真的論文、真的摘要）。

形狀照 OpenAlex 官方文件的 location 與 source 欄位（`primary_location.license`、
`primary_location.source.host_organization` 等），出版社 ID 與名稱都是編的。
"""

import json
from typing import Any

from radar.domain.policy import PublisherExclusions, normalize_publisher_name

# 編的出版社：一家可以放（Example Open Press）、一家在測試用的排除清單裡（Excluded Society）。
OPEN_PUBLISHER_ID = "https://openalex.org/P9000000001"
OPEN_PUBLISHER_NAME = "Example Open Press"
EXCLUDED_PUBLISHER_ID = "https://openalex.org/P9000000002"
EXCLUDED_PUBLISHER_NAME = "Excluded Engineering Society"
EXCLUDED_PARENT_NAME = "Excluded Engineering Group"

TEST_EXCLUSIONS = PublisherExclusions(
    ids=frozenset({"P9000000002"}),
    names=frozenset(
        {
            normalize_publisher_name(EXCLUDED_PUBLISHER_NAME),
            normalize_publisher_name(EXCLUDED_PARENT_NAME),
        }
    ),
)

_MISSING = object()


def demo_work(
    number: int,
    *,
    issn_l: str,
    license: object = "cc-by",
    publisher_id: object = OPEN_PUBLISHER_ID,
    publisher_name: object = OPEN_PUBLISHER_NAME,
    lineage: object = _MISSING,
    lineage_names: object = _MISSING,
    best_oa_license: object = None,
    other_location_licenses: tuple[object, ...] = (),
    abstract: bool = True,
    source: bool = True,
    year: int = 2022,
) -> dict[str, Any]:
    """一篇編的 OpenAlex work（JSON 物件）。`source=False`＝`primary_location.source` 是 null。

    `lineage`／`lineage_names` 沒給時是 [直屬的值]（照 OpenAlex 的寫法，自己也在 lineage 裡）。
    """
    primary_source: dict[str, Any] | None = None
    if source:
        primary_source = {
            "id": "https://openalex.org/S9000000001",
            "display_name": "Journal of Made Up Studies",
            "issn_l": issn_l,
            "issn": [issn_l],
            "type": "journal",
            "host_organization": publisher_id,
            "host_organization_name": publisher_name,
            "host_organization_lineage": _lineage(lineage, publisher_id),
            "host_organization_lineage_names": _lineage(lineage_names, publisher_name),
        }
    primary = {
        "is_oa": license is not None,
        "landing_page_url": f"https://doi.org/10.9999/demo.{number}",
        "source": primary_source,
        "license": license,
        "version": "publishedVersion",
    }
    best_oa = (
        None
        if best_oa_license is None
        else {
            "is_oa": True,
            "source": {"id": "https://openalex.org/S9000000009", "display_name": "Repo"},
            "license": best_oa_license,
            "version": "acceptedVersion",
        }
    )
    locations = [primary]
    if best_oa is not None:
        locations.append(best_oa)
    locations.extend(
        {"is_oa": True, "source": None, "license": other, "version": "submittedVersion"}
        for other in other_location_licenses
    )
    work: dict[str, Any] = {
        "id": f"https://openalex.org/W8{number:08d}",
        "doi": f"https://doi.org/10.9999/demo.{number}",
        "title": f"Made up demo study {number}",
        "display_name": f"Made up demo study {number}",
        "publication_year": year,
        "type": "article",
        "authorships": [{"author": {"display_name": "Test Author"}}],
        "biblio": {"volume": "1", "issue": "1", "first_page": "1", "last_page": "9"},
        "primary_location": primary,
        "best_oa_location": best_oa,
        "locations": locations,
        "abstract_inverted_index": (
            {"Made": [0], "up": [1], "abstract": [2], str(number): [3]} if abstract else None
        ),
        "referenced_works": [],
        "cited_by_count": 0,
        "keywords": [],
    }
    return work


def _lineage(given: object, direct: object) -> object:
    if given is not _MISSING:
        return given
    return [] if direct is None else [direct]


def demo_work_bytes(work: dict[str, Any]) -> bytes:
    return json.dumps(work, ensure_ascii=False).encode("utf-8")
