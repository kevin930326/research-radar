"""依測試所在的資料夾自動加上 pytest 標記：tests/unit → unit，依此類推。"""

from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).parent
MARKED_DIRS = ("unit", "integration", "arch")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if not item.path.is_relative_to(TESTS_ROOT):
            continue
        relative = item.path.relative_to(TESTS_ROOT)
        top = relative.parts[0] if len(relative.parts) > 1 else ""
        if top in MARKED_DIRS:
            item.add_marker(getattr(pytest.mark, top))
