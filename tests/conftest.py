"""依測試所在的資料夾自動加上 pytest 標記：tests/unit → unit，依此類推。

網路：pyproject.toml 的 `--disable-socket` 讓所有測試預設不能建網路連線（pytest-socket）；
integration 另外加 `allow_hosts`，只放行連到本機（127.0.0.1、::1、localhost）。
"""

from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).parent
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
