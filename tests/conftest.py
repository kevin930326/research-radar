"""依測試所在的資料夾自動加上 pytest 標記：tests/unit → unit，依此類推。

網路：pyproject.toml 的 `--disable-socket` 讓所有測試預設不能建網路連線（pytest-socket）；
integration 另外加 `allow_hosts`，只放行連到本機（127.0.0.1、::1、localhost）。

資料庫：integration 與 arch 用的測試資料庫 fixture 在這裡（做法見 `tests/database.py`）。

錄製的外部 API 回應：`vcr_config`、`vcr_cassette_dir` 給 pytest-recording；設定只有
`tests/vcr_settings.py` 一份，錄好的檔都在 `tests/cassettes/`。
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from radar.domain import prompt_hashes
from tests import vcr_settings
from tests.database import TestDatabase, create_database, drop_database, migrate

TESTS_ROOT = Path(__file__).parent
CASSETTES_DIR = TESTS_ROOT / "cassettes"
MARKED_DIRS = ("unit", "integration", "arch")
LOCAL_HOSTS = ["127.0.0.1", "::1", "localhost"]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if not item.path.is_relative_to(TESTS_ROOT):
            continue
        relative = item.path.relative_to(TESTS_ROOT)
        top = relative.parts[0] if len(relative.parts) > 1 else ""
        if top not in MARKED_DIRS:
            # CI 分三次、依標記跑；沒有標記的測試在 CI 會被默默略過，所以直接擋下。
            raise pytest.UsageError(
                f"{item.nodeid}：測試要放在 tests/ 底下的 {'、'.join(MARKED_DIRS)} 其中一個資料夾"
            )
        item.add_marker(getattr(pytest.mark, top))
        if top == "integration":
            # 只限制 connect：建 socket、bind 本機埠（例如找空埠）仍可以。
            item.add_marker(pytest.mark.allow_hosts(LOCAL_HOSTS))


@pytest.fixture(autouse=True)
def restore_prompt_hashes() -> Iterator[None]:
    """每個測試結束都還原 `radar/domain/prompt_hashes.py` 的登記表。

    測試會暫時登記自己編的 prompt 檔（`tests/llm_support.py`、`tests/prompt_support.py`）；
    `radar/` 裡只有 policy 與 prompt loader 讀它，沒有程式改它（架構測試守著）。
    """
    saved = (prompt_hashes.CURRENT_PROMPT_VERSIONS, prompt_hashes.PROMPT_FILE_HASHES)
    try:
        yield
    finally:
        prompt_hashes.CURRENT_PROMPT_VERSIONS, prompt_hashes.PROMPT_FILE_HASHES = saved


@pytest.fixture
def fresh_database() -> Iterator[TestDatabase]:
    """全新的資料庫：有兩個擴充套件、還沒遷移。"""
    database = create_database(with_extensions=True)
    try:
        yield database
    finally:
        drop_database(database)


@pytest.fixture
def bare_database() -> Iterator[TestDatabase]:
    """從 template0 建、沒有任何擴充套件的資料庫。"""
    database = create_database(with_extensions=False)
    try:
        yield database
    finally:
        drop_database(database)


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[TestDatabase]:
    """已遷移到最新版本的資料庫，整個測試階段共用：只能讀，不要在裡面留下東西。"""
    database = create_database(with_extensions=True)
    try:
        migrate(database)
        yield database
    finally:
        drop_database(database)


@pytest.fixture
def vcr_config() -> dict[str, Any]:
    """pytest-recording 的錄製設定（唯一一份在 `tests/vcr_settings.py`）。"""
    return vcr_settings.vcr_config()


@pytest.fixture(scope="module")
def vcr_cassette_dir() -> str:
    """錄好的回應都放 `tests/cassettes/`。

    不照 pytest-recording 預設的「每個測試檔一個資料夾」。
    """
    return str(CASSETTES_DIR)
