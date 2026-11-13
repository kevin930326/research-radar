"""第一個遷移的功能檢查（純函式）：每一項不符各一條原因。"""

from dataclasses import replace

import pytest

from radar.domain.database_requirements import (
    APP_ROLE,
    OWNER_ROLE,
    DatabaseFeatures,
    DatabaseRequirementsError,
    find_missing_features,
)

OK = DatabaseFeatures(
    has_uuidv7=True,
    pgvector_version="0.8.7",
    has_pg_trgm=True,
    has_app_role=True,
    runs_as_database_owner=True,
)


def test_all_present_means_no_problems() -> None:
    assert find_missing_features(OK) == []


@pytest.mark.parametrize("version", ["0.8.0", "0.8.2", "0.8.7", "0.10.0", "1.0.0", "0.9"])
def test_supported_pgvector_versions(version: str) -> None:
    assert find_missing_features(replace(OK, pgvector_version=version)) == []


def test_missing_uuidv7() -> None:
    problems = find_missing_features(replace(OK, has_uuidv7=False))

    assert len(problems) == 1
    assert "uuidv7()" in problems[0]


def test_missing_pgvector() -> None:
    problems = find_missing_features(replace(OK, pgvector_version=None))

    assert len(problems) == 1
    assert "pgvector" in problems[0]
    assert "0.8.0" in problems[0]


@pytest.mark.parametrize("version", ["0.7.4", "0.5.1", "0.7"])
def test_pgvector_too_old(version: str) -> None:
    problems = find_missing_features(replace(OK, pgvector_version=version))

    assert len(problems) == 1
    assert "低於 0.8.0" in problems[0]
    assert version in problems[0]


@pytest.mark.parametrize("version", ["", "abc", "0.8.0-dev", "v0.8.0", "0.8.0.1", "0..8"])
def test_unreadable_pgvector_version(version: str) -> None:
    problems = find_missing_features(replace(OK, pgvector_version=version))

    assert len(problems) == 1
    assert "無法判讀 pgvector 的版本" in problems[0]


def test_version_is_compared_as_numbers_not_text() -> None:
    # 文字比較的話 "0.10.0" < "0.8.0"。
    assert find_missing_features(replace(OK, pgvector_version="0.10.0")) == []
    assert len(find_missing_features(replace(OK, pgvector_version="0.7.10"))) == 1


def test_missing_pg_trgm() -> None:
    problems = find_missing_features(replace(OK, has_pg_trgm=False))

    assert len(problems) == 1
    assert "pg_trgm" in problems[0]


def test_missing_app_role() -> None:
    problems = find_missing_features(replace(OK, has_app_role=False))

    assert len(problems) == 1
    assert APP_ROLE in problems[0]


def test_not_database_owner() -> None:
    problems = find_missing_features(replace(OK, runs_as_database_owner=False))

    assert len(problems) == 1
    assert OWNER_ROLE in problems[0]
    assert "超級使用者" in problems[0]


def test_every_problem_is_listed() -> None:
    features = DatabaseFeatures(
        has_uuidv7=False,
        pgvector_version=None,
        has_pg_trgm=False,
        has_app_role=False,
        runs_as_database_owner=False,
    )

    problems = find_missing_features(features)

    assert len(problems) == 5
    assert "uuidv7()" in problems[0]
    assert "pgvector" in problems[1]
    assert "pg_trgm" in problems[2]
    assert APP_ROLE in problems[3]
    assert OWNER_ROLE in problems[4]


def test_error_keeps_every_problem() -> None:
    error = DatabaseRequirementsError(["甲", "乙"])

    assert error.problems == ["甲", "乙"]
    assert "甲" in str(error)
    assert "乙" in str(error)
